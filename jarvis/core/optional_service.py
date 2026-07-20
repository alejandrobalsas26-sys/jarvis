"""core/optional_service.py — V69 M55.4: one small contract for optional async services.

The live run exposed two service-lifecycle faults:

  * a CancelledError traceback printed from inside the Starlette/Uvicorn lifespan on a
    NORMAL shutdown — because ``run_graceful_shutdown``'s blanket task-cancel hit the
    ``aura-server`` serve() coroutine MID-lifespan instead of asking it to exit through
    its supported API (``should_exit``);
  * no common notion of "this is an optional service; it must never block text
    dispatch, and at shutdown it stops in a defined order and expected cancellation is
    not an error".

This module is that small contract. It is NOT a new orchestration framework: it does
not own the event loop, does not replace ``TaskWatchdog`` (which supervises the
long-lived restartable producers) and does not rewrite the collectors. It wraps a
start coroutine + optional stop hooks with a truthful state machine and a bounded,
cancellation-safe ``stop()`` — applied where it removes a real fault (AURA/Uvicorn,
the MCP bridge, the native capability probe) and available for the rest.
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Awaitable, Callable

from loguru import logger


class ServiceState(str, Enum):
    REGISTERED = "REGISTERED"   # constructed, not started
    STARTING = "STARTING"       # start() scheduled, coroutine running
    READY = "READY"             # started and serving
    DORMANT = "DORMANT"         # intentionally not started (disabled/absent dep)
    DEGRADED = "DEGRADED"       # running with reduced capability
    STOPPING = "STOPPING"       # stop requested
    STOPPED = "STOPPED"         # fully stopped
    FAILED = "FAILED"           # start/run raised


class Criticality(str, Enum):
    CRITICAL = "critical"       # may affect CORE readiness
    OPTIONAL = "optional"       # must NEVER block text dispatch


# Terminal states in which start() must refuse to (re)start.
_DEAD = frozenset({ServiceState.STOPPING, ServiceState.STOPPED, ServiceState.FAILED})


class OptionalService:
    """One supervised optional async service with a truthful, bounded lifecycle.

    ``start`` is a coroutine ``async def start(service) -> None`` that brings the
    service up (it may set ``service.state = DEGRADED`` and still be useful).
    ``request_stop`` is a sync signal (e.g. ``server.should_exit = True``).
    ``stop`` is an optional coroutine/callable for a graceful teardown awaited under a
    bound. Expected :class:`asyncio.CancelledError` at the ownership boundary is
    normal and never surfaced as an error; unexpected exceptions are preserved.
    """

    def __init__(
        self,
        name: str,
        *,
        criticality: Criticality = Criticality.OPTIONAL,
        start: "Callable[[OptionalService], Awaitable[None]] | None" = None,
        request_stop: "Callable[[], None] | None" = None,
        stop: "Callable[[], Awaitable[None] | None] | None" = None,
    ) -> None:
        self.name = name
        self.criticality = criticality
        self._start_fn = start
        self._request_stop_fn = request_stop
        self._stop_fn = stop
        self.state = ServiceState.REGISTERED
        self.last_error: str | None = None
        self.ready_event = asyncio.Event()
        self._task: "asyncio.Task | None" = None

    # ── Start (non-blocking, off the critical path) ───────────────────────────
    def start(self) -> None:
        """Start the service in its OWN task (non-blocking). Refuses to start once the
        runtime is STOPPING (no optional job starts after shutdown begins) or if the
        service is already started/dead. Never raises."""
        try:
            from core.lifecycle import is_stopping
            if is_stopping():
                logger.debug(f"SERVICE[{self.name}]: not started — runtime is stopping")
                return
        except Exception:  # noqa: BLE001
            pass
        if self._task is not None or self.state in _DEAD or self._start_fn is None:
            return
        self.state = ServiceState.STARTING

        async def _run() -> None:
            try:
                await self._start_fn(self)
                if self.state == ServiceState.STARTING:
                    self.state = ServiceState.READY
                self.ready_event.set()
            except asyncio.CancelledError:
                if self.state not in _DEAD:
                    self.state = ServiceState.STOPPED
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.state = ServiceState.FAILED
                self.ready_event.set()   # unblock waiters; they must check state
                logger.warning(f"SERVICE[{self.name}]: start failed — {self.last_error}")

        try:
            self._task = asyncio.ensure_future(_run())
        except RuntimeError:
            self._task = None   # no running loop — caller may start later

    def mark_dormant(self, reason: str = "") -> None:
        """Declare the service intentionally not started (disabled / missing dep)."""
        self.state = ServiceState.DORMANT
        if reason:
            self.last_error = reason

    # ── Stop (bounded, cancellation-safe) ─────────────────────────────────────
    def request_stop(self) -> None:
        """Idempotent, non-blocking stop signal."""
        if self.state in (ServiceState.STOPPING, ServiceState.STOPPED):
            return
        if self.state not in _DEAD:
            self.state = ServiceState.STOPPING
        if self._request_stop_fn is not None:
            try:
                self._request_stop_fn()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"SERVICE[{self.name}]: request_stop suppressed: {exc}")

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop the service, bounded. Signals stop, runs the stop hook, then awaits the
        service task. EXPECTED CancelledError at this ownership boundary is normal and
        suppressed — that is the fix for the Uvicorn/Starlette shutdown traceback."""
        self.request_stop()
        if self._stop_fn is not None:
            try:
                res = self._stop_fn()
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"SERVICE[{self.name}]: stop hook suppressed: {exc}")
        t = self._task
        if t is not None and not t.done():
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=timeout)
            except asyncio.TimeoutError:
                t.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(t, return_exceptions=True), timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            except asyncio.CancelledError:
                pass   # expected at the owner boundary — not an error
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"SERVICE[{self.name}]: task await suppressed: {exc}")
        self.state = ServiceState.STOPPED
        logger.info(f"SERVICE[{self.name}]: stopped normally")

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "criticality": self.criticality.value,
            "ready": self.ready_event.is_set(),
            "last_error": self.last_error,
        }


