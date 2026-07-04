"""
tests/test_telegram_hud_consent.py — V62.0 Phase 6: Telegram /hud consent gate.

core/telegram_bridge.py's _cmd_hud captured a full desktop screenshot on a
remote /hud command, gated only by a chat_id whitelist (authentication, not
consent) — core.ironman_mode.SessionConsent was never consulted. /hud now
also requires the shared session's screen consent.
"""
from __future__ import annotations

import asyncio

import core.telegram_bridge as tb
from core.ironman_mode import SessionConsent


class _FakeMessage:
    def __init__(self):
        self.texts: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, chat_id):
        self.effective_chat = _FakeChat(chat_id)
        self.message = _FakeMessage()


def test_hud_blocked_without_consent(monkeypatch):
    monkeypatch.setattr(tb, "_consent", None)
    monkeypatch.setattr(tb, "_auth", lambda update: True)
    calls = []
    monkeypatch.setattr("core.vision_engine._capture_screen", lambda: calls.append(1), raising=False)

    update = _FakeUpdate(chat_id=123)
    asyncio.run(tb._cmd_hud(update, context=None))

    assert not calls, "screenshot must never run without consent"
    assert update.message.texts
    assert "screen access" in update.message.texts[-1].lower()


def test_hud_blocked_when_consent_denies_screen(monkeypatch):
    monkeypatch.setattr(tb, "_consent", SessionConsent(screen=False))
    monkeypatch.setattr(tb, "_auth", lambda update: True)
    calls = []
    monkeypatch.setattr("core.vision_engine._capture_screen", lambda: calls.append(1), raising=False)

    update = _FakeUpdate(chat_id=123)
    asyncio.run(tb._cmd_hud(update, context=None))

    assert not calls


def test_hud_proceeds_with_screen_consent_granted(monkeypatch, tmp_path):
    monkeypatch.setattr(tb, "_consent", SessionConsent(screen=True))
    monkeypatch.setattr(tb, "_auth", lambda update: True)

    calls = []
    monkeypatch.setattr("core.vision_engine._capture_screen", lambda: b"data", raising=False)
    monkeypatch.setattr(
        "core.vision_engine._save_screenshot",
        lambda data, tag: calls.append(tag) or (tmp_path / "x.png"),
        raising=False,
    )

    async def _fake_push(*a, **k):
        pass

    monkeypatch.setattr(tb, "_push", _fake_push)

    update = _FakeUpdate(chat_id=123)
    asyncio.run(tb._cmd_hud(update, context=None))

    assert calls == ["telegram_hud"]


def test_unauthorized_chat_id_still_rejected_regardless_of_consent(monkeypatch):
    """Consent gating must not weaken the existing chat_id authentication."""
    monkeypatch.setattr(tb, "_consent", SessionConsent(screen=True))
    # deliberately do NOT patch _auth — real _CHAT_ID default is 0, so any
    # nonzero chat id must be rejected.
    monkeypatch.setattr(tb, "_CHAT_ID", 999)
    calls = []
    monkeypatch.setattr("core.vision_engine._capture_screen", lambda: calls.append(1), raising=False)

    update = _FakeUpdate(chat_id=123)
    asyncio.run(tb._cmd_hud(update, context=None))

    assert not calls
    assert not update.message.texts, "unauthorized chat must get silent rejection, not even a consent message"
