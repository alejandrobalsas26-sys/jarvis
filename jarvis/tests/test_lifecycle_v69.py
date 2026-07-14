"""
tests/test_lifecycle_v69.py — V69 M54.10 global lifecycle state machine.

Locks the invariants every scheduler / task-creation / signal / shutdown seam
depends on: monotonic boot phases, exactly-once stopping, and truthful phase
timings. No event loop, no live services — pure deterministic state transitions
driven by an injected clock.
"""
from __future__ import annotations

from core.lifecycle import LifecycleManager, LifecycleState


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _lm() -> tuple[LifecycleManager, FakeClock]:
    clk = FakeClock()
    return LifecycleManager(clock=clk), clk


# ── Initial posture ───────────────────────────────────────────────────────────

def test_starts_in_starting_no_input():
    lm, _ = _lm()
    assert lm.state is LifecycleState.STARTING
    assert not lm.accepts_input()          # symptom #2: no input before TEXT_READY
    assert lm.can_start_task()
    assert not lm.is_stopping()


# ── Monotonic boot advancement ────────────────────────────────────────────────

def test_boot_phases_advance_monotonically_and_enable_input():
    lm, _ = _lm()
    assert lm.mark_text_ready() is True
    assert lm.accepts_input()              # user may type at TEXT_READY
    assert lm.mark_core_ready() is True
    assert lm.mark_operational() is True
    assert lm.state is LifecycleState.OPERATIONAL
    assert lm.accepts_input()


def test_advance_never_moves_backward():
    lm, _ = _lm()
    lm.mark_core_ready()                    # jump straight to CORE_READY
    assert lm.state is LifecycleState.CORE_READY
    # A lower target is ignored (monotonic).
    assert lm.mark_text_ready() is False
    assert lm.state is LifecycleState.CORE_READY


def test_advance_to_rejects_terminal_states():
    lm, _ = _lm()
    import pytest
    with pytest.raises(ValueError):
        lm.advance_to(LifecycleState.STOPPING)


# ── Exactly-once stopping (symptom #9) ────────────────────────────────────────

def test_begin_stopping_is_exactly_once():
    lm, _ = _lm()
    lm.mark_operational()
    assert lm.begin_stopping() is True      # first caller owns the shutdown
    assert lm.begin_stopping() is False     # repeated SIGINT: no second shutdown
    assert lm.begin_stopping() is False
    assert lm.is_stopping()


def test_no_task_or_input_after_stopping(monkeypatch=None):
    lm, _ = _lm()
    lm.mark_operational()
    lm.begin_stopping()
    assert not lm.can_start_task()          # symptom #10: no new background work
    assert not lm.accepts_input()
    assert not lm.can_narrate()             # normal narration halts
    assert lm.can_narrate(critical=True)    # shutdown-critical speech still allowed


def test_cannot_advance_boot_after_stopping():
    lm, _ = _lm()
    lm.begin_stopping()
    assert lm.mark_operational() is False
    assert lm.state is LifecycleState.STOPPING


# ── Terminal transitions ──────────────────────────────────────────────────────

def test_mark_stopped_and_failed():
    lm, _ = _lm()
    lm.begin_stopping()
    assert lm.mark_stopped() is True
    assert lm.mark_stopped() is False
    assert lm.state is LifecycleState.STOPPED
    assert not lm.can_narrate(critical=True)  # nothing after fully stopped

    lm2, _ = _lm()
    assert lm2.mark_failed() is True
    assert lm2.state is LifecycleState.FAILED
    assert not lm2.can_start_task()


# ── Phase timing ledger (M54.2 measurements) ─────────────────────────────────

def test_phase_timings_stamped_once_and_monotonic():
    lm, clk = _lm()
    clk.advance(0.10)
    lm.mark_text_ready()
    clk.advance(0.50)
    lm.mark_core_ready()
    clk.advance(1.00)
    lm.mark_operational()
    snap = lm.snapshot()
    assert snap["state"] == "OPERATIONAL"
    assert snap["text_ready_ms"] == 100.0
    assert snap["core_ready_ms"] == 600.0
    assert snap["operational_ready_ms"] == 1600.0
    # First-write-wins: re-reaching a phase does not move its stamp.
    lm.mark_operational()
    assert lm.snapshot()["operational_ready_ms"] == 1600.0


def test_stopping_elapsed_tracks_forced_exit_window():
    lm, clk = _lm()
    assert lm.stopping_elapsed_s() is None
    lm.begin_stopping()
    clk.advance(3.5)
    assert lm.stopping_elapsed_s() == 3.5
