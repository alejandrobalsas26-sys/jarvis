"""
tools/memory_hunter.py — Native live process memory forensics (v39.0).

Uses MiniDumpWriteDump via ctypes for targeted memory capture.
SeDebugPrivilege acquired via AdjustTokenPrivileges.
Dump flags: MiniDumpWithPrivateReadWriteMemory — captures only
private R/W pages (actual injected code), skips mapped files.
Typical dump size: 5-50MB vs 2GB for MiniDumpWithFullMemory.

After dump: triggers YARA scan + Volatility malfind automatically.
"""

import asyncio, ctypes, ctypes.wintypes as wt, os
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_DUMPS_DIR = Path("logs/memory_dumps")
_DUMPS_DIR.mkdir(parents=True, exist_ok=True)

# ── Windows API constants ─────────────────────────────────────────────────────
PROCESS_ALL_ACCESS              = 0x1F0FFF
GENERIC_WRITE                   = 0x40000000
CREATE_ALWAYS                   = 2
FILE_ATTRIBUTE_NORMAL           = 0x80
INVALID_HANDLE_VALUE            = ctypes.c_void_p(-1).value

# Targeted dump: private R/W pages only (injected shellcode lives here)
# Avoids dumping mapped DLLs and shared sections → keeps dumps small
MiniDumpWithPrivateReadWriteMemory = 0x00000200
MiniDumpWithThreadInfo             = 0x00001000
_DUMP_TYPE = MiniDumpWithPrivateReadWriteMemory | MiniDumpWithThreadInfo

# SeDebugPrivilege LUID
SE_DEBUG_NAME      = "SeDebugPrivilege"
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY         = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002


# ── ctypes structures ─────────────────────────────────────────────────────────

class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", wt.DWORD),
        ("Privileges",     LUID_AND_ATTRIBUTES * 1),
    ]


# ── API setup with explicit argtypes ─────────────────────────────────────────

def _setup_apis():
    k32     = ctypes.windll.kernel32
    advapi  = ctypes.windll.advapi32
    dbghelp = ctypes.windll.LoadLibrary("dbghelp.dll")

    # OpenProcess
    k32.OpenProcess.restype  = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

    # CloseHandle
    k32.CloseHandle.restype  = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    # CreateFileW
    k32.CreateFileW.restype  = wt.HANDLE
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,   # lpFileName
        wt.DWORD,           # dwDesiredAccess
        wt.DWORD,           # dwShareMode
        ctypes.c_void_p,    # lpSecurityAttributes
        wt.DWORD,           # dwCreationDisposition
        wt.DWORD,           # dwFlagsAndAttributes
        wt.HANDLE,          # hTemplateFile
    ]

    # GetCurrentProcess
    k32.GetCurrentProcess.restype  = wt.HANDLE
    k32.GetCurrentProcess.argtypes = []

    # OpenProcessToken
    advapi.OpenProcessToken.restype  = wt.BOOL
    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                         ctypes.POINTER(wt.HANDLE)]

    # LookupPrivilegeValueW
    advapi.LookupPrivilegeValueW.restype  = wt.BOOL
    advapi.LookupPrivilegeValueW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(LUID)
    ]

    # AdjustTokenPrivileges
    advapi.AdjustTokenPrivileges.restype  = wt.BOOL
    advapi.AdjustTokenPrivileges.argtypes = [
        wt.HANDLE,                          # TokenHandle
        wt.BOOL,                            # DisableAllPrivileges
        ctypes.POINTER(TOKEN_PRIVILEGES),   # NewState
        wt.DWORD,                           # BufferLength
        ctypes.c_void_p,                    # PreviousState
        ctypes.c_void_p,                    # ReturnLength
    ]

    # MiniDumpWriteDump
    dbghelp.MiniDumpWriteDump.restype  = wt.BOOL
    dbghelp.MiniDumpWriteDump.argtypes = [
        wt.HANDLE,      # hProcess
        wt.DWORD,       # ProcessId
        wt.HANDLE,      # hFile
        wt.DWORD,       # DumpType
        ctypes.c_void_p,# ExceptionParam
        ctypes.c_void_p,# UserStreamParam
        ctypes.c_void_p,# CallbackParam
    ]

    return k32, advapi, dbghelp


