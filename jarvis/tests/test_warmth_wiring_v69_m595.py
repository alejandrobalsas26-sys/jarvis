"""V69 M59.5 — live wiring of session warmth & predictive rewarm. No server.

M59.2 proved the baseline and the policy in isolation. This proves the BRIDGE: that a
real prewarm reaches PREWARMED and nothing stronger, that only live measured evidence
promotes warmth, that every deterministic posture change invalidates before evidence is
folded, and that a predictive rewarm is bounded, governed, power-aware, preemptible by
the operator, impossible after STOPPING and impossible to loop.
"""
from __future__ import annotations

import asyncio

import pytest

from core.session_warmth import (
    PredictiveRewarmPolicy,
    RewarmAction,
    RewarmTrigger,
    SessionWarmthBaseline,
    WarmthState,
)
from core.warmth_runtime import (
    INVALIDATION_TRIGGERS,
    WarmthRuntime,
    get_warmth_runtime,
    reset_warmth_runtime,
    warmth_runtime_health,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_warmth_runtime(None)
    yield
    reset_warmth_runtime(None)


def _wr(**kw):
    """A runtime whose live signals are all injected: nothing reads the real host."""
    defaults = dict(
        baseline=SessionWarmthBaseline(session_id="test"),
        policy=PredictiveRewarmPolicy(),
        is_stopping=lambda: False,
        active_fast=lambda: False,
        embedding_requested=lambda: False,
        power_prewarm_allowed=lambda: True,
        family_prewarm=None,
    )
    defaults.update(kw)
    return WarmthRuntime(**defaults)


class FakeManifest:
    """The comparable fields core.prefix_cache.diff_invalidation reads."""

    def __init__(self, **kw):
        self.model = kw.get("model", "qwen3:8b")
        self.transport = kw.get("transport", "native")
        self.num_ctx = kw.get("num_ctx", 2048)
        self.think = kw.get("think", False)
        self.core_fingerprint = kw.get("core_fingerprint", "core0")
        self.language = kw.get("language", "es")
        self.authority_mode = kw.get("authority_mode", "STANDARD")
        self.scope_fingerprint = kw.get("scope_fingerprint", "scope0")
        self.security_policy_version = kw.get("security_policy_version", "v1")
        self.personality_fingerprint = kw.get("personality_fingerprint", "p0")
        self.tool_schema_fingerprint = kw.get("tool_schema_fingerprint", "tools0")
        self.contract_schema_version = kw.get("contract_schema_version", "c1")
        self._identity = kw.get("identity", "id-a")

    def compatibility_identity(self):
        return self._identity


class FakeRecord:
    """A core.contract_family.FamilyRecord shape."""

    class _State:
        def __init__(self, v):
            self.value = v

    def __init__(self, *, success=True, state="READY", family="CONCISE",
                 identity="id-a", runner="runner-a", model="qwen3:8b"):
        self.success = success
        self.state = self._State(state)
        self.family = family
        self.compatibility_identity = identity
        self.prewarm_runner_identity = runner
        self.live_runner_identity = runner
        self.model = model


class FakeFamilyPrewarm:
    def __init__(self, *, success=True, delay=0.0):
        self.calls: list = []
        self.invalidations: list = []
        self._success = success
        self._delay = delay

    async def warm_family(self, family, *, force=False, power_profile="UNKNOWN",
                          cancellation=None):
        self.calls.append((getattr(family, "value", str(family)), force))
        if self._delay:
            await asyncio.sleep(self._delay)
        return FakeRecord(success=self._success,
                          state="READY" if self._success else "FAILED")

    def note_invalidation(self, reason):
        self.invalidations.append(reason)


# ══════════════════════════════════════════════════════════════════════════════
#  1. Prewarm alone is never observed reuse (criterion 13)
# ══════════════════════════════════════════════════════════════════════════════
def test_successful_prewarm_reaches_prewarmed_and_nothing_stronger():
    wr = _wr()
    state = wr.note_prewarm(model="qwen3:8b", runner_identity="r1",
                            prefix_identity="p1", family="CONCISE")
    assert state is WarmthState.PREWARMED
    assert wr.baseline.is_reuse_observed() is False
    assert wr.snapshot()["reuse_state"] == "PREWARMED"


def test_family_record_folds_exactly_once_and_only_when_successful():
    wr = _wr()
    assert wr.note_family_record(FakeRecord(success=False, state="FAILED")) is None
    assert wr.prewarm_observations == 0
    assert wr.note_family_record(None) is None
    st = wr.note_family_record(FakeRecord())
    assert st is WarmthState.PREWARMED
    assert wr.prewarm_observations == 1
    assert wr.snapshot()["active_family"] == "CONCISE"


def test_a_boot_prewarm_is_not_a_rewarm_policy_attempt():
    """Folding a successful boot prewarm must NOT reset a family the policy has
    correctly backed off — a prewarm is not an attempt the policy authorised."""
    wr = _wr()
    wr.policy.note_attempt("CONCISE")
    wr.policy.note_result("CONCISE", success=False)
    before = wr.policy.snapshot()
    wr.note_family_record(FakeRecord())
    after = wr.policy.snapshot()
    assert after["attempts"] == before["attempts"]
    assert after["successes"] == before["successes"]


def test_a_cancelled_prewarm_records_the_retry_trigger_not_a_warm():
    wr = _wr()
    assert wr.note_family_record(
        FakeRecord(success=False, state="CANCELLED")) is None
    assert wr.last_invalidation_reason == "PREWARM_CANCELLED"
    assert wr.baseline.state is WarmthState.UNINITIALIZED   # nothing was claimed


# ══════════════════════════════════════════════════════════════════════════════
#  2. Live evidence is the only path to reuse (criterion 14)
# ══════════════════════════════════════════════════════════════════════════════
def test_live_evidence_promotes_warmth_honestly_over_two_observations():
    wr = _wr()
    m = FakeManifest()
    wr.note_prewarm(model="qwen3:8b", runner_identity="r1", prefix_identity="id-a",
                    family="CONCISE")
    # one measured reuse is only LIKELY
    st = wr.observe_turn(manifest=m, cache_state="PREFIX_REUSE_OBSERVED",
                         runner_identity="r1", power_profile="AC")
    assert st is WarmthState.REUSE_LIKELY
    # a second compatible measurement earns the durable claim
    st = wr.observe_turn(manifest=m, cache_state="PREFIX_REUSE_OBSERVED",
                         runner_identity="r1", power_profile="AC")
    assert st is WarmthState.REUSE_OBSERVED
    assert wr.live_observations == 2
    assert wr.snapshot()["observation_count"] == 2


def test_cold_and_unknown_evidence_never_invents_a_reuse_state():
    wr = _wr()
    m = FakeManifest()
    assert wr.observe_turn(manifest=m, cache_state="COLD_MODEL",
                           runner_identity="r1", power_profile="AC"
                           ) is WarmthState.MODEL_COLD
    assert wr.observe_turn(manifest=m, cache_state="INSUFFICIENT_EVIDENCE",
                           runner_identity="r1", power_profile="AC"
                           ) is WarmthState.MODEL_COLD   # unchanged, not invented


def test_measurements_are_recorded_content_free():
    wr = _wr()
    wr.observe_turn(manifest=FakeManifest(), cache_state="PREFIX_REUSE_LIKELY",
                    runner_identity="r1", prompt_eval_count=120,
                    prompt_eval_ms=980.0, load_ms=12.0, first_content_ms=1500.0,
                    power_profile="AC")
    snap = wr.baseline.snapshot()
    assert snap["prompt_eval_count"] == 120 and snap["prompt_eval_ms"] == 980.0
    blob = repr(wr.snapshot())
    for leak in ("prompt", "answer", "content", "message"):
        assert f'"{leak}"' not in blob


# ══════════════════════════════════════════════════════════════════════════════
#  3. Deterministic invalidation triggers
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field,value,expected", [
    ("model", "llama3:8b", "MODEL_CHANGED"),
    ("num_ctx", 4096, "NUM_CTX_CHANGED"),
    ("language", "en", "LANGUAGE_CHANGED"),
    ("authority_mode", "ELEVATED", "AUTHORITY_CHANGED"),
    ("scope_fingerprint", "scope-b", "SCOPE_CHANGED"),
    ("security_policy_version", "v2", "SECURITY_POLICY_CHANGED"),
    ("tool_schema_fingerprint", "tools-b", "TOOL_SCHEMA_CHANGED"),
])
def test_every_required_posture_change_invalidates(field, value, expected):
    fam = FakeFamilyPrewarm()
    wr = _wr(family_prewarm=fam)
    first = FakeManifest()
    wr.observe_turn(manifest=first, cache_state="PREFIX_REUSE_LIKELY",
                    runner_identity="r1", power_profile="AC")
    changed = FakeManifest(**{field: value, "identity": "id-b"})
    wr.observe_turn(manifest=changed, cache_state="MODEL_WARM_PREFIX_UNKNOWN",
                    runner_identity="r2", power_profile="AC")
    assert wr.last_invalidation_reason == expected
    assert wr.invalidations == 1
    # the family prewarm's once-per-identity guard is cleared too, or a stale warm
    # would be reported as ready for the NEW identity
    assert fam.invalidations == [expected]


