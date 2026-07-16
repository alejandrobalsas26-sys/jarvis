"""
tests/test_safe_enqueue_v691.py — V69 M54.1.1: safe thread->async enqueue.

Proves the exact live failure and its fix:

    ERROR:asyncio:Exception in callback Queue.put_nowait(WindowsPath(...))
    asyncio.queues.QueueFull

The decisive detail: a QueueFull raised inside a loop callback does NOT fail pytest
on its own — it goes to the loop's default exception handler and is printed. So
every test here installs `loop.set_exception_handler` and asserts it stayed empty.
Producers fire from a REAL threading.Thread: calling the handler inline would let the
old (dead) `except asyncio.QueueFull` catch spuriously and pass against a broken
implementation.

House convention: sync test fns driving `asyncio.run` (no pytest-asyncio dependency).
"""
from __future__ import annotations

import asyncio
import threading

from core.safe_enqueue import EventPriority, SafeEnqueue, safe_call_soon


class FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _capture_loop_errors(loop) -> list:
    """Install a recording exception handler; returns the list it appends to."""
    errs: list = []
    loop.set_exception_handler(lambda _loop, ctx: errs.append(ctx))
    return errs


async def _pump() -> None:
    """Let scheduled call_soon_threadsafe callbacks run."""
    for _ in range(5):
        await asyncio.sleep(0)


def _in_thread(fn) -> None:
    """Run `fn` on a REAL thread and join — inline calls would not reproduce the
    thread->loop handoff this module is about."""
    t = threading.Thread(target=fn)
    t.start()
    t.join()


# -- The regression: the OLD shape really does traceback -----------------------
def test_unsafe_shape_leaks_queuefull_to_loop_handler():
    """Characterizes the BUG this module fixes: scheduling `queue.put_nowait`
    directly lets QueueFull escape into the loop's exception handler, and the
    producer's own try/except cannot see it. If this ever stops behaving this way,
    the premise of M54.1.1 changed."""
    async def _run():
        loop = asyncio.get_running_loop()
        errs = _capture_loop_errors(loop)
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        q.put_nowait("occupied")
        caught: list = []

        def producer() -> None:
            try:
                # The exact live shape from yara_file_monitor.py:176.
                loop.call_soon_threadsafe(q.put_nowait, "overflow")
            except asyncio.QueueFull as exc:
                caught.append(exc)

        _in_thread(producer)
        await _pump()

        assert caught == [], "the producer thread cannot see a QueueFull raised in the loop"
        assert len(errs) == 1, "expected the unsafe shape to reach the loop handler"
        assert isinstance(errs[0].get("exception"), asyncio.QueueFull)

    asyncio.run(_run())


# -- The fix -------------------------------------------------------------------
def test_queuefull_is_caught_inside_the_loop_callback():
    """THE acceptance test: overflow through SafeEnqueue produces no loop-handler
    traceback, and the drop is counted instead."""
    async def _run():
        loop = asyncio.get_running_loop()
        errs = _capture_loop_errors(loop)
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        se = SafeEnqueue(queue=q, loop=loop, name="TEST_WATCHER",
                         clock=FakeClock(), warn_fn=lambda _m: None,
                         stopping_fn=lambda: False)

        _in_thread(lambda: [se.offer(f"item-{i}") for i in range(50)])
        await _pump()

        assert errs == [], f"QueueFull reached the loop exception handler: {errs}"
        m = se.metrics()
        assert m["queue_depth"] <= 2, "queue must stay bounded"
        assert m["dropped"] > 0, "overflow must be counted, not raised"
        assert m["received"] == 50

    asyncio.run(_run())


def test_producer_thread_never_sees_an_exception():
    """offer() must never raise into the producer thread, even while overflowing."""
    async def _run():
        loop = asyncio.get_running_loop()
        _capture_loop_errors(loop)
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        se = SafeEnqueue(queue=q, loop=loop, clock=FakeClock(),
                         warn_fn=lambda _m: None, stopping_fn=lambda: False)
        raised: list = []

        def producer() -> None:
            try:
                for i in range(100):
                    se.offer(i)
            except BaseException as exc:  # noqa: BLE001 - the whole point
                raised.append(exc)

        _in_thread(producer)
        await _pump()
        assert raised == []

    asyncio.run(_run())


def test_duplicate_events_coalesce_within_debounce_window():
    """Repeated events sharing a key collapse instead of each scheduling work —
    the Windows 'several on_modified per write' storm."""
    async def _run():
        loop = asyncio.get_running_loop()
        clk = FakeClock()
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        se = SafeEnqueue(queue=q, loop=loop, clock=clk, debounce_s=1.0,
                         warn_fn=lambda _m: None, stopping_fn=lambda: False)

        for _ in range(20):
            se.offer("C:/x/a.exe", key="c:/x/a.exe", priority=EventPriority.LOW)
        await _pump()

        m = se.metrics()
        assert m["accepted"] == 1, "only the first of a burst is enqueued"
        assert m["coalesced"] == 19
        assert q.qsize() == 1

        # Past the debounce window the same path is admitted again (real news).
        clk.advance(1.5)
        se.offer("C:/x/a.exe", key="c:/x/a.exe", priority=EventPriority.LOW)
        await _pump()
        assert se.metrics()["accepted"] == 2

    asyncio.run(_run())


