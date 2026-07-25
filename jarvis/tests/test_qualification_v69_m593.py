"""V69 M59.3 — automated prefix qualification matrix. Deterministic, server-free.

Proves the qualification matrix is bounded (no Cartesian explosion), its JSON artifact
is content-safe (fixture IDs only, no raw prompts/bodies/secrets), cold/warm/battery
thresholds are separate, and a missing live server is INSUFFICIENT_EVIDENCE — never a
false PASS.
"""
from __future__ import annotations

import json

from core.qualification import (
    FIXTURES,
    FULL_LIVE_FIXTURES,
    MAX_FULL_LIVE_GENERATIONS,
    MAX_QUICK_LIVE_GENERATIONS,
    QUICK_LIVE_FIXTURES,
    CaseResult,
    CaseVerdict,
    ThresholdProfile,
    aggregate_verdict,
    build_artifact,
    evaluate_live_case,
    host_profile_snapshot,
    run_deterministic_matrix,
    select_threshold_profile,
)


# ── deterministic matrix ──────────────────────────────────────────────────────
def test_deterministic_matrix_all_pass_and_bounded():
    cases = run_deterministic_matrix()
    assert len(cases) == 9                      # curated, not a product
    assert all(c.verdict is CaseVerdict.PASS for c in cases), \
        [c.snapshot() for c in cases if c.verdict is not CaseVerdict.PASS]
    assert all(c.kind == "deterministic" for c in cases)


def test_matrix_case_ids_are_unique():
    cases = run_deterministic_matrix()
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))


def test_fixture_ids_are_the_documented_four():
    assert set(FIXTURES) == {"GREETING_ES", "BRIEF_MATH_ES", "STANDARD_PYTHON_ES",
                             "GREETING_EN"}


# ── threshold profiles ────────────────────────────────────────────────────────
def test_cold_and_warm_thresholds_are_separate():
    warm = select_threshold_profile(power_profile="AC", warm=True)
    cold = select_threshold_profile(power_profile="AC", warm=False)
    assert warm.name == "WARM_AC"
    assert cold.name == "COLD_AC"
    # A cold run must never be judged by the warm bound.
    assert cold.max_prompt_eval_ms > warm.max_prompt_eval_ms


def test_battery_profile_is_separate():
    bat = select_threshold_profile(power_profile="BATTERY", warm=True)
    assert bat.name == "WARM_BATTERY"
    warm_ac = select_threshold_profile(power_profile="AC", warm=True)
    assert bat.max_first_content_ms >= warm_ac.max_first_content_ms


def test_unknown_power_is_conservative():
    unk = select_threshold_profile(power_profile="UNKNOWN", warm=True)
    assert unk.name == "UNKNOWN"


# ── live case evaluation ──────────────────────────────────────────────────────
def _warm_ac():
    return select_threshold_profile(power_profile="AC", warm=True)


def test_missing_server_is_insufficient_evidence():
    case = evaluate_live_case("live_greeting", {"error": "connect_refused"}, _warm_ac())
    assert case.verdict is CaseVerdict.INSUFFICIENT_EVIDENCE


def test_live_case_within_bounds_passes():
    metrics = {"dispatch_ms": 200.0, "prompt_eval_ms": 400.0,
               "first_content_ms": 1200.0, "total_ms": 5000.0, "cache_state": "X",
               "num_ctx": 2048}
    case = evaluate_live_case("live_greeting", metrics, _warm_ac())
    assert case.verdict is CaseVerdict.PASS


def test_live_case_over_bound_fails():
    metrics = {"dispatch_ms": 200.0, "prompt_eval_ms": 9000.0,
               "first_content_ms": 12000.0, "total_ms": 5000.0}
    case = evaluate_live_case("live_greeting", metrics, _warm_ac())
    assert case.verdict is CaseVerdict.FAIL
    assert "prompt_eval_ms" in case.detail


# ── aggregate verdict ─────────────────────────────────────────────────────────
def test_aggregate_deterministic_only_passes():
    cases = run_deterministic_matrix()
    assert aggregate_verdict(cases, live_requested=False) == CaseVerdict.PASS.value


