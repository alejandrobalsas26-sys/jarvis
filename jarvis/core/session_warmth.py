"""core/session_warmth.py — V69 M59.2: session warmth baseline & predictive rewarm.

WHAT M58 COULD NOT ANSWER
-------------------------
M58's family prewarm reported ``successes`` and the prefix-cache observer classified
each turn's prefill, but nothing held a SESSION-level memory of "what did THIS JARVIS
process actually warm, and has any live turn PROVEN it was reused?" A prewarm that
returned a token is not proof of reuse — Ollama may have loaded the weights and then
lost the prefix, or the model may have been evicted before the operator typed. M58
correctly refused to treat residency as reuse, but it kept no baseline to compare a
later turn against.

THE BASELINE
------------
:class:`SessionWarmthBaseline` is that memory: a bounded, content-free record of the
warmed runner + prefix identity and the strongest reuse evidence observed for it. It
distinguishes:

  * PREWARMED            — a prewarm returned a token (NOT reuse; criterion 7)
  * REUSE_LIKELY         — one live turn's prefill dropped (weak evidence)
  * REUSE_OBSERVED       — a live turn PROVED prefill reuse against a cold baseline
  * STALE / INVALIDATED  — the session posture changed under the warmed identity

State is promoted only by LIVE measurable evidence (the prefix-cache classification),
never by a prewarm alone. Its default lifetime is the current process; a small durable
benchmark history lives elsewhere (the qualification artifact, M59.3) and is never
consulted as current-session readiness.

PREDICTIVE REWARM
-----------------
:class:`PredictiveRewarmPolicy` decides — from DETERMINISTIC workload and cache
signals only, never an LLM guess about future topics — whether a bounded rewarm should
run. The operator's live turn always wins: an active FAST turn or a requested embedding
outranks a speculative rewarm, nothing runs after STOPPING, battery disables it, and a
failed family backs off with a bounded exponential cooldown and a hard per-family
attempt cap so a rewarm can never loop.

Everything exposed is content-free: identities are fingerprints, evidence is counts and
milliseconds, states are enums. No prompt, answer, tool argument or key is stored.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable


# ══════════════════════════════════════════════════════════════════════════════
#  Session warmth baseline
# ══════════════════════════════════════════════════════════════════════════════
class WarmthState(str, Enum):
    """The session's warmth for the currently-tracked runner+prefix identity.
    Ordered from least to most evidence of reuse (STALE/INVALIDATED/DEGRADED are
    off-ladder honesty states)."""

    UNINITIALIZED = "UNINITIALIZED"
    MODEL_COLD = "MODEL_COLD"
    MODEL_RESIDENT_PREFIX_UNKNOWN = "MODEL_RESIDENT_PREFIX_UNKNOWN"
    PREWARMED = "PREWARMED"
    REUSE_LIKELY = "REUSE_LIKELY"
    REUSE_OBSERVED = "REUSE_OBSERVED"
    STALE = "STALE"
    INVALIDATED = "INVALIDATED"
    DEGRADED = "DEGRADED"


# Map a prefix-cache CacheState value → the warmth transition it justifies. Kept as
# strings so this module never hard-imports the cache enum (loose coupling).
_CACHE_TO_WARMTH: dict[str, WarmthState] = {
    "COLD_MODEL": WarmthState.MODEL_COLD,
    "MODEL_WARM_PREFIX_UNKNOWN": WarmthState.MODEL_RESIDENT_PREFIX_UNKNOWN,
    "PREFIX_REUSE_LIKELY": WarmthState.REUSE_LIKELY,
    "PREFIX_REUSE_OBSERVED": WarmthState.REUSE_OBSERVED,
    "CONFIG_MISMATCH": WarmthState.STALE,
    "PREFIX_INVALIDATED": WarmthState.INVALIDATED,
}


@dataclass
class WarmthRecord:
    """Content-free snapshot of the session's warmth for one identity."""

    session_id: str = ""
    model: str = ""
    transport: str = ""
    runner_identity: str = ""
    prefix_identity: str = ""
    family: str = ""
    state: WarmthState = WarmthState.UNINITIALIZED
    prewarm_at: float | None = None
    first_observation_at: float | None = None
    prompt_eval_count: int | None = None
    prompt_eval_ms: float | None = None
    load_ms: float | None = None
    first_content_ms: float | None = None
    reuse_state: str = WarmthState.UNINITIALIZED.value
    invalidation_reason: str | None = None
    observation_count: int = 0

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id, "model": self.model,
            "transport": self.transport, "runner_identity": self.runner_identity,
            "prefix_identity": self.prefix_identity, "family": self.family,
            "state": self.state.value, "prewarm_at": self.prewarm_at,
            "first_observation_at": self.first_observation_at,
            "prompt_eval_count": self.prompt_eval_count,
            "prompt_eval_ms": self.prompt_eval_ms, "load_ms": self.load_ms,
            "first_content_ms": self.first_content_ms,
            "reuse_state": self.reuse_state,
            "invalidation_reason": self.invalidation_reason,
            "observation_count": self.observation_count,
        }


