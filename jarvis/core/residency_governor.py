"""core/residency_governor.py — V69 M56.5: inference arbitration and residency policy.

WHY ARBITRATION, NOT ANOTHER ROUTER
-----------------------------------
Model SELECTION already has one owner: :mod:`core.model_router` (roles) plus
:mod:`core.model_capabilities` (surfaces). This module adds nothing to that and keeps
no second model table. It answers a different question: given that two pieces of work
both want the CPU, which one runs, and which one waits?

On this host that question has a hard answer. One 6-core 15 W CPU serves every
generation; a second concurrent decode does not make one turn fast and the other
slow — it makes BOTH slow, and it doubles the chance of an eviction. So heavy
inference is SERIALIZED by default, and the ordering is by explicit priority:

    1 CRITICAL       an active HITL / authorization response
    2 INTERACTIVE    the operator's live FAST turn
    3 VERIFICATION   verification of an effectful operation
    4 SEMANTIC_QUERY a requested semantic lookup (the operator is waiting on it)
    5 BACKGROUND     background semantic work (consolidation, indexing)
    6 PREWARM        the optional warmup — always last, by definition optional

The rule that motivated all of this: a background embedding batch must never make the
operator's live turn wait. But the mirror rule matters just as much — background work
must never be silently dropped or starved forever, because a lost semantic write is a
lost memory. So deferral is explicit, bounded, counted, and ages into priority.

SLOT BORROWING (M56.5.1)
------------------------
DEEP / CODER / VISION / VERIFIER stay on demand. When one is requested, the governor
grants it, records WHY, and waits inside the REQUESTING turn's budget. It never
unloads a model itself — eviction is the server's business — but it observes the
consequence, marks FAST as needing restoration when the server evicted it, and
schedules ONE bounded background restoration prewarm after the heavy work finishes.
Restoration never starts after STOPPING and is never claimed until verified.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

# Bounded by construction: a queue that can grow without limit is a memory leak with
# extra steps, and on this host a backlog of 64 heavy requests is already pathological.
_DEFAULT_QUEUE_CAPACITY = 64
# A waiter this old is promoted one step so background work cannot be starved forever.
_AGING_THRESHOLD_S = 30.0
_DEFAULT_ACQUIRE_TIMEOUT_S = 120.0


class Priority(IntEnum):
    """Lower value = served first. Explicit, total, and never derived from text."""

    CRITICAL = 1        # active HITL / authorization response
    INTERACTIVE = 2     # the operator's live FAST turn
    VERIFICATION = 3    # verification of an effectful operation
    SEMANTIC_QUERY = 4  # a requested semantic lookup
    BACKGROUND = 5      # background semantic work
    PREWARM = 6         # optional warmup — always last


# Roles that are ON DEMAND: they are granted, but they are never kept resident and
# never speculatively loaded.
ON_DEMAND_ROLES = frozenset({"deep", "coder", "vision", "verifier"})
# The preferred steady-state pair (logical policy — NOT a claim about server slots).
PRIMARY_INTERACTIVE_ROLE = "fast"
SEMANTIC_ROLE = "embedding"


class GovernorClosed(Exception):
    """Raised when work is submitted after shutdown began."""


@dataclass
class WorkRequest:
    """One request for the heavy-inference slot."""

    role: str
    priority: Priority
    reason: str = ""
    enqueued_at: float = 0.0
    model: str = ""

    def effective_priority(self, now: float) -> float:
        """Priority with AGING: a request that has waited long enough is promoted by
        one step (never past CRITICAL). This is what makes 'never permanently
        starved' a property of the code rather than a hope."""
        waited = max(0.0, now - self.enqueued_at)
        steps = int(waited // _AGING_THRESHOLD_S)
        return max(float(Priority.CRITICAL), float(self.priority) - steps)


@dataclass
class GovernorMetrics:
    """Bounded counters. No prompts, no content — roles, counts and milliseconds."""

    active_role: str | None = None
    active_priority: str | None = None
    queue_depth: int = 0
    queue_capacity: int = _DEFAULT_QUEUE_CAPACITY
    high_watermark: int = 0
    total_wait_ms: float = 0.0
    completed: int = 0
    background_deferrals: int = 0
    cancellations: int = 0
    starvation_preventions: int = 0
    rejections: int = 0
    duplicate_loads_avoided: int = 0

    @property
    def average_wait_ms(self) -> float | None:
        return round(self.total_wait_ms / self.completed, 1) if self.completed else None

    def snapshot(self) -> dict:
        return {
            "active_role": self.active_role,
            "active_priority": self.active_priority,
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue_capacity,
            "high_watermark": self.high_watermark,
            "average_wait_ms": self.average_wait_ms,
            "completed": self.completed,
            "background_deferrals": self.background_deferrals,
            "cancellations": self.cancellations,
            "starvation_preventions": self.starvation_preventions,
            "rejections": self.rejections,
            "duplicate_loads_avoided": self.duplicate_loads_avoided,
        }


@dataclass
class _Waiter:
    request: WorkRequest
    future: "asyncio.Future"
    seq: int


class ResidencyGovernor:
    """Serializes heavy inference and owns the residency restoration policy.

    Usage is a single async context manager, so a slot cannot leak on any exit path —
    success, exception, timeout or cancellation:

        async with governor.slot(role="fast", priority=Priority.INTERACTIVE):
            ...run the generation...
    """

    def __init__(self, *, capacity: int = _DEFAULT_QUEUE_CAPACITY,
                 max_concurrent: int = 1,
                 clock: Callable[[], float] = time.monotonic,
                 is_stopping: Callable[[], bool] | None = None) -> None:
        self.capacity = max(1, int(capacity))
        # Serialized by default: one heavy generation at a time on this CPU.
        self.max_concurrent = max(1, int(max_concurrent))
        self._clock = clock
        self._is_stopping = is_stopping or (lambda: False)
        self.metrics = GovernorMetrics(queue_capacity=self.capacity)
        self._waiters: list[_Waiter] = []
        self._active: list[WorkRequest] = []
        self._seq = 0
        self._closed = False
        # Residency bookkeeping (logical policy, informed by observation).
        self._needs_restoration = False
        self._restoration_reason: str | None = None
        self._restoration_task: "asyncio.Task | None" = None
        self._loading: set[str] = set()

    # ── introspection ────────────────────────────────────────────────────────
    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    @property
    def active_roles(self) -> tuple[str, ...]:
        return tuple(r.role for r in self._active)

    def needs_restoration(self) -> bool:
        return self._needs_restoration

    def snapshot(self) -> dict:
        self.metrics.queue_depth = self.queue_depth
        active = self._active[0] if self._active else None
        self.metrics.active_role = active.role if active else None
        self.metrics.active_priority = active.priority.name if active else None
        out = self.metrics.snapshot()
        out["max_concurrent"] = self.max_concurrent
        out["needs_restoration"] = self._needs_restoration
        out["restoration_reason"] = self._restoration_reason
        out["closed"] = self._closed
        out["waiting"] = [
            {"role": w.request.role, "priority": w.request.priority.name,
             "waited_ms": round((self._clock() - w.request.enqueued_at) * 1000.0, 1)}
            for w in self._waiters
        ]
        return out

    # ── admission ────────────────────────────────────────────────────────────
    def _admit(self, request: WorkRequest) -> None:
        if self._closed or self._is_stopping():
            self.metrics.rejections += 1
            raise GovernorClosed(f"governor closed; {request.role} work refused")
        if len(self._waiters) >= self.capacity:
            self.metrics.rejections += 1
            raise GovernorClosed(
                f"inference queue full ({self.capacity}); {request.role} work refused")

    def _order_key(self, waiter: _Waiter, now: float) -> tuple:
        # Effective priority first (aging applied), then FIFO within a priority so
        # equal-priority work is served fairly and deterministically.
        return (waiter.request.effective_priority(now), waiter.seq)

    def _next_waiter(self) -> _Waiter | None:
        if not self._waiters:
            return None
        now = self._clock()
        best = min(self._waiters, key=lambda w: self._order_key(now=now, waiter=w))
        if best.request.effective_priority(now) < float(best.request.priority):
            self.metrics.starvation_preventions += 1
        return best

    def _pump(self) -> None:
        """Grant the slot to the best waiter, if capacity allows. Never blocks."""
        while self._waiters and len(self._active) < self.max_concurrent:
            waiter = self._next_waiter()
            if waiter is None:
                return
            self._waiters.remove(waiter)
            if waiter.future.done():      # cancelled while queued
                continue
            self._active.append(waiter.request)
            waiter.future.set_result(True)

    async def acquire(self, request: WorkRequest, *,
                      timeout_s: float = _DEFAULT_ACQUIRE_TIMEOUT_S) -> float:
        """Wait for the heavy-inference slot. Returns the wait in ms.

        The wait is ALWAYS bounded: ``timeout_s`` is the caller's own budget, so a
        DEEP request waits inside the requesting turn's deadline rather than inheriting
        an unrelated one.
        """
        request.enqueued_at = self._clock()
        self._admit(request)
        if len(self._active) < self.max_concurrent and not self._waiters:
            self._active.append(request)
            return 0.0
        # Something heavier (or equally heavy) is running: this request waits, and if
        # it is background work being displaced by live work, that is counted.
        if request.priority >= Priority.BACKGROUND:
            self.metrics.background_deferrals += 1
        loop = asyncio.get_running_loop()
        self._seq += 1
        waiter = _Waiter(request=request, future=loop.create_future(), seq=self._seq)
        self._waiters.append(waiter)
        self.metrics.high_watermark = max(self.metrics.high_watermark, len(self._waiters))
        # Defensive: if capacity freed up between the admission check and here, grant
        # immediately (in priority order) rather than waiting for the next release.
        self._pump()
        try:
            await asyncio.wait_for(waiter.future, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._drop(waiter)
            raise
        except asyncio.CancelledError:
            self.metrics.cancellations += 1
            self._drop(waiter)
            raise
        return round((self._clock() - request.enqueued_at) * 1000.0, 1)

    def _drop(self, waiter: _Waiter) -> None:
        if waiter in self._waiters:
            self._waiters.remove(waiter)
        # The slot may have been granted in the same tick the wait expired; if so,
        # release it so it is never leaked.
        if waiter.future.done() and not waiter.future.cancelled():
            try:
                if waiter.future.result() and waiter.request in self._active:
                    self.release(waiter.request)
            except Exception:  # noqa: BLE001
                pass

    def release(self, request: WorkRequest, *, wait_ms: float = 0.0) -> None:
        """Return the slot and hand it to the next waiter. Never raises."""
        try:
            self._active.remove(request)
        except ValueError:
            return
        self.metrics.completed += 1
        self.metrics.total_wait_ms += max(0.0, wait_ms)
        self._pump()

    def slot(self, *, role: str, priority: Priority, reason: str = "",
             model: str = "", timeout_s: float = _DEFAULT_ACQUIRE_TIMEOUT_S):
        """The async context manager every heavy inference passes through."""
        return _SlotContext(self, WorkRequest(role=role, priority=priority,
                                              reason=reason, model=model),
                            timeout_s=timeout_s)

    # ── duplicate-load suppression ───────────────────────────────────────────
    def begin_load(self, model: str) -> bool:
        """Claim the right to load ``model``. False when a load is already in flight —
        two concurrent cold loads of the same model is the worst thing that can happen
        on this CPU, and it is pure waste."""
        if not model:
            return True
        if model in self._loading:
            self.metrics.duplicate_loads_avoided += 1
            return False
        self._loading.add(model)
        return True

    def end_load(self, model: str) -> None:
        self._loading.discard(model)

    # ── residency policy (M56.5.1 slot borrowing) ────────────────────────────
    def note_on_demand_grant(self, role: str, *, reason: str = "") -> None:
        """Record that an on-demand role borrowed the machine. Never evicts anything —
        eviction is the server's decision; this only records the cause so a later
        observation can be explained."""
        if role in ON_DEMAND_ROLES:
            self._restoration_reason = reason or f"{role}_request"

    def note_residency_observation(self, loaded_models, *, fast_model: str) -> bool:
        """Fold an OBSERVED loaded-model list. Returns True when FAST is missing and
        restoration is now needed. Observation-driven: nothing is assumed."""
        from core.residency import model_matches

        names = tuple(loaded_models or ())
        present = any(model_matches(n, fast_model) for n in names if n)
        if not present and fast_model:
            self._needs_restoration = True
            self._restoration_reason = self._restoration_reason or "fast_not_resident"
        else:
            self._needs_restoration = False
        return self._needs_restoration

    def schedule_restoration(self, restore_fn) -> "asyncio.Task | None":
        """Schedule ONE bounded background restoration prewarm.

        Refused when: shutdown began, nothing needs restoring, heavy work is still
        active (restoration must not compete with the workload it is recovering from),
        or a restoration is already in flight. Restoration success is NEVER claimed
        here — ``restore_fn`` reports it, and readiness only becomes READY on a real
        token (see :mod:`core.fast_prewarm`).
        """
        if self._closed or self._is_stopping():
            return None
        if not self._needs_restoration:
            return None
        if self._active:
            return None
        if self._restoration_task is not None and not self._restoration_task.done():
            return self._restoration_task

        async def _restore() -> None:
            try:
                await restore_fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — restoration is best-effort
                pass
            finally:
                self._needs_restoration = False

        self._restoration_task = asyncio.ensure_future(_restore())
        return self._restoration_task

    # ── shutdown ─────────────────────────────────────────────────────────────
    async def close(self) -> None:
        """Refuse new work and release every waiter with a bounded failure.

        Waiters are FAILED, never left pending: a caller blocked on a slot that will
        never be granted is exactly the orphan-task class M55.4 removed.
        """
        self._closed = True
        waiters, self._waiters = self._waiters, []
        for w in waiters:
            if not w.future.done():
                w.future.set_exception(GovernorClosed("governor closed during shutdown"))
        task = self._restoration_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                                       timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass


class _SlotContext:
    """Async context manager binding acquire/release to one `async with` block."""

    __slots__ = ("_gov", "_req", "_timeout", "_wait_ms", "_held")

    def __init__(self, governor: ResidencyGovernor, request: WorkRequest, *,
                 timeout_s: float) -> None:
        self._gov = governor
        self._req = request
        self._timeout = timeout_s
        self._wait_ms = 0.0
        self._held = False

    async def __aenter__(self) -> "WorkRequest":
        self._wait_ms = await self._gov.acquire(self._req, timeout_s=self._timeout)
        self._held = True
        self._gov.note_on_demand_grant(self._req.role, reason=self._req.reason)
        return self._req

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._held:
            if exc_type is asyncio.CancelledError:
                self._gov.metrics.cancellations += 1
            self._gov.release(self._req, wait_ms=self._wait_ms)
            self._held = False
        return False

    @property
    def wait_ms(self) -> float:
        return self._wait_ms


# ── Process-global singleton ─────────────────────────────────────────────────
_governor: ResidencyGovernor | None = None


def get_governor() -> ResidencyGovernor:
    global _governor
    if _governor is None:
        stopping = None
        try:
            from core.lifecycle import get_lifecycle
            stopping = get_lifecycle().is_stopping
        except Exception:  # noqa: BLE001
            pass
        _governor = ResidencyGovernor(is_stopping=stopping)
    return _governor


def reset_governor(instance: ResidencyGovernor | None = None) -> None:
    """Tests / a fresh process."""
    global _governor
    _governor = instance
