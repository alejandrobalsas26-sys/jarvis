"""
core/turn_budget.py — V69 M54.5 / M54.1.5-.7: end-to-end interactive-turn budget.

The live run bounded only the final verifier CALL (20 s) while the *whole* turn ran
for ~12 minutes — model loading, queue waiting, routing, generation and verification
orchestration were all unbounded (symptom #5). This module gives the turn a single
real deadline and lets the verifier receive only the REMAINING budget, never a fresh
full timeout.

M54.1.5 — WHY M54 DID NOT ACTUALLY FIX THIS. `TurnBudget` shipped as a passive
stopwatch: pure arithmetic over an injected clock, with no `asyncio` import at all.
It has to be voluntarily polled, and the generation path never polled it — `_budget`
reached exactly ONE call site repo-wide (`budget=_budget` into the verifier). So M54
deadlined the one stage that was already bounded and left the stage that hung
untouched. Worse, the turn's only real bound was an accident: `AsyncOpenAI(...)` was
constructed with no `timeout=`, inheriting the SDK default `read=600` — ten minutes
nobody chose. On the failing turn ("como saco la raiz cubica de algo" →
SKIP_LLM_VERIFIER) the budget was constructed, read by nobody, and snapshotted.

So this module now carries BOTH halves:

  * the ledger (unchanged API: remaining_s/expired/phase/verifier_budget_s), and
  * `StageTimeouts` + `bounded_stream()`, a real cancellation boundary that wraps
    the whole user-visible operation, bounds first-token and idle-stream waits
    separately, closes the async generator, and reports WHICH stage timed out.

A ``TurnBudget`` is created at the start of a turn with a risk-aware total, stamps
per-phase elapsed times (routing / queue_wait / model_load / generation / tool /
verification), and answers `remaining_s()` / `expired()`. It is pure and clock
-injectable (deterministic tests). A small bounded ring records completed-turn
latencies for runtime health.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

# ── Risk-aware total budgets (seconds) for the CPU-bound host. ────────────────
# M54.1.7 — these were RAISED from the M54 values (25/35/60/60/90/120) because
# those were calibrated against a warm model and made a cold turn hopeless: on this
# 15W Ryzen 5 7430U with OLLAMA_MAX_LOADED_MODELS=1, a boot-time nomic embedding
# load EVICTS qwen3:8b, so the operator's first real turn pays a full cold reload
# (weights off disk + prefill) before a single token appears. A 25 s ceiling on
# "simple education" could not be met by physics, so the honest ceiling is higher —
# what matters is that it is FINITE and that the prompt always comes back. Warm
# turns still complete far faster; the ceiling is a backstop, not a target.
# Calibrated against the live measurements recorded in
# docs/V69_M54_1_RUNTIME_BACKPRESSURE_FIRST_TURN.md.
_BUDGET_BY_POLICY: dict[str, float] = {
    # VerifyPolicy.value -> total turn budget
    "SKIP_LLM_VERIFIER": 60.0,          # greeting / basic education / time
    "DETERMINISTIC_CHECKS_ONLY": 60.0,  # general educational
    "GROUNDING_CHECK": 75.0,            # private-document factual answer
    "EVIDENCE_REFERENCE_CHECK": 75.0,   # operational state answer
    "BOUNDED_MODEL_VERIFIER": 90.0,     # cyber-sensitive procedural content
    "FULL_VERIFICATION": 120.0,         # effectful action recommendation + HITL
}
_DEFAULT_BUDGET_S = 60.0

# Absolute ceiling for ANY operator override. An env typo must never create an
# unbounded (or absurd) wait — that is the failure we are fixing.
_MAX_TOTAL_S = 300.0
_MIN_TOTAL_S = 5.0
# Below this remaining budget the verifier is not worth starting (a cold model swap
# alone can exceed it) — surface human-review status instead of blocking.
_MIN_VERIFIER_BUDGET_S = 4.0

_PHASES = ("routing", "queue_wait", "connect", "model_load", "first_token",
           "generation", "tool", "verification")
# Phases Ollama cannot report separately from first-token latency. We refuse to
# invent a split: they stay `null` in the snapshot unless something really
# measured them (M54.1.6 — "do not fake model-load timing").
_UNOBSERVABLE_PHASES = ("model_load", "connect")


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
    # M54.1.6 outcome ledger — which bound fired, and whether teardown worked.
    timeout_stage: str | None = field(default=None)
    cancel_success: bool | None = field(default=None)
    model_role: str | None = field(default=None)

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
        ms: dict = {}
        for k, v in self._phase_s.items():
            # Never report 0.0 for a phase we cannot actually observe — that reads
            # as "instant" when the truth is "unknown".
            if v == 0.0 and k in _UNOBSERVABLE_PHASES:
                ms[f"{k}_ms"] = None
            else:
                ms[f"{k}_ms"] = round(v * 1000.0, 1)
        ms["total_turn_ms"] = round(self.elapsed_s() * 1000.0, 1)
        ms["budget_ms"] = round(self.total_s * 1000.0, 1)
        ms["remaining_ms"] = round(self.remaining_s() * 1000.0, 1)
        ms["expired"] = self.expired()
        ms["timeout_stage"] = self.timeout_stage
        ms["cancel_success"] = self.cancel_success
        ms["model_role"] = self.model_role
        return ms


# ── M54.1.6 — separate, observable stage bounds ──────────────────────────────
class TurnTimeout(Exception):
    """The turn exceeded a bound. `stage` names WHICH one, so runtime health can
    report the truth instead of a generic 'slow'."""

    def __init__(self, stage: str, limit_s: float = 0.0) -> None:
        super().__init__(f"turn timed out at stage={stage} (limit={limit_s:.1f}s)")
        self.stage = stage
        self.limit_s = limit_s


@dataclass(frozen=True)
class StageTimeouts:
    """Per-stage bounds. A server that connects but never yields a token must not
    hold the user indefinitely; a stream that starts then stalls must be cancelled.

    queue_wait_s   waiting for the inference lock / model queue
    connect_s      TCP+HTTP connection establishment
    first_token_s  connect -> first streamed token (covers Ollama's model swap;
                   this is the wait that hung the live run for minutes)
    idle_s         maximum gap BETWEEN chunks once streaming has begun
    total_s        the whole user-visible turn (the outer boundary)
    """

    queue_wait_s: float = 30.0
    connect_s: float = 5.0
    first_token_s: float = 45.0
    idle_s: float = 20.0
    total_s: float = _DEFAULT_BUDGET_S

    def clamped(self) -> "StageTimeouts":
        """No stage bound may exceed the total — a stage timeout that can never
        fire before the total is just dead configuration."""
        total = max(_MIN_TOTAL_S, min(self.total_s, _MAX_TOTAL_S))
        return StageTimeouts(
            queue_wait_s=max(0.5, min(self.queue_wait_s, total)),
            connect_s=max(0.5, min(self.connect_s, total)),
            first_token_s=max(1.0, min(self.first_token_s, total)),
            idle_s=max(1.0, min(self.idle_s, total)),
            total_s=total,
        )


def timeouts_for(turn_policy=None, *, settings=None) -> StageTimeouts:
    """Stage bounds for `turn_policy`, honoring operator overrides within safe
    caps. core.config is the single source of truth — never os.getenv here."""
    total = budget_for(turn_policy)
    if settings is None:
        try:
            from core.config import settings as _s
            settings = _s
        except Exception:
            settings = None
    if settings is not None:
        scale = float(getattr(settings, "turn_budget_scale", 1.0) or 1.0)
        total *= max(0.25, min(scale, 3.0))
        ft = getattr(settings, "turn_first_token_timeout_s", None)
        idle = getattr(settings, "turn_stream_idle_timeout_s", None)
        return StageTimeouts(
            first_token_s=float(ft) if ft else StageTimeouts.first_token_s,
            idle_s=float(idle) if idle else StageTimeouts.idle_s,
            total_s=total,
        ).clamped()
    return StageTimeouts(total_s=total).clamped()


async def _safe_aclose(agen) -> bool:
    """Close an async generator, bounded and never raising.

    This is the M54.1.5 requirement 'close the async generator with aclose()':
    it throws GeneratorExit at the generator's suspension point, which runs its
    `finally` blocks — releasing the inference lock and closing the live Ollama
    HTTP response instead of leaking a pooled connection (a leaked pool slot would
    make the NEXT turn hang on pool acquisition).
    """
    close = getattr(agen, "aclose", None)
    if close is None:
        return False
    try:
        await asyncio.wait_for(close(), timeout=2.0)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError, Exception):
        # Cleanup must never itself hang or crash the turn.
        return False


async def bounded_stream(agen, *, budget: "TurnBudget",
                         timeouts: StageTimeouts | None = None) -> AsyncIterator:
    """Yield from `agen` under real first-token / idle / total bounds.

    THE outer boundary M54 lacked. Notes on correctness with async generators:

      * each step awaits `agen.__anext__()` inside `asyncio.wait_for`, so a stall
        anywhere INSIDE the generator (HTTP connect, Ollama's model swap, a silent
        socket) is interrupted — not merely measured between stages;
      * `wait_for` cancels AND awaits the inner task before raising, so the
        generator is never left mid-flight when we close it (no "async generator
        is already running");
      * `aclose()` always runs in `finally`, on every exit path including
        cancellation, so locks/HTTP are released and NO late chunk can be yielded
        after the deadline;
      * the effective per-step wait is min(stage bound, remaining total), so the
        total is a true ceiling: queue wait and first-token wait both count
        against it by construction.
    """
    t = (timeouts or StageTimeouts(total_s=budget.total_s)).clamped()
    first = True
    try:
        while True:
            remaining = budget.remaining_s()
            if remaining <= 0.0:
                raise TurnTimeout("total", t.total_s)
            limit = t.first_token_s if first else t.idle_s
            wait = min(limit, remaining)
            try:
                chunk = await asyncio.wait_for(agen.__anext__(), timeout=wait)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                # Attribute the timeout to the stage that actually ran out.
                if budget.remaining_s() <= 0.0:
                    raise TurnTimeout("total", t.total_s) from None
                raise TurnTimeout("first_token" if first else "stream_idle",
                                  limit) from None
            if first:
                # Honest attribution: Ollama does not report model-load separately
                # from prefill, so we record the OBSERVABLE quantity (time to first
                # token, which CONTAINS queue wait + model swap + connect) and
                # leave model_load unknown rather than inventing a split.
                budget.record("first_token", budget.elapsed_s())
                first = False
            yield chunk
    finally:
        # Runs on success, timeout AND cancellation — the generator is always
        # closed, so no orphan inference keeps burning CPU invisibly.
        await _safe_aclose(agen)


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
