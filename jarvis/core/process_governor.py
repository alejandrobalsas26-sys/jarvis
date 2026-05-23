"""
core/process_governor.py — Windows CPU Priority Governor (v29.0).

Elevates ollama.exe to HIGH_PRIORITY_CLASS so LLM inference gets
maximum CPU time on U-series hardware.

IMPORTANT: Does NOT touch the main Python process. LLM inference
and the asyncio event loop run in the main process — demoting it
would strangle the very thing we are trying to accelerate.
Background bridges (ETW, Zeek, eBPF) are already I/O-bound and
are deprioritized automatically by the Windows scheduler when they
block on network/disk I/O. No manual demotion needed or wanted.
"""

import psutil
from loguru import logger


_ELEVATED_PIDS: set[int] = set()


def enforce_cpu_priorities() -> None:
    """
    Scan for ollama.exe processes and elevate each to HIGH_PRIORITY_CLASS.

    Idempotent and best-effort: AccessDenied / NoSuchProcess are swallowed
    silently so a missing ollama install or insufficient privileges never
    blocks startup. Already-elevated PIDs are skipped to avoid log spam.
    """
    high = getattr(psutil, "HIGH_PRIORITY_CLASS", None)
    if high is None:
        return  # non-Windows or psutil missing the constant

    elevated  = 0
    attempted = 0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name != "ollama.exe":
                continue
            attempted += 1
            pid = proc.info["pid"]
            if pid in _ELEVATED_PIDS:
                continue
            if proc.nice() == high:
                _ELEVATED_PIDS.add(pid)
                continue
            proc.nice(high)
            _ELEVATED_PIDS.add(pid)
            elevated += 1
            logger.info(
                f"GOVERNOR: elevated ollama.exe pid={pid} → HIGH_PRIORITY_CLASS"
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception as e:
            logger.debug(f"GOVERNOR: could not elevate {proc}: {e}")
            continue

    if attempted == 0:
        logger.debug("GOVERNOR: no ollama.exe processes found")