def test_high_priority_event_evicts_a_low_priority_one():
    """A create/delete/security event must survive a queue full of modify noise."""
    async def _run():
        loop = asyncio.get_running_loop()
        errs = _capture_loop_errors(loop)
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        se = SafeEnqueue(queue=q, loop=loop, clock=FakeClock(),
                         warn_fn=lambda _m: None, stopping_fn=lambda: False)

        se.offer("noise-1", priority=EventPriority.LOW)
        se.offer("noise-2", priority=EventPriority.LOW)
        await _pump()
        assert q.qsize() == 2

        se.offer("SECURITY", priority=EventPriority.HIGH)
        await _pump()

        assert errs == []
        drained = [q.get_nowait() for _ in range(q.qsize())]
        assert "SECURITY" in drained, "HIGH must be admitted by evicting an older item"
        assert se.metrics()["last_drop_reason"] == "overflow_evicted_oldest"

    asyncio.run(_run())


def test_stopping_rejects_low_priority_but_admits_high():
    """Lifecycle: once STOPPING, no new low-value work is accepted."""
    async def _run():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        stopping = {"v": False}
        se = SafeEnqueue(queue=q, loop=loop, clock=FakeClock(),
                         warn_fn=lambda _m: None, stopping_fn=lambda: stopping["v"])

        se.offer("before", priority=EventPriority.NORMAL)
        await _pump()
        assert q.qsize() == 1

        stopping["v"] = True
        se.offer("after-low", priority=EventPriority.LOW)
        se.offer("after-normal", priority=EventPriority.NORMAL)
        await _pump()
        assert q.qsize() == 1, "low/normal work must be refused while stopping"
        assert se.metrics()["last_drop_reason"] == "stopping"
        assert se.metrics()["ignored"] == 2

        se.offer("after-high", priority=EventPriority.HIGH)
        await _pump()
        assert q.qsize() == 2, "HIGH still admitted during shutdown"

    asyncio.run(_run())


def test_one_bounded_warning_per_cooldown_not_one_per_event():
    """The live run printed a traceback per dropped path. Overflow must produce at
    most ONE aggregated warning per cooldown window."""
    async def _run():
        loop = asyncio.get_running_loop()
        clk = FakeClock()
        warnings: list = []
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        se = SafeEnqueue(queue=q, loop=loop, name="FILE_WATCHER", clock=clk,
                         warn_cooldown_s=10.0, warn_fn=warnings.append,
                         stopping_fn=lambda: False)

        for i in range(200):
            se.offer(f"p{i}")
        await _pump()
        assert len(warnings) == 1, f"expected 1 aggregated warning, got {len(warnings)}"
        assert "backpressure active" in warnings[0]
        assert "FILE_WATCHER" in warnings[0]

        clk.advance(11.0)
        for i in range(50):
            se.offer(f"q{i}")
        await _pump()
        assert len(warnings) == 2, "a new window may warn once more"

    asyncio.run(_run())


def test_overflow_signals_reconciliation_exactly_once():
    """Overflow must schedule ONE bounded reconciliation, never one per dropped
    event (M54.1.4)."""
    async def _run():
        loop = asyncio.get_running_loop()
        calls: list = []
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        se = SafeEnqueue(queue=q, loop=loop, clock=FakeClock(),
                         warn_fn=lambda _m: None, stopping_fn=lambda: False,
                         on_overflow=lambda: calls.append(1))

        for i in range(500):
            se.offer(f"p{i}")
        await _pump()
        assert len(calls) == 1, f"expected exactly one reconciliation signal, got {len(calls)}"

        # Re-arming allows a LATER episode to reconcile again.
        se.clear_overflow_signal()
        for i in range(10):
            se.offer(f"r{i}")
        await _pump()
        assert len(calls) == 2

    asyncio.run(_run())


def test_high_watermark_and_metric_shape():
    async def _run():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        se = SafeEnqueue(queue=q, loop=loop, clock=FakeClock(),
                         warn_fn=lambda _m: None, stopping_fn=lambda: False)
        for i in range(5):
            se.offer(i)
        await _pump()
        m = se.metrics()
        assert m["queue_high_watermark"] == 5
        assert m["queue_capacity"] == 8
        expected = {
            "received", "accepted", "coalesced", "ignored", "dropped", "overflows",
            "queue_depth", "queue_capacity", "queue_high_watermark",
            "last_drop_reason", "last_overflow_at", "warnings_emitted",
        }
        assert expected.issubset(set(m)), f"missing metrics: {expected - set(m)}"

    asyncio.run(_run())


def test_debounce_cache_stays_bounded_under_unique_keys():
    """An infinite stream of UNIQUE paths must not grow memory without bound."""
    async def _run():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        se = SafeEnqueue(queue=q, loop=loop, clock=FakeClock(), max_recent=64,
                         warn_fn=lambda _m: None, stopping_fn=lambda: False)
        for i in range(5000):
            se.offer(f"p{i}", key=f"key-{i}", priority=EventPriority.LOW)
        await _pump()
        assert len(se._recent) <= 64

    asyncio.run(_run())


def test_safe_call_soon_swallows_closed_loop():
    """A shutdown race must not raise into a producer thread."""
    loop = asyncio.new_event_loop()
    loop.close()
    assert safe_call_soon(loop, lambda: None) is False
