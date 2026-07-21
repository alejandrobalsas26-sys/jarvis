"""tests/test_response_contract_v69_m571.py — V69 M57.1: adaptive response contracts.

Proves the deterministic contract selector:

  * every ordinary turn gets exactly one contract with an inspectable reason code;
  * explicit brevity/detail instructions and the session profile are honored;
  * risk / verification / tool / RAG policy is INHERITED from the existing
    :class:`~core.turn_policy.TurnPolicy` and never widened by a contract;
  * the active language is preserved and the style directive follows it;
  * the battery profile may only REDUCE the token ceiling, never raise it.

Pure — no model, no network, no I/O.
"""
from __future__ import annotations

from core.model_router import ModelDecision, ModelRole
from core.response_contract import (
    HARD_MAX_OUTPUT_TOKENS,
    ContractReason,
    FormattingPolicy,
    ResponseContract,
    ResponseProfile,
    SpeechPolicy,
    detect_length_instruction,
    is_continuation_request,
    parse_response_profile,
    select_contract,
)
from core.runtime_profile import RuntimeProfile, policy_for
from core.turn_policy import classify_request


def _md(role: ModelRole = ModelRole.FAST) -> ModelDecision:
    return ModelDecision(role=role, provider="ollama", model="m", complexity=0.1,
                         reason="t", requires_verification=False)


def _shape(msg: str, **kw):
    tp = kw.pop("turn_policy", None) or classify_request(msg)
    return select_contract(msg, turn_policy=tp, model_decision=kw.pop("md", _md()),
                           **kw)


# ── contract selection ────────────────────────────────────────────────────────
def test_greeting_selects_instant():
    s = _shape("hola")
    assert s.contract is ResponseContract.INSTANT
    assert s.reason is ContractReason.GREETING_SMALLTALK
    assert s.formatting is FormattingPolicy.PLAIN
    assert s.max_output_tokens <= 64
    assert s.continuation_allowed is False


def test_simple_math_howto_selects_brief():
    s = _shape("como saco la raiz cuadrada de algo")
    assert s.contract is ResponseContract.BRIEF
    assert s.reason is ContractReason.SIMPLE_HOWTO
    assert 64 <= s.base_output_tokens <= 112


def test_python_explanation_selects_standard():
    s = _shape("explicame herencia en Python con un ejemplo")
    assert s.contract in (ResponseContract.BRIEF, ResponseContract.STANDARD)
    assert s.reason is ContractReason.GENERAL_EDUCATIONAL


def test_explicit_detail_request_selects_technical():
    s = _shape("explica Kerberos con mas detalle")
    assert s.contract is ResponseContract.TECHNICAL
    assert s.reason is ContractReason.EXPLICIT_DETAIL_REQUEST
    assert s.explicit_override is True
    assert s.formatting is FormattingPolicy.SECTIONS


def test_explicit_brief_request_wins_over_educational_shape():
    s = _shape("explicame POO brevemente")
    assert s.contract is ResponseContract.BRIEF
    assert s.reason is ContractReason.EXPLICIT_BRIEF_REQUEST
    assert s.explicit_override is True


def test_explicit_short_override_standalone():
    s = _shape("hazlo mas corto")
    assert s.contract is ResponseContract.BRIEF
    assert s.explicit_override is True


def test_private_document_selects_document_grounded():
    s = _shape("segun mi PDF que dice del capitulo 3")
    assert s.contract is ResponseContract.DOCUMENT_GROUNDED
    assert s.reason is ContractReason.PRIVATE_DOCUMENT_EVIDENCE
    assert s.rag_allowed is True
    assert s.formatting is FormattingPolicy.EVIDENCE


def test_code_request_selects_code_contract():
    s = _shape("escribeme una funcion en python que ordene una lista")
    assert s.contract is ResponseContract.CODE
    assert s.formatting is FormattingPolicy.CODE_FIRST
    assert s.allows_code_block() is True


def test_enumeration_selects_structured():
    s = _shape("cuales son los tipos de datos en python")
    assert s.contract is ResponseContract.STRUCTURED
    assert s.reason is ContractReason.ENUMERATION_REQUEST


