"""
tests/test_turn_budget_v69.py — V69 M54.5 end-to-end turn budget.

Locks symptom #5: the whole turn has a real deadline, the verifier receives only the
REMAINING budget (never a fresh full timeout), and once the budget is exhausted the
verifier is skipped rather than blocking for minutes. Deterministic via an injected
clock.
"""
from __future__ import annotations

from core.turn_budget import (
    TurnBudget,
    budget_for,
    record_turn,
    turn_latency_stats,
)
from core.turn_policy import classify_request


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ── Risk-aware budgets ────────────────────────────────────────────────────────

def test_budget_scales_with_policy():
    greeting = classify_request("hola")
    educational = classify_request("¿Qué es POO?")
    effectful = classify_request("run a scan on 10.0.0.5 and open Wireshark")
    assert budget_for(greeting) <= budget_for(educational) <= budget_for(effectful)


def test_budget_default_for_none():
    assert budget_for(None) > 0


# ── Remaining budget drives the verifier ──────────────────────────────────────

def test_remaining_decreases_with_time():
    clk = FakeClock()
    b = TurnBudget(total_s=45.0, clock=clk)
    assert b.remaining_s() == 45.0
    clk.advance(30.0)
    assert b.remaining_s() == 15.0
    assert not b.expired()
    clk.advance(20.0)
    assert b.remaining_s() == 0.0
    assert b.expired()


def test_verifier_gets_only_remaining_budget():
    clk = FakeClock()
    b = TurnBudget(total_s=45.0, clock=clk)
    clk.advance(32.0)   # routing + generation + tool already spent 32s
    # A 20s verifier request is capped to the 13s remaining (the directive's example).
    assert b.verifier_budget_s(20.0) == 13.0


def test_no_verifier_when_budget_exhausted():
    clk = FakeClock()
    b = TurnBudget(total_s=45.0, clock=clk)
    clk.advance(44.0)
    assert b.can_afford_verifier() is False   # < 4s floor
    assert b.verifier_budget_s(20.0) == 1.0


def test_can_afford_verifier_true_with_headroom():
    clk = FakeClock()
    b = TurnBudget(total_s=45.0, clock=clk)
    clk.advance(10.0)
    assert b.can_afford_verifier() is True


# ── Phase timing ledger ───────────────────────────────────────────────────────

def test_phase_context_accumulates():
    clk = FakeClock()
    b = TurnBudget(total_s=45.0, clock=clk)
    with b.phase("generation"):
        clk.advance(5.0)
    with b.phase("verification"):
        clk.advance(2.0)
    snap = b.snapshot()
    assert snap["generation_ms"] == 5000.0
    assert snap["verification_ms"] == 2000.0
    assert snap["total_turn_ms"] == 7000.0


# ── Latency ring for health ───────────────────────────────────────────────────

def test_record_turn_and_stats():
    clk = FakeClock()
    b = TurnBudget(total_s=45.0, clock=clk)
    clk.advance(12.0)
    record_turn(b.snapshot())
    stats = turn_latency_stats()
    assert stats["count"] >= 1
    assert stats["max_total_ms"] >= 12000.0
