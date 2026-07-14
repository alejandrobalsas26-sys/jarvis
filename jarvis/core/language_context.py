"""
core/language_context.py — V62.0 Phase 1: Multilingual Core.

Session-scoped language state so voice/text output can follow the user
across code-switches instead of being fixed at process start or left to an
unenforced LLM prose instruction. Pure, I/O-free (same pattern as
core/ironman_mode.py) — callers own the actual STT/TTS side effects.

V69 M54.4 extends the SAME state to the text loop. The live run drifted to
English mid-Spanish-conversation because text turns carried no language state
(only voice fed whisper language-ID here) and the system-prompt rule was soft.
``observe_text`` applies a deterministic (no-LLM) detector plus explicit
"answer in English"/"responde en español" overrides, keeping the active language
sticky across tool failures, verifier timeouts and model switches; ``directive``
emits a first-party system-prompt instruction that enforces it. Short/ambiguous
tokens ("POO") do not switch — they inherit the active conversation language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Languages we have a native TTS voice preference for (core/tts.py's
# TTSVoiceRouter). Any other detected code falls back to the default voice —
# graceful fallback, never an error.
KNOWN_LANGUAGES: frozenset[str] = frozenset({"en", "es"})

_FALLBACK_LANGUAGE = "es"
_MIN_SWITCH_CONFIDENCE = 0.5  # below this, a detection is too unreliable to act on

# ── Deterministic text language detection (M54.4) ─────────────────────────────
# Common function words that are strongly language-discriminative. Substring-free
# whole-word matching (tokenized) so "elenco" doesn't match "en". Kept small and
# high-signal; the detector is a tie-broken hit count, never an LLM call.
_ES_MARKERS: frozenset[str] = frozenset({
    "qué", "que", "cómo", "como", "por", "para", "esto", "eso", "una", "uno",
    "el", "la", "los", "las", "un", "de", "del", "en", "con", "sin", "sobre",
    "explícame", "explicame", "explica", "dime", "hola", "gracias", "porque",
    "cuál", "cual", "dónde", "donde", "cuándo", "cuando", "quién", "quien",
    "hora", "día", "dia", "ayuda", "ayúdame", "muéstrame", "muestrame",
    "mi", "mis", "tu", "tus", "según", "segun", "archivo", "documento",
    "es", "son", "está", "esta", "están", "estan", "puedes", "quiero",
})
_EN_MARKERS: frozenset[str] = frozenset({
    "what", "how", "why", "the", "and", "for", "this", "that", "with", "without",
    "explain", "tell", "hello", "thanks", "please", "which", "where", "when",
    "who", "time", "day", "help", "show", "me", "my", "your", "about", "file",
    "document", "is", "are", "can", "you", "want", "according",
})
# Explicit override phrases → force a language until the user says otherwise.
_OVERRIDE_EN = (
    "answer in english", "reply in english", "in english please",
    "respond in english", "responde en inglés", "responde en ingles",
    "contesta en inglés", "contesta en ingles", "en inglés por favor",
    "habla en inglés", "habla en ingles",
)
_OVERRIDE_ES = (
    "answer in spanish", "reply in spanish", "in spanish please",
    "respond in spanish", "responde en español", "responde en espanol",
    "contesta en español", "contesta en espanol", "en español por favor",
    "habla en español", "habla en espanol",
)
_WORD_RE = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)
# Minimum informative tokens before a detection is trusted; below this the turn is
# ambiguous (e.g. "POO", "hola") and inherits the active language.
_MIN_TOKENS_FOR_DETECT = 3
_SPANISH_DIACRITICS = re.compile(r"[áéíóúñ¿¡]", re.IGNORECASE)


def parse_language_override(text: str) -> str | None:
    """Return 'en'/'es' if *text* explicitly requests a reply language, else None."""
    low = (text or "").lower()
    if any(p in low for p in _OVERRIDE_EN):
        return "en"
    if any(p in low for p in _OVERRIDE_ES):
        return "es"
    return None


def detect_text_language(text: str) -> tuple[str | None, float]:
    """Deterministically detect 'es'/'en' from *text*, or (None, 0.0) when the
    turn is too short/ambiguous to decide. Confidence is a bounded ratio of the
    winning marker share — never an LLM call.

    A Spanish diacritic (á, ñ, ¿, ...) is decisive on its own. Otherwise the
    winner is whichever marker set has more whole-word hits; a tie or too-few
    tokens yields no decision so the caller keeps the active language.
    """
    low = (text or "").lower().strip()
    if not low:
        return None, 0.0
    if _SPANISH_DIACRITICS.search(low):
        return "es", 0.9
    tokens = _WORD_RE.findall(low)
    if len(tokens) < _MIN_TOKENS_FOR_DETECT:
        return None, 0.0
    tset = set(tokens)
    es_hits = len(tset & _ES_MARKERS)
    en_hits = len(tset & _EN_MARKERS)
    if es_hits == en_hits:
        return None, 0.0
    winner = "es" if es_hits > en_hits else "en"
    total = max(1, es_hits + en_hits)
    confidence = round(0.5 + 0.5 * abs(es_hits - en_hits) / total, 2)
    return winner, confidence


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
    # M54.4 — explicit operator override ("answer in English"). Sticky: once set it
    # wins over automatic detection until the operator changes it. None = auto.
    override_lang: str | None = None

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
        active = self.active_language()
        return active if active in KNOWN_LANGUAGES else _FALLBACK_LANGUAGE

    # ── Text-loop language continuity (M54.4) ────────────────────────────────
    def active_language(self) -> str:
        """The language the assistant should currently reply in: an explicit
        override wins, otherwise the last confidently-detected language. Stable
        across tool failures / verifier timeouts / model switches because it only
        changes on a new confident user turn or an explicit override."""
        return self.override_lang or self.detected_lang

    def observe_text(self, text: str) -> str:
        """Update the active language from one USER text turn and return it.

        Precedence: explicit override ("answer in English") sets a sticky override;
        otherwise a confident deterministic detection updates the active language;
        an ambiguous/short turn ("POO", "hola") changes nothing and inherits the
        current language. Never calls an LLM.
        """
        override = parse_language_override(text)
        if override is not None:
            self.override_lang = override
            self.detected_lang = override
            self.confidence = 1.0
            self.updated_at = datetime.now(timezone.utc)
            return override
        # No override in this turn. If an override is active, it stays sticky.
        cand, conf = detect_text_language(text)
        if cand is not None and conf >= _MIN_SWITCH_CONFIDENCE and self.override_lang is None:
            if cand != self.detected_lang:
                self.detected_lang = cand
                self.confidence = conf
                self.updated_at = datetime.now(timezone.utc)
        return self.active_language()

    def directive(self) -> str:
        """First-party system-prompt instruction enforcing the active language.
        Injected each turn so language continuity is enforced in code, not left to
        an unenforced prose hint. Technical terms stay in English per house style."""
        lang = self.active_language()
        if lang == "en":
            return ("LANGUAGE DIRECTIVE: Reply to the user in English. Keep this "
                    "language for the whole turn regardless of tool output or "
                    "internal model switches.")
        # Default / Spanish.
        return ("DIRECTIVA DE IDIOMA: Responde al usuario en español (mantén los "
                "términos técnicos en inglés: payload, buffer overflow, thread). "
                "Conserva este idioma durante todo el turno, sin importar el "
                "resultado de las herramientas ni los cambios internos de modelo.")
