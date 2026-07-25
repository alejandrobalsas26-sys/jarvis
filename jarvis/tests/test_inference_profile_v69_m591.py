"""V69 M59.1 — prewarm / live sampling parity. Deterministic, server-free.

Proves the canonical inference-profile split (RUNNER_IDENTITY / PREFIX_IDENTITY /
GENERATION_ONLY): a derived prewarm profile is runner- and prefix-compatible with the
live profile it came from, an output-cap difference is allowed, and every runner-level
change (model / num_ctx / think / grammar / runner option) invalidates — while an
UNKNOWN option is treated conservatively.
"""
from __future__ import annotations

from core.generation_budget import budget_for_shape
from core.inference_profile import (
    GenerationOnlyOptions,
    InferenceProfile,
    ProfileKind,
    ResidualLoadClass,
    RunnerIdentity,
    classify_option,
    compare_runner,
    derive_prewarm_profile,
    live_profile_from_budget,
    profile_compatibility,
    profiles_for_shape,
    sampling_parity_health,
)
from core.response_contract import (
    ContractReason,
    ResponseContract,
    ResponseShape,
    _BASE_SHAPES,
)


def _shape(contract=ResponseContract.BRIEF, language="es"):
    return ResponseShape(contract=contract, reason=ContractReason.GENERAL_EDUCATIONAL,
                         language=language, **_BASE_SHAPES[contract])


def _live(contract=ResponseContract.BRIEF, *, model="qwen3:8b", num_ctx=2048,
          think=False, language="es", grammar="", runner_options=None):
    shape = _shape(contract, language)
    budget = budget_for_shape(shape, num_ctx=num_ctx)
    return live_profile_from_budget(
        budget, shape=shape, model=model, think=think,
        language=language, grammar=grammar, runner_options=runner_options)


# ── option classification ─────────────────────────────────────────────────────
def test_option_classification_is_total():
    assert classify_option("num_batch") == "runner"
    assert classify_option("num_predict") == "generation"
    assert classify_option("temperature") == "generation"
    assert classify_option("repeat_penalty") == "generation"
    assert classify_option("something_new_ollama_added") == "unknown"


# ── derived prewarm parity ────────────────────────────────────────────────────
def test_derived_prewarm_is_runner_and_prefix_compatible():
    live = _live()
    prewarm = derive_prewarm_profile(live)
    verdict = profile_compatibility(prewarm, live)
    assert verdict.runner_compatible is True
    assert verdict.prefix_compatible is True
    assert verdict.compatible is True
    assert verdict.incompatible_fields == ()
    assert verdict.unknown_fields == ()


def test_output_cap_difference_is_allowed_generation_only():
    live = _live()
    prewarm = derive_prewarm_profile(live, num_predict=4)
    verdict = profile_compatibility(prewarm, live)
    # The cap differs — that is generation-only and must NOT break compatibility.
    assert prewarm.generation.num_predict == 4
    assert live.generation.num_predict != 4
    assert verdict.generation_only_differs is True
    assert verdict.compatible is True


def test_derived_prewarm_keeps_live_sampling_posture():
    live = _live(ResponseContract.STANDARD)
    prewarm = derive_prewarm_profile(live)
    # Same temperature / top_p / repeat_penalty as the live turn — no hand-written set.
    assert prewarm.generation.temperature == live.generation.temperature
    assert prewarm.generation.top_p == live.generation.top_p
    assert prewarm.generation.repeat_penalty == live.generation.repeat_penalty


# ── runner-identity invalidation ──────────────────────────────────────────────
def test_model_change_invalidates_runner():
    live = _live()
    other = derive_prewarm_profile(_live(model="qwen3:14b"))
    verdict = profile_compatibility(other, live)
    assert verdict.runner_compatible is False
    assert "model" in verdict.incompatible_fields


def test_num_ctx_change_invalidates_runner():
    live = _live(num_ctx=2048)
    other = derive_prewarm_profile(_live(num_ctx=1024))
    verdict = profile_compatibility(other, live)
    assert verdict.runner_compatible is False
    assert "num_ctx" in verdict.incompatible_fields


def test_think_change_invalidates_runner():
    live = _live(think=False)
    other = derive_prewarm_profile(_live(think=True))
    verdict = profile_compatibility(other, live)
    assert verdict.runner_compatible is False
    assert "think" in verdict.incompatible_fields


def test_grammar_change_invalidates_runner():
    live = _live(grammar="")
    other = derive_prewarm_profile(_live(grammar="json"))
    verdict = profile_compatibility(other, live)
    assert verdict.runner_compatible is False
    assert "grammar" in verdict.incompatible_fields


def test_runner_affecting_option_change_invalidates():
    live = _live(runner_options={"num_batch": 512})
    other = derive_prewarm_profile(_live(runner_options={"num_batch": 256}))
    verdict = profile_compatibility(other, live)
    assert verdict.runner_compatible is False
    assert "option:num_batch" in verdict.incompatible_fields


