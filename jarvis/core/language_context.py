"""
core/language_context.py — V62.0 Phase 1: Multilingual Core.

Session-scoped language state so voice/text output can follow the user
across code-switches instead of being fixed at process start or left to an
unenforced LLM prose instruction. Pure, I/O-free (same pattern as
core/ironman_mode.py) — callers own the actual STT/TTS side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Languages we have a native TTS voice preference for (core/tts.py's
# TTSVoiceRouter). Any other detected code falls back to the default voice —
# graceful fallback, never an error.
KNOWN_LANGUAGES: frozenset[str] = frozenset({"en", "es"})

_FALLBACK_LANGUAGE = "es"
_MIN_SWITCH_CONFIDENCE = 0.5  # below this, a detection is too unreliable to act on


def _configured_default_language() -> str:
    """The operator-configured fixed language, or the fallback if STT runs in
    auto-detect mode (there is no meaningful 'default' to prefer there)."""
    try:
        from core.config import settings
        lang = (settings.whisper_language or "").strip().lower()
        if lang and lang != "auto":
            return lang
    except Exception:
        pass
    return _FALLBACK_LANGUAGE


@dataclass
class LanguageContext:
    """Mutable, session-scoped language state.

    detected_lang/confidence are fed by core.audio.HighPrioritySTTListener's
    last_detected_language/last_language_confidence (populated from
    faster-whisper's language-ID output) after each transcription. In fixed-
    language STT mode (the default), the detector always reports back the
    same forced language, so update() is a permanent no-op — this object is
    purely additive and never changes behavior unless whisper_language='auto'.
    """
    detected_lang: str = field(default_factory=_configured_default_language)
    confidence: float = 1.0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def should_switch(self, candidate_lang: str | None, candidate_confidence: float) -> bool:
        """True if *candidate_lang* is a confident, actual change of language."""
        if not candidate_lang:
            return False
        if candidate_confidence < _MIN_SWITCH_CONFIDENCE:
            return False
        return candidate_lang != self.detected_lang

    def update(self, candidate_lang: str | None, candidate_confidence: float) -> bool:
        """Apply a new detection if it clears should_switch().

        Returns whether the context actually changed, so callers know
        whether to re-route TTS voice selection.
        """
        if not self.should_switch(candidate_lang, candidate_confidence):
            return False
        self.detected_lang = candidate_lang
        self.confidence = candidate_confidence
        self.updated_at = datetime.now(timezone.utc)
        return True

    def voice_hint(self) -> str:
        """Language code for TTSVoiceRouter. Unknown/unsupported languages
        fall back to the default so voice selection never raises or picks an
        unrelated voice for a language we have no preference mapped."""
        return self.detected_lang if self.detected_lang in KNOWN_LANGUAGES else _FALLBACK_LANGUAGE
