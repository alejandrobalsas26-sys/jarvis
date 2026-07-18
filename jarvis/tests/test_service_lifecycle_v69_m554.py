"""
tests/test_service_lifecycle_v69_m554.py — V69 M55.4 optional service supervision.

Locks:
  * the OptionalService contract — start off the critical path, refuse to start after
    STOPPING, a failed start is FAILED+last_error (not a crash), and a bounded stop()
    that treats EXPECTED CancelledError at the owner boundary as normal;
  * stop_uvicorn_gracefully — the proven M55.4.1 fix: request should_exit and await
    serve() so Starlette's lifespan tears down cleanly (no CancelledError traceback),
    force-cancel only as a last resort, and still surface a REAL serve() error.

No real Uvicorn needed: a tiny fake server reproduces serve()'s should_exit loop.
"""
from __future__ import annotations

import asyncio

from core.optional_service import (
    Criticality,
    OptionalService,
    ServiceState,
    stop_uvicorn_gracefully,
)


# ── OptionalService contract ──────────────────────────────────────────────────
def test_service_starts_off_task_and_becomes_ready():
    async def _run():
        started = {"v": False}

        async def _start(_svc):
            started["v"] = True

        s = OptionalService("x", start=_start, criticality=Criticality.OPTIONAL)
        s.start()
        assert s.state is ServiceState.STARTING   # non-blocking: task scheduled
        await asyncio.sleep(0.01)
        assert started["v"] is True
        assert s.state is ServiceState.READY
        assert s.ready_event.is_set()
        await s.stop()
        assert s.state is ServiceState.STOPPED
    asyncio.run(_run())


def test_service_refuses_to_start_after_stopping():
    from core.lifecycle import get_lifecycle, reset_lifecycle
    reset_lifecycle()
    get_lifecycle().begin_stopping()
    try:
        async def _run():
            started = {"v": False}

            async def _start(_svc):
                started["v"] = True

            s = OptionalService("late", start=_start)
            s.start()
            await asyncio.sleep(0.01)
            assert started["v"] is False              # no optional job after STOPPING
            assert s.state is ServiceState.REGISTERED
        asyncio.run(_run())
    finally:
        reset_lifecycle()


def test_service_failed_start_is_recorded_not_raised():
    async def _run():
        async def _start(_svc):
            raise RuntimeError("dependency missing")

        s = OptionalService("y", start=_start)
        s.start()
        await asyncio.sleep(0.01)
        assert s.state is ServiceState.FAILED
        assert "dependency missing" in (s.last_error or "")
        assert s.snapshot()["state"] == "FAILED"
    asyncio.run(_run())


def test_service_stop_suppresses_expected_cancellation():
    async def _run():
        async def _start(_svc):
            await asyncio.sleep(30)                    # long-running; will be cancelled

        s = OptionalService("z", start=_start)
        s.start()
        await asyncio.sleep(0.01)
        await s.stop(timeout=0.1)                      # times out -> cancels -> suppressed
        assert s.state is ServiceState.STOPPED         # no traceback, clean terminal state
    asyncio.run(_run())


def test_request_stop_is_idempotent():
    stops = {"n": 0}

    def _req():
        stops["n"] += 1

    s = OptionalService("w", request_stop=_req)
    s.request_stop()
    s.request_stop()
    assert s.state is ServiceState.STOPPING
    assert stops["n"] == 1                             # only the first transition signals


# ── stop_uvicorn_gracefully (the M55.4.1 fix) ─────────────────────────────────
class _FakeUvicorn:
    """Mimics uvicorn.Server.serve(): a loop that returns cleanly once should_exit is
    set, unless told to ignore it (a server wedged mid-shutdown) or to fail outright."""

    def __init__(self, *, ignore_exit: bool = False, fail: BaseException | None = None):
        self.should_exit = False
        self._ignore = ignore_exit
        self._fail = fail

    async def serve(self):
        if self._fail is not None:
            raise self._fail
        while True:
            if self.should_exit and not self._ignore:
                return
            await asyncio.sleep(0.01)


def test_uvicorn_clean_stop_has_no_cancellation():
    async def _run():
        srv = _FakeUvicorn()
        task = asyncio.ensure_future(srv.serve())
        await asyncio.sleep(0.02)
        status = await stop_uvicorn_gracefully(srv, task, timeout=2.0)
        assert "stopped normally" in status
        assert task.done()
        assert task.exception() is None               # clean return, NOT a CancelledError
    asyncio.run(_run())


def test_uvicorn_forced_cancel_after_timeout_is_suppressed():
    async def _run():
        srv = _FakeUvicorn(ignore_exit=True)          # ignores should_exit (wedged)
        task = asyncio.ensure_future(srv.serve())
        await asyncio.sleep(0.02)
        status = await stop_uvicorn_gracefully(srv, task, timeout=0.1)
        assert "after timeout" in status
        assert task.done()                            # force-cancelled, no traceback raised
    asyncio.run(_run())


def test_uvicorn_real_serve_error_is_reported():
    async def _run():
        srv = _FakeUvicorn(fail=RuntimeError("address already in use"))
        task = asyncio.ensure_future(srv.serve())
        await asyncio.sleep(0.02)                      # let serve() raise
        status = await stop_uvicorn_gracefully(srv, task, timeout=1.0)
        assert "error" in status                      # a REAL failure is NOT hidden
    asyncio.run(_run())


def test_uvicorn_not_running_is_noop():
    async def _run():
        assert "not running" in await stop_uvicorn_gracefully(None, None)
    asyncio.run(_run())


def test_uvicorn_sets_should_exit_via_supported_api():
    async def _run():
        srv = _FakeUvicorn()
        task = asyncio.ensure_future(srv.serve())
        await asyncio.sleep(0.02)
        await stop_uvicorn_gracefully(srv, task, timeout=2.0)
        assert srv.should_exit is True                # used the public flag, no monkeypatch
    asyncio.run(_run())
