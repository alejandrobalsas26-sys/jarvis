"""
tests/test_eval_harness.py — V64 M14 Evaluation Harness.

Mission-required coverage: deterministic case execution, reproducible result
serialization, baseline comparison, regression detection, timeout handling, and
missing-ground-truth handling. Plus a real-caller run measuring the M12 firewall
over the adversarial injection-resistance dataset.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from core.eval_harness import (
    EvalCase,
    EvalResult,
    EvalRun,
    EvalRunner,
    Expect,
    compare_runs,
    evaluate_output,
    firewall_eval_target,
    load_cases,
    save_cases,
)

_INJECTION_SET = Path(__file__).resolve().parents[1] / "evals" / "prompt_injection" / "injection_resistance.jsonl"


def _run(coro):
    return asyncio.run(coro)


def _dict_target():
    """Target that returns whatever the case stashed under context['output']."""
    async def target(case: EvalCase) -> dict:
        return case.context.get("output", {})
    return target


# ── deterministic scoring ─────────────────────────────────────────────────────
def test_evaluate_output_contains_and_forbidden():
    case = EvalCase(id="c1", domain="general", prompt="p",
                    expect=Expect(contains=("paris",), not_contains=("london",)))
    ok = evaluate_output(case, {"answer": "The capital is Paris."}, 0.1)
    assert ok.passed and ok.score == 1.0
    bad = evaluate_output(case, {"answer": "It is London."}, 0.1)
    assert not bad.passed
    assert any("correctness" in f or "forbidden_output" in f for f in bad.failures)


def test_tool_choice_and_safety_dimensions():
    case = EvalCase(id="c2", domain="research", prompt="p",
                    expect=Expect(required_tools=("web_search",), forbidden_tools=("run_shell_command",)))
    good = evaluate_output(case, {"tools_used": ["web_search"]}, 0.1)
    assert good.passed
    bad = evaluate_output(case, {"tools_used": ["run_shell_command"]}, 0.1)
    assert not bad.passed
    assert "tool_safety" in bad.metrics and not bad.metrics["tool_safety"]["passed"]


def test_missing_ground_truth_skips_dimension():
    # No expectations and no ground truth ⇒ nothing to score ⇒ trivially passes.
    case = EvalCase(id="c3", domain="general", prompt="p")
    res = evaluate_output(case, {"answer": "anything"}, 0.1)
    assert res.passed and res.score == 1.0
    assert res.metrics == {}


def test_ground_truth_dimension_when_present():
    case = EvalCase(id="c4", domain="general", prompt="p", ground_truth="42")
    assert evaluate_output(case, {"answer": "the answer is 42"}, 0.1).passed
    assert not evaluate_output(case, {"answer": "the answer is 7"}, 0.1).passed


def test_citation_validity_dimension():
    case = EvalCase(id="c5", domain="research", prompt="p", expect=Expect(must_cite=True))
    good = evaluate_output(case, {"citations": [{"fetched": True, "tier": "primary"}]}, 0.1)
    assert good.passed
    invented = evaluate_output(case, {"citations": [{"fetched": False, "tier": "primary"}]}, 0.1)
    assert not invented.passed
    none = evaluate_output(case, {"citations": []}, 0.1)
    assert not none.passed


# ── runner: timeout + error handling ──────────────────────────────────────────
def test_runner_timeout_handling():
    async def slow(case):
        await asyncio.sleep(1.0)
        return {"answer": "too late"}

    case = EvalCase(id="t1", domain="general", prompt="p", timeout_s=0.05)
    res = _run(EvalRunner(slow).run_case(case))
    assert not res.passed
    assert res.error == "timeout"


def test_runner_target_error_is_a_failure_not_a_crash():
    async def boom(case):
        raise RuntimeError("kaboom")

    res = _run(EvalRunner(boom).run_case(EvalCase(id="e1", domain="general", prompt="p")))
    assert not res.passed
    assert res.error and "kaboom" in res.error


# ── run aggregation + serialization ───────────────────────────────────────────
def test_run_summary_and_jsonl_roundtrip():
    cases = [
        EvalCase(id="a", domain="general", prompt="p",
                 expect=Expect(contains=("x",)), context={"output": {"answer": "x"}}),
        EvalCase(id="b", domain="coding", prompt="p",
                 expect=Expect(contains=("y",)), context={"output": {"answer": "nope"}}),
    ]
    run = _run(EvalRunner(_dict_target()).run_suite(cases, run_id="r1", now_ts=1000.0))
    s = run.summary()
    assert s["cases"] == 2
    assert s["passed"] == 1 and s["failed"] == 1
    assert 0.0 < s["pass_rate"] < 1.0
    # deterministic metadata (now_ts injected — reproducible)
    assert run.metadata["created_at"] == 1000.0
    # JSONL: header line + one per result
    lines = run.to_jsonl().strip().splitlines()
    assert len(lines) == 3


def test_run_save_writes_file(tmp_path):
    case = EvalCase(id="a", domain="general", prompt="p", context={"output": {"answer": "x"}})
    run = _run(EvalRunner(_dict_target()).run_suite([case], run_id="r2", now_ts=1.0))
    out = run.save(tmp_path / "res" / "r2.jsonl")
    assert out.exists()
    assert "run_header" in out.read_text(encoding="utf-8")


# ── baseline comparison + regression detection ────────────────────────────────
def _run_from_results(run_id, results):
    return EvalRun(run_id=run_id, results=results, metadata={})


def test_compare_runs_detects_regression_and_improvement():
    baseline = _run_from_results("base", [
        EvalResult("a", "general", True, 1.0, {"injection_resistance": {"passed": True}}),
        EvalResult("b", "general", True, 1.0, {"injection_resistance": {"passed": True}}),
    ])
    worse = _run_from_results("cand", [
        EvalResult("a", "general", True, 1.0, {"injection_resistance": {"passed": True}}),
        EvalResult("b", "general", False, 0.0, {"injection_resistance": {"passed": False}}),
    ])
    report = compare_runs(baseline, worse)
    assert report.has_regression
    assert report.pass_rate_delta < 0
    assert any("injection_resistance" in r for r in report.regressions)

    better = _run_from_results("cand2", [
        EvalResult("a", "general", True, 1.0, {"correctness": {"passed": True}}),
        EvalResult("b", "general", True, 1.0, {"correctness": {"passed": True}}),
    ])
    base2 = _run_from_results("base2", [
        EvalResult("a", "general", False, 0.0, {"correctness": {"passed": False}}),
        EvalResult("b", "general", True, 1.0, {"correctness": {"passed": True}}),
    ])
    rep2 = compare_runs(base2, better)
    assert not rep2.has_regression
    assert any("correctness" in i for i in rep2.improvements)


def test_compare_runs_no_change_no_regression():
    a = _run_from_results("a", [EvalResult("x", "general", True, 1.0, {"correctness": {"passed": True}})])
    b = _run_from_results("b", [EvalResult("x", "general", True, 1.0, {"correctness": {"passed": True}})])
    assert not compare_runs(a, b).has_regression


# ── dataset persistence ───────────────────────────────────────────────────────
def test_cases_jsonl_roundtrip(tmp_path):
    cases = [
        EvalCase(id="a", domain="general", prompt="p1", expect=Expect(contains=("x",)), tags=("t",)),
        EvalCase(id="b", domain="coder", prompt="p2", ground_truth="gt", timeout_s=5.0),
    ]
    path = save_cases(cases, tmp_path / "d.jsonl")
    loaded = load_cases(path)
    assert [c.id for c in loaded] == ["a", "b"]
    assert loaded[0].expect.contains == ("x",)
    assert loaded[1].ground_truth == "gt" and loaded[1].timeout_s == 5.0


# ── real caller: measure the M12 firewall over the adversarial set ────────────
def test_firewall_injection_resistance_dataset_all_pass():
    cases = load_cases(_INJECTION_SET)
    assert len(cases) >= 5
    run = _run(EvalRunner(firewall_eval_target()).run_suite(cases, run_id="fw", now_ts=1.0))
    # The M12 firewall must detect/quarantine every attack AND not false-positive
    # on the benign controls — a measured, reproducible resistance score.
    assert run.pass_rate == 1.0, [r.failures for r in run.results if not r.passed]
    assert run.metric_pass_rates().get("injection_resistance") == 1.0
