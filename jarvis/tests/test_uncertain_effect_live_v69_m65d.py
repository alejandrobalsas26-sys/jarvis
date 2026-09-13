"""
tests/test_uncertain_effect_live_v69_m65d.py — V69 M65D: THE FLAGSHIP
REGRESSION, and the failure-window matrix around it.

Everything here meets a REAL localhost server that applies a durable effect and
then loses the response. That is not decoration. The whole milestone rests on
one claim about the world —

        an observed error does not prove that nothing happened

— and a mocked failure cannot demonstrate it, because a mock leaves no external
state behind to count. The server here does, so the tests can assert on the
number of distinct external effects rather than on the number of invocations.

Against M65C's semantics the flagship test FAILS: the effect count reaches 2.
Under M65D it stays at 1.

Scope: 127.0.0.1 on an ephemeral port, and a temporary directory. Nothing here
reaches a network, mutates any real system, or touches a process it did not
create. Every wait is bounded, so a broken implementation fails instead of
hanging.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.effect_journal import (
    DurableEffectJournal,
    EffectDurabilityClass,
    EffectState,
    ExecutionDisposition,
    ExternalOutcome,
    ReservationOutcome,
    RetryAuthority,
    compute_effect_id,
    derive_idempotency_key,
    register_durability,
    register_reconciler,
    unregister_durability,
)
from _test_support import m65d_uncertain_world
from _test_support.m65d_uncertain_world import (
    CRASH_EXIT,
    LostResponseServer,
    SyntheticWorld,
    call_remote,
    make_reconciler,
)

WORKER = Path(m65d_uncertain_world.__file__).resolve()
JARVIS_ROOT = WORKER.parent.parent.parent

TOOL = "m65d_remote_effect"
ARGS = {"target": "acct-1", "amount": 100}
SCOPE = "task:m65d-uncertain"
JOIN_S = 60.0
DEADLINE_S = 10.0

NON_REPLAYABLE = EffectDurabilityClass.NON_REPLAYABLE
RECONCILABLE = EffectDurabilityClass.RECONCILABLE
IDEMPOTENT_KEY = EffectDurabilityClass.IDEMPOTENT_WITH_KEY


#: A fresh interpreter per worker, rather than a fork: a forked child inherits
#: the parent's module state — including the cached runtime instance id — so it
#: would not be a new owner and a "restart" would not be a restart.
#:
#: Both `jarvis/` and `jarvis/tests/` go on the path: the worker imports the
#: runtime as `core.*`/`tools.*` and the synthetic world as `_test_support.*`,
#: and the two live at different depths.
def _worker_env() -> dict:
    env = dict(os.environ)
    roots = f"{JARVIS_ROOT}{os.pathsep}{JARVIS_ROOT / 'tests'}"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{roots}{os.pathsep}{existing}" if existing else roots
    return env


def start_worker(payload: dict, result: "Path | None" = None) -> subprocess.Popen:
    argv = [sys.executable, str(WORKER), json.dumps(payload),
            str(result) if result else ""]
    return subprocess.Popen(argv, env=_worker_env(), cwd=str(JARVIS_ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(proc: subprocess.Popen, *, expect_crash: bool = False) -> int:
    try:
        _out, err = proc.communicate(timeout=JOIN_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"a worker did not finish within {JOIN_S}s")
    if expect_crash:
        assert proc.returncode == CRASH_EXIT, (
            f"expected a deliberate crash, got {proc.returncode}: "
            f"{err.decode()[-800:]}")
    else:
        assert proc.returncode == 0, (
            f"worker failed ({proc.returncode}): {err.decode()[-800:]}")
    return proc.returncode


def read_result(path: Path) -> dict:
    assert path.exists(), "the worker wrote no result"
    return json.loads(path.read_text())


@pytest.fixture
def world(tmp_path) -> SyntheticWorld:
    return SyntheticWorld(tmp_path / "world").prepare()


class LiveHarness:
    """A real ToolExecutor whose one tool talks to a real localhost server."""

    def __init__(self, tmp_path, server, *, cls=NON_REPLAYABLE,
                 instance_id="inst-a", scope=SCOPE, timeout=3.0):
        from tools.executor import ToolExecutor

        self.journal = DurableEffectJournal(tmp_path / "effects.db",
                                            instance_id=instance_id)
        self.executor = ToolExecutor(journal=self.journal)
        self.executor.begin_effect_epoch(scope)
        self.server = server
        self.scope = scope
        self.handler_calls = 0
        register_durability(TOOL, cls)
        self.effect_id = compute_effect_id(surface="native", tool_id=TOOL,
                                           identity_scope=scope,
                                           tool_input=ARGS)
        self.idem = derive_idempotency_key(self.effect_id)
        outer = self

        def _handler(**kwargs):
            outer.handler_calls += 1
            return call_remote(server.port, outer.idem, timeout=timeout)

        setattr(self.executor, f"_tool_{TOOL}", _handler)

        async def _granted(tool_name, preview):
            return True, "test:granted"

        self.executor._challenge = _granted

    async def call(self, note=None):
        return await self.executor.aexecute(TOOL, dict(ARGS), "caller",
                                            effect_note=note)

    def forget_in_process_ledger(self) -> None:
        """Drop layers 1 and 2 so the DURABLE layer is what is under test.

        Without this the in-process ledger would answer first and the test
        would prove M64.1 works, which is not the question.
        """
        self.executor._effect_ledger.clear()


@pytest.fixture
def live(tmp_path, world, monkeypatch):
    from core.security_effects import SCOPES

    SCOPES.scopes = []

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    made: list = []

    def _build(server, **kw):
        h = LiveHarness(tmp_path, server, **kw)
        made.append(h)
        return h

    yield _build
    unregister_durability(TOOL)


# ══════════════════════════════════════════════════════════════════════════════
#  THE FLAGSHIP — §19
# ══════════════════════════════════════════════════════════════════════════════
def test_an_effect_that_landed_and_lost_its_response_is_never_repeated(
        live, world):
    """THE regression. Fails against M65C, passes under M65D.

    First call:  remote effect count 1, local result an uncertain failure.
    Second call: the handler MUST NOT run, and the remote count MUST stay 1.
    """
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="lose_response") as server:
        h = live(server)

        first: dict = {}
        result = asyncio.run(h.call(note=first))

        assert world.effect_count == 1, "the fixture did not apply the effect"
        assert h.handler_calls == 1
        assert first["disposition"] == (
            ExecutionDisposition.FAILED_OBSERVED_UNKNOWN.value)
        assert first["external_outcome"] == ExternalOutcome.UNKNOWN.value
        assert first["retry_authority"] == (
            RetryAuthority.BLOCKED_INDETERMINATE.value)
        assert result["effect_uncertainty"]["external_outcome"] == "UNKNOWN"
        assert h.journal.get(h.effect_id).state is EffectState.FAILED_OBSERVED

        h.forget_in_process_ledger()
        second: dict = {}
        blocked = asyncio.run(h.call(note=second))

        assert h.handler_calls == 1, "the handler ran a second time"
        assert world.effect_count == 1, (
            f"THE M65D FAILURE: {world.effect_count} external effects for one "
            f"logical NON_REPLAYABLE effect")
        assert second["disposition"] == (
            ExecutionDisposition.BLOCKED_INDETERMINATE.value)
        assert blocked["error_class"] == "indeterminate_effect"


def test_the_historical_semantics_would_have_duplicated_it(live, world):
    """§34 — do not erase the evidence that justified the milestone.

    M65C's rule, stated exactly: a FAILED_OBSERVED row was grouped with the two
    genuinely pre-effect states and handed back to the next caller as OWNED,
    for every durability class. Re-running that rule against this fixture is
    what produced two external effects, and this test keeps the demonstration
    executable rather than anecdotal.
    """
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="lose_response") as server:
        h = live(server)
        asyncio.run(h.call())
        assert world.effect_count == 1

        # The historical branch, applied by hand to the very same row.
        record = h.journal.get(h.effect_id)
        assert record.state is EffectState.FAILED_OBSERVED
        historically_retryable = record.state in (
            EffectState.FAILED_BEFORE_EFFECT,
            EffectState.FAILED_OBSERVED,
            EffectState.RECONCILED_NOT_EXECUTED)
        assert historically_retryable, (
            "the historical grouping no longer selects this row, so this "
            "regression no longer describes the bug it was written for")

        # ...and what it authorised: a second real external effect. The call
        # raises for the same reason the first one did — the response is lost —
        # which is precisely why the caller could not tell it had duplicated.
        with pytest.raises(Exception):
            call_remote(server.port, h.idem)
        assert world.effect_count == 2, (
            "the fixture cannot demonstrate duplication, so the flagship test "
            "above is vacuous")

        # M65D's rule, on the same row, refuses.
        from core.effect_journal import retry_authority

        assert retry_authority(
            state=record.state, durability_class=NON_REPLAYABLE,
            recorded_outcome=record.external_outcome
        ) is RetryAuthority.BLOCKED_INDETERMINATE


def test_a_reconcilable_tool_recovers_the_same_window_without_replaying(
        live, world):
    """The same lost response, for a tool the external system CAN be asked."""
    with LostResponseServer(world.root, mode="reconcilable",
                            behaviour="lose_response") as server:
        h = live(server, cls=RECONCILABLE)
        register_reconciler(TOOL, make_reconciler(str(world.root)))

        asyncio.run(h.call())
        assert world.effect_count == 1
        h.forget_in_process_ledger()

        note: dict = {}
        result = asyncio.run(h.call(note=note))
        assert h.handler_calls == 1, "a reconcilable effect was replayed"
        assert world.effect_count == 1
        assert note["disposition"] == (
            ExecutionDisposition.RECONCILED_COMMITTED.value)
        assert result["status"] == "recovered"


def test_an_idempotent_with_key_tool_replays_into_one_external_effect(
        live, world):
    """The far side deduplicates on the key JARVIS replays with."""
    with LostResponseServer(world.root, mode="idempotent_key",
                            behaviour="lose_response") as server:
        h = live(server, cls=IDEMPOTENT_KEY)
        asyncio.run(h.call())
        assert world.effect_count == 1
        assert world.attempt_count == 1

        h.forget_in_process_ledger()
        asyncio.run(h.call())

        assert h.handler_calls == 2, "the contract permits this replay"
        assert world.attempt_count == 2
        assert world.effect_count == 1, (
            "the replay used a different key and the far side could not "
            "deduplicate it")


def test_a_transport_failure_before_the_server_is_still_unknown(live, world,
                                                               tmp_path):
    """F3. The server never saw the request, and JARVIS still cannot say so.

    This is the case where the conservative answer costs something real: the
    external truth is zero effects and the retry is blocked anyway. It is kept
    because the alternative — inferring "connection refused means nothing
    happened" — is an exception-type heuristic, and the same inference is wrong
    the moment the reset arrives after the send.
    """
    class Closed:
        port = 1        # nothing listens; the connection is refused

    h = live(Closed())
    first: dict = {}
    asyncio.run(h.call(note=first))
    assert world.effect_count == 0
    assert first["external_outcome"] == ExternalOutcome.UNKNOWN.value

    h.forget_in_process_ledger()
    second: dict = {}
    asyncio.run(h.call(note=second))
    assert h.handler_calls == 1
    assert second["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value


def test_a_client_timeout_over_a_hung_server_is_unknown(live, world):
    """F6. The effect is applied and the server never answers."""
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="hang") as server:
        h = live(server, timeout=0.5)
        first: dict = {}
        asyncio.run(h.call(note=first))
        assert world.effect_count == 1
        assert first["external_outcome"] == ExternalOutcome.UNKNOWN.value

        h.forget_in_process_ledger()
        asyncio.run(h.call())
        assert h.handler_calls == 1
        assert world.effect_count == 1


def test_a_postprocessing_failure_after_a_real_reply_is_unknown(live, world):
    """F5. The server applied AND answered; the local wrapper broke."""
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="apply_and_reply") as server:
        h = live(server)
        original = getattr(h.executor, f"_tool_{TOOL}")

        def _handler(**kwargs):
            original(**kwargs)
            raise ValueError("could not normalise the response")

        setattr(h.executor, f"_tool_{TOOL}", _handler)
        first: dict = {}
        asyncio.run(h.call(note=first))
        assert world.effect_count == 1
        assert first["external_outcome"] == ExternalOutcome.UNKNOWN.value

        h.forget_in_process_ledger()
        asyncio.run(h.call())
        assert world.effect_count == 1


# ══════════════════════════════════════════════════════════════════════════════
#  CROSS_PROCESS — §18 F7/F11/F12, §22, §26
# ══════════════════════════════════════════════════════════════════════════════
def worker_spec(tmp_path, world, port, **kw) -> dict:
    spec = {
        "journal": str(tmp_path / "effects.db"),
        "world": str(world.root),
        "tool": TOOL,
        "args": json.dumps(ARGS),
        "scope": SCOPE,
        "durability_class": NON_REPLAYABLE.value,
        "port": port,
    }
    spec.update(kw)
    return spec


def wait_for(predicate, *, timeout=DEADLINE_S, what="condition") -> None:
    """Bounded poll. A test that can hang is a test that will hang in CI."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(f"{what} never happened within {timeout}s")


