"""
tests/test_specialist_runtime.py — V63 Milestone 4 coverage.

Proves the controlled multi-agent runtime:
  * team selector chooses appropriate specialists from a TaskDecision;
  * simple chat does NOT invoke a team;
  * max concurrency is respected (fast ≤ 2, deep ≤ 1 by default);
  * blackboard is bounded and dedups evidence;
  * provenance survives on evidence/reports;
  * conflicting agent verdicts are detected structurally;
  * critic receives a bounded digest; verifier runs when required;
  * specialists cannot bypass the ToolExecutor (ToolBroker delegates only,
    and refuses categories outside the spec allowlist);
  * legacy cyber specialists are preserved and runnable;
  * resource pressure reduces concurrency.
"""
from __future__ import annotations

import asyncio

from core.agent_runtime import assemble_task_decision
from core.specialist_runtime import (
    AgentReport,
    AgentTeamSelector,
    EvidenceItem,
    SharedBlackboard,
    SpecialistRole,
    SpecialistTeamRuntime,
    TeamExecutionPolicy,
    ToolBroker,
    legacy_spec,
    spec_for,
    _team_confidence,
)


# ── Fakes ─────────────────────────────────────────────────────────────────────
class _ConcurrencyTracker:
    """A fake inference fn that records peak concurrency per tier."""

    def __init__(self, delay: float = 0.02, canned: dict | None = None) -> None:
        self.delay = delay
        self.canned = canned or {}
        self.active = {"fast": 0, "deep": 0}
        self.peak = {"fast": 0, "deep": 0}

    async def __call__(self, system, user, *, tier, timeout_s, num_ctx, temperature):
        bucket = "deep" if tier.value in ("deep", "synthesis") else "fast"
        self.active[bucket] += 1
        self.peak[bucket] = max(self.peak[bucket], self.active[bucket])
        try:
            await asyncio.sleep(self.delay)
            # Return a canned answer keyed by a substring of the system prompt.
            for key, val in self.canned.items():
                if key in system or key in user:
                    return val
            return "- finding one\n- finding two"
        finally:
            self.active[bucket] -= 1


# ── Selector ──────────────────────────────────────────────────────────────────
def test_selector_no_team_for_simple_chat():
    td = assemble_task_decision("hello, how are you")
    sel = AgentTeamSelector()
    assert sel.should_form_team(td) is False
    assert sel.select(td) == []


def test_selector_forms_team_for_research():
    td = assemble_task_decision("research and investigate the sources thoroughly")
    roles = AgentTeamSelector().select(td)
    assert SpecialistRole.RESEARCH in roles
    # RESEARCH is a planning domain → the CRITIC joins the fan-in.
    assert SpecialistRole.CRITIC in roles


def test_selector_security_turn_adds_verifier():
    td = assemble_task_decision("analyze this exploit payload and c2 beacon")
    roles = AgentTeamSelector().select(td)
    assert SpecialistRole.VERIFIER in roles


def test_selector_respects_total_cap():
    policy = TeamExecutionPolicy(max_total_agents=2)
    td = assemble_task_decision("architect a complex distributed system design tradeoff")
    roles = AgentTeamSelector(policy).select(td)
    assert len(roles) <= 2


# ── Blackboard: bounded, dedup, provenance, conflict ──────────────────────────
def test_blackboard_dedups_evidence_and_keeps_provenance():
    bb = SharedBlackboard("objective")
    e1 = EvidenceItem(content="port 445 open", source="nmap", confidence=0.9, agent="A")
    e2 = EvidenceItem(content="port 445 open", source="nmap", confidence=0.9, agent="B")  # dup
    assert bb.add_evidence(e1) is True
    assert bb.add_evidence(e2) is False
    assert len(bb.evidence) == 1
    assert bb.evidence[0].source == "nmap"
    assert bb.evidence[0].agent == "A"


