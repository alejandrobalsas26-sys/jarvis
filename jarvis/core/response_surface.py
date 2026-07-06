"""
core/response_surface.py — V63 Milestone 6: response-surface routing.

Separates *reasoning depth* from *presentation format*. One reasoning result
(the final answer text produced once by chat_stream) is rendered into a
surface-appropriate form **without re-running the model**. This closes the
V62-deferred "brief in voice, detailed in technical surfaces" gap (V62 residual
risk #6) — previously response tone/brevity was 100% persona-prompt-driven and
TTS spoke raw markdown aloud.

Invariant: rendering changes *presentation only*, never reasoning truth. The
lossless surfaces (TEXT / TECHNICAL / REPORT) preserve the answer verbatim; VOICE
strips markup that would be nonsense when spoken (code fences, backticks, table
pipes) but keeps every prose word; HUD / NOTIFICATION are explicitly
length-bounded summaries (lossy-by-design, documented, never fabricating).

Pure and dependency-free — safe to call per-sentence on the hot streaming path.
"""
from __future__ import annotations

import re
from enum import Enum


class ResponseSurface(str, Enum):
    VOICE = "voice"                # concise, natural, no markup/tables/code
    TEXT = "text"                  # normal detail (verbatim)
    HUD = "hud"                    # compact, telemetry-friendly, bounded
    TECHNICAL = "technical"        # implementation detail (verbatim)
    REPORT = "report"              # long-form (verbatim)
    NOTIFICATION = "notification"  # one-line actionable alert


# Surfaces that must reproduce the reasoning result verbatim (lossless).
LOSSLESS_SURFACES: frozenset[ResponseSurface] = frozenset({
    ResponseSurface.TEXT, ResponseSurface.TECHNICAL, ResponseSurface.REPORT,
})

_HUD_MAX_CHARS = 280
_NOTIFICATION_MAX_CHARS = 140

# ── Markup-stripping regexes (compiled once) ─────────────────────────────────
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_BOLD_ITALIC_RE = re.compile(r"(\*\*|\*|__|_)(.+?)\1")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_LIST_NUM_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"[ \t]{2,}")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def strip_markup(text: str, *, code_placeholder: str = " ") -> str:
    """Remove Markdown markup so text reads naturally when spoken/compacted.

    Preserves every prose word; only removes syntax (fences, backticks, emphasis
    markers, headers, list bullets, table pipes) and replaces links with their
    label / URLs with a neutral token.
    """
    if not text:
        return ""
    out = _FENCED_CODE_RE.sub(code_placeholder, text)
    out = _INLINE_CODE_RE.sub(r"\1", out)
    # Apply emphasis stripping twice to handle nested/adjacent markers.
    out = _BOLD_ITALIC_RE.sub(r"\2", out)
    out = _BOLD_ITALIC_RE.sub(r"\2", out)
    out = _HEADER_RE.sub("", out)
    out = _BLOCKQUOTE_RE.sub("", out)
    out = _TABLE_SEP_RE.sub("", out)          # drop |---|---| separator rows
    out = _LIST_BULLET_RE.sub("", out)
    out = _LIST_NUM_RE.sub("", out)
    out = _MD_LINK_RE.sub(r"\1", out)         # [label](url) -> label
    out = _BARE_URL_RE.sub("", out)
    out = out.replace("|", " ")               # residual table cell separators
    out = _WS_RE.sub(" ", out)
    out = _MULTI_NL_RE.sub("\n\n", out)
    # Trim trailing spaces per line without dropping intentional blank lines.
    out = "\n".join(line.rstrip() for line in out.splitlines())
    return out.strip()


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    m = re.search(r"(.+?[.!?])(\s|$)", stripped, re.DOTALL)
    return (m.group(1) if m else stripped).strip()


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    return cut + "…"


def render(text: str, surface: ResponseSurface, *, max_chars: int | None = None) -> str:
    """Render one reasoning result for *surface*. Never calls the model.

    - TEXT / TECHNICAL / REPORT: verbatim (lossless).
    - VOICE: markup stripped, single spaces, natural for TTS.
    - HUD: markup stripped, collapsed to one block, bounded (~280 chars).
    - NOTIFICATION: markup stripped, first sentence, one line, bounded (~140).
    """
    text = text or ""
    if surface in LOSSLESS_SURFACES:
        return text

    if surface is ResponseSurface.VOICE:
        return strip_markup(text)

    if surface is ResponseSurface.HUD:
        compact = " ".join(strip_markup(text).split())
        return _truncate(compact, max_chars or _HUD_MAX_CHARS)

    if surface is ResponseSurface.NOTIFICATION:
        one_line = " ".join(strip_markup(text).split())
        return _truncate(_first_sentence(one_line), max_chars or _NOTIFICATION_MAX_CHARS)

    return text  # unreachable, defensive


def select_surface(
    *,
    voice: bool = False,
    notification: bool = False,
    technical: bool = False,
    report: bool = False,
    hud: bool = False,
) -> ResponseSurface:
    """Map coarse turn context to a surface. Priority: notification > voice >
    report > technical > hud > text. Callers that already know the surface pass
    it directly; this is the default resolver used by the runtime layer (M1)."""
    if notification:
        return ResponseSurface.NOTIFICATION
    if voice:
        return ResponseSurface.VOICE
    if report:
        return ResponseSurface.REPORT
    if technical:
        return ResponseSurface.TECHNICAL
    if hud:
        return ResponseSurface.HUD
    return ResponseSurface.TEXT
