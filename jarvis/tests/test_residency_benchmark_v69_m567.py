"""tests/test_residency_benchmark_v69_m567.py — V69 M56.7 benchmark harness.

Every performance claim in M55 came from reading a log by hand. These tests cover the
harness that replaces that: scenario definitions as data, honest derivation (a missing
measurement is never treated as zero), controlled prompts only, and the invariant that
time/status questions issue NO model request.
"""
from __future__ import annotations

import asyncio

from core.residency_benchmark import (
    CONTROLLED_PROMPTS,
    DETERMINISTIC_PROMPTS,
    PROMPT_CONCEPT,
    PROMPT_GREETING,
    BenchmarkHarness,
    BenchmarkReport,
    Scenario,
    ScenarioResult,
    TrialMetrics,
)

FAST = "qwen3:8b"
EMBED = "nomic-embed-text:latest"


def _run(coro):
    return asyncio.run(coro)


def _harness(*, fast_series=None, embedding_ok=True, deep_ok=True, cancel_ok=True,
             prewarm_ok=True, models=(FAST,), **kw):
    """A harness over fakes. ``fast_series`` supplies successive first-token times."""
    series = list(fast_series or [1200.0, 1200.0, 1200.0, 1200.0, 1200.0, 1200.0])
    state = {"i": 0}

    async def fast_turn(prompt, label):
        i = min(state["i"], len(series) - 1)
        state["i"] += 1
        ft = series[i]
        return TrialMetrics(label=label, prompt=prompt, first_token_ms=ft,
                            total_ms=ft + 800.0, load_duration_ms=0.0,
                            tokens_per_second=5.4, eval_count=32,
                            models_after=tuple(models), ok=True)

    async def embedding():
        return TrialMetrics(label="embedding", total_ms=300.0,
                            models_after=(EMBED,), ok=embedding_ok,
                            error=None if embedding_ok else "timeout")

    async def deep_turn():
        return TrialMetrics(label="deep", total_ms=40000.0, ok=deep_ok)

    async def cancel_turn():
        return TrialMetrics(label="cancelled", cancelled=True, total_ms=2000.0,
                            ok=cancel_ok)

    async def prewarm():
        return TrialMetrics(label="prewarm", first_token_ms=11000.0, total_ms=11500.0,
                            load_duration_ms=9500.0, ok=prewarm_ok)

    async def inspector():
        return tuple(models)

    async def deterministic(prompt):
        # A deterministic bypass answers with NO model request.
        return TrialMetrics(label=f"deterministic:{prompt}", prompt=prompt,
                            total_ms=3.0, model_requested=False, ok=True)

    return BenchmarkHarness(fast_turn=fast_turn, embedding=embedding,
                            deep_turn=deep_turn, cancel_turn=cancel_turn,
                            prewarm=prewarm, inspector=inspector,
                            deterministic=deterministic, **kw)


# ── controlled prompts ───────────────────────────────────────────────────────
def test_controlled_prompts_are_fixed_and_not_user_content():
    assert PROMPT_GREETING == "hola"
    assert CONTROLLED_PROMPTS == ("hola", "como saco la raiz cuadrada de algo",
                                  "explicame POO brevemente")
    for p in CONTROLLED_PROMPTS + DETERMINISTIC_PROMPTS:
        assert isinstance(p, str) and 0 < len(p) < 64


def test_deterministic_questions_issue_no_model_request():
    trials = _run(_harness().deterministic_bypass_check())
    assert len(trials) == len(DETERMINISTIC_PROMPTS)
    for t in trials:
        assert t.model_requested is False, "time/status must never touch the model"
        assert t.ok is True


# ── scenarios ────────────────────────────────────────────────────────────────
def test_scenario_a_records_cold_residency_context():
    res = _run(_harness(models=()).scenario_a_process_cold())
    assert res.scenario is Scenario.PROCESS_COLD
    assert len(res.trials) == 1
    assert res.trials[0].label == "cold_fast"
    assert any("no model was resident" in n for n in res.notes)


def test_scenario_b_runs_bounded_repeats():
    res = _run(_harness(trials=3).scenario_b_fast_warm())
    assert len(res.trials) == 3
    assert [t.label for t in res.trials] == ["warm_fast_1", "warm_fast_2", "warm_fast_3"]


def test_trials_are_clamped_to_a_small_number():
    """A 15W CPU must never be handed dozens of expensive permutations."""
    res = _run(_harness(trials=99).scenario_b_fast_warm())
    assert len(res.trials) <= 5


def test_scenario_c_measures_the_post_embedding_reload():
    res = _run(_harness(fast_series=[1200.0, 14000.0]).scenario_c_post_embedding())
    assert [t.label for t in res.trials] == [
        "fast_before_embedding", "embedding", "fast_after_embedding"]
    # The post-embedding turn was 12.8s slower: that is the eviction cost.
    assert res.improvement_ms("fast_after_embedding", "fast_before_embedding") == 12800.0


