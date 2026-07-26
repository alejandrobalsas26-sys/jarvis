"""
tests/conftest.py — JARVIS V55.0 TITAN test factory.
Platform-transparent: mocks Windows-only modules on Linux CI,
provides RBAC actor fixtures, fake DB/Redis/SIEM URLs, and safe subprocess stubs.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


# ── Windows-only module stubs — applied at collection time ───────────────────

def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_win32_stubs() -> None:
    if sys.platform == "win32":
        return  # real pywin32 is available

    stubs = {
        "win32evtlog": {
            "OpenEventLog": MagicMock(), "ReadEventLog": MagicMock(),
            "CloseEventLog": MagicMock(), "EVENTLOG_BACKWARDS_READ": 8,
            "EVENTLOG_SEQUENTIAL_READ": 1,
        },
        "win32evtlogutil": {"SafeFormatMessage": MagicMock()},
        "win32api": {
            "GetLastError": MagicMock(return_value=0),
            "RegOpenKey": MagicMock(), "RegQueryValueEx": MagicMock(),
        },
        "win32con": {
            "HKEY_LOCAL_MACHINE": 0x80000002, "KEY_READ": 0x20019,
            "KEY_WRITE": 0x20006, "SW_HIDE": 0, "GENERIC_READ": 0x80000000,
        },
        "win32security": {"GetFileSecurity": MagicMock(), "OWNER_SECURITY_INFORMATION": 1},
        "pythoncom": {"CoInitialize": MagicMock(), "CoUninitialize": MagicMock()},
        "pywintypes": {"error": Exception, "Time": MagicMock()},
        "wmi": {"WMI": MagicMock()},
        "pywintrace": {"ETWProvider": MagicMock()},
    }

    for name, attrs in stubs.items():
        if name not in sys.modules:
            sys.modules[name] = _stub(name, **attrs)

    # Scapy stub — raw sockets unavailable without root on Linux CI
    if "scapy" not in sys.modules:
        sys.modules["scapy"] = _stub("scapy")
        sys.modules["scapy.all"] = _stub(
            "scapy.all",
            sniff=MagicMock(), IP=MagicMock(), TCP=MagicMock(),
            UDP=MagicMock(), ARP=MagicMock(), send=MagicMock(), sendp=MagicMock(),
        )
        sys.modules["scapy.layers"]     = _stub("scapy.layers")
        sys.modules["scapy.layers.all"] = _stub("scapy.layers.all")


def _install_optional_gui_stubs() -> None:
    """Stub the GUI/clipboard/imaging packages when their profile is not installed.

    V69 M61 RC1. ``pyautogui`` and ``pyperclip`` live in requirements/lab.txt and
    ``Pillow`` in requirements/docs.txt; the authoritative CI job installs dev +
    soc, so none of the three is present there.

    The suites that exercise the SECURITY properties of these tools — the consent
    gate and the screenshot path sandbox — were patching them with
    ``monkeypatch.setattr("pyautogui.screenshot", ...)``, which resolves the dotted
    target by IMPORTING the real package. So the test harness, not the product,
    raised ModuleNotFoundError, and fourteen consent- and traversal-rejection tests
    failed for a reason unrelated to what they assert.

    Skipping them was the alternative and it is the worse one: these prove that
    screen capture is refused without consent and that a traversal path never
    reaches the capture call, and skipping would remove exactly those proofs from
    the only job that gates a merge. A stub module keeps them running with no
    optional dependency at all. Where the real package IS installed it is left
    alone, so nothing is masked.

    The stubs deliberately implement no behaviour: every test that needs any
    installs its own fake over the top.
    """
    if "pyautogui" not in sys.modules:
        try:
            import pyautogui  # noqa: F401
        except Exception:
            sys.modules["pyautogui"] = _stub(
                "pyautogui",
                screenshot=MagicMock(), hotkey=MagicMock(), write=MagicMock(),
                click=MagicMock(), moveTo=MagicMock(), press=MagicMock(),
                position=MagicMock(return_value=(0, 0)),
                size=MagicMock(return_value=(1920, 1080)),
                FAILSAFE=True,
            )
    if "pyperclip" not in sys.modules:
        try:
            import pyperclip  # noqa: F401
        except Exception:
            sys.modules["pyperclip"] = _stub(
                "pyperclip", paste=MagicMock(return_value=""), copy=MagicMock())
    if "PIL" not in sys.modules:
        try:
            import PIL  # noqa: F401
        except Exception:
            pil = _stub("PIL")
            image = _stub("PIL.Image", open=MagicMock())
            grab = _stub("PIL.ImageGrab", grab=MagicMock())
            pil.Image = image
            pil.ImageGrab = grab
            sys.modules["PIL"] = pil
            sys.modules["PIL.Image"] = image
            sys.modules["PIL.ImageGrab"] = grab


_install_win32_stubs()
_install_optional_gui_stubs()


# ── Subprocess guard — block destructive OS commands in tests ─────────────────

@pytest.fixture(autouse=True)
def stub_destructive_subprocesses(monkeypatch):
    """Prevent netsh / vssadmin / powershell / wevtutil from executing."""
    import subprocess

    _BLOCKED = frozenset({"netsh", "vssadmin", "wevtutil", "powershell"})
    _original_run = subprocess.run

    def _safe_run(cmd, *args, **kwargs):
        exe = ""
        if isinstance(cmd, (list, tuple)) and cmd:
            exe = str(cmd[0]).split("\\")[-1].split("/")[-1].lower()
        elif isinstance(cmd, str):
            exe = cmd.strip().split()[0].split("\\")[-1].split("/")[-1].lower()
        if any(b in exe for b in _BLOCKED):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result
        return _original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _safe_run)


# ── RBAC actor fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def analyst_actor():
    from core.rbac_manager import ActorContext, ClearanceLevel
    return ActorContext(identity="analyst@test", clearance=ClearanceLevel.Analyst)


@pytest.fixture
def hunter_actor():
    from core.rbac_manager import ActorContext, ClearanceLevel
    return ActorContext(identity="hunter@test", clearance=ClearanceLevel.L3_Hunter)


@pytest.fixture
def admin_actor():
    from core.rbac_manager import ActorContext, ClearanceLevel
    return ActorContext(identity="admin@test", clearance=ClearanceLevel.Admin)


@pytest.fixture(autouse=True)
def clear_rbac_context():
    """Ensure actor context never bleeds between tests."""
    from core import rbac_manager
    rbac_manager.clear_current_actor()
    yield
    rbac_manager.clear_current_actor()


# ── Infrastructure URL fixtures ───────────────────────────────────────────────

@pytest.fixture
def fake_db_url():
    return "postgresql://jarvis:jarvis@localhost:5432/jarvis_test"


@pytest.fixture
def fake_redis_url():
    return "redis://localhost:6379/15"


@pytest.fixture
def mock_siem_endpoint(monkeypatch):
    monkeypatch.setenv("SIEM_ENDPOINT", "http://localhost:9999/mock-siem")
    monkeypatch.setenv("SIEM_API_KEY", "test-key")
    return "http://localhost:9999/mock-siem"
