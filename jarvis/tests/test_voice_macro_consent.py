"""
tests/test_voice_macro_consent.py — V62.0 Phase 6: consent gating in the
voice-macro dispatcher.

core/voice_macros.py's execute_macro() previously dispatched analyze_screen,
take_screenshot, ocr_analyze_screen, vision_room, and vision_screen
unconditionally — the module never imported core.ironman_mode, and no macro
branch checked consent at all. These tests prove each of those five actions
is blocked before it imports/calls the underlying vision_engine/ocr_engine
function when consent is missing, and proceeds when the right surface
(screen or camera) is granted.
"""
from __future__ import annotations

import asyncio

import pytest

from core.ironman_mode import SessionConsent
from core.voice_macros import execute_macro


class _FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []

    async def speak_async(self, text, lang=None):
        self.spoken.append(text)


async def _fake_broadcast(event):
    pass


@pytest.mark.parametrize("action,surface", [
    ("analyze_screen", "screen"),
    ("take_screenshot", "screen"),
    ("ocr_analyze_screen", "screen"),
    ("vision_room", "camera"),
    ("vision_screen", "screen"),
])
def test_capture_macro_blocked_without_consent(action, surface, monkeypatch):
    # If the gate is missing, execute_macro would import these — fail the
    # test loudly instead of silently capturing anything.
    monkeypatch.setattr("core.vision_engine.analyze_screen", None, raising=False)
    monkeypatch.setattr("core.vision_engine.capture_and_save", None, raising=False)
    monkeypatch.setattr("core.vision_engine.analyze_room", None, raising=False)
    monkeypatch.setattr("core.vision_engine.analyze_screen_vision", None, raising=False)
    monkeypatch.setattr("core.ocr_engine.read_screen_and_analyze", None, raising=False)

    tts = _FakeTTS()
    executed = asyncio.run(execute_macro(
        {"action": action, "params": {}}, _fake_broadcast, tts, consent=None,
    ))

    assert executed is False


@pytest.mark.parametrize("action,surface", [
    ("analyze_screen", "screen"),
    ("vision_room", "camera"),
    ("vision_screen", "screen"),
])
def test_capture_macro_proceeds_with_consent_granted(action, surface, monkeypatch):
    called = []

    async def _fake_analyze_screen(*a, **k):
        called.append("analyze_screen")

    async def _fake_analyze_room(*a, **k):
        called.append("analyze_room")
        return "a room"

    async def _fake_analyze_screen_vision(*a, **k):
        called.append("analyze_screen_vision")
        return "a screen"

    monkeypatch.setattr("core.vision_engine.analyze_screen", _fake_analyze_screen, raising=False)
    monkeypatch.setattr("core.vision_engine.analyze_room", _fake_analyze_room, raising=False)
    monkeypatch.setattr("core.vision_engine.analyze_screen_vision", _fake_analyze_screen_vision, raising=False)
    monkeypatch.setattr(
        "core.agent_orchestrator.orchestrator",
        type("O", (), {"_ollama_client": None})(),
        raising=False,
    )

    consent = SessionConsent(**{surface: True})
    tts = _FakeTTS()

    executed = asyncio.run(execute_macro(
        {"action": action, "params": {}}, _fake_broadcast, tts, consent=consent,
    ))

    assert executed is True
    assert called, f"expected the underlying {action} implementation to run once granted"


def test_non_capture_macro_unaffected_by_missing_consent():
    """Consent gating must only touch the five capture actions — everything
    else in the dispatcher keeps working with consent=None."""
    executed = asyncio.run(execute_macro(
        {"action": "punisher_disable", "params": {}}, _fake_broadcast, _FakeTTS(), consent=None,
    ))
    assert executed is True
