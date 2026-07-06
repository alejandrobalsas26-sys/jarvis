"""
tests/test_agent_planner.py — V63 M3 bridge coverage.

Proves the TaskDecision → bounded-graph bridge:
  * simple chat is NOT planned (fast path preserved);
  * planning-worthy turns build a small bounded graph and run it;
  * a team-preferring turn produces an AGENT node that delegates to the runtime;
  * verification-required turns append a VERIFY node.
"""
from __future__ import annotations

import asyncio

from core.agent_planner import AgentPlanner, build_graph_for_objective, should_plan
from core.agent_runtime import assemble_task_decision
from core.task_graph import NodeType


def test_simple_chat_is_not_planned():
    assert should_plan(assemble_task_decision("what time is it")) is False
    assert should_plan(assemble_task_decision("hello there")) is False


def test_planning_domain_is_planned():
    td = assemble_task_decision("do a DFIR forensic root-cause investigation of this incident")
    assert should_plan(td) is True


def test_explicit_request_is_planned_even_if_simple():
    assert should_plan(assemble_task_decision("hi"), explicit=True) is True


def test_graph_shape_reasoning_only_turn():
    td = assemble_task_decision("architect a large-scale system design tradeoff analysis")
    g = build_graph_for_objective("design it", td)
    assert "analyze" in g.nodes and "synthesize" in g.nodes
    # synthesize depends on analyze
    assert "analyze" in g.nodes["synthesize"].depends_on


def test_team_turn_produces_agent_node():
    td = assemble_task_decision("research and investigate the sources and compare options")
    g = build_graph_for_objective("research it", td)
    # RESEARCH prefers a team → analyze is an AGENT node
    assert g.nodes["analyze"].type == NodeType.AGENT


def test_verification_turn_appends_verify_node():
    td = assemble_task_decision("analyze this exploit payload and c2 beacon for the lab")
    g = build_graph_for_objective("analyze it", td)
    assert "verify" in g.nodes
    assert "synthesize" in g.nodes["verify"].depends_on


def test_plan_and_run_executes_against_fakes():
    class _FakeTeam:
        async def run_team_for_decision(self, td, objective, context=None):
            class _R:
                summary = "team result"

                def to_dict(self):
                    return {"summary": "team result"}
            return _R()

    async def _infer(system, user):
        return "reasoned output"

    planner = AgentPlanner(infer=_infer, team_runtime=_FakeTeam())
    td = assemble_task_decision("research and investigate the sources thoroughly")
    res = asyncio.run(planner.plan_and_run("investigate", td))
    assert res.status in ("completed", "partial")
    assert "analyze" in res.completed
    assert "synthesize" in res.completed
