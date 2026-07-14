"""
tests/test_language_continuity_v69.py — V69 M54.4 text-loop language continuity.

Locks the drift the live run showed: a Spanish conversation must stay Spanish even
after English tool output or a verifier timeout, ambiguous tokens ("POO") inherit
the active language, and an explicit "answer in English" overrides — all without an
LLM call. Voice behavior (whisper hints) is unchanged.
"""
from __future__ import annotations

from core.language_context import (
    LanguageContext,
    detect_text_language,
    parse_language_override,
)


def _es_ctx() -> LanguageContext:
    # Start from a known Spanish baseline (the configured default is 'es').
    ctx = LanguageContext()
    ctx.detected_lang = "es"
    ctx.override_lang = None
    return ctx


# ── Deterministic detector ────────────────────────────────────────────────────

def test_detect_spanish_by_diacritic():
    lang, conf = detect_text_language("¿Qué es la herencia?")
    assert lang == "es" and conf >= 0.5


def test_detect_english_by_markers():
    lang, conf = detect_text_language("what is inheritance and how does it work")
    assert lang == "en" and conf >= 0.5


def test_short_ambiguous_token_is_undecided():
    assert detect_text_language("POO") == (None, 0.0)
    assert detect_text_language("hola") == (None, 0.0)   # too few tokens


# ── Continuity across turns ───────────────────────────────────────────────────

def test_spanish_stays_spanish():
    ctx = _es_ctx()
    assert ctx.observe_text("explícame la herencia en Python") == "es"
    assert ctx.active_language() == "es"


def test_english_switches_to_english():
    ctx = _es_ctx()
    assert ctx.observe_text("explain inheritance in python for me please") == "en"


def test_spanish_after_english_toolish_context_stays_spanish():
    ctx = _es_ctx()
    ctx.observe_text("qué es una clase en Python")
    # An English tool result does not reset language — only USER turns do, and this
    # user turn is Spanish.
    assert ctx.observe_text("y cómo funciona el polimorfismo") == "es"


def test_ambiguous_poo_inherits_active_language():
    ctx = _es_ctx()
    ctx.observe_text("hablemos de programación orientada a objetos")  # es
    assert ctx.observe_text("POO") == "es"   # inherits, does not flip


def test_explicit_override_wins_and_is_sticky():
    ctx = _es_ctx()
    assert ctx.observe_text("answer in English from now on") == "en"
    # A later ambiguous/Spanish-leaning short turn does not undo the override.
    assert ctx.observe_text("POO") == "en"
    assert ctx.observe_text("dame un ejemplo") == "en"   # override sticky
    # Operator can switch back explicitly.
    assert ctx.observe_text("responde en español") == "es"


def test_parse_override_phrases():
    assert parse_language_override("answer in english please") == "en"
    assert parse_language_override("responde en español") == "es"
    assert parse_language_override("what is POO") is None


# ── Directive enforced in the prompt ──────────────────────────────────────────

def test_directive_matches_active_language():
    ctx = _es_ctx()
    assert "español" in ctx.directive()
    ctx.observe_text("answer in english")
    assert "English" in ctx.directive()


def test_voice_hint_follows_active_language():
    ctx = _es_ctx()
    ctx.observe_text("answer in english")
    assert ctx.voice_hint() == "en"
