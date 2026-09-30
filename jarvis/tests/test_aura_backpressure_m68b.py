"""V69 M68B (B) — one slow AURA consumer cannot hold a producer.

THE DEFECT THESE PIN
--------------------
``BroadcastManager.broadcast()`` awaited ``ws.send_text(payload)`` once per client,
sequentially, on the producer's own stack. A browser tab that stopped reading
filled its TCP receive window; the send then waited on the transport drain with no
deadline. ``telemetry_broadcaster`` stalled, and — because ``core.correlator`` is
attached directly to ``manager.broadcast`` — so did compound-incident correlation.
Detection latency became a function of the slowest HUD tab.

The property under test is ISOLATION, not speed: a producer's completion must not
depend on any consumer. Every test below therefore measures whether the PRODUCER
returned while a consumer was still stuck, never how long a send took.

No real sockets: a fake websocket whose ``send_text`` awaits an Event is a more
reliable "slow client" than a real one, and the manager cannot tell the difference.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

# aura/server.py imports FastAPI at module scope. The AST-level absent-control
# checks in test_resource_lifecycle_absent_controls_m68b.py need no import and run
# everywhere; these BEHAVIOURAL tests need the real module, so they skip when the
# optional `all` profile is absent (the CI runner installs dev+soc only).
pytest.importorskip("fastapi")

from aura import server  # noqa: E402
from aura.server import (  # noqa: E402
    AURA_CLIENT_QUEUE_MAX,
    BroadcastManager,
    OverflowPolicy,
    _ClientChannel,
)


class _FakeWS:
    """A websocket that accepts everything, instantly."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


