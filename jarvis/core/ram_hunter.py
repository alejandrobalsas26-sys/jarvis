"""
core/ram_hunter.py — JARVIS V47.0 TITAN
On-demand live process-memory forensics. Receives a PID, scans its address
space against the compiled YARA ruleset, and reports matches to the correlator.
Invoked by core/correlator.py ONLY on high-severity process-injection events.
Heavy scans run in a thread executor to keep the asyncio loop responsive.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.ram_hunter")

# --- Optional dependency gates ----------------------------------------------
try:
    import yara  # yara-python (libyara handles VirtualQueryEx/ReadProcessMemory)
    _YARA_OK = True
except Exception:
    yara = None
    _YARA_OK = False

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

_IS_WINDOWS = os.name == "nt"

# --- Config ------------------------------------------------------------------
_RULES_DIR = Path(os.environ.get("JARVIS_YARA_RULES", "rules"))
_SCAN_TIMEOUT = 30          # seconds, per-scan ceiling
_MAX_CONCURRENT = 1         # U-Series CPU constraint: serialize memory scans
_PROTECTED_PIDS = {0, 4}    # System Idle / System — never scan or touch

_compiled: Optional["yara.Rules"] = None
_sema = asyncio.Semaphore(_MAX_CONCURRENT)


def _enable_se_debug() -> bool:
    """Best-effort SeDebugPrivilege escalation (mirrors core/memory_hunter.py)."""
    if not _IS_WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        SE_PRIVILEGE_ENABLED = 0x00000002
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        htok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(htok)):
            return False
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege",
                                              ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        return bool(advapi32.AdjustTokenPrivileges(
            htok, False, ctypes.byref(tp), 0, None, None))
    except Exception as e:
        logger.debug("ram_hunter: SeDebugPrivilege escalation failed: %s", e)
        return False


def _compile_rules() -> bool:
    global _compiled
    if not _YARA_OK:
        return False
    if not _RULES_DIR.is_dir():
        logger.warning("ram_hunter: rules dir %s missing", _RULES_DIR)
        return False
    sources = {}
    for i, f in enumerate(sorted(_RULES_DIR.rglob("*"))):
        if f.is_file() and f.suffix.lower() in (".yar", ".yara"):
            sources[f"ns{i}"] = str(f)
    if not sources:
        logger.warning("ram_hunter: no .yar/.yara rules under %s", _RULES_DIR)
        return False
    try:
        _compiled = yara.compile(filepaths=sources)
    except yara.Error as e:
        logger.error("ram_hunter: rule compilation failed: %s", e)
        return False
    logger.info("ram_hunter: compiled %d YARA rule file(s)", len(sources))
    return True


def _scan_pid_blocking(pid: int) -> list[dict]:
    """Blocking YARA scan of a live process. Executed off-loop."""
    if _compiled is None:
        return []
    try:
        matches = _compiled.match(pid=pid, timeout=_SCAN_TIMEOUT)
    except yara.Error as e:
        # Access Denied, protected process, or process already exited.
        logger.info("ram_hunter: cannot scan pid=%s (%s)", pid, e)
        return []
    out = []
    for m in matches:
        try:
            out.append({"rule": m.rule, "namespace": m.namespace,
                        "tags": list(m.tags), "meta": dict(m.meta)})
        except Exception:
            out.append({"rule": getattr(m, "rule", "unknown"),
                        "namespace": "", "tags": [], "meta": {}})
    return out


async def _report(correlator, result: dict) -> None:
    rule_names = ", ".join(m["rule"] for m in result["matches"]) or "unknown"
    event = {
        "source": "ram_hunter",
        "type": "memory_yara_match",
        "severity": 9.0,
        "pid": result["pid"],
        "proc_name": result.get("proc_name"),
        "proc_path": result.get("proc_path"),
        "rules": rule_names,
        "match_count": len(result["matches"]),
        "attck": ["T1055", "T1620"],   # Process Injection / Reflective Loading
        "ts": result["ts"],
        "detail": result["matches"],
    }
    try:
        if hasattr(correlator, "ingest_event"):
            await correlator.ingest_event(event)
        elif hasattr(correlator, "add_event"):
            r = correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
        else:
            logger.error("ram_hunter: correlator has no ingest hook; event=%s", event)
    except Exception as e:
        logger.error("ram_hunter: correlator dispatch failed: %s", e)


async def hunt(pid: int, *, reason: str = "manual", correlator=None) -> dict:
    """Scan a live process's memory with YARA. On hits, raise an incident via
    the correlator. Safe to call from the event loop (heavy work runs off-loop)."""
    result = {"pid": pid, "reason": reason, "ts": time.time(),
              "scanned": False, "matches": [], "error": None}

    if not (_YARA_OK and _PSUTIL_OK and _compiled is not None):
        result["error"] = "ram_hunter dormant/uninitialized"
        return result
    if pid in _PROTECTED_PIDS:
        result["error"] = "protected pid refused"
        return result

    try:
        p = psutil.Process(pid)
        result["proc_name"] = p.name()
        try:
            result["proc_path"] = p.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            result["proc_path"] = None
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        result["error"] = f"process unavailable: {e.__class__.__name__}"
        return result
    except Exception as e:
        result["error"] = f"identity lookup failed: {e}"
        return result

    async with _sema:
        loop = asyncio.get_running_loop()
        try:
            result["matches"] = await loop.run_in_executor(
                None, _scan_pid_blocking, pid)
        except Exception as e:
            result["error"] = f"scan exception: {e}"
            return result

    result["scanned"] = True
    if result["matches"]:
        logger.warning("ram_hunter: %d YARA hit(s) in pid=%s (%s) [reason=%s]",
                       len(result["matches"]), pid, result.get("proc_name"), reason)
        if correlator is not None:
            await _report(correlator, result)
    else:
        logger.info("ram_hunter: clean scan pid=%s (%s)", pid, result.get("proc_name"))
    return result


async def start(correlator=None) -> None:
    """main.py startup hook. Arms the ruleset and stays resident so the
    correlator can call hunt() on demand. JARVIS Watchdog Pattern: on missing
    deps / no rules, log + dormant sleep (never bare return)."""
    if not _YARA_OK:
        logger.warning("RAM_HUNTER: yara-python unavailable — dormant")
        await asyncio.Event().wait()
        return
    if not _PSUTIL_OK:
        logger.warning("RAM_HUNTER: psutil unavailable — dormant")
        await asyncio.Event().wait()
        return
    _enable_se_debug()
    if not _compile_rules():
        logger.warning("RAM_HUNTER: no usable YARA ruleset — dormant")
        await asyncio.Event().wait()
        return

    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("ram_hunter", hunt)
        except Exception as e:
            logger.debug("ram_hunter: responder registration skipped: %s", e)

    logger.info("RAM_HUNTER: armed — on-demand memory forensics ready")
    await asyncio.Event().wait()   # resident; work is event-driven via hunt()
