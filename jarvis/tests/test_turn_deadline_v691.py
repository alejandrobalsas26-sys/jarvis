"""
tests/test_turn_deadline_v691.py — V69 M54.1.5/.6/.12: the real outer turn deadline.

Proves what M54 only claimed. M54's TurnBudget was a passive stopwatch (no asyncio
import at all) whose `_budget` reached exactly ONE call site repo-wide — the
verifier — so it deadlined the stage that was already bounded and left generation
unbounded. Its tests were pure FakeClock arithmetic: every assertion would still
have passed if TurnBudget were deleted from the runtime, because none asserted that
anything CALLED it.

These tests assert the BOUNDARY, not the arithmetic:
  * the deadline covers lock/queue wait and first-token wait (not just "stages");
  * a stalled stream is really cancelled and the async generator really closed;
  * no late chunk escapes after the deadline;
  * locks/semaphores are released and a SECOND turn succeeds immediately.

No live Ollama: fakes stall on demand under a fake clock.
"""
from __future__ import annotations

import asyncio

from core.turn_budget import (
    StageTimeouts,
    TurnBudget,
    TurnTimeout,
    bounded_stream,
    budget_for,
    timeouts_for,
)


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class StreamProbe:
    """A fake chat_stream. Records whether it was closed and whether it kept
    producing after the deadline (a late chunk = leaked inference)."""

    def __init__(self, chunks, *, stall_after=None, stall_forever=False,
                 clock=None, per_chunk_s=0.0, lock=None):
        self.chunks = list(chunks)
        self.stall_after = stall_after
        self.stall_forever = stall_forever
        self.clock = clock
        self.per_chunk_s = per_chunk_s
        self.lock = lock
        self.closed = False
        self.emitted = 0
        self.emitted_after_close = 0
        self.lock_held = False

    async def __aiter__(self):
        # Mirrors the real shape: acquire the inference lock, then stream.
        if self.lock is not None:
            await self.lock.acquire()
            self.lock_held = True
        try:
            for i, c in enumerate(self.chunks):
                if self.stall_forever or (self.stall_after is not None
                                          and i >= self.stall_after):
                    await asyncio.sleep(3600)   # the hang
                if self.per_chunk_s and self.clock is not None:
                    self.clock.advance(self.per_chunk_s)
                if self.closed:
                    self.emitted_after_close += 1
                self.emitted += 1
                yield c
        finally:
            # The real generator releases the lock / closes the HTTP response here;
            # aclose() is what makes this run.
            self.closed = True
            if self.lock is not None and self.lock_held:
                self.lock.release()
                self.lock_held = False


def _agen(probe):
    return probe.__aiter__()


# -- The deadline is real ------------------------------------------------------
def test_first_token_stall_is_cancelled_and_generator_closed():
    """The live failure: the server connects but never yields a token. Before this
    patch the turn parked on the SDK's read=600 default."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=60.0, clock=clk)
        probe = StreamProbe(["never"], stall_forever=True)
        t = StageTimeouts(first_token_s=0.05, idle_s=0.05, total_s=60.0)
        got = []
        try:
            async for c in bounded_stream(_agen(probe), budget=budget, timeouts=t):
                got.append(c)
            raise AssertionError("expected TurnTimeout")
        except TurnTimeout as exc:
            assert exc.stage == "first_token", f"wrong stage: {exc.stage}"
        assert got == [], "no chunk should have been produced"
        await asyncio.sleep(0)
        assert probe.closed is True, "the async generator must be closed (aclose)"

    asyncio.run(_run())


def test_stream_that_starts_then_stalls_is_cancelled():
    """One chunk then silence must not hold the operator forever."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=60.0, clock=clk)
        probe = StreamProbe(["hola", "resto"], stall_after=1)
        t = StageTimeouts(first_token_s=1.0, idle_s=0.05, total_s=60.0)
        got = []
        try:
            async for c in bounded_stream(_agen(probe), budget=budget, timeouts=t):
                got.append(c)
            raise AssertionError("expected TurnTimeout")
        except TurnTimeout as exc:
            assert exc.stage == "stream_idle", f"wrong stage: {exc.stage}"
        assert got == ["hola"], "chunks before the stall are kept"
        await asyncio.sleep(0)
        assert probe.closed is True

    asyncio.run(_run())


def test_total_deadline_includes_lock_wait():
    """M54.1.5 — semaphore/lock acquisition must count against the deadline. A turn
    that never gets the lock must still return control."""
    async def _run():
        clk = FakeClock()
        lock = asyncio.Lock()
        await lock.acquire()          # somebody else holds the model lock
        budget = TurnBudget(total_s=60.0, clock=clk)
        probe = StreamProbe(["x"], lock=lock)
        t = StageTimeouts(first_token_s=0.05, idle_s=0.05, total_s=60.0)
        try:
            async for _c in bounded_stream(_agen(probe), budget=budget, timeouts=t):
                raise AssertionError("must not stream while the lock is held")
            raise AssertionError("expected TurnTimeout")
        except TurnTimeout as exc:
            # The wait happened INSIDE the generator, before any token — exactly
            # like Ollama's server-side model swap.
            assert exc.stage == "first_token"
        assert probe.emitted == 0
        lock.release()

    asyncio.run(_run())


