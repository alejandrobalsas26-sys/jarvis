"""
core/watch_reconcile.py — V69 M54.1.4: honest recovery after event overflow.

A bounded queue legitimately drops and coalesces events. The dishonest outcome is
to then behave as if nothing changed. This module owns the recovery contract:

  * a root that overflowed is marked STALE — we KNOW we lost events there;
  * exactly ONE bounded reconciliation scan is scheduled per root per episode
    (never one per dropped event — that was the flood we are fixing);
  * the scan is lifecycle-aware: it never STARTS after STOPPING, and it stops
    mid-flight when shutdown begins (M54.1.4 + the M54 shutdown ordering);
  * it walks in bounded pages with a yield between them, so a huge tree can never
    monopolize the CPU-bound event loop;
  * it reports truthful state: CURRENT / RECONCILING / STALE / DEGRADED.

It deliberately does NOT decide what to do with a rediscovered file — it re-offers
the path through the same policy + SafeEnqueue seam the live watcher uses, so
recovery and steady state share one path.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

# Files examined per page before yielding to the loop.
_PAGE_SIZE = 200
# Absolute ceiling on one reconciliation walk — recovery must stay bounded even
# if an operator points a root at an enormous tree.
_MAX_FILES_PER_SCAN = 20_000


class RootState(str, Enum):
    """Truthful per-root freshness."""

    CURRENT = "CURRENT"          # no known loss
    RECONCILING = "RECONCILING"  # a bounded rescan is running
    STALE = "STALE"              # events were dropped; rescan pending
    DEGRADED = "DEGRADED"        # rescan could not complete (error/shutdown)


@dataclass
class RootStatus:
    state: RootState = RootState.CURRENT
    overflows: int = 0
    reconciliations: int = 0
    last_overflow_at: float | None = None
    last_reconcile_at: float | None = None
    last_scanned: int = 0
    truncated: bool = False       # hit _MAX_FILES_PER_SCAN
    last_error: str | None = None


@dataclass
class WatchReconciler:
    """Bounded, lifecycle-aware overflow recovery for a set of watch roots."""

    # Re-offer one rediscovered path through the normal policy+enqueue seam.
    offer_path: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    page_size: int = _PAGE_SIZE
    max_files: int = _MAX_FILES_PER_SCAN
    # Injectable for deterministic tests; defaults to the real lifecycle.
    stopping_fn: Callable[[], bool] | None = None
    # Injectable directory walker (tests inject a synthetic tree).
    walk_fn: Callable[[str], object] | None = None

    _status: dict[str, RootStatus] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stopping_fn is None:
            self.stopping_fn = _default_stopping
        if self.walk_fn is None:
            self.walk_fn = os.walk

    # -- State ---------------------------------------------------------------
    def status(self, root: str) -> RootStatus:
        return self._status.setdefault(root, RootStatus())

    def snapshot(self) -> dict:
        """Bounded, JSON-ready view for runtime health. Root paths are operator
        configuration, not user content, so they are safe to expose."""
        return {
            "roots": {
                r: {
                    "state": s.state.value,
                    "overflows": s.overflows,
                    "reconciliations": s.reconciliations,
                    "last_scanned": s.last_scanned,
                    "truncated": s.truncated,
                    "last_error": s.last_error,
                }
                for r, s in self._status.items()
            },
            "stale_roots": sum(
                1 for s in self._status.values()
                if s.state in (RootState.STALE, RootState.DEGRADED)
            ),
            "reconciliations": sum(s.reconciliations for s in self._status.values()),
        }

    # -- Overflow entry point ------------------------------------------------
    def mark_overflow(self, root: str) -> None:
        """Record that `root` lost events. Marks it STALE — we must never claim
        nothing changed. Idempotent; scheduling is a separate, gated step."""
        st = self.status(root)
        st.overflows += 1
        st.last_overflow_at = self.clock()
        if st.state is not RootState.RECONCILING:
            st.state = RootState.STALE

    def schedule_reconcile(self, root: str) -> bool:
        """Schedule AT MOST ONE bounded reconciliation for `root`.

        Returns True only if this call started one. Refuses when a scan is already
        in flight for the root (one per episode, never one per dropped event) and
        after STOPPING (no new background work once shutdown begins).
        """
        if self.stopping_fn():
            return False
        existing = self._tasks.get(root)
        if existing is not None and not existing.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(self.reconcile(root), name=f"watch-reconcile:{root}")
        self._tasks[root] = task
        return True

    # -- The bounded scan ----------------------------------------------------
    async def reconcile(self, root: str) -> RootStatus:
        """Walk `root` in bounded pages, re-offering each interesting path. Honest
        about the outcome: DEGRADED if it could not finish."""
        st = self.status(root)
        if self.stopping_fn():
            st.state = RootState.DEGRADED
            st.last_error = "stopping"
            return st
        st.state = RootState.RECONCILING
        st.truncated = False
        seen = 0
        try:
            for dirpath, _dirnames, filenames in self.walk_fn(root):
                for fn in filenames:
                    # Cancellation/shutdown is checked per page, not per file, but
                    # the page is small enough to stay responsive.
                    if seen % self.page_size == 0:
                        if self.stopping_fn():
                            st.state = RootState.DEGRADED
                            st.last_error = "stopping"
                            return st
                        await asyncio.sleep(0)
                    if seen >= self.max_files:
                        st.truncated = True
                        st.state = RootState.DEGRADED
                        st.last_error = "scan_limit"
                        st.last_scanned = seen
                        return st
                    seen += 1
                    try:
                        self.offer_path(os.path.join(dirpath, fn))
                    except Exception:
                        # One bad path must never abort recovery.
                        pass
        except asyncio.CancelledError:
            st.state = RootState.DEGRADED
            st.last_error = "cancelled"
            st.last_scanned = seen
            raise
        except Exception as exc:
            st.state = RootState.DEGRADED
            st.last_error = type(exc).__name__
            st.last_scanned = seen
            return st
        st.last_scanned = seen
        st.reconciliations += 1
        st.last_reconcile_at = self.clock()
        st.state = RootState.CURRENT
        return st

    async def aclose(self, timeout: float = 2.0) -> None:
        """Cancel any in-flight reconciliation (bounded). Called from shutdown so
        no rescan outlives STOPPING."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()


def _default_stopping() -> bool:
    try:
        from core.lifecycle import is_stopping
        return is_stopping()
    except Exception:
        return False
