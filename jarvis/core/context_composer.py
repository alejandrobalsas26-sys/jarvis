"""core/context_composer.py — V69 M57.6/.6.2: bounded live-prompt composition.

THE PROBLEM
-----------
Both transports built their message list the same way::

    messages = [{"role": "system", ...}, *self.history]

The FULL transcript, every turn, forever. The only limiter was turn-COUNT
compression at 15 turns (which spends an extra CPU-bound model call), and nothing
ever measured tokens. On a 2048-token FAST context that means prefill grows until
the server silently drops the oldest messages — and the messages a server drops
are chosen by position, not by importance, so the security instructions at the
front are exactly what goes first.

THE COMPOSER
------------
One bounded assembly with an explicit RETENTION order. Layers, most authoritative
first::

    1  SYSTEM      security / identity / language / contract   never trimmed
    2  PINNED      explicit operator preferences (digest)      never trimmed
    3  CURRENT     the user message being answered             never trimmed
    4  TOOL        tool evidence required by this turn         never trimmed
    5  MEMORY      retrieved document / episodic evidence      trimmed late
    6  RECENT      recent complete turns                       trimmed oldest-first
    7  DIGEST      bounded extractive digest of older turns    trimmed first

Trimming walks the OPPOSITE order and, inside RECENT, drops the cheapest content
first: repeated assistant boilerplate, greetings and completed small talk before
anything substantive.

WHAT IS NEVER COMPOSED IN
-------------------------
Chain of thought, raw logs, stale errors, secrets, unrelated old tool results, and
whole semantic collections. Tool evidence is carried only for the CURRENT turn.

Pure and deterministic — no model, no network, no memory write.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from core.conversation_digest import ConversationDigest, estimate_tokens

# Layer budgets are expressed as a share of the turn's token budget so a smaller
# FAST context shrinks every layer proportionally instead of overflowing.
_DIGEST_SHARE = 0.18
_MEMORY_SHARE = 0.25
_TOOL_SHARE = 0.30
# Recent turns get whatever remains; never fewer than this many messages, so the
# conversation never loses its immediate thread to a tight budget.
_MIN_RECENT_MESSAGES = 2
_DEFAULT_BUDGET_TOKENS = 1400

# Content that is cheap to lose, checked before anything substantive is trimmed.
_BOILERPLATE_MARKERS = (
    "hola", "buenas", "gracias", "de nada", "hello", "hi ", "thanks", "thank you",
    "you're welcome", "¡hola", "hasta luego", "adiós", "adios", "bye",
    "¿en qué puedo ayudarte", "en que puedo ayudarte", "how can i help",
)
# Runtime status lines that are useful ONCE and pure noise afterwards.
_STATUS_MARKERS = (
    "no pude completar", "couldn't finish", "respuesta acortada",
    "answer shortened", "el runtime sigue activo", "the runtime is still active",
)


class ContextLayer(str, Enum):
    SYSTEM = "system"
    PINNED = "pinned"
    CURRENT = "current"
    TOOL = "tool"
    MEMORY = "memory"
    RECENT = "recent"
    DIGEST = "digest"


# Layers that must survive any budget. Trimming these would change what is TRUE
# or what is ALLOWED, which a token budget is never permitted to do.
PROTECTED_LAYERS: frozenset[ContextLayer] = frozenset({
    ContextLayer.SYSTEM, ContextLayer.PINNED, ContextLayer.CURRENT,
    ContextLayer.TOOL,
})
# The order trimming walks: cheapest first.
_TRIM_ORDER = (ContextLayer.DIGEST, ContextLayer.RECENT, ContextLayer.MEMORY)


@dataclass
class ComposedContext:
    """The message list for one turn, plus bounded, content-free metrics."""

    messages: list[dict] = field(default_factory=list)
    token_budget: int = _DEFAULT_BUDGET_TOKENS
    estimated_total_tokens: int = 0
    layer_tokens: dict = field(default_factory=dict)
    trimmed_items: int = 0
    trimmed_by_layer: dict = field(default_factory=dict)
    digest_age_turns: int = 0
    cache_key: str = ""
    over_budget: bool = False

    def snapshot(self) -> dict:
        return {
            "estimated_total_tokens": self.estimated_total_tokens,
            "token_budget": self.token_budget,
            "system_tokens": self.layer_tokens.get(ContextLayer.SYSTEM.value, 0),
            "pinned_tokens": self.layer_tokens.get(ContextLayer.PINNED.value, 0),
            "recent_turn_tokens": self.layer_tokens.get(ContextLayer.RECENT.value, 0),
            "digest_tokens": self.layer_tokens.get(ContextLayer.DIGEST.value, 0),
            "tool_evidence_tokens": self.layer_tokens.get(ContextLayer.TOOL.value, 0),
            "memory_evidence_tokens": self.layer_tokens.get(
                ContextLayer.MEMORY.value, 0),
            "trimmed_items": self.trimmed_items,
            "trimmed_by_layer": dict(self.trimmed_by_layer),
            "digest_age_turns": self.digest_age_turns,
            "messages": len(self.messages),
            "over_budget": self.over_budget,
            "cache_key": self.cache_key,
        }


def _tokens(msg: dict) -> int:
    return estimate_tokens(str(msg.get("content") or ""))


def _is_boilerplate(msg: dict) -> bool:
    """Greetings, thanks and repeated runtime status — droppable before content."""
    content = str(msg.get("content") or "").strip().lower()
    if not content:
        return True
    if len(content) <= 60 and any(m in content for m in _BOILERPLATE_MARKERS):
        return True
    return any(m in content for m in _STATUS_MARKERS) and len(content) <= 400


def context_cache_key(*, model: str, role: str, transport: str, num_ctx: int,
                      system_prompt: str, language: str, contract: str,
                      policy_version: str = "m57") -> str:
    """M57.6.2 — the identity of a reusable prompt prefix.

    Every field that changes what the prefix MEANS is in the key: model, role,
    transport, context size, a fingerprint of the system prompt (which carries the
    security instructions, the language directive and the response contract),
    plus an explicit policy version. Change any of them and the key changes, so an
    incompatible prefix can never be reused.

    This names a cache identity; it does NOT claim Ollama reuses a KV cache. That
    is not observable from here and is therefore not asserted anywhere.
    """
    digest = hashlib.sha256((system_prompt or "").encode("utf-8", "replace")).hexdigest()
    return "|".join((
        str(model or ""), str(role or ""), str(transport or ""), str(int(num_ctx or 0)),
        digest[:16], str(language or ""), str(contract or ""), str(policy_version),
    ))


def compose_context(
    *,
    system_prompt: str,
    history: list[dict],
    digest: ConversationDigest | None = None,
    tool_evidence: list[dict] | None = None,
    memory_evidence: str = "",
    token_budget: int = _DEFAULT_BUDGET_TOKENS,
    language: str = "es",
    keep_recent: int = 6,
    cache_key: str = "",
) -> ComposedContext:
    """Assemble the bounded message list for ONE turn.

    ``history`` is the live conversation (the current user message is expected to
    be its last entry). It is never mutated: the composer returns a NEW list.
    """
    budget = max(128, int(token_budget))
    history = [m for m in (history or []) if isinstance(m, dict)]
    out = ComposedContext(token_budget=budget, cache_key=cache_key)
    layer_tokens: dict[str, int] = {layer.value: 0 for layer in ContextLayer}
    trimmed: dict[str, int] = {}

    # ── 1. SYSTEM (never trimmed) ────────────────────────────────────────────
    sys_msg = {"role": "system", "content": system_prompt or ""}
    layer_tokens[ContextLayer.SYSTEM.value] = _tokens(sys_msg)

    # ── 2. PINNED: the operator's EXPLICIT preferences only ──────────────────
    pinned_text = ""
    if digest is not None and not digest.is_empty():
        from core.conversation_digest import Evidence
        explicit = digest.by_evidence(Evidence.EXPLICIT)
        if explicit:
            pinned_text = "\n".join(i.render() for i in explicit)
            layer_tokens[ContextLayer.PINNED.value] = estimate_tokens(pinned_text)

    # ── 3. CURRENT: the message being answered (never trimmed) ───────────────
    current = history[-1] if history and history[-1].get("role") == "user" else None
    older = history[:-1] if current is not None else list(history)
    if current is not None:
        layer_tokens[ContextLayer.CURRENT.value] = _tokens(current)

    # ── 4. TOOL evidence for THIS turn (never trimmed) ───────────────────────
    tools = [m for m in (tool_evidence or []) if isinstance(m, dict)]
    tool_cap = int(budget * _TOOL_SHARE)
    tool_msgs: list[dict] = []
    used = 0
    for msg in tools:
        cost = _tokens(msg)
        if used + cost > tool_cap and tool_msgs:
            trimmed[ContextLayer.TOOL.value] = trimmed.get(ContextLayer.TOOL.value, 0) + 1
            continue
        tool_msgs.append(msg)
        used += cost
    layer_tokens[ContextLayer.TOOL.value] = used

    # ── 5. MEMORY evidence (bounded, trimmed late) ───────────────────────────
    mem_cap = int(budget * _MEMORY_SHARE)
    mem_text = (memory_evidence or "").strip()
    if mem_text and estimate_tokens(mem_text) > mem_cap:
        mem_text = mem_text[: max(0, mem_cap * 4 - 1)] + "…"
        trimmed[ContextLayer.MEMORY.value] = trimmed.get(ContextLayer.MEMORY.value, 0) + 1
    layer_tokens[ContextLayer.MEMORY.value] = estimate_tokens(mem_text)

    # ── 7. DIGEST of the older conversation (trimmed first) ──────────────────
    digest_cap = int(budget * _DIGEST_SHARE)
    digest_text = ""
    if digest is not None and not digest.is_empty():
        digest_text = digest.render(language=language, max_chars=digest_cap * 4)
        layer_tokens[ContextLayer.DIGEST.value] = estimate_tokens(digest_text)
        out.digest_age_turns = digest.turns_covered

    # ── 6. RECENT turns fill what remains ────────────────────────────────────
    fixed = (layer_tokens[ContextLayer.SYSTEM.value]
             + layer_tokens[ContextLayer.PINNED.value]
             + layer_tokens[ContextLayer.CURRENT.value]
             + layer_tokens[ContextLayer.TOOL.value]
             + layer_tokens[ContextLayer.MEMORY.value]
             + layer_tokens[ContextLayer.DIGEST.value])
    remaining = budget - fixed
    pool_size = max(0, int(keep_recent) * 2) if keep_recent else len(older)
    recent_pool = older[-pool_size:] if pool_size else list(older)
    # NO SILENT CAPS: messages excluded by the pool window are dropped just as
    # surely as budget-trimmed ones, so they are counted. A metric that reports 0
    # trimmed while 68 turns were discarded reads as "everything was included".
    if len(older) > len(recent_pool):
        trimmed[ContextLayer.RECENT.value] = trimmed.get(
            ContextLayer.RECENT.value, 0) + (len(older) - len(recent_pool))
    kept: list[dict] = []
    used = 0
    # Newest first: the immediate thread is what a conversation actually needs.
    for msg in reversed(recent_pool):
        cost = _tokens(msg)
        if used + cost <= remaining:
            kept.append(msg)
            used += cost
            continue
        # Over budget from here on. Everything older is dropped, cheapest-first
        # accounting so the metric says WHAT was lost.
        trimmed[ContextLayer.RECENT.value] = trimmed.get(
            ContextLayer.RECENT.value, 0) + 1
    kept.reverse()

    # If the budget was so tight that nothing recent survived, force the minimum
    # thread back in: a turn with no immediate context is incoherent, and the
    # digest cannot substitute for the last exchange.
    if not kept and older:
        kept = older[-_MIN_RECENT_MESSAGES:]
        used = sum(_tokens(m) for m in kept)
    # Drop boilerplate FIRST when still over budget (M57.6 trim priority).
    if used > remaining and kept:
        survivors = [m for m in kept if not _is_boilerplate(m)]
        dropped = len(kept) - len(survivors)
        if dropped and survivors:
            trimmed[ContextLayer.RECENT.value] = trimmed.get(
                ContextLayer.RECENT.value, 0) + dropped
            kept = survivors
            used = sum(_tokens(m) for m in kept)
    layer_tokens[ContextLayer.RECENT.value] = used

    # ── assemble ─────────────────────────────────────────────────────────────
    prefix_parts = [sys_msg["content"]]
    if pinned_text:
        prefix_parts.append(pinned_text)
    if digest_text:
        prefix_parts.append(digest_text)
    if mem_text:
        prefix_parts.append(mem_text)
    messages: list[dict] = [{"role": "system",
                             "content": "\n\n".join(p for p in prefix_parts if p)}]
    messages.extend(kept)
    messages.extend(tool_msgs)
    if current is not None:
        messages.append(current)

    out.messages = messages
    out.layer_tokens = layer_tokens
    out.trimmed_by_layer = trimmed
    out.trimmed_items = sum(trimmed.values())
    out.estimated_total_tokens = sum(layer_tokens.values())
    out.over_budget = out.estimated_total_tokens > budget
    return out


def resolve_context_budget(*, settings=None, num_ctx: int | None = None) -> int:
    """The live-prompt token budget.

    Bounded BELOW the model context so prefill always leaves room for generation:
    a prompt that fills num_ctx has nowhere to put the answer.
    """
    configured = _DEFAULT_BUDGET_TOKENS
    if settings is None:
        try:
            from core.config import settings as _s
            settings = _s
        except Exception:  # noqa: BLE001
            settings = None
    if settings is not None:
        try:
            configured = int(getattr(settings, "response_context_tokens",
                                     _DEFAULT_BUDGET_TOKENS))
        except (TypeError, ValueError):
            configured = _DEFAULT_BUDGET_TOKENS
    if num_ctx:
        # Leave at least a quarter of the window for the answer.
        configured = min(configured, int(num_ctx * 0.75))
    return max(256, configured)


# Bounded last-turn metrics for /context-status and runtime health.
_last_metrics: dict = {}


def publish_context_metrics(metrics: dict) -> None:
    global _last_metrics
    _last_metrics = dict(metrics or {})


def last_context_metrics() -> dict:
    return dict(_last_metrics)
