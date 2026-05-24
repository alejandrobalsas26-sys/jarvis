"""
core/shutdown_manager.py — Graceful shutdown handler (v30.0).

Handles SIGINT (Ctrl+C) and SIGTERM cleanly:
  1. Signal watchdog to stop all managed tasks.
  2. Flush ChromaDB episodic memory and knowledge vault.
  3. Write final audit log entry.
  4. Cancel all remaining asyncio tasks.
  5. Exit with code 0.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Callable, Coroutine
from loguru import logger

_shutdown_event: asyncio.Event | None = None
_shutdown_callbacks: list[Callable[[], Coroutine]] = []


def get_shutdown_event() -> asyncio.Event:
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def register_shutdown_callback(coro_fn: Callable[[], Coroutine]) -> None:
    """Register an async callable to run during graceful shutdown."""
    _shutdown_callbacks.append(coro_fn)


def install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """
    Install SIGINT/SIGTERM handlers. Call from main.py after loop creation.
    On Windows, only SIGINT (Ctrl+C) is supported — SIGTERM is ignored.
    """
    def _handler(signame: str):
        logger.warning(f"SHUTDOWN: {signame} received — initiating graceful shutdown")
        loop.call_soon_threadsafe(get_shutdown_event().set)

    try:
        import signal as _sig
        loop.add_signal_handler(_sig.SIGINT,  lambda: _handler("SIGINT"))
        loop.add_signal_handler(_sig.SIGTERM, lambda: _handler("SIGTERM"))
    except (NotImplementedError, AttributeError):
        # Windows: add_signal_handler not supported — use signal.signal instead
        import signal as _sig
        try:
            _sig.signal(_sig.SIGINT, lambda s, f: _handler("SIGINT"))
        except Exception:
            pass


async def run_graceful_shutdown(watchdog=None) -> None:
    """
    Execute the full shutdown sequence.
    Call this when get_shutdown_event() fires.
    """
    logger.info("SHUTDOWN: beginning graceful shutdown sequence")
    t0 = time.monotonic()

    # 1. Stop watchdog (prevents task restarts during shutdown)
    if watchdog:
        try:
            stop_fn = getattr(watchdog, "stop", None)
            if callable(stop_fn):
                stop_fn()
        except Exception:
            pass

    # 2. Run registered callbacks (DB flush, session save, etc.)
    for cb in _shutdown_callbacks:
        try:
            await asyncio.wait_for(cb(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug(f"SHUTDOWN: callback {getattr(cb, '__name__', repr(cb))} timed out")
        except Exception as e:
            logger.debug(f"SHUTDOWN: callback {getattr(cb, '__name__', repr(cb))} error: {e}")

    # 3. Final audit log entry
    try:
        from pathlib import Path
        Path("logs").mkdir(parents=True, exist_ok=True)
        audit_path = "logs/tactic_audit.jsonl"
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event":     "system_shutdown",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round(time.monotonic() - t0, 2),
            }) + "\n")
    except Exception:
        pass

    # 4. Cancel all remaining tasks except the current one
    current = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = round((time.monotonic() - t0) * 1000, 1)
    logger.info(f"SHUTDOWN: complete in {elapsed}ms — goodbye")
