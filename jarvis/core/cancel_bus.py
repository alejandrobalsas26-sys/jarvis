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
import uuid
from dataclasses import dataclass, field

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

# ── Per-execution cancellation (V69 M68A §D) ──────────────────────────────────
#
# The four events above are per-KIND and global. That is correct for the singleton
# streams; it was wrong for anything that can run concurrently with itself, and an
# external audit found the consequences in `run_agentic_incident`:
#
#   * identity was the literal string "agentic_loop", so two incidents shared one
#     registry slot — run B's `unregister_operation` erased run A while A was
#     still executing effectful tools;
#   * the loop CLEARED the shared event at start-up, so launching run B silently
#     revoked the operator's cancellation of run A, and A carried on.
#
# A handle fixes the identity: one per execution, with its own event, and nobody
# can clear anybody else's. The generation counter below fixes the second half
# without reintroducing a clear.

#: Monotonic count of system-wide aborts. An execution records the value it saw at
#: registration; any abort fired AFTER that cancels it. Nothing ever decrements
#: this — which is exactly why no run can un-cancel another (or itself) and why
#: `reset_all` cannot resurrect a cancelled run. An abort fired BEFORE a run
#: existed does not cancel it, so a fresh execution needs no clear to start clean.
_cancel_generation: int = 0

#: token → handle, for visibility and targeted cancellation. Keyed by a unique
#: token, never by kind: popping one execution can never remove another.
_executions: dict[str, "ExecutionHandle"] = {}


@dataclass(frozen=True)
class ExecutionHandle:
    """One execution's OWN cancellation identity and absolute deadline.

    Frozen on purpose. The mutable parts of a cancellation live in the event and
    in the module-level generation; a handle whose ``token``, ``kind`` or
    ``deadline`` could be reassigned would be a shared identity again, which is
    the defect this type exists to remove.
    """

    kind: str
    token: str
    event: asyncio.Event = field(repr=False)
    #: `_cancel_generation` as observed when this execution registered.
    generation: int = 0
    #: `time.monotonic()` at registration.
    started: float = 0.0
    #: Absolute monotonic deadline, or None for "no deadline". ABSOLUTE, not a
    #: per-step budget: a deadline that is recomputed per step is not a deadline.
    deadline: float | None = None

    @property
    def name(self) -> str:
        """The visibility key. Unique, and it carries the kind for a human."""
        return f"{self.kind}#{self.token[:8]}"

    def cancelled(self) -> bool:
        """Whether THIS execution has been cancelled."""
        return self.event.is_set() or _cancel_generation > self.generation

    def cancel(self) -> None:
        """Cancel this execution only. Never reaches another's state."""
        if not self.event.is_set():
            self.event.set()

    def remaining(self) -> float:
        """Seconds left before the absolute deadline; ``inf`` when there is none.

        Clamped at 0.0: a passed deadline yields no budget rather than a negative
        timeout, which `asyncio.wait_for` would treat as "fire immediately" in a
        way that reads like a cancellation instead of an expiry.
        """
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def elapsed(self) -> float:
        return time.monotonic() - self.started


def register_execution(kind: str, *, timeout: float | None = None) -> ExecutionHandle:
    """Register ONE execution and return its own cancellation handle.

    ``timeout`` is turned into an ABSOLUTE deadline here, once, at the start of
    the run — so every later check compares against the same instant no matter how
    many steps, retries or nested awaits sit in between.
    """
    token = uuid.uuid4().hex
    handle = ExecutionHandle(
        kind=kind,
        token=token,
        event=asyncio.Event(),
        generation=_cancel_generation,
        started=time.monotonic(),
        deadline=(time.monotonic() + float(timeout)) if timeout else None,
    )
    _executions[token] = handle
    return handle


def unregister_execution(handle: ExecutionHandle) -> None:
    """Deregister ONE execution, by token. Idempotent.

    The bug this replaces was ``_active_operations.pop("agentic_loop")``: a bare
    kind, so whichever run finished first deregistered every run of that kind.
    """
    _executions.pop(handle.token, None)


def cancel_execution(token: str) -> bool:
    """Cancel one execution by token. Returns whether a live one was found."""
    handle = _executions.get(token)
    if handle is None:
        return False
    handle.cancel()
    return True


def get_active_executions() -> dict[str, float]:
    """Every live execution, by unique name → elapsed seconds."""
    return {h.name: round(h.elapsed(), 1) for h in _executions.values()}


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
    """Return currently active operations with elapsed seconds.

    V69 M68A §D — per-execution handles are AGGREGATED BY KIND here, reporting the
    longest-running of each. Callers ask this question by kind
    (``"agentic_loop" not in ops``), and answering per token would have silently
    told the hunt scheduler that no incident was running. The per-token view is
    :func:`get_active_executions`; the two are different questions.
    """
    now = time.monotonic()
    out = {name: round(now - start, 1)
           for name, start in _active_operations.items()}
    for handle in _executions.values():
        elapsed = round(now - handle.started, 1)
        # Longest-running wins: the aggregate must not shrink because a newer
        # execution of the same kind started.
        if elapsed > out.get(handle.kind, -1.0):
            out[handle.kind] = elapsed
    return out


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

    # V69 M68A §D — every execution registered BEFORE this abort is now cancelled,
    # and no later one is. Bumping a counter rather than setting each event keeps
    # the property true for a run that is mid-registration, and leaves nothing for
    # a subsequent run to have to clear.
    global _cancel_generation
    _cancel_generation += 1
    count += len(_executions)

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
    global _cancel_generation
    _cancel_generation += 1


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
    # V69 M68A §D — the execution REGISTRY is cleared (it is visibility state),
    # but `_cancel_generation` is deliberately NOT rewound. A reset is an operator
    # saying "ready for new work", not "the abort I just fired never happened": a
    # run that was cancelled stays cancelled, and only a NEW registration starts
    # clean. Un-cancelling a live execution here would be the audited defect with
    # a different name.
    _executions.clear()
    logger.info("CANCEL_BUS: all events reset — ready for new operations")


def cancel_llm_only() -> bool:
    """Cancel just the LLM stream without stopping other operations."""
    if llm_stream_cancel and not llm_stream_cancel.is_set():
        llm_stream_cancel.set()
        logger.info("CANCEL_BUS: LLM stream cancelled")
        return True
    return False
