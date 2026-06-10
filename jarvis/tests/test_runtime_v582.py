"""
tests/test_runtime_v582.py — V58.2 RUNTIME STABILIZATION regression tests.

Covers the safe-default / watchdog / HITL behaviours hardened in V58.2:
  - health_watchdog passive presence markers (no spurious restart loop)
  - windows_hardener dry-run-by-default (no real service/firewall changes)
  - executor NATO challenge keyboard fallback on low-confidence STT
  - llm.aclose idempotency + anyio cancel-scope error suppression

Pure unit tests — no ETW session, Ollama, audio, or elevation required.
"""
from __future__ import annotations

import asyncio

import pytest


# ─────────────────────────── health_watchdog (Task 1) ───────────────────────
def test_mark_present_is_passive_not_supervised():
    import core.health_watchdog as hw

    hw._PASSIVE.pop("unit_passive", None)
    hw._SUP.pop("unit_passive", None)

    hw.mark_present("unit_passive")
    # Registered as a passive marker, never as a supervised (restartable) task.
    assert "unit_passive" in hw._PASSIVE
    assert "unit_passive" not in hw._SUP

    status = hw._passive_status()
    assert status["unit_passive"] == {"alive": True, "restarts": 0, "passive": True}


def test_passive_status_reflects_status_fn():
    import core.health_watchdog as hw

    hw.mark_present("unit_up", status_fn=lambda: True)
    hw.mark_present("unit_down", status_fn=lambda: False)
    hw.mark_present("unit_raises", status_fn=lambda: (_ for _ in ()).throw(RuntimeError()))

    status = hw._passive_status()
    assert status["unit_up"]["alive"] is True
    assert status["unit_down"]["alive"] is False
    assert status["unit_raises"]["alive"] is False  # exception → treated as down


def test_supervise_once_never_restarts_passive_modules():
    import core.health_watchdog as hw

    hw._SUP.clear()
    hw._PASSIVE.clear()
    hw.mark_present("cognitive_core")

    # _supervise_once only walks _SUP; a passive marker must be untouched and
    # must never log "module down" / trigger a restart.
    asyncio.run(hw._supervise_once())
    assert hw._SUP == {}
    assert "cognitive_core" in hw._PASSIVE


# ─────────────────────────── windows_hardener (Task 3) ──────────────────────
def _install_hardener_spies(monkeypatch):
    import core.windows_hardener as wh

    calls = {"disable": 0, "firewall": 0, "ollama": 0, "defender": 0}
    monkeypatch.setattr(wh, "_disable_service",
                        lambda *a, **k: calls.__setitem__("disable", calls["disable"] + 1) or True)
    monkeypatch.setattr(wh, "_apply_firewall_rule",
                        lambda *a, **k: calls.__setitem__("firewall", calls["firewall"] + 1) or True)
    monkeypatch.setattr(wh, "_harden_ollama",
                        lambda *a, **k: calls.__setitem__("ollama", calls["ollama"] + 1) or True)
    monkeypatch.setattr(wh, "_harden_defender",
                        lambda *a, **k: calls.__setitem__("defender", calls["defender"] + 1))
    return wh, calls


async def _noop_broadcast(_event):
    return None


def test_hardener_dry_run_by_default_makes_no_changes(monkeypatch):
    monkeypatch.delenv("JARVIS_HARDENER_ENABLE", raising=False)
    monkeypatch.delenv("JARVIS_HARDENER_DRY_RUN", raising=False)
    wh, calls = _install_hardener_spies(monkeypatch)

    report = asyncio.run(wh.apply_host_hardening(_noop_broadcast))

    assert report["dry_run"] is True
    assert report["enabled"] is False
    assert report["services_disabled"] == 0
    assert report["planned_changes"], "dry-run must enumerate planned changes"
    # No mutating helper may run when the hardener is not explicitly enabled.
    assert calls == {"disable": 0, "firewall": 0, "ollama": 0, "defender": 0}


def test_hardener_enforce_requires_explicit_enable(monkeypatch):
    monkeypatch.setenv("JARVIS_HARDENER_ENABLE", "true")
    monkeypatch.setenv("JARVIS_HARDENER_DRY_RUN", "false")
    wh, calls = _install_hardener_spies(monkeypatch)

    report = asyncio.run(wh.apply_host_hardening(_noop_broadcast))

    assert report["dry_run"] is False
    assert report["enabled"] is True
    # Live path actually drives the (spied) mutating helpers.
    assert calls["firewall"] == len(wh._FIREWALL_RULES)
    assert calls["ollama"] == 1
    assert calls["defender"] == 1


# ─────────────────────────── executor NATO MFA (Task 4) ─────────────────────
class _FakeSTT:
    """Always returns an empty / zero-confidence transcript (STT misfire)."""
    def record(self):
        return b""

    def transcribe_with_confidence(self, _audio):
        return ("", 0.0)


def _make_executor():
    from tools.executor import ToolExecutor
    return ToolExecutor(stt_queue=asyncio.Queue(), stt_listener=_FakeSTT())


def _run_challenge(monkeypatch, keyboard_value):
    import tools.executor as ex

    async def _noop(_evt):
        return None

    monkeypatch.setattr(ex, "_aura_broadcast", _noop)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: keyboard_value)

    te = _make_executor()
    return asyncio.run(te._challenge("run_shell_command", "ls -la"))


def test_low_confidence_falls_back_to_keyboard_grant(monkeypatch):
    granted, audit = _run_challenge(monkeypatch, "y")
    assert granted is True
    assert audit == "keyboard:granted"


def test_low_confidence_keyboard_no_denies(monkeypatch):
    granted, audit = _run_challenge(monkeypatch, "n")
    assert granted is False
    assert audit == "keyboard:denied"


def test_low_confidence_keyboard_empty_denies(monkeypatch):
    granted, audit = _run_challenge(monkeypatch, "")
    assert granted is False
    assert audit == "keyboard:denied"


# ─────────────────────────── llm.aclose (Task 7) ────────────────────────────
def test_llm_aclose_idempotent_and_suppresses_cancel_scope():
    try:
        from core.llm import LLM
    except Exception as e:  # pragma: no cover - env without openai/.env
        pytest.skip(f"core.llm import unavailable: {e}")

    obj = LLM.__new__(LLM)  # bypass heavy __init__

    class _Stack:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            self.calls += 1
            raise RuntimeError(
                "Attempted to exit cancel scope in a different task than it was entered in"
            )

    obj._exit_stack = _Stack()
    obj._closed = False

    # First close swallows the anyio cross-task RuntimeError; second is a no-op.
    asyncio.run(obj.aclose())
    asyncio.run(obj.aclose())
    assert obj._exit_stack.calls == 1