class _SlowWS(_FakeWS):
    """A websocket whose send never completes until released."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def send_text(self, data: str) -> None:
        self.entered.set()
        await self.gate.wait()
        self.sent.append(data)


class _DeadWS(_FakeWS):
    """A websocket whose send always raises, as a closed socket does."""

    async def send_text(self, data: str) -> None:
        raise ConnectionResetError("client gone")


async def _settle(times: int = 6) -> None:
    """Let the writer tasks run without depending on wall-clock timing."""
    for _ in range(times):
        await asyncio.sleep(0)


class TestProducerIsolation:
    def test_a_slow_client_does_not_hold_the_producer(self):
        async def scenario():
            mgr = BroadcastManager()
            slow, fast = _SlowWS(), _FakeWS()
            await mgr.connect(slow)
            await mgr.connect(fast)

            # The producer completes N broadcasts while `slow` is wedged inside
            # its FIRST send. Under the old sequential loop this awaited `slow`
            # and never reached `fast` at all.
            for i in range(5):
                await asyncio.wait_for(mgr.broadcast({"n": i}), timeout=2.0)
            await _settle()

            assert len(fast.sent) == 5, "a slow peer starved an unrelated consumer"
            assert slow.sent == [], "the slow client was not actually slow"
            slow.gate.set()
            await mgr.aclose(drain_s=0.05)

        asyncio.run(scenario())

    def test_a_dead_client_is_reaped_without_touching_the_producer(self):
        async def scenario():
            mgr = BroadcastManager()
            dead, fast = _DeadWS(), _FakeWS()
            await mgr.connect(dead)
            await mgr.connect(fast)
            await mgr.broadcast({"e": 1})
            await _settle()
            assert dead not in mgr._clients, "a dead client was never reaped"
            await mgr.broadcast({"e": 2})
            await _settle()
            assert len(fast.sent) == 2
            await mgr.aclose()

        asyncio.run(scenario())

    def test_correlation_producer_completes_despite_a_stuck_consumer(self):
        """The audit's real concern: correlation is attached to broadcast, so a
        wedged HUD tab must not delay incident correlation."""
        async def scenario():
            mgr = BroadcastManager()
            slow = _SlowWS()
            await mgr.connect(slow)
            produced = 0
            for i in range(50):
                await asyncio.wait_for(mgr.broadcast({"i": i}), timeout=2.0)
                produced += 1
            assert produced == 50
            slow.gate.set()
            await mgr.aclose(drain_s=0.05)

        asyncio.run(scenario())


class TestBoundedQueue:
    def test_queue_growth_is_bounded_and_overflow_is_counted(self):
        async def scenario():
            mgr = BroadcastManager(queue_max=4)
            slow = _SlowWS()
            await mgr.connect(slow)
            for i in range(50):
                await mgr.broadcast({"i": i})
            channel = mgr._channels[id(slow)]
            assert channel.depth() <= 4, "the per-connection queue is unbounded"
            assert channel.metrics.dropped_overflow > 0
            assert channel.metrics.last_drop_reason == "overflow_evicted_oldest"
            slow.gate.set()
            await mgr.aclose(drain_s=0.05)

        asyncio.run(scenario())

    def test_the_overflow_policy_keeps_the_newest_frame(self):
        """DROP_OLDEST, stated and observable. Telemetry is a stream of
        snapshots; the newest is the one a HUD needs."""
        async def scenario():
            ws = _SlowWS()
            channel = _ClientChannel(ws, maxsize=2, send_timeout=5.0)
            for i in range(5):
                channel.offer(f"frame-{i}")
            drained = []
            while channel.depth():
                drained.append(channel._queue.get_nowait())
            assert drained == ["frame-3", "frame-4"], drained
            assert channel.metrics.dropped_overflow == 3
            assert OverflowPolicy.DROP_OLDEST.value == "drop_oldest"

        asyncio.run(scenario())

    def test_the_default_capacity_is_finite(self):
        assert isinstance(AURA_CLIENT_QUEUE_MAX, int)
        assert 0 < AURA_CLIENT_QUEUE_MAX < 100_000


class TestSendDeadline:
    def test_a_client_that_misses_the_send_deadline_is_dropped(self):
        async def scenario():
            mgr = BroadcastManager(queue_max=4, send_timeout=0.05)
            slow = _SlowWS()
            await mgr.connect(slow)
            await mgr.broadcast({"x": 1})
            await asyncio.sleep(0.2)
            assert slow not in mgr._clients, "a wedged client was never timed out"
            slow.gate.set()
            await mgr.aclose(drain_s=0.05)

        asyncio.run(scenario())

    def test_the_send_deadline_is_finite(self):
        assert 0 < server.AURA_SEND_TIMEOUT_S < 600


class TestLifecycle:
    def test_disconnect_while_frames_are_queued_leaves_no_task(self):
        async def scenario():
            mgr = BroadcastManager(queue_max=8)
            slow = _SlowWS()
            await mgr.connect(slow)
            channel = mgr._channels[id(slow)]
            for i in range(8):
                await mgr.broadcast({"i": i})
            slow.gate.set()
            await mgr.aclose_client(slow, drain_s=0.5)
            assert channel.task.done(), "the writer task outlived the connection"
            assert slow not in mgr._clients
            assert id(slow) not in mgr._channels

        asyncio.run(scenario())

    def test_shutdown_with_pending_work_cancels_and_awaits_every_writer(self):
        async def scenario():
            mgr = BroadcastManager(queue_max=64)
            clients = [_SlowWS() for _ in range(5)]
            for ws in clients:
                await mgr.connect(ws)
            tasks = [c.task for c in mgr._channels.values()]
            for i in range(20):
                await mgr.broadcast({"i": i})
            await mgr.aclose(drain_s=0.05)
            assert all(t.done() for t in tasks), "a writer survived shutdown"
            assert mgr._clients == set()
            assert mgr._channels == {}

        asyncio.run(scenario())

    def test_no_writer_task_leaks_across_a_connect_disconnect_cycle(self):
        async def scenario():
            before = len(asyncio.all_tasks())
            mgr = BroadcastManager()
            for _ in range(10):
                ws = _FakeWS()
                await mgr.connect(ws)
                await mgr.broadcast({"ping": 1})
                await mgr.aclose_client(ws)
            await _settle()
            after = len(asyncio.all_tasks())
            assert after <= before + 1, f"leaked writer tasks: {before} -> {after}"

        asyncio.run(scenario())

    def test_exactly_one_writer_task_per_connection(self):
        async def scenario():
            mgr = BroadcastManager()
            clients = [_FakeWS() for _ in range(6)]
            for ws in clients:
                await mgr.connect(ws)
            for i in range(30):
                await mgr.broadcast({"i": i})
            await _settle()
            writers = [t for t in asyncio.all_tasks()
                       if (t.get_name() or "").startswith("aura-writer-")]
            assert len(writers) == 6, (
                f"{len(writers)} writer tasks for 6 connections — the fan-out is "
                "per-event, not per-connection")
            await mgr.aclose()

        asyncio.run(scenario())


class TestBoundedFireAndForget:
    def test_the_ingest_fan_out_is_bounded(self):
        """`broadcast()` spawned an untracked `create_task(correlator.ingest(...))`
        per event: unbounded concurrency, and a task asyncio may collect mid-await
        because it holds only a weak reference."""
        async def scenario():
            server._reset_bounded_tasks()
            spawned = []

            async def _never():
                spawned.append(1)
                await asyncio.sleep(3600)

            for _ in range(server.AURA_MAX_INFLIGHT_TASKS * 3):
                server._spawn_bounded(_never(), name="probe")
            await _settle()
            assert len(server._bounded_tasks) <= server.AURA_MAX_INFLIGHT_TASKS
            assert server.bounded_task_stats()["dropped"] > 0
            server._reset_bounded_tasks()

        asyncio.run(scenario())

    def test_bounded_spawn_keeps_a_strong_reference(self):
        async def scenario():
            server._reset_bounded_tasks()
            done = asyncio.Event()

            async def _work():
                await asyncio.sleep(0)
                done.set()

            task = server._spawn_bounded(_work(), name="probe")
            assert task in server._bounded_tasks
            await asyncio.wait_for(done.wait(), timeout=2.0)
            await _settle()
            assert task not in server._bounded_tasks, "the done callback never ran"

        asyncio.run(scenario())


class TestDirectSendsAreBounded:
    def test_a_wedged_client_cannot_hold_the_welcome_frame(self):
        """The welcome is sent straight to the socket, before the receive loop, so
        it bypasses the bounded channel entirely. A client that completes the
        handshake and then stops reading used to hold this connection's handler
        for as long as it liked."""
        async def scenario():
            class _NeverReads(_FakeWS):
                def __init__(self):
                    super().__init__()
                    self.headers = {"origin": "http://127.0.0.1:8765"}
                    self.cookies = {"aura_token": server._AURA_WS_TOKEN}
                    self.query_params: dict = {}
                    self.entered = asyncio.Event()

                async def send_json(self, payload):
                    self.entered.set()
                    await asyncio.sleep(3600)

                async def receive_text(self):
                    from fastapi import WebSocketDisconnect
                    raise WebSocketDisconnect(1000)

            ws = _NeverReads()
            original = server.AURA_SEND_TIMEOUT_S
            server.AURA_SEND_TIMEOUT_S = 0.05
            try:
                await asyncio.wait_for(server._ws_endpoint(ws), timeout=5.0)
            finally:
                server.AURA_SEND_TIMEOUT_S = original
            assert ws not in server.manager._clients

        asyncio.run(scenario())

    def test_the_bounded_sender_reports_a_dead_client(self):
        async def scenario():
            class _Dead(_FakeWS):
                async def send_json(self, payload):
                    raise ConnectionResetError("gone")

            assert await server._ws_send(_Dead(), {"a": 1}) is False
            assert await server._ws_send(_FakeWSJson(), {"a": 1}) is True

        asyncio.run(scenario())


class _FakeWSJson(_FakeWS):
    async def send_json(self, payload):
        self.sent.append(payload)


class TestEndpointCleanup:
    def test_the_endpoint_removes_a_client_on_any_exception(self):
        """The old endpoint cleaned up only inside `except WebSocketDisconnect`.
        Any other error left the client in the set with a live writer."""
        async def scenario():
            class _Exploding(_FakeWS):
                def __init__(self):
                    super().__init__()
                    self.headers = {"origin": "http://127.0.0.1:8765"}
                    self.cookies = {"aura_token": server._AURA_WS_TOKEN}
                    self.query_params: dict = {}

                async def receive_text(self):
                    raise RuntimeError("transport exploded")

            ws = _Exploding()
            with pytest.raises(RuntimeError):
                await server._ws_endpoint(ws)
            assert ws not in server.manager._clients
            assert id(ws) not in server.manager._channels

        asyncio.run(scenario())