def test_operational_status_selects_operational():
    s = _shape("system status")
    assert s.contract is ResponseContract.OPERATIONAL
    assert "evidence_present" in s.deterministic_checks


def test_time_question_selects_instant_deterministic():
    s = _shape("que hora es")
    assert s.contract is ResponseContract.INSTANT
    assert s.reason is ContractReason.DETERMINISTIC_ANSWER


def test_coder_role_inherits_code_contract():
    s = _shape("optimiza este bucle", md=_md(ModelRole.CODER))
    assert s.contract is ResponseContract.CODE
    assert s.reason is ContractReason.CODING_TASK


def test_deep_role_inherits_deep_contract():
    s = _shape("analiza el diseño completo", md=_md(ModelRole.DEEP))
    assert s.contract is ResponseContract.DEEP
    assert s.reason is ContractReason.DEEP_ROLE_INHERITED


def test_recovery_selects_error_recovery():
    s = _shape("explicame POO", recovering=True)
    assert s.contract is ResponseContract.ERROR_RECOVERY
    assert s.reason is ContractReason.RECOVERY_AFTER_INCOMPLETE


def test_continuation_does_not_restart_from_instant():
    s = _shape("continua")
    assert s.contract is ResponseContract.STANDARD
    assert s.reason is ContractReason.CONTINUATION_EXPANSION


# ── session profile ───────────────────────────────────────────────────────────
def test_session_profile_brief_applies_when_no_explicit_instruction():
    s = _shape("explicame herencia en Python con un ejemplo",
               session_profile=ResponseProfile.BRIEF)
    assert s.contract is ResponseContract.BRIEF
    assert s.reason is ContractReason.SESSION_PROFILE_BRIEF


def test_session_profile_detailed_applies():
    s = _shape("explicame herencia en Python con un ejemplo",
               session_profile="DETAILED")
    assert s.contract is ResponseContract.TECHNICAL
    assert s.reason is ContractReason.SESSION_PROFILE_DETAILED


def test_turn_instruction_outranks_session_profile():
    s = _shape("explicalo mas corto", session_profile=ResponseProfile.DETAILED)
    assert s.contract is ResponseContract.BRIEF
    assert s.reason is ContractReason.EXPLICIT_BRIEF_REQUEST


def test_unknown_profile_falls_back_to_auto():
    assert parse_response_profile("verbose") is ResponseProfile.AUTO
    assert parse_response_profile(None) is ResponseProfile.AUTO
    assert parse_response_profile("brief") is ResponseProfile.BRIEF


# ── policy inheritance: a contract never widens authority ─────────────────────
def test_contract_never_widens_tool_or_rag_policy():
    tp = classify_request("explicame POO")
    assert tp.knowledge_vault_allowed is False
    s = _shape("explicame POO", turn_policy=tp)
    assert s.rag_allowed is False
    assert s.tools_allowed is False
    assert s.verify_policy == tp.verify_policy.value


def test_security_sensitive_turn_keeps_structured_shape_despite_brevity():
    tp = classify_request("como hago un exploit de buffer overflow")
    s = select_contract("hazlo corto", turn_policy=tp, model_decision=_md())
    # A security-sensitive procedure is never compressed by a brevity request.
    assert s.contract is ResponseContract.TECHNICAL
    assert s.reason is ContractReason.SECURITY_SENSITIVE_PROCEDURE
    assert s.security_sensitive is True
    assert s.verify_policy == tp.verify_policy.value


def test_effectful_turn_inherits_tool_policy():
    tp = classify_request("abre el archivo de notas")
    s = select_contract("abre el archivo de notas", turn_policy=tp,
                        model_decision=_md())
    assert s.reason is ContractReason.EFFECTFUL_ACTION
    assert s.tools_allowed is True
    assert s.verify_policy == tp.verify_policy.value