def test_f7_a_process_killed_after_the_boundary_leaves_a_blocked_effect(
        tmp_path, world):
    """F7. SIGKILL while the external call is in flight and the effect has
    already landed. The replacement process must not repeat it.

    A real kill, not an exception: a ``finally`` that tidied the reservation is
    exactly what a machine losing power does not run.
    """
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="hang") as server:
        result = tmp_path / "f7.json"
        proc = start_worker(
            worker_spec(tmp_path, world, server.port, timeout=30.0,
                        lease_s=0.5, lease_grace_s=0.0), result)
        try:
            wait_for(lambda: world.effect_count == 1,
                     what="the external effect")
            os.kill(proc.pid, signal.SIGKILL)
        finally:
            proc.wait(timeout=JOIN_S)
            proc.communicate()

    assert world.effect_count == 1
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    journal = DurableEffectJournal(tmp_path / "effects.db",
                                   instance_id="inst-replacement")
    record = journal.get(eid)
    assert record.state is EffectState.EXECUTING, (
        "the killed owner did not leave a durable EXECUTING row")
    assert record.external_effect is ExternalOutcome.UNKNOWN
    journal.close()

    # The replacement process asks, with the lease long expired.
    expired = DurableEffectJournal(tmp_path / "effects.db", lease_s=0.0,
                                   lease_grace_s=0.0,
                                   instance_id="inst-replacement")
    wait_for(lambda: expired.lease_expired(expired.get(eid)),
             what="the dead owner's lease expiring")
    again = expired.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                            durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert again.outcome is ReservationOutcome.INDETERMINATE, (
        f"the stale-EXECUTING branch was not taken: {again.outcome.value}")
    assert again.owned is False, (
        "a lease expiry was read as evidence that the effect did not happen")
    assert world.effect_count == 1


