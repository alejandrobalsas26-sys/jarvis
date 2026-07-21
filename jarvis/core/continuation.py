"""core/continuation.py — V69 M57.7/.7.1: continuation, expansion and recovery.

WHY THIS EXISTS
---------------
M57.1/.2 make answers SHORT on purpose: a greeting no longer costs 256 tokens, and
a bounded answer that reaches its cap is marked truthfully instead of pretending to
be complete. That is only honest if the operator can actually get the rest — so
"continúa" has to resume from where the answer stopped, without regenerating the
part already on screen (which at ~6 tok/s is the expensive half).

WHAT IT REMEMBERS — AND WHAT IT REFUSES TO
------------------------------------------
Only bounded, already-VISIBLE facts:

  * the previous turn id, its contract and its terminal state;
  * the last stable displayed boundary (the tail of what the operator actually
    saw), so a continuation is anchored to shown text, never to tokens nobody read;
  * completed structural checkpoints (headings / list items / code blocks), taken
    at stable boundaries only — never per token;
  * a topic fingerprint, so a change of subject invalidates the cursor.

It stores NO hidden model state and NO chain of thought. There is nothing here to
resume FROM except text the operator already has.

Ephemeral by design: this is in-process state. A shutdown discards it, and the next
"continúa" then honestly reports that there is nothing to continue rather than
inventing a cursor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_MAX_BOUNDARY_CHARS = 320
_MAX_SECTIONS = 8
_TOPIC_TERMS = 12
_MIN_TOPIC_OVERLAP = 0.25

_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9]{4,}")
_STOP = frozenset({
    "para", "como", "cual", "esto", "eso", "esta", "este", "pero", "porque",
    "cuando", "donde", "sobre", "entre", "desde", "hasta", "explicame",
    "explícame", "explica", "dime", "mas", "más", "menos", "the", "and", "for",
    "this", "that", "with", "what", "when", "where", "which", "explain", "tell",
    "about", "more", "less", "please",
})


class ContinuationIntent(str, Enum):
    """A deterministic conversational instruction about the PREVIOUS answer."""

    NONE = "NONE"
    CONTINUE = "CONTINUE"              # "continúa", "sigue"
    MORE_DETAIL = "MORE_DETAIL"        # "más detalles"
    EXPAND_SECTION = "EXPAND_SECTION"  # "explica el segundo punto"
    EXAMPLE = "EXAMPLE"                # "pon un ejemplo"
    SUMMARIZE = "SUMMARIZE"            # "resúmelo"
    SHORTEN = "SHORTEN"                # "hazlo más corto"
    LANGUAGE_SWITCH = "LANGUAGE_SWITCH"  # "answer in English"


class ContinuationRefusal(str, Enum):
    """Why a continuation could not be honored. Always truthful, never silent."""

    OK = "OK"
    NO_PREVIOUS_ANSWER = "NO_PREVIOUS_ANSWER"
    TOPIC_CHANGED = "TOPIC_CHANGED"
    NOTHING_LEFT = "NOTHING_LEFT"
    NOT_CONTINUABLE = "NOT_CONTINUABLE"


_INTENT_MARKERS: dict[ContinuationIntent, tuple[str, ...]] = {
    ContinuationIntent.SHORTEN: (
        "hazlo mas corto", "hazlo más corto", "mas corto", "más corto",
        "acortalo", "acórtalo", "make it shorter", "shorter", "keep it short",
    ),
    ContinuationIntent.SUMMARIZE: (
        "resumelo", "resúmelo", "resume eso", "en resumen", "haz un resumen",
        "summarize it", "summarise it", "sum it up", "tl;dr", "tldr",
    ),
    ContinuationIntent.EXAMPLE: (
        "pon un ejemplo", "ponme un ejemplo", "dame un ejemplo", "un ejemplo",
        "con un ejemplo", "give me an example", "an example", "show an example",
    ),
    ContinuationIntent.EXPAND_SECTION: (
        "explica el", "explicame el", "explícame el", "amplia el", "amplía el",
        "desarrolla el", "el segundo punto", "el primer punto", "el tercer punto",
        "expand on the", "explain the second", "explain the first",
        "explain the third", "elaborate on the",
    ),
    ContinuationIntent.MORE_DETAIL: (
        "mas detalles", "más detalles", "mas detalle", "más detalle",
        "profundiza", "amplia", "amplía", "more details", "more detail",
        "tell me more", "go deeper",
    ),
    ContinuationIntent.CONTINUE: (
        "continua", "continúa", "sigue", "seguimos", "adelante", "y luego",
        "continue", "go on", "keep going", "carry on",
    ),
}
# Ordinal cues for EXPAND_SECTION targeting. Bounded and closed.
_ORDINALS: dict[str, int] = {
    "primer": 1, "primero": 1, "primera": 1, "first": 1, "1": 1,
    "segundo": 2, "segunda": 2, "second": 2, "2": 2,
    "tercer": 3, "tercero": 3, "tercera": 3, "third": 3, "3": 3,
    "cuarto": 4, "cuarta": 4, "fourth": 4, "4": 4,
    "quinto": 5, "quinta": 5, "fifth": 5, "5": 5,
}
# Intents that need a previous answer to mean anything at all.
_NEEDS_PREVIOUS: frozenset[ContinuationIntent] = frozenset({
    ContinuationIntent.CONTINUE, ContinuationIntent.MORE_DETAIL,
    ContinuationIntent.EXPAND_SECTION, ContinuationIntent.EXAMPLE,
    ContinuationIntent.SUMMARIZE, ContinuationIntent.SHORTEN,
})


def topic_fingerprint(text: str) -> frozenset[str]:
    """A bounded bag of significant terms. Deterministic, no model."""
    terms = [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP]
    return frozenset(terms[:_TOPIC_TERMS])


def topic_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def classify_continuation(text: str) -> tuple[ContinuationIntent, int | None]:
    """Classify a turn as a continuation instruction. Deterministic and total.

    Returns ``(intent, section_ordinal)``. Precedence is most-specific-first so
    "hazlo más corto" is SHORTEN and not MORE_DETAIL just because both mention
    length. A turn that is not an instruction about the previous answer is NONE.
    """
    low = (text or "").lower().strip()
    if not low:
        return ContinuationIntent.NONE, None
    try:
        from core.language_context import parse_language_override
        if parse_language_override(low) is not None:
            return ContinuationIntent.LANGUAGE_SWITCH, None
    except Exception:  # noqa: BLE001
        pass
    for intent, markers in _INTENT_MARKERS.items():
        if any(m in low for m in markers):
            ordinal = None
            if intent is ContinuationIntent.EXPAND_SECTION:
                for word, num in _ORDINALS.items():
                    if re.search(rf"\b{re.escape(word)}\b", low):
                        ordinal = num
                        break
                if ordinal is None:
                    # "explica el X" without an ordinal is a new question, not an
                    # expansion of a numbered section.
                    continue
            return intent, ordinal
    return ContinuationIntent.NONE, None


@dataclass
class ContinuationState:
    """What can be continued after ONE finished turn. Bounded and ephemeral."""

    turn_id: int = 0
    contract: str = ""
    terminal_state: str = ""
    language: str = "es"
    available: bool = False
    truncated_by_cap: bool = False
    last_boundary: str = ""
    completed_sections: tuple[str, ...] = ()
    topic: frozenset[str] = field(default_factory=frozenset)
    question: str = ""

    def snapshot(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "contract": self.contract,
            "terminal_state": self.terminal_state,
            "language": self.language,
            "available": self.available,
            "truncated_by_cap": self.truncated_by_cap,
            "boundary_chars": len(self.last_boundary),
            "completed_sections": len(self.completed_sections),
            "topic_terms": len(self.topic),
        }


def build_state(*, turn_id: int, contract: str, terminal_state: str,
                language: str, displayed_text: str, question: str,
                truncated_by_cap: bool = False,
                sections: list[str] | None = None) -> ContinuationState:
    """Capture continuation state from what was ACTUALLY DISPLAYED.

    ``displayed_text`` is the operator's screen, not the generator's output: an
    interrupted turn can only be resumed from what was shown.
    """
    text = (displayed_text or "").strip()
    boundary = text[-_MAX_BOUNDARY_CHARS:] if text else ""
    available = bool(text) and terminal_state != "FAILED"
    return ContinuationState(
        turn_id=int(turn_id), contract=str(contract or ""),
        terminal_state=str(terminal_state or ""), language=str(language or "es"),
        available=available, truncated_by_cap=bool(truncated_by_cap),
        last_boundary=boundary,
        completed_sections=tuple((sections or [])[:_MAX_SECTIONS]),
        topic=topic_fingerprint(f"{question} {text[:600]}"),
        question=(question or "")[:200],
    )


def checkpoints_from_fragments(fragments) -> list[str]:
    """M57.7.1 — structural checkpoints at STABLE boundaries only.

    A heading, a list item or a closed code block is a place an answer can resume
    from. A token is not. Bounded to a handful of entries and never persisted.
    """
    out: list[str] = []
    for frag in fragments or []:
        kind = getattr(getattr(frag, "kind", None), "value", "")
        text = (getattr(frag, "text", "") or "").strip()
        if not text:
            continue
        if kind in ("PARAGRAPH", "LIST_ITEM") and len(out) < _MAX_SECTIONS:
            out.append(text[:80])
        elif kind == "CODE_BLOCK_BOUNDARY" and len(out) < _MAX_SECTIONS:
            out.append("<code block>")
    return out


def evaluate(intent: ContinuationIntent, state: "ContinuationState | None", *,
             user_message: str = "") -> ContinuationRefusal:
    """Can this continuation be honored? Fail-closed and inspectable."""
    if intent is ContinuationIntent.NONE:
        return ContinuationRefusal.OK
    if intent is ContinuationIntent.LANGUAGE_SWITCH:
        return ContinuationRefusal.OK          # never needs a previous answer
    if intent in _NEEDS_PREVIOUS:
        if state is None or not state.available or not state.last_boundary:
            return ContinuationRefusal.NO_PREVIOUS_ANSWER
        if user_message:
            asked = topic_fingerprint(user_message)
            # A bare "continúa" carries no topic of its own, so overlap is only
            # meaningful when the operator actually named a subject.
            if len(asked) >= 3 and topic_overlap(asked, state.topic) < _MIN_TOPIC_OVERLAP:
                return ContinuationRefusal.TOPIC_CHANGED
    return ContinuationRefusal.OK


_DIRECTIVES = {
    ContinuationIntent.CONTINUE: (
        "CONTINUACIÓN: retoma la respuesta anterior justo donde terminó el texto "
        "mostrado abajo. NO repitas nada de lo ya mostrado y no vuelvas a "
        "introducir el tema.",
        "CONTINUATION: resume the previous answer exactly where the shown text "
        "below stops. Do NOT repeat anything already shown and do not "
        "re-introduce the topic."),
    ContinuationIntent.MORE_DETAIL: (
        "AMPLIACIÓN: profundiza en el tema anterior añadiendo información NUEVA. "
        "No repitas lo ya mostrado abajo.",
        "EXPANSION: go deeper on the previous topic with NEW information only. "
        "Do not repeat what is shown below."),
    ContinuationIntent.EXPAND_SECTION: (
        "AMPLIACIÓN DE SECCIÓN: desarrolla únicamente el punto indicado de la "
        "respuesta anterior. No repitas el resto.",
        "SECTION EXPANSION: develop ONLY the indicated point of the previous "
        "answer. Do not repeat the rest."),
    ContinuationIntent.EXAMPLE: (
        "EJEMPLO: añade un ejemplo concreto y breve del tema anterior. No "
        "repitas la explicación.",
        "EXAMPLE: add one concrete, short example of the previous topic. Do not "
        "repeat the explanation."),
    ContinuationIntent.SUMMARIZE: (
        "RESUMEN: resume la respuesta anterior en pocas frases. No añadas "
        "información nueva.",
        "SUMMARY: summarize the previous answer in a few sentences. Do not add "
        "new information."),
    ContinuationIntent.SHORTEN: (
        "VERSIÓN CORTA: reformula la respuesta anterior de forma más breve, "
        "conservando lo esencial.",
        "SHORT VERSION: restate the previous answer more briefly, keeping the "
        "essentials."),
}
_RESUMING_INCOMPLETE = (
    "AVISO: la respuesta anterior quedó incompleta ({state}). Continúa desde el "
    "texto mostrado y dilo en una frase corta al empezar.",
    "NOTE: the previous answer was left incomplete ({state}). Resume from the "
    "shown text and say so in one short sentence at the start.")


def build_directive(intent: ContinuationIntent, state: ContinuationState, *,
                    language: str | None = None, ordinal: int | None = None) -> str:
    """The bounded prompt block for a continuation turn.

    It carries only DISPLAYED text plus a stylistic instruction. No hidden state,
    no reasoning, no runtime error text ever reaches the model here.
    """
    lang = str(language or state.language or "es").lower()
    en = lang.startswith("en")
    pair = _DIRECTIVES.get(intent)
    if pair is None:
        return ""
    parts = [pair[1] if en else pair[0]]
    if state.terminal_state and state.terminal_state not in ("COMPLETED", ""):
        tmpl = _RESUMING_INCOMPLETE[1] if en else _RESUMING_INCOMPLETE[0]
        parts.append(tmpl.format(state=state.terminal_state))
    if ordinal is not None and state.completed_sections:
        idx = max(0, min(ordinal - 1, len(state.completed_sections) - 1))
        label = "SECTION" if en else "SECCIÓN"
        parts.append(f"{label} {ordinal}: {state.completed_sections[idx]}")
    shown = "SHOWN SO FAR" if en else "TEXTO YA MOSTRADO"
    parts.append(f"[{shown}]\n{state.last_boundary}")
    return "\n\n".join(parts)


_REFUSALS = {
    ContinuationRefusal.NO_PREVIOUS_ANSWER: (
        "No hay una respuesta previa que continuar.",
        "There is no previous answer to continue."),
    ContinuationRefusal.TOPIC_CHANGED: (
        "El tema cambió, así que empiezo una respuesta nueva.",
        "The topic changed, so I'm starting a new answer."),
    ContinuationRefusal.NOTHING_LEFT: (
        "La respuesta anterior ya estaba completa.",
        "The previous answer was already complete."),
    ContinuationRefusal.NOT_CONTINUABLE: (
        "Esa respuesta no se puede continuar.",
        "That answer cannot be continued."),
}


def describe_refusal(refusal: ContinuationRefusal, *, language: str = "es") -> str:
    pair = _REFUSALS.get(refusal)
    if pair is None:
        return ""
    return pair[1] if str(language or "es").lower().startswith("en") else pair[0]


# ── Process-global, bounded, ephemeral ───────────────────────────────────────
_state: ContinuationState | None = None


def get_continuation() -> "ContinuationState | None":
    return _state


def set_continuation(state: "ContinuationState | None") -> None:
    global _state
    _state = state


def clear_continuation() -> None:
    """Explicitly forget the cursor (topic change, shutdown, model switch)."""
    global _state
    _state = None
