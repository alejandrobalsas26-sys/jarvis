"""V69 M58.5/.7.1 — cache observation, compatibility, invalidation, tool schema.

Deterministic and server-free.
"""
from __future__ import annotations

from core.prefix_cache import (
    CacheState,
    InvalidationReason,
    PrefixCacheObserver,
    diff_invalidation,
)
from core.prompt_manifest import build_manifest
from core.tool_schema import (
    EMPTY_TOOL_SCHEMA_FINGERPRINT,
    build_tool_schema_fingerprint,
    tool_schema_fingerprint,
)


def _m(**over):
    base = dict(model="qwen3:8b", transport="native", think=False, num_ctx=2048,
                language="es", authority_mode="STANDARD", scope_fingerprint="",
                tool_schema_fingerprint="")
    base.update(over)
    return build_manifest(**base)


# ── invalidation reasons ──────────────────────────────────────────────────────
def test_compatible_manifests_have_no_invalidation():
    assert diff_invalidation(_m(), _m()) is None


def test_each_changed_field_names_its_deterministic_reason():
    cases = [
        (dict(model="qwen3:14b"), InvalidationReason.MODEL_CHANGED),
        (dict(transport="openai"), InvalidationReason.TRANSPORT_CHANGED),
        (dict(num_ctx=1024), InvalidationReason.NUM_CTX_CHANGED),
        (dict(think=True), InvalidationReason.THINK_CHANGED),
        (dict(language="en"), InvalidationReason.LANGUAGE_CHANGED),
        (dict(authority_mode="ELEVATED"), InvalidationReason.AUTHORITY_CHANGED),
        (dict(scope_fingerprint="s2"), InvalidationReason.SCOPE_CHANGED),
        (dict(tool_schema_fingerprint="abc"), InvalidationReason.TOOL_SCHEMA_CHANGED),
    ]
    for over, expected in cases:
        assert diff_invalidation(_m(), _m(**over)) is expected, over


def test_power_profile_change_is_an_invalidation():
    assert diff_invalidation(_m(), _m(), old_power="AC", new_power="BATTERY") \
        is InvalidationReason.POWER_PROFILE_CHANGED


# ── cache classification: never infer reuse from residency alone ──────────────
def test_model_loaded_alone_does_not_prove_reuse():
    obs = PrefixCacheObserver()
    # A cold load with a big prompt_eval — this is COLD, never reuse.
    st = obs.classify(compatibility_identity="id", prompt_eval_count=500,
                      prompt_eval_ms=4000.0, load_ms=9000.0)
    assert st is CacheState.COLD_MODEL


def test_insufficient_evidence_stays_unknown():
    obs = PrefixCacheObserver()
    st = obs.classify(compatibility_identity="id", prompt_eval_count=None,
                      prompt_eval_ms=None, load_ms=None)
    assert st is CacheState.INSUFFICIENT_EVIDENCE


def test_prefix_reuse_observed_requires_a_prompt_eval_drop():
    obs = PrefixCacheObserver()
    # 1) cold baseline for the identity
    obs.classify(compatibility_identity="id", prompt_eval_count=500,
                 prompt_eval_ms=4000.0, load_ms=9000.0)
    # 2) warm turn, prefill dropped hard → OBSERVED
    st = obs.classify(compatibility_identity="id", prompt_eval_count=500,
                      prompt_eval_ms=800.0, load_ms=100.0)
    assert st is CacheState.PREFIX_REUSE_OBSERVED
    assert obs.observed_reuse_ratio() is not None and obs.observed_reuse_ratio() > 0


def test_warm_without_baseline_is_prefix_unknown():
    obs = PrefixCacheObserver()
    st = obs.classify(compatibility_identity="id", prompt_eval_count=500,
                      prompt_eval_ms=800.0, load_ms=50.0)
    assert st is CacheState.MODEL_WARM_PREFIX_UNKNOWN


def test_config_mismatch_when_identity_differs_from_warmed():
    obs = PrefixCacheObserver()
    st = obs.classify(compatibility_identity="idA", prompt_eval_count=10,
                      prompt_eval_ms=100.0, load_ms=50.0, warmed_identity="idB")
    assert st is CacheState.CONFIG_MISMATCH


def test_invalidation_clears_baselines_and_counts():
    obs = PrefixCacheObserver()
    obs.classify(compatibility_identity="id", prompt_eval_count=500,
                 prompt_eval_ms=4000.0, load_ms=9000.0)
    obs.note_invalidation(InvalidationReason.NUM_CTX_CHANGED)
    assert obs.invalidations == 1
    assert obs.last_invalidation_reason == "NUM_CTX_CHANGED"
    # after invalidation, a warm turn cannot claim reuse (baseline was cleared)
    st = obs.classify(compatibility_identity="id", prompt_eval_count=500,
                      prompt_eval_ms=800.0, load_ms=50.0)
    assert st is CacheState.MODEL_WARM_PREFIX_UNKNOWN


def test_snapshot_is_content_free():
    obs = PrefixCacheObserver()
    obs.classify(compatibility_identity="id", prompt_eval_count=500,
                 prompt_eval_ms=4000.0, load_ms=9000.0)
    blob = repr(obs.snapshot())
    assert "hola" not in blob and "user" not in blob.lower()
    assert "cache_state" in obs.snapshot()


# ── tool-schema fingerprint (M58.7.1) ─────────────────────────────────────────
def test_tool_ordering_does_not_change_the_fingerprint():
    a = [{"type": "function", "function": {"name": "b", "parameters": {}}},
         {"type": "function", "function": {"name": "a", "parameters": {}}}]
    b = list(reversed(a))
    assert tool_schema_fingerprint(a) == tool_schema_fingerprint(b)


def test_field_ordering_does_not_change_the_fingerprint():
    a = [{"type": "function", "function": {"name": "x", "description": "d",
                                           "parameters": {"type": "object"}}}]
    b = [{"function": {"parameters": {"type": "object"}, "name": "x",
                       "description": "d"}, "type": "function"}]
    assert tool_schema_fingerprint(a) == tool_schema_fingerprint(b)


def test_empty_schema_has_a_stable_distinct_fingerprint():
    assert tool_schema_fingerprint([]) == EMPTY_TOOL_SCHEMA_FINGERPRINT
    assert tool_schema_fingerprint(None) == EMPTY_TOOL_SCHEMA_FINGERPRINT
    populated = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    assert tool_schema_fingerprint(populated) != EMPTY_TOOL_SCHEMA_FINGERPRINT


def test_schema_change_changes_the_fingerprint():
    a = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    b = [{"type": "function", "function": {"name": "x", "parameters": {
        "type": "object", "properties": {"q": {"type": "string"}}}}}]
    assert tool_schema_fingerprint(a) != tool_schema_fingerprint(b)


def test_eligible_subset_is_measured_before_and_after_filtering():
    full = [{"type": "function", "function": {"name": f"t{i}", "parameters": {}}}
            for i in range(5)]
    eligible = full[:2]
    fp = build_tool_schema_fingerprint(full, eligible_tools=eligible)
    assert fp.tool_count == 5
    assert fp.eligible_tool_count == 2
    assert fp.estimated_tokens >= fp.eligible_estimated_tokens
    # the fingerprint is over the ELIGIBLE subset (what the model sees)
    assert fp.fingerprint == tool_schema_fingerprint(eligible)
