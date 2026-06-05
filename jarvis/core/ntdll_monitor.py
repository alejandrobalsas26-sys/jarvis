"""
core/ntdll_monitor.py — JARVIS V49.0 OMNISCIENCE
Userland EDR-tamper / unhooking detection. Baselines the canonical syscall-stub
prologues of ntdll.dll from the on-disk image, then reads the same stubs from
live processes via ReadProcessMemory. A control-flow redirect over a stub
prologue (inline hook) flags the process -> guarded kill + CRITICAL alert.

FP control (critical): only Nt* stub prologues are compared (not whole .text),
and a diff is treated as a hook ONLY if it is a control-flow redirect
(jmp/call/push-ret). WOW64 targets are skipped. If the SAME hook is prevalent
across many processes it is treated as an environmental AV/EDR and NEVER killed.
Protected/system processes and self are never killed.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("jarvis.ntdll_monitor")

_IS_WINDOWS = os.name == "nt"

try:
    import pefile
    _PEFILE_OK = True
except Exception:
    pefile = None
    _PEFILE_OK = False

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

# --- Config ------------------------------------------------------------------
_AUTO_KILL_ENABLED = True
_SWEEP_SECONDS = 90
_STUB_LEN = 24
_NTDLL_PATH = r"C:\Windows\System32\ntdll.dll"
_ENV_PROC_THRESHOLD = 8          # >= this many hooked procs in a sweep => env AV
_CRITICAL_STUBS = [
    "NtAllocateVirtualMemory", "NtProtectVirtualMemory", "NtWriteVirtualMemory",
    "NtReadVirtualMemory", "NtCreateThreadEx", "NtQueueApcThread",
    "NtSetContextThread", "NtResumeThread", "NtMapViewOfSection",
    "NtCreateSection", "NtOpenProcess", "NtUnmapViewOfSection",
    "NtAdjustPrivilegesToken",
]
_PROTECTED_PIDS = {0, 4}
_PROTECTED_NAMES = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "winlogon.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe",
    "explorer.exe", "memcompression", "memory compression",
}

_REDIRECT_FIRST = {0xE9, 0xEB, 0xE8}
_baseline: dict = {}             # name -> prologue bytes (from disk)
_ntdll_base = 0
_sema = asyncio.Semaphore(1)

if _IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _k32 = None


def _enable_se_debug() -> None:
    if not _IS_WINDOWS:
        return
    try:
        SE_PRIVILEGE_ENABLED = 0x00000002
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LAA(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TP(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LAA * 1)]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        htok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(_k32.GetCurrentProcess(),
                                         TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                         ctypes.byref(htok)):
            return
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege",
                                              ctypes.byref(luid)):
            return
        tp = TP()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(htok, False, ctypes.byref(tp), 0, None, None)
    except Exception as e:
        logger.debug("ntdll_monitor: SeDebugPrivilege failed: %s", e)


def _build_baseline() -> bool:
    global _baseline, _ntdll_base
    if not (_PEFILE_OK and _IS_WINDOWS):
        return False
    try:
        pe = pefile.PE(_NTDLL_PATH, fast_load=False)
    except Exception as e:
        logger.warning("ntdll_monitor: cannot parse ntdll on disk: %s", e)
        return False
    want = set(_CRITICAL_STUBS)
    rvas = {}
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for s in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if s.name:
                nm = s.name.decode("ascii", "ignore")
                if nm in want:
                    rvas[nm] = s.address
    with open(_NTDLL_PATH, "rb") as f:
        raw = f.read()
    for nm, rva in rvas.items():
        try:
            off = pe.get_offset_from_rva(rva)
            _baseline[nm] = raw[off:off + _STUB_LEN]
        except Exception:
            continue
    pe.close()
    try:
        _ntdll_base = int(_k32.GetModuleHandleW("ntdll.dll"))
    except Exception:
        _ntdll_base = 0
    # store RVAs for live reads
    _build_baseline.rvas = rvas
    ok = bool(_baseline) and _ntdll_base != 0
    if ok:
        logger.info("ntdll_monitor: baseline built for %d stubs (base=0x%x)",
                    len(_baseline), _ntdll_base)
    return ok


def _is_redirect(b: bytes) -> bool:
    if not b:
        return False
    if b[0] in _REDIRECT_FIRST:
        return True
    if len(b) >= 2 and b[0] == 0xFF and b[1] in (0x25, 0x15):
        return True
    if len(b) >= 6 and b[0] == 0x68 and b[5] == 0xC3:                     # push imm32; ret
        return True
    if len(b) >= 12 and b[0] == 0x48 and b[1] == 0xB8 and b[10] == 0xFF and b[11] == 0xE0:
        return True                                                       # mov rax,imm64; jmp rax
    if len(b) >= 2 and b[0] == 0x49 and b[1] == 0xBB:                     # mov r11,imm64; jmp r11
        return True
    return False


def _open_read(pid: int):
    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION,
                         False, pid)
    return h or None


def _is_wow64(h) -> bool:
    b = wintypes.BOOL(0)
    try:
        if _k32.IsWow64Process(h, ctypes.byref(b)):
            return bool(b.value)
    except Exception:
        pass
    return False


def _rpm(h, addr: int, size: int) -> bytes:
    buf = (ctypes.c_char * size)()
    read = ctypes.c_size_t(0)
    ok = _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                ctypes.byref(read))
    if not ok or read.value == 0:
        return b""
    return buf.raw[:read.value]


def _check_pid_blocking(pid: int) -> dict:
    """Returns {'hooked': bool, 'redirect': bool, 'stubs': [names], 'name': str}."""
    out = {"pid": pid, "hooked": False, "redirect": False, "stubs": [], "name": None,
           "skipped": None}
    rvas = getattr(_build_baseline, "rvas", {})
    if not rvas or not _ntdll_base:
        out["skipped"] = "no baseline"
        return out
    h = _open_read(pid)
    if not h:
        out["skipped"] = "open denied"
        return out
    try:
        if _is_wow64(h):
            out["skipped"] = "wow64"
            return out
        try:
            out["name"] = psutil.Process(pid).name().lower() if _PSUTIL_OK else None
        except Exception:
            out["name"] = None
        for nm, rva in rvas.items():
            base_bytes = _baseline.get(nm)
            if not base_bytes:
                continue
            live = _rpm(h, _ntdll_base + rva, _STUB_LEN)
            if not live or len(live) < 4:
                continue
            if live[:4] == base_bytes[:4]:
                continue                       # canonical mov r10,rcx; mov eax,ssn => clean
            out["hooked"] = True
            out["stubs"].append(nm)
            if _is_redirect(live):
                out["redirect"] = True
    finally:
        _k32.CloseHandle(h)
    return out


def _neutralize(pid: int, name: str) -> dict:
    o = {"pid": pid, "killed": False, "reason": None, "name": name}
    if not _AUTO_KILL_ENABLED:
        o["reason"] = "auto-kill disabled"; return o
    if pid in _PROTECTED_PIDS or pid == os.getpid():
        o["reason"] = "protected/self — refused"; return o
    if (name or "").lower() in _PROTECTED_NAMES:
        o["reason"] = "critical system process — refused"; return o
    try:
        psutil.Process(pid).kill()
        o["killed"] = True; o["reason"] = "terminated"
    except psutil.NoSuchProcess:
        o["reason"] = "already gone"
    except psutil.AccessDenied:
        o["reason"] = "access denied"
    except Exception as e:
        o["reason"] = f"kill failed: {e}"
    return o


async def _alert(correlator, finding: dict, outcome: dict, environmental: bool) -> None:
    logger.critical("NTDLL_MONITOR: ntdll hook in pid=%s (%s) stubs=%s redirect=%s env=%s",
                    finding["pid"], finding.get("name"), finding["stubs"],
                    finding["redirect"], environmental)
    event = {"source": "ntdll_monitor", "type": "ntdll_hook", "severity": 9.0,
             "pid": finding["pid"], "proc_name": finding.get("name"),
             "hooked_stubs": finding["stubs"], "control_flow_redirect": finding["redirect"],
             "environmental_av_suspected": environmental,
             "response": outcome, "attck": ["T1055.001", "T1562.001", "T1106"],
             "ts": time.time()}
    if correlator is None:
        return
    try:
        if hasattr(correlator, "ingest_event"):
            await correlator.ingest_event(event)
        elif hasattr(correlator, "add_event"):
            r = correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
        else:
            logger.error("ntdll_monitor: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("ntdll_monitor: alert dispatch failed: %s", e)


async def scan_pid(pid: int, *, reason: str = "manual", correlator=None,
                   environmental: bool = False) -> dict:
    if not (_IS_WINDOWS and _baseline):
        return {"pid": pid, "skipped": "dormant/uninitialized"}
    loop = asyncio.get_running_loop()
    async with _sema:
        finding = await loop.run_in_executor(None, _check_pid_blocking, pid)
    if not finding.get("hooked"):
        return finding
    # Kill only on a real control-flow redirect, single-process (non-environmental).
    if finding["redirect"] and not environmental:
        outcome = await loop.run_in_executor(
            None, _neutralize, pid, finding.get("name"))
    else:
        outcome = {"pid": pid, "killed": False,
                   "reason": "alert-only (env AV or non-redirect modification)"}
    await _alert(correlator, finding, outcome, environmental)
    return {**finding, "response": outcome}


async def _sweep(correlator) -> None:
    if not _PSUTIL_OK:
        return
    loop = asyncio.get_running_loop()
    pids = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            pid = p.info["pid"]
            if pid in _PROTECTED_PIDS:
                continue
            pids.append(pid)
        except Exception:
            continue
    findings = []
    for pid in pids:
        async with _sema:
            f = await loop.run_in_executor(None, _check_pid_blocking, pid)
        if f.get("redirect"):
            findings.append(f)
    environmental = len(findings) >= _ENV_PROC_THRESHOLD
    for f in findings:
        if f["redirect"] and not environmental and _AUTO_KILL_ENABLED:
            outcome = await loop.run_in_executor(None, _neutralize, f["pid"], f.get("name"))
        else:
            outcome = {"pid": f["pid"], "killed": False,
                       "reason": "alert-only (env AV detected)" if environmental
                       else "alert-only"}
        await _alert(correlator, f, outcome, environmental)


async def start(correlator=None) -> None:
    """main.py startup hook. Watchdog Pattern: dormant if non-Windows, pefile or
    psutil missing, or baseline cannot be built."""
    if not _IS_WINDOWS:
        logger.warning("NTDLL_MONITOR: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not (_PEFILE_OK and _PSUTIL_OK):
        logger.warning("NTDLL_MONITOR: pefile/psutil missing — dormant")
        await asyncio.Event().wait(); return
    _enable_se_debug()
    if not _build_baseline():
        logger.warning("NTDLL_MONITOR: baseline unavailable — dormant")
        await asyncio.Event().wait(); return
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("ntdll_monitor", scan_pid)
        except Exception:
            pass
    logger.info("NTDLL_MONITOR: armed — sweeping every %ds (auto-kill=%s)",
                _SWEEP_SECONDS, _AUTO_KILL_ENABLED)
    while True:
        try:
            await _sweep(correlator)
        except Exception as e:
            logger.debug("ntdll_monitor: sweep error: %s", e)
        await asyncio.sleep(_SWEEP_SECONDS)