# ── Uvicorn/AURA graceful stop (the proven M55.4.1 fix) ───────────────────────
def _task_exception(task: "asyncio.Task"):
    try:
        return task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return None


async def stop_uvicorn_gracefully(server, task, *, timeout: float = 5.0,
                                  name: str = "AURA") -> str:
    """V69 M55.4.1 — stop a ``uvicorn.Server`` through its SUPPORTED API and await its
    ``serve()`` task, bounded. Setting ``should_exit`` lets uvicorn tear the Starlette
    lifespan down cleanly; the live CancelledError traceback came from the blanket
    task-cancel hitting ``serve()`` MID-lifespan instead. Expected CancelledError at
    THIS ownership boundary is suppressed (normal shutdown); a real serve() error is
    preserved and reported. Never raises; returns a short status string.

    This does NOT monkey-patch uvicorn — it only uses the public ``should_exit`` flag.
    """
    if server is None and task is None:
        return f"{name} not running"
    try:
        if server is not None:
            server.should_exit = True   # supported graceful-exit signal
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{name}: should_exit set failed: {exc}")
    if task is None:
        logger.info(f"{name} stopped normally")
        return f"{name} stopped normally"
    if task.done():
        exc = _task_exception(task)
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            logger.warning(f"{name}: server task ended with {type(exc).__name__}: {exc}")
            return f"{name} stopped with error"
        logger.info(f"{name} stopped normally")
        return f"{name} stopped normally"
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        logger.info(f"{name} stopped normally")
        return f"{name} stopped normally"
    except asyncio.TimeoutError:
        # serve() did not honor should_exit in time — cancel as a last resort and
        # suppress the expected CancelledError so no traceback reaches the operator.
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"{name} stopped (forced after timeout)")
        return f"{name} stopped after timeout"
    except asyncio.CancelledError:
        logger.info(f"{name} stopped normally")
        return f"{name} stopped normally"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{name}: unexpected stop error {type(exc).__name__}: {exc}")
        return f"{name} stop error"