def test_scenario_d_post_deep_sequence():
    res = _run(_harness().scenario_d_post_deep())
    assert [t.label for t in res.trials] == ["fast_before_deep", "deep", "fast_after_deep"]


def test_scenario_e_cancellation_recovery():
    res = _run(_harness().scenario_e_post_cancel())
    assert res.trials[0].cancelled is True
    assert res.trials[1].label == "fast_after_cancel"
    assert res.trials[1].ok is True, "the next FAST turn must work after a cancellation"


def test_scenario_f_prewarm_then_first_operator_turn():
    res = _run(_harness(fast_series=[1300.0]).scenario_f_prewarm())
    assert [t.label for t in res.trials] == ["prewarm", "first_operator_turn"]
    assert res.trials[1].prompt == PROMPT_CONCEPT


# ── honest derivation ────────────────────────────────────────────────────────
def test_missing_measurement_is_never_treated_as_zero():
    res = ScenarioResult(scenario=Scenario.FAST_WARM)
    res.trials.append(TrialMetrics(label="a", first_token_ms=None, ok=False))
    res.trials.append(TrialMetrics(label="b", first_token_ms=1000.0, ok=True))
    assert res.improvement_ms("a", "b") is None
    assert res.improvement_ms("missing", "b") is None


def test_scenario_ok_requires_every_trial_to_succeed():
    res = _run(_harness(embedding_ok=False).scenario_c_post_embedding())
    assert res.ok() is False


def test_missing_probe_is_recorded_not_raised():
    h = BenchmarkHarness(fast_turn=_harness()._fast)   # no embedding/deep/cancel probes
    res = _run(h.scenario_c_post_embedding())
    assert res.trial("embedding").error == "no_probe"
    assert res.ok() is False


def test_raising_probe_is_captured():
    async def boom(prompt, label):
        raise RuntimeError("model exploded")

    h = BenchmarkHarness(fast_turn=boom)
    res = _run(h.scenario_a_process_cold())
    assert res.trials[0].ok is False
    assert res.trials[0].error == "RuntimeError"


# ── report ───────────────────────────────────────────────────────────────────
def test_report_derives_cold_versus_warm_improvement():
    h = _harness(fast_series=[19000.0, 1300.0, 1250.0], trials=2)
    report = _run(h.run([Scenario.PROCESS_COLD, Scenario.FAST_WARM]))
    assert report.cold_vs_warm_ms() == 17750.0


def test_report_cold_versus_warm_is_none_without_both_scenarios():
    report = _run(_harness().run([Scenario.FAST_WARM]))
    assert report.cold_vs_warm_ms() is None


def test_report_eviction_cost():
    h = _harness(fast_series=[1200.0, 14000.0])
    report = _run(h.run([Scenario.POST_EMBEDDING]))
    assert report.eviction_cost_ms() == 12800.0


def test_full_run_covers_all_six_scenarios():
    report = _run(_harness().run())
    assert [r.scenario for r in report.results] == list(Scenario)
    assert report.finished_at is not None


def test_report_render_is_ascii_and_bounded():
    report = _run(_harness().run([Scenario.PROCESS_COLD, Scenario.FAST_WARM]))
    text = report.render()
    assert text.isascii()
    assert "RESIDENCY BENCHMARK" in text
    assert len(text.splitlines()) < 40


def test_model_restoration_is_reported_from_observation():
    res = _run(_harness(models=(FAST,)).scenario_d_post_deep())
    assert res.model_restored(FAST) is True
    gone = _run(_harness(models=(EMBED,)).scenario_d_post_deep())
    assert gone.model_restored(FAST) is False
    empty = ScenarioResult(scenario=Scenario.POST_DEEP)
    assert empty.model_restored(FAST) is None


def test_power_profile_is_carried_into_every_result():
    report = _run(_harness(power_profile="BATTERY_SAVER").run([Scenario.FAST_WARM]))
    assert report.power_profile == "BATTERY_SAVER"
    assert report.results[0].power_profile == "BATTERY_SAVER"


def test_snapshot_is_json_shaped_and_complete():
    report = _run(_harness().run([Scenario.POST_EMBEDDING]))
    snap = report.snapshot()
    for key in ("power_profile", "residency_state", "cold_vs_warm_ms",
                "eviction_cost_ms", "results"):
        assert key in snap
    trial = snap["results"][0]["trials"][0]
    for key in ("label", "first_token_ms", "total_ms", "load_duration_ms",
                "tokens_per_second", "models_before", "models_after",
                "model_requested", "ok"):
        assert key in trial


def test_empty_report_is_safe():
    report = BenchmarkReport()
    assert report.cold_vs_warm_ms() is None
    assert report.eviction_cost_ms() is None
    assert "RESIDENCY BENCHMARK" in report.render()
