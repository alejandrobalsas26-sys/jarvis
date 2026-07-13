"""
core/lifecycle.py — V69 M54.10: the single authoritative runtime lifecycle state.

The live execution proved that "am I still running?" and "am I ready for input?"
were answered ad hoc across the codebase: schedulers checked `get_active_operations`,
shutdown relied on call-order, boot readiness was implicit in `_main_async`'s
sequence, and each SIGINT independently set a shutdown event. Nothing owned the
*global* posture, so a hunt could start after shutdown began (symptom #10), input
was reachable before boot finished (symptom #2), and repeated Ctrl+C re-entered the
sequence (symptom #9).

This module is that single owner. It is a small, pure, dependency-light finite
state machine plus a monotonic phase-timing ledger. Everything that creates a
background task, narrates, accepts input, or begins a scheduler iteration consults
it. It never imports heavy runtime modules and never blocks — it is safe on the hot
path and in tests (no event loop required for reads).

States (monotonic forward except the terminal transitions):

    STARTING     process up; config + security controls loading; NO user input
    TEXT_READY   console coordinator + FAST routing up; user MAY type; warmup ongoing
    CORE_READY   model roles validated; memory metadata restored; executor ready
    OPERATIONAL  collectors / hunts / feeds / Whisper / integrations warmed
    STOPPING     shutdown initiated exactly once; NO new background work
    STOPPED      shutdown finished
    FAILED       unrecoverable startup/runtime fault

`begin_stopping()` is the idempotent gate the signal handler and shutdown driver
share: the first caller transitions to STOPPING and returns True (it owns the one
shutdown); every later caller returns False (already stopping). `can_start_task()`
is the guard every task-creation / scheduler-iteration seam consults.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum


class LifecycleState(IntEnum):
    """Runtime posture. IntEnum so readiness comparisons (`>=`) are cheap and the
    forward-only invariant among the boot states is expressible as ordering."""

    STARTING = 0
    TEXT_READY = 1
    CORE_READY = 2
    OPERATIONAL = 3
    STOPPING = 4
    STOPPED = 5
    FAILED = 6


# Boot states that advance monotonically upward toward OPERATIONAL.
_BOOT_ORDER = (
    LifecycleState.STARTING,
    LifecycleState.TEXT_READY,
    LifecycleState.CORE_READY,
    LifecycleState.OPERATIONAL,
)
_BOOT_SET = frozenset(_BOOT_ORDER)


@dataclass
class LifecycleManager:
    """Thread-safe lifecycle state + monotonic phase-timing ledger.

    All mutation is guarded by a single lock; reads of the current state are lock
    -free (a plain attribute read of an IntEnum is atomic in CPython) so the hot
    path — `can_start_task()`, `is_stopping()` — never contends. `clock` is
    injectable for deterministic tests (default: `time.monotonic`).
    """

    clock: "callable" = time.monotonic
    _state: LifecycleState = field(default=LifecycleState.STARTING)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    # phase name -> elapsed ms since construction, stamped once when first reached
    _phase_ms: dict[str, float] = field(default_factory=dict)
    _t0: float = field(default=0.0)
    _stopping_at: float | None = field(default=None)

    def __post_init__(self) -> None:
        self._t0 = self.clock()

    # ── Reads (lock-free) ────────────────────────────────────────────────────
    @property
    def state(self) -> LifecycleState:
        return self._state

    def is_stopping(self) -> bool:
        """True once shutdown has begun (STOPPING or STOPPED). The single question
        every scheduler/task-creation seam asks before starting new work."""
        return self._state >= LifecycleState.STOPPING

    def is_text_ready(self) -> bool:
        """True once the user is allowed to type (TEXT_READY..OPERATIONAL)."""
        return LifecycleState.TEXT_READY <= self._state <= LifecycleState.OPERATIONAL

    def accepts_input(self) -> bool:
        """Interactive input is accepted only in the ready band, never while
        stopping/stopped/failed/starting."""
        return self.is_text_ready()

    def can_start_task(self) -> bool:
        """True unless the runtime is stopping or has already failed. Consulted by
        every background-task / scheduler-iteration creation site so no new work is
        spawned after shutdown begins (fixes symptom #10)."""
        return self._state < LifecycleState.STOPPING

    def can_narrate(self, *, critical: bool = False) -> bool:
        """Normal narration stops at STOPPING; shutdown-critical speech is still
        allowed so the operator hears why it's exiting."""
        if critical:
            return self._state != LifecycleState.STOPPED
        return not self.is_stopping()

    # ── Boot advancement (monotonic) ─────────────────────────────────────────
    def advance_to(self, target: LifecycleState) -> bool:
        """Advance a boot phase forward. Never moves backward and never past
        STOPPING once stopping has begun. Returns whether the state changed.

        Only accepts the four boot states; use `begin_stopping()`/`mark_stopped()`
        /`mark_failed()` for the terminal transitions."""
        if target not in _BOOT_SET:
            raise ValueError(f"advance_to expects a boot state, got {target!r}")
        with self._lock:
            if self._state >= LifecycleState.STOPPING:
                return False  # never resurrect a stopping/failed runtime
            if target <= self._state:
                return False  # monotonic — ignore same/backward
            self._state = target
            self._stamp_phase(target.name)
            return True

    def mark_text_ready(self) -> bool:
        return self.advance_to(LifecycleState.TEXT_READY)

    def mark_core_ready(self) -> bool:
        return self.advance_to(LifecycleState.CORE_READY)

    def mark_operational(self) -> bool:
        return self.advance_to(LifecycleState.OPERATIONAL)

    # ── Terminal transitions ─────────────────────────────────────────────────
    def begin_stopping(self) -> bool:
        """Idempotent shutdown gate. The FIRST caller transitions to STOPPING,
        stamps the time, and returns True (it owns the single graceful shutdown).
        Every subsequent caller returns False — this is what makes repeated SIGINT
        start exactly one shutdown (fixes symptom #9). A FAILED runtime may still
        transition to STOPPING for cleanup."""
        with self._lock:
            if self._state >= LifecycleState.STOPPING:
                return False
            self._state = LifecycleState.STOPPING
            self._stopping_at = self.clock()
            self._stamp_phase("STOPPING")
            return True

    def mark_stopped(self) -> bool:
        with self._lock:
            if self._state == LifecycleState.STOPPED:
                return False
            self._state = LifecycleState.STOPPED
            self._stamp_phase("STOPPED")
            return True

    def mark_failed(self) -> bool:
        """Unrecoverable fault. Allowed from any non-terminal state."""
        with self._lock:
            if self._state in (LifecycleState.STOPPED, LifecycleState.FAILED):
                return False
            self._state = LifecycleState.FAILED
            self._stamp_phase("FAILED")
            return True

    def stopping_elapsed_s(self) -> float | None:
        """Seconds since STOPPING began, or None if not stopping. Used by the
        second-stage forced-exit policy (M54.11)."""
        if self._stopping_at is None:
            return None
        return self.clock() - self._stopping_at

    # ── Phase timing ledger ──────────────────────────────────────────────────
    def _stamp_phase(self, name: str) -> None:
        # First-write-wins: a phase time is the moment it was first reached.
        self._phase_ms.setdefault(name, round((self.clock() - self._t0) * 1000.0, 1))

    def phase_timings_ms(self) -> dict[str, float]:
        """Read-only snapshot of when each phase was first reached (ms since start).
        Exposed through runtime health so process_started/text_ready/core_ready/
        operational latency is measurable (M54.2)."""
        with self._lock:
            return dict(self._phase_ms)

    def snapshot(self) -> dict:
        """Bounded, JSON-ready lifecycle view for runtime health / diagnostics."""
        ph = self.phase_timings_ms()
        return {
            "state": self._state.name,
            "is_stopping": self.is_stopping(),
            "accepts_input": self.accepts_input(),
            "process_started_ms": ph.get("STARTING", 0.0),
            "text_ready_ms": ph.get("TEXT_READY"),
            "core_ready_ms": ph.get("CORE_READY"),
            "operational_ready_ms": ph.get("OPERATIONAL"),
            "phase_ms": ph,
        }


# ── Process-global singleton ─────────────────────────────────────────────────
# One lifecycle per process. Modules import `lifecycle` and consult it directly;
# tests can construct an isolated LifecycleManager or call `reset_lifecycle()`.
lifecycle = LifecycleManager()


def get_lifecycle() -> LifecycleManager:
    """Return the process-global lifecycle manager."""
    return lifecycle


def reset_lifecycle(clock: "callable" = time.monotonic) -> LifecycleManager:
    """Reset the global lifecycle (tests + a fresh process). Returns the new
    manager. NOT for production use mid-run."""
    global lifecycle
    lifecycle = LifecycleManager(clock=clock)
    return lifecycle


# ── Convenience module-level guards (read the current global) ────────────────
def is_stopping() -> bool:
    return lifecycle.is_stopping()


def can_start_task() -> bool:
    return lifecycle.can_start_task()


def accepts_input() -> bool:
    return lifecycle.accepts_input()


def begin_stopping() -> bool:
    return lifecycle.begin_stopping()
