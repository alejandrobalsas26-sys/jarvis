"""
tests/test_critic.py — V58.0 COGNITIVE CORE self-evaluation gates.

Verifies the critic flags unsafe/destructive plans, scores results, classifies
failure modes, and never executes tools. Pure CPU, no network/admin/hardware.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest
from core.critic import CriticEngine
from core.cognitive_types import (
    CognitivePlan, PlanStep, ExecutionTrace, RiskLevel, CompletionStatus,
)


@pytest.fixture
def critic() -> CriticEngine:
    return CriticEngine()


def _plan(steps):
    return CognitivePlan(objective="test", plan_steps=steps)


class TestScorePlan:
    def test_flags_destructive_plan(self, critic):
        plan = _plan([
            PlanStep(index=0, action="rm -rf / on host", tool="run_shell_command"),
        ])
        verdict = critic.score_plan(plan)
        assert verdict["approved"] is False
        assert verdict["scores"]["safety"] == 0.0
        assert any("destructive" in f for f in verdict["flags"])

    def test_high_risk_tool_requires_approval(self, critic):
        plan = _plan([
            PlanStep(index=0, action="execute offensive_rpc", tool="offensive_rpc",
                     risk_level=RiskLevel.HIGH),
            PlanStep(index=1, action="verify outcome"),
        ])
        verdict = critic.score_plan(plan)
        assert verdict["scores"]["operator_approval_required"] is True
        assert any("high_risk_tool" in f for f in verdict["flags"])

    def test_safe_plan_approved(self, critic):
        plan = _plan([
            PlanStep(index=0, action="assess objective"),
            PlanStep(index=1, action="execute web_search", tool="web_search"),
            PlanStep(index=2, action="verify outcome and summarize"),
        ])
        verdict = critic.score_plan(plan)
        assert verdict["approved"] is True
        assert verdict["scores"]["operator_approval_required"] is False

    def test_missing_verification_flagged(self, critic):
        plan = _plan([PlanStep(index=0, action="execute web_search", tool="web_search")])
        verdict = critic.score_plan(plan)
        assert "no_verification_step" in verdict["flags"]


class TestScoreResult:
    def test_failed_steps_penalized(self, critic):
        result = {
            "status": "failed",
            "traces": [
                {"status": "completed"},
                {"status": "failed", "error": "boom"},
            ],
            "errors": ["boom"],
        }
        score = critic.score_result("objective", result)
        assert score["passed"] is False
        assert any("failed_steps" in f for f in score["flags"])

    def test_clean_result_passes(self, critic):
        result = {
            "status": "completed",
            "traces": [{"status": "completed"}, {"status": "completed"},
                       {"status": "completed"}],
            "errors": [],
        }
        score = critic.score_result("objective", result)
        assert score["passed"] is True

    def test_no_evidence_flagged(self, critic):
        score = critic.score_result("obj", {"status": "completed", "traces": []})
        assert "no_evidence" in score["flags"]


class TestFailureModes:
    def test_detect_timeout(self, critic):
        t = ExecutionTrace(step_index=0, tool="x", error="operation timed out",
                           status=CompletionStatus.FAILED)
        assert "timeout" in critic.detect_failure_modes(t)

    def test_detect_blocked(self, critic):
        t = ExecutionTrace(step_index=0, tool="x", status=CompletionStatus.BLOCKED)
        assert "blocked_by_guardrail" in critic.detect_failure_modes(t)

    def test_detect_operator_denied(self, critic):
        t = ExecutionTrace(step_index=0, tool="x",
                           observation={"error": "Ejecución cancelada por el usuario."},
                           status=CompletionStatus.FAILED)
        modes = critic.detect_failure_modes(t)
        assert "operator_denied" in modes

    def test_recommend_repair_maps_modes(self, critic):
        advice = critic.recommend_repair(["timeout", "operator_denied"])
        assert len(advice) == 2
        assert any("timeout" in a.lower() for a in advice)


def test_critic_has_no_execution_surface(critic):
    # The critic must never expose a way to run tools.
    for attr in ("aexecute", "execute", "run", "invoke", "tool_executor"):
        assert not hasattr(critic, attr)