def test_an_observed_eviction_invalidates_and_justifies_a_rewarm():
    wr = _wr()
    wr.note_prewarm(model="m", runner_identity="r1", prefix_identity="p1")
    trigger = wr.note_eviction()
    assert trigger is RewarmTrigger.MODEL_EVICTED
    assert wr.baseline.state is WarmthState.INVALIDATED
    assert wr.pending_trigger is RewarmTrigger.MODEL_EVICTED


def test_the_documented_trigger_vocabulary_is_complete():
    for name in ("MODEL_CHANGED", "NUM_CTX_CHANGED", "LANGUAGE_CHANGED",
                 "AUTHORITY_CHANGED", "SCOPE_CHANGED", "SECURITY_POLICY_CHANGED",
                 "TOOL_SCHEMA_CHANGED", "MODEL_EVICTED", "PREWARM_CANCELLED"):
        assert name in INVALIDATION_TRIGGERS


def test_a_compatible_turn_never_invalidates():
    wr = _wr()
    m = FakeManifest()
    wr.observe_turn(manifest=m, cache_state="PREFIX_REUSE_LIKELY",
                    runner_identity="r1", power_profile="AC")
    wr.observe_turn(manifest=FakeManifest(), cache_state="PREFIX_REUSE_OBSERVED",
                    runner_identity="r1", power_profile="AC")
    assert wr.invalidations == 0


