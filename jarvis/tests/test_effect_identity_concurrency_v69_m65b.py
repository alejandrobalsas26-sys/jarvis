"""
tests/test_effect_identity_concurrency_v69_m65b.py — V69 M65B EFFECT IDENTITY
UNDER CONCURRENCY.

This file is about ONE layer: ``ToolExecutor``'s effect ledger, and whether it
still says "exactly once" when two callers arrive at the same time.

WHY IT IS A SEPARATE FILE FROM THE CONFLICT SCHEDULER
-----------------------------------------------------
M65B has two independent controls that both stop a duplicate effect, and
conflating them is how a milestone claims a property it does not have:

  * RESOURCE_CONFLICT_CONTROL — the team scheduler refuses to run two tasks that
    claim the same resource at the same time. It protects effects the SCHEDULER
    routed.
  * EFFECT_IDENTITY_DEDUPE — this file. The ledger refuses to run one effect
    identity twice, whoever asks and however they got here.

Every test below calls ``ToolExecutor.aexecute`` directly or drives a path with
the scheduler's serialisation deliberately lifted, so nothing here can pass
because a scheduler happened to serialise it.

WHY THE WINDOW HAS TO BE FORCED
-------------------------------
The race is narrow and real: the ledger is READ near the top of the gate and
WRITTEN after the handler returns, with the HITL challenge, an AURA broadcast and
``run_in_executor`` awaiting in between. A duplicate that arrives after the owner
has already committed is caught by the plain ledger read and proves nothing about
the reservation.

So every concurrency test here PARKS the owner inside that window — in the HITL
challenge, which is after the ledger read and before the handler — and only then
admits the duplicate. Measured: the first version of these tests did not park the
owner, the duplicate always arrived after the commit, and two mutations that
delete the reservation outright survived the whole suite.

Nothing here sleeps "long enough". Ordering is established with
``asyncio.Event`` and a single ``asyncio.sleep(0)`` hand-off, which on a
single-threaded event loop is a scheduling fact rather than a timing hope. Every
wait is bounded, so a broken implementation FAILS instead of hanging.
"""
from __future__ import annotations

import asyncio
import dataclasses
import threading

import pytest

from core.cognitive_mesh import REGISTRY, AutonomyLevel, SpecialistId
from core.model_role_router import ModelRoleRouter
from core.security_effects import SCOPES
from core.specialist_execution import (
    HitlApproval,
    HitlApprovalRegistry,
    SpecialistExecutor,
    ToolIntent,
)

#: Every await in this file is bounded by this. A concurrency test that can hang
#: is a concurrency test that will one day hang in CI and be silently retried.
DEADLINE_S = 5.0

#: An effectful tool the ToolBroker actually maps, so nothing here invents a
#: policy entry. HIGH_IMPACT, therefore challenged, therefore parkable.
TOOL = "code_execute"
ARGS = {"code": "print(1)"}
OTHER_ARGS = {"code": "print(2)"}