def test_a_lease_expiry_alone_never_reopens_a_non_replayable_effect(tmp_path,
                                                                    world):
    """§22, stated as its own property rather than as a side effect of F7."""
    journal = DurableEffectJournal(tmp_path / "effects.db", lease_s=0.0,
                                   lease_grace_s=0.0)
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    journal.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                    durability_class=NON_REPLAYABLE, tool_input=ARGS)
    journal.mark_executing(eid)
    journal.fail_observed(eid, "tool_returned_error")

    for _ in range(3):
        again = journal.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                                durability_class=NON_REPLAYABLE,
                                tool_input=ARGS)
        assert again.owned is False


def test_f11_dying_before_the_no_effect_transition_stays_conservative(
        tmp_path, world):
    """F11. The adapter proved nothing was applied, and the process died before
    that could be written down. What survives is EXECUTING, which is UNKNOWN.

    Losing evidence must cost a reconciliation, never a duplicate: the
    conservative direction is the one that survives a crash.
    """
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="lose_response") as server:
        result = tmp_path / "f11.json"
        proc = start_worker(
            worker_spec(tmp_path, world, server.port, crash_at="F11",
                        lease_s=0.5, lease_grace_s=0.0), result)
        finish(proc, expect_crash=True)

    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    journal = DurableEffectJournal(tmp_path / "effects.db", lease_s=0.0,
                                   lease_grace_s=0.0, instance_id="inst-next")
    record = journal.get(eid)
    assert record.state is EffectState.EXECUTING
    assert record.external_effect is ExternalOutcome.UNKNOWN
    wait_for(lambda: journal.lease_expired(journal.get(eid)),
             what="the dead owner's lease expiring")
    again = journal.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                            durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert again.outcome is ReservationOutcome.INDETERMINATE
    assert again.owned is False


