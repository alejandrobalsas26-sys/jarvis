"""core/response_runtime.py — V69 M57: the single response-pipeline state holder.

ONE process-global object owns everything the adaptive response pipeline needs to
remember BETWEEN turns:

  * the session response profile (``/brief`` | ``/standard`` | ``/detailed``),
  * the speech mute flag (``/mute`` | ``/unmute``),
  * the rolling throughput estimate that moves generation budgets (M57.8.1),
  * the monotonic turn counter and the CURRENT turn handle, which is what makes
    late-chunk suppression and turn replacement possible at all (M57.5),
  * bounded, content-free counters for the advisory runtime-health subsystem.

WHY A SINGLETON AND NOT A NEW SUBSYSTEM
---------------------------------------
The runtime already has one lifecycle FSM, one console coordinator, one cancel bus
and one health collector. M57 adds ONE more small piece of shared state rather than
a parallel registry: commands in ``main`` mutate it, ``core.llm`` reads it per turn,
and ``core.runtime_health`` snapshots it. Nothing here holds prompts, answers,
secrets or model state — only enums, counters and timings.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from core.generation_budget import ThroughputTracker
from core.response_contract import ResponseProfile, parse_response_profile


class TurnState(str, Enum):
    """The terminal (or current) state of ONE interactive turn.

    Exactly one of these is true at any moment for the active turn, and the value
    a finished turn carries is the value its history entry is labelled with.
    """

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    INTERRUPTED_BY_OPERATOR = "INTERRUPTED_BY_OPERATOR"
    REPLACED_BY_NEW_TURN = "REPLACED_BY_NEW_TURN"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"
    CANCELLED_ON_SHUTDOWN = "CANCELLED_ON_SHUTDOWN"


TERMINAL_STATES: frozenset[TurnState] = frozenset({
    TurnState.COMPLETED, TurnState.INTERRUPTED_BY_OPERATOR,
    TurnState.REPLACED_BY_NEW_TURN, TurnState.TIMED_OUT, TurnState.FAILED,
    TurnState.CANCELLED_ON_SHUTDOWN,
})
# States in which the answer the operator saw is INCOMPLETE. History must say so.
INCOMPLETE_STATES: frozenset[TurnState] = frozenset({
    TurnState.INTERRUPTED_BY_OPERATOR, TurnState.REPLACED_BY_NEW_TURN,
    TurnState.TIMED_OUT, TurnState.FAILED, TurnState.CANCELLED_ON_SHUTDOWN,
})


@dataclass
class TurnHandle:
    """Bounded per-turn record. NEVER stores the prompt or the answer text.

    ``turn_id`` is the mechanism that makes a replaced turn harmless: any output
    carrying a stale id is dropped at the presentation boundary instead of being
    printed on top of the new answer.
    """

    turn_id: int
    state: TurnState = TurnState.ACTIVE
    contract: str = ""
    selection_reason: str = ""
    language: str = "es"
    token_budget: int = 0
    context_budget: int = 0
    started_at: float = 0.0
    ended_at: float | None = None
    first_fragment_ms: float | None = None
    first_sentence_ms: float | None = None
    first_utterance_ms: float | None = None
    chars_shown: int = 0
    truncated_by_cap: bool = False
    continuation_available: bool = False

    def is_active(self) -> bool:
        return self.state is TurnState.ACTIVE

    def duration_ms(self) -> float | None:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000.0, 1)

    def snapshot(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "state": self.state.value,
            "contract": self.contract,
            "selection_reason": self.selection_reason,
            "language": self.language,
            "token_budget": self.token_budget,
            "context_budget": self.context_budget,
            "first_fragment_ms": self.first_fragment_ms,
            "first_sentence_ms": self.first_sentence_ms,
            "first_utterance_ms": self.first_utterance_ms,
            "chars_shown": self.chars_shown,
            "truncated_by_cap": self.truncated_by_cap,
            "continuation_available": self.continuation_available,
            "duration_ms": self.duration_ms(),
        }


@dataclass
class ResponseRuntime:
    """Session-scoped response-pipeline state. Bounded and content-free."""

    profile: ResponseProfile = ResponseProfile.AUTO
    muted: bool = False
    clock: Callable[[], float] = time.monotonic
    throughput: ThroughputTracker = field(default_factory=ThroughputTracker)

    _turn_seq: int = 0
    current: TurnHandle | None = None
    _recent: "deque[dict]" = field(default_factory=lambda: deque(maxlen=20))

    # Bounded counters (advisory health only).
    turns_started: int = 0
    turns_completed: int = 0
    interrupted_turns: int = 0
    replaced_turns: int = 0
    timed_out_turns: int = 0
    late_chunks_suppressed: int = 0
    truncated_turns: int = 0
    continuation_offers: int = 0
    cancellation_latency_ms: float | None = None

    # ── session controls ─────────────────────────────────────────────────────
    def set_profile(self, value) -> ResponseProfile:
        """Set the session verbosity profile. An unknown value resets to AUTO."""
        self.profile = parse_response_profile(value)
        return self.profile

    def set_muted(self, value: bool) -> bool:
        self.muted = bool(value)
        return self.muted

    # ── turn lifecycle ───────────────────────────────────────────────────────
    def begin_turn(self, *, contract: str = "", selection_reason: str = "",
                   language: str = "es", token_budget: int = 0,
                   context_budget: int = 0) -> TurnHandle:
        """Open a new turn. A still-ACTIVE previous turn is marked REPLACED so its
        late output can be recognised and dropped rather than interleaved."""
        previous = self.current
        if previous is not None and previous.is_active():
            self.end_turn(TurnState.REPLACED_BY_NEW_TURN, handle=previous)
        self._turn_seq += 1
        self.turns_started += 1
        self.current = TurnHandle(
            turn_id=self._turn_seq, contract=contract,
            selection_reason=selection_reason, language=language,
            token_budget=int(token_budget), context_budget=int(context_budget),
            started_at=self.clock(),
        )
        return self.current

    def end_turn(self, state: TurnState, *, handle: TurnHandle | None = None) -> TurnHandle | None:
        """Close a turn exactly once with a truthful terminal state."""
        h = handle or self.current
        if h is None or not h.is_active():
            return h
        h.state = state if isinstance(state, TurnState) else TurnState.FAILED
        h.ended_at = self.clock()
        if h.state is TurnState.COMPLETED:
            self.turns_completed += 1
        elif h.state is TurnState.INTERRUPTED_BY_OPERATOR:
            self.interrupted_turns += 1
        elif h.state is TurnState.REPLACED_BY_NEW_TURN:
            self.replaced_turns += 1
        elif h.state is TurnState.TIMED_OUT:
            self.timed_out_turns += 1
        if h.truncated_by_cap:
            self.truncated_turns += 1
        if h.continuation_available:
            self.continuation_offers += 1
        self._recent.append(h.snapshot())
        return h

    def accepts(self, turn_id: int | None) -> bool:
        """Whether output stamped with *turn_id* may still reach the operator.

        This is the late-chunk gate: a chunk from a replaced or finished turn is
        refused (and counted), so no old text can appear after an interruption.
        """
        if turn_id is None:
            return True
        cur = self.current
        if cur is None or cur.turn_id != int(turn_id) or not cur.is_active():
            self.late_chunks_suppressed += 1
            return False
        return True

    def note_cancellation_latency(self, ms: float) -> None:
        try:
            self.cancellation_latency_ms = round(float(ms), 1)
        except (TypeError, ValueError):
            pass

    def record_throughput(self, *, tokens_per_second: float | None,
                          first_token_ms: float | None = None) -> None:
        self.throughput.record(tokens_per_second=tokens_per_second,
                               first_token_ms=first_token_ms)

    # ── diagnostics ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "profile": self.profile.value,
            "muted": self.muted,
            "turns_started": self.turns_started,
            "turns_completed": self.turns_completed,
            "interrupted_turns": self.interrupted_turns,
            "replaced_turns": self.replaced_turns,
            "timed_out_turns": self.timed_out_turns,
            "late_chunks_suppressed": self.late_chunks_suppressed,
            "truncated_turns": self.truncated_turns,
            "continuation_offers": self.continuation_offers,
            "cancellation_latency_ms": self.cancellation_latency_ms,
            "throughput": self.throughput.snapshot(),
            "current_turn": self.current.snapshot() if self.current else None,
        }

    def recent(self) -> list[dict]:
        return list(self._recent)


# ── Process-global singleton ─────────────────────────────────────────────────
_runtime: ResponseRuntime | None = None


def get_response_runtime() -> ResponseRuntime:
    """The process response runtime, seeded from operator config on first use."""
    global _runtime
    if _runtime is None:
        profile = ResponseProfile.AUTO
        try:
            from core.config import settings
            profile = parse_response_profile(getattr(settings, "response_profile",
                                                     "AUTO"))
        except Exception:  # noqa: BLE001
            profile = ResponseProfile.AUTO
        _runtime = ResponseRuntime(profile=profile)
    return _runtime


def reset_response_runtime(instance: "ResponseRuntime | None" = None) -> None:
    """Tests / a fresh process."""
    global _runtime
    _runtime = instance
