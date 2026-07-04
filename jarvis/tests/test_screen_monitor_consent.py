"""
tests/test_screen_monitor_consent.py — V62.0 Phase 6: screen_monitor consent gate.

core/screen_monitor.py's background poll loop captured the screen every N
seconds whenever JARVIS_SCREEN_MONITOR=1 was set, with no consent check at
all — env config opting the *feature* in is not the same as operator consent
for the current session. Each poll now also requires consent.screen.
"""
from __future__ import annotations

import asyncio
import contextlib

import core.screen_monitor as sm
from core.ironman_mode import SessionConsent


async def _fake_broadcast(event):
    pass


def test_poll_skips_capture_without_consent(monkeypatch):
    monkeypatch.setattr(sm, "_ENABLED", True)
    monkeypatch.setattr(sm, "_POLL_INTERVAL", 0.01)
    calls = []
    monkeypatch.setattr("core.vision_engine._capture_screen", lambda: calls.append(1), raising=False)

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                sm.start_screen_monitor(_fake_broadcast, None, consent=None),
                timeout=0.2,
            )

    asyncio.run(_run())
    assert not calls, "screen capture must never run without consent, regardless of env config"


def test_poll_skips_capture_when_consent_object_denies_screen(monkeypatch):
    monkeypatch.setattr(sm, "_ENABLED", True)
    monkeypatch.setattr(sm, "_POLL_INTERVAL", 0.01)
    calls = []
    monkeypatch.setattr("core.vision_engine._capture_screen", lambda: calls.append(1), raising=False)

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                sm.start_screen_monitor(_fake_broadcast, None, consent=SessionConsent(screen=False)),
                timeout=0.2,
            )

    asyncio.run(_run())
    assert not calls


def test_poll_captures_when_consent_granted(monkeypatch):
    monkeypatch.setattr(sm, "_ENABLED", True)
    monkeypatch.setattr(sm, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sm, "_last_hash", None)
    calls = []

    def _fake_capture():
        calls.append(1)
        return b"\x00" * 100

    async def _fake_analyze_image(*a, **k):
        return ""

    monkeypatch.setattr("core.vision_engine._capture_screen", _fake_capture, raising=False)
    monkeypatch.setattr("core.vision_engine.analyze_image", _fake_analyze_image, raising=False)

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                sm.start_screen_monitor(_fake_broadcast, None, consent=SessionConsent(screen=True)),
                timeout=0.3,
            )

    asyncio.run(_run())
    assert calls, "screen capture must run once consent is granted"