def _acquire_debug_privilege() -> bool:
    """
    Acquire SeDebugPrivilege for the current process.
    Required to open handles to protected system processes.
    Must be running as Administrator — otherwise returns False.
    """
    try:
        k32, advapi, _ = _setup_apis()

        # Get current process token
        token_handle = wt.HANDLE()
        if not advapi.OpenProcessToken(
            k32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token_handle),
        ):
            logger.debug("MEM_HUNTER: OpenProcessToken failed")
            return False

        # Look up the LUID for SeDebugPrivilege
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, SE_DEBUG_NAME,
                                             ctypes.byref(luid)):
            k32.CloseHandle(token_handle)
            logger.debug("MEM_HUNTER: LookupPrivilegeValue failed")
            return False

        # Enable the privilege
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount          = 1
        tp.Privileges[0].Luid      = luid
        tp.Privileges[0].Attributes= SE_PRIVILEGE_ENABLED

        advapi.AdjustTokenPrivileges(
            token_handle, False,
            ctypes.byref(tp), ctypes.sizeof(tp),
            None, None,
        )

        k32.CloseHandle(token_handle)

        err = ctypes.get_last_error()
        if err != 0:
            logger.debug(f"MEM_HUNTER: AdjustTokenPrivileges error {err}")
            return False

        logger.info("MEM_HUNTER: SeDebugPrivilege acquired")
        return True

    except Exception as e:
        logger.debug(f"MEM_HUNTER: privilege error: {e}")
        return False


async def dump_process_memory(
    pid: int,
    process_name: str,
    broadcast_fn,
) -> str | None:
    """
    Dump targeted memory regions of a running process.
    Triggers YARA scan and Volatility analysis on the dump.
    Requires Administrator privileges.
    """
    logger.warning(
        f"MEM_HUNTER: initiating memory capture — "
        f"PID {pid} ({process_name})"
    )

    await broadcast_fn({
        "type":         "memory_dump_started",
        "pid":          pid,
        "process_name": process_name,
        "severity":     "HIGH",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    loop = asyncio.get_running_loop()

    def _do_dump() -> str | None:
        k32, advapi, dbghelp = _setup_apis()

        # Acquire SeDebugPrivilege
        if not _acquire_debug_privilege():
            logger.warning(
                "MEM_HUNTER: could not acquire SeDebugPrivilege — "
                "dump may fail on protected processes. "
                "Run JARVIS as Administrator for full access."
            )

        # Open target process
        h_process = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not h_process:
            err = ctypes.get_last_error()
            logger.error(f"MEM_HUNTER: OpenProcess failed (PID {pid}) error={err}")
            return None

        # Create output dump file
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_path = _DUMPS_DIR / f"dump_{process_name}_{pid}_{ts}.dmp"

        h_file = k32.CreateFileW(
            str(dump_path),
            GENERIC_WRITE,
            0,
            None,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )

        if h_file == INVALID_HANDLE_VALUE or h_file is None:
            err = ctypes.get_last_error()
            logger.error(f"MEM_HUNTER: CreateFileW failed error={err}")
            k32.CloseHandle(h_process)
            return None

        # Write targeted dump
        success = dbghelp.MiniDumpWriteDump(
            h_process,    # hProcess
            wt.DWORD(pid),# ProcessId
            h_file,       # hFile
            wt.DWORD(_DUMP_TYPE),  # DumpType (targeted, not full)
            None,         # ExceptionParam
            None,         # UserStreamParam
            None,         # CallbackParam
        )

        k32.CloseHandle(h_file)
        k32.CloseHandle(h_process)

        if success:
            size_mb = dump_path.stat().st_size / (1024 * 1024)
            logger.info(
                f"MEM_HUNTER: dump complete — "
                f"{dump_path.name} ({size_mb:.1f} MB)"
            )
            return str(dump_path)
        else:
            err = ctypes.get_last_error()
            logger.error(f"MEM_HUNTER: MiniDumpWriteDump failed error={err}")
            if dump_path.exists():
                dump_path.unlink()
            return None

    dump_path_str = await loop.run_in_executor(None, _do_dump)

    if not dump_path_str:
        await broadcast_fn({
            "type":    "memory_dump_failed",
            "pid":     pid,
            "process": process_name,
        })
        return None

    dump_size_mb = Path(dump_path_str).stat().st_size / (1024 * 1024)

    await broadcast_fn({
        "type":         "memory_dump_complete",
        "pid":          pid,
        "process_name": process_name,
        "dump_path":    dump_path_str,
        "size_mb":      round(dump_size_mb, 1),
        "severity":     "HIGH",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    # Queue YARA scan on dump
    try:
        from tools.yara_file_monitor import _scan_queue_ref
        if _scan_queue_ref is not None:
            loop.call_soon_threadsafe(
                _scan_queue_ref.put_nowait, Path(dump_path_str)
            )
    except Exception as e:
        logger.debug(f"MEM_HUNTER: YARA queue error: {e}")

    # Trigger Volatility malfind async
    asyncio.create_task(
        _run_volatility_on_dump(dump_path_str, broadcast_fn)
    )

    return dump_path_str


async def _run_volatility_on_dump(
    dump_path: str,
    broadcast_fn,
) -> None:
    """Run Volatility malfind on the captured dump."""
    try:
        from tools.forensic_volatility import trigger_forensic_capture
        logger.info("MEM_HUNTER: triggering Volatility malfind on dump")
        await broadcast_fn({
            "type":      "volatility_queued",
            "dump_path": dump_path,
            "plugin":    "windows.malfind",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.debug(f"MEM_HUNTER: Volatility trigger error: {e}")
