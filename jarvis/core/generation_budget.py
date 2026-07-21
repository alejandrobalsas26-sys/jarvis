"""core/generation_budget.py — V69 M57.2/M57.8.1: intent-aware generation budgets.

Turns a :class:`~core.response_contract.ResponseShape` into the ACTUAL native Ollama
generation options for one turn, and adapts the token budget conservatively to the
throughput this host is really achieving.

THE MEASURED PROBLEM
--------------------
M56 measured ~5.2-6.4 tokens/second sustained on the target host. Every FAST turn —
greeting or Kerberos deep-dive — asked for the same ``num_predict=256`` (config
default). At 6 tok/s a 256-token answer is ~43 s of generation, so a greeting that
happens to ramble costs the operator most of a minute. The cap was a SAFETY bound,
never an intent-aware budget.

THE num_ctx INVARIANT (M56 REGRESSION FOUND IN M57)
---------------------------------------------------
M56.4 proved Ollama reloads the model runner when generation parameters change:
warming at ``num_ctx=512`` and serving at 2048 cost **8 723 ms** of load on an
already-resident model, versus **411 ms** when they matched. M56 fixed the PREWARM
to read ``settings.fast_context``.

It did not fix the LIVE turn. ``core.llm._adaptive_ctx`` shrinks a short
conversation to ``min(1024, base_ctx)`` — so the operator's first real turn asks for
``num_ctx=1024`` against a runner warmed at 2048 and pays the full reload anyway.
This module therefore owns ONE rule for the native FAST path:

    the live num_ctx IS ``core.fast_prewarm.resolve_fast_context()`` — the same
    single setting the prewarm reads. No per-turn context shrinking on the path the
    prewarm warms.

Pure, deterministic and I/O-free apart from reading ``core.config`` defaults.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from core.response_contract import (
    HARD_MAX_OUTPUT_TOKENS,
    HARD_MIN_OUTPUT_TOKENS,
    ResponseContract,
    ResponseShape,
)

# Sampling/penalty posture per contract. Conservative and closed — a contract may
# not invent options, and nothing here is operator-writable at runtime.
_SAMPLING: dict[ResponseContract, tuple[float, float, float]] = {
    # contract: (temperature, top_p, repeat_penalty)
    ResponseContract.INSTANT: (0.2, 0.90, 1.10),
    ResponseContract.BRIEF: (0.3, 0.90, 1.10),
    ResponseContract.STANDARD: (0.35, 0.90, 1.12),
    ResponseContract.TECHNICAL: (0.4, 0.92, 1.15),
    ResponseContract.STRUCTURED: (0.3, 0.90, 1.15),
    # Code legitimately repeats tokens (indentation, identifiers) — penalising it
    # produces subtly wrong programs, so CODE gets the LOWEST repeat penalty.
    ResponseContract.CODE: (0.2, 0.95, 1.05),
    ResponseContract.DOCUMENT_GROUNDED: (0.2, 0.90, 1.10),
    ResponseContract.OPERATIONAL: (0.2, 0.90, 1.10),
    ResponseContract.DEEP: (0.5, 0.92, 1.15),
    ResponseContract.ERROR_RECOVERY: (0.3, 0.90, 1.12),
}
_DEFAULT_SAMPLING = (0.3, 0.90, 1.10)

# Throughput bounds for the adaptive layer. Anything outside this window is a
# measurement artefact (a cold load counted as generation, a 3-token reply), never
# a new belief about the host.
_MIN_PLAUSIBLE_TOK_S = 1.0
_MAX_PLAUSIBLE_TOK_S = 60.0
# Minimum samples before adaptation is allowed to move the budget at all.
_MIN_SAMPLES_FOR_ADAPT = 3
# The adaptive layer may move the base budget by at most this factor in one step,
# so one abnormal run can never distort policy.
_MAX_ADAPT_FACTOR = 1.5
_MIN_ADAPT_FACTOR = 0.5


@dataclass(frozen=True)
class GenerationBudget:
    """The concrete generation options for ONE turn. Bounded and inspectable."""

    contract: str
    reason: str
    num_predict: int
    num_ctx: int
    temperature: float
    top_p: float
    repeat_penalty: float
    keep_alive: str = "10m"
    # Wall-clock bounds (seconds) — the existing StageTimeouts/TurnBudget own
    # enforcement; these are the values this contract asks for.
    total_turn_s: float = 60.0
    first_token_s: float = 90.0
    idle_s: float = 20.0
    # Adaptation provenance (M57.8.1). Never contains prompt or answer content.
    base_num_predict: int = 0
    throughput_basis: float | None = None
    throughput_samples: int = 0
    adjustment_reason: str = "contract_base"
    target_completion_ms: int = 0

    def options(self) -> dict:
        """The extra native ``options`` this budget contributes beyond the scalar
        arguments ``chat_stream`` already takes (num_predict/temperature/num_ctx)."""
        return {"top_p": self.top_p, "repeat_penalty": self.repeat_penalty}

    def telemetry(self) -> dict:
        return {
            "contract": self.contract,
            "selection_reason": self.reason,
            "token_budget": self.num_predict,
            "base_token_budget": self.base_num_predict,
            "context_budget": self.num_ctx,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
            "keep_alive": self.keep_alive,
            "total_turn_s": self.total_turn_s,
            "first_token_s": self.first_token_s,
            "idle_s": self.idle_s,
            "throughput_basis": self.throughput_basis,
            "throughput_samples": self.throughput_samples,
            "adjustment_reason": self.adjustment_reason,
            "target_completion_ms": self.target_completion_ms,
        }


@dataclass
class ThroughputTracker:
    """Bounded rolling generation statistics for the adaptive budget.

    Deliberately small, deliberately median-based: the mean is dominated by one
    pathological cold turn, the median is not. Values outside the plausible window
    are REJECTED rather than clamped, so an artefact never becomes a belief.
    """

    maxlen: int = 20
    _tok_s: "deque[float]" = field(default_factory=lambda: deque(maxlen=20))
    _first_token_ms: "deque[float]" = field(default_factory=lambda: deque(maxlen=20))
    rejected: int = 0

    def __post_init__(self) -> None:
        n = max(3, min(int(self.maxlen), 100))
        self._tok_s = deque(self._tok_s, maxlen=n)
        self._first_token_ms = deque(self._first_token_ms, maxlen=n)

    def record(self, *, tokens_per_second: float | None = None,
               first_token_ms: float | None = None) -> None:
        """Record ONE completed turn's measurements. Never raises."""
        try:
            if tokens_per_second is not None:
                v = float(tokens_per_second)
                if _MIN_PLAUSIBLE_TOK_S <= v <= _MAX_PLAUSIBLE_TOK_S:
                    self._tok_s.append(v)
                else:
                    self.rejected += 1
            if first_token_ms is not None:
                ft = float(first_token_ms)
                if 0.0 < ft <= 600_000.0:
                    self._first_token_ms.append(ft)
        except (TypeError, ValueError):
            self.rejected += 1

    @property
    def samples(self) -> int:
        return len(self._tok_s)

    def estimate_tok_s(self) -> float | None:
        """Median observed throughput, or ``None`` when there is not enough data to
        justify moving a budget. Never extrapolates from a single run."""
        if len(self._tok_s) < _MIN_SAMPLES_FOR_ADAPT:
            return None
        return round(statistics.median(self._tok_s), 2)

    def estimate_first_token_ms(self) -> float | None:
        if not self._first_token_ms:
            return None
        return round(statistics.median(self._first_token_ms), 1)

    def snapshot(self) -> dict:
        return {
            "samples": self.samples,
            "median_tok_s": self.estimate_tok_s(),
            "median_first_token_ms": self.estimate_first_token_ms(),
            "rejected_samples": self.rejected,
        }

    def reset(self) -> None:
        self._tok_s.clear()
        self._first_token_ms.clear()
        self.rejected = 0


