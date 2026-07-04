"""
tests/test_task_domain.py — V63 Milestone 2: semantic task-domain classifier.

Proves classify_domain() is deterministic, bilingual, additive (never imports or
mutates route()/ModelRole precedence), and maps each domain to an advisory role.
"""
from __future__ import annotations

import pytest

from core.model_router import ModelRole
from core.task_domain import (
    DomainSignal,
    TaskDomain,
    classify_domain,
    preferred_role_for,
)


@pytest.mark.parametrize(
    "prompt, expected",
    [
        # EN
        ("write a python function to refactor this code", TaskDomain.CODER),
        ("solve this quadratic equation and show the derivative", TaskDomain.MATHEMATICS),
        ("take a screenshot and describe the network diagram", TaskDomain.VISION),
        ("run incident response triage on this compromised host", TaskDomain.DFIR),
        ("design the system architecture for scalability", TaskDomain.ARCHITECT),
        ("translate this text and fix the grammar", TaskDomain.LANGUAGE),
        ("research the latest developments and compare options", TaskDomain.RESEARCH),
        ("audit our compliance with iso 27001 governance policy", TaskDomain.GRC),
        ("build a purple team adversary emulation for lateral movement", TaskDomain.CYBER_PURPLE),
        ("write a sigma rule for detection engineering in the siem", TaskDomain.CYBER_BLUE),
        # ES
        ("escribe una función en python para depurar el código", TaskDomain.CODER),
        ("resuelve la ecuación y calcula la derivada", TaskDomain.MATHEMATICS),
        ("haz una captura de pantalla del diagrama", TaskDomain.VISION),
        ("necesito respuesta a incidentes y triage del incidente", TaskDomain.DFIR),
        ("traducir este texto y corregir la gramática", TaskDomain.LANGUAGE),
    ],
)
def test_classify_domain_expected(prompt, expected):
    sig = classify_domain(prompt)
    assert sig.domain == expected
    assert isinstance(sig, DomainSignal)
    assert 0.0 <= sig.confidence <= 1.0
    assert sig.matched, "a matched-domain result must record which keywords fired"


def test_plain_chat_is_general_low_confidence():
    for prompt in ("hello, how are you today", "what time is it", "tell me a joke"):
        sig = classify_domain(prompt)
        assert sig.domain == TaskDomain.GENERAL
        assert sig.preferred_role == ModelRole.FAST
        assert sig.confidence < 0.5
        assert sig.requires_planning is False
        assert sig.prefers_agent_team is False


def test_preferred_role_mapping():
    assert preferred_role_for(TaskDomain.CODER) == ModelRole.CODER
    assert preferred_role_for(TaskDomain.VISION) == ModelRole.VISION
    assert preferred_role_for(TaskDomain.VERIFIER) == ModelRole.VERIFIER
    assert preferred_role_for(TaskDomain.CRITIC) == ModelRole.VERIFIER
    assert preferred_role_for(TaskDomain.GENERAL) == ModelRole.FAST
    for deep in (
        TaskDomain.RESEARCH, TaskDomain.ARCHITECT, TaskDomain.MATHEMATICS,
        TaskDomain.DFIR, TaskDomain.CYBER_BLUE, TaskDomain.CYBER_PURPLE,
        TaskDomain.GRC, TaskDomain.PLANNER,
    ):
        assert preferred_role_for(deep) == ModelRole.DEEP


def test_classification_is_deterministic():
    prompt = "run incident response triage and write a detection sigma rule"
    first = classify_domain(prompt)
    for _ in range(5):
        assert classify_domain(prompt) == first


def test_tie_break_is_fixed_and_documented():
    # "review this" (CRITIC) and "verify" (VERIFIER) each fire once -> tie.
    # _TIE_BREAK_ORDER places VERIFIER before CRITIC, so VERIFIER wins.
    sig = classify_domain("review this and verify the result")
    assert sig.domain == TaskDomain.VERIFIER


def test_tool_hint_without_keywords():
    # No domain keywords, but a vision tool is in play -> VISION via tool hint.
    sig = classify_domain("do it now", tool_names=["take_screenshot"])
    assert sig.domain == TaskDomain.VISION
    assert any(m.startswith("tool:") for m in sig.matched)


def test_planning_and_agent_flags():
    research = classify_domain("research and investigate the sources thoroughly")
    assert research.domain == TaskDomain.RESEARCH
    assert research.requires_planning is True
    assert research.prefers_agent_team is True

    coder = classify_domain("debug this python traceback")
    assert coder.domain == TaskDomain.CODER
    assert coder.requires_planning is False
    assert coder.prefers_agent_team is False


def test_confidence_scales_with_hits():
    one = classify_domain("translate this")
    assert one.domain == TaskDomain.LANGUAGE
    many = classify_domain("translate and rewrite and rephrase the grammar wording")
    assert many.domain == TaskDomain.LANGUAGE
    assert many.confidence >= one.confidence


def test_empty_and_none_prompt_is_general():
    assert classify_domain("").domain == TaskDomain.GENERAL
    assert classify_domain(None).domain == TaskDomain.GENERAL  # type: ignore[arg-type]


def test_all_domains_have_role_and_are_str_enum():
    for domain in TaskDomain:
        assert isinstance(domain.value, str)
        assert isinstance(preferred_role_for(domain), ModelRole)