def test_unknown_option_is_conservatively_incompatible():
    live = _live(runner_options={"mystery_knob": 1})
    other = derive_prewarm_profile(_live(runner_options={"mystery_knob": 2}))
    verdict = profile_compatibility(other, live)
    assert verdict.runner_compatible is False
    assert "mystery_knob" in verdict.unknown_fields
    assert "option:mystery_knob" in verdict.incompatible_fields


def test_generation_only_change_does_not_invalidate_runner():
    # Two profiles that differ ONLY in temperature must still be runner-compatible.
    live = _live()
    hot = derive_prewarm_profile(live)
    object.__setattr__(hot, "generation",
                       GenerationOnlyOptions(num_predict=4, temperature=0.99,
                                             top_p=live.generation.top_p,
                                             repeat_penalty=live.generation.repeat_penalty))
    verdict = profile_compatibility(hot, live)
    assert verdict.runner_compatible is True
    assert verdict.generation_only_differs is True
    assert verdict.compatible is True


# ── prefix identity ───────────────────────────────────────────────────────────
def test_prefix_change_makes_incompatible():
    live = _live(language="es")
    other = derive_prewarm_profile(_live(language="en"))
    verdict = profile_compatibility(other, live)
    assert verdict.prefix_compatible is False
    assert "prefix_identity" in verdict.incompatible_fields


def test_same_family_different_contract_shares_prefix_identity():
    # BRIEF and INSTANT are the same family: their prefix identity must match.
    brief = _live(ResponseContract.BRIEF)
    instant = _live(ResponseContract.INSTANT)
    assert brief.prefix_identity == instant.prefix_identity


# ── residual load honesty ─────────────────────────────────────────────────────
def test_residual_load_no_reload_when_warm():
    live = _live()
    verdict = profile_compatibility(derive_prewarm_profile(live), live)
    assert verdict.classify_residual_load(120.0) is ResidualLoadClass.NO_RELOAD


def test_residual_load_despite_compatible_is_honest():
    live = _live()
    verdict = profile_compatibility(derive_prewarm_profile(live), live)
    # A real load with a proven-compatible profile is an eviction reload, never a
    # sampling mismatch.
    assert verdict.classify_residual_load(9000.0) is \
        ResidualLoadClass.RELOAD_DESPITE_COMPATIBLE


def test_residual_load_runner_mismatch():
    live = _live()
    other = derive_prewarm_profile(_live(num_ctx=1024))
    verdict = profile_compatibility(other, live)
    assert verdict.classify_residual_load(9000.0) is \
        ResidualLoadClass.RELOAD_RUNNER_MISMATCH


def test_residual_load_unknown_without_measurement():
    live = _live()
    verdict = profile_compatibility(derive_prewarm_profile(live), live)
    assert verdict.classify_residual_load(None) is ResidualLoadClass.UNKNOWN


# ── profiles_for_shape derives from the budget ────────────────────────────────
def test_profiles_for_shape_derives_from_budget():
    live, prewarm = profiles_for_shape(
        _shape(ResponseContract.BRIEF), model="qwen3:8b", num_ctx=2048)
    assert live.kind is ProfileKind.LIVE
    assert prewarm.kind is ProfileKind.PREWARM
    verdict = profile_compatibility(prewarm, live)
    assert verdict.compatible is True
    # The prewarm caps output but keeps the live sampling — no duplicate option set.
    assert prewarm.generation.num_predict < live.generation.num_predict
    assert prewarm.generation.temperature == live.generation.temperature


# ── health block is content-free ──────────────────────────────────────────────
def test_sampling_parity_health_is_content_free():
    live = _live()
    prewarm = derive_prewarm_profile(live)
    health = sampling_parity_health(prewarm, live, observed_load_ms=120.0)
    assert health["compatible"] is True
    assert health["residual_load_class"] == ResidualLoadClass.NO_RELOAD.value
    # Only fingerprints and field names — never raw options or prompt text.
    assert isinstance(health["prewarm_runner_identity"], str)
    assert len(health["prewarm_runner_identity"]) == 16
    for value in health.values():
        assert "You are" not in str(value)
        assert "temperature" not in str(value)


def test_sampling_parity_health_none_is_safe():
    health = sampling_parity_health(None, None)
    assert health["compatible"] is None
    assert health["residual_load_class"] == ResidualLoadClass.UNKNOWN.value


def test_runner_identity_fingerprint_is_stable_and_16_hex():
    a = RunnerIdentity(model="qwen3:8b", transport="native", think=False, num_ctx=2048)
    b = RunnerIdentity(model="qwen3:8b", transport="native", think=False, num_ctx=2048)
    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 16
    assert isinstance(
        InferenceProfile(kind=ProfileKind.LIVE, runner=a, prefix_identity="x",
                         generation=GenerationOnlyOptions(
                             num_predict=64, temperature=0.3, top_p=0.9,
                             repeat_penalty=1.1)).snapshot()["runner_fingerprint"], str)


def test_compare_runner_reports_no_unknowns_when_equal():
    a = RunnerIdentity(model="qwen3:8b", transport="native", think=False, num_ctx=2048)
    ok, incompatible, unknown = compare_runner(a, a)
    assert ok is True
    assert incompatible == ()
    assert unknown == ()
