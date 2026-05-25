"""
core/cancel_bus.py — Global operation cancellation bus (v35.0).

Every JARVIS operation that can run for more than 1 second registers
a cancellation event here. The operator can abort all of them instantly
via voice command, HUD button, or keyboard shortcut.

Thread-safe: all events are asyncio.Event objects.
Cross-thread callers use cancel_all_threadsafe().
"""

import asyncio
import time

from loguru import logger

# ── Cancellation events ───────────────────────────────────────────────────────

llm_stream_cancel:   asyncio.Event | None = None
agentic_loop_cancel: asyncio.Event | None = None
playbook_cancel:     asyncio.Event | None = None
tts_cancel:          asyncio.Event | None = None

# Track the main event loop for cross-thread calls
_loop: asyncio.AbstractEventLoop | None = None

# Timestamp of last cancel for debouncing
_last_cancel_ts:   float = 0.0
_DEBOUNCE_SECONDS: float = 1.0

# Operation registry — what's currently running
_active_operations: dict[str, float] = {}   # name → start_time


def initialize(loop: asyncio.AbstractEventLoop) -> None:
    """Call from main.py after event loop creation."""
    global llm_stream_cancel, agentic_loop_cancel
    global playbook_cancel, tts_cancel, _loop
    _loop                = loop
    llm_stream_cancel    = asyncio.Event()
    agentic_loop_cancel  = asyncio.Event()
    playbook_cancel      = asyncio.Event()
    tts_cancel           = asyncio.Event()
    logger.info("CANCEL_BUS: initialized — all cancellation events ready")


def register_operation(name: str) -> None:
    """Mark an operation as active."""
    _active_operations[name] = time.monotonic()


def unregister_operation(name: str) -> None:
    """Mark an operation as complete."""
    _active_operations.pop(name, None)


def get_active_operations() -> dict[str, float]:
    """Return currently active operations with elapsed seconds."""
    now = time.monotonic()
    return {name: round(now - start, 1)
            for name, start in _active_operations.items()}


def cancel_all() -> int:
    """
    Abort all active operations simultaneously.
    Returns count of events fired.
    Call from async context only.
    """
    global _last_cancel_ts
    now = time.monotonic()
    if (now - _last_cancel_ts) < _DEBOUNCE_SECONDS:
        return 0   # debounce — prevent double-fire

    _last_cancel_ts = now
    count = 0

    for event in (llm_stream_cancel, agentic_loop_cancel,
                  playbook_cancel, tts_cancel):
        if event and not event.is_set():
            event.set()
            count += 1

    active = get_active_operations()
    logger.warning(
        f"CANCEL_BUS: ABORT fired — {count} events set | "
        f"active ops: {list(active.keys())}"
    )
    return count


def cancel_all_threadsafe() -> None:
    """
    Cancel all operations from a non-async thread (audio thread, etc).
    Uses loop.call_soon_threadsafe for safety.
    """
    if _loop and not _loop.is_closed():
        _loop.call_soon_threadsafe(_cancel_sync)


def _cancel_sync() -> None:
    """Sync version for call_soon_threadsafe."""
    global _last_cancel_ts
    now = time.monotonic()
    if (now - _last_cancel_ts) < _DEBOUNCE_SECONDS:
        return
    _last_cancel_ts = now
    for event in (llm_stream_cancel, agentic_loop_cancel,
                  playbook_cancel, tts_cancel):
        if event and not event.is_set():
            event.set()


def reset_all() -> None:
    """
    Clear all cancellation events after abort completes.
    Call after cleanup to restore normal operation.
    """
    for event in (llm_stream_cancel, agentic_loop_cancel,
                  playbook_cancel, tts_cancel):
        if event:
            event.clear()
    _active_operations.clear()
    logger.info("CANCEL_BUS: all events reset — ready for new operations")


def cancel_llm_only() -> bool:
    """Cancel just the LLM stream without stopping other operations."""
    if llm_stream_cancel and not llm_stream_cancel.is_set():
        llm_stream_cancel.set()
        logger.info("CANCEL_BUS: LLM stream cancelled")
        return True
    return False