def test_a_power_profile_change_invalidates_the_warm_plan():
    wr = _wr()
    wr.observe_turn(manifest=FakeManifest(), cache_state="PREFIX_REUSE_LIKELY",
                    runner_identity="r1", power_profile="AC")
    wr.observe_turn(manifest=FakeManifest(), cache_state="PREFIX_REUSE_LIKELY",
                    runner_identity="r1", power_profile="BATTERY")
    assert wr.last_invalidation_reason == "POWER_PROFILE_CHANGED"


# ══════════════════════════════════════════════════════════════════════════════
#  4. Predictive rewarm — deterministic, bounded, preemptible
# ══════════════════════════════════════════════════════════════════════════════
def test_rewarm_is_deferred_by_an_active_user_turn():
    wr = _wr(active_fast=lambda: True)
    d = wr.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
    assert d.action is RewarmAction.DEFER and d.reason == "active_fast_outranks"
    assert wr.rewarm_deferred == 1


def test_a_requested_embedding_outranks_a_speculative_rewarm():
    wr = _wr(embedding_requested=lambda: True)
    d = wr.evaluate(RewarmTrigger.MODEL_EVICTED)
    assert d.action is RewarmAction.DEFER and d.reason == "embedding_outranks"


def test_no_rewarm_after_stopping():
    wr = _wr(is_stopping=lambda: True)
    d = wr.evaluate(RewarmTrigger.MODEL_EVICTED)
    assert d.action is RewarmAction.SKIP and d.reason == "stopping"
    assert wr.schedule_rewarm(RewarmTrigger.MODEL_EVICTED) is None


def test_battery_disables_speculative_rewarm():
    wr = _wr(power_prewarm_allowed=lambda: False)
    d = wr.evaluate(RewarmTrigger.MODEL_EVICTED)
    assert d.action is RewarmAction.SKIP and d.reason == "battery_prewarm_disabled"


