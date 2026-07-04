"""
tests/test_language_context.py — V62.0 Phase 1: Multilingual Core.

Covers core/language_context.py's LanguageContext (session-scoped language
state fed by faster-whisper's language-ID output) and core/tts.py's
TTSVoiceRouter (per-language voice selection with graceful fallback).

TTSVoiceRouter is deliberately engine-independent (takes a plain iterable of
voice-like objects) so these tests never construct a real pyttsx3 engine —
portable across platforms/CI without requiring SAPI5/espeak.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.language_context import LanguageContext, KNOWN_LANGUAGES
from core.tts import TTSVoiceRouter


class _FakeVoice:
    def __init__(self, id_: str, name: str, languages=None):
        self.id = id_
        self.name = name
        self.languages = languages or []


# ── LanguageContext ──────────────────────────────────────────────────────────

def test_default_language_matches_fixed_whisper_config(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "whisper_language", "en")
    ctx = LanguageContext()
    assert ctx.detected_lang == "en"
    assert ctx.confidence == 1.0
    assert isinstance(ctx.updated_at, datetime)
    assert ctx.updated_at.tzinfo is timezone.utc


def test_default_language_falls_back_when_config_is_auto(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "whisper_language", "auto")
    ctx = LanguageContext()
    assert ctx.detected_lang == "es"  # _FALLBACK_LANGUAGE — no forced language to prefer


def test_low_confidence_detection_does_not_switch():
    ctx = LanguageContext(detected_lang="es")
    assert ctx.should_switch("en", 0.2) is False
    assert ctx.update("en", 0.2) is False
    assert ctx.detected_lang == "es"


def test_none_or_empty_candidate_does_not_switch():
    ctx = LanguageContext(detected_lang="es")
    assert ctx.should_switch(None, 0.99) is False
    assert ctx.should_switch("", 0.99) is False
    assert ctx.update(None, 0.99) is False
    assert ctx.detected_lang == "es"


def test_same_language_is_not_a_switch():
    ctx = LanguageContext(detected_lang="es")
    assert ctx.should_switch("es", 0.95) is False
    assert ctx.update("es", 0.95) is False


def test_confident_new_language_switches_and_updates_timestamp():
    ctx = LanguageContext(detected_lang="es", confidence=1.0)
    before = ctx.updated_at
    changed = ctx.update("en", 0.9)
    assert changed is True
    assert ctx.detected_lang == "en"
    assert ctx.confidence == 0.9
    assert ctx.updated_at >= before


def test_code_switching_across_multiple_turns():
    """Same session must be able to flip languages back and forth."""
    ctx = LanguageContext(detected_lang="es")
    assert ctx.update("en", 0.9) is True
    assert ctx.update("es", 0.85) is True
    assert ctx.detected_lang == "es"


def test_voice_hint_falls_back_for_unknown_language():
    ctx = LanguageContext(detected_lang="es")
    ctx.update("fr", 0.99)  # fr not in KNOWN_LANGUAGES -> should_switch True, applied
    assert ctx.detected_lang == "fr"
    assert "fr" not in KNOWN_LANGUAGES
    assert ctx.voice_hint() == "es"  # graceful fallback, never raises


def test_voice_hint_passes_through_known_language():
    ctx = LanguageContext(detected_lang="en")
    assert ctx.voice_hint() == "en"


# ── TTSVoiceRouter ────────────────────────────────────────────────────────────

def test_bare_es_substring_no_longer_false_positives_on_windows_voice_path():
    """Regression guard for the bug this router replaces: every Windows SAPI5
    voice id contains '...Speech\\Voices\\Tokens\\...', and 'Voices' contains
    the substring 'es' — the OLD heuristic ("es" in id.lower()) matched every
    single voice and always picked the first one, regardless of language."""
    voices = [
        _FakeVoice(
            r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_DAVID_11.0",
            "Microsoft David Desktop - English (United States)",
        ),
        _FakeVoice(
            r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_ES-ES_HELENA_11.0",
            "Microsoft Helena Desktop - Spanish (Spain)",
        ),
    ]
    router = TTSVoiceRouter(voices)
    assert router.voice_for("es") == voices[1].id
    assert router.voice_for("en") == voices[0].id


def test_voice_router_uses_languages_attribute_when_available():
    voices = [
        _FakeVoice("v1", "Some Voice", languages=[b"en-US"]),
        _FakeVoice("v2", "Otra Voz", languages=["es-MX"]),
    ]
    router = TTSVoiceRouter(voices)
    assert router.voice_for("en") == "v1"
    assert router.voice_for("es") == "v2"


def test_voice_router_graceful_fallback_for_missing_language():
    router = TTSVoiceRouter([_FakeVoice("v1", "Microsoft David - English (US)", languages=["en-US"])])
    assert router.voice_for("es") is None  # no Spanish voice installed
    assert router.voice_for(None) is None
    assert router.voice_for("") is None


def test_voice_router_empty_voice_list_never_raises():
    router = TTSVoiceRouter([])
    assert router.voice_for("es") is None
    assert router.voice_for("en") is None
