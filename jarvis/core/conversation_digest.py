"""core/conversation_digest.py — V69 M57.6.1: deterministic conversation digest.

WHY EXTRACTIVE, NOT GENERATED
-----------------------------
The runtime already compacts long conversations by asking the MODEL to summarize
the old block (``core.llm._maybe_compress_history``). That has two costs this
milestone cannot accept: it spends a whole extra CPU-bound generation on a 15 W
host mid-turn, and — worse — its output becomes indistinguishable from something
the user actually said. A generated sentence like "the user prefers short answers"
is a HYPOTHESIS, and once it is inside the prompt it reads as a FACT.

So the digest is extractive and deterministic, and every item is LABELLED with how
it is known:

    EXPLICIT   the user said it, in those words (an instruction, a stated goal)
    OBSERVED   measured from the conversation (recurring topic, entity, unresolved
               question) — true about the transcript, not a claim about the user
    INFERRED   produced by optional model-assisted compaction; never authoritative
    UNKNOWN    the slot exists but nothing supports filling it

The rendered digest carries those labels into the prompt, so the model can never
silently promote an observation into a fact about the operator. An INFERRED item
may never overwrite an EXPLICIT one.

Pure, bounded and I/O-free: no model, no network, no memory write.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum

# Bounds — a digest that can grow is just the transcript again.
_MAX_ITEMS_PER_KIND = 3
_MAX_ITEM_CHARS = 140
_MAX_DIGEST_CHARS = 900
_MIN_TERM_LEN = 4
_TOPIC_MIN_HITS = 2

_STOPWORDS: frozenset[str] = frozenset({
    # ES
    "para", "como", "cual", "cuales", "esto", "eso", "esta", "este", "estos",
    "estas", "pero", "porque", "cuando", "donde", "sobre", "entre", "desde",
    "hasta", "todo", "toda", "todos", "todas", "otro", "otra", "puede", "puedes",
    "quiero", "necesito", "hacer", "tiene", "tienen", "hay", "muy", "más", "mas",
    "menos", "bien", "ahora", "luego", "también", "tambien", "explicame",
    "explícame", "explica", "dime", "favor", "gracias", "hola", "usar", "usa",
    "forma", "manera", "ejemplo", "ejemplos", "cosa", "cosas", "algo", "alguna",
    # EN
    "the", "and", "for", "this", "that", "with", "without", "what", "when",
    "where", "which", "there", "their", "from", "into", "about", "would", "could",
    "should", "have", "has", "been", "being", "explain", "tell", "please",
    "thanks", "hello", "some", "more", "less", "very", "then", "than", "example",
    "examples", "thing", "things",
})
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü][A-Za-zÁÉÍÓÚÑÜáéíóúñü_\-.]{2,}")
# Technical identifiers deserve to survive compaction verbatim.
_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9]{2,}(?:\.[A-Za-z0-9]+)*|[a-z_]+\.[a-z_]+\(?\)?|"
    r"[A-Za-z0-9_\-]+\.(?:py|md|json|yaml|yml|txt|pdf|log|sh|ps1))\b")

# Deterministic EXPLICIT-instruction cues. Narrow on purpose: only phrasings where
# the operator is stating a durable preference or goal, not discussing one.
_PREFERENCE_CUES = (
    "prefiero", "siempre responde", "siempre contesta", "no uses", "no utilices",
    "evita", "de ahora en adelante", "a partir de ahora", "recuerda que",
    "quiero que siempre", "responde en español", "responde en ingles",
    "responde en inglés", "hazlo corto", "sé breve", "se breve",
    "i prefer", "always answer", "always reply", "do not use", "don't use",
    "avoid", "from now on", "remember that", "answer in english",
    "answer in spanish", "keep it short", "be brief",
)
_GOAL_CUES = (
    "quiero construir", "quiero hacer", "estoy construyendo", "estoy haciendo",
    "necesito construir", "necesito hacer", "mi objetivo", "mi meta",
    "estoy trabajando en", "el proyecto es", "i want to build", "i am building",
    "i'm building", "i need to build", "my goal", "i am working on",
    "i'm working on", "the project is",
)
_CONSTRAINT_CUES = (
    "no puedo usar", "sin usar", "solo con", "únicamente", "unicamente",
    "tiene que ser", "debe ser", "sin conexión", "sin conexion", "offline",
    "i cannot use", "can't use", "without using", "only with", "it must be",
    "must not", "no internet",
)
# Markers the runtime itself appends when an answer did not finish.
_INCOMPLETE_MARKERS = (
    "no pude completar", "respuesta acortada", "couldn't finish",
    "answer shortened", "interrump", "incomplet",
)


class Evidence(str, Enum):
    """How a digest item is known. The label travels INTO the prompt."""

    EXPLICIT = "EXPLICIT"
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class ItemKind(str, Enum):
    GOAL = "goal"
    PREFERENCE = "preference"
    TOPIC = "topic"
    CONSTRAINT = "constraint"
    OPEN_QUESTION = "open_question"
    ENTITY = "entity"
    DECISION = "decision"


# Kinds an optional model-assisted pass is allowed to contribute at all.
_MODEL_ASSISTABLE: frozenset[ItemKind] = frozenset({
    ItemKind.TOPIC, ItemKind.DECISION, ItemKind.OPEN_QUESTION,
})


@dataclass(frozen=True)
class DigestItem:
    """One bounded fact about the conversation, with its epistemic label."""

    kind: ItemKind
    text: str
    evidence: Evidence
    source_turns: tuple[int, ...] = ()

    def render(self) -> str:
        turns = f" (turns {','.join(str(t) for t in self.source_turns[:3])})" \
            if self.source_turns else ""
        return f"[{self.evidence.value}] {self.kind.value}: {self.text}{turns}"

    def snapshot(self) -> dict:
        return {"kind": self.kind.value, "evidence": self.evidence.value,
                "chars": len(self.text), "source_turns": list(self.source_turns[:3])}


@dataclass
class ConversationDigest:
    """A bounded, labelled digest of the OLDER part of a conversation."""

    items: tuple[DigestItem, ...] = ()
    last_updated_turn: int = 0
    turns_covered: int = 0
    max_chars: int = _MAX_DIGEST_CHARS
    model_assisted: bool = False

    def by_evidence(self, evidence: Evidence) -> tuple[DigestItem, ...]:
        return tuple(i for i in self.items if i.evidence is evidence)

    def is_empty(self) -> bool:
        return not self.items

    def render(self, *, language: str = "es", max_chars: int | None = None) -> str:
        """The prompt block. Labels are part of the text — never stripped.

        The header states the contract in the turn's own language so the model is
        told, in code and not by convention, that OBSERVED is not EXPLICIT.
        """
        if not self.items:
            return ""
        en = str(language or "es").lower().startswith("en")
        header = ("[CONVERSATION DIGEST — EXPLICIT = the user said it; OBSERVED = "
                  "measured from the conversation; INFERRED = unverified. Never "
                  "present OBSERVED or INFERRED as something the user stated.]"
                  if en else
                  "[RESUMEN DE CONVERSACIÓN — EXPLICIT = lo dijo el usuario; "
                  "OBSERVED = medido en la conversación; INFERRED = sin verificar. "
                  "Nunca presentes OBSERVED ni INFERRED como algo que el usuario "
                  "afirmó.]")
        cap = max(80, int(max_chars if max_chars is not None else self.max_chars))
        lines = [header]
        used = len(header)
        # EXPLICIT first: if the budget runs out, what the user actually said is
        # what survives.
        order = (Evidence.EXPLICIT, Evidence.OBSERVED, Evidence.INFERRED)
        for ev in order:
            for item in self.by_evidence(ev):
                line = item.render()
                if used + len(line) + 1 > cap:
                    return "\n".join(lines)
                lines.append(line)
                used += len(line) + 1
        return "\n".join(lines)

    def estimated_tokens(self, *, language: str = "es") -> int:
        return estimate_tokens(self.render(language=language))

    def snapshot(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.evidence.value] = counts.get(item.evidence.value, 0) + 1
        return {
            "items": len(self.items),
            "by_evidence": counts,
            "last_updated_turn": self.last_updated_turn,
            "turns_covered": self.turns_covered,
            "model_assisted": self.model_assisted,
            "chars": len(self.render()),
        }


def estimate_tokens(text: str) -> int:
    """Bounded token estimate. ~4 chars/token, the same heuristic the runtime
    already uses for num_ctx sizing — an ESTIMATE, never claimed as exact."""
    return max(0, len(text or "") // 4)


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _MAX_ITEM_CHARS else text[:_MAX_ITEM_CHARS - 1] + "…"


def _sentence_with(text: str, cue: str) -> str:
    """The clause containing *cue*, so an item quotes the user rather than the
    whole turn."""
    low = text.lower()
    idx = low.find(cue)
    if idx < 0:
        return _clip(text)
    start = max(0, low.rfind(".", 0, idx) + 1)
    end = low.find(".", idx)
    end = len(text) if end < 0 else end + 1
    return _clip(text[start:end])


def build_digest(history: list[dict], *, keep_recent: int = 6,
                 max_chars: int = _MAX_DIGEST_CHARS) -> ConversationDigest:
    """Extract a bounded digest from the turns OLDER than the recent tail.

    Deterministic and extractive: every item quotes or measures the transcript.
    Nothing here calls a model, writes memory, or claims an inference as a fact.
    """
    history = [m for m in (history or []) if isinstance(m, dict)]
    if not history:
        return ConversationDigest(max_chars=max_chars)
    older = history[:-keep_recent] if keep_recent > 0 else list(history)
    if not older:
        return ConversationDigest(max_chars=max_chars)

    items: list[DigestItem] = []
    terms: Counter = Counter()
    entities: Counter = Counter()
    entity_turns: dict[str, list[int]] = {}
    prefs: list[DigestItem] = []
    goals: list[DigestItem] = []
    constraints: list[DigestItem] = []
    open_qs: list[DigestItem] = []
    last_user_text = ""
    last_user_turn = 0

    for idx, msg in enumerate(older):
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if not content:
            continue
        low = content.lower()
        if role == "user":
            last_user_text, last_user_turn = content, idx
            for cue in _PREFERENCE_CUES:
                if cue in low and len(prefs) < _MAX_ITEMS_PER_KIND:
                    prefs.append(DigestItem(ItemKind.PREFERENCE,
                                            _sentence_with(content, cue),
                                            Evidence.EXPLICIT, (idx,)))
                    break
            for cue in _GOAL_CUES:
                if cue in low and len(goals) < _MAX_ITEMS_PER_KIND:
                    goals.append(DigestItem(ItemKind.GOAL,
                                            _sentence_with(content, cue),
                                            Evidence.EXPLICIT, (idx,)))
                    break
            for cue in _CONSTRAINT_CUES:
                if cue in low and len(constraints) < _MAX_ITEMS_PER_KIND:
                    constraints.append(DigestItem(ItemKind.CONSTRAINT,
                                                  _sentence_with(content, cue),
                                                  Evidence.EXPLICIT, (idx,)))
                    break
            for w in _WORD_RE.findall(low):
                if len(w) >= _MIN_TERM_LEN and w not in _STOPWORDS:
                    terms[w] += 1
        if role in ("user", "assistant"):
            for ent in _ENTITY_RE.findall(content):
                entities[ent] += 1
                entity_turns.setdefault(ent, []).append(idx)
        if role == "assistant" and any(m in low for m in _INCOMPLETE_MARKERS):
            # OBSERVED, not EXPLICIT: the transcript shows the answer did not
            # finish. That is a measurement, not something the user told us.
            if last_user_text and len(open_qs) < _MAX_ITEMS_PER_KIND:
                open_qs.append(DigestItem(ItemKind.OPEN_QUESTION,
                                          _clip(last_user_text),
                                          Evidence.OBSERVED, (last_user_turn, idx)))

    items.extend(prefs)
    items.extend(goals)
    items.extend(constraints)
    items.extend(open_qs)
    for term, hits in terms.most_common(_MAX_ITEMS_PER_KIND):
        if hits >= _TOPIC_MIN_HITS:
            items.append(DigestItem(ItemKind.TOPIC, f"{term} (x{hits})",
                                    Evidence.OBSERVED))
    for ent, hits in entities.most_common(_MAX_ITEMS_PER_KIND):
        if hits >= _TOPIC_MIN_HITS:
            items.append(DigestItem(ItemKind.ENTITY, ent, Evidence.OBSERVED,
                                    tuple(entity_turns.get(ent, ())[:3])))
    return ConversationDigest(items=tuple(items), last_updated_turn=len(history),
                              turns_covered=len(older), max_chars=max_chars)


def merge_model_assisted(base: ConversationDigest,
                         proposed: list[DigestItem]) -> ConversationDigest:
    """Fold an OPTIONAL model-assisted pass into the extractive digest.

    The validator is the point of this function, so state it plainly:

      * a proposed item is forced to :data:`Evidence.INFERRED` regardless of what
        the model claimed — a model cannot mint EXPLICIT;
      * only the kinds in ``_MODEL_ASSISTABLE`` are accepted at all;
      * an item that duplicates or contradicts an EXPLICIT item is DROPPED, so
        model output can never overwrite what the user actually said;
      * everything stays bounded by the same per-kind and per-item limits.

    The extractive digest remains the source of truth; this only adds to it.
    """
    if not proposed:
        return base
    explicit_text = {i.text.strip().lower() for i in base.by_evidence(Evidence.EXPLICIT)}
    existing = {(i.kind, i.text.strip().lower()) for i in base.items}
    kept: list[DigestItem] = []
    per_kind: Counter = Counter()
    for item in proposed:
        if not isinstance(item, DigestItem) or item.kind not in _MODEL_ASSISTABLE:
            continue
        text = _clip(item.text)
        key = (item.kind, text.strip().lower())
        if not text or key in existing or text.strip().lower() in explicit_text:
            continue
        if per_kind[item.kind] >= _MAX_ITEMS_PER_KIND:
            continue
        per_kind[item.kind] += 1
        existing.add(key)
        kept.append(DigestItem(item.kind, text, Evidence.INFERRED,
                               item.source_turns))
    if not kept:
        return base
    return ConversationDigest(items=base.items + tuple(kept),
                              last_updated_turn=base.last_updated_turn,
                              turns_covered=base.turns_covered,
                              max_chars=base.max_chars, model_assisted=True)
