"""
tests/test_critic.py — regression floor for core.critic.CriticEngine.

The CriticEngine is the scoring backbone the V64 eval harness (M14) and failure
dataset (M16) reuse, but it had zero tests. This locks its result-scoring,
plan-scoring, failure-mode classification, and repair-advice contracts.
"""
from __future__ import annotations

from core.cognitive_types import (
    CognitivePlan,
    CompletionStatus,
    ExecutionTrace,
    PlanStep,
    RiskLevel,
)
from core.critic import CriticEngine


def _critic() -> CriticEngine:
    return CriticEngine()


# ── score_result ──────────────────────────────────────────────────────────────
def test_score_result_completed_clean():
    res = {"status": "completed", "traces": [{"tool": "a"}, {"tool": "b"}, {"tool": "c"}], "errors": []}
    out = _critic().score_result("do X", res)
    assert out["passed"] is True
    assert out["scores"]["correctness"] == 1.0
    assert out["overall"] >= 0.5


def test_score_result_with_failed_trace_not_passed():
    res = {"status": "completed", "traces": [{"tool": "a", "error": "boom"}], "errors": ["boom"]}
    out = _critic().score_result("do X", res)
    assert out["passed"] is False
    assert out["scores"]["correctness"] < 1.0
    assert any("failed_steps" in f for f in out["flags"])


def test_score_result_no_evidence_flagged():
    out = _critic().score_result("do X", {"status": "completed", "traces": [], "errors": []})
    assert "no_evidence" in out["flags"]
    assert out["scores"]["evidence_quality"] == 0.0


# ── score_plan ────────────────────────────────────────────────────────────────
def test_score_plan_flags_destructive_and_requires_approval():
    plan = CognitivePlan(objective="cleanup", plan_steps=[
        PlanStep(index=0, action="run rm -rf /home/x", tool="run_shell_command",
                 tool_input={"cmd": "rm -rf /home/x"}, risk_level=RiskLevel.HIGH),
    ])
    out = _critic().score_plan(plan)
    assert out["scores"]["operator_approval_required"] is True
    assert out["scores"]["safety"] == 0.0
    assert any("destructive_action" in f for f in out["flags"])
    assert out["approved"] is False


def test_score_plan_clean_plan_approved():
    plan = CognitivePlan(objective="lookup", plan_steps=[
        PlanStep(index=0, action="query dns", tool="dns_lookup", tool_input={"host": "x"}),
        PlanStep(index=1, action="verify result", tool=None),
    ])
    out = _critic().score_plan(plan)
    assert out["approved"] is True
    assert out["scores"]["safety"] == 1.0
    # a verification step is present ⇒ no "no_verification_step" flag
    assert "no_verification_step" not in out["flags"]


def test_score_plan_missing_verification_flagged():
    plan = CognitivePlan(objective="do", plan_steps=[
        PlanStep(index=0, action="fetch data", tool="fetch_webpage", tool_input={}),
    ])
    out = _critic().score_plan(plan)
    assert "no_verification_step" in out["flags"]


# ── failure modes + repair advice ─────────────────────────────────────────────
def test_detect_failure_modes_classifies_tokens():
    c = _critic()
    timeout = ExecutionTrace(step_index=0, tool="x", error="operation timed out",
                             status=CompletionStatus.FAILED)
    assert "timeout" in c.detect_failure_modes(timeout)

    blocked = ExecutionTrace(step_index=1, tool="y", status=CompletionStatus.BLOCKED)
    assert "blocked_by_guardrail" in c.detect_failure_modes(blocked)

    denied = ExecutionTrace(step_index=2, tool="z", observation="operator denied the action",
                            status=CompletionStatus.FAILED)
    assert "operator_denied" in c.detect_failure_modes(denied)


def test_recommend_repair_maps_modes_to_advice():
    advice = _critic().recommend_repair(["timeout", "operator_denied", "unknown_mode"])
    assert any("timeout" in a.lower() or "scope" in a.lower() for a in advice)
    assert any("approval" in a.lower() for a in advice)
    # unknown modes yield no advice (no crash, no fabricated guidance)
    assert all(isinstance(a, str) for a in advice)
