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
import os
import time
from datetime import datetime, timezone
from typing import Callable, Coroutine
from loguru import logger

import core.lifecycle as _lc

_shutdown_event: asyncio.Event | None = None
_shutdown_callbacks: list[Callable[[], Coroutine]] = []

# V69 M54.11 — signal accounting for exactly-once shutdown + a documented
# second-stage forced exit. The FIRST SIGINT begins the one graceful shutdown;
# further SIGINTs are idempotent no-ops UNTIL the operator insists (>= this many
# total) while already STOPPING, which triggers an emergency os._exit so a wedged
# shutdown can always be escaped by pressing Ctrl+C again.
_signal_count = 0
_FORCE_EXIT_SIGNAL_COUNT = 3

# Hard ceiling on step 4 (cancel + await every remaining task). Individual
# tasks are asked to cancel cooperatively, but a task blocked inside a
# run_in_executor() thread cannot actually be interrupted — cancelling its
# asyncio.Task only stops *awaiting* it once this deadline passes, it does
# not kill the underlying OS thread. Without this ceiling, one misbehaving
# background loop can hang the entire shutdown indefinitely.
_TASK_DRAIN_TIMEOUT_S = 10.0


def get_shutdown_event() -> asyncio.Event:
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def register_shutdown_callback(coro_fn: Callable[[], Coroutine]) -> None:
    """Register an async callable to run during graceful shutdown."""
    _shutdown_callbacks.append(coro_fn)


def handle_shutdown_signal(signame: str) -> str:
    """Idempotent signal handler body (V69 M54.11). Exactly-once semantics:

      * FIRST signal  → lifecycle transitions to STOPPING, the shutdown event is
        set, one WARNING is logged. Returns "initiated".
      * REPEAT signal while stopping → no second shutdown; one concise DEBUG line.
        Returns "already_stopping" (or forces an emergency exit past the threshold).

    Separated from the OS-signal registration so tests can inject signals without
    sending real SIGINT. Never schedules more than one graceful shutdown.
    """
    global _signal_count
    _signal_count += 1
    started = _lc.begin_stopping()   # atomic; True only for the first caller
    if started:
        logger.warning(f"SHUTDOWN: {signame} received — initiating graceful shutdown")
        try:
            get_shutdown_event().set()
        except Exception:
            pass
        return "initiated"
    # Already stopping — do NOT start another shutdown sequence.
    if _signal_count >= _FORCE_EXIT_SIGNAL_COUNT:
        logger.warning(
            f"SHUTDOWN: {signame} received {_signal_count}x — forcing emergency exit"
        )
        os._exit(1)
    logger.debug(f"SHUTDOWN: {signame} received — shutdown already in progress")
    return "already_stopping"


def reset_signal_state() -> None:
    """Reset the signal counter (tests / a fresh process)."""
    global _signal_count
    _signal_count = 0


def install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """
    Install SIGINT/SIGTERM handlers. Call from main.py after loop creation.
    On Windows, only SIGINT (Ctrl+C) is supported — SIGTERM is ignored. The
    handler body is idempotent (see handle_shutdown_signal): one Ctrl+C starts
    exactly one shutdown, repeats do not restart or duplicate it.
    """
    def _handler(signame: str):
        # Marshal onto the loop thread so lifecycle/event mutation is race-free.
        loop.call_soon_threadsafe(handle_shutdown_signal, signame)

    try:
        import signal as _sig
        loop.add_signal_handler(_sig.SIGINT,  lambda: _handler("SIGINT"))
        loop.add_signal_handler(_sig.SIGTERM, lambda: _handler("SIGTERM"))
    except (NotImplementedError, AttributeError):
        # Windows: add_signal_handler not supported — use signal.signal instead.
        # signal.signal runs the handler in the main thread; handle_shutdown_signal
        # is itself idempotent so this is safe without the loop marshal.
        import signal as _sig
        try:
            _sig.signal(_sig.SIGINT, lambda s, f: handle_shutdown_signal("SIGINT"))
        except Exception:
            pass


async def run_graceful_shutdown(watchdog=None) -> None:
    """
    Execute the full shutdown sequence in the CORRECT order (V69 M54.12).

    The live run flushed storage while background jobs could still start new writes,
    and a hunt began *after* shutdown had started. The ordering below fixes both:

      1. transition lifecycle to STOPPING (idempotent) — every task-creation and
         scheduler-iteration seam now refuses new work;
      2. stop the watchdog — no supervised task is restarted during shutdown;
      3. cancel scheduler/worker tasks and wait a BOUNDED time — writers are stopped
         *before* any store is touched;
      4. only THEN run the checkpoint/flush/close callbacks (semantic checkpoint,
         Chroma flush, DB/SIEM close, TTS stop) — storage closes after writers stop
         and the semantic checkpoint stays durable;
      5. write the final audit entry;
      6. mark STOPPED.
    """
    logger.info("SHUTDOWN: beginning graceful shutdown sequence")
    t0 = time.monotonic()

    # 1. Authoritative lifecycle transition (idempotent). From here can_start_task()
    #    is False everywhere, so no scheduler starts a new iteration.
    _lc.begin_stopping()

    # 2. Stop watchdog (prevents task restarts during shutdown).
    if watchdog:
        for _name in ("request_stop", "stop"):
            try:
                fn = getattr(watchdog, _name, None)
                if callable(fn):
                    fn()
                    break
            except Exception:
                pass

    # 3. Cancel all remaining tasks except the current one, BEFORE flushing/closing
    #    storage — this is what guarantees writers are stopped before their stores
    #    are touched. Bounded by _TASK_DRAIN_TIMEOUT_S: a task stuck inside
    #    run_in_executor() ignores .cancel() until its thread returns, so this must
    #    not be allowed to block process exit forever.
    current = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current]
    for t in tasks:
        t.cancel()
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_TASK_DRAIN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            stuck = [t.get_name() for t in tasks if not t.done()]
            logger.warning(
                f"SHUTDOWN: {len(stuck)} task(s) still running after "
                f"{_TASK_DRAIN_TIMEOUT_S}s drain deadline — forcing exit: {stuck}"
            )

    # 4. Run registered callbacks (semantic checkpoint, DB/Chroma flush+close, SIEM
    #    stop, session/journal save, TTS stop) concurrently — now that writers are
    #    stopped. Independent subsystems, so no reason to pay each latency serially.
    async def _run_cb(cb):
        name = getattr(cb, "__name__", repr(cb))
        try:
            await asyncio.wait_for(cb(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug(f"SHUTDOWN: callback {name} timed out")
        except Exception as e:
            logger.debug(f"SHUTDOWN: callback {name} error: {e}")

    if _shutdown_callbacks:
        await asyncio.gather(*(_run_cb(cb) for cb in _shutdown_callbacks))

    # 5. Final audit log entry.
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

    # 6. Mark terminal state — the runtime is fully stopped.
    _lc.mark_stopped()

    elapsed = round((time.monotonic() - t0) * 1000, 1)
    logger.info(f"SHUTDOWN: complete in {elapsed}ms — goodbye")
