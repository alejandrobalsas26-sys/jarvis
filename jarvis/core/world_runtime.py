"""
core/world_runtime.py — V69 M63: the wiring that makes the World State live.

WHAT THIS IS
------------
The single place where the pieces are joined: the connector registry produces
observations, the World State folds them in, staleness is aged, and the
Presence bridge turns the resulting transitions into PROPOSALS. Everything it
touches already exists; this module owns none of it and only sequences it.

WHY IT IS SEPARATE FROM THE PIECES
----------------------------------
:mod:`core.world_state`, :mod:`core.world_connectors` and
:mod:`core.world_presence` are each independently testable with no scheduler,
no clock and no I/O. Putting the loop here keeps them that way: the parts that
must be deterministic have no timer in them, and the part that has a timer has
no logic in it.

BOUNDEDNESS
-----------
The refresh loop is registered with the existing
:class:`core.task_watchdog.TaskWatchdog` (the repository's supervisor for
long-lived coroutines) rather than with a bare ``asyncio.create_task``. It
sleeps a configured interval, never busy-polls, and every cycle is bounded by
the connector timeouts and item ceilings in :mod:`core.world_bounds`.

WHAT IT NEVER DOES
------------------
It performs no remediation. A cycle's entire output is: observations recorded,
staleness aged, and a list of things a human might want to look at. There is no
branch anywhere in this file that calls a tool, and the bridge it uses cannot
reach ``ACT``.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger

from core.world_bounds import DEFAULT_BOUNDS, WorldBounds
from core.world_connectors import (
    ConnectorRegistry,
    ConnectorResult,
    ConnectorState,
    build_default_registry,
)
from core.world_presence import BridgeOutcome, WorldPresenceBridge
from core.world_state import WorldState, world

SCHEMA_VERSION = "world-runtime-1"

#: Seconds between refresh cycles. Finite, and clamped when overridden.
DEFAULT_REFRESH_S = 60.0
_MIN_REFRESH_S = 15.0
_MAX_REFRESH_S = 3_600.0


@dataclass
class CycleReport:
    """What one refresh cycle actually did."""
    started_at: float = 0.0
    duration_s: float = 0.0
    connectors: int = 0
    observations_ingested: int = 0
    observations_refused: int = 0
    stale_transitions: int = 0
    proposals: int = 0
    connector_states: dict = field(default_factory=dict)
    optional_missing: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "duration_s": round(self.duration_s, 3),
            "connectors": self.connectors,
            "observations_ingested": self.observations_ingested,
            "observations_refused": self.observations_refused,
            "stale_transitions": self.stale_transitions,
            "proposals": self.proposals,
            "connector_states": self.connector_states,
            "optional_missing": self.optional_missing,
            "world_effects": 0,
            "remediations_performed": 0,
        }


class WorldRuntime:
    """Sequences connector collection into the World State. Causes no effects."""

    def __init__(self, state: WorldState | None = None, *,
                 registry: ConnectorRegistry | None = None,
                 bounds: WorldBounds | None = None,
                 refresh_s: float = DEFAULT_REFRESH_S) -> None:
        self.bounds = bounds or DEFAULT_BOUNDS
        self.state = state if state is not None else world
        self.registry = registry if registry is not None else build_default_registry(
            bounds=self.bounds)
        self.bridge = WorldPresenceBridge(self.state, bounds=self.bounds)
        self.refresh_s = max(_MIN_REFRESH_S, min(float(refresh_s), _MAX_REFRESH_S))
        self._last_results: list[ConnectorResult] = []
        self._last_cycle: CycleReport | None = None
        self._cycles = 0

    # ── one cycle ────────────────────────────────────────────────────────────
    async def refresh_once(self, signal=None) -> CycleReport:
        """Collect, ingest, age, propose. Never raises; never acts."""
        report = CycleReport(started_at=time.monotonic())
        results = await self.registry.collect_all()
        self._last_results = results
        report.connectors = len(results)
        report.connector_states = {r.connector_id: r.state.value for r in results}
        report.optional_missing = [r.connector_id for r in results if r.optional_missing]

        for result in results:
            outcome = self.state.ingest_many(result.observations)
            report.observations_ingested += outcome["accepted"]
            report.observations_refused += outcome["refused"]

        report.stale_transitions = len(self.state.sweep_stale())

        if signal is not None:
            try:
                bridge_outcome: BridgeOutcome = self.bridge.run_cycle(signal)
                report.proposals = len(bridge_outcome.delivered)
            except Exception as exc:  # noqa: BLE001 — a proposal failure is not fatal
                logger.warning(f"WORLD_RUNTIME: presence bridge failed: {exc}")

        report.duration_s = time.monotonic() - report.started_at
        self._last_cycle = report
        self._cycles += 1
        return report

    # ── the supervised loop ──────────────────────────────────────────────────
    async def run_forever(self, signal_factory=None) -> None:
        """Refresh on a fixed interval until cancelled. Cancellation is clean."""
        logger.info(f"WORLD_RUNTIME: refresh loop every {self.refresh_s:.0f}s "
                    f"across {len(self.registry.all())} connectors")
        try:
            while True:
                try:
                    signal = signal_factory() if signal_factory else None
                    await self.refresh_once(signal)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad cycle, not the loop
                    logger.warning(f"WORLD_RUNTIME: cycle failed: {exc}")
                await asyncio.sleep(self.refresh_s)
        except asyncio.CancelledError:
            logger.info("WORLD_RUNTIME: refresh loop cancelled cleanly")
            raise

    # ── read surfaces ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Last known connector availability. Probes NOTHING when called."""
        by_state: dict[str, int] = {}
        for result in self._last_results:
            by_state[result.state.value] = by_state.get(result.state.value, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "cycles": self._cycles,
            "refresh_s": self.refresh_s,
            "registered": [c.connector_id for c in self.registry.all()],
            "by_state": dict(sorted(by_state.items())),
            "optional_missing": [r.connector_id for r in self._last_results
                                 if r.optional_missing],
            "results": [r.to_dict() for r in self._last_results],
            "last_cycle": self._last_cycle.to_dict() if self._last_cycle else None,
            "probed_on_this_call": False,
        }


#: Module singleton, following the repository's attach-at-boot convention.
runtime = WorldRuntime()


def connector_snapshot() -> dict:
    """The HUD's read. Deliberately never triggers a collection."""
    return runtime.snapshot()


def attach_world_runtime(watchdog, *, signal_factory=None,
                         refresh_s: float = DEFAULT_REFRESH_S):
    """Register the refresh loop with the existing supervisor.

    Called from ``main.py`` at boot alongside the other collectors. Returns the
    supervised task, or None if the watchdog refused (shutdown in progress).
    """
    from core.task_watchdog import RestartPolicy
    runtime.refresh_s = max(_MIN_REFRESH_S, min(float(refresh_s), _MAX_REFRESH_S))
    return watchdog.register(
        "world-state-refresh",
        lambda: runtime.run_forever(signal_factory),
        RestartPolicy.BACKOFF,
    )


__all__ = [
    "DEFAULT_REFRESH_S", "SCHEMA_VERSION", "ConnectorState", "CycleReport",
    "WorldRuntime", "attach_world_runtime", "connector_snapshot", "runtime",
]