def resolve_live_fast_context(settings=None) -> int:
    """The num_ctx a native FAST turn MUST use.

    Single source of truth shared with :func:`core.fast_prewarm.resolve_fast_context`
    so the prewarm can never warm a runner configuration the live turn does not use
    (M56.4's 8.7 s reload). Deliberately NOT history-adaptive on this path.
    """
    if settings is not None:
        try:
            return int(getattr(settings, "fast_context", 2048))
        except (TypeError, ValueError):
            return 2048
    try:
        from core.fast_prewarm import resolve_fast_context
        return int(resolve_fast_context())
    except Exception:  # noqa: BLE001
        return 2048


def _operator_ceiling(settings) -> int:
    """The operator's hard output ceiling, clamped into the module's own window."""
    raw = 512
    if settings is not None:
        try:
            raw = int(getattr(settings, "response_max_output_tokens", 512))
        except (TypeError, ValueError):
            raw = 512
    return max(HARD_MIN_OUTPUT_TOKENS, min(raw, HARD_MAX_OUTPUT_TOKENS))


def _adapt(base: int, *, shape: ResponseShape, tok_s: float | None,
           samples: int, remaining_s: float | None) -> tuple[int, str, float | None]:
    """Move *base* toward the contract's latency target using measured throughput.

    Conservative by construction:
      * no movement at all below ``_MIN_SAMPLES_FOR_ADAPT`` samples;
      * movement is capped to [0.5x, 1.5x] of the contract base in one step;
      * the remaining turn budget can only SHRINK the result, never grow it;
      * the contract's own floor is never crossed — a safety-relevant answer is not
        allowed to become unusable because the host is slow.
    """
    if tok_s is None or samples < _MIN_SAMPLES_FOR_ADAPT:
        return base, "contract_base", tok_s
    target_s = max(1.0, shape.target_completion_ms / 1000.0)
    desired = int(target_s * tok_s)
    lo = int(base * _MIN_ADAPT_FACTOR)
    hi = int(base * _MAX_ADAPT_FACTOR)
    adapted = max(lo, min(desired, hi))
    reason = ("throughput_reduced" if adapted < base
              else "throughput_increased" if adapted > base
              else "throughput_neutral")
    if remaining_s is not None and remaining_s > 0.0:
        # Never ask for more tokens than the REMAINING turn time can produce; a cap
        # the deadline will cut is a truncation we chose to walk into.
        affordable = int(max(0.0, remaining_s - 1.0) * tok_s)
        if affordable and affordable < adapted:
            adapted = affordable
            reason = "remaining_turn_time"
    return adapted, reason, tok_s


