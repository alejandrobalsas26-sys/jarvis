"""core/task_watchdog.py — Asyncio task watchdog with auto-restart policies (v29.0).

v29.0 adds *exponential silent backoff*: a task that fails repeatedly
with the same root cause is only logged loudly on the first failure
and on escalations (every 5th attempt, or when the error signature
changes). All other restarts are logged at DEBUG so the terminal
stays quiet on U-series hardware where slow startup races are common.
"""

import asyncio, time
from enum import Enum
from loguru import logger

from core.lifecycle import is_stopping as _lifecycle_stopping


class RestartPolicy(Enum):
    ALWAYS  = "always"
    NEVER   = "never"
    BACKOFF = "backoff"


# After this many consecutive failures, escalate one log line back to WARNING
# so a wedged subsystem is still visible without spamming every cycle.
_ESCALATE_EVERY = 5


class TaskWatchdog:

    def __init__(self):
        self._registry: dict[str, dict] = {}
        # V69 M54.12 — standard lifecycle contract. Once requested to stop (or the
        # global lifecycle is STOPPING) the monitor loop stops restarting tasks and
        # register() refuses new supervision, so no worker is resurrected during
        # shutdown.
        self._stopping = False

    def _should_stop(self) -> bool:
        return self._stopping or _lifecycle_stopping()

    def request_stop(self) -> None:
        """Idempotent: stop supervising/restarting. Does not itself cancel running
        tasks — run_graceful_shutdown cancels the asyncio tasks; this just prevents
        the monitor from creating replacements."""
        self._stopping = True

    def stop(self) -> None:
        """Alias kept for the shutdown driver's getattr(watchdog, 'stop')."""
        self.request_stop()

    def status(self) -> dict:
        """Lifecycle-contract status accessor (start/request_stop/stop/status)."""
        return {"stopping": self._should_stop(), "supervised": len(self._registry)}

    def register(
        self,
        name: str,
        coro_factory,
        policy: RestartPolicy = RestartPolicy.BACKOFF,
    ) -> asyncio.Task | None:
        # Never begin supervising new work once shutdown has started.
        if self._should_stop():
            logger.debug(f"WATCHDOG: register('{name}') refused — shutting down")
            return None
        task = asyncio.create_task(coro_factory(), name=name)
        self._registry[name] = {
            "task":            task,
            "coro_factory":    coro_factory,
            "policy":          policy,
            "restart_count":   0,
            "last_restart":    time.monotonic(),
            "last_error_sig":  None,
            "silent_streak":   0,
        }
        return task

    async def _monitor_loop(self, broadcast_fn) -> None:
        while True:
            await asyncio.sleep(30)
            # V69 M54.12 — stop supervising once shutdown begins. No task is
            # restarted after STOPPING (fixes a scheduler resurrecting mid-shutdown).
            if self._should_stop():
                logger.debug("WATCHDOG: monitor loop exiting — shutdown in progress")
                return
            for name, entry in list(self._registry.items()):
                task = entry["task"]
                if not task.done():
                    continue
                # Re-check inside the loop: a shutdown may have started while we were
                # inspecting tasks — never spawn a replacement during shutdown.
                if self._should_stop():
                    return

                exc     = task.exception() if not task.cancelled() else None
                policy  = entry["policy"]
                count   = entry["restart_count"]

                # Build a stable signature for the failure so repeats of the
                # same root cause can be silenced. Type + truncated repr is
                # enough to detect "same error as last time".
                err_sig = f"{type(exc).__name__}:{repr(exc)[:120]}" if exc else "exit"
                same_as_last = err_sig == entry["last_error_sig"]
                streak       = entry["silent_streak"] + 1 if same_as_last else 1

                # First occurrence (or novel error) → loud; repeats → silent;
                # every Nth repeat → one warning so we don't hide a wedged task.
                should_escalate = streak == 1 or (streak % _ESCALATE_EVERY == 0)

                if should_escalate:
                    if exc:
                        suffix = f" (recurring x{streak})" if streak > 1 else ""
                        logger.warning(f"WATCHDOG: task '{name}' failed: {exc}{suffix}")
                    else:
                        logger.warning(f"WATCHDOG: task '{name}' exited unexpectedly")
                else:
                    logger.debug(
                        f"WATCHDOG: task '{name}' failed again ({err_sig}) "
                        f"streak={streak}"
                    )

                entry["last_error_sig"] = err_sig
                entry["silent_streak"]  = streak

                if policy == RestartPolicy.NEVER:
                    await broadcast_fn({"type": "task_watchdog_event",
                                        "name": name, "action": "abandoned",
                                        "restart_count": count})
                    del self._registry[name]
                    continue

                # Backoff delay
                delay = 0 if policy == RestartPolicy.ALWAYS \
                        else min(30 * (2 ** count), 300)

                if delay:
                    await asyncio.sleep(delay)

                try:
                    entry["task"] = asyncio.create_task(
                        entry["coro_factory"](), name=name
                    )
                except (RuntimeError, BaseExceptionGroup, Exception) as e:
                    if should_escalate:
                        logger.warning(f"WATCHDOG: could not restart '{name}': {e}")
                    else:
                        logger.debug(f"WATCHDOG: could not restart '{name}': {e}")
                    entry["restart_count"] += 1
                    continue

                entry["restart_count"] += 1
                entry["last_restart"]   = time.monotonic()

                if should_escalate:
                    logger.info(f"WATCHDOG: restarted '{name}' (attempt {count + 1})")
                else:
                    logger.debug(f"WATCHDOG: restarted '{name}' (attempt {count + 1})")
                await broadcast_fn({"type": "task_watchdog_event",
                                    "name": name, "action": "restarted",
                                    "restart_count": count + 1})

    async def start(self, broadcast_fn) -> None:
        await self._monitor_loop(broadcast_fn)

    def get_status(self) -> dict[str, str]:
        return {
            name: (
                "running"    if not e["task"].done() else
                "restarting" if e["restart_count"] > 0 else
                "done"
            )
            for name, e in self._registry.items()
        }

    def restart_counts(self) -> dict[str, int]:
        """Cumulative restart count per supervised task (for M39 telemetry flapping
        detection). Read-only snapshot; the caller diffs it to derive a rate."""
        return {name: int(e["restart_count"]) for name, e in self._registry.items()}