class SessionWarmthBaseline:
    """A bounded, session-scoped record of the warmed identity and its reuse evidence.

    Not persisted: its default lifetime is the current JARVIS process. Every promotion
    to a stronger reuse state requires LIVE evidence; a prewarm is only ever PREWARMED.
    """

    def __init__(self, *, session_id: str = "session",
                 clock: Callable[[], float] = time.time) -> None:
        self.session_id = session_id
        self._clock = clock
        self.record = WarmthRecord(session_id=session_id)
        self.invalidation_count = 0
        # A tiny ring of recent states for a truthful, bounded history.
        self._history: "deque[str]" = deque(maxlen=16)

    # ── identity tracking ─────────────────────────────────────────────────────
    def _identity_changed(self, runner_identity: str, prefix_identity: str) -> bool:
        return (bool(self.record.runner_identity) and
                (runner_identity != self.record.runner_identity or
                 prefix_identity != self.record.prefix_identity))

    def _rebaseline(self, *, model, transport, runner_identity, prefix_identity,
                    family) -> None:
        self.record.model = model or self.record.model
        self.record.transport = transport or self.record.transport
        self.record.runner_identity = runner_identity
        self.record.prefix_identity = prefix_identity
        if family:
            self.record.family = family
        self.record.observation_count = 0
        self.record.first_observation_at = None
        self.record.prompt_eval_count = None
        self.record.prompt_eval_ms = None
        self.record.load_ms = None
        self.record.first_content_ms = None

    # ── prewarm (never reuse) ─────────────────────────────────────────────────
    def note_prewarm(self, *, model: str, transport: str, runner_identity: str,
                     prefix_identity: str, family: str = "") -> WarmthState:
        """Record a successful prewarm. This is PREWARMED — NEVER observed reuse."""
        if self._identity_changed(runner_identity, prefix_identity):
            self._rebaseline(model=model, transport=transport,
                             runner_identity=runner_identity,
                             prefix_identity=prefix_identity, family=family)
        else:
            self.record.model = model or self.record.model
            self.record.transport = transport or self.record.transport
            self.record.runner_identity = runner_identity
            self.record.prefix_identity = prefix_identity
            if family:
                self.record.family = family
        # A prewarm never downgrades a stronger, already-proven live reuse state.
        if self.record.state not in (WarmthState.REUSE_LIKELY,
                                     WarmthState.REUSE_OBSERVED):
            self._set_state(WarmthState.PREWARMED)
        self.record.prewarm_at = self._clock()
        return self.record.state

    # ── live observation (the only path to reuse) ─────────────────────────────
    def observe_live(self, *, runner_identity: str, prefix_identity: str,
                     cache_state: str, model: str = "", transport: str = "",
                     family: str = "", prompt_eval_count: int | None = None,
                     prompt_eval_ms: float | None = None, load_ms: float | None = None,
                     first_content_ms: float | None = None) -> WarmthState:
        """Fold one LIVE turn's prefix-cache classification into the baseline.

        ``cache_state`` is a :class:`core.prefix_cache.CacheState` value. Reuse states
        are reached ONLY here, never from a prewarm."""
        if self._identity_changed(runner_identity, prefix_identity):
            # The session posture changed under the warmed identity: mark stale and
            # re-baseline to the new identity before folding the evidence.
            self.invalidation_count += 1
            self.record.invalidation_reason = "identity_changed"
            self._rebaseline(model=model, transport=transport,
                             runner_identity=runner_identity,
                             prefix_identity=prefix_identity, family=family)
            self._set_state(WarmthState.STALE)
        else:
            self.record.runner_identity = runner_identity
            self.record.prefix_identity = prefix_identity
            if model:
                self.record.model = model
            if transport:
                self.record.transport = transport
            if family:
                self.record.family = family
        self.record.observation_count += 1
        if self.record.first_observation_at is None:
            self.record.first_observation_at = self._clock()
        self.record.prompt_eval_count = prompt_eval_count
        self.record.prompt_eval_ms = prompt_eval_ms
        self.record.load_ms = load_ms
        self.record.first_content_ms = first_content_ms
        target = _CACHE_TO_WARMTH.get(str(cache_state))
        if target is None:
            # INSUFFICIENT_EVIDENCE / UNKNOWN: keep the prior state, do not invent one.
            return self.record.state
        if target is WarmthState.REUSE_OBSERVED:
            # REUSE_OBSERVED requires TWO compatible observations of reuse evidence, so
            # a single lucky measurement never becomes a durable session claim.
            if self.record.state in (WarmthState.REUSE_LIKELY,
                                     WarmthState.REUSE_OBSERVED):
                self._set_state(WarmthState.REUSE_OBSERVED)
            else:
                self._set_state(WarmthState.REUSE_LIKELY)
        else:
            self._set_state(target)
        return self.record.state

    def invalidate(self, reason: str) -> None:
        """A deterministic invalidation (language/scope/policy/model/eviction). Clears
        the warmed identity so a stale metric can never be reused as readiness."""
        self.invalidation_count += 1
        self.record.invalidation_reason = str(reason)
        self.record.runner_identity = ""
        self.record.prefix_identity = ""
        self.record.observation_count = 0
        self.record.first_observation_at = None
        self._set_state(WarmthState.INVALIDATED)

    def note_degraded(self, reason: str) -> None:
        self.record.invalidation_reason = str(reason)
        self._set_state(WarmthState.DEGRADED)

    def _set_state(self, state: WarmthState) -> None:
        self.record.state = state
        self.record.reuse_state = state.value
        self._history.append(state.value)

    @property
    def state(self) -> WarmthState:
        return self.record.state

    def is_reuse_observed(self) -> bool:
        return self.record.state is WarmthState.REUSE_OBSERVED

    def snapshot(self) -> dict:
        out = self.record.snapshot()
        out["invalidation_count"] = self.invalidation_count
        out["history"] = list(self._history)
        return out

    def reset(self) -> None:
        self.record = WarmthRecord(session_id=self.session_id)
        self.invalidation_count = 0
        self._history.clear()


