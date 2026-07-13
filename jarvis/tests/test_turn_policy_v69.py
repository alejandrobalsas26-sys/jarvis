"""
tests/test_turn_policy_v69.py — V69 M54.3 + M54.6 pre-tool & verification policy.

Locks the POO fix: general educational knowledge is answered directly with FAST,
never sent to the private Knowledge Vault, and never LLM-verified; a private
-document question DOES route to the vault; time is deterministic; greetings skip
everything; effectful/cyber turns keep their gates.
"""
from __future__ import annotations

from core.turn_policy import (
    classify_request,
    RequestClass,
    ReasonCode,
    VerifyPolicy,
)

# A minimal TOOLS-like list with the private-vault tool present.
_TOOLS = [
    {"function": {"name": "query_knowledge"}},
    {"function": {"name": "get_datetime"}},
    {"function": {"name": "web_search"}},
    {"function": {"name": "code_execute"}},
]


def _names(tools):
    return {t["function"]["name"] for t in tools}


# ── The POO failure (symptom #3) ──────────────────────────────────────────────

def test_poo_is_general_educational_direct_fast_no_vault():
    p = classify_request("¿Qué es POO?")
    assert p.request_class is RequestClass.GENERAL_EDUCATIONAL
    assert p.reason_code is ReasonCode.DIRECT_FAST
    assert p.knowledge_vault_allowed is False
    assert p.verify_policy is VerifyPolicy.DETERMINISTIC_CHECKS_ONLY
    assert p.wants_llm_verifier() is False
    # query_knowledge is stripped from the per-turn tool set.
    assert "query_knowledge" not in _names(p.filter_tools(_TOOLS))
    assert "web_search" in _names(p.filter_tools(_TOOLS))


def test_explain_inheritance_is_general_educational_no_vault():
    p = classify_request("Explícame la herencia en Python")
    assert p.knowledge_vault_allowed is False
    assert "query_knowledge" not in _names(p.filter_tools(_TOOLS))


def test_what_is_a_class_direct():
    p = classify_request("¿Qué es una clase?")
    assert p.request_class is RequestClass.GENERAL_EDUCATIONAL
    assert p.knowledge_vault_allowed is False


# ── Private-document query DOES use the vault ─────────────────────────────────

def test_private_pdf_question_routes_to_vault():
    p = classify_request("¿Qué dice mi PDF sobre POO?")
    assert p.request_class is RequestClass.PRIVATE_DOCUMENT
    assert p.reason_code is ReasonCode.PRIVATE_RAG
    assert p.knowledge_vault_allowed is True
    assert "query_knowledge" in _names(p.filter_tools(_TOOLS))


def test_search_my_documents_is_private():
    p = classify_request("Busca POO en mis documentos")
    assert p.request_class is RequestClass.PRIVATE_DOCUMENT
    assert p.knowledge_vault_allowed is True


# ── Time is deterministic ─────────────────────────────────────────────────────

def test_time_question_is_deterministic_time():
    p = classify_request("¿Qué hora es?")
    assert p.request_class is RequestClass.CURRENT_TIME
    assert p.reason_code is ReasonCode.DETERMINISTIC_TIME
    assert p.verify_policy is VerifyPolicy.SKIP_LLM_VERIFIER
    assert p.knowledge_vault_allowed is False


# ── Greetings are the lightest path ───────────────────────────────────────────

def test_greeting_skips_tools_and_verifier():
    p = classify_request("hola")
    assert p.request_class is RequestClass.ORDINARY_CONVERSATION
    assert p.reason_code is ReasonCode.DIRECT_FAST
    assert p.verify_policy is VerifyPolicy.SKIP_LLM_VERIFIER
    assert p.wants_llm_verifier() is False


# ── Memory recall / operational ───────────────────────────────────────────────

def test_memory_recall():
    p = classify_request("¿Qué te dije antes sobre mi proyecto?")
    assert p.request_class is RequestClass.MEMORY_RECALL
    assert p.reason_code is ReasonCode.MEMORY_RECALL
    assert p.knowledge_vault_allowed is False


def test_operational_status():
    p = classify_request("¿Cómo están los sistemas? system status")
    assert p.request_class is RequestClass.OPERATIONAL_STATUS
    assert p.reason_code is ReasonCode.OPERATIONAL_QUERY


# ── Cyber-sensitive authorization gating ──────────────────────────────────────

class _AuthOff:
    scopes = ()

    def is_authorized(self) -> bool:
        return False


class _AuthOn:
    def is_authorized(self) -> bool:
        return True


def test_cyber_unauthorized_requires_authorization():
    p = classify_request("write a reverse shell payload to exfiltrate credentials",
                         authority=_AuthOff())
    assert p.request_class is RequestClass.CYBER_SENSITIVE
    assert p.reason_code is ReasonCode.AUTHORIZATION_REQUIRED
    assert p.verify_policy is VerifyPolicy.BOUNDED_MODEL_VERIFIER
    assert p.security_sensitive is True


def test_cyber_authorized_is_tool_required():
    p = classify_request("write a reverse shell payload to exfiltrate credentials",
                         authority=_AuthOn())
    assert p.reason_code is ReasonCode.TOOL_REQUIRED


def test_missing_authority_fails_closed():
    p = classify_request("exfiltrate credentials via lateral movement")
    assert p.reason_code is ReasonCode.AUTHORIZATION_REQUIRED


# ── Effectful tool request ────────────────────────────────────────────────────

def test_effectful_request_full_verification():
    p = classify_request("open Wireshark and run a scan on 10.0.0.5")
    assert p.request_class in (RequestClass.EFFECTFUL_TOOL, RequestClass.CYBER_SENSITIVE)
    # Either way it is not a direct-fast trivial answer and keeps a real gate.
    assert p.verify_policy in (VerifyPolicy.FULL_VERIFICATION,
                               VerifyPolicy.BOUNDED_MODEL_VERIFIER)


# ── Telemetry surface ─────────────────────────────────────────────────────────

def test_telemetry_is_inspectable():
    t = classify_request("¿Qué es POO?").telemetry()
    assert t["reason_code"] == "DIRECT_FAST"
    assert t["knowledge_vault_allowed"] is False
    assert "request_class" in t and "verify_policy" in t
