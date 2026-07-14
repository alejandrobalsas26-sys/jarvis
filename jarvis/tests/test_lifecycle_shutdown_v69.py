"""
tests/test_lifecycle_shutdown_v69.py — V69 M54.11 + M54.12 signals + scheduler + order.

Locks: one SIGINT starts exactly one shutdown, repeated SIGINT does not start
another (and eventually force-exits); no hunt begins after STOPPING; the watchdog
refuses to supervise/restart during shutdown; and shutdown cancels tasks BEFORE
running the flush/close callbacks so storage closes only after writers are stopped.
Signals are injected — no real SIGINT is sent. The lifecycle-consuming modules use
call-time is_stopping()/begin_stopping(), so reset_lifecycle() is observed.
"""
from __future__ import annotations

import asyncio

import core.shutdown_manager as sm
import core.lifecycle as lc
from core.lifecycle import reset_lifecycle, LifecycleState
from core.task_watchdog import TaskWatchdog, RestartPolicy


def setup_function(_):
    reset_lifecycle()
    sm.reset_signal_state()
    sm._shutdown_callbacks.clear()


def teardown_function(_):
    sm._shutdown_callbacks.clear()
    reset_lifecycle()
    sm.reset_signal_state()


# ── Idempotent signals (M54.11) ───────────────────────────────────────────────

def test_first_signal_initiates_repeat_does_not():
    assert sm.handle_shutdown_signal("SIGINT") == "initiated"
    assert lc.lifecycle.state is LifecycleState.STOPPING
    assert sm.handle_shutdown_signal("SIGINT") == "already_stopping"
    assert lc.lifecycle.state is LifecycleState.STOPPING


def test_third_signal_forces_exit(monkeypatch):
    calls = {}
    monkeypatch.setattr(sm.os, "_exit", lambda code: calls.setdefault("code", code))
    sm.handle_shutdown_signal("SIGINT")   # initiate
    sm.handle_shutdown_signal("SIGINT")   # already
    sm.handle_shutdown_signal("SIGINT")   # force exit
    assert calls.get("code") == 1


# ── Scheduler cancellation (M54.12) ───────────────────────────────────────────

def test_no_hunt_after_stopping():
    from core.hunt_scheduler import run_single_hunt
    lc.begin_stopping()
    res = asyncio.run(run_single_hunt(3))   # H04 — the exact live symptom
    assert res["verdict"] == "SKIPPED"


def test_watchdog_refuses_register_and_reports_stopping():
    async def _run():
        wd = TaskWatchdog()
        lc.begin_stopping()
        t = wd.register("late", lambda: asyncio.sleep(0), RestartPolicy.ALWAYS)
        assert t is None
        assert wd.status()["stopping"] is True
    asyncio.run(_run())


def test_watchdog_registers_normally_before_stopping():
    async def _run():
        wd = TaskWatchdog()
        t = wd.register("early", lambda: asyncio.sleep(0), RestartPolicy.NEVER)
        assert t is not None
        wd.request_stop()
        assert wd._should_stop() is True
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(_run())


# ── Shutdown ordering: writers cancelled BEFORE storage flush (M54.12) ─────────

def test_storage_flush_runs_after_task_cancellation():
    order: list[str] = []

    async def _run():
        writer_cancelled = asyncio.Event()

        async def _writer():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                order.append("writer_cancelled")
                writer_cancelled.set()
                raise

        async def _flush():
            order.append("flush_ran")
            assert writer_cancelled.is_set(), "flush ran before writer was cancelled!"

        sm.register_shutdown_callback(_flush)
        asyncio.create_task(_writer(), name="writer")
        await asyncio.sleep(0)   # let the writer start
        await sm.run_graceful_shutdown()

    asyncio.run(_run())
    assert order == ["writer_cancelled", "flush_ran"]
    assert lc.lifecycle.state is LifecycleState.STOPPED


def test_shutdown_marks_stopped_and_is_bounded():
    asyncio.run(sm.run_graceful_shutdown())
    assert lc.lifecycle.state is LifecycleState.STOPPED