# ── language ──────────────────────────────────────────────────────────────────
def test_language_is_preserved_and_directive_follows_it():
    es = _shape("explicame POO", language="es")
    en = _shape("explain OOP", language="en")
    assert es.language == "es" and en.language == "en"
    assert "ESTILO" in es.style_directive()
    assert "STYLE" in en.style_directive()
    # The directive must never leak reasoning or tool vocabulary.
    for text in (es.style_directive(), en.style_directive()):
        low = text.lower()
        assert "think" not in low and "tool" not in low and "json" not in low


def test_style_directive_requests_answer_first_for_brief():
    es = _shape("como saco la raiz cuadrada de algo", language="es")
    assert "primera frase" in es.style_directive()


# ── power profile ─────────────────────────────────────────────────────────────
def test_battery_saver_reduces_token_ceiling():
    ac = _shape("explica Kerberos con mas detalle",
                power_policy=policy_for(RuntimeProfile.AC_PERFORMANCE))
    bat = _shape("explica Kerberos con mas detalle",
                 power_policy=policy_for(RuntimeProfile.BATTERY_SAVER))
    assert bat.max_output_tokens < ac.max_output_tokens
    # A cap that bites must reduce the BASE too, otherwise an unadapted turn
    # generates exactly the same answer on battery as on mains.
    assert bat.base_output_tokens < ac.base_output_tokens
    assert bat.power_profile == "BATTERY_SAVER"
    # But never below the contract's own floor.
    assert bat.max_output_tokens >= bat.min_output_tokens
    assert bat.base_output_tokens >= bat.min_output_tokens


def test_battery_does_not_shorten_an_already_tiny_contract():
    ac = _shape("hola", power_policy=policy_for(RuntimeProfile.AC_PERFORMANCE))
    bat = _shape("hola", power_policy=policy_for(RuntimeProfile.BATTERY_SAVER))
    assert bat.base_output_tokens == ac.base_output_tokens


def test_power_policy_can_never_raise_the_ceiling():
    class _Greedy:
        max_generation_tokens = 99999
        profile = "AC_PERFORMANCE"

    s = _shape("hola", power_policy=_Greedy())
    assert s.max_output_tokens <= 64
    assert s.max_output_tokens <= HARD_MAX_OUTPUT_TOKENS


# ── deterministic helpers ─────────────────────────────────────────────────────
def test_detect_length_instruction_both_families_brevity_wins():
    kind, matched = detect_length_instruction("explicalo en detalle pero hazlo corto")
    assert kind == "brief"
    assert matched


def test_detect_length_instruction_none_for_ordinary_prose():
    assert detect_length_instruction("explicame como funciona TCP")[0] is None
    assert detect_length_instruction("")[0] is None


def test_continuation_markers_detected():
    assert is_continuation_request("continúa") is True
    assert is_continuation_request("mas detalles por favor") is True
    assert is_continuation_request("explicame TCP") is False


# ── shape invariants ──────────────────────────────────────────────────────────
def test_every_contract_has_bounded_monotonic_token_window():
    for msg in ("hola", "como saco la raiz cuadrada", "explicame POO",
                "explica Kerberos con mas detalle", "segun mi PDF que dice",
                "escribeme una funcion en python", "system status",
                "cuales son los tipos de datos"):
        s = _shape(msg)
        assert 0 < s.min_output_tokens <= s.base_output_tokens <= s.max_output_tokens
        assert s.max_output_tokens <= HARD_MAX_OUTPUT_TOKENS
        assert s.target_completion_ms > 0
        assert isinstance(s.speech, SpeechPolicy)
        assert s.deterministic_checks


def test_telemetry_is_bounded_and_content_free():
    s = _shape("segun mi PDF, cual es el procedimiento secreto de la empresa")
    tel = s.telemetry()
    blob = " ".join(str(v) for v in tel.values()).lower()
    assert "pdf" not in blob and "secreto" not in blob and "procedimiento" not in blob
    assert tel["contract"] == "DOCUMENT_GROUNDED"
    assert tel["selection_reason"]
    assert set(tel) >= {"contract", "selection_reason", "token_budget"} - {"token_budget"}
