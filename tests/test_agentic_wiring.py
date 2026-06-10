"""
tests/test_agentic_wiring.py — V58.1 cognitive engine activation in incident flow.

Verifies run_agentic_incident emits a cognitive plan event when a CognitiveEngine
is supplied, and falls back cleanly (no plan event, no error) when it is None.
Uses fakes for the LLM/broadcast — no network/admin/hardware.
"""
import sys
import types
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

# Test isolation: stub the heavy episodic-memory module (pulls in ML deps) so the
# agentic loop's fire-and-forget store_episode import resolves instantly. The loop
# imports it lazily inside a try/except, so a lightweight stub is transparent.
if "core.episodic_memory" not in sys.modules:
    _stub = types.ModuleType("core.episodic_memory")
    async def _noop_store_episode(*a, **k):  # noqa: ANN001
        return None
    _stub.store_episode = _noop_store_episode
    sys.modules["core.episodic_memory"] = _stub

import pytest
from core.agentic_loop import run_agentic_incident
from core.cognitive_engine import CognitiveEngine


class FakeLLM:
    """Resolves immediately so the ReAct loop exits on cycle 0."""
    async def decide_next_action(self, context):
        return {"tool": "RESOLVED", "reasoning": "no action required"}


class FakeExecutor:
    async def aexecute(self, tool_name, tool_input, reasoning=""):
        return {"status": "ok"}


def _run(cognitive_engine):
    events = []

    async def broadcast(ev):
        events.append(ev)

    trigger = {"type": "canary_intrusion", "attacker_ip": "10.0.0.9", "port": 22}
    asyncio.run(run_agentic_incident(
        trigger_event=trigger,
        tool_executor=FakeExecutor(),
        broadcast_fn=broadcast,
        llm_client=FakeLLM(),
        cognitive_engine=cognitive_engine,
    ))
    return events


def _types(events):
    return [e.get("type") for e in events if isinstance(e, dict)]


class TestActivation:
    def test_plan_event_emitted_when_engine_provided(self):
        events = _run(CognitiveEngine(tool_executor=FakeExecutor()))
        types = _types(events)
        assert "agentic_plan" in types
        plan_ev = next(e for e in events if e.get("type") == "agentic_plan")
        assert plan_ev.get("task_id")
        assert isinstance(plan_ev.get("steps"), list)

    def test_fallback_no_engine_no_plan_event(self):
        events = _run(None)
        types = _types(events)
        assert "agentic_plan" not in types
        # loop still ran and reached resolution/summary
        assert "agentic_loop_start" in types

    def test_no_crash_either_path(self):
        # both paths complete without raising
        assert _run(None) is not None
        assert _run(CognitiveEngine(tool_executor=FakeExecutor())) is not None
