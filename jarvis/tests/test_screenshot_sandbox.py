"""
tests/test_screenshot_sandbox.py — V63 Milestone 0 (safety closure).

take_screenshot's ``save_path`` was previously written verbatim with no
containment check (V62 residual risk #8), unlike read_file/write_file. These
tests prove the save_path is now contained to the same allowed roots
(Downloads / Documents / project cwd) via the shared ``_resolve_within_allowed``
helper, that the consent gate still runs *before* any path handling, and that
the screen is never captured when a path is rejected.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.ironman_mode import SessionConsent
from tools.executor import ToolExecutor, _resolve_within_allowed


class _FakeShot:
    """Stand-in for a pyautogui screenshot; records the path it was saved to."""

    def __init__(self, sink: list[str]):
        self._sink = sink

    def save(self, path):
        self._sink.append(str(path))


def _install_fake_pyautogui(monkeypatch) -> tuple[list[int], list[str]]:
    """Patch pyautogui.screenshot; return (capture_calls, save_calls) sinks."""
    captures: list[int] = []
    saves: list[str] = []

    def _screenshot():
        captures.append(1)
        return _FakeShot(saves)

    monkeypatch.setattr("pyautogui.screenshot", _screenshot)
    return captures, saves


# ── Live handler: valid paths ────────────────────────────────────────────────

def test_screenshot_allows_path_inside_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captures, saves = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor(consent=SessionConsent(screen=True))

    save_path = str(tmp_path / "Downloads" / "shot.png")
    result = te.execute("take_screenshot", {"save_path": save_path})

    assert "error" not in result
    assert result["saved"] == save_path
    assert captures == [1]
    assert saves == [save_path]


def test_screenshot_default_path_lands_in_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captures, saves = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor(consent=SessionConsent(screen=True))

    result = te.execute("take_screenshot", {})

    assert "error" not in result
    saved = Path(result["saved"])
    assert saved.parent == (tmp_path / "Downloads")
    assert saved.suffix == ".png"
    assert captures == [1]


# ── Live handler: rejected paths (fail-closed, no capture) ───────────────────

@pytest.mark.parametrize(
    "bad_path",
    [
        "../../../etc/passwd.png",
        "..\\..\\..\\Windows\\System32\\evil.png",
        "~/.ssh/id_rsa.png",
    ],
)
def test_screenshot_rejects_traversal(monkeypatch, bad_path):
    captures, saves = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor(consent=SessionConsent(screen=True))

    result = te.execute("take_screenshot", {"save_path": bad_path})

    assert "error" in result
    assert "seguridad" in result["error"].lower()
    assert captures == [], "screen must never be captured for a rejected path"
    assert saves == []


def test_screenshot_rejects_absolute_outside(monkeypatch, tmp_path):
    # tmp_path is a system temp dir — outside real Downloads/Documents/cwd.
    captures, _ = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor(consent=SessionConsent(screen=True))

    result = te.execute("take_screenshot", {"save_path": str(tmp_path / "evil.png")})

    assert "error" in result
    assert "seguridad" in result["error"].lower()
    assert captures == []


def test_consent_gate_precedes_path_check(monkeypatch):
    """No screen consent must short-circuit before any capture, even for a
    malicious path — the consent error, not a path error, is returned."""
    captures, _ = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor()  # default consent: screen=False

    result = te.execute("take_screenshot", {"save_path": "../../../etc/evil.png"})

    assert "error" in result
    assert "consent" in result["error"].lower()
    assert captures == []


# ── Helper unit tests ────────────────────────────────────────────────────────

def test_resolve_within_allowed_accepts_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    target = tmp_path / "Downloads" / "ok.png"

    resolved = _resolve_within_allowed(str(target))

    assert resolved == target.resolve()


@pytest.mark.parametrize("bad", ["", "   ", "../secret", "/etc/passwd"])
def test_resolve_within_allowed_rejects(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    assert _resolve_within_allowed(bad) is None


def test_resolve_within_allowed_rejects_non_str(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_within_allowed(None) is None  # type: ignore[arg-type]


def test_resolve_within_allowed_rejects_symlink_escape(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(b"x")

    link = downloads / "link.png"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    # The symlink lives inside an allowed dir, but resolves outside → rejected.
    assert _resolve_within_allowed(str(link)) is None
