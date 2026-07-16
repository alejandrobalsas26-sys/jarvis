"""
core/safe_enqueue.py — V69 M54.1.1: the one safe thread->async enqueue seam.

The live boot printed dozens of full tracebacks:

    ERROR:asyncio:Exception in callback Queue.put_nowait(WindowsPath(...))
    asyncio.queues.QueueFull

The producer looked defended but was not:

    try:
        loop.call_soon_threadsafe(scan_queue.put_nowait, Path(event.src_path))
    except asyncio.QueueFull:      # dead code
        pass

`call_soon_threadsafe` only SCHEDULES a Handle and returns on the producer thread;
it raises RuntimeError (loop closed) and never QueueFull. The `put_nowait` runs
LATER on the loop thread, by which time that `except` frame has already returned —
so QueueFull escapes into the loop's default exception handler, one traceback per
dropped event. The try/except created a false sense of safety.

This module fixes the SHAPE, not the size: QueueFull is caught INSIDE the callback
that executes put_nowait. Producers hand work to `SafeEnqueue.offer()`, which never
blocks, never raises, and never lets a drop reach the event loop's handler.

Design:
  * offer() runs on the PRODUCER thread: dedup/debounce/stopping checks happen here
    so a storm never even schedules N callbacks (cheap, lock-guarded, non-blocking);
  * _put_in_loop() runs on the EVENT LOOP: the only frame that touches the queue,
    and the only place QueueFull can be raised — so it is the only place it is
    caught (the critical M54.1.1 rule);
  * priority: HIGH events (create/delete/security) evict an older LOW event rather
    than being dropped; LOW events are dropped first;
  * bounded everything: an LRU debounce cache, a fixed-capacity queue, counter-only
    metrics, and ONE overflow warning per cooldown (never one per dropped path);
  * lifecycle-aware: once STOPPING, new low-priority work is rejected outright.

Reused by the filesystem watchers (M54.1.2/.3) and safe for any thread->loop handoff.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable

# One warning per this window while backpressure persists (never one per path).
_DEFAULT_WARN_COOLDOWN_S = 10.0
# Repeated events sharing a coalesce key collapse inside this window.
_DEFAULT_DEBOUNCE_S = 1.0
# Upper bound on the debounce LRU — memory is bounded even under an infinite storm.
_DEFAULT_MAX_RECENT = 2048


class EventPriority(IntEnum):
    """Drop order. LOW is sacrificed first; HIGH survives by evicting a LOW event.

    LOW     repeated/derived noise (a second on_modified for the same path)
    NORMAL  ordinary work
    HIGH    create/delete/security events — operator-meaningful, dropped last
    """

    LOW = 0
    NORMAL = 1
    HIGH = 2


@dataclass
class EnqueueMetrics:
    """Bounded counters for runtime health. Counters only — never event payloads,
    user text, or paths (a path is operator data; only reasons are recorded)."""

    received: int = 0
    accepted: int = 0
    coalesced: int = 0
    ignored: int = 0
    dropped: int = 0
    overflows: int = 0
    queue_high_watermark: int = 0
    last_drop_reason: str | None = None
    last_overflow_at: float | None = None
    warnings_emitted: int = 0

    def snapshot(self, *, depth: int = 0, capacity: int = 0) -> dict:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "coalesced": self.coalesced,
            "ignored": self.ignored,
            "dropped": self.dropped,
            "overflows": self.overflows,
            "queue_depth": depth,
            "queue_capacity": capacity,
            "queue_high_watermark": self.queue_high_watermark,
            "last_drop_reason": self.last_drop_reason,
            "last_overflow_at": self.last_overflow_at,
            "warnings_emitted": self.warnings_emitted,
        }


def _lifecycle_is_stopping() -> bool:
    """Consult the lifecycle without importing it at module scope (keeps this
    module dependency-light and import-cheap on the hot path). Fails open: if the
    lifecycle is unavailable we are NOT stopping."""
    try:
        from core.lifecycle import is_stopping
        return is_stopping()
    except Exception:
        return False


@dataclass
class SafeEnqueue:
    """A thread-safe, non-blocking, overflow-safe handoff into an asyncio.Queue.

    Construct on the event loop (it captures the running loop by default), then
    call `offer()` from any producer thread. `offer()` returns True only when the
    item was actually scheduled toward the queue.
    """

    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop | None = None
    name: str = "queue"
    clock: Callable[[], float] = time.monotonic
    debounce_s: float = _DEFAULT_DEBOUNCE_S
    warn_cooldown_s: float = _DEFAULT_WARN_COOLDOWN_S
    max_recent: int = _DEFAULT_MAX_RECENT
    # Emitted at most once per cooldown while overflowing. Injected so tests
    # capture it and production routes it to loguru/console.
    warn_fn: Callable[[str], None] | None = None
    # Invoked (on the loop) the first time a root overflows, so the owner can
    # schedule exactly ONE bounded reconciliation (M54.1.4) — never one per event.
    on_overflow: Callable[[], None] | None = None
    # Lifecycle probe, injectable for deterministic tests.
    stopping_fn: Callable[[], bool] = _lifecycle_is_stopping

    _m: EnqueueMetrics = field(default_factory=EnqueueMetrics)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _recent: "OrderedDict[str, float]" = field(default_factory=OrderedDict)
    _last_warn_at: float | None = field(default=None)
    _overflow_signalled: bool = field(default=False)

    def __post_init__(self) -> None:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()

    # -- Producer-thread entry point -----------------------------------------
    def offer(self, item: Any, *, key: str | None = None,
              priority: EventPriority = EventPriority.NORMAL) -> bool:
        """Offer one item. Safe from ANY thread. Never blocks, never raises.

        Returns True if the item was scheduled toward the queue; False if it was
        coalesced, ignored (stopping) or dropped. A False return is normal
        backpressure, not an error — the caller must not retry or log per item.
        """
        with self._lock:
            self._m.received += 1
            # Lifecycle: once STOPPING, low-priority work is refused outright so a
            # late storm cannot resurrect work during shutdown. HIGH still passes
            # (a deletion during shutdown is still worth recording).
            if priority < EventPriority.HIGH and self.stopping_fn():
                self._m.ignored += 1
                self._m.last_drop_reason = "stopping"
                return False
            # Debounce/coalesce on the producer thread: a repeat inside the window
            # never even schedules a callback. HIGH is never coalesced away.
            if key is not None and priority < EventPriority.HIGH:
                now = self.clock()
                last = self._recent.get(key)
                if last is not None and (now - last) < self.debounce_s:
                    self._recent[key] = now
                    self._recent.move_to_end(key)
                    self._m.coalesced += 1
                    return False
                self._recent[key] = now
                self._recent.move_to_end(key)
                while len(self._recent) > self.max_recent:
                    self._recent.popitem(last=False)

        try:
            # Schedule the ONLY frame that touches the queue. A bound method here
            # (self._put_in_loop) is a plain callable that handles its own errors —
            # never `queue.put_nowait` directly, which would raise into the loop.
            self.loop.call_soon_threadsafe(self._put_in_loop, item, priority)
        except RuntimeError:
            # Loop closed/closing (shutdown race). Never surface to the producer.
            with self._lock:
                self._m.dropped += 1
                self._m.last_drop_reason = "loop_closed"
            return False
        return True

    # -- Event-loop frame (THE critical one) ---------------------------------
    def _put_in_loop(self, item: Any, priority: EventPriority) -> None:
        """Runs ON the event loop. This is the frame M54.1.1 is about: put_nowait
        executes here, so QueueFull is caught HERE and never reaches the loop's
        default exception handler. It must never raise."""
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._handle_overflow(item, priority)
            return
        except Exception:
            # A queue must never crash the loop. Count it; do not traceback.
            with self._lock:
                self._m.dropped += 1
                self._m.last_drop_reason = "put_error"
            return
        with self._lock:
            self._m.accepted += 1
            depth = self.queue.qsize()
            if depth > self._m.queue_high_watermark:
                self._m.queue_high_watermark = depth

    def _handle_overflow(self, item: Any, priority: EventPriority) -> None:
        """The queue is full. Apply the drop policy, count it, and emit AT MOST one
        bounded warning per cooldown. Runs on the loop; must never raise."""
        admitted = False
        if priority >= EventPriority.HIGH:
            # Operator-meaningful: make room by discarding the oldest queued item
            # (public API only — get_nowait/put_nowait, no private deque access).
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(item)
                admitted = True
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                admitted = False
        with self._lock:
            self._m.overflows += 1
            self._m.last_overflow_at = self.clock()
            self._m.dropped += 1  # exactly one item is lost either way
            self._m.last_drop_reason = "overflow_evicted_oldest" if admitted else "overflow"
            if admitted:
                self._m.accepted += 1
            first_overflow = not self._overflow_signalled
            self._overflow_signalled = True
        self._maybe_warn()
        # Exactly ONE reconciliation signal per overflow episode, not one per event.
        if first_overflow and self.on_overflow is not None:
            try:
                self.on_overflow()
            except Exception:
                pass

    def _maybe_warn(self) -> None:
        """One bounded, aggregated warning per cooldown window — never one per
        dropped path (that flooding is exactly what we are fixing)."""
        with self._lock:
            now = self.clock()
            if (self._last_warn_at is not None
                    and (now - self._last_warn_at) < self.warn_cooldown_s):
                return
            self._last_warn_at = now
            self._m.warnings_emitted += 1
            msg = (
                f"{self.name}: backpressure active — "
                f"{self._m.coalesced} events coalesced, {self._m.dropped} dropped"
            )
        fn = self.warn_fn
        if fn is None:
            try:
                from loguru import logger
                logger.warning(msg)
            except Exception:
                pass
            return
        try:
            fn(msg)
        except Exception:
            pass

    # -- Recovery / observability --------------------------------------------
    def clear_overflow_signal(self) -> None:
        """Re-arm the one-shot overflow signal (called once a reconciliation for
        this root has completed, so a LATER episode can schedule another one)."""
        with self._lock:
            self._overflow_signalled = False

    def metrics(self) -> dict:
        with self._lock:
            m = self._m
            snap = m.snapshot(depth=self.queue.qsize(),
                              capacity=self.queue.maxsize)
        return snap


def safe_call_soon(loop: asyncio.AbstractEventLoop, fn: Callable[..., Any],
                   *args: Any) -> bool:
    """Schedule `fn(*args)` on `loop` from any thread, swallowing the loop-closed
    race. `fn` MUST handle its own exceptions — anything it raises lands in the
    loop's default handler (which is the bug this module exists to prevent).
    Returns False if the loop could not accept the callback."""
    try:
        loop.call_soon_threadsafe(fn, *args)
        return True
    except RuntimeError:
        return False
