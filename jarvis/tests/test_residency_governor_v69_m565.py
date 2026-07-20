"""tests/test_residency_governor_v69_m565.py — V69 M56.5 inference arbitration.

The rule that motivated this: a background embedding batch must never make the
operator's live FAST turn wait. The mirror rule matters just as much: background work
must never be silently dropped or starved forever, because a lost semantic write is a
lost memory. Both are asserted here, along with serialization, bounded queues,
cancellation, on-demand slot borrowing and shutdown behaviour.
"""
from __future__ import annotations

import asyncio

import pytest

from core.residency_governor import (
    ON_DEMAND_ROLES,
    GovernorClosed,
    Priority,
    ResidencyGovernor,
    WorkRequest,
    get_governor,
    reset_governor,
)

FAST = "qwen3:8b"
EMBED = "nomic-embed-text:latest"


def teardown_function(_):
    reset_governor()


def _run(coro):
    return asyncio.run(coro)


# ── priority ordering ────────────────────────────────────────────────────────
def test_priority_ladder_is_explicit_and_ordered():
    assert (Priority.CRITICAL < Priority.INTERACTIVE < Priority.VERIFICATION
            < Priority.SEMANTIC_QUERY < Priority.BACKGROUND < Priority.PREWARM)


def test_active_fast_turn_outranks_queued_background_embedding():
    gov = ResidencyGovernor()
    order: list[str] = []

    async def work(role, priority, hold):
        async with gov.slot(role=role, priority=priority):
            order.append(role)
            await asyncio.sleep(hold)

    async def scenario():
        # A background embedding batch takes the slot first...
        holder = asyncio.ensure_future(work("embedding", Priority.BACKGROUND, 0.05))
        await asyncio.sleep(0.01)
        # ...then a background batch and a live FAST turn both queue behind it.
        b2 = asyncio.ensure_future(work("embedding2", Priority.BACKGROUND, 0))
        await asyncio.sleep(0)
        fast = asyncio.ensure_future(work("fast", Priority.INTERACTIVE, 0))
        await asyncio.gather(holder, b2, fast)

    _run(scenario())
    assert order[0] == "embedding"
    # The live turn jumps the queued background work, even though it arrived later.
    assert order[1] == "fast", f"expected fast to preempt queued background, got {order}"
    assert order[2] == "embedding2"


