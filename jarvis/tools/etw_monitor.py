"""
tools/etw_monitor.py — Kernel Telemetry Ingestion Layer (v26.0).

ETW providers monitored:
  Microsoft-Windows-Kernel-Process  {22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}
  Microsoft-Windows-Kernel-Network  {7DD42A49-5329-4832-8DFD-43D979153A88}

Architecture:
  1. start_etw_monitor() spawns _etw_trace_loop in a daemon thread.
  2. The thread calls pywintrace (if installed) or ctypes OpenTrace/ProcessTrace.
  3. Each ETW event is pushed to asyncio.Queue via loop.call_soon_threadsafe.
  4. The async consumer in start_etw_monitor() awaits the queue and broadcasts.
  The asyncio event loop is never blocked.

v26.0 fixes:
  - threading.Event pair (_etw_ready / _etw_failed) signals async consumer
    whether init succeeded before entering the queue loop.
    Prevents "Task was destroyed but it is pending" when ETW init fails.
  - TRACEHANDLE → ctypes.c_uint64 (c_ulong is 32-bit on Windows, causes OverflowError).
  - EnableTraceEx2.argtypes explicitly typed with c_ulonglong for MatchAnyKeyword/
    MatchAllKeyword (args 5+6), preventing OverflowError on 0xFFFFFFFFFFFFFFFF.
  - All ULONG/HANDLE fields use wintypes equivalents.
"""

import asyncio
import threading

from loguru import logger
from core.events import make_event

_PROVIDER_KERNEL_PROCESS = "Microsoft-Windows-Kernel-Process"
_PROVIDER_KERNEL_NETWORK  = "Microsoft-Windows-Kernel-Network"
_GUID_KERNEL_PROCESS = "{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}"
_GUID_KERNEL_NETWORK  = "{7DD42A49-5329-4832-8DFD-43D979153A88}"

_SUSPICIOUS_EIDS: frozenset[int] = frozenset({
    1,    # ProcessStart
    2,    # ProcessStop
    5,    # ImageLoad (DLL injection vector)
    9,    # PageFaultCopyOnWrite (potential code injection)
    15,   # NtMapViewOfSection (process hollowing)
    30,   # RemoteThreadCreate (classic injection)
})

# Threading events to signal init outcome to the async consumer
_etw_ready  = threading.Event()
_etw_failed = threading.Event()


def _classify_event(event_id: int, data: dict) -> str:
    _MAP = {
        5:  "Suspicious DLL load / image injection vector",
        9:  "CopyOnWrite page fault — possible code injection staging",
        15: "NtMapViewOfSection — process hollowing or mapping anomaly",
        30: "Remote thread creation detected — classic code injection",
    }
    if event_id in _MAP:
        return _MAP[event_id]
    name = data.get("ImageName") or data.get("ProcessName") or ""
    if name:
        return f"Kernel process event (EID={event_id}) on {name}"
    return f"Kernel event EID={event_id}"


# ── pywintrace implementation ─────────────────────────────────────────────────