def test_unreadable_signals_default_to_the_safe_refusal():
    """A signal that cannot be read must never be optimistically assumed favourable."""
    def boom():
        raise RuntimeError("unreadable")
    wr = _wr(is_stopping=boom)
    assert wr.is_stopping() is True
    wr2 = _wr(active_fast=boom, power_prewarm_allowed=boom,
              embedding_requested=boom)
    assert wr2.active_fast() is True
    assert wr2.embedding_requested() is True
    assert wr2.power_prewarm_allowed() is False


def test_rewarm_runs_through_the_family_prewarm_and_records_a_measured_result():
    fam = FakeFamilyPrewarm(success=True)
    wr = _wr(family_prewarm=fam)
    assert _run(wr.run_rewarm(family="CONCISE")) is True
    assert fam.calls == [("CONCISE", True)]
    snap = wr.policy.snapshot()
    assert snap["attempts"] == 1 and snap["successes"] == 1


def test_a_failing_family_backs_off_and_can_never_loop():
    fam = FakeFamilyPrewarm(success=False)
    wr = _wr(family_prewarm=fam)
    for _ in range(6):                    # ask far more times than the cap allows
        if wr.evaluate(RewarmTrigger.MODEL_EVICTED,
                       family="CONCISE").action is RewarmAction.SCHEDULE:
            _run(wr.run_rewarm(family="CONCISE"))
        # clear the cooldown so ONLY the attempt cap can stop it
        wr.policy._fam("CONCISE").cooldown_until = 0.0
    d = wr.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
    assert d.action is RewarmAction.SKIP and d.reason == "max_attempts_reached"
    assert len(fam.calls) == 3            # bounded: never a hot loop


