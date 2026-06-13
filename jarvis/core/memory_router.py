"""
core/memory_router.py — Memory discipline gate (V60.0, Phase 6).

Pure, dependency-free policy layer that decides WHETHER and WHERE a turn should
touch persistent memory, and refuses to let secrets enter it. It does not store
anything itself — the existing memory subsystems (episodic_memory, session_*,
knowledge vault) call these predicates before writing.

Design rules:
  - Never persist API keys, tokens, passwords, cookies, or private keys.
  - Treat web / file / RAG context as UNTRUSTED (callers tag it; we expose
    `is_untrusted_source`).
  - project  → repo architecture / decisions (stable across a project).
  - session  → ephemeral task context for the current conversation.
  - long_term→ stable user preferences / durable project facts only.
  - none     → transient chatter; do not persist.
"""
from __future__ import annotations

import re
from typing import Literal

MemoryScope = Literal["session", "project", "long_term", "none"]


# ── Secret detection ─────────────────────────────────────────────────────────
# Conservative, high-precision patterns. We would rather miss an exotic format
# than leak — but these cover the common, high-value credential shapes.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|contraseña)\b\s*[:=]\s*\S{4,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                       # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),               # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # AWS access key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),             # Slack tokens
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(set-)?cookie\b\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),  # JWT
)


def contains_secret(text: str) -> bool:
    """True if *text* looks like it contains a credential / secret."""
    if not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    """Replace any detected secret span with a redaction marker."""
    if not text:
        return text
    out = text
    for p in _SECRET_PATTERNS:
        out = p.sub("[REDACTED-SECRET]", out)
    return out


# ── Read / write decisions ───────────────────────────────────────────────────
_RECALL_KW = (
    "remember", "recall", "earlier", "previously", "last time", "what did",
    "as i said", "we discussed", "my preference", "you know that",
    "recuerda", "recordás", "recordas", "antes", "anteriormente", "la última vez",
    "ya te dije", "como dije", "mi preferencia", "lo que hablamos",
)
_PERSIST_KW = (
    "remember this", "save this", "note that", "keep in mind", "from now on",
    "always", "my name is", "i prefer", "i use", "i'm working on",
    "recuerda esto", "anota", "guarda", "ten en cuenta", "de ahora en adelante",
    "siempre", "me llamo", "prefiero", "estoy trabajando en",
)
_PROJECT_KW = (
    "architecture", "module", "repo", "codebase", "design decision", "refactor",
    "we use", "the project", "this project", "convention",
    "arquitectura", "módulo", "modulo", "proyecto", "decisión", "convención",
)
_LONGTERM_KW = (
    "my name", "i prefer", "i always", "i live in", "my timezone", "my role",
    "from now on", "default to",
    "me llamo", "mi nombre", "prefiero", "siempre uso", "vivo en", "mi rol",
)


def _has(text: str, vocab: tuple[str, ...]) -> bool:
    return any(kw in text for kw in vocab)


def should_use_memory(prompt: str) -> bool:
    """True if answering *prompt* should consult prior memory/context."""
    text = (prompt or "").lower()
    if _has(text, _RECALL_KW):
        return True
    # Questions referencing continuity ("the", "this") + project nouns benefit.
    return _has(text, _PROJECT_KW) and ("?" in (prompt or "") or _has(text, ("what", "qué", "que", "how", "cómo", "como")))


def should_write_memory(prompt: str, answer: str) -> bool:
    """True if this turn produced something worth persisting — and it is safe."""
    if contains_secret(prompt) or contains_secret(answer):
        return False
    text = (prompt or "").lower()
    if _has(text, _PERSIST_KW):
        return True
    # Durable project/long-term facts are worth keeping even if not explicitly asked.
    return _has(text, _LONGTERM_KW) or _has(text, _PROJECT_KW)


def classify_memory_scope(prompt: str) -> MemoryScope:
    """Bucket *prompt* into the narrowest appropriate persistence scope."""
    text = (prompt or "").lower()
    if contains_secret(prompt):
        return "none"
    if _has(text, _LONGTERM_KW):
        return "long_term"
    if _has(text, _PROJECT_KW):
        return "project"
    if _has(text, _PERSIST_KW) or _has(text, _RECALL_KW):
        return "session"
    return "none"


def is_untrusted_source(source: str | None) -> bool:
    """Web / file / RAG / tool-derived context is untrusted for memory writes."""
    if not source:
        return False
    return source.strip().lower() in {
        "web", "url", "fetch", "file", "rag", "tool", "search", "scrape", "external",
    }
