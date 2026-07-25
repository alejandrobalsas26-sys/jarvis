"""core/warmth_runtime.py — V69 M59.5: live wiring for session warmth & rewarm.

WHAT M59.2 LEFT
---------------
M59.2 built the two pieces — :class:`~core.session_warmth.SessionWarmthBaseline` (a
bounded, content-free record of what THIS process warmed and what live evidence exists
for it) and :class:`~core.session_warmth.PredictiveRewarmPolicy` (a deterministic,
bounded arbiter of whether a speculative rewarm may run). Both were pure decision
logic with no runtime attached: nothing called them from a real prewarm, a real turn,
a real eviction or a real shutdown, so the honest states they can express were never
actually reached in a live session.

This module is that missing bridge, and ONLY that. It owns no model, no prompt and no
thread: it folds already-measured evidence into the baseline, names deterministic
invalidations, and — when and only when the policy says so — launches ONE bounded,
governor-arbitrated rewarm through the EXISTING family prewarm.

THE HONESTY RULES IT ENFORCES
-----------------------------
  * a successful prewarm sets PREWARMED and nothing stronger — residency is not reuse;
  * reuse states are reached ONLY from a live prefix-cache classification;
  * an identity/posture change INVALIDATES before any evidence is folded, so a stale
    measurement can never be read as current readiness;
  * a rewarm is speculative work: the operator's live FAST turn preempts it, a
    requested embedding outranks it, battery forbids it, STOPPING forbids it, and the
    policy's attempt cap plus exponential cooldown make a rewarm LOOP impossible.

WHAT IT REFUSES TO DO
---------------------
No LLM topic prediction — every rewarm trigger is a deterministic workload or cache
signal. No blocking of TEXT_READY: the rewarm always runs as a supervised background
task. No semantic write, no host mutation, no Ollama configuration change. Everything
it exposes is content-free: fingerprints, counts, milliseconds and enum names.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from core.session_warmth import (
    PredictiveRewarmPolicy,
    RewarmAction,
    RewarmDecision,
    RewarmTrigger,
    SessionWarmthBaseline,
    WarmthState,
    get_rewarm_policy,
    get_session_warmth,
)

# The deterministic reasons a warmed identity stops being usable. The first seven
# mirror core.prefix_cache.InvalidationReason (one vocabulary, not two); the last two
# are runtime events that no manifest diff can express.
INVALIDATION_TRIGGERS: tuple[str, ...] = (
    "MODEL_CHANGED", "NUM_CTX_CHANGED", "THINK_CHANGED", "LANGUAGE_CHANGED",
    "AUTHORITY_CHANGED", "SCOPE_CHANGED", "SECURITY_POLICY_CHANGED",
    "TOOL_SCHEMA_CHANGED", "MODEL_EVICTED", "PREWARM_CANCELLED",
)

# Which deterministic invalidation reason justifies which rewarm trigger. A reason with
# no entry here invalidates the baseline but never schedules speculative work — that is
# deliberate for MODEL_CHANGED / NUM_CTX_CHANGED / THINK_CHANGED: those rebuild the
# runner outright, and spending this 15 W CPU on a speculative reload the operator did
# not ask for is exactly the behaviour a bounded policy exists to prevent. The family
# prewarm is still re-armed, so the next legitimate idle window may warm it.
_REASON_TO_TRIGGER: dict[str, RewarmTrigger] = {
    "LANGUAGE_CHANGED": RewarmTrigger.LANGUAGE_CHANGED,
    "AUTHORITY_CHANGED": RewarmTrigger.AUTHORITY_SCOPE_CHANGED,
    "SCOPE_CHANGED": RewarmTrigger.AUTHORITY_SCOPE_CHANGED,
    "TOOL_SCHEMA_CHANGED": RewarmTrigger.TOOL_SCHEMA_CHANGED,
    "MODEL_EVICTED": RewarmTrigger.MODEL_EVICTED,
    "PREWARM_CANCELLED": RewarmTrigger.PREVIOUS_PREWARM_CANCELLED,
}

# A rewarm is bounded twice: by the policy (attempts + cooldown) and by this hard
# wall-clock ceiling, so a wedged transport can never hold a background task open.
_REWARM_TIMEOUT_S = 60.0
# The governor role a speculative rewarm requests. Same role as the live FAST turn —
# the PRIORITY (PREWARM, the lowest) is what makes it yield.
_REWARM_ROLE = "fast"


class WarmthRuntime:
    """The live bridge. Every collaborator is injectable, so the whole state machine
    is deterministic and testable without Ollama, a governor or an event loop."""

    def __init__(
        self,
        *,
        baseline: SessionWarmthBaseline | None = None,
        policy: PredictiveRewarmPolicy | None = None,
        is_stopping: Callable[[], bool] | None = None,
        active_fast: Callable[[], bool] | None = None,
        embedding_requested: Callable[[], bool] | None = None,
        power_prewarm_allowed: Callable[[], bool] | None = None,
        family_prewarm: object | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._baseline = baseline
        self._policy = policy
        self._is_stopping = is_stopping
        self._active_fast = active_fast
        self._embedding_requested = embedding_requested
        self._power_allowed = power_prewarm_allowed
        self._family_prewarm = family_prewarm
        self._clock = clock
        # Bounded, content-free accounting.
        self.invalidations: int = 0
        self.last_invalidation_reason: str | None = None
        self.rewarm_scheduled: int = 0
        self.rewarm_deferred: int = 0
        self.rewarm_skipped: int = 0
        self.rewarm_preemptions: int = 0
        self.prewarm_observations: int = 0
        self.live_observations: int = 0
        self.last_decision: RewarmDecision | None = None
        # A deterministic trigger raised DURING a turn. It is never acted on inline
        # (the operator's turn owns the CPU); the loop consumes it once the turn is
        # over, which is also the only moment a rewarm could legitimately be scheduled.
        self.pending_trigger: RewarmTrigger | None = None
        self.pending_family: str = "default"
        self._task: "asyncio.Task | None" = None
        self._last_manifest = None
        self._last_power: str = "UNKNOWN"

    # ── collaborators (resolved late so import order never matters) ──────────
    @property
    def baseline(self) -> SessionWarmthBaseline:
        return self._baseline if self._baseline is not None else get_session_warmth()

    @property
    def policy(self) -> PredictiveRewarmPolicy:
        return self._policy if self._policy is not None else get_rewarm_policy()

    def _family(self):
        if self._family_prewarm is not None:
            return self._family_prewarm
        try:
            from core.contract_family import get_family_prewarm
            return get_family_prewarm()
        except Exception:  # noqa: BLE001
            return None

    # ── live signals (each guarded; an unreadable signal is the SAFE value) ──
    def is_stopping(self) -> bool:
        if self._is_stopping is not None:
            try:
                return bool(self._is_stopping())
            except Exception:  # noqa: BLE001
                return True
        try:
            from core.lifecycle import get_lifecycle
            return bool(get_lifecycle().is_stopping())
        except Exception:  # noqa: BLE001
            return False

    def active_fast(self) -> bool:
        """Is the operator's live turn running right now? An unreadable answer is
        treated as YES: deferring speculative work is always the safe error."""
        if self._active_fast is not None:
            try:
                return bool(self._active_fast())
            except Exception:  # noqa: BLE001
                return True
        try:
            from core.response_runtime import get_response_runtime
            cur = get_response_runtime().current
            return bool(cur is not None and cur.is_active())
        except Exception:  # noqa: BLE001
            return False

    def embedding_requested(self) -> bool:
        """Is a REQUESTED embedding holding or waiting for the inference slot? A
        needed embedding outranks a speculative rewarm — observed from the governor,
        never assumed."""
        if self._embedding_requested is not None:
            try:
                return bool(self._embedding_requested())
            except Exception:  # noqa: BLE001
                return True
        try:
            from core.residency_governor import SEMANTIC_ROLE, get_governor
            gov = get_governor()
            if str(getattr(gov.metrics, "active_role", "") or "") == SEMANTIC_ROLE:
                return True
            for waiter in tuple(getattr(gov, "_waiters", ()) or ()):
                req = getattr(waiter, "request", None)
                if req is not None and str(getattr(req, "role", "")) == SEMANTIC_ROLE:
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def power_prewarm_allowed(self) -> bool:
        if self._power_allowed is not None:
            try:
                return bool(self._power_allowed())
            except Exception:  # noqa: BLE001
                return False
        try:
            from core.runtime_profile import get_runtime_profile
            return bool(get_runtime_profile().detect().policy()
                        .background_prewarm_allowed)
        except Exception:  # noqa: BLE001
            # Power state unreadable → do NOT speculatively spend the CPU.
            return False

    def power_profile(self) -> str:
        try:
            from core.runtime_profile import get_runtime_profile
            return str(get_runtime_profile().detect().effective.value)
        except Exception:  # noqa: BLE001
            return "UNKNOWN"

    # ══════════════════════════════════════════════════════════════════════════
    #  1. PREWARM  →  PREWARMED, never observed reuse
    # ══════════════════════════════════════════════════════════════════════════
    def note_prewarm(self, *, model: str, transport: str = "native",
                     runner_identity: str, prefix_identity: str,
                     family: str = "") -> WarmthState:
        """Fold ONE successful prewarm. Criterion 13: this can only ever reach
        PREWARMED — a returned token proves the runner answered, not that a later turn
        will reuse the prefix."""
        self.prewarm_observations += 1
        return self.baseline.note_prewarm(
            model=model, transport=transport, runner_identity=runner_identity,
            prefix_identity=prefix_identity, family=family)

    def note_family_record(self, record) -> WarmthState | None:
        """Fold a :class:`core.contract_family.FamilyRecord`. A failed/cancelled
        attempt is NOT a prewarm: it records the cancellation trigger instead."""
        if record is None:
            return None
        state = str(getattr(getattr(record, "state", ""), "value", "") or "")
        if not bool(getattr(record, "success", False)):
            if state == "CANCELLED":
                self.note_prewarm_cancelled(str(getattr(record, "family", "") or ""))
            return None
        runner = (str(getattr(record, "prewarm_runner_identity", "") or "")
                  or str(getattr(record, "live_runner_identity", "") or ""))
        prefix = str(getattr(record, "compatibility_identity", "") or "")
        if not prefix:
            return None
        # Deliberately does NOT touch the rewarm policy: a boot prewarm is not a
        # policy ATTEMPT, and folding it as one would let a successful boot warm
        # silently reset a family that the policy had correctly backed off.
        return self.note_prewarm(
            model=str(getattr(record, "model", "") or ""), transport="native",
            runner_identity=runner, prefix_identity=prefix,
            family=str(getattr(record, "family", "") or ""))

    # ══════════════════════════════════════════════════════════════════════════
    #  2. LIVE EVIDENCE  →  the only path to a reuse state
    # ══════════════════════════════════════════════════════════════════════════
    def observe_turn(self, *, manifest, cache_state: str, runner_identity: str = "",
                     model: str = "", transport: str = "native", family: str = "",
                     prompt_eval_count: int | None = None,
                     prompt_eval_ms: float | None = None,
                     load_ms: float | None = None,
                     first_content_ms: float | None = None,
                     power_profile: str | None = None) -> WarmthState:
        """Fold ONE live turn's measured prefill evidence into the session baseline.

        The posture diff runs FIRST: when this turn's manifest is incompatible with the
        previous one, the deterministic reason is named and the baseline is invalidated
        before any measurement is folded — so an incompatible metric can never promote
        warmth. Only then is the prefix-cache classification applied, which is the
        single path to REUSE_LIKELY / REUSE_OBSERVED (criterion 14).
        """
        self.live_observations += 1
        power = str(power_profile or self.power_profile())
        reason = self._diff_posture(manifest, power)
        if reason:
            self.invalidate(reason)
        self._last_manifest, self._last_power = manifest, power
        prefix_identity = ""
        try:
            prefix_identity = str(manifest.compatibility_identity())
        except Exception:  # noqa: BLE001
            prefix_identity = ""
        return self.baseline.observe_live(
            runner_identity=str(runner_identity or ""),
            prefix_identity=prefix_identity, cache_state=str(cache_state),
            model=str(model or getattr(manifest, "model", "") or ""),
            transport=str(transport or ""), family=str(family or ""),
            prompt_eval_count=prompt_eval_count, prompt_eval_ms=prompt_eval_ms,
            load_ms=load_ms, first_content_ms=first_content_ms)

    def _diff_posture(self, manifest, power: str) -> str | None:
        """The deterministic invalidation reason between the last manifest and this
        one, or None. Delegates to the EXISTING M58 field precedence — one vocabulary
        for 'why is the warmed prefix no longer usable', not a second one."""
        if self._last_manifest is None or manifest is None:
            return None
        try:
            from core.prefix_cache import diff_invalidation
            reason = diff_invalidation(self._last_manifest, manifest,
                                       old_power=self._last_power, new_power=power)
        except Exception:  # noqa: BLE001
            return None
        return getattr(reason, "value", None) if reason is not None else None

    # ══════════════════════════════════════════════════════════════════════════
    #  3. INVALIDATION
    # ══════════════════════════════════════════════════════════════════════════
    def invalidate(self, reason: str, *, family: str = "default") -> RewarmTrigger | None:
        """Invalidate the warmed identity everywhere it is remembered, and return the
        rewarm trigger this reason justifies (or None when it justifies none).

        The prefix observer's baselines, the family prewarm's once-per-identity guard
        and the session baseline are cleared together: leaving any one of them holding
        the old identity is exactly how a stale metric becomes a false readiness claim.
        """
        why = str(reason or "MANUAL_INVALIDATION")
        self.invalidations += 1
        self.last_invalidation_reason = why
        try:
            self.baseline.invalidate(why)
        except Exception:  # noqa: BLE001
            pass
        try:
            from core.prefix_cache import get_prefix_cache_observer
            get_prefix_cache_observer().note_invalidation(why)
        except Exception:  # noqa: BLE001
            pass
        fam = self._family()
        if fam is not None:
            try:
                fam.note_invalidation(why)
            except Exception:  # noqa: BLE001
                pass
        try:
            # A genuine identity change re-arms the family: the old warm state is gone,
            # so a bounded rewarm may be allowed again.
            self.policy.note_invalidation(family)
        except Exception:  # noqa: BLE001
            pass
        trigger = _REASON_TO_TRIGGER.get(why)
        if trigger is not None:
            self.pending_trigger, self.pending_family = trigger, (family or "default")
        return trigger

    def note_eviction(self) -> RewarmTrigger | None:
        """An OBSERVED model eviction (never an assumed one)."""
        return self.invalidate("MODEL_EVICTED")

    def note_prewarm_cancelled(self, family: str = "default") -> RewarmTrigger:
        """A prewarm the operator preempted. This is NOT an invalidation of measured
        warmth — nothing became untrue — it only records that the intended warm never
        happened, so a later idle moment may legitimately retry it."""
        self.last_invalidation_reason = "PREWARM_CANCELLED"
        try:
            self.policy.note_invalidation(family or "default")
        except Exception:  # noqa: BLE001
            pass
        return RewarmTrigger.PREVIOUS_PREWARM_CANCELLED

    # ══════════════════════════════════════════════════════════════════════════
    #  4. PREDICTIVE REWARM — bounded, governed, always preemptible
    # ══════════════════════════════════════════════════════════════════════════
    def evaluate(self, trigger: RewarmTrigger, *, family: str = "default"
                 ) -> RewarmDecision:
        """Ask the policy, with the LIVE signals read at this instant."""
        decision = self.policy.evaluate(
            trigger, family=family,
            is_stopping=self.is_stopping(),
            active_fast=self.active_fast(),
            embedding_requested=self.embedding_requested(),
            power_prewarm_allowed=self.power_prewarm_allowed(),
        )
        self.last_decision = decision
        if decision.action is RewarmAction.DEFER:
            self.rewarm_deferred += 1
        elif decision.action is RewarmAction.SKIP:
            self.rewarm_skipped += 1
        return decision

    def schedule_rewarm(self, trigger: RewarmTrigger, *, family: str = "default"
                        ) -> "asyncio.Task | None":
        """Evaluate and, when allowed, launch ONE supervised background rewarm.

        Returns the task, or None when the policy refused or a rewarm is already in
        flight. Never awaited by the caller — a rewarm must never delay TEXT_READY or
        an operator turn.
        """
        if self._task is not None and not self._task.done():
            return None
        decision = self.evaluate(trigger, family=family)
        if not decision.should_schedule:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None       # no loop (tests / headless): nothing to schedule onto
        self.rewarm_scheduled += 1
        self._task = loop.create_task(self._supervised_rewarm(family=family),
                                      name="predictive-rewarm")
        return self._task

    async def _supervised_rewarm(self, *, family: str = "default") -> bool:
        """Run ONE bounded rewarm. Never raises into the loop; always records a
        MEASURED result so a failing family backs off and eventually stops."""
        try:
            return await asyncio.wait_for(self.run_rewarm(family=family),
                                          timeout=_REWARM_TIMEOUT_S)
        except asyncio.CancelledError:
            # Preempted by the operator: not a failure, and not a success either.
            raise
        except Exception:  # noqa: BLE001
            self.policy.note_result(family, success=False)
            return False

    async def run_rewarm(self, *, family: str = "default") -> bool:
        """Perform the rewarm through the EXISTING family prewarm (which already owns
        the governor slot at PREWARM priority, the once-per-identity guard and the
        cache-safe prompt). Returns whether it measurably succeeded."""
        self.policy.note_attempt(family)
        if self.is_stopping():
            self.policy.note_result(family, success=False)
            return False
        fam = self._family()
        if fam is None:
            self.policy.note_result(family, success=False)
            return False
        try:
            from core.contract_family import ContractFamily
            target = ContractFamily(str(family).upper())
        except Exception:  # noqa: BLE001
            from core.contract_family import ContractFamily
            target = ContractFamily.CONCISE
        record = await fam.warm_family(target, force=True,
                                       power_profile=self.power_profile())
        success = bool(getattr(record, "success", False))
        # The baseline is folded by the family prewarm's own publish hook, so the
        # result is recorded exactly once regardless of which path warmed it.
        self.policy.note_result(family, success=success)
        return success

    def consume_pending(self) -> "asyncio.Task | None":
        """Act on a trigger raised during a turn — AFTER the turn, never during it.

        The pending trigger is consumed whether or not the policy allows a rewarm, so
        one invalidation can raise at most one scheduling decision and a repeated
        refusal can never accumulate into a backlog of speculative work.
        """
        trigger, family = self.pending_trigger, self.pending_family
        self.pending_trigger, self.pending_family = None, "default"
        if trigger is None:
            return None
        return self.schedule_rewarm(trigger, family=family)

    def preempt(self) -> bool:
        """The operator's live work always wins: cancel an in-flight speculative
        rewarm immediately. Returns whether anything was cancelled. Non-blocking —
        teardown is awaited only by :meth:`cancel`."""
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        self.rewarm_preemptions += 1
        return True

    async def cancel(self) -> None:
        """Cancel and AWAIT teardown of the rewarm task (shutdown path). Bounded, so
        no rewarm task can survive shutdown."""
        task = self._task
        if task is None or task.done():
            self._task = None
            return
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self._task = None

    def has_pending_rewarm(self) -> bool:
        t = self._task
        return bool(t is not None and not t.done())

    # ══════════════════════════════════════════════════════════════════════════
    #  5. Health (content-free)
    # ══════════════════════════════════════════════════════════════════════════
    def snapshot(self) -> dict:
        """Bounded diagnostics. Fingerprints, counts, enum names — never a prompt, an
        answer, a tool argument or a secret."""
        base = self.baseline.snapshot()
        pol = self.policy.snapshot()
        fam = str(base.get("family") or "") or "default"
        families = pol.get("families", {}) or {}
        cooldown = (families.get(fam, {}) or {}).get("cooldown_remaining_s", 0.0)
        return {
            "session_state": base.get("state"),
            "active_family": base.get("family") or None,
            "observation_count": base.get("observation_count"),
            "reuse_state": base.get("reuse_state"),
            "invalidation_count": base.get("invalidation_count"),
            "last_invalidation_reason": (base.get("invalidation_reason")
                                         or self.last_invalidation_reason),
            "predictive_rewarm_attempts": pol.get("attempts"),
            "predictive_rewarm_successes": pol.get("successes"),
            "cooldown_remaining": cooldown,
            # wiring-level accounting (still content-free)
            "prewarm_observations": self.prewarm_observations,
            "live_observations": self.live_observations,
            "rewarm_scheduled": self.rewarm_scheduled,
            "rewarm_deferred": self.rewarm_deferred,
            "rewarm_skipped": self.rewarm_skipped,
            "rewarm_preemptions": self.rewarm_preemptions,
            "rewarm_pending": self.has_pending_rewarm(),
            "last_decision": pol.get("last_decision"),
        }


# ── Process-global singleton ─────────────────────────────────────────────────
_runtime: WarmthRuntime | None = None


def get_warmth_runtime() -> WarmthRuntime:
    global _runtime
    if _runtime is None:
        _runtime = WarmthRuntime()
    return _runtime


def reset_warmth_runtime(instance: WarmthRuntime | None = None) -> None:
    """Tests / a fresh process."""
    global _runtime
    _runtime = instance


def warmth_runtime_health() -> dict:
    """The live SESSION-WARMTH health block (guarded; never raises)."""
    try:
        return get_warmth_runtime().snapshot()
    except Exception:  # noqa: BLE001
        return {}