def test_aggregate_live_requested_without_evidence_is_insufficient():
    cases = run_deterministic_matrix()
    cases.append(evaluate_live_case("live", {"error": "down"}, _warm_ac()))
    # Live asked for, server down → never a PASS.
    assert aggregate_verdict(cases, live_requested=True) == \
        CaseVerdict.INSUFFICIENT_EVIDENCE.value


def test_aggregate_deterministic_failure_is_fail():
    cases = run_deterministic_matrix()
    cases.append(CaseResult("broken", "deterministic", CaseVerdict.FAIL))
    assert aggregate_verdict(cases, live_requested=False) == CaseVerdict.FAIL.value


# ── artifact ──────────────────────────────────────────────────────────────────
def test_artifact_is_valid_json_and_content_safe():
    cases = run_deterministic_matrix()
    art = build_artifact(cases, mode="quick", live_requested=False, timestamp=1234.0,
                         git={"commit": "abc123", "branch": "m59"},
                         host=host_profile_snapshot(), power_profile="AC")
    text = json.dumps(art)                       # must round-trip
    reloaded = json.loads(text)
    assert reloaded["schema_version"] == "m59.3.1"
    assert reloaded["verdict"] == CaseVerdict.PASS.value
    assert reloaded["counts"]["passed"] == 9
    # Content safety: NO raw fixture prompt text may appear anywhere in the artifact.
    for fx in FIXTURES.values():
        assert fx.prompt not in text
    # Fixtures are present only as IDs.
    assert "GREETING_ES" in reloaded["fixtures"]


def test_artifact_excludes_generated_bodies_and_secrets():
    cases = [CaseResult("live", "live", CaseVerdict.PASS,
                        metrics={"total_ms": 5000.0, "num_ctx": 2048})]
    art = build_artifact(cases, mode="full", live_requested=True, timestamp=1.0)
    text = json.dumps(art)
    assert "sk-" not in text
    assert "password" not in text.lower()


def test_live_generation_caps_are_bounded():
    assert len(QUICK_LIVE_FIXTURES) <= MAX_QUICK_LIVE_GENERATIONS
    assert len(FULL_LIVE_FIXTURES) <= MAX_FULL_LIVE_GENERATIONS
    assert MAX_QUICK_LIVE_GENERATIONS <= MAX_FULL_LIVE_GENERATIONS


def test_host_profile_has_no_private_paths():
    host = host_profile_snapshot()
    text = json.dumps(host)
    assert "Users" not in text and "home" not in text.lower()
    assert "system" in host and "logical_cpus" in host


def test_threshold_profile_snapshot_shape():
    p = ThresholdProfile("X", 1, 2, 3, 4)
    snap = p.snapshot()
    assert snap["max_stable_fp_count"] == 1
    assert snap["max_num_ctx_count"] == 1


# ── release verdict (M59.6) ───────────────────────────────────────────────────
def _all_green(**over):
    base = dict(deterministic_ok=True, regression_ok=True, ruff_ok=True,
                compile_ok=True, soak_ok=True)
    base.update(over)
    return base


def test_release_pass_when_all_green_no_live():
    from core.qualification import release_verdict
    assert release_verdict(**_all_green(), live_verdict=None) == "PASS"


def test_release_fail_on_any_mandatory_red():
    from core.qualification import release_verdict
    assert release_verdict(**_all_green(regression_ok=False)) == "FAIL"
    assert release_verdict(**_all_green(ruff_ok=False)) == "FAIL"
    assert release_verdict(**_all_green(compile_ok=False)) == "FAIL"


def test_release_fail_on_live_regression():
    from core.qualification import release_verdict
    assert release_verdict(**_all_green(), live_verdict="FAIL") == "FAIL"


def test_release_missing_live_is_pass_with_warnings():
    from core.qualification import release_verdict
    v = release_verdict(**_all_green(), live_verdict="INSUFFICIENT_EVIDENCE")
    assert v == "PASS_WITH_WARNINGS"


def test_release_security_failure_is_fail():
    from core.qualification import release_verdict
    assert release_verdict(**_all_green(), security_ok=False) == "FAIL"
    assert release_verdict(**_all_green(), orphan_ok=False) == "FAIL"
