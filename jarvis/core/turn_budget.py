"""
core/turn_budget.py — V69 M54.5: end-to-end interactive-turn budget.

The live run bounded only the final verifier CALL (20 s) while the *whole* turn ran
for ~12 minutes — model loading, queue waiting, routing, generation and verification
orchestration were all unbounded (symptom #5). This module gives the turn a single
real deadline and lets the verifier receive only the REMAINING budget, never a fresh
full timeout.

A ``TurnBudget`` is created at the start of a turn with a risk-aware total, stamps
per-phase elapsed times (routing / queue_wait / model_load / generation / tool /
verification), and answers `remaining_s()` / `expired()`. It is pure and clock
-injectable (deterministic tests). A small bounded ring records completed-turn
latencies for runtime health.
"""
from __future__ import annotations

import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

# ── Risk-aware total budgets (seconds) for the CPU-bound host. Interactive
# responsiveness matters more than an extra model call, so simple turns are tight
# and only genuinely effectful/security turns get a long ceiling. ─────────────
_BUDGET_BY_POLICY: dict[str, float] = {
    # VerifyPolicy.value -> total turn budget
    "SKIP_LLM_VERIFIER": 25.0,          # greeting / basic education / time
    "DETERMINISTIC_CHECKS_ONLY": 35.0,  # general educational
    "GROUNDING_CHECK": 60.0,            # private-document factual answer
    "EVIDENCE_REFERENCE_CHECK": 60.0,   # operational state answer
    "BOUNDED_MODEL_VERIFIER": 90.0,     # cyber-sensitive procedural content
    "FULL_VERIFICATION": 120.0,         # effectful action recommendation + HITL
}
_DEFAULT_BUDGET_S = 45.0
# Below this remaining budget the verifier is not worth starting (a cold model swap
# alone can exceed it) — surface human-review status instead of blocking.
_MIN_VERIFIER_BUDGET_S = 4.0

_PHASES = ("routing", "queue_wait", "model_load", "generation", "tool", "verification")


def budget_for(turn_policy=None) -> float:
    """Total turn budget (seconds) for *turn_policy*. Falls back to a safe default
    for an unknown/None policy."""
    if turn_policy is None:
        return _DEFAULT_BUDGET_S
    vp = getattr(turn_policy, "verify_policy", None)
    key = getattr(vp, "value", None) or str(vp)
    return _BUDGET_BY_POLICY.get(key, _DEFAULT_BUDGET_S)


@dataclass
class TurnBudget:
    """Per-turn deadline + phase-timing ledger."""

    total_s: float = _DEFAULT_BUDGET_S
    clock: Callable[[], float] = time.monotonic
    _t0: float = field(default=0.0)
    _phase_s: dict = field(default_factory=lambda: {p: 0.0 for p in _PHASES})

    def __post_init__(self) -> None:
        self._t0 = self.clock()

    def elapsed_s(self) -> float:
        return self.clock() - self._t0

    def remaining_s(self) -> float:
        return max(0.0, self.total_s - self.elapsed_s())

    def expired(self) -> bool:
        return self.elapsed_s() >= self.total_s

    def record(self, phase: str, seconds: float) -> None:
        if phase in self._phase_s:
            self._phase_s[phase] += max(0.0, seconds)

    @contextmanager
    def phase(self, name: str):
        """Time a code block into *name* (accumulates)."""
        start = self.clock()
        try:
            yield
        finally:
            self.record(name, self.clock() - start)

    def verifier_budget_s(self, requested: float) -> float:
        """The verifier may use at most the smaller of its policy timeout and the
        REMAINING turn budget — never a fresh full timeout on an already-slow turn."""
        return max(0.0, min(requested, self.remaining_s()))

    def can_afford_verifier(self) -> bool:
        """True only if enough turn budget remains to be worth a verifier pass."""
        return self.remaining_s() >= _MIN_VERIFIER_BUDGET_S

    def snapshot(self) -> dict:
        ms = {f"{k}_ms": round(v * 1000.0, 1) for k, v in self._phase_s.items()}
        ms["total_turn_ms"] = round(self.elapsed_s() * 1000.0, 1)
        ms["budget_ms"] = round(self.total_s * 1000.0, 1)
        ms["remaining_ms"] = round(self.remaining_s() * 1000.0, 1)
        ms["expired"] = self.expired()
        return ms


# ── Bounded latency ring for runtime health ──────────────────────────────────
_TURN_SAMPLES: "deque[dict]" = deque(maxlen=50)


def record_turn(snapshot: dict) -> None:
    """Record one completed-turn latency snapshot (bounded, deterministic)."""
    _TURN_SAMPLES.append(dict(snapshot))


def turn_latency_stats() -> dict:
    """Read-only turn-latency rollup for runtime health."""
    samples = list(_TURN_SAMPLES)
    if not samples:
        return {"count": 0, "avg_total_ms": 0.0, "max_total_ms": 0.0, "expired": 0}
    totals = [s.get("total_turn_ms", 0.0) for s in samples]
    return {
        "count": len(samples),
        "avg_total_ms": round(sum(totals) / len(totals), 1),
        "max_total_ms": round(max(totals), 1),
        "last_total_ms": totals[-1],
        "expired": sum(1 for s in samples if s.get("expired")),
    }