def test_f12_dying_before_the_committed_transition_stays_conservative(
        tmp_path, world):
    """F12. The effect landed, the receipt was in hand, and the process died
    before COMMITTED reached the disk. The replacement must not repeat it."""
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="apply_and_reply") as server:
        result = tmp_path / "f12.json"
        proc = start_worker(
            worker_spec(tmp_path, world, server.port, crash_at="F12",
                        lease_s=0.5, lease_grace_s=0.0), result)
        finish(proc, expect_crash=True)

    assert world.effect_count == 1
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    journal = DurableEffectJournal(tmp_path / "effects.db", lease_s=0.0,
                                   lease_grace_s=0.0, instance_id="inst-next")
    assert journal.get(eid).state is EffectState.EXECUTING
    wait_for(lambda: journal.lease_expired(journal.get(eid)),
             what="the dead owner's lease expiring")
    again = journal.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                            durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert again.outcome is ReservationOutcome.INDETERMINATE
    assert again.owned is False
    assert world.effect_count == 1


def test_a_replacement_process_reports_the_same_semantics(tmp_path, world):
    """Process replacement, through the REAL executor on both sides."""
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="lose_response") as server:
        first = tmp_path / "one.json"
        finish(start_worker(worker_spec(tmp_path, world, server.port), first))
        one = read_result(first)
        assert one["state"] == EffectState.FAILED_OBSERVED.value
        assert one["external_outcome"] == ExternalOutcome.UNKNOWN.value
        assert world.effect_count == 1

        second = tmp_path / "two.json"
        finish(start_worker(worker_spec(tmp_path, world, server.port), second))
        two = read_result(second)

    assert two["instance_id"] != one["instance_id"], "not a real replacement"
    assert two["result_error_class"] == "indeterminate_effect"
    assert two["note"]["disposition"] == (
        ExecutionDisposition.BLOCKED_INDETERMINATE.value)
    assert world.effect_count == 1, (
        "a fresh process repeated an effect whose outcome was unknown")


