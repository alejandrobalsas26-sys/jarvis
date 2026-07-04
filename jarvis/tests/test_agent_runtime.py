"""
tests/test_agent_runtime.py — V63 Milestone 1: unified per-turn runtime decision.

Proves the composed TaskDecision:
  * keeps model selection / verifier gating byte-identical (decision.model_decision
    == the routing ModelDecision the live path uses),
  * keeps the fast path fast (GENERAL chat: no planning, no agent team, no verify),
  * still escalates on force_deep (FAST -> DEEP + requires_verification),
  * separates semantic domain from the authoritative model role,
  * is actually wired into LLM.chat_stream / LLM._route_turn (single source).
"""
from __future__ import annotations

import inspect

from core.agent_runtime import TaskDecision, assemble_task_decision, route_turn
from core.model_router import ModelRole
from core.response_surface import ResponseSurface
from core.task_domain import TaskDomain


# ── route_turn: the single routing source ────────────────────────────────────

def test_route_turn_matches_router_for_plain_cases():
    assert route_turn("what time is it").role == ModelRole.FAST
    assert route_turn("refactor this python function").role == ModelRole.CODER
    dfir = route_turn("run incident response forensics on this host")
    assert dfir.role == ModelRole.DEEP
    assert dfir.requires_verification is True


def test_route_turn_force_deep_escalates_only_fast():
    base = route_turn("what time is it")
    assert base.role == ModelRole.FAST
    escalated = route_turn("what time is it", force_deep=True)
    assert escalated.role == ModelRole.DEEP
    assert escalated.requires_verification is True
    # Never overrides a role the router chose deliberately.
    coder = route_turn("refactor this python function", force_deep=True)
    assert coder.role == ModelRole.CODER


def test_route_turn_model_decision_parity_with_llm_route_turn():
    """LLM._route_turn now delegates here — the two must return equal decisions."""
    from core.llm import LLM
    for msg, fd in [("hello", False), ("hello", True), ("debug this code", False)]:
        assert route_turn(msg, force_deep=fd) == LLM._route_turn(msg, force_deep=fd)


# ── assemble_task_decision: the composed object ──────────────────────────────

def test_fast_path_stays_fast():
    td = assemble_task_decision("hello, how are you")
    assert isinstance(td, TaskDecision)
    assert td.model_decision.role == ModelRole.FAST
    assert td.domain == TaskDomain.GENERAL
    assert td.response_surface == ResponseSurface.TEXT
    assert td.requires_planning is False
    assert td.prefers_agent_team is False
    assert td.requires_verification is False


def test_force_deep_escalates_through_composed_layer():
    td = assemble_task_decision("hello", force_deep=True)
    assert td.model_decision.role == ModelRole.DEEP
    assert td.requires_verification is True
    assert td.requires_planning is True  # force_deep implies planning
    # And model_decision is exactly what route_turn produced.
    assert td.model_decision == route_turn("hello", force_deep=True)


def test_coding_domain_and_role_align():
    td = assemble_task_decision("refactor this python function and fix the bug")
    assert td.domain == TaskDomain.CODER
    assert td.model_decision.role == ModelRole.CODER
    assert td.preferred_model_role == ModelRole.CODER


def test_domain_is_independent_of_model_role():
    """RESEARCH domain prefers DEEP even when route() picks a lighter role —
    domain and model role are separate dimensions."""
    td = assemble_task_decision("research and investigate the sources thoroughly")
    assert td.domain == TaskDomain.RESEARCH
    assert td.preferred_model_role == ModelRole.DEEP
    assert td.requires_planning is True
    assert td.prefers_agent_team is True


def test_security_sensitive_forces_verification():
    td = assemble_task_decision("write an exploit payload with a c2 beacon")
    assert td.security_sensitive is True
    assert td.requires_verification is True


def test_requires_tools_prediction():
    assert assemble_task_decision("hello").requires_tools is False
    assert assemble_task_decision("what's up", tool_names=["run_shell_command"]).requires_tools is True


def test_telemetry_shape():
    td = assemble_task_decision("refactor this python code")
    tel = td.telemetry()
    assert tel["domain"] == "coder"
    assert tel["response_surface"] == "text"
    assert set(tel) >= {
        "domain", "domain_confidence", "response_surface",
        "requires_planning", "prefers_agent_team", "requires_tools",
        "preferred_model_role",
    }


def test_accessors():
    td = assemble_task_decision("debug this traceback")
    assert td.role == td.model_decision.role
    assert td.model == td.model_decision.model
    assert td.provider == td.model_decision.provider


# ── Live wiring (source characterization, matching repo convention) ──────────

def test_llm_route_turn_delegates_to_single_source():
    from core.llm import LLM
    src = inspect.getsource(LLM._route_turn)
    assert "route_turn" in src, "_route_turn must delegate to agent_runtime.route_turn"


def test_chat_stream_consults_composed_decision():
    from core.llm import LLM
    src = inspect.getsource(LLM.chat_stream)
    assert "assemble_task_decision" in src
    assert "task_decision.model_decision" in src
    assert "telemetry()" in src, "domain/surface telemetry must reach the AURA model_decision event"