def test_a_failure_sets_a_bounded_cooldown():
    fam = FakeFamilyPrewarm(success=False)
    wr = _wr(family_prewarm=fam)
    _run(wr.run_rewarm(family="CONCISE"))
    d = wr.evaluate(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
    assert d.action is RewarmAction.DEFER and d.reason == "cooldown"
    assert d.cooldown_remaining_s > 0


def test_only_one_rewarm_is_ever_in_flight():
    fam = FakeFamilyPrewarm(success=True, delay=0.2)
    wr = _wr(family_prewarm=fam)

    async def scenario():
        first = wr.schedule_rewarm(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
        second = wr.schedule_rewarm(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
        assert first is not None and second is None
        await wr.cancel()

    _run(scenario())


def test_user_input_preempts_an_in_flight_rewarm():
    fam = FakeFamilyPrewarm(success=True, delay=5.0)
    wr = _wr(family_prewarm=fam)

    async def scenario():
        task = wr.schedule_rewarm(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
        assert task is not None
        await asyncio.sleep(0)
        assert wr.preempt() is True
        assert wr.rewarm_preemptions == 1
        await wr.cancel()
        assert wr.has_pending_rewarm() is False
        assert task.cancelled() or task.done()

    _run(scenario())


def test_cancel_leaves_no_orphan_rewarm_task():
    fam = FakeFamilyPrewarm(success=True, delay=5.0)
    wr = _wr(family_prewarm=fam)

    async def scenario():
        wr.schedule_rewarm(RewarmTrigger.MODEL_EVICTED, family="CONCISE")
        assert wr.has_pending_rewarm() is True
        await wr.cancel()
        assert wr.has_pending_rewarm() is False
        await wr.cancel()          # idempotent

    _run(scenario())


def test_a_rewarm_that_raises_is_recorded_as_a_measured_failure():
    class Exploding(FakeFamilyPrewarm):
        async def warm_family(self, family, **kw):
            raise RuntimeError("transport gone")

    wr = _wr(family_prewarm=Exploding())
    assert _run(wr._supervised_rewarm(family="CONCISE")) is False
    assert wr.policy.snapshot()["successes"] == 0
    assert wr.policy.snapshot()["attempts"] == 1


def test_pending_trigger_is_consumed_exactly_once():
    wr = _wr()
    wr.note_eviction()
    assert wr.pending_trigger is RewarmTrigger.MODEL_EVICTED
    wr.consume_pending()                 # no running loop -> nothing scheduled
    assert wr.pending_trigger is None
    assert wr.consume_pending() is None   # a refusal cannot accumulate a backlog


def test_scheduling_without_an_event_loop_is_a_safe_no_op():
    wr = _wr()
    assert wr.schedule_rewarm(RewarmTrigger.MODEL_EVICTED) is None


def test_rewarm_never_uses_an_llm_topic_prediction():
    """Every trigger is a deterministic workload/cache signal, by construction."""
    import ast
    import inspect

    import core.warmth_runtime as mod
    src = inspect.getsource(mod)
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # No chat/completion/embedding call surface is reachable from this module.
    assert not any(m in imported for m in ("openai", "anthropic"))
    assert "core.llm" not in imported
    assert all(t.name for t in RewarmTrigger)


# ══════════════════════════════════════════════════════════════════════════════
#  5. Health block
# ══════════════════════════════════════════════════════════════════════════════
def test_health_block_exposes_every_required_metric_and_no_content():
    wr = _wr()
    wr.note_prewarm(model="qwen3:8b", runner_identity="r1", prefix_identity="p1",
                    family="CONCISE")
    reset_warmth_runtime(wr)
    block = warmth_runtime_health()
    for key in ("session_state", "active_family", "observation_count", "reuse_state",
                "invalidation_count", "last_invalidation_reason",
                "predictive_rewarm_attempts", "predictive_rewarm_successes",
                "cooldown_remaining"):
        assert key in block, f"missing required metric: {key}"
    for value in block.values():
        assert isinstance(value, (str, int, float, bool, dict, type(None)))


def test_the_process_singleton_is_stable_and_resettable():
    a = get_warmth_runtime()
    assert get_warmth_runtime() is a
    reset_warmth_runtime(None)
    assert get_warmth_runtime() is not a


def test_health_never_raises_even_with_a_broken_baseline():
    class Broken:
        def snapshot(self):
            raise RuntimeError("broken")

    wr = _wr(baseline=Broken())
    reset_warmth_runtime(wr)
    assert warmth_runtime_health() == {}


# ══════════════════════════════════════════════════════════════════════════════
#  6. The runtime-health registry contract (ONE registry, extended — not a new one)
# ══════════════════════════════════════════════════════════════════════════════
_REQUIRED_BARGE_IN_METRICS = (
    "selected_backend", "portable_backend_available", "fallback_reason",
    "active_interruptions", "command_interruptions", "cancellation_latency_ms",
    "terminal_restore_failures", "orphan_reader_count", "console_busy_denials",
)
_REQUIRED_WARMTH_METRICS = (
    "session_state", "active_family", "observation_count", "reuse_state",
    "invalidation_count", "last_invalidation_reason", "predictive_rewarm_attempts",
    "predictive_rewarm_successes", "rewarm_preemptions", "cooldown_remaining",
    "rewarm_pending",
)


def test_runtime_health_exposes_every_required_m59_metric():
    from core.runtime_health import _prompt_cache_subsystem
    metrics = _prompt_cache_subsystem().metrics
    for key in _REQUIRED_BARGE_IN_METRICS + _REQUIRED_WARMTH_METRICS:
        assert key in metrics, f"runtime health is missing required metric: {key}"


def test_runtime_health_stays_advisory_and_bounded_and_content_free():
    from core.runtime_health import HealthStatus, _prompt_cache_subsystem
    sub = _prompt_cache_subsystem()
    # Advisory only: a cold prefix or a COMMAND_ONLY backend is a performance fact,
    # never a runtime fault, so it must not degrade the overall verdict.
    assert sub.status is HealthStatus.OPTIONAL
    blob = repr(sub.metrics)
    for leak in ("\x1b", "\x07", "password", "api_key", "secret", "Bearer "):
        assert leak not in blob
    for key, value in sub.metrics.items():
        assert isinstance(value, (str, bool, int, float, dict, type(None))), key
        if isinstance(value, str):
            assert len(value) <= 512, key


def test_the_m59_health_block_lives_in_the_single_existing_registry():
    """One health surface, extended — M59 must not add a second registry."""
    from core.runtime_health import collect_runtime_health
    snapshot = collect_runtime_health()
    names = [s.name for s in snapshot.subsystems]
    assert names.count("prompt_cache") == 1
    assert "session_warmth" not in names and "barge_in" not in names
    assert any(k.startswith("prompt_cache.selected_backend")
               for k in snapshot.metrics)
