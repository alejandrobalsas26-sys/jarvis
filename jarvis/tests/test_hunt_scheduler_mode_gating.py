"""
tests/test_hunt_scheduler_mode_gating.py — V62.0 Phase 8: autonomous
background work respects the live AssistantMode + hardware pressure.

core/hunt_scheduler.py's start_hunt_scheduler ran its 4-hourly hypothesis
sweep unconditionally (besides a concurrency guard against an active LLM/
agentic operation) — core.ironman_mode.should_run_background_tasks (fully
implemented, unit-tested, zero callers) was never consulted. Quiet modes
(FOCUS/PRESENTATION/PASSIVE) and CPU/RAM/battery pressure now skip the sweep
for that interval instead of running it.
"""
from __future__ import annotations

import asyncio
import contextlib

import core.hunt_scheduler as hs
from core.assistant_state import AssistantState
from core.ironman_mode import AssistantMode


async def _fake_broadcast(event):
    pass


def test_resource_state_fails_open_on_psutil_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _raising_import(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _raising_import)
    on_battery, cpu, ram = hs._current_resource_state()
    assert on_battery is False
    assert cpu == 0.0
    assert ram == 0.0


def test_sweep_skipped_in_focus_mode(monkeypatch):
    monkeypatch.setattr(hs, "_WARMUP_S", 0.0)
    monkeypatch.setattr(hs, "_HUNT_INTERVAL_S", 0.01)
    monkeypatch.setattr(hs, "_current_resource_state", lambda: (False, 10.0, 10.0))
    calls = []
    monkeypatch.setattr(hs, "run_all_hunts", lambda *a, **k: calls.append(1))

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                hs.start_hunt_scheduler(
                    _fake_broadcast, None, "model", state=AssistantState(mode=AssistantMode.FOCUS),
                ),
                timeout=0.05,
            )

    asyncio.run(_run())
    assert not calls, "FOCUS mode must skip the autonomous sweep"


def test_sweep_skipped_under_cpu_pressure(monkeypatch):
    monkeypatch.setattr(hs, "_WARMUP_S", 0.0)
    monkeypatch.setattr(hs, "_HUNT_INTERVAL_S", 0.01)
    monkeypatch.setattr(hs, "_current_resource_state", lambda: (False, 99.0, 10.0))
    calls = []
    monkeypatch.setattr(hs, "run_all_hunts", lambda *a, **k: calls.append(1))

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                hs.start_hunt_scheduler(
                    _fake_broadcast, None, "model", state=AssistantState(mode=AssistantMode.ACTIVE),
                ),
                timeout=0.05,
            )

    asyncio.run(_run())
    assert not calls, "high CPU pressure must skip the sweep even in ACTIVE mode"


def test_sweep_runs_in_active_mode_with_no_pressure(monkeypatch):
    monkeypatch.setattr(hs, "_WARMUP_S", 0.0)
    monkeypatch.setattr(hs, "_HUNT_INTERVAL_S", 0.01)
    monkeypatch.setattr(hs, "_current_resource_state", lambda: (False, 5.0, 5.0))
    monkeypatch.setattr("core.cancel_bus.get_active_operations", lambda: {})
    calls = []

    async def _fake_run_all_hunts(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(hs, "run_all_hunts", _fake_run_all_hunts)

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                hs.start_hunt_scheduler(
                    _fake_broadcast, None, "model", state=AssistantState(mode=AssistantMode.ACTIVE),
                ),
                timeout=0.1,
            )

    asyncio.run(_run())
    assert calls, "ACTIVE mode with no pressure must run the sweep"


def test_no_state_wired_fails_open(monkeypatch):
    """state=None (a caller that never wired it) must behave exactly as
    before this retrofit — no mode/resource gate applied."""
    monkeypatch.setattr(hs, "_WARMUP_S", 0.0)
    monkeypatch.setattr(hs, "_HUNT_INTERVAL_S", 0.01)
    monkeypatch.setattr("core.cancel_bus.get_active_operations", lambda: {})
    calls = []

    async def _fake_run_all_hunts(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(hs, "run_all_hunts", _fake_run_all_hunts)

    async def _run():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                hs.start_hunt_scheduler(_fake_broadcast, None, "model", state=None),
                timeout=0.1,
            )

    asyncio.run(_run())
    assert calls