# ══════════════════════════════════════════════════════════════════════════════
#  Predictive rewarm policy
# ══════════════════════════════════════════════════════════════════════════════
class RewarmTrigger(str, Enum):
    """A DETERMINISTIC workload/cache signal that may justify a rewarm. Never an LLM
    prediction of future user topics."""

    FAST_STALE_AFTER_DEEP = "FAST_STALE_AFTER_DEEP"
    LANGUAGE_CHANGED = "LANGUAGE_CHANGED"
    AUTHORITY_SCOPE_CHANGED = "AUTHORITY_SCOPE_CHANGED"
    TOOL_SCHEMA_CHANGED = "TOOL_SCHEMA_CHANGED"
    MODEL_EVICTED = "MODEL_EVICTED"
    POWER_RETURNED_TO_AC = "POWER_RETURNED_TO_AC"
    PREVIOUS_PREWARM_CANCELLED = "PREVIOUS_PREWARM_CANCELLED"


class RewarmAction(str, Enum):
    SCHEDULE = "SCHEDULE"    # a bounded rewarm may run now
    DEFER = "DEFER"          # something outranks it; try again later
    SKIP = "SKIP"            # policy forbids it (stopping / battery / capped)


@dataclass(frozen=True)
class RewarmDecision:
    action: RewarmAction
    reason: str
    trigger: str = ""
    family: str = ""
    cooldown_remaining_s: float = 0.0

    @property
    def should_schedule(self) -> bool:
        return self.action is RewarmAction.SCHEDULE

    def snapshot(self) -> dict:
        return {"action": self.action.value, "reason": self.reason,
                "trigger": self.trigger, "family": self.family,
                "cooldown_remaining_s": round(self.cooldown_remaining_s, 1)}