def test_blackboard_is_size_bounded():
    bb = SharedBlackboard("obj")
    for i in range(500):
        bb.add_fact(f"fact number {i}")
    from core.specialist_runtime import _BB_MAX_FACTS
    assert len(bb.facts) <= _BB_MAX_FACTS


def test_blackboard_detects_conflicting_verdicts():
    bb = SharedBlackboard("is host X compromised?")
    bb.add_report(AgentReport("A", SpecialistRole.DFIR, "t0",
                              summary="looks compromised", verdict="compromised", confidence=0.8))
    bb.add_report(AgentReport("B", SpecialistRole.CYBER_BLUE, "t1",
                              summary="looks clean", verdict="clean", confidence=0.7))
    assert len(bb.conflicts) == 1
    c = bb.conflicts[0]
    assert {c.verdict_a, c.verdict_b} == {"compromised", "clean"}


def test_blackboard_no_false_conflict_for_agreeing_verdicts():
    bb = SharedBlackboard("obj")
    bb.add_report(AgentReport("A", SpecialistRole.DFIR, "t0", verdict="malicious"))
    bb.add_report(AgentReport("B", SpecialistRole.DFIR, "t1", verdict="malicious"))
    assert bb.conflicts == []


def test_context_digest_is_bounded():
    bb = SharedBlackboard("obj")
    for i in range(100):
        bb.add_fact(f"a moderately long fact string number {i} " * 3)
    digest = bb.context_digest(budget=1000)
    assert len(digest) <= 1000


# ── Concurrency limits ────────────────────────────────────────────────────────
def test_deep_concurrency_is_one_by_default():
    infer = _ConcurrencyTracker(delay=0.03)
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    # 3 deep roles — must never run more than max_deep_agents (1) at once.
    asyncio.run(rt.run_team(
        "obj", [SpecialistRole.DFIR, SpecialistRole.RESEARCH, SpecialistRole.ARCHITECT]))
    assert infer.peak["deep"] <= 1


def test_fast_concurrency_capped_at_two():
    infer = _ConcurrencyTracker(delay=0.03)
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    # 4 fast roles — must never exceed max_fast_agents (2).
    roles = [SpecialistRole.GENERAL, SpecialistRole.LANGUAGE,
             SpecialistRole.CRITIC, SpecialistRole.VERIFIER]
    asyncio.run(rt.run_team("obj", roles))
    assert infer.peak["fast"] <= 2


def test_resource_pressure_reduces_concurrency():
    infer = _ConcurrencyTracker(delay=0.03)
    # Report pressure (high CPU) — fast pool must collapse to 1.
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (99.0, 50.0, False))
    roles = [SpecialistRole.GENERAL, SpecialistRole.LANGUAGE, SpecialistRole.CRITIC]
    asyncio.run(rt.run_team("obj", roles))
    assert infer.peak["fast"] <= 1


def test_policy_under_pressure_only_reduces():
    p = TeamExecutionPolicy()
    calm = p.under_pressure(10.0, 10.0, False)
    assert calm.max_fast_agents == p.max_fast_agents
    stressed = p.under_pressure(99.0, 10.0, False)
    assert stressed.max_fast_agents == 1
    assert stressed.max_total_agents <= p.max_total_agents
    # Unreadable metrics are treated as pressure (fail-safe).
    unknown = p.under_pressure("x", None, False)  # type: ignore[arg-type]
    assert unknown.max_fast_agents == 1


# ── End-to-end team run ───────────────────────────────────────────────────────
def test_run_team_produces_synthesis_with_reports_and_confidence():
    infer = _ConcurrencyTracker(canned={})
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    res = asyncio.run(rt.run_team(
        "map the attack surface", [SpecialistRole.RESEARCH, SpecialistRole.CRITIC]))
    assert res.summary
    assert len(res.reports) == 2
    assert 0.0 < res.confidence <= 1.0
    assert set(res.agents) == {"research", "critic"}


def test_run_team_for_decision_returns_none_for_simple_chat():
    infer = _ConcurrencyTracker()
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    td = assemble_task_decision("what time is it")
    assert asyncio.run(rt.run_team_for_decision(td, "what time is it")) is None