def test_concurrent_processes_cannot_both_read_uncertainty_as_permission(
        tmp_path, world):
    """§26 — the compare-and-swap, over the M65D branch.

    Four fresh interpreters ask about one uncertain identity at once. None may
    execute, and the external effect count must not move.
    """
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour="lose_response") as server:
        seed = tmp_path / "seed.json"
        finish(start_worker(worker_spec(tmp_path, world, server.port), seed))
        assert world.effect_count == 1

        results = [tmp_path / f"racer-{i}.json" for i in range(4)]
        procs = [
            start_worker(
                worker_spec(tmp_path, world, server.port,
                            barrier_dir=str(tmp_path / "barrier"),
                            barrier_count=4, worker_tag=f"r{i}"),
                results[i])
            for i in range(4)]
        for proc in procs:
            finish(proc)

    assert world.effect_count == 1, (
        f"{world.effect_count} external effects after a concurrent recovery")
    outcomes = [read_result(p)["note"]["disposition"] for p in results]
    assert all(d == ExecutionDisposition.BLOCKED_INDETERMINATE.value
               for d in outcomes), outcomes


def test_the_idempotency_key_is_identical_across_two_real_processes(tmp_path,
                                                                    world):
    """§20 — the key must not be regenerated by a new interpreter, or the far
    side sees two requests and deduplicates neither."""
    with LostResponseServer(world.root, mode="idempotent_key",
                            behaviour="lose_response") as server:
        spec = worker_spec(tmp_path, world, server.port,
                           durability_class=IDEMPOTENT_KEY.value)
        first = tmp_path / "k1.json"
        second = tmp_path / "k2.json"
        finish(start_worker(spec, first))
        finish(start_worker(spec, second))

    one, two = read_result(first), read_result(second)
    assert one["instance_id"] != two["instance_id"]
    assert one["idempotency_key"] == two["idempotency_key"]
    assert world.attempt_count == 2
    assert world.effect_count == 1