# Bounded backoff: a family that keeps failing to warm backs off geometrically so a
# rewarm can never turn into a hot loop, and stops entirely at the attempt cap.
_BASE_COOLDOWN_S = 30.0
_MAX_COOLDOWN_S = 600.0
_MAX_ATTEMPTS_PER_FAMILY = 3


@dataclass
class _FamilyRewarmState:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    cooldown_until: float = 0.0
    last_trigger: str = ""


class PredictiveRewarmPolicy:
    """Deterministic, bounded, power-aware rewarm arbitration. Pure decision logic —
    it never itself launches a model; a scheduler consults it."""

    def __init__(self, *, max_attempts_per_family: int = _MAX_ATTEMPTS_PER_FAMILY,
                 base_cooldown_s: float = _BASE_COOLDOWN_S,
                 max_cooldown_s: float = _MAX_COOLDOWN_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_attempts = max(1, int(max_attempts_per_family))
        self.base_cooldown_s = float(base_cooldown_s)
        self.max_cooldown_s = float(max_cooldown_s)
        self._clock = clock
        self._families: dict[str, _FamilyRewarmState] = {}
        self.total_attempts = 0
        self.total_successes = 0
        self.total_deferrals = 0
        self.total_skips = 0
        self.last_decision: RewarmDecision | None = None

    def _fam(self, family: str) -> _FamilyRewarmState:
        return self._families.setdefault(family or "default", _FamilyRewarmState())

    def evaluate(
        self,
        trigger: RewarmTrigger,
        *,
        family: str = "default",
        is_stopping: bool = False,
        active_fast: bool = False,
        embedding_requested: bool = False,
        power_prewarm_allowed: bool = True,
    ) -> RewarmDecision:
        """Decide whether a rewarm for ``family`` should run now.

        Priority of refusal, in order:
          1. STOPPING            → never rewarm during shutdown
          2. battery / disabled  → power policy forbids speculative prewarm
          3. active FAST turn    → the operator's live turn always wins
          4. requested embedding → a needed embedding outranks speculation
          5. attempt cap reached → never loop
          6. within cooldown     → back off after a failure
          7. otherwise           → SCHEDULE
        """
        trig = getattr(trigger, "value", str(trigger))
        st = self._fam(family)
        now = self._clock()

        def _decide(action: RewarmAction, reason: str,
                    cd: float = 0.0) -> RewarmDecision:
            dec = RewarmDecision(action=action, reason=reason, trigger=trig,
                                 family=family, cooldown_remaining_s=cd)
            self.last_decision = dec
            if action is RewarmAction.DEFER:
                self.total_deferrals += 1
            elif action is RewarmAction.SKIP:
                self.total_skips += 1
            return dec

        if is_stopping:
            return _decide(RewarmAction.SKIP, "stopping")
        if not power_prewarm_allowed:
            return _decide(RewarmAction.SKIP, "battery_prewarm_disabled")
        if active_fast:
            return _decide(RewarmAction.DEFER, "active_fast_outranks")
        if embedding_requested:
            return _decide(RewarmAction.DEFER, "embedding_outranks")
        if st.attempts >= self.max_attempts:
            return _decide(RewarmAction.SKIP, "max_attempts_reached")
        if now < st.cooldown_until:
            return _decide(RewarmAction.DEFER, "cooldown",
                           cd=st.cooldown_until - now)
        st.last_trigger = trig
        return _decide(RewarmAction.SCHEDULE, "scheduled")

    def note_attempt(self, family: str = "default") -> None:
        """Record that a rewarm attempt is being launched (consumed even if it later
        fails, so a failing family exhausts its cap and cannot loop)."""
        st = self._fam(family)
        st.attempts += 1
        self.total_attempts += 1

    def note_result(self, family: str = "default", *, success: bool) -> None:
        """Fold a MEASURED rewarm outcome. Success resets the family (a fresh, proven
        warm state); failure sets a bounded exponential cooldown."""
        st = self._fam(family)
        if success:
            st.successes += 1
            self.total_successes += 1
            st.failures = 0
            st.cooldown_until = 0.0
        else:
            st.failures += 1
            backoff = min(self.max_cooldown_s,
                          self.base_cooldown_s * (2 ** (st.failures - 1)))
            st.cooldown_until = self._clock() + backoff

    def note_invalidation(self, family: str = "default") -> None:
        """A genuine identity change re-arms a family: the old warm state is gone, so a
        rewarm should be allowed again (attempts reset, cooldown cleared)."""
        st = self._fam(family)
        st.attempts = 0
        st.failures = 0
        st.cooldown_until = 0.0

    def cooldown_remaining(self, family: str = "default") -> float:
        st = self._fam(family)
        return max(0.0, round(st.cooldown_until - self._clock(), 1))

    def snapshot(self) -> dict:
        return {
            "attempts": self.total_attempts,
            "successes": self.total_successes,
            "deferrals": self.total_deferrals,
            "skips": self.total_skips,
            "max_attempts_per_family": self.max_attempts,
            "last_decision": self.last_decision.snapshot() if self.last_decision else None,
            "families": {
                name: {"attempts": s.attempts, "successes": s.successes,
                       "failures": s.failures,
                       "cooldown_remaining_s": max(0.0, round(
                           s.cooldown_until - self._clock(), 1))}
                for name, s in self._families.items()
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Health block (content-free)
# ══════════════════════════════════════════════════════════════════════════════
def session_warmth_health(baseline: SessionWarmthBaseline | None = None,
                          policy: PredictiveRewarmPolicy | None = None) -> dict:
    """The M59.2 SESSION-WARMTH health block. Content-free."""
    b = (baseline or get_session_warmth()).snapshot()
    p = (policy or get_rewarm_policy()).snapshot()
    families = p.get("families", {})
    active_family = b.get("family") or ""
    cooldown = 0.0
    if active_family and active_family in families:
        cooldown = families[active_family].get("cooldown_remaining_s", 0.0)
    return {
        "session_state": b.get("state"),
        "active_family": active_family or None,
        "observation_count": b.get("observation_count"),
        "reuse_state": b.get("reuse_state"),
        "invalidation_count": b.get("invalidation_count"),
        "last_invalidation_reason": b.get("invalidation_reason"),
        "predictive_rewarm_attempts": p.get("attempts"),
        "predictive_rewarm_successes": p.get("successes"),
        "cooldown_remaining": cooldown,
    }


# ── Process-global singletons ─────────────────────────────────────────────────
_baseline: SessionWarmthBaseline | None = None
_policy: PredictiveRewarmPolicy | None = None


def get_session_warmth() -> SessionWarmthBaseline:
    global _baseline
    if _baseline is None:
        _baseline = SessionWarmthBaseline()
    return _baseline


def get_rewarm_policy() -> PredictiveRewarmPolicy:
    global _policy
    if _policy is None:
        _policy = PredictiveRewarmPolicy()
    return _policy


def reset_session_warmth(baseline: SessionWarmthBaseline | None = None,
                         policy: PredictiveRewarmPolicy | None = None) -> None:
    """Tests / a fresh process."""
    global _baseline, _policy
    _baseline = baseline
    _policy = policy