def test_hitl_critical_outranks_an_interactive_fast_turn():
    gov = ResidencyGovernor()
    order: list[str] = []

    async def work(role, priority):
        async with gov.slot(role=role, priority=priority):
            order.append(role)

    async def scenario():
        blocker = WorkRequest(role="blocker", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        f = asyncio.ensure_future(work("fast", Priority.INTERACTIVE))
        await asyncio.sleep(0)
        c = asyncio.ensure_future(work("hitl", Priority.CRITICAL))
        await asyncio.sleep(0)
        gov.release(blocker)
        await asyncio.gather(f, c)

    _run(scenario())
    assert order == ["hitl", "fast"]


def test_prewarm_is_always_last():
    gov = ResidencyGovernor()
    order: list[str] = []

    async def work(role, priority):
        async with gov.slot(role=role, priority=priority):
            order.append(role)

    async def scenario():
        blocker = WorkRequest(role="blocker", priority=Priority.CRITICAL)
        await gov.acquire(blocker)
        tasks = [asyncio.ensure_future(work("prewarm", Priority.PREWARM))]
        await asyncio.sleep(0)
        tasks.append(asyncio.ensure_future(work("background", Priority.BACKGROUND)))
        await asyncio.sleep(0)
        tasks.append(asyncio.ensure_future(work("fast", Priority.INTERACTIVE)))
        await asyncio.sleep(0)
        gov.release(blocker)
        await asyncio.gather(*tasks)

    _run(scenario())
    assert order == ["fast", "background", "prewarm"]


def test_equal_priority_is_fifo():
    gov = ResidencyGovernor()
    order: list[int] = []

    async def work(i):
        async with gov.slot(role=f"bg{i}", priority=Priority.BACKGROUND):
            order.append(i)

    async def scenario():
        blocker = WorkRequest(role="blocker", priority=Priority.CRITICAL)
        await gov.acquire(blocker)
        tasks = []
        for i in range(4):
            tasks.append(asyncio.ensure_future(work(i)))
            await asyncio.sleep(0)
        gov.release(blocker)
        await asyncio.gather(*tasks)

    _run(scenario())
    assert order == [0, 1, 2, 3]


# ── serialization ────────────────────────────────────────────────────────────
def test_heavy_inference_is_serialized_by_default():
    gov = ResidencyGovernor()
    concurrent = {"now": 0, "max": 0}

    async def work(role, priority):
        async with gov.slot(role=role, priority=priority):
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
            await asyncio.sleep(0.01)
            concurrent["now"] -= 1

    async def scenario():
        await asyncio.gather(*[work(f"r{i}", Priority.INTERACTIVE) for i in range(5)])

    _run(scenario())
    assert concurrent["max"] == 1, "no two heavy generations may run at once"


def test_no_simultaneous_deep_and_fast_generation():
    gov = ResidencyGovernor()
    active: list[str] = []
    overlaps: list[tuple] = []

    async def work(role):
        async with gov.slot(role=role, priority=Priority.INTERACTIVE):
            active.append(role)
            if len(active) > 1:
                overlaps.append(tuple(active))
            await asyncio.sleep(0.01)
            active.remove(role)

    async def scenario():
        await asyncio.gather(work("deep"), work("fast"))

    _run(scenario())
    assert overlaps == []


# ── background work is deferred, never lost ──────────────────────────────────
def test_deferred_background_work_eventually_runs():
    gov = ResidencyGovernor()
    done: list[str] = []

    async def work(role, priority):
        async with gov.slot(role=role, priority=priority):
            done.append(role)

    async def scenario():
        blocker = WorkRequest(role="fast", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        bg = asyncio.ensure_future(work("embedding", Priority.BACKGROUND))
        await asyncio.sleep(0)
        assert gov.queue_depth == 1
        gov.release(blocker)
        await bg

    _run(scenario())
    assert done == ["embedding"], "background semantic work must not be dropped"
    assert gov.metrics.background_deferrals == 1


def test_aging_prevents_permanent_starvation():
    """A background waiter old enough is promoted, so a stream of live turns cannot
    starve it forever."""
    t = [0.0]
    gov = ResidencyGovernor(clock=lambda: t[0])
    order: list[str] = []

    async def work(role, priority):
        async with gov.slot(role=role, priority=priority):
            order.append(role)

    async def scenario():
        blocker = WorkRequest(role="blocker", priority=Priority.CRITICAL)
        await gov.acquire(blocker)
        bg = asyncio.ensure_future(work("old_background", Priority.BACKGROUND))
        await asyncio.sleep(0)
        t[0] = 200.0        # the background request has now waited 200s
        fresh = asyncio.ensure_future(work("fresh_fast", Priority.INTERACTIVE))
        await asyncio.sleep(0)
        gov.release(blocker)
        await asyncio.gather(bg, fresh)

    _run(scenario())
    assert order[0] == "old_background"
    assert gov.metrics.starvation_preventions >= 1


# ── bounded queues ───────────────────────────────────────────────────────────
def test_queue_capacity_is_bounded_and_refusal_is_explicit():
    gov = ResidencyGovernor(capacity=2)

    async def scenario():
        blocker = WorkRequest(role="fast", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        queued = [WorkRequest(role=f"bg{i}", priority=Priority.BACKGROUND)
                  for i in range(2)]
        waiting = [asyncio.ensure_future(gov.acquire(req)) for req in queued]
        await asyncio.sleep(0)
        with pytest.raises(GovernorClosed):
            await gov.acquire(WorkRequest(role="overflow", priority=Priority.BACKGROUND))
        gov.release(blocker)
        # Each waiter must be released in turn, or the next one never gets the slot.
        for req, fut in zip(queued, waiting):
            await asyncio.wait_for(fut, timeout=1.0)
            gov.release(req)

    _run(scenario())
    assert gov.metrics.rejections == 1
    assert gov.metrics.high_watermark == 2


def test_acquire_timeout_is_bounded_and_releases_the_waiter():
    gov = ResidencyGovernor()

    async def scenario():
        blocker = WorkRequest(role="deep", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        with pytest.raises(asyncio.TimeoutError):
            await gov.acquire(WorkRequest(role="fast", priority=Priority.INTERACTIVE),
                              timeout_s=0.02)
        assert gov.queue_depth == 0, "a timed-out waiter must not stay queued"
        gov.release(blocker)

    _run(scenario())


# ── cancellation ─────────────────────────────────────────────────────────────
def test_cancellation_releases_the_slot_for_the_next_request():
    gov = ResidencyGovernor()
    served: list[str] = []

    async def cancellable():
        async with gov.slot(role="deep", priority=Priority.INTERACTIVE):
            served.append("deep")
            await asyncio.sleep(3600)

    async def follower():
        async with gov.slot(role="fast", priority=Priority.INTERACTIVE):
            served.append("fast")

    async def scenario():
        t = asyncio.ensure_future(cancellable())
        await asyncio.sleep(0.01)
        f = asyncio.ensure_future(follower())
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.wait_for(f, timeout=1.0)

    _run(scenario())
    assert served == ["deep", "fast"]
    assert gov.metrics.cancellations >= 1


def test_cancelling_a_queued_waiter_does_not_leak_the_slot():
    gov = ResidencyGovernor()

    async def scenario():
        blocker = WorkRequest(role="fast", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        w = asyncio.ensure_future(gov.acquire(
            WorkRequest(role="bg", priority=Priority.BACKGROUND)))
        await asyncio.sleep(0)
        w.cancel()
        await asyncio.gather(w, return_exceptions=True)
        gov.release(blocker)
        # The slot must be grantable again immediately.
        await asyncio.wait_for(
            gov.acquire(WorkRequest(role="fast2", priority=Priority.INTERACTIVE)),
            timeout=1.0)

    _run(scenario())


# ── duplicate load suppression ───────────────────────────────────────────────
def test_duplicate_concurrent_model_load_is_refused():
    gov = ResidencyGovernor()
    assert gov.begin_load(FAST) is True
    assert gov.begin_load(FAST) is False, "two cold loads of one model is pure waste"
    assert gov.metrics.duplicate_loads_avoided == 1
    gov.end_load(FAST)
    assert gov.begin_load(FAST) is True
    # A different model is unaffected.
    assert gov.begin_load(EMBED) is True


# ── on-demand slot borrowing (M56.5.1) ───────────────────────────────────────
def test_on_demand_roles_are_the_expected_set():
    assert ON_DEMAND_ROLES == {"deep", "coder", "vision", "verifier"}


def test_deep_request_waits_inside_its_own_budget():
    gov = ResidencyGovernor()

    async def scenario():
        blocker = WorkRequest(role="fast", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        with pytest.raises(asyncio.TimeoutError):
            # DEEP waits inside the REQUESTING turn's budget, not an unrelated one.
            await gov.acquire(WorkRequest(role="deep", priority=Priority.INTERACTIVE),
                              timeout_s=0.02)
        gov.release(blocker)

    _run(scenario())


def test_grant_records_the_reason_without_evicting_anything():
    gov = ResidencyGovernor()

    async def scenario():
        async with gov.slot(role="deep", priority=Priority.INTERACTIVE,
                            reason="operator_deep_analysis"):
            pass

    _run(scenario())
    snap = gov.snapshot()
    assert snap["restoration_reason"] == "operator_deep_analysis"
    # The governor never unloads a model itself.
    assert "unload" not in repr(snap) and "evict_model" not in repr(snap)


def test_restoration_is_flagged_only_from_an_observation():
    gov = ResidencyGovernor()
    assert gov.needs_restoration() is False
    # FAST observed missing after the heavy workload -> restoration needed.
    assert gov.note_residency_observation([EMBED], fast_model=FAST) is True
    assert gov.needs_restoration() is True
    # A later observation showing FAST back clears it.
    assert gov.note_residency_observation([EMBED, FAST], fast_model=FAST) is False


def test_restoration_is_not_scheduled_while_heavy_work_is_active():
    gov = ResidencyGovernor()
    gov.note_residency_observation([EMBED], fast_model=FAST)
    ran = {"n": 0}

    async def restore():
        ran["n"] += 1

    async def scenario():
        held = WorkRequest(role="deep", priority=Priority.INTERACTIVE)
        await gov.acquire(held)
        assert gov.schedule_restoration(restore) is None
        gov.release(held)
        task = gov.schedule_restoration(restore)
        assert task is not None
        await task

    _run(scenario())
    assert ran["n"] == 1


def test_restoration_is_never_scheduled_after_stopping():
    gov = ResidencyGovernor(is_stopping=lambda: True)
    gov.note_residency_observation([EMBED], fast_model=FAST)
    ran = {"n": 0}

    async def restore():
        ran["n"] += 1

    async def scenario():
        return gov.schedule_restoration(restore)

    assert _run(scenario()) is None
    assert ran["n"] == 0


def test_restoration_is_not_stacked():
    gov = ResidencyGovernor()
    gov.note_residency_observation([EMBED], fast_model=FAST)
    started = asyncio.Event()
    release = asyncio.Event()
    ran = {"n": 0}

    async def restore():
        ran["n"] += 1
        started.set()
        await release.wait()

    async def scenario():
        t1 = gov.schedule_restoration(restore)
        await started.wait()
        t2 = gov.schedule_restoration(restore)
        assert t1 is t2
        release.set()
        await t1

    _run(scenario())
    assert ran["n"] == 1


def test_restoration_failure_is_swallowed_and_clears_the_flag():
    gov = ResidencyGovernor()
    gov.note_residency_observation([EMBED], fast_model=FAST)

    async def restore():
        raise RuntimeError("server busy")

    async def scenario():
        task = gov.schedule_restoration(restore)
        await task
        return task

    task = _run(scenario())
    assert task.exception() is None
    assert gov.needs_restoration() is False


# ── shutdown ─────────────────────────────────────────────────────────────────
def test_shutdown_rejects_new_work():
    gov = ResidencyGovernor()

    async def scenario():
        await gov.close()
        with pytest.raises(GovernorClosed):
            await gov.acquire(WorkRequest(role="fast", priority=Priority.INTERACTIVE))

    _run(scenario())


def test_shutdown_fails_waiters_instead_of_leaving_them_pending():
    gov = ResidencyGovernor()

    async def scenario():
        blocker = WorkRequest(role="fast", priority=Priority.INTERACTIVE)
        await gov.acquire(blocker)
        w = asyncio.ensure_future(gov.acquire(
            WorkRequest(role="bg", priority=Priority.BACKGROUND)))
        await asyncio.sleep(0)
        await gov.close()
        with pytest.raises(GovernorClosed):
            await w

    _run(scenario())


def test_stopping_lifecycle_refuses_admission():
    gov = ResidencyGovernor(is_stopping=lambda: True)

    async def scenario():
        with pytest.raises(GovernorClosed):
            await gov.acquire(WorkRequest(role="fast", priority=Priority.INTERACTIVE))

    _run(scenario())
    assert gov.metrics.rejections == 1


def test_close_cancels_an_in_flight_restoration():
    gov = ResidencyGovernor()
    gov.note_residency_observation([EMBED], fast_model=FAST)

    async def restore():
        await asyncio.sleep(3600)

    async def scenario():
        task = gov.schedule_restoration(restore)
        await asyncio.sleep(0)
        await gov.close()
        return task

    task = _run(scenario())
    assert task.cancelled() or task.done()


# ── metrics ──────────────────────────────────────────────────────────────────
def test_metrics_expose_depth_wait_and_capacity_without_content():
    gov = ResidencyGovernor(capacity=8)

    async def scenario():
        async with gov.slot(role="fast", priority=Priority.INTERACTIVE,
                            reason="user_turn"):
            pass

    _run(scenario())
    snap = gov.snapshot()
    for key in ("active_role", "active_priority", "queue_depth", "queue_capacity",
                "high_watermark", "average_wait_ms", "background_deferrals",
                "cancellations", "starvation_preventions"):
        assert key in snap
    assert snap["queue_capacity"] == 8
    assert snap["completed"] == 1
    flat = repr(snap).lower()
    for forbidden in ("prompt", "hola", "content", "vector"):
        assert forbidden not in flat


def test_singleton_is_resettable():
    g = get_governor()
    assert get_governor() is g
    reset_governor()
    assert get_governor() is not g
