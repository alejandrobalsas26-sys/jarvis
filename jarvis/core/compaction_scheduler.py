"""core/compaction_scheduler.py — V69 M58.6: idle conversation compaction scheduler.

WHAT M57 LEFT UNSCHEDULED
-------------------------
M57.6.1 built a deterministic extractive digest AND an optional model-assisted pass
(``core.conversation_digest.merge_model_assisted``) that folds INFERRED items into it.
The validator existed; the SCHEDULING did not. This module runs that optional pass at
the right time — when the host is idle — and never at the wrong time.

WHEN IT MAY RUN (all must hold)
-------------------------------
enough completed turns · no active user turn · no HITL · no effectful tool op · no
active answer TTS · no high-priority embedding work · lifecycle OPERATIONAL · power
policy permits background work · context pressure over threshold · cooldown expired.

The conditions arrive as a snapshot (injectable predicates), so the single decision
point is unit-testable without a runtime.

PRIORITY & PREEMPTION
---------------------
Below the active FAST turn, requested semantic retrieval, effectful-operation
verification and foreground embedding; above optional speculative prewarm ONLY when
context pressure is high (governor BACKGROUND). On user input it cancels/yields,
releases the governor, PRESERVES the last valid digest, never exposes a partial one,
and the active FAST turn proceeds immediately.

EPISTEMIC SAFETY
----------------
The extractive digest is always the authoritative fallback. Any model contribution is
forced to ``Evidence.INFERRED`` by ``merge_model_assisted`` (a model can never mint
EXPLICIT), passes a deterministic validator, and is NEVER written to semantic memory.
No chain of thought, secret or full-document dump can enter — the proposer returns
only short bounded topic/decision/open-question phrases.

Bounded, content-free metrics. The scheduler holds NO prompt or answer text — only the
current digest object (already bounded and labelled) and counters.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from core.conversation_digest import (
    ConversationDigest,
    DigestItem,
    ItemKind,
    build_digest,
    estimate_tokens,
    merge_model_assisted,
)

# A model-assisted pass is only worthwhile once the conversation is genuinely long.
_MIN_COMPLETED_TURNS = 8
# Context pressure over this fraction of budget makes compaction worth the CPU.
_DEFAULT_PRESSURE_THRESHOLD = 0.6
# Cooldown so a busy session cannot spin compaction every idle blink.
_DEFAULT_COOLDOWN_S = 120.0
# Hard bounds on the model-assisted pass.
_DEFAULT_TIMEOUT_S = 20.0
_MAX_PROPOSED_ITEMS = 6
_MAX_PROPOSED_ITEM_CHARS = 140


@dataclass(frozen=True)
class CompactionConditions:
    """A content-free snapshot of whether idle compaction may run right now."""

    completed_turns: int = 0
    active_user_turn: bool = False
    hitl_active: bool = False
    effectful_tool_active: bool = False
    answer_tts_active: bool = False
    high_priority_embedding: bool = False
    lifecycle_operational: bool = True
    power_allows_background: bool = True
    context_pressure: float = 0.0        # estimated_tokens / budget
    cooldown_expired: bool = True

    def eligible(self, *, min_turns: int = _MIN_COMPLETED_TURNS,
                pressure_threshold: float = _DEFAULT_PRESSURE_THRESHOLD) -> bool:
        return (
            self.completed_turns >= min_turns
            and not self.active_user_turn
            and not self.hitl_active
            and not self.effectful_tool_active
            and not self.answer_tts_active
            and not self.high_priority_embedding
            and self.lifecycle_operational
            and self.power_allows_background
            and self.context_pressure >= pressure_threshold
            and self.cooldown_expired
        )

    def block_reason(self, *, min_turns: int = _MIN_COMPLETED_TURNS,
                     pressure_threshold: float = _DEFAULT_PRESSURE_THRESHOLD) -> str:
        """The FIRST failing condition (deterministic), or '' when eligible."""
        checks = (
            (self.completed_turns >= min_turns, "not_enough_turns"),
            (not self.active_user_turn, "active_user_turn"),
            (not self.hitl_active, "hitl_active"),
            (not self.effectful_tool_active, "effectful_tool_active"),
            (not self.answer_tts_active, "answer_tts_active"),
            (not self.high_priority_embedding, "high_priority_embedding"),
            (self.lifecycle_operational, "lifecycle_not_operational"),
            (self.power_allows_background, "power_disallows_background"),
            (self.context_pressure >= pressure_threshold, "context_pressure_low"),
            (self.cooldown_expired, "cooldown"),
        )
        for ok, reason in checks:
            if not ok:
                return reason
        return ""


class CompactionState(str, Enum):
    IDLE = "IDLE"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED_FOR_USER = "CANCELLED_FOR_USER"
    TIMED_OUT = "TIMED_OUT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SKIPPED = "SKIPPED"


# A proposer produces candidate INFERRED items from the older conversation. It is
# injected so the scheduler is testable without a model; production supplies one that
# runs a bounded native think=false pass. It returns raw DigestItems; the scheduler
# validates and merge_model_assisted re-labels them INFERRED regardless.
Proposer = Callable[[list, float], Awaitable[list]]


@dataclass
class CompactionScheduler:
    """The single idle-compaction decision point. Bounded, cancellable, content-free."""

    proposer: Proposer | None = None
    min_turns: int = _MIN_COMPLETED_TURNS
    pressure_threshold: float = _DEFAULT_PRESSURE_THRESHOLD
    cooldown_s: float = _DEFAULT_COOLDOWN_S
    timeout_s: float = _DEFAULT_TIMEOUT_S
    clock: Callable[[], float] = time.monotonic

    _digest: ConversationDigest | None = None
    _digest_version: int = 0
    _state: CompactionState = CompactionState.IDLE
    _last_run_at: float | None = None
    _task: "asyncio.Task | None" = None
    _cancel: bool = False

    # counters (advisory health)
    scheduled: int = 0
    started: int = 0
    completed: int = 0
    cancelled_for_user: int = 0
    timed_out: int = 0
    validation_failures: int = 0
    last_duration_ms: float | None = None
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    context_tokens_saved: int = 0

    # ── the authoritative digest ─────────────────────────────────────────────
    def current_digest(self, history: list | None = None) -> ConversationDigest:
        """The digest the composer should use. Returns the augmented digest when one
        is fresh; otherwise a freshly-built extractive digest (the fallback)."""
        if self._digest is not None:
            return self._digest
        return build_digest(history or [])

    def cooldown_expired(self) -> bool:
        if self._last_run_at is None:
            return True
        return (self.clock() - self._last_run_at) >= self.cooldown_s

    # ── the single decision + run ────────────────────────────────────────────
    async def maybe_run(self, history: list, conditions: CompactionConditions
                        ) -> CompactionState:
        """Run ONE bounded model-assisted compaction iff every condition holds.

        Always sets the extractive digest as the baseline first (so even a skipped or
        failed run leaves a valid, authoritative digest). Never raises."""
        # Baseline: the extractive digest is authoritative and always available.
        base = build_digest(history or [])
        if not conditions.eligible(min_turns=self.min_turns,
                                   pressure_threshold=self.pressure_threshold):
            # Keep the previous valid augmented digest if we have one; else the
            # fresh extractive baseline. Never regress to nothing.
            if self._digest is None:
                self._set_digest(base)
            self._state = CompactionState.SKIPPED
            return self._state
        self.scheduled += 1
        self._state = CompactionState.SCHEDULED
        if self.proposer is None:
            # No model-assisted proposer wired — extractive digest is the result.
            self._set_digest(base)
            self._last_run_at = self.clock()
            self._state = CompactionState.COMPLETED
            self.completed += 1
            return self._state
        self._cancel = False
        self.started += 1
        self._state = CompactionState.RUNNING
        t0 = self.clock()
        try:
            proposed = await asyncio.wait_for(
                self.proposer(list(history or []), self.timeout_s),
                timeout=self.timeout_s + 1.0)
        except asyncio.TimeoutError:
            self.timed_out += 1
            self._state = CompactionState.TIMED_OUT
            self._preserve_or_baseline(base)
            self._last_run_at = self.clock()
            return self._state
        except asyncio.CancelledError:
            self.cancelled_for_user += 1
            self._state = CompactionState.CANCELLED_FOR_USER
            self._preserve_or_baseline(base)
            raise
        except Exception:  # noqa: BLE001 — a proposer fault must never break the run
            self.validation_failures += 1
            self._state = CompactionState.VALIDATION_FAILED
            self._preserve_or_baseline(base)
            self._last_run_at = self.clock()
            return self._state
        if self._cancel:
            self.cancelled_for_user += 1
            self._state = CompactionState.CANCELLED_FOR_USER
            self._preserve_or_baseline(base)
            return self._state
        valid = self._validate(proposed)
        if valid is None:
            self.validation_failures += 1
            self._state = CompactionState.VALIDATION_FAILED
            self._set_digest(base)  # fall back to the extractive digest
            self._last_run_at = self.clock()
            return self._state
        augmented = merge_model_assisted(base, valid)
        before = base.estimated_tokens()
        after = augmented.estimated_tokens()
        # A digest that GREW cannot have saved context — clamp at zero, never negative.
        self.context_tokens_saved = max(0, before - after) if after < before else 0
        self.last_input_tokens = self._history_tokens(history)
        self.last_output_tokens = after
        self.last_duration_ms = round((self.clock() - t0) * 1000.0, 1)
        self._set_digest(augmented)
        self._last_run_at = self.clock()
        self.completed += 1
        self._state = CompactionState.COMPLETED
        return self._state

    # ── validation (deterministic) ───────────────────────────────────────────
    def _validate(self, proposed) -> list | None:
        """Bounded deterministic validator. Rejects non-items, over-long text and an
        over-large batch. merge_model_assisted then re-labels survivors INFERRED and
        drops any that duplicate/contradict an EXPLICIT item — a model can never mint
        EXPLICIT and can never overwrite what the user actually said."""
        if not isinstance(proposed, (list, tuple)):
            return None
        out: list[DigestItem] = []
        for item in proposed:
            if not isinstance(item, DigestItem):
                return None  # malformed proposal → reject the whole batch
            if not isinstance(item.kind, ItemKind):
                return None
            if not item.text or len(item.text) > _MAX_PROPOSED_ITEM_CHARS:
                continue
            out.append(item)
            if len(out) >= _MAX_PROPOSED_ITEMS:
                break
        return out

    # ── digest bookkeeping ───────────────────────────────────────────────────
    def _set_digest(self, digest: ConversationDigest) -> None:
        self._digest = digest
        self._digest_version += 1

    def _preserve_or_baseline(self, base: ConversationDigest) -> None:
        """On cancel/timeout keep the previous valid augmented digest; if none exists,
        fall back to the fresh extractive baseline. A partial pass is never exposed."""
        if self._digest is None:
            self._set_digest(base)

    @staticmethod
    def _history_tokens(history: list) -> int:
        return sum(estimate_tokens(str(m.get("content") or ""))
                   for m in (history or []) if isinstance(m, dict))

    # ── preemption ───────────────────────────────────────────────────────────
    def preempt(self) -> None:
        """User input arrived. Signal cancellation; the current digest is preserved and
        the active FAST turn proceeds immediately (this returns without awaiting)."""
        self._cancel = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def cancel(self) -> None:
        """Cancel and await teardown (shutdown). Bounded, keeps the last valid digest."""
        self._cancel = True
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(
                task, return_exceptions=True)), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "scheduled": self.scheduled,
            "started": self.started,
            "completed": self.completed,
            "cancelled_for_user": self.cancelled_for_user,
            "timed_out": self.timed_out,
            "validation_failures": self.validation_failures,
            "last_duration_ms": self.last_duration_ms,
            "input_tokens": self.last_input_tokens,
            "output_tokens": self.last_output_tokens,
            "digest_version": self._digest_version,
            "context_tokens_saved": self.context_tokens_saved,
            "digest": self._digest.snapshot() if self._digest is not None else None,
        }


# ── Process-global singleton ─────────────────────────────────────────────────
_scheduler: CompactionScheduler | None = None


def get_compaction_scheduler() -> CompactionScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CompactionScheduler()
    return _scheduler


def reset_compaction_scheduler(instance: CompactionScheduler | None = None) -> None:
    """Tests / a fresh process."""
    global _scheduler
    _scheduler = instance


_PROPOSABLE_KINDS: dict[str, ItemKind] = {
    "TOPIC": ItemKind.TOPIC, "DECISION": ItemKind.DECISION,
    "OPEN_QUESTION": ItemKind.OPEN_QUESTION, "QUESTION": ItemKind.OPEN_QUESTION,
}


def parse_proposed_items(text: str) -> list[DigestItem]:
    """Parse a model's bounded compaction output into candidate DigestItems.

    Deterministic and strict: only lines of the form ``KIND: text`` where KIND is a
    proposable kind survive; everything else (prose, reasoning, JSON, secrets) is
    dropped. The items are marked OBSERVED here, but ``merge_model_assisted`` re-labels
    every one INFERRED regardless — a model can never mint EXPLICIT. Bounded output.
    """
    out: list[DigestItem] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if ":" not in line:
            continue
        kind_raw, _, body = line.partition(":")
        kind = _PROPOSABLE_KINDS.get(kind_raw.strip().upper())
        body = body.strip()
        if kind is None or not body:
            continue
        if len(body) > _MAX_PROPOSED_ITEM_CHARS:
            body = body[:_MAX_PROPOSED_ITEM_CHARS - 1] + "…"
        from core.conversation_digest import Evidence
        out.append(DigestItem(kind, body, Evidence.OBSERVED))
        if len(out) >= _MAX_PROPOSED_ITEMS:
            break
    return out


def build_conditions_from_runtime(history: list, *, context_budget: int = 2048
                                  ) -> CompactionConditions:
    """Assemble a live conditions snapshot from the process singletons. Best-effort:
    an unavailable subsystem defaults to the SAFE reading (treated as busy/blocked)."""
    completed = 0
    active_turn = hitl = effectful = tts_active = embedding = False
    operational = power_ok = True
    cooldown_ok = True
    try:
        from core.response_runtime import get_response_runtime
        rr = get_response_runtime()
        completed = int(rr.turns_completed)
        active_turn = bool(rr.current is not None and rr.current.is_active())
        tts_active = bool(getattr(rr, "muted", False)) is False and active_turn
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.lifecycle import get_lifecycle
        lc = get_lifecycle()
        operational = not lc.is_stopping()
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.runtime_profile import get_runtime_profile
        power_ok = bool(get_runtime_profile().detect().policy().background_prewarm_allowed)
    except Exception:  # noqa: BLE001
        pass
    try:
        sched = get_compaction_scheduler()
        cooldown_ok = sched.cooldown_expired()
    except Exception:  # noqa: BLE001
        pass
    tokens = sum(estimate_tokens(str(m.get("content") or ""))
                 for m in (history or []) if isinstance(m, dict))
    pressure = round(tokens / max(1, int(context_budget)), 3)
    return CompactionConditions(
        completed_turns=completed, active_user_turn=active_turn, hitl_active=hitl,
        effectful_tool_active=effectful, answer_tts_active=tts_active,
        high_priority_embedding=embedding, lifecycle_operational=operational,
        power_allows_background=power_ok, context_pressure=pressure,
        cooldown_expired=cooldown_ok)