# ══════════════════════════════════════════════════════════════════════════════
#  FAILURE_WINDOW_MATRIX — §18, as one auditable table
# ══════════════════════════════════════════════════════════════════════════════
#: (window, how it is produced, external truth, what JARVIS may know,
#:  whether the handler runs again).
#: F7/F11/F12 need process death and are proven above; F0/F8/F9/F10 are
#: journal-level and live in ``test_effect_semantics_v69_m65d.py``.
_WINDOWS = [
    ("F1", "refused_before_effect", 0, ExternalOutcome.PROVEN_NOT_EXECUTED, True),
    ("F2", "raise_before_call", 0, ExternalOutcome.UNKNOWN, False),
    ("F3", "connection_refused", 0, ExternalOutcome.UNKNOWN, False),
    ("F4", "response_lost", 1, ExternalOutcome.UNKNOWN, False),
    ("F5", "postprocess_raises", 1, ExternalOutcome.UNKNOWN, False),
    ("F6", "client_timeout", 1, ExternalOutcome.UNKNOWN, False),
]


@pytest.mark.parametrize("window,mode,effects,knowledge,runs_again", _WINDOWS)
def test_the_failure_window_matrix(live, world, window, mode, effects,
                                   knowledge, runs_again):
    """Every window, its external truth, and what JARVIS is entitled to say.

    F1 is the only row where the handler may run again, and it is the only row
    whose external truth is PROVEN. That correspondence is the milestone: F2
    and F3 really did nothing, and JARVIS still declines to say so, because
    the only thing separating them from F4 is knowledge it does not have.
    """
    behaviour = {"client_timeout": "hang",
                 "postprocess_raises": "apply_and_reply"}.get(
                     mode, "lose_response")
    with LostResponseServer(world.root, mode="non_replayable",
                            behaviour=behaviour) as server:
        target = server if mode != "connection_refused" else type(
            "Closed", (), {"port": 1})()
        h = live(target, timeout=0.5 if mode == "client_timeout" else 3.0)

        if mode == "refused_before_effect":
            refusals = {"n": 0}

            async def _first_refuses(tool_name, preview):
                refusals["n"] += 1
                return refusals["n"] > 1, "test"

            h.executor._challenge = _first_refuses
        elif mode == "raise_before_call":
            def _raises(**kwargs):
                h.handler_calls += 1
                raise RuntimeError("failed before contacting the remote system")
            setattr(h.executor, f"_tool_{TOOL}", _raises)
        elif mode == "postprocess_raises":
            original = getattr(h.executor, f"_tool_{TOOL}")

            def _post(**kwargs):
                original(**kwargs)
                raise ValueError("could not normalise the response")
            setattr(h.executor, f"_tool_{TOOL}", _post)

        first: dict = {}
        asyncio.run(h.call(note=first))
        assert world.effect_count == effects, (
            f"{window}: the fixture's external truth is not what the row claims")
        assert first["external_outcome"] == knowledge.value, (
            f"{window}: JARVIS claimed {first['external_outcome']}")

        before = h.handler_calls
        h.forget_in_process_ledger()
        asyncio.run(h.call())
        ran_again = h.handler_calls > before
        assert ran_again is runs_again, (
            f"{window}: handler {'ran' if ran_again else 'did not run'} again")
        if not runs_again:
            assert world.effect_count == effects, (
                f"{window}: the external effect count moved")
