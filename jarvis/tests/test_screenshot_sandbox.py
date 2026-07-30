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

import os
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

@pytest.fixture
def relocated_roots(monkeypatch, tmp_path):
    """Pin the allowed roots AND the CWD into an isolated tree.

    V69 M61.7 — these traversal fixtures previously resolved against the real
    process CWD, and ``_sandbox_allowed_dirs()`` includes ``Path.cwd()``. This
    checkout lives under ``~/Downloads``, so from the APPLICATION directory
    ``../../../etc/passwd.png`` resolves to ``~/Downloads/etc/passwd.png`` — which
    is genuinely INSIDE an allowed root. The sandbox was right to allow it and the
    test was wrong to demand a refusal: the two pytest layouts disagreed about
    whether one string was an escape, which says nothing about containment.

    Relocating home and the CWD into ``tmp_path`` makes "outside" mean outside on
    every host and from every working directory. Same discipline as the M61
    ``sandbox`` fixture in tests/test_security.py.
    """
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Documents").mkdir(parents=True)
    project = home / "Downloads" / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: project))
    monkeypatch.chdir(project)
    return {"home": home, "project": project}


def _deterministic_escapes(roots: dict) -> list[str]:
    """Relative paths that leave EVERY allowed root, computed from the real depth."""
    depth = len(roots["project"].parts)
    return [
        "/".join([".."] * depth) + "/etc/passwd.png",
        "sub/" + "/".join([".."] * (depth + 1)) + "/etc/passwd.png",
    ]


def _allowed_roots(roots: dict) -> list[Path]:
    return [
        (roots["home"] / "Downloads").resolve(),
        (roots["home"] / "Documents").resolve(),
        roots["project"].resolve(),
    ]


def test_screenshot_rejects_deterministic_traversal(monkeypatch, relocated_roots):
    """A path that escapes every allowed root is refused, on any host and any CWD."""
    for bad_path in _deterministic_escapes(relocated_roots):
        captures, saves = _install_fake_pyautogui(monkeypatch)
        te = ToolExecutor(consent=SessionConsent(screen=True))

        result = te.execute("take_screenshot", {"save_path": bad_path})

        assert "error" in result, bad_path
        assert "seguridad" in result["error"].lower()
        assert captures == [], "screen must never be captured for a rejected path"
        assert saves == []


def test_the_escape_fixtures_are_not_vacuous(relocated_roots):
    """Guard: the deterministic escapes must really be outside every allowed root."""
    allowed = _allowed_roots(relocated_roots)
    for bad_path in _deterministic_escapes(relocated_roots):
        resolved = (relocated_roots["project"] / bad_path).resolve()
        assert not any(resolved.is_relative_to(root) for root in allowed), bad_path


def test_screenshot_rejects_home_relative_secret_path(monkeypatch, relocated_roots):
    """``~`` expands to home, which is NOT itself an allowed root."""
    captures, saves = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor(consent=SessionConsent(screen=True))

    result = te.execute("take_screenshot", {"save_path": "~/.ssh/id_rsa.png"})

    assert "error" in result
    assert "seguridad" in result["error"].lower()
    assert captures == []
    assert saves == []


@pytest.mark.parametrize(
    "traversal",
    [
        "../../../etc/passwd.png",
        "..\\..\\..\\Windows\\System32\\evil.png",
    ],
)
def test_cwd_relative_traversal_is_contained_not_merely_refused(
    monkeypatch, relocated_roots, traversal
):
    """The invariant that actually matters, and that holds in BOTH pytest layouts.

    These two literals are the historical regression fixtures. Whether they *escape*
    depends on how deep the CWD sits below the allowed roots, so asserting "refused"
    made the outcome a property of the checkout location — which is why the two
    invocations disagreed. What must hold unconditionally is that the handler never
    writes outside an allowed root: either the path is refused with no capture, or it
    resolved inside a root and was contained there.
    """
    captures, saves = _install_fake_pyautogui(monkeypatch)
    te = ToolExecutor(consent=SessionConsent(screen=True))

    result = te.execute("take_screenshot", {"save_path": traversal})

    if "error" in result:
        assert captures == [], "no capture may happen for a refused path"
        assert saves == []
        return

    written = Path(result["saved"]).resolve()
    assert any(
        written.is_relative_to(root) for root in _allowed_roots(relocated_roots)
    ), f"{written} escaped every allowed root"


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


# ── V69 M61 RC1: the sandbox verdict must not depend on the host flavour ─────
# Found only once the conftest GUI stubs let this suite actually run: the
# ``..\..\..\Windows\System32\evil.png`` case had been failing in the test
# HARNESS (monkeypatch importing a missing pyautogui), which masked the fact that
# the guard accepted the payload and reported {"saved": ".../..\\..\\..\\Windows
# \\System32\\evil.png"}. ``pathlib`` binds to the host flavour, so on POSIX the
# whole string is one filename that lands inside an allowed root — while the same
# string is a genuine escape on the Windows deployment target.
@pytest.mark.skipif(os.name == "nt",
                    reason="on Windows both separators are legitimate and Path "
                           "normalises them; this asserts the POSIX direction")
@pytest.mark.parametrize("payload", [
    r"..\..\..\Windows\System32\evil.png",
    r"..\evil.png",
    r"C:\Windows\System32\evil.png",
    r"C:evil.png",
    r"\\server\share\evil.png",
    "Downloads\\evil.png",
])
def test_windows_shaped_paths_are_refused_on_a_posix_host(monkeypatch, tmp_path,
                                                          payload):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    assert _resolve_within_allowed(payload) is None, (
        "a Windows-flavour path was accepted on POSIX; it is an escape on the "
        "deployment target"
    )


def test_posix_paths_inside_the_sandbox_are_still_accepted(monkeypatch, tmp_path):
    """Positive control: the fix must not reject legitimate in-scope paths."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "capture.png"
    resolved = _resolve_within_allowed(str(target))
    assert resolved is not None and resolved == target.resolve()
