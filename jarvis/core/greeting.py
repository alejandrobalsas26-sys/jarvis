"""
core/greeting.py — V69 M54.1.10: one deterministic startup greeting.

The live run greeted the operator with:

    "Pues ahora mismo son [hora actual]."

The literal placeholder reached the user even though core/host_time.py exists and
is correct. The cause was in the prompt, not the clock (main.py:200):

    f"Saluda a {user}. Dile la hora actual y pregúntale en qué lo puedes ayudar. "

It ORDERED the model to state the current time and never SUPPLIED it, so the model
emitted a placeholder for the value it did not have. Two things follow:

  1. Time is a host fact. It is rendered here, deterministically, from HostTime —
     the model is never asked to produce a value only the host knows.
  2. The greeting needs no LLM at all. Generating it made the FIRST Ollama call of
     the session happen before the prompt existed, so the operator waited through a
     cold qwen3:8b load just to be greeted (and, under OLLAMA_MAX_LOADED_MODELS=1,
     it could then be evicted again before their first real question).

Every user-visible string is validated for unresolved placeholders before it is
emitted; if one survives, we log ONE bounded diagnostic and fall back to a safe
deterministic string rather than showing a broken template.
"""
from __future__ import annotations

import re

from core import host_time

# Any of these in a user-visible string means a template was not rendered.
# Deliberately structural (bracketed/braced/angled tokens), not a list of known
# placeholder names — an unknown placeholder is exactly what we must catch.
_PLACEHOLDER_RE = re.compile(
    r"""(
        \[[^\[\]\n]{2,40}\]        |   # [hora actual]
        \{\{[^{}\n]{1,40}\}\}      |   # {{current_time}}
        \{[a-z_][a-z0-9_ ]{1,38}\} |   # {current_time} / {hora actual}
        <[a-z_][a-z0-9_ ]{1,38}>   |   # <current_time>
        TODO_[A-Z_]{2,30}              # TODO_TIME
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# Markdown links/images are legitimately bracketed; never flag them.
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")


def find_placeholders(text: str) -> list[str]:
    """Every unresolved placeholder in `text`. Empty means safe to emit."""
    if not text:
        return []
    scrubbed = _MARKDOWN_LINK_RE.sub("", text)
    return [m.group(0) for m in _PLACEHOLDER_RE.finditer(scrubbed)]


def has_unresolved_placeholder(text: str) -> bool:
    return bool(find_placeholders(text))


def render_greeting(*, name: str, language: str = "es",
                    readiness: str | None = None,
                    include_time: bool = True,
                    now=None) -> str:
    """The deterministic startup greeting. No LLM, no network, no placeholders.

    `now` is an injectable HostTime (tests freeze it); production reads the real
    host clock through core.host_time, the single grounding point.
    """
    ht = now if now is not None else host_time.now()
    lang = (language or "es").lower()
    english = lang.startswith("en")

    parts: list[str] = []
    if english:
        parts.append(f"Hello, {name}.")
        if include_time:
            parts.append(f"It's {ht.time_hms()}.")
        parts.append(readiness or "JARVIS is ready.")
    else:
        parts.append(f"Hola, {name}.")
        if include_time:
            parts.append(f"Son las {ht.time_hms()}.")
        parts.append(readiness or "JARVIS está listo.")

    text = " ".join(p for p in parts if p)
    return _validated(text, name=name, english=english)


def _validated(text: str, *, name: str, english: bool) -> str:
    """Never show a broken template. One bounded diagnostic, then a safe string.

    The fallback must itself be placeholder-free UNCONDITIONALLY: interpolating an
    unvalidated `name` here would leak the very thing we are suppressing (a name of
    "{user_name}" would produce "Hola, {user_name}."), so the name is scrubbed and
    dropped entirely when it is not clean.
    """
    found = find_placeholders(text)
    if not found:
        return text
    try:
        from loguru import logger
        logger.warning(
            f"GREETING: refusing to emit {len(found)} unresolved placeholder(s) "
            f"— using safe fallback"
        )
    except Exception:
        pass
    safe_name = name if name and not find_placeholders(name) else ""
    if english:
        return f"Hello, {safe_name}." if safe_name else "Hello."
    return f"Hola, {safe_name}." if safe_name else "Hola."


def safe_user_text(text: str, *, fallback: str) -> str:
    """Guard for any user-visible string that came from a template or a model.

    A model must never emit an unresolved placeholder to the operator; if one
    appears we substitute `fallback` and log ONE bounded diagnostic.
    """
    if not has_unresolved_placeholder(text):
        return text
    try:
        from loguru import logger
        logger.warning("OUTPUT: unresolved placeholder suppressed — safe fallback used")
    except Exception:
        pass
    return fallback