def _etw_pywintrace(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    import etw  # type: ignore[import]

    def _callback(event_tuple):
        try:
            if isinstance(event_tuple, (list, tuple)) and len(event_tuple) >= 2:
                eid = int(event_tuple[0]) if event_tuple[0] is not None else 0
                data = event_tuple[-1] if isinstance(event_tuple[-1], dict) else {}
            elif isinstance(event_tuple, dict):
                eid = int(event_tuple.get("EventId", 0))
                data = event_tuple
            else:
                return

            pid  = int(data.get("ProcessId", 0) or data.get("TargetProcessId", 0))
            name = str(data.get("ImageName") or data.get("ProcessName") or "unknown")

            if eid not in _SUSPICIOUS_EIDS:
                return

            loop.call_soon_threadsafe(queue.put_nowait, make_event(
                "etw_threat_event",
                pid=pid,
                process_name=name,
                event_id=eid,
                description=_classify_event(eid, data),
            ))
        except Exception as exc:
            logger.warning(f"ETW pywintrace callback: {exc}")

    providers = []
    try:
        from etw.GUID import GUID as _EtwGUID   # type: ignore[import]
        providers = [
            etw.ProviderInfo(_PROVIDER_KERNEL_PROCESS, _EtwGUID(_GUID_KERNEL_PROCESS)),
            etw.ProviderInfo(_PROVIDER_KERNEL_NETWORK,  _EtwGUID(_GUID_KERNEL_NETWORK)),
        ]
    except AttributeError:
        pass

    if providers:
        consumer = etw.ETW(providers=providers, event_callback=_callback)
    else:
        consumer = etw.ETW(event_callback=_callback)

    # Signal ready before the blocking start() call
    _etw_ready.set()
    consumer.start()


# ── ctypes implementation (OpenTrace / ProcessTrace / CloseTrace) ─────────────

def _etw_ctypes(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """
    ETW real-time consumer via ctypes Windows APIs.

    Key fixes (v26.0):
    - TRACEHANDLE → c_uint64 (c_ulong is 32-bit on Windows x64)
    - EnableTraceEx2.argtypes uses c_ulonglong for MatchAnyKeyword/MatchAllKeyword
    - All ULONG fields use wintypes.ULONG for cross-arch safety
    """
    import ctypes
    import ctypes.wintypes as wt
    import platform

    if platform.system() != "Windows":
        logger.warning("ETW ctypes: not running on Windows — skipping")
        return

    advapi32 = ctypes.WinDLL("advapi32")

    c_ulong     = ctypes.c_ulong
    c_ushort    = ctypes.c_ushort
    c_ubyte     = ctypes.c_ubyte
    c_uint64    = ctypes.c_uint64
    c_longlong  = ctypes.c_longlong
    c_ulonglong = ctypes.c_ulonglong
    c_long      = ctypes.c_long
    c_void_p    = ctypes.c_void_p
    c_byte      = ctypes.c_byte

    # TRACEHANDLE must be c_uint64 — c_ulong is only 32-bit on Windows
    TRACEHANDLE          = c_uint64
    INVALID_TRACEHANDLE  = c_uint64(-1).value
    PROCESS_TRACE_MODE_RT  = 0x00000100
    PROCESS_TRACE_MODE_ER  = 0x10000000
    EVENT_TRACE_RT_MODE    = 0x00000100
    WNODE_FLAG_TRACED_GUID = 0x00020000
    EVENT_CONTROL_CODE_ENABLE = 1
    TRACE_LEVEL_VERBOSE    = 5

    # ── Structures ────────────────────────────────────────────────────────────

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                    ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def _parse_guid(s: str) -> GUID:
        s = s.strip("{}")
        p = s.split("-")
        g = GUID()
        g.Data1 = int(p[0], 16)
        g.Data2 = int(p[1], 16)
        g.Data3 = int(p[2], 16)
        b = bytes.fromhex(p[3] + p[4])
        for i, v in enumerate(b):
            g.Data4[i] = v
        return g

    class WNODE_HEADER(ctypes.Structure):
        _fields_ = [
            ("BufferSize",        wt.ULONG),
            ("ProviderId",        wt.ULONG),
            ("HistoricalContext", c_ulonglong),
            ("TimeStamp",         c_longlong),
            ("Guid",              GUID),
            ("ClientContext",     wt.ULONG),
            ("Flags",             wt.ULONG),
        ]

    class EVENT_TRACE_PROPERTIES(ctypes.Structure):
        _fields_ = [
            ("Wnode",                  WNODE_HEADER),
            ("BufferSize",             wt.ULONG),
            ("MinimumBuffers",         wt.ULONG),
            ("MaximumBuffers",         wt.ULONG),
            ("MaximumFileSize",        wt.ULONG),
            ("LogFileMode",            wt.ULONG),
            ("FlushTimer",             wt.ULONG),
            ("EnableFlags",            wt.ULONG),
            ("AgeLimit",               c_long),
            ("NumberOfBuffers",        wt.ULONG),
            ("FreeBuffers",            wt.ULONG),
            ("EventsLost",             wt.ULONG),
            ("BuffersWritten",         wt.ULONG),
            ("LogBuffersLost",         wt.ULONG),
            ("RealTimeBuffersLost",    wt.ULONG),
            ("LoggerThreadId",         wt.HANDLE),
            ("LogFileNameOffset",      wt.ULONG),
            ("LoggerNameOffset",       wt.ULONG),
        ]

    class EVENT_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("Id", c_ushort), ("Version", c_ubyte), ("Channel", c_ubyte),
            ("Level", c_ubyte), ("Opcode", c_ubyte), ("Task", c_ushort),
            ("Keyword", c_ulonglong),
        ]

    class EVENT_HEADER(ctypes.Structure):
        _fields_ = [
            ("Size",            c_ushort),
            ("HeaderType",      c_ushort),
            ("Flags",           c_ushort),
            ("EventProperty",   c_ushort),
            ("ThreadId",        wt.ULONG),
            ("ProcessId",       wt.ULONG),
            ("TimeStamp",       c_longlong),
            ("ProviderId",      GUID),
            ("EventDescriptor", EVENT_DESCRIPTOR),
            ("KernelTime",      wt.ULONG),
            ("UserTime",        wt.ULONG),
            ("ActivityId",      GUID),
        ]

    class ETW_BUFFER_CONTEXT(ctypes.Structure):
        _fields_ = [("ProcessorIndex", c_ushort), ("LoggerId", c_ushort)]

    class EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("EventHeader",       EVENT_HEADER),
            ("BufferContext",      ETW_BUFFER_CONTEXT),
            ("ExtendedDataCount", c_ushort),
            ("UserDataLength",    c_ushort),
            ("ExtendedData",      c_void_p),
            ("UserData",          c_void_p),
            ("UserContext",       c_void_p),
        ]

    class _TRACE_LOGFILE_HEADER(ctypes.Structure):
        _fields_ = [
            ("BufferSize",          wt.ULONG),
            ("Version",             wt.ULONG),
            ("ProviderVersion",     wt.ULONG),
            ("NumberOfProcessors",  wt.ULONG),
            ("EndTime",             c_longlong),
            ("TimerResolution",     wt.ULONG),
            ("MaximumFileSize",     wt.ULONG),
            ("LogFileMode",         wt.ULONG),
            ("BuffersWritten",      wt.ULONG),
            ("LogInstanceGuid",     GUID),
            ("LoggerName2",         c_void_p),
            ("LogFileName2",        c_void_p),
            ("TimeZone",            c_byte * 172),
            ("BootTime",            c_longlong),
            ("PerfFreq",            c_longlong),
            ("StartTime",           c_longlong),
            ("ReservedFlags",       wt.ULONG),
            ("BuffersLost",         wt.ULONG),
        ]

    class _EVENT_TRACE_LOGFILE(ctypes.Structure):
        _fields_ = [
            ("LogFileName",         c_void_p),
            ("LoggerName",          c_void_p),
            ("CurrentTime",         c_longlong),
            ("BuffersRead",         wt.ULONG),
            ("ProcessTraceMode",    wt.ULONG),
            ("CurrentEvent",        c_byte * 88),
            ("LogfileHeader",       _TRACE_LOGFILE_HEADER),
            ("BufferCallback",      c_void_p),
            ("BufferSize",          wt.ULONG),
            ("Filled",              wt.ULONG),
            ("EventsLost",          wt.ULONG),
            ("EventRecordCallback", c_void_p),
            ("IsKernelTrace",       wt.ULONG),
            ("Context",             c_void_p),
        ]

    class ENABLE_TRACE_PARAMETERS(ctypes.Structure):
        _fields_ = [
            ("Version",           wt.ULONG),
            ("EnableProperty",    wt.ULONG),
            ("ControlFlags",      wt.ULONG),
            ("SourceId",          GUID),
            ("EnableFilterDesc",  c_void_p),
            ("FilterDescCount",   wt.ULONG),
        ]

    EventRecordCallbackType = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))

    # ── ETW session setup ─────────────────────────────────────────────────────

    try:
        SESSION_NAME  = "JARVISKernelETW"
        props_base_sz = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
        name_wbytes   = (SESSION_NAME + "\x00").encode("utf-16-le")
        buf_sz        = props_base_sz + len(name_wbytes)
        props_buf     = (ctypes.c_char * buf_sz)()

        props = ctypes.cast(props_buf, ctypes.POINTER(EVENT_TRACE_PROPERTIES)).contents
        props.Wnode.BufferSize  = wt.ULONG(buf_sz)
        props.Wnode.Flags       = wt.ULONG(WNODE_FLAG_TRACED_GUID)
        props.LogFileMode       = wt.ULONG(EVENT_TRACE_RT_MODE)
        props.LoggerNameOffset  = wt.ULONG(props_base_sz)

        ctypes.memmove(
            ctypes.addressof(props_buf) + props_base_sz,
            name_wbytes,
            len(name_wbytes),
        )

        trace_handle = TRACEHANDLE(0)

        StartTraceW = advapi32.StartTraceW
        StartTraceW.restype  = wt.ULONG
        StartTraceW.argtypes = [
            ctypes.POINTER(TRACEHANDLE),
            ctypes.c_wchar_p,
            ctypes.c_void_p,
        ]
        ret = StartTraceW(
            ctypes.byref(trace_handle),
            SESSION_NAME,
            ctypes.cast(props_buf, ctypes.c_void_p),
        )
        if ret not in (0, 183):
            logger.warning(f"ETW ctypes: StartTraceW returned {ret} — session may need elevation")
            return

        # ── Enable providers ──────────────────────────────────────────────────

        EnableTraceEx2 = advapi32.EnableTraceEx2
        EnableTraceEx2.restype  = wt.ULONG
        # Explicit argtypes prevent OverflowError: args 5+6 are ULONGLONG (64-bit unsigned)
        EnableTraceEx2.argtypes = [
            TRACEHANDLE,                             # TraceHandle
            ctypes.POINTER(GUID),                    # ProviderId
            wt.ULONG,                                # ControlCode
            c_ubyte,                                 # Level
            c_ulonglong,                             # MatchAnyKeyword  ← 64-bit unsigned
            c_ulonglong,                             # MatchAllKeyword
            wt.ULONG,                                # Timeout
            ctypes.POINTER(ENABLE_TRACE_PARAMETERS), # EnableParameters
        ]

        for guid_str in (_GUID_KERNEL_PROCESS, _GUID_KERNEL_NETWORK):
            provider_guid = _parse_guid(guid_str)
            params = ENABLE_TRACE_PARAMETERS()
            params.Version = wt.ULONG(2)
            EnableTraceEx2(
                trace_handle,
                ctypes.byref(provider_guid),
                EVENT_CONTROL_CODE_ENABLE,
                TRACE_LEVEL_VERBOSE,
                c_ulonglong(0xFFFFFFFFFFFFFFFF),
                c_ulonglong(0),
                wt.ULONG(0),
                ctypes.byref(params),
            )

        # ── Callback and OpenTrace ─────────────────────────────────────────────

        def _event_record_callback(rec_ptr):
            try:
                rec = rec_ptr.contents
                pid = rec.EventHeader.ProcessId
                eid = rec.EventHeader.EventDescriptor.Id
                if eid not in _SUSPICIOUS_EIDS:
                    return
                loop.call_soon_threadsafe(queue.put_nowait, make_event(
                    "etw_threat_event",
                    pid=pid,
                    process_name="unknown",
                    event_id=eid,
                    description=_classify_event(eid, {}),
                ))
            except Exception:
                pass

        cb = EventRecordCallbackType(_event_record_callback)

        session_name_buf = ctypes.create_unicode_buffer(SESSION_NAME)
        logfile = _EVENT_TRACE_LOGFILE()
        logfile.LoggerName          = ctypes.cast(session_name_buf, ctypes.c_void_p).value
        logfile.ProcessTraceMode    = wt.ULONG(PROCESS_TRACE_MODE_RT | PROCESS_TRACE_MODE_ER)
        logfile.EventRecordCallback = ctypes.cast(cb, ctypes.c_void_p).value

        OpenTraceW = advapi32.OpenTraceW
        OpenTraceW.restype = TRACEHANDLE
        consumer_handle = OpenTraceW(ctypes.byref(logfile))
        if consumer_handle == INVALID_TRACEHANDLE:
            err = ctypes.get_last_error()
            logger.warning(f"ETW ctypes: OpenTraceW failed (error={err})")
            return

        # Init complete — signal the async consumer before blocking
        _etw_ready.set()

        ProcessTrace = advapi32.ProcessTrace
        ProcessTrace.restype = wt.ULONG
        handles = (TRACEHANDLE * 1)(consumer_handle)
        ProcessTrace(handles, 1, None, None)   # blocks until CloseTrace

        advapi32.CloseTrace(consumer_handle)

    except Exception as exc:
        logger.warning(f"ETW ctypes: fatal — {exc}")
        raise   # re-raise so _etw_trace_loop catches and sets _etw_failed


