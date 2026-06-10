"""
tests/test_cognitive_engine.py — V58.0 COGNITIVE CORE planner/executor/critic.

Verifies the engine respects max_steps/max_retries, routes ALL tool execution
through ToolExecutor.aexecute() (never bypasses guardrails), and fails closed on
destructive/ambiguous actions. Uses a fake async executor — no network/hardware.
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest
from core.cognitive_engine import CognitiveEngine
from core.cognitive_types import RiskLevel, CompletionStatus, PlanStep, CognitivePlan


class FakeExecutor:
    """Records every aexecute() call; the ONLY sanctioned execution path."""
    def __init__(self, result=None, raises=False):
        self.calls = []
        self.result = result if result is not None else {"status": "ok"}
        self.raises = raises

    async def aexecute(self, tool_name, tool_input, reasoning=""):
        self.calls.append((tool_name, tool_input, reasoning))
        if self.raises:
            raise RuntimeError("tool blew up")
        return self.result


class TestPlanning:
    def test_plan_is_bounded(self):
        eng = CognitiveEngine(max_steps=3)
        plan = eng.create_plan("scan and isolate and capture and research the host")
        assert len(plan.plan_steps) <= 3

    def test_infers_tools_from_objective(self):
        eng = CognitiveEngine()
        plan = eng.create_plan("scan the target network for open ports")
        assert "network_scan" in plan.required_tools

    def test_plan_has_assess_and_verify(self):
        eng = CognitiveEngine(max_steps=8)
        plan = eng.create_plan("search threat intel")
        actions = [s.action for s in plan.plan_steps]
        assert any(a.startswith("assess") for a in actions)
        assert any(a.startswith("verify") for a in actions)


class TestExecutionRoutesThroughExecutor:
    def test_tool_step_uses_aexecute(self):
        fake = FakeExecutor()
        eng = CognitiveEngine(tool_executor=fake)
        plan = CognitivePlan(objective="x")
        step = PlanStep(index=0, action="execute web_search", tool="web_search")
        trace = asyncio.run(eng.execute_step(plan, step))
        assert len(fake.calls) == 1
        assert fake.calls[0][0] == "web_search"
        assert trace.status == CompletionStatus.COMPLETED

    def test_no_executor_fails_closed(self):
        eng = CognitiveEngine(tool_executor=None)
        plan = CognitivePlan(objective="x")
        step = PlanStep(index=0, action="execute web_search", tool="web_search")
        trace = asyncio.run(eng.execute_step(plan, step))
        assert trace.status == CompletionStatus.BLOCKED
        assert "no guarded" in (trace.error or "")

    def test_destructive_step_blocked_without_executor_call(self):
        fake = FakeExecutor()
        eng = CognitiveEngine(tool_executor=fake)
        plan = CognitivePlan(objective="x")
        step = PlanStep(index=0, action="run rm -rf / now", tool="run_shell_command")
        trace = asyncio.run(eng.execute_step(plan, step))
        assert trace.status == CompletionStatus.BLOCKED
        assert fake.calls == []  # destructive action never reached the executor

    def test_reasoning_step_needs_no_executor(self):
        eng = CognitiveEngine(tool_executor=None)
        plan = CognitivePlan(objective="x")
        step = PlanStep(index=0, action="assess objective")
        trace = asyncio.run(eng.execute_step(plan, step))
        assert trace.status == CompletionStatus.COMPLETED


class TestRunTaskBounds:
    def test_respects_max_steps(self):
        fake = FakeExecutor()
        eng = CognitiveEngine(tool_executor=fake, max_steps=2)
        result = asyncio.run(eng.run_task("scan and isolate the host"))
        assert result["steps_run"] <= 2

    def test_respects_max_retries(self):
        fake = FakeExecutor(raises=True)  # always fails -> retryable
        eng = CognitiveEngine(tool_executor=fake, max_steps=8, max_retries=2)
        result = asyncio.run(eng.run_task("search threat intel"))
        assert result["retries"] <= 2

    def test_run_task_returns_audit_dict(self):
        fake = FakeExecutor()
        eng = CognitiveEngine(tool_executor=fake)
        result = asyncio.run(eng.run_task("search threat intel"))
        for key in ("task_id", "status", "plan", "traces", "reflection",
                    "result_score"):
            assert key in result

    def test_blocked_destructive_objective_fails_closed(self):
        fake = FakeExecutor()
        eng = CognitiveEngine(tool_executor=fake)
        # objective with destructive verb maps to a CRITICAL-risk shell-ish plan
        result = asyncio.run(eng.run_task("shutdown and format the workstation"))
        # nothing destructive should have been dispatched to the executor
        assert all("format" not in c[0] for c in fake.calls)

    def test_memory_recorded_when_wired(self):
        class FakeMem:
            def __init__(self):
                self.records = []
            def record_task(self, plan, traces, reflection):
                self.records.append((plan, traces, reflection))
        mem = FakeMem()
        eng = CognitiveEngine(tool_executor=FakeExecutor(), memory=mem)
        asyncio.run(eng.run_task("search threat intel"))
        assert len(mem.records) == 1
