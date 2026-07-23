"""
tests/test_response_surface.py — V63 Milestone 6: response-surface routing.

Proves: reasoning result is rendered per surface WITHOUT re-reasoning; lossless
surfaces are verbatim; VOICE strips markup but preserves prose words; HUD /
NOTIFICATION are bounded; and the live TTS consumer (_run_turn) speaks the VOICE
rendering while reasoning truth is unchanged.
"""
from __future__ import annotations

import asyncio

from core.response_surface import (
    LOSSLESS_SURFACES,
    ResponseSurface,
    render,
    select_surface,
    strip_markup,
)

MARKDOWN = (
    "# Findings\n\n"
    "The **critical** issue is in `auth.py`. See the table:\n\n"
    "| col | val |\n| --- | --- |\n| a | 1 |\n\n"
    "- first point\n- second point\n\n"
    "```python\nprint('secret')\n```\n"
    "More at [the docs](https://example.com/x)."
)


def test_lossless_surfaces_are_verbatim():
    for surface in (ResponseSurface.TEXT, ResponseSurface.TECHNICAL, ResponseSurface.REPORT):
        assert render(MARKDOWN, surface) == MARKDOWN
        assert surface in LOSSLESS_SURFACES


def test_voice_strips_markup_but_keeps_prose_words():
    spoken = render(MARKDOWN, ResponseSurface.VOICE)
    # Markup gone
    for token in ("**", "`", "#", "|", "```", "](", "http"):
        assert token not in spoken, f"{token!r} must not survive into VOICE"
    # Prose words preserved (reasoning truth unchanged)
    for word in ("critical", "issue", "auth.py", "first point", "second point", "the docs"):
        assert word in spoken


def test_voice_is_identity_for_plain_prose():
    prose = "The scan finished. No anomalies were found on the host."
    assert render(prose, ResponseSurface.VOICE) == prose


def test_hud_is_bounded_and_stripped():
    out = render(MARKDOWN, ResponseSurface.HUD)
    assert len(out) <= 280
    assert "**" not in out and "`" not in out
    assert "\n" not in out  # collapsed to a single block


def test_notification_is_one_line_and_short():
    out = render(MARKDOWN, ResponseSurface.NOTIFICATION)
    assert len(out) <= 140
    assert "\n" not in out


def test_notification_takes_first_sentence():
    text = "Host compromised. Additional detail follows here with more words."
    out = render(text, ResponseSurface.NOTIFICATION)
    assert out.startswith("Host compromised.")


def test_render_does_not_call_any_model():
    # Purely synchronous/pure — no awaitables, no I/O. Sanity: many calls, fast.
    for _ in range(1000):
        render(MARKDOWN, ResponseSurface.VOICE)


def test_select_surface_priority():
    assert select_surface(voice=True) is ResponseSurface.VOICE
    assert select_surface(notification=True, voice=True) is ResponseSurface.NOTIFICATION
    assert select_surface(technical=True) is ResponseSurface.TECHNICAL
    assert select_surface(report=True, technical=True) is ResponseSurface.REPORT
    assert select_surface(hud=True) is ResponseSurface.HUD
    assert select_surface() is ResponseSurface.TEXT


def test_strip_markup_empty():
    assert strip_markup("") == ""
    assert render("", ResponseSurface.VOICE) == ""


# ── Live wiring: the TTS leg of _run_turn speaks the VOICE rendering ─────────

class _MarkdownLLM:
    async def chat_stream(self, user_message: str):
        yield "Use the **bold** `code` value here."


class _CapturingTTS:
    def __init__(self):
        self.spoken: list[str] = []

    async def speak_async(self, text: str, lang: str | None = None, **kw) -> None:
        # V69 M57.4 — accept the real API's keyword-only priority/coalesce_key.
        self.spoken.append(text)


def test_run_turn_speaks_voice_rendered_text():
    import main

    llm = _MarkdownLLM()
    tts = _CapturingTTS()
    asyncio.run(main._run_turn(llm, tts, "q", "Operator"))

    spoken = " ".join(tts.spoken)
    assert "**" not in spoken and "`" not in spoken, "TTS must not speak raw markdown"
    assert "bold" in spoken and "code" in spoken and "value" in spoken