def _future_iso(hours: int = 2) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class Gate:
    """Parks the OWNER inside the effect gate, after the ledger read and before
    the ledger write.

    It works by standing in for the NATO challenge, which is exactly where the
    window is. ``entered`` fires when a caller reaches the challenge — only the
    owner ever does, because a duplicate is suppressed before it gets there —
    and the caller then waits for ``release``.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.challenges = 0
        #: One verdict per challenge, in order. A single mutable flag would be
        #: raced by the very interleaving these tests exist to create, and the
        #: test would then assert whatever the race happened to produce.
        self.verdicts: "list[bool]" = []

    async def challenge(self, tool_name, preview):
        index = self.challenges
        self.challenges += 1
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=DEADLINE_S)
        granted = self.verdicts[index] if index < len(self.verdicts) else True
        return (True, "test:granted") if granted else (False, "test:denied")


class Harness:
    """The REAL ToolExecutor, with a counting handler and a parkable challenge."""

    def __init__(self):
        from tools.executor import ToolExecutor

        self.executor = ToolExecutor()
        self.effects: "dict[str, int]" = {}
        self.gate = Gate()
        self.executor._challenge = self.gate.challenge
        self.executor.begin_effect_epoch("turn:concurrency")

    def add_tool(self, name: str = TOOL, result: dict | None = None, *,
                 hook=None, fails: bool = False):
        payload = result if result is not None else {"stdout": "1"}

        def _handler(**kwargs):
            self.effects[name] = self.effects.get(name, 0) + 1
            if hook is not None:
                hook(kwargs)
            if fails:
                return {"error": "the tool failed"}
            return dict(payload)

        setattr(self.executor, f"_tool_{name}", _handler)
        return self

    def count(self, name: str = TOOL) -> int:
        return self.effects.get(name, 0)

    @property
    def inflight(self) -> dict:
        return self.executor._effect_inflight

    def call(self, args=None, reasoning="caller"):
        return self.executor.aexecute(TOOL, dict(args or ARGS), reasoning)


@pytest.fixture
def h(monkeypatch):
    SCOPES.scopes = []

    async def _no_broadcast(_payload):
        return None
    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    return Harness()


class ArrivalSpy:
    """Signals when the Nth caller has PARKED inside ``aexecute``.

    Everything between entering ``aexecute`` and the reservation wait is
    synchronous, and awaiting a coroutine does not yield — so by the time this
    spy's event wakes the test, the call it counted has already reached its
    decision. That is a scheduling fact about a single-threaded loop, not a
    timing assumption, which is why no test here counts ``sleep(0)`` calls.
    """

    def __init__(self, executor) -> None:
        self._real = executor.aexecute
        self.arrivals = 0
        self.parked = asyncio.Event()
        self.wanted = 2
        executor.aexecute = self

    async def __call__(self, tool_name, tool_input, reasoning="", **kw):
        self.arrivals += 1
        if self.arrivals >= self.wanted:
            self.parked.set()
        return await self._real(tool_name, tool_input, reasoning, **kw)


async def _admit_duplicate() -> None:
    """Hand the loop to a just-created duplicate so it reaches its decision.

    One iteration is enough and is not a guess: everything in ``aexecute``
    between entry and the reservation wait is synchronous, so a task that has
    been scheduled runs straight to that await on its first step.
    """
    await asyncio.sleep(0)


# ══════════════════════════════════════════════════════════════════════════════
#  The core claim: one effect identity, one effect, whoever asks
# ══════════════════════════════════════════════════════════════════════════════
def test_two_concurrent_callers_of_one_effect_produce_one_effect(h):
    """§6 — the owner is parked in the gate when the duplicate arrives."""
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        assert h.count() == 0, "the owner committed before the duplicate arrived"
        duplicate = asyncio.create_task(h.call(reasoning="duplicate"))
        await _admit_duplicate()
        h.gate.release.set()
        return await asyncio.wait_for(
            asyncio.gather(owner, duplicate), timeout=DEADLINE_S)

    first, second = asyncio.run(scenario())
    assert h.count() == 1, "two concurrent callers both executed the effect"
    assert h.executor.effect_count(TOOL) == 1
    assert h.gate.challenges == 1, "the duplicate reached the challenge"
    assert first == second == {"stdout": "1"}
    assert h.inflight == {}, "a reservation outlived its effect"


def test_the_duplicate_receives_the_owners_recorded_result(h):
    """§6 — a suppressed caller gets the canonical result, never an error."""
    h.add_tool(result={"stdout": "canonical", "id": 7})

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        duplicate = asyncio.create_task(h.call(reasoning="duplicate"))
        await _admit_duplicate()
        h.gate.release.set()
        return await asyncio.wait_for(
            asyncio.gather(owner, duplicate), timeout=DEADLINE_S)

    first, second = asyncio.run(scenario())
    assert first == second == {"stdout": "canonical", "id": 7}
    assert "error" not in second


def test_many_concurrent_duplicates_still_produce_one_effect(h):
    """Eight waiters on one identity. The count is one and nothing deadlocks."""
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        waiters = [asyncio.create_task(h.call(reasoning=f"dup{i}"))
                   for i in range(8)]
        await _admit_duplicate()
        h.gate.release.set()
        return await asyncio.wait_for(
            asyncio.gather(owner, *waiters), timeout=DEADLINE_S)

    results = asyncio.run(scenario())
    assert h.count() == 1
    assert len(results) == 9
    assert all(r == {"stdout": "1"} for r in results)
    assert h.inflight == {}


def test_argument_order_cannot_manufacture_a_second_identity_concurrently(h):
    """Canonicalisation is what makes two callers the SAME caller."""
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(
            h.executor.aexecute(TOOL, {"a": 1, "b": 2}, "owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        duplicate = asyncio.create_task(
            h.executor.aexecute(TOOL, {"b": 2, "a": 1}, "duplicate"))
        await _admit_duplicate()
        h.gate.release.set()
        return await asyncio.wait_for(
            asyncio.gather(owner, duplicate), timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.count() == 1


# ══════════════════════════════════════════════════════════════════════════════
#  §7, §8 — failure semantics. A reservation must never poison an identity.
# ══════════════════════════════════════════════════════════════════════════════
def test_an_owner_refused_before_the_effect_lets_a_duplicate_run(h):
    """§7 — the owner is DENIED at the challenge, so nothing committed. The
    waiter must run rather than inherit a refusal as if it were a result."""
    h.add_tool()
    h.gate.verdicts = [False, True]   # the owner is refused; the waiter is not

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        duplicate = asyncio.create_task(h.call(reasoning="duplicate"))
        await _admit_duplicate()
        h.gate.release.set()
        return await asyncio.wait_for(
            asyncio.gather(owner, duplicate), timeout=DEADLINE_S)

    refused, ran = asyncio.run(scenario())
    assert "error" in refused, "a refused owner must report its refusal"
    assert h.count() == 1, (
        "the waiter did not run; a refusal poisoned the effect identity")
    assert "error" not in ran
    assert h.inflight == {}


def test_an_owner_whose_effect_fails_is_not_recorded_as_committed(h):
    """§8 — the ledger represents reality, not control flow. A failed effect
    left the world unchanged, so a retry is legitimate."""
    h.add_tool(fails=True)

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        duplicate = asyncio.create_task(h.call(reasoning="duplicate"))
        await _admit_duplicate()
        h.gate.release.set()
        return await asyncio.wait_for(
            asyncio.gather(owner, duplicate), timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.executor.effect_count(TOOL) == 0, "a failed effect was ledgered"
    assert h.count() == 2, (
        "the waiter was told a failed effect had committed and did not retry")
    assert h.inflight == {}


def test_an_owner_that_raises_releases_its_reservation(h, monkeypatch):
    """§8 — an exception between the check and the write must not strand the
    identity. Raised from the gate itself, which is inside the window."""
    h.add_tool()
    boom = {"n": 0}

    async def _explode(tool_name, preview):
        boom["n"] += 1
        h.gate.entered.set()
        if boom["n"] == 1:
            raise RuntimeError("the gate itself broke")
        return True, "test:granted"

    h.executor._challenge = _explode

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        try:
            await asyncio.wait_for(owner, timeout=DEADLINE_S)
        except RuntimeError:
            pass
        assert h.inflight == {}, "the raising owner left its reservation behind"
        return await asyncio.wait_for(h.call(reasoning="after"),
                                      timeout=DEADLINE_S)

    result = asyncio.run(scenario())
    assert "error" not in result
    assert h.count() == 1


# ══════════════════════════════════════════════════════════════════════════════
#  §9 — cancellation safety
# ══════════════════════════════════════════════════════════════════════════════
def test_an_owner_cancelled_before_its_effect_commits_nothing_and_no_poison(h):
    """§9 A — no committed effect, and the identity is still runnable."""
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=DEADLINE_S)
        assert h.count() == 0, "a cancelled owner committed an effect"
        assert h.inflight == {}, "a cancelled owner left its reservation behind"
        h.gate.release.set()
        return await asyncio.wait_for(h.call(reasoning="after"),
                                      timeout=DEADLINE_S)

    result = asyncio.run(scenario())
    assert h.count() == 1, "the identity was poisoned by a cancellation"
    assert "error" not in result


def test_a_cancelled_waiter_does_not_cancel_the_owners_effect(h):
    """§9 C — one impatient duplicate must not take the real effect with it.

    This is what ``asyncio.shield`` is for on the wait: cancelling the waiter
    cancels the waiter, not the future the owner is going to complete.
    """
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        waiter = asyncio.create_task(h.call(reasoning="waiter"))
        await _admit_duplicate()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=DEADLINE_S)
        h.gate.release.set()
        return await asyncio.wait_for(owner, timeout=DEADLINE_S)

    result = asyncio.run(scenario())
    assert h.count() == 1, "cancelling a waiter cancelled the owner's effect"
    assert result == {"stdout": "1"}
    assert h.inflight == {}


def test_a_cancelled_waiter_leaves_the_remaining_waiters_alone(h):
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        doomed = asyncio.create_task(h.call(reasoning="doomed"))
        patient = asyncio.create_task(h.call(reasoning="patient"))
        await _admit_duplicate()
        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(doomed, timeout=DEADLINE_S)
        h.gate.release.set()
        return await asyncio.wait_for(asyncio.gather(owner, patient),
                                      timeout=DEADLINE_S)

    first, second = asyncio.run(scenario())
    assert h.count() == 1
    assert first == second == {"stdout": "1"}


# ══════════════════════════════════════════════════════════════════════════════
#  §11 — per-effect, not a global mutex
# ══════════════════════════════════════════════════════════════════════════════
def test_two_different_effect_identities_run_concurrently(h):
    """§11 — proven with a deadlock fixture, not by inspecting a lock.

    Effect A's handler blocks until effect B's handler has started. If the
    reservation were a global mutex, A would wait for a B that can never start
    and this test would fail on its own deadline.
    """
    both_in = threading.Event()
    first_in = threading.Event()
    order: "list[str]" = []

    def _a(_kwargs):
        order.append("a")
        first_in.set()
        assert both_in.wait(timeout=DEADLINE_S), (
            "effect A waited for effect B, which never started: the effect "
            "reservation is serialising unrelated identities")

    def _b(_kwargs):
        order.append("b")
        assert first_in.wait(timeout=DEADLINE_S)
        both_in.set()

    def _dispatch(**kwargs):
        h.effects[TOOL] = h.effects.get(TOOL, 0) + 1
        (_a if kwargs.get("code") == ARGS["code"] else _b)(kwargs)
        return {"stdout": "ok"}

    setattr(h.executor, f"_tool_{TOOL}", _dispatch)
    h.gate.release.set()                       # neither call is parked here

    async def scenario():
        return await asyncio.wait_for(
            asyncio.gather(h.call(ARGS, "a"), h.call(OTHER_ARGS, "b")),
            timeout=DEADLINE_S)

    results = asyncio.run(scenario())
    assert len(results) == 2
    assert set(order) == {"a", "b"}
    assert h.count() == 2, "two DIFFERENT identities were deduplicated together"
    assert h.inflight == {}


def test_a_second_identity_is_not_blocked_by_a_parked_owner(h):
    """The same property from the reservation's side: an identity parked in the
    gate holds a reservation on ITS key and on nothing else."""
    h.add_tool()

    async def scenario():
        parked = asyncio.create_task(h.call(ARGS, "parked"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        assert len(h.inflight) == 1
        h.gate.release.set()
        other = await asyncio.wait_for(h.call(OTHER_ARGS, "other"),
                                       timeout=DEADLINE_S)
        return await asyncio.wait_for(parked, timeout=DEADLINE_S), other

    asyncio.run(scenario())
    assert h.count() == 2
    assert h.inflight == {}


def test_read_only_calls_are_never_reserved(h):
    """Repeating a read is not an effect, so it is neither keyed nor parked."""
    h.add_tool("read_file", {"content": "x"})

    async def scenario():
        return await asyncio.wait_for(
            asyncio.gather(*(
                h.executor.aexecute("read_file", {"path": "/etc/hostname"}, "r")
                for _ in range(3))),
            timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.count("read_file") == 3
    assert h.inflight == {}
    assert h.gate.challenges == 0


# ══════════════════════════════════════════════════════════════════════════════
#  §12 — the same effect from two different SPECIALISTS, concurrently
# ══════════════════════════════════════════════════════════════════════════════
def elevate(monkeypatch, specialist: SpecialistId, level: AutonomyLevel):
    record = REGISTRY.get(specialist)
    monkeypatch.setitem(REGISTRY._by_id, specialist,
                        dataclasses.replace(record, default_autonomy=level))


def _approvals_for(*specialists, epoch: str) -> HitlApprovalRegistry:
    """Reusable approvals, deliberately. A single-use approval would refuse the
    second specialist before the ledger was ever consulted, and the test would
    pass while proving nothing — the M65A finding, restated under concurrency."""
    registry = HitlApprovalRegistry()
    identity = ToolIntent(tool=TOOL, tool_input=ARGS).effect_identity(epoch)
    for specialist in specialists:
        registry.grant(HitlApproval(
            approval_id=f"appr:{specialist.value}",
            specialist_id=specialist, effect_identity=identity,
            single_use=False, expires_at=_future_iso(),
            reason="test-only bound approval"))
    return registry


def test_two_specialists_submitting_one_effect_concurrently_execute_it_once(
        h, monkeypatch):
    """§12 — the load-bearing cross-specialist proof, through the REAL
    SpecialistExecutor and the REAL ToolExecutor, with no scheduler present."""
    import json

    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    elevate(monkeypatch, SpecialistId.CIRCUIT, AutonomyLevel.HITL_EXECUTE)
    h.add_tool()
    epoch = "turn:cross-specialist"
    h.executor.begin_effect_epoch(epoch)
    approvals = _approvals_for(SpecialistId.FORGE, SpecialistId.CIRCUIT,
                               epoch=epoch)

    intent = "TOOL_INTENT: " + json.dumps(
        {"tool": TOOL, "tool_input": ARGS, "why": "the objective needs it",
         "hypothesis": "the bounded change applies cleanly"})

    async def _infer(system, user, **kw):
        return "Analysis: one action settles this.\n" + intent

    engine = SpecialistExecutor(
        infer=_infer, tool_executor=h.executor, scopes=SCOPES,
        approvals=approvals,
        role_router=ModelRoleRouter(availability=ModelRoleRouter.probe()))

    from core.specialist_execution import SpecialistExecutionRequest

    def _request(specialist):
        return SpecialistExecutionRequest(
            execution_id=f"exec:{specialist.value}", plan_id="plan:cross",
            specialist_id=specialist, objective="Apply the bounded change.",
            autonomy_level=AutonomyLevel.HITL_EXECUTE,
            allowed_tools=frozenset({TOOL}), effect_epoch=epoch)

    spy = ArrivalSpy(h.executor)

    async def scenario():
        # The first specialist is PARKED in the gate — past the ledger read,
        # before the ledger write — when the second one arrives. Without this
        # the first execution completes before the second begins, the plain
        # ledger read catches it, and the reservation is never exercised at all.
        forge = asyncio.create_task(engine.run(_request(SpecialistId.FORGE)))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        assert h.count() == 0, "the first specialist committed too early"
        circuit = asyncio.create_task(engine.run(_request(SpecialistId.CIRCUIT)))
        await asyncio.wait_for(spy.parked.wait(), timeout=DEADLINE_S)
        h.gate.release.set()
        return await asyncio.wait_for(asyncio.gather(forge, circuit),
                                      timeout=DEADLINE_S)

    forge, circuit = asyncio.run(scenario())
    assert h.gate.challenges == 1, (
        "the second specialist reached the HITL challenge, so it was never "
        "suppressed by the ledger and this test proves nothing")

    assert h.count() == 1, "two specialists both executed one effect"
    assert h.executor.effect_count(TOOL) == 1
    executed = [r for r in (forge, circuit) if r.executed_effects == 1]
    deduped = [r for r in (forge, circuit) if r.deduplicated_effects == 1]
    assert len(executed) == 1, "more than one specialist claims the effect"
    assert len(deduped) == 1, "the suppressed specialist did not say so"
    assert h.inflight == {}


def test_the_suppressed_specialist_still_gets_a_truthful_receipt(h, monkeypatch):
    """One effect, two receipts, and the second says it was deduplicated."""
    import json

    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    elevate(monkeypatch, SpecialistId.CIRCUIT, AutonomyLevel.HITL_EXECUTE)
    h.add_tool()
    epoch = "turn:cross-receipt"
    h.executor.begin_effect_epoch(epoch)
    approvals = _approvals_for(SpecialistId.FORGE, SpecialistId.CIRCUIT,
                               epoch=epoch)
    intent = "TOOL_INTENT: " + json.dumps(
        {"tool": TOOL, "tool_input": ARGS, "why": "needed",
         "hypothesis": "the bounded change applies cleanly"})

    async def _infer(system, user, **kw):
        return "Analysis.\n" + intent

    engine = SpecialistExecutor(
        infer=_infer, tool_executor=h.executor, scopes=SCOPES,
        approvals=approvals,
        role_router=ModelRoleRouter(availability=ModelRoleRouter.probe()))
    from core.specialist_execution import SpecialistExecutionRequest

    spy = ArrivalSpy(h.executor)

    def _run(specialist):
        return engine.run(SpecialistExecutionRequest(
            execution_id=f"exec:{specialist.value}", plan_id="plan:x",
            specialist_id=specialist, objective="Apply it.",
            autonomy_level=AutonomyLevel.HITL_EXECUTE,
            allowed_tools=frozenset({TOOL}), effect_epoch=epoch))

    async def scenario():
        first = asyncio.create_task(_run(SpecialistId.FORGE))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        second = asyncio.create_task(_run(SpecialistId.CIRCUIT))
        await asyncio.wait_for(spy.parked.wait(), timeout=DEADLINE_S)
        h.gate.release.set()
        return await asyncio.wait_for(asyncio.gather(first, second),
                                      timeout=DEADLINE_S)

    results = asyncio.run(scenario())

    receipts = [r.tool_receipts[0] for r in results]
    assert len({rc.effect_identity for rc in receipts}) == 1, (
        "the two specialists disagreed about what the effect WAS")
    assert len({rc.receipt_id for rc in receipts}) == 2, (
        "two specialists shared one receipt id; attribution was lost")
    assert sum(1 for rc in receipts if rc.deduplicated) == 1


# ══════════════════════════════════════════════════════════════════════════════
#  §13, §14 — the same effect from two TEAM TASKS, with the scheduler lifted
# ══════════════════════════════════════════════════════════════════════════════
def test_two_team_tasks_converging_on_one_effect_execute_it_once(
        h, monkeypatch):
    """§13 — two layers, tested apart.

    RESOURCE_CONFLICT_CONTROL is lifted for the duration of this test:
    ``MAX_PARALLEL_EFFECTFUL`` is raised and the two tasks declare no
    overlapping claim, so the scheduler positively permits both to submit at
    once. What is left holding the line is EFFECT_IDENTITY_DEDUPE alone.

    Without the lift this test would pass on a build whose ledger did nothing,
    because the scheduler would have serialised the tasks and the second would
    have met a committed ledger. That is precisely the confusion §14 exists to
    forbid.
    """
    import json

    from core.specialist_team import (
        EffectClass,
        SpecialistTeamPlan,
        SpecialistTeamTask,
        TeamAdmissionController,
        TeamCounters,
        TeamOrchestrator,
    )

    monkeypatch.setattr("core.specialist_team.MAX_PARALLEL_EFFECTFUL", 2)
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    elevate(monkeypatch, SpecialistId.CIRCUIT, AutonomyLevel.HITL_EXECUTE)
    h.add_tool()
    epoch = "turn:team-converge"
    h.executor.begin_effect_epoch(epoch)
    approvals = _approvals_for(SpecialistId.FORGE, SpecialistId.CIRCUIT,
                               epoch=epoch)
    intent = "TOOL_INTENT: " + json.dumps(
        {"tool": TOOL, "tool_input": ARGS, "why": "the plan requires it",
         "hypothesis": "the bounded change applies cleanly"})

    async def _infer(system, user, **kw):
        return "Analysis: one action settles this.\n" + intent

    engine = SpecialistExecutor(
        infer=_infer, tool_executor=h.executor, scopes=SCOPES,
        approvals=approvals,
        role_router=ModelRoleRouter(availability=ModelRoleRouter.probe()))
    orchestrator = TeamOrchestrator(
        executor=engine, counters=TeamCounters(),
        admission=TeamAdmissionController())
    spy = ArrivalSpy(h.executor)

    def _task(task_id, specialist):
        return SpecialistTeamTask(
            task_id=task_id, specialist_id=specialist,
            objective=f"[task={task_id}] apply the bounded change",
            autonomy=AutonomyLevel.HITL_EXECUTE,
            allowed_tools=frozenset({TOOL}),
            effect_class=EffectClass.EFFECTFUL,
            resource_claims=())          # deliberately no overlapping claim
    plan = SpecialistTeamPlan(
        plan_id="plan:converge", turn_id="turn:1",
        objective="two independent branches that need the same change",
        tasks=(_task("a", SpecialistId.FORGE),
               _task("b", SpecialistId.CIRCUIT)),
        authority_ceiling=AutonomyLevel.HITL_EXECUTE, effect_epoch=epoch)

    async def scenario():
        run = asyncio.ensure_future(orchestrator.run(plan, admit=False))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        assert h.count() == 0, "the first task committed before the second ran"
        await asyncio.wait_for(spy.parked.wait(), timeout=DEADLINE_S)
        h.gate.release.set()
        return await asyncio.wait_for(run, timeout=DEADLINE_S)

    result = asyncio.run(scenario())

    assert h.count() == 1, "two team tasks both executed one effect"
    assert h.executor.effect_count(TOOL) == 1
    assert h.gate.challenges == 1, (
        "the second task reached the challenge, so the ledger never suppressed "
        "it and this test proves nothing")
    assert result.executed_effects == 1, (
        f"the team reported {result.executed_effects} effects for one effect")
    assert result.deduplicated_effects == 1
    assert result.parallel_overlaps >= 1, (
        "the two tasks did not actually overlap, so the scheduler serialised "
        "them and the ledger was never the thing under test")


def test_the_team_reports_one_effect_not_one_per_claimant(h, monkeypatch):
    """The truthfulness half of the same fact, stated as a team-level count.

    Before M65B this read 2: both specialists sampled the ledger count across
    an await, neither could attribute the change, and both claimed the effect.
    """
    import json

    from core.specialist_team import (
        EffectClass,
        SpecialistTeamPlan,
        SpecialistTeamTask,
        TeamAdmissionController,
        TeamCounters,
        TeamOrchestrator,
    )

    monkeypatch.setattr("core.specialist_team.MAX_PARALLEL_EFFECTFUL", 2)
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    elevate(monkeypatch, SpecialistId.CIRCUIT, AutonomyLevel.HITL_EXECUTE)
    h.add_tool()
    epoch = "turn:team-count"
    h.executor.begin_effect_epoch(epoch)
    approvals = _approvals_for(SpecialistId.FORGE, SpecialistId.CIRCUIT,
                               epoch=epoch)
    intent = "TOOL_INTENT: " + json.dumps(
        {"tool": TOOL, "tool_input": ARGS, "why": "needed",
         "hypothesis": "the bounded change applies cleanly"})

    async def _infer(system, user, **kw):
        return "Analysis.\n" + intent

    engine = SpecialistExecutor(
        infer=_infer, tool_executor=h.executor, scopes=SCOPES,
        approvals=approvals,
        role_router=ModelRoleRouter(availability=ModelRoleRouter.probe()))
    orchestrator = TeamOrchestrator(executor=engine, counters=TeamCounters(),
                                    admission=TeamAdmissionController())
    spy = ArrivalSpy(h.executor)
    plan = SpecialistTeamPlan(
        plan_id="plan:count", turn_id="turn:1", objective="converging branches",
        tasks=tuple(
            SpecialistTeamTask(
                task_id=tid, specialist_id=sid,
                objective=f"[task={tid}] apply it",
                autonomy=AutonomyLevel.HITL_EXECUTE,
                allowed_tools=frozenset({TOOL}),
                effect_class=EffectClass.EFFECTFUL)
            for tid, sid in (("a", SpecialistId.FORGE),
                             ("b", SpecialistId.CIRCUIT))),
        authority_ceiling=AutonomyLevel.HITL_EXECUTE, effect_epoch=epoch)

    async def scenario():
        run = asyncio.ensure_future(orchestrator.run(plan, admit=False))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        await asyncio.wait_for(spy.parked.wait(), timeout=DEADLINE_S)
        h.gate.release.set()
        return await asyncio.wait_for(run, timeout=DEADLINE_S)

    result = asyncio.run(scenario())
    receipts = result.receipts
    assert len(receipts) == 2, "both tasks must keep their own receipt"
    assert len({rc.effect_identity for rc in receipts}) == 1
    assert sum(1 for rc in receipts if rc.deduplicated) == 1
    assert result.executed_effects == 1
    assert h.count() == 1


# ══════════════════════════════════════════════════════════════════════════════
#  The audit distinguishes a REPLAY from a CONCURRENT duplicate
# ══════════════════════════════════════════════════════════════════════════════
def _audit_spy(executor) -> list:
    """Record what the audit trail was told, without changing what it does."""
    entries: list = []
    real = executor._audit.log_action

    def _log(tool, reasoning, auth, status, detail=""):
        entries.append((tool, auth, status))
        return real(tool, reasoning, auth, status, detail)

    executor._audit.log_action = _log
    return entries


def test_a_concurrent_duplicate_is_audited_differently_from_a_replay(h):
    """Two suppressions, two different facts, two different audit records.

    A sequential replay means a caller asked twice. A concurrent duplicate means
    two callers raced and the reservation caught one. An operator reading the
    trail needs to tell those apart, and the value the owner publishes on its
    reservation is what makes the second one distinguishable — so it is
    load-bearing for observability even though the ledger would have produced
    the same effect count either way.
    """
    h.add_tool()
    entries = _audit_spy(h.executor)

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        concurrent = asyncio.create_task(h.call(reasoning="concurrent"))
        await _admit_duplicate()
        h.gate.release.set()
        await asyncio.wait_for(asyncio.gather(owner, concurrent),
                               timeout=DEADLINE_S)
        # Now everything has settled, so this one is a plain replay.
        return await asyncio.wait_for(h.call(reasoning="replay"),
                                      timeout=DEADLINE_S)

    asyncio.run(scenario())
    audits = [auth for _tool, auth, _status in entries]
    assert "deduplicated:concurrent" in audits, (
        "a concurrent duplicate was recorded as an ordinary replay")
    assert "deduplicated" in audits, "a replay was not recorded at all"
    assert h.count() == 1


def test_the_effect_count_is_one_however_the_duplicate_was_suppressed(h):
    """Whichever branch caught it, the world changed once."""
    h.add_tool()

    async def scenario():
        owner = asyncio.create_task(h.call(reasoning="owner"))
        await asyncio.wait_for(h.gate.entered.wait(), timeout=DEADLINE_S)
        concurrent = asyncio.create_task(h.call(reasoning="concurrent"))
        await _admit_duplicate()
        h.gate.release.set()
        await asyncio.wait_for(asyncio.gather(owner, concurrent),
                               timeout=DEADLINE_S)
        return await asyncio.wait_for(
            asyncio.gather(*(h.call(reasoning=f"late{i}") for i in range(3))),
            timeout=DEADLINE_S)

    late = asyncio.run(scenario())
    assert h.count() == 1
    assert all(r == {"stdout": "1"} for r in late)


def test_concurrent_reads_are_never_serialised_behind_each_other(h):
    """Reads must stay CHEAP, not merely correct.

    Asserting only that three reads all ran leaves a build that funnels them
    through the effect reservation looking identical — same count, same empty
    registry, just serialised. This is a deadlock fixture: the first read cannot
    return until the second has started, so a reservation on a read-only
    identity makes the test fail on its own deadline instead of passing quietly.
    """
    both_in = threading.Barrier(2, timeout=DEADLINE_S)

    def _handler(**kwargs):
        h.effects["read_file"] = h.effects.get("read_file", 0) + 1
        # Neither read can return until BOTH have started. Under a reservation
        # the second would be parked waiting for the first, which is waiting for
        # the second, and the barrier times out.
        both_in.wait()
        return {"content": "x"}

    setattr(h.executor, "_tool_read_file", _handler)

    async def scenario():
        # IDENTICAL arguments on purpose. Different arguments are different
        # identities, so they would run concurrently even under a reservation
        # and the test would prove nothing — measured, the first version of this
        # test used two different paths and the mutation survived it.
        return await asyncio.wait_for(asyncio.gather(*(
            h.executor.aexecute("read_file", {"path": "/etc/hostname"}, f"r{i}")
            for i in range(2))), timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.count("read_file") == 2, (
        "identical reads were deduplicated; repeating a read is not an effect")
    assert h.inflight == {}
    assert h.gate.challenges == 0, "a read-only call was challenged"
