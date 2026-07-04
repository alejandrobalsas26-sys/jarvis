"""
tests/test_consent_gating.py — V62.0 Phase 6: consent enforcement at real
capture call sites.

Screen/OCR/clipboard tools previously executed unconditionally — no import
of core.ironman_mode anywhere in tools/executor.py, no consent check at all.
These tests prove ToolExecutor.consent (core.ironman_mode.SessionConsent,
defaults fully OFF) actually blocks each handler before it touches
pyautogui/pyperclip/pytesseract, and that granting consent unblocks it.
"""
from __future__ import annotations

from core.ironman_mode import SessionConsent
from tools.executor import ToolExecutor


def test_default_consent_is_fully_revoked():
    te = ToolExecutor()
    assert te.consent.screen is False
    assert te.consent.clipboard is False
    assert te.consent.camera is False


def test_screenshot_blocked_without_screen_consent(monkeypatch):
    te = ToolExecutor()
    calls = []
    monkeypatch.setattr("pyautogui.screenshot", lambda: calls.append(1))

    result = te.execute("take_screenshot", {})

    assert "error" in result
    assert "consent" in result["error"].lower()
    assert not calls, "pyautogui.screenshot must never run without consent"


def test_screenshot_allowed_with_screen_consent(monkeypatch, tmp_path):
    te = ToolExecutor(consent=SessionConsent(screen=True))

    class _FakeShot:
        def save(self, path):
            pass

    monkeypatch.setattr("pyautogui.screenshot", lambda: _FakeShot())

    result = te.execute("take_screenshot", {"save_path": str(tmp_path / "x.png")})

    assert "error" not in result
    assert result["saved"] == str(tmp_path / "x.png")


def test_ocr_scan_blocked_without_screen_consent(monkeypatch):
    te = ToolExecutor()
    calls = []
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: calls.append(1))

    result = te.execute("escanear_pantalla", {})

    assert "error" in result
    assert "consent" in result["error"].lower()
    assert not calls, "screen grab must never run without consent"


def test_get_clipboard_blocked_without_clipboard_consent(monkeypatch):
    te = ToolExecutor()
    calls = []
    monkeypatch.setattr("pyperclip.paste", lambda: calls.append(1) or "secret")

    result = te.execute("get_clipboard", {})

    assert "error" in result
    assert "consent" in result["error"].lower()
    assert not calls, "pyperclip.paste must never run without consent"


def test_get_clipboard_allowed_with_clipboard_consent(monkeypatch):
    te = ToolExecutor(consent=SessionConsent(clipboard=True))
    monkeypatch.setattr("pyperclip.paste", lambda: "hello")

    result = te.execute("get_clipboard", {})

    assert result == {"clipboard": "hello"}


def test_set_clipboard_blocked_without_clipboard_consent(monkeypatch):
    te = ToolExecutor()
    calls = []
    monkeypatch.setattr("pyperclip.copy", lambda text: calls.append(text))

    result = te.execute("set_clipboard", {"text": "hello"})

    assert "error" in result
    assert not calls, "pyperclip.copy must never run without consent"


def test_set_clipboard_allowed_with_clipboard_consent(monkeypatch):
    te = ToolExecutor(consent=SessionConsent(clipboard=True))
    monkeypatch.setattr("pyperclip.copy", lambda text: None)

    result = te.execute("set_clipboard", {"text": "hello"})

    assert result == {"status": "copied", "length": 5}


def test_screen_consent_does_not_grant_clipboard():
    """Consent is per-surface — granting one must never leak into another."""
    te = ToolExecutor(consent=SessionConsent(screen=True))
    result = te.execute("get_clipboard", {})
    assert "error" in result