def test_run_team_for_decision_runs_when_warranted():
    infer = _ConcurrencyTracker()
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    td = assemble_task_decision("do a DFIR forensic root-cause investigation of this incident")
    res = asyncio.run(rt.run_team_for_decision(td, "investigate incident"))
    assert res is not None
    assert res.reports


def test_verifier_runs_when_required():
    infer = _ConcurrencyTracker(canned={"verifier": "YES supported"})
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    res = asyncio.run(rt.run_team("obj", [SpecialistRole.RESEARCH], verify=True))
    assert res.verified is True


def test_legacy_agents_preserved_and_runnable():
    # The 5 legacy names must still resolve and run.
    for name in ("MalwareAnalyst", "NetworkRecon", "ThreatIntelligence",
                 "IncidentResponder", "CodeAnalyst"):
        assert legacy_spec(name) is not None
    infer = _ConcurrencyTracker()
    rt = SpecialistTeamRuntime(infer=infer, resource_probe=lambda: (10.0, 10.0, False))
    res = asyncio.run(rt.run_legacy_agents(
        "Analyze current incident", ["ThreatIntelligence", "IncidentResponder"], {}
    ))
    legacy = res.to_legacy_dict()
    assert set(legacy.keys()) >= {"task", "agents", "results", "synthesis", "elapsed_s", "timestamp"}
    assert "ThreatIntelligence" in legacy["results"]


# ── Tool gateway: no bypass ───────────────────────────────────────────────────
class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def aexecute(self, tool_name, tool_input, reasoning=""):
        self.calls.append((tool_name, tool_input, reasoning))
        return {"ok": True, "tool": tool_name}


def test_toolbroker_delegates_to_executor_only():
    ex = _RecordingExecutor()
    broker = ToolBroker(ex, spec_for(SpecialistRole.RESEARCH))  # allows READ, WEB
    out = asyncio.run(broker.call("fetch_webpage", {"url": "http://x"}, "gather"))
    assert out["ok"] is True
    assert ex.calls and ex.calls[0][0] == "fetch_webpage"
    # reasoning is tagged with the role — audit trail preserved
    assert "research" in ex.calls[0][2]


def test_toolbroker_refuses_disallowed_category():
    ex = _RecordingExecutor()
    # RESEARCH is not allowed CODE tools.
    broker = ToolBroker(ex, spec_for(SpecialistRole.RESEARCH))
    out = asyncio.run(broker.call("code_execute", {"code": "print(1)"}, "run"))
    assert "error" in out
    assert ex.calls == []  # never reached the executor — no bypass


def test_toolbroker_refuses_uncategorized_tool():
    ex = _RecordingExecutor()
    broker = ToolBroker(ex, spec_for(SpecialistRole.OPERATIONAL))
    out = asyncio.run(broker.call("run_shell_command", {"command": "ls"}, "x"))
    assert "error" in out
    assert ex.calls == []


def test_toolbroker_fails_closed_without_executor():
    broker = ToolBroker(None, spec_for(SpecialistRole.RESEARCH))
    out = asyncio.run(broker.call("fetch_webpage", {"url": "http://x"}))
    assert "error" in out


# ── Confidence math ───────────────────────────────────────────────────────────
def test_conflicts_lower_confidence():
    reports = [AgentReport("A", SpecialistRole.DFIR, "t", confidence=0.8),
               AgentReport("B", SpecialistRole.DFIR, "t", confidence=0.8)]
    from core.specialist_runtime import Conflict
    no_conf = _team_confidence(reports, [], None)
    with_conf = _team_confidence(
        reports, [Conflict("t", "a", "A", "b", "B")], None
    )
    assert with_conf < no_conf


def test_failed_verification_halves_confidence():
    reports = [AgentReport("A", SpecialistRole.DFIR, "t", confidence=0.8)]
    assert _team_confidence(reports, [], False) < _team_confidence(reports, [], None)