def test_total_budget_is_a_true_ceiling_even_with_a_generous_stage_bound():
    """min(stage bound, remaining total): a per-stage limit can never exceed the
    turn total, so the total is the real ceiling."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=10.0, clock=clk)
        clk.advance(10.0)             # budget already exhausted
        probe = StreamProbe(["x"])
        t = StageTimeouts(first_token_s=999.0, idle_s=999.0, total_s=10.0)
        try:
            async for _c in bounded_stream(_agen(probe), budget=budget, timeouts=t):
                raise AssertionError("no chunk after the total expired")
            raise AssertionError("expected TurnTimeout")
        except TurnTimeout as exc:
            assert exc.stage == "total"

    asyncio.run(_run())


def test_no_late_chunks_after_timeout():
    """A cancelled stream must not deliver anything afterwards."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=60.0, clock=clk)
        probe = StreamProbe(["a", "b", "c"], stall_after=1)
        t = StageTimeouts(first_token_s=1.0, idle_s=0.05, total_s=60.0)
        got = []
        try:
            async for c in bounded_stream(_agen(probe), budget=budget, timeouts=t):
                got.append(c)
        except TurnTimeout:
            pass
        await asyncio.sleep(0.05)     # give any orphan a chance to misbehave
        assert got == ["a"]
        assert probe.emitted_after_close == 0, "a chunk escaped after the deadline"

    asyncio.run(_run())


def test_lock_is_released_and_a_second_turn_succeeds():
    """M54.1.12 — turn 1 stalls -> timeout -> resources released -> turn 2 works
    immediately. This is the property the operator actually needs."""
    async def _run():
        clk = FakeClock()
        lock = asyncio.Lock()

        # Turn 1: acquires the lock, then stalls forever.
        b1 = TurnBudget(total_s=60.0, clock=clk)
        p1 = StreamProbe(["x"], stall_forever=True, lock=lock)
        t = StageTimeouts(first_token_s=0.05, idle_s=0.05, total_s=60.0)
        try:
            async for _c in bounded_stream(_agen(p1), budget=b1, timeouts=t):
                pass
        except TurnTimeout as exc:
            assert exc.stage == "first_token"
        await asyncio.sleep(0)
        assert p1.closed is True
        assert not lock.locked(), "a stuck model lock would wedge every later turn"

        # Turn 2: must succeed immediately.
        b2 = TurnBudget(total_s=60.0, clock=clk)
        p2 = StreamProbe(["La ", "raíz ", "cúbica"], lock=lock)
        got = []
        async for c in bounded_stream(_agen(p2), budget=b2, timeouts=t):
            got.append(c)
        assert "".join(got) == "La raíz cúbica"
        assert not lock.locked()

    asyncio.run(_run())


def test_successful_stream_still_closes_the_generator():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        probe = StreamProbe(["ok"])
        got = [c async for c in bounded_stream(_agen(probe), budget=budget)]
        assert got == ["ok"]
        await asyncio.sleep(0)
        assert probe.closed is True

    asyncio.run(_run())


def test_cancellation_from_outside_closes_the_generator():
    """Ctrl+C / outer cancellation must also release resources."""
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        lock = asyncio.Lock()
        probe = StreamProbe(["x"], stall_forever=True, lock=lock)

        async def _consume():
            async for _c in bounded_stream(_agen(probe), budget=budget):
                pass

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        assert probe.closed is True, "cancellation must still close the generator"
        assert not lock.locked(), "cancellation must release the lock"

    asyncio.run(_run())


# -- Timing ledger honesty -----------------------------------------------------
def test_first_token_time_is_recorded_and_model_load_stays_unknown():
    """M54.1.6 — do not fake model-load timing. Ollama cannot report it separately
    from prefill, so it must read as unknown (None), never 0.0 ('instant')."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=60.0, clock=clk)
        probe = StreamProbe(["a", "b"], clock=clk, per_chunk_s=2.0)
        got = [c async for c in bounded_stream(_agen(probe), budget=budget)]
        assert got == ["a", "b"]
        snap = budget.snapshot()
        assert snap["first_token_ms"] == 2000.0, "time-to-first-token is observable"
        assert snap["model_load_ms"] is None, "model_load is NOT separately observable"
        assert snap["connect_ms"] is None

    asyncio.run(_run())


def test_snapshot_reports_timeout_stage_and_cancel_success():
    budget = TurnBudget(total_s=60.0, clock=FakeClock())
    budget.timeout_stage = "first_token"
    budget.cancel_success = True
    budget.model_role = "FAST"
    snap = budget.snapshot()
    assert snap["timeout_stage"] == "first_token"
    assert snap["cancel_success"] is True
    assert snap["model_role"] == "FAST"


# -- Calibration (M54.1.7) -----------------------------------------------------
def test_budgets_are_finite_and_bounded_for_every_policy():
    """No policy may be unbounded, and none may exceed the hard cap."""
    from core.turn_policy import VerifyPolicy

    class _P:
        def __init__(self, vp):
            self.verify_policy = vp

    for vp in VerifyPolicy:
        total = budget_for(_P(vp))
        assert 5.0 <= total <= 300.0, f"{vp} budget {total} out of safe range"


def test_stage_bounds_never_exceed_the_total():
    t = StageTimeouts(first_token_s=999.0, idle_s=999.0, queue_wait_s=999.0,
                      total_s=30.0).clamped()
    assert t.first_token_s <= 30.0 and t.idle_s <= 30.0 and t.queue_wait_s <= 30.0


def test_operator_override_cannot_unbound_the_turn():
    """An env typo must not create an effectively unlimited wait."""
    class _S:
        turn_budget_scale = 10_000.0     # absurd
        turn_first_token_timeout_s = 10_000.0
        turn_stream_idle_timeout_s = 10_000.0

    t = timeouts_for(None, settings=_S())
    assert t.total_s <= 300.0, f"total escaped the hard cap: {t.total_s}"
    assert t.first_token_s <= t.total_s