# ── ETW trace loop (daemon thread target) ────────────────────────────────────

def _etw_trace_loop(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """
    Runs in daemon thread. Tries pywintrace first; falls back to ctypes.
    Sets _etw_ready on successful init or _etw_failed on exception.
    """
    try:
        try:
            _etw_pywintrace(loop, queue)
            return  # pywintrace ran and returned (trace stopped)
        except ImportError:
            logger.info("ETW: pywintrace not installed — using ctypes fallback")
        except Exception as exc:
            logger.warning(f"ETW pywintrace error: {exc}")
            raise  # propagate to outer except

        _etw_ctypes(loop, queue)

    except Exception as exc:
        logger.error(f"ETW: initialization failed: {exc}")
        _etw_failed.set()


# ── Public entry point ────────────────────────────────────────────────────────

async def start_etw_monitor(broadcast_fn) -> None:
    """
    Launch ETW daemon thread and async consumer queue.

    Waits up to 10s for _etw_ready before entering the consumer loop.
    Exits cleanly (no pending task) if init fails or times out.
    """
    from core.telemetry_auth import make_signed_broadcaster
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "etw")
    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    thread = threading.Thread(
        target=_etw_trace_loop,
        args=(loop, queue),
        daemon=True,
        name="ETWTraceLoop",
    )
    thread.start()

    # Wait up to 10s for init result — prevents hanging consumer on failure
    ready = await loop.run_in_executor(
        None, lambda: _etw_ready.wait(10)
    )
    if _etw_failed.is_set() or not ready:
        logger.warning("ETW: initialization failed on this host — monitor disabled")
        return   # exit cleanly, no pending task

    logger.info("ETW: kernel telemetry started (Kernel-Process + Kernel-Network)")

    while True:
        event = await queue.get()
        await broadcast_fn(event)
        # v33.0 — process injection sub-technique classification
        eid = event.get("event_id")
        if eid in {1, 3, 8, 10, 25, 30}:
            pid = event.get("pid", 0)
            if pid:
                try:
                    from tools.injection_classifier import analyze_and_broadcast
                    asyncio.create_task(analyze_and_broadcast(pid, event, broadcast_fn))
                except Exception:
                    pass
        # v43.0 — feed purple coordinator on injection-class ETW events
        if eid in {5, 9, 15, 30}:
            try:
                from core.purple_coordinator import register_detection_event
                # Map suspicious EIDs back to MITRE techniques covered
                tech_map = {
                    5:  "T1055",        # ImageLoad / DLL injection vector
                    9:  "T1055.012",    # PageFaultCopyOnWrite (process hollowing)
                    15: "T1055.012",    # NtMapViewOfSection (hollowing)
                    30: "T1055.003",    # RemoteThreadCreate
                }
                technique_id = tech_map.get(eid)
                if technique_id:
                    asyncio.create_task(register_detection_event(
                        technique_id, "etw", broadcast_fn,
                    ))
            except Exception:
                pass
