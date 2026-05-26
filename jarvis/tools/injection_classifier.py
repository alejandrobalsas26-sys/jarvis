"""
tools/injection_classifier.py — Process injection technique classifier (v33.0).

Correlates ETW + Sysmon events to identify specific injection sub-techniques:
  T1055.001 — DLL Injection          (LoadLibrary + CreateRemoteThread)
  T1055.002 — PE Injection           (VirtualAllocEx + WriteProcessMemory)
  T1055.003 — Thread Hijacking       (OpenThread + SuspendThread + SetContext)
  T1055.004 — APC Injection          (QueueUserAPC + NtAlertResumeThread)
  T1055.012 — Process Hollowing      (NtUnmapViewOfSection + WriteProcessMemory)
  T1055.013 — Process Doppelgänging  (NtCreateTransaction + NtWriteFile)
"""

import asyncio
import collections
import time
from datetime import datetime, timezone

from loguru import logger

_WINDOW_SECONDS = 30
_process_events: dict[int, collections.deque] = {}


def _classify_injection(events: list[dict]) -> str | None:
    api_calls = {str(e.get("api_call", "")).lower() for e in events}
    event_ids = {int(e.get("event_id", 0) or 0) for e in events}

    if ("ntunmapviewofsection" in api_calls or "zwunmapviewofsection" in api_calls) \
            and "writeprocessmemory" in api_calls:
        return "T1055.012"

    if "loadlibrarya" in api_calls or "loadlibraryw" in api_calls:
        if "createremotethread" in api_calls or 30 in event_ids:
            return "T1055.001"

    if "virtualallocex" in api_calls and "writeprocessmemory" in api_calls:
        if "createremotethread" in api_calls or 30 in event_ids:
            return "T1055.002"

    if "queueuserapc" in api_calls or "ntalertresumethread" in api_calls:
        return "T1055.004"

    if "ntcreatetransaction" in api_calls or "ntwritefile" in api_calls:
        if "ntcreatesection" in api_calls:
            return "T1055.013"

    if "openthread" in api_calls and "suspendthread" in api_calls:
        if "setthreadcontext" in api_calls:
            return "T1055.003"

    if 8 in event_ids or 25 in event_ids:
        return "T1055"

    return None


def ingest_process_event(pid: int, event: dict) -> dict | None:
    now = time.monotonic()
    if pid not in _process_events:
        _process_events[pid] = collections.deque()

    _process_events[pid].append({**event, "_ts": now})

    cutoff = now - _WINDOW_SECONDS
    while _process_events[pid] and _process_events[pid][0]["_ts"] < cutoff:
        _process_events[pid].popleft()

    technique = _classify_injection(list(_process_events[pid]))
    if technique:
        return {
            "pid":       pid,
            "technique": technique,
            "events":    len(_process_events[pid]),
            "process":   event.get("process", "unknown"),
        }
    return None


async def analyze_and_broadcast(pid: int, event: dict,
                                broadcast_fn) -> None:
    """Called from ETW/Sysmon bridge for process events."""
    result = ingest_process_event(pid, event)
    if result:
        logger.warning(
            f"INJECTION: {result['technique']} detected — "
            f"PID {pid} ({result['process']})"
        )
        await broadcast_fn({
            "type":      "injection_classified",
            "technique": result["technique"],
            "pid":       pid,
            "process":   result["process"],
            "severity":  "CRITICAL",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Auto-dump memory on high-confidence injection detection
        if result["technique"] in {"T1055.012", "T1055.001",
                                    "T1055.002", "T1055.004"}:
            try:
                from tools.memory_hunter import dump_process_memory
                asyncio.create_task(
                    dump_process_memory(pid, result["process"], broadcast_fn)
                )
            except Exception as e:
                logger.debug(f"INJECTION: memory_hunter trigger error: {e}")
