"""
core/kernel_telemetry.py — JARVIS V55.0 OMNI-REDUNDANCY
Kernel-level ETW telemetry bridge. Subscribes to Microsoft-Windows-Kernel-Process
in its own named ETW session (distinct from etw_monitor) for REAL-TIME:
  - Process creation (EventID 1): LOLBin and suspicious-path detection.
  - Image load (EventID 5): DLL injection / side-loading from user-writable paths,
    and reflective-load detection (image path not on disk).
Complements cmd_analyser (which polls 4688) with kernel resolution and no polling
lag. Bridges from a daemon thread to the asyncio loop via run_coroutine_threadsafe.
Watchdog Pattern: dormant if pywintrace missing or not elevated.
"""
from __future__ import annotations
import asyncio, logging, os, re, threading, time

logger = logging.getLogger("jarvis.kernel_telemetry")

_IS_WINDOWS = os.name == "nt"

try:
    from etw import ETW, ProviderInfo, GUID
    _PYWINTRACE_OK = True
except Exception:
    ETW = ProviderInfo = GUID = None; _PYWINTRACE_OK = False

try:
    import psutil; _PSUTIL_OK = True
except Exception:
    psutil = None; _PSUTIL_OK = False

_KERNEL_PROCESS_GUID = "{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}"
_SUSP_DIRS = re.compile(r"\\temp\\|\\tmp\\|\\downloads\\|\\appdata\\local\\temp\\|\\users\\public\\",
                        re.IGNORECASE)
_LOLBINS = {"mshta.exe", "certutil.exe", "rundll32.exe", "regsvr32.exe", "bitsadmin.exe",
            "wmic.exe", "installutil.exe", "msbuild.exe", "cscript.exe", "wscript.exe",
            "cmstp.exe", "mavinject.exe", "forfiles.exe", "scrcons.exe"}

_correlator = None
_loop = None
_stop = threading.Event()
_ALERT_TTL = 60
_alerted = {}


def _is_admin():
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


async def _dispatch(event):
    if _correlator is None:
        return
    try:
        if hasattr(_correlator, "ingest_event"):
            await _correlator.ingest_event(event)
        elif hasattr(_correlator, "add_event"):
            r = _correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
    except Exception as e:
        logger.error("kernel_telemetry: dispatch failed: %s", e)


def _emit(kind, sev, attck, extra):
    now = time.time()
    key = (kind, extra.get("image", "") or extra.get("pid", ""))
    if now - _alerted.get(key, 0) < _ALERT_TTL:
        return
    _alerted[key] = now
    event = {"source": "kernel_telemetry", "type": kind, "severity": sev,
             "attck": attck, "ts": now}
    event.update(extra)
    if _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_dispatch(event), _loop)
        except Exception:
            pass


def _proc_name(pid):
    if _PSUTIL_OK and pid and str(pid).isdigit():
        try:
            return psutil.Process(int(pid)).name()
        except Exception:
            pass
    return None


def _analyze(record):
    try:
        ev = record[1] if isinstance(record, (tuple, list)) and len(record) > 1 else record
        if not isinstance(ev, dict):
            return
    except Exception:
        return
    try:
        eid = int(ev.get("EventID") or ev.get("Id") or 0)
        props = {k.lower(): str(v) for k, v in ev.items() if v is not None}
    except Exception:
        return
    if eid == 1:                           # process create
        img = props.get("imagename") or props.get("imagefilename") or ""
        cmd = props.get("commandline") or ""
        pid = props.get("processid", "?")
        ppid = props.get("parentprocessid", "?")
        pname = _proc_name(ppid)
        base = os.path.basename(img).lower()
        reasons = []
        if base in _LOLBINS:
            reasons.append(f"LOLBin process: {base}")
        if img and _SUSP_DIRS.search(img):
            reasons.append(f"process from user-writable path: {img}")
        if reasons:
            _emit("kernel_process_create", 8.5, ["T1059", "T1218"],
                  {"image": img, "cmdline": cmd[:400], "pid": pid,
                   "ppid": ppid, "parent_name": pname, "reasons": reasons})
    elif eid == 5:                         # image load (DLL)
        img = props.get("imagename") or props.get("dllname") or ""
        pid = props.get("processid", "?")
        if not img:
            return
        reasons = []
        if _SUSP_DIRS.search(img):
            reasons.append(f"DLL loaded from suspicious path: {img}")
        try:
            if not os.path.exists(img):
                reasons.append(f"image path not on disk — reflective load indicator: {img}")
        except Exception:
            pass
        if reasons:
            pname = _proc_name(pid)
            _emit("kernel_image_load", 9.0, ["T1055", "T1574"],
                  {"image": img, "pid": pid, "proc_name": pname, "reasons": reasons})


def _etw_thread():
    try:
        providers = [ProviderInfo("Microsoft-Windows-Kernel-Process", GUID(_KERNEL_PROCESS_GUID))]
        job = ETW(providers=providers, event_callback=_analyze,
                  session_name="JARVIS-KernelTelemetry")
        job.start()
        while not _stop.is_set():
            time.sleep(0.5)
        job.stop()
    except Exception as e:
        logger.error("kernel_telemetry: ETW thread crashed: %s", e)


async def start(correlator=None):
    global _correlator, _loop
    _correlator = correlator; _loop = asyncio.get_running_loop()
    if not _IS_WINDOWS:
        logger.warning("KERNEL_TELEMETRY: non-Windows — dormant")
        await asyncio.Event().wait(); return
    if not _PYWINTRACE_OK:
        logger.warning("KERNEL_TELEMETRY: pywintrace unavailable — dormant (pip install pywintrace)")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("KERNEL_TELEMETRY: not elevated — dormant")
        await asyncio.Event().wait(); return
    _stop.clear()
    threading.Thread(target=_etw_thread, name="kernel-etw", daemon=True).start()
    logger.info("KERNEL_TELEMETRY: armed — real-time kernel process-create + DLL image-load via ETW")
    try:
        await asyncio.Event().wait()
    finally:
        _stop.set()