def budget_for_shape(
    shape: ResponseShape,
    *,
    settings=None,
    throughput: ThroughputTracker | None = None,
    remaining_s: float | None = None,
    keep_alive: str | None = None,
    total_turn_s: float | None = None,
    first_token_s: float | None = None,
    idle_s: float | None = None,
    num_ctx: int | None = None,
) -> GenerationBudget:
    """Build the concrete generation budget for one turn. Total and deterministic.

    The resulting ``num_predict`` is clamped, in order, by:
      1. the contract's own [min, max] window (already power-reduced by M57.1),
      2. the operator ceiling ``settings.response_max_output_tokens``,
      3. the module's absolute [16, 1024] window.

    ``num_ctx`` defaults to the single live-FAST context so prewarm parity holds.
    """
    if settings is None:
        try:
            from core.config import settings as _s
            settings = _s
        except Exception:  # noqa: BLE001
            settings = None

    temp, top_p, rep = _SAMPLING.get(shape.contract, _DEFAULT_SAMPLING)
    base = int(shape.base_output_tokens)
    tok_s = throughput.estimate_tok_s() if throughput is not None else None
    samples = throughput.samples if throughput is not None else 0
    adapted, reason, basis = _adapt(base, shape=shape, tok_s=tok_s, samples=samples,
                                    remaining_s=remaining_s)

    ceiling = min(int(shape.max_output_tokens), _operator_ceiling(settings))
    floor = max(HARD_MIN_OUTPUT_TOKENS, min(int(shape.min_output_tokens), ceiling))
    num_predict = max(floor, min(adapted, ceiling, HARD_MAX_OUTPUT_TOKENS))

    ka = keep_alive if keep_alive is not None else (
        getattr(settings, "fast_keep_alive", "10m") if settings is not None else "10m")
    return GenerationBudget(
        contract=shape.contract.value,
        reason=shape.reason.value,
        num_predict=int(num_predict),
        num_ctx=int(num_ctx if num_ctx is not None
                    else resolve_live_fast_context(settings)),
        temperature=float(temp), top_p=float(top_p), repeat_penalty=float(rep),
        keep_alive=str(ka),
        total_turn_s=float(total_turn_s) if total_turn_s else 60.0,
        first_token_s=float(first_token_s) if first_token_s else 90.0,
        idle_s=float(idle_s) if idle_s else 20.0,
        base_num_predict=base,
        throughput_basis=basis, throughput_samples=samples,
        adjustment_reason=reason,
        target_completion_ms=int(shape.target_completion_ms),
    )


# ── Truncation truth (acceptance criterion 30) ───────────────────────────────
def hit_generation_cap(done_reason: str | None, eval_count: int | None,
                       num_predict: int) -> bool:
    """True when generation stopped because it ran out of BUDGET, not of content.

    Ollama reports ``done_reason="length"`` for this; the eval-count comparison is
    the belt-and-braces path for servers that omit it. Never guesses from text.
    """
    if (done_reason or "").strip().lower() == "length":
        return True
    try:
        return bool(eval_count) and bool(num_predict) and int(eval_count) >= int(num_predict)
    except (TypeError, ValueError):
        return False


_TRUNCATION_NOTE_ES = "(Respuesta acortada por el límite de longitud. Di «continúa» para seguir.)"
_TRUNCATION_NOTE_EN = "(Answer shortened by the length budget. Say \"continue\" to go on.)"


def truncation_note(language: str | None) -> str:
    """The bounded, truthful marker appended when the token cap cut the answer.

    Never pretends the requested explanation was completed (M57.2 requirement).
    """
    return (_TRUNCATION_NOTE_EN if str(language or "es").lower().startswith("en")
            else _TRUNCATION_NOTE_ES)


# ── Process-global tracker ───────────────────────────────────────────────────
_tracker: ThroughputTracker | None = None


def get_throughput_tracker() -> ThroughputTracker:
    global _tracker
    if _tracker is None:
        _tracker = ThroughputTracker()
    return _tracker


def reset_throughput_tracker(instance: "ThroughputTracker | None" = None) -> None:
    """Tests / a fresh process."""
    global _tracker
    _tracker = instance


def record_observed_turn(*, tokens_per_second: float | None,
                         first_token_ms: float | None = None,
                         tracker: ThroughputTracker | None = None,
                         clock: Callable[[], float] | None = None) -> None:
    """Fold ONE completed turn's measurements into the rolling estimate. Never
    raises — a metrics failure must never break a turn."""
    try:
        (tracker or get_throughput_tracker()).record(
            tokens_per_second=tokens_per_second, first_token_ms=first_token_ms)
    except Exception:  # noqa: BLE001
        pass
