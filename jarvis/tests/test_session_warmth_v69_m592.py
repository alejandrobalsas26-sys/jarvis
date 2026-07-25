"""V69 M59.2 — session warmth baseline & predictive rewarm. Deterministic, no server.

Proves: a prewarm is never observed reuse; live evidence promotes reuse honestly;
identity changes invalidate; and the predictive rewarm policy is deterministic,
bounded, power-aware, stopping-aware, and cannot loop.
"""
from __future__ import annotations

from core.session_warmth import (
    PredictiveRewarmPolicy,
    RewarmAction,
    RewarmTrigger,
    SessionWarmthBaseline,
    WarmthState,
    session_warmth_health,
)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ── baseline: prewarm is not reuse ────────────────────────────────────────────
def test_prewarm_is_prewarmed_not_reuse():
    b = SessionWarmthBaseline(clock=_Clock())
    state = b.note_prewarm(model="qwen3:8b", transport="native",
                           runner_identity="R1", prefix_identity="P1", family="CONCISE")
    assert state is WarmthState.PREWARMED
    assert b.is_reuse_observed() is False


def test_first_live_observation_updates_state():
    b = SessionWarmthBaseline(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    st = b.observe_live(runner_identity="R1", prefix_identity="P1",
                        cache_state="MODEL_WARM_PREFIX_UNKNOWN",
                        prompt_eval_ms=200.0)
    assert st is WarmthState.MODEL_RESIDENT_PREFIX_UNKNOWN
    assert b.record.observation_count == 1
    assert b.record.first_observation_at is not None


def test_repeated_reuse_evidence_becomes_reuse_observed():
    b = SessionWarmthBaseline(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    # First reuse evidence → LIKELY (a single lucky read is not a durable claim).
    st1 = b.observe_live(runner_identity="R1", prefix_identity="P1",
                         cache_state="PREFIX_REUSE_OBSERVED", prompt_eval_ms=80.0)
    assert st1 is WarmthState.REUSE_LIKELY
    # Second compatible reuse observation → OBSERVED.
    st2 = b.observe_live(runner_identity="R1", prefix_identity="P1",
                         cache_state="PREFIX_REUSE_OBSERVED", prompt_eval_ms=70.0)
    assert st2 is WarmthState.REUSE_OBSERVED
    assert b.is_reuse_observed() is True


def test_cold_model_observation_is_cold():
    b = SessionWarmthBaseline(clock=_Clock())
    st = b.observe_live(runner_identity="R1", prefix_identity="P1",
                        cache_state="COLD_MODEL", load_ms=9000.0)
    assert st is WarmthState.MODEL_COLD


def test_identity_change_marks_stale_and_rebaselines():
    b = SessionWarmthBaseline(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    st = b.observe_live(runner_identity="R2", prefix_identity="P2",
                        cache_state="MODEL_WARM_PREFIX_UNKNOWN", prompt_eval_ms=200.0)
    # A different identity under the warmed one is stale, then re-baselined.
    assert b.invalidation_count == 1
    assert b.record.runner_identity == "R2"
    assert st in (WarmthState.MODEL_RESIDENT_PREFIX_UNKNOWN, WarmthState.STALE)


def test_invalidate_clears_identity():
    b = SessionWarmthBaseline(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    b.invalidate("language_changed")
    assert b.state is WarmthState.INVALIDATED
    assert b.record.runner_identity == ""
    assert b.record.invalidation_reason == "language_changed"


def test_prewarm_never_downgrades_observed_reuse():
    b = SessionWarmthBaseline(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    b.observe_live(runner_identity="R1", prefix_identity="P1",
                   cache_state="PREFIX_REUSE_OBSERVED", prompt_eval_ms=80.0)
    b.observe_live(runner_identity="R1", prefix_identity="P1",
                   cache_state="PREFIX_REUSE_OBSERVED", prompt_eval_ms=70.0)
    assert b.is_reuse_observed()
    # A subsequent prewarm of the same identity must not erase proven reuse.
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    assert b.state is WarmthState.REUSE_OBSERVED


def test_baseline_snapshot_is_content_free():
    b = SessionWarmthBaseline(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    snap = b.snapshot()
    for value in snap.values():
        assert "You are" not in str(value)
    assert snap["state"] == WarmthState.PREWARMED.value


# ── predictive rewarm policy ──────────────────────────────────────────────────
def test_rewarm_scheduled_on_clean_trigger():
    p = PredictiveRewarmPolicy(clock=_Clock())
    dec = p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
    assert dec.action is RewarmAction.SCHEDULE
    assert dec.should_schedule is True


def test_stopping_skips_rewarm():
    p = PredictiveRewarmPolicy(clock=_Clock())
    dec = p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE", is_stopping=True)
    assert dec.action is RewarmAction.SKIP
    assert dec.reason == "stopping"


def test_active_fast_defers_rewarm():
    p = PredictiveRewarmPolicy(clock=_Clock())
    dec = p.evaluate(RewarmTrigger.FAST_STALE_AFTER_DEEP, family="CONCISE",
                     active_fast=True)
    assert dec.action is RewarmAction.DEFER
    assert dec.reason == "active_fast_outranks"


def test_embedding_defers_rewarm():
    p = PredictiveRewarmPolicy(clock=_Clock())
    dec = p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE",
                     embedding_requested=True)
    assert dec.action is RewarmAction.DEFER
    assert dec.reason == "embedding_outranks"


def test_battery_disables_rewarm():
    p = PredictiveRewarmPolicy(clock=_Clock())
    dec = p.evaluate(RewarmTrigger.POWER_RETURNED_TO_AC, family="CONCISE",
                     power_prewarm_allowed=False)
    assert dec.action is RewarmAction.SKIP
    assert dec.reason == "battery_prewarm_disabled"


def test_max_attempts_caps_rewarm():
    p = PredictiveRewarmPolicy(max_attempts_per_family=2, clock=_Clock())
    for _ in range(2):
        assert p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE").should_schedule
        p.note_attempt("CONCISE")
    dec = p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
    assert dec.action is RewarmAction.SKIP
    assert dec.reason == "max_attempts_reached"


def test_failure_sets_bounded_cooldown_and_backs_off():
    clock = _Clock()
    p = PredictiveRewarmPolicy(base_cooldown_s=30.0, clock=clock)
    p.note_attempt("CONCISE")
    p.note_result("CONCISE", success=False)
    dec = p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
    assert dec.action is RewarmAction.DEFER
    assert dec.reason == "cooldown"
    assert 0.0 < dec.cooldown_remaining_s <= 30.0
    # After the cooldown elapses, it can schedule again.
    clock.advance(31.0)
    assert p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE").should_schedule


def test_success_resets_family():
    clock = _Clock()
    p = PredictiveRewarmPolicy(clock=clock)
    p.note_attempt("CONCISE")
    p.note_result("CONCISE", success=False)
    p.note_result("CONCISE", success=True)
    assert p.cooldown_remaining("CONCISE") == 0.0
    assert p.total_successes == 1


def test_invalidation_rearms_family():
    p = PredictiveRewarmPolicy(max_attempts_per_family=1, clock=_Clock())
    p.note_attempt("CONCISE")
    assert p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE").action \
        is RewarmAction.SKIP
    p.note_invalidation("CONCISE")
    assert p.evaluate(RewarmTrigger.LANGUAGE_CHANGED, family="CONCISE").should_schedule


def test_rewarm_cannot_loop_forever():
    # Repeated failing attempts must exhaust the cap, never loop indefinitely.
    clock = _Clock()
    p = PredictiveRewarmPolicy(max_attempts_per_family=3, clock=clock)
    schedules = 0
    for _ in range(50):
        dec = p.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
        if dec.should_schedule:
            schedules += 1
            p.note_attempt("CONCISE")
            p.note_result("CONCISE", success=False)
            clock.advance(10_000.0)  # skip past any cooldown
    assert schedules == 3


# ── health block ──────────────────────────────────────────────────────────────
def test_session_warmth_health_shape():
    b = SessionWarmthBaseline(clock=_Clock())
    p = PredictiveRewarmPolicy(clock=_Clock())
    b.note_prewarm(model="qwen3:8b", transport="native", runner_identity="R1",
                   prefix_identity="P1", family="CONCISE")
    health = session_warmth_health(b, p)
    assert health["session_state"] == WarmthState.PREWARMED.value
    assert health["active_family"] == "CONCISE"
    assert health["observation_count"] == 0
    assert "predictive_rewarm_attempts" in health
    assert "cooldown_remaining" in health
