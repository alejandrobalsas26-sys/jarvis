"""core/task_watchdog.py — Asyncio task watchdog with auto-restart policies (v25.0)."""

import asyncio, time
from enum import Enum
from loguru import logger


class RestartPolicy(Enum):
    ALWAYS  = "always"
    NEVER   = "never"
    BACKOFF = "backoff"


class TaskWatchdog:

    def __init__(self):
        self._registry: dict[str, dict] = {}

    def register(
        self,
        name: str,
        coro_factory,
        policy: RestartPolicy = RestartPolicy.BACKOFF,
    ) -> asyncio.Task:
        task = asyncio.create_task(coro_factory(), name=name)
        self._registry[name] = {
            "task":          task,
            "coro_factory":  coro_factory,
            "policy":        policy,
            "restart_count": 0,
            "last_restart":  time.monotonic(),
        }
        return task

    async def _monitor_loop(self, broadcast_fn) -> None:
        while True:
            await asyncio.sleep(30)
            for name, entry in list(self._registry.items()):
                task = entry["task"]
                if not task.done():
                    continue

                exc     = task.exception() if not task.cancelled() else None
                policy  = entry["policy"]
                count   = entry["restart_count"]

                if exc:
                    logger.error(f"WATCHDOG: task '{name}' failed: {exc}")
                else:
                    logger.warning(f"WATCHDOG: task '{name}' exited unexpectedly")

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

                entry["task"]          = asyncio.create_task(
                    entry["coro_factory"](), name=name)
                entry["restart_count"] += 1
                entry["last_restart"]   = time.monotonic()

                logger.info(f"WATCHDOG: restarted '{name}' (attempt {count + 1})")
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
