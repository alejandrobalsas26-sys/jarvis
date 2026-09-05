"""
tests/test_effect_journal_crash_recovery_v69_m65c.py — V69 M65C CROSS-PROCESS
OWNERSHIP AND CRASH RECOVERY.

This is the file M65C exists for. Everything here spawns REAL processes and
kills them, because the boundary being tested is process death and nothing
simulated crosses it:

  * a thread cannot be SIGKILLed;
  * an exception runs ``finally`` blocks, and a crash does not;
  * a mocked "restart" keeps the module state a restart would lose.

Workers are launched as SUBPROCESSES of the support module, so each is a
genuinely fresh interpreter with its own ``runtime_instance_id``. (Measured:
``multiprocessing``'s ``spawn`` re-executes the parent's ``__main__``, which
under ``python -m pytest`` is pytest — every worker started a nested pytest
session and the suite hung silently. A plain subprocess has no such coupling.)

Ordering is forced with an N-way file barrier, never with a sleep, so a race
that must happen does happen rather than being hoped for. Every wait is bounded,
so a broken implementation FAILS instead of hanging.

The parent terminates ONLY processes it created, and every effect is synthetic
and confined to a temporary directory (see ``tests/support/m65c_effect_world``).
"""
from __future__ import annotations

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
    ReconciliationVerdict,
    ReservationOutcome,
    compute_effect_id,
    derive_idempotency_key,
    may_auto_retry,
)
from tests.support import m65c_effect_world
from tests.support.m65c_effect_world import (
    CRASH_EXIT,
    SyntheticWorld,
    make_reconciler,
)

WORKER = Path(m65c_effect_world.__file__).resolve()
JARVIS_ROOT = WORKER.parent.parent.parent

#: Every join in this file is bounded by this. A cross-process test that can
#: hang is a test that will one day hang in CI and be silently retried.
JOIN_S = 60.0
TOOL = "m65c_synthetic_effect"
ARGS = {"target": "synthetic", "n": 1}
SCOPE = "task:m65c-durable"

#: A fresh interpreter per worker, rather than a fork: a forked child inherits
#: the parent's module state — including the cached runtime instance id — so it
#: would not be a new owner and a "restart" would not be a restart.
def _worker_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (f"{JARVIS_ROOT}{os.pathsep}{existing}"
                         if existing else str(JARVIS_ROOT))
    return env


def start_worker(payload: dict, result: "Path | None" = None) -> subprocess.Popen:
    """Launch one worker. The caller owns it and must not outlive it."""
    argv = [sys.executable, str(WORKER), json.dumps(payload),
            str(result) if result else ""]
    return subprocess.Popen(argv, env=_worker_env(), cwd=str(JARVIS_ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(proc: subprocess.Popen, *, expect_crash: bool = False) -> int:
    """Wait for a worker within the deadline and assert why it ended."""
    try:
        _out, err = proc.communicate(timeout=JOIN_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("worker did not finish within the deadline")
    if expect_crash:
        assert proc.returncode == CRASH_EXIT, (
            f"expected a deliberate hard exit, got {proc.returncode}: "
            f"{err.decode()[-800:]}")
    else:
        assert proc.returncode == 0, (
            f"worker failed with {proc.returncode}: {err.decode()[-800:]}")
    return proc.returncode


@pytest.fixture
def world(tmp_path) -> SyntheticWorld:
    return SyntheticWorld(tmp_path / "world").prepare()


@pytest.fixture
def journal_path(tmp_path) -> Path:
    return tmp_path / "effects.db"


def spec(journal_path, world, *, mode="non_replayable",
         cls=EffectDurabilityClass.NON_REPLAYABLE, crash_at="", args=None,
         scope=SCOPE, tool=TOOL, **kw) -> dict:  # noqa: D401
    return {
        "journal": str(journal_path), "world": str(world.root),
        "tool": tool, "scope": scope,
        "args": json.dumps(args if args is not None else ARGS),
        "durability_class": cls.value, "mode": mode, "crash_at": crash_at,
        **kw,
    }


def run_worker(payload, *, result: "Path | None" = None,
               expect_crash: bool = False) -> dict:
    """Run one worker to completion and return what it reported.

    Bounded wait, and the exit code is asserted so a worker that died for the
    wrong reason cannot be mistaken for an injected crash.
    """
    proc = start_worker(payload, result)
    finish(proc, expect_crash=expect_crash)
    if result is not None and result.exists():
        return json.loads(result.read_text())
    return {}


def await_parked(park: Path, count: int, timeout_s: float = 25.0) -> None:
    """Block until *count* workers are parked holding their reservations."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if park.exists() and len(list(park.glob("*.parked"))) >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f"only {len(list(park.glob('*.parked'))) if park.exists() else 0}"
                         f" of {count} workers parked within the deadline")


def release_parked(park: Path) -> None:
    park.mkdir(parents=True, exist_ok=True)
    (park / "release").write_text("1", encoding="utf-8")


def race(payloads, results) -> list[dict]:
    """Start every worker, wait for all of them, and return their reports."""
    procs = [start_worker(p, r) for p, r in zip(payloads, results)]
    try:
        for proc in procs:
            finish(proc)
    finally:
        for proc in procs:            # never leave a child of ours behind
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    return [json.loads(r.read_text()) for r in results]


def open_journal(path, **kw) -> DurableEffectJournal:
    return DurableEffectJournal(path, **kw)


def effect_id(scope=SCOPE, tool=TOOL, args=None) -> str:
    return compute_effect_id(surface="native", tool_id=tool,
                             identity_scope=scope,
                             tool_input=args if args is not None else ARGS)


# ══════════════════════════════════════════════════════════════════════════════
#  CROSS_PROCESS_DEDUPE — §34, the two-process race
# ══════════════════════════════════════════════════════════════════════════════
def test_two_processes_racing_one_identity_produce_one_owner(journal_path, world,
                                                             tmp_path):
    """§34 — both processes arrive at the reservation together, on a barrier.

    Exactly one may own it, and the loser must not execute. This is the on-disk
    version of the bug M65B found in RAM: a SELECT-then-INSERT here would let
    both through and would look correct in every sequential test.
    """
    results = [tmp_path / "a.json", tmp_path / "b.json"]
    payloads = [spec(journal_path, world, barrier_dir=str(tmp_path / "gate"),
                     barrier_count=2, worker_tag=tag) for tag in ("a", "b")]
    reported = race(payloads, results)
    owners = [r for r in reported if r["owned"]]
    assert len(owners) == 1, f"{len(owners)} processes claimed ownership"
    assert world.effect_count == 1, (
        f"{world.effect_count} external effects for one identity")
    loser = [r for r in reported if not r["owned"]][0]
    assert loser["reservation"] in (
        ReservationOutcome.OWNED_ELSEWHERE.value,
        ReservationOutcome.ALREADY_COMMITTED.value)


def test_the_two_racers_are_genuinely_different_instances(journal_path, world,
                                                          tmp_path):
    """If both workers shared a runtime instance id the race would be fake."""
    results = [tmp_path / "a.json", tmp_path / "b.json"]
    payloads = [spec(journal_path, world, barrier_dir=str(tmp_path / "gate"),
                     barrier_count=2, worker_tag=tag) for tag in ("a", "b")]
    reported = race(payloads, results)
    ids = {r["instance_id"] for r in reported}
    assert len(ids) == 2, "the two workers shared an owner identity"


@pytest.mark.parametrize("n", [4, 8])
def test_n_processes_racing_one_identity_produce_one_effect(n, journal_path,
                                                            world, tmp_path):
    """§35 — bounded contention, one effect, no deadlock. Not a load test."""
    results = [tmp_path / f"w{i}.json" for i in range(n)]
    payloads = [spec(journal_path, world, barrier_dir=str(tmp_path / "gate"),
                     barrier_count=n, worker_tag=f"w{i}") for i in range(n)]
    started = time.monotonic()
    reported = race(payloads, results)
    elapsed = time.monotonic() - started
    assert sum(1 for r in reported if r["owned"]) == 1
    assert world.effect_count == 1, (
        f"{n} racers produced {world.effect_count} external effects")
    assert elapsed < JOIN_S, "contention did not resolve in bounded time"


def test_different_identities_progress_concurrently(journal_path, world,
                                                    tmp_path):
    """§36 — the journal must serialise ONE identity, not every process.

    All four workers meet on a barrier AFTER reserving. If the journal
    serialised globally, the second worker could not reserve while the first was
    parked, the barrier would never fill, and the join would time out.
    """
    n = 4
    results = [tmp_path / f"w{i}.json" for i in range(n)]
    payloads = [
        spec(journal_path, world, args={"target": f"host-{i}"},
             barrier_at="after_reserve", barrier_dir=str(tmp_path / "gate"),
             barrier_count=n, worker_tag=f"w{i}")
        for i in range(n)
    ]
    reported = race(payloads, results)
    assert all(r["owned"] for r in reported), "distinct identities blocked"
    assert world.effect_count == n
    assert len({r["effect_id"] for r in reported}) == n


def test_no_write_transaction_is_held_across_the_effect(journal_path, world,
                                                        tmp_path):
    """§9/§23 — proved by construction: four workers hold EXECUTING rows at the
    same time. A transaction spanning the tool call would make that impossible."""
    n = 4
    park = tmp_path / "park"
    results = [tmp_path / f"w{i}.json" for i in range(n)]
    payloads = [
        spec(journal_path, world, args={"target": f"h{i}"},
             park_dir=str(park), worker_tag=f"w{i}")
        for i in range(n)
    ]
    procs = [start_worker(p, r) for p, r in zip(payloads, results)]
    try:
        # All four are now parked HOLDING their reservations. If the reservation
        # transaction were still open, this read would block until they finished.
        await_parked(park, n)
        journal = open_journal(journal_path)
        assert journal.status()["reserved"] == n, (
            "the journal was not readable while four owners held reservations")
        release_parked(park)
        for proc in procs:
            finish(proc)
    finally:
        release_parked(park)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    assert world.effect_count == n


# ══════════════════════════════════════════════════════════════════════════════
#  PROCESS_RESTART — §37, the canonical M65C win
# ══════════════════════════════════════════════════════════════════════════════
def test_a_committed_effect_is_not_repeated_by_a_fresh_process(journal_path,
                                                               world, tmp_path):
    """§37 — process A commits and exits; process B starts a brand new
    interpreter and asks for the same effect. One effect."""
    first = run_worker(spec(journal_path, world), result=tmp_path / "a.json")
    assert first["committed"] is True
    assert world.effect_count == 1

    second = run_worker(spec(journal_path, world), result=tmp_path / "b.json")
    assert second["owned"] is False
    assert second["reservation"] == ReservationOutcome.ALREADY_COMMITTED.value
    assert world.effect_count == 1, "a restart repeated a committed effect"
    assert world.attempt_count == 1, "the tool was invoked a second time"


def test_the_durable_receipt_survives_the_process_that_wrote_it(journal_path,
                                                               world, tmp_path):
    run_worker(spec(journal_path, world), result=tmp_path / "a.json")
    record = open_journal(journal_path).get(effect_id())
    assert record.state is EffectState.COMMITTED
    assert record.receipt_digest and len(record.receipt_digest) == 64
    assert record.committed_at


# ══════════════════════════════════════════════════════════════════════════════
#  CHAOS POINTS C0-C5 — §32
# ══════════════════════════════════════════════════════════════════════════════
def test_c0_crash_before_reserve_leaves_no_trace(journal_path, world, tmp_path):
    """P0. Nothing was reserved and nothing ran, so a retry is free."""
    run_worker(spec(journal_path, world, crash_at="C0"), expect_crash=True)
    assert world.effect_count == 0
    journal = open_journal(journal_path)
    assert journal.get(effect_id()) is None

    run_worker(spec(journal_path, world), result=tmp_path / "b.json")
    assert world.effect_count == 1


def test_c1_crash_after_reserve_leaves_a_reclaimable_reservation(journal_path,
                                                                 world, tmp_path):
    """P1. RESERVED, never EXECUTING, so the effect provably never started."""
    run_worker(spec(journal_path, world, crash_at="C1", lease_s=1,
                    lease_grace_s=0), expect_crash=True)
    assert world.effect_count == 0
    record = open_journal(journal_path).get(effect_id())
    assert record.state is EffectState.RESERVED

    time.sleep(1.2)   # the only wall-clock wait: a real lease must really expire
    second = run_worker(spec(journal_path, world, lease_s=60, lease_grace_s=0),
                        result=tmp_path / "b.json")
    assert second["reservation"] == ReservationOutcome.RECLAIMED.value
    assert world.effect_count == 1, "a reclaimed pre-effect reservation misfired"


def test_c2_crash_immediately_before_the_tool_is_still_pre_effect(journal_path,
                                                                 world, tmp_path):
    run_worker(spec(journal_path, world, crash_at="C2", lease_s=1,
                    lease_grace_s=0), expect_crash=True)
    assert world.effect_count == 0
    assert open_journal(journal_path).get(effect_id()).state is EffectState.RESERVED


def test_c3_the_effect_happened_and_the_journal_says_executing(journal_path,
                                                               world, tmp_path):
    """P3, the window the milestone is named for. The effect is real and the
    journal cannot tell that from C2."""
    run_worker(spec(journal_path, world, crash_at="C3"), expect_crash=True)
    assert world.effect_count == 1, "the synthetic effect did not happen"
    assert open_journal(journal_path).get(effect_id()).state is EffectState.EXECUTING


def test_c4_crash_after_commit_is_fully_recoverable(journal_path, world,
                                                    tmp_path):
    """§17/P4 — the easiest durable case and it must be proven. The caller never
    saw the result; a fresh process must recover it and not re-run."""
    run_worker(spec(journal_path, world, crash_at="C4"), expect_crash=True)
    assert world.effect_count == 1
    assert open_journal(journal_path).get(effect_id()).state is EffectState.COMMITTED

    second = run_worker(spec(journal_path, world), result=tmp_path / "b.json")
    assert second["reservation"] == ReservationOutcome.ALREADY_COMMITTED.value
    assert world.effect_count == 1, "a P4 crash was repeated on restart"


def test_c5_crash_after_delivery_changes_nothing(journal_path, world, tmp_path):
    """P5. The result was delivered; a later death is irrelevant to the world."""
    result = tmp_path / "a.json"
    run_worker(spec(journal_path, world, crash_at="C5"), result=result,
               expect_crash=True)
    assert json.loads(result.read_text())["committed"] is True
    assert world.effect_count == 1
    second = run_worker(spec(journal_path, world), result=tmp_path / "b.json")
    assert second["reservation"] == ReservationOutcome.ALREADY_COMMITTED.value
    assert world.effect_count == 1


# ══════════════════════════════════════════════════════════════════════════════
#  P3_CRASH by durability class — §16, the mandatory three
# ══════════════════════════════════════════════════════════════════════════════
def test_p3_non_replayable_becomes_indeterminate_and_is_not_retried(
        journal_path, world, tmp_path):
    """§16/§61 — the fail-closed case. The effect happened; nothing local can
    prove it; the system refuses to guess."""
    payload = spec(journal_path, world, crash_at="C3", lease_s=1, lease_grace_s=0)
    run_worker(payload, expect_crash=True)
    assert world.effect_count == 1
    time.sleep(1.2)

    second = run_worker(spec(journal_path, world, lease_s=60, lease_grace_s=0),
                        result=tmp_path / "b.json")
    assert second["owned"] is False
    assert second["reservation"] == ReservationOutcome.INDETERMINATE.value
    assert world.effect_count == 1, "a NON_REPLAYABLE P3 effect was duplicated"
    assert open_journal(journal_path).get(effect_id()).state is \
        EffectState.INDETERMINATE
    assert not may_auto_retry(EffectState.INDETERMINATE,
                              EffectDurabilityClass.NON_REPLAYABLE)


def test_p3_idempotent_with_key_recovers_to_exactly_one_external_effect(
        journal_path, world, tmp_path):
    """§16 — replay with the SAME derived key; the external system deduplicates.
    Two invocations, one external effect."""
    payload = spec(journal_path, world, mode="idempotent_key",
                   cls=EffectDurabilityClass.IDEMPOTENT_WITH_KEY,
                   crash_at="C3", lease_s=1, lease_grace_s=0)
    run_worker(payload, expect_crash=True)
    assert world.effect_count == 1 and world.attempt_count == 1
    time.sleep(1.2)

    second = run_worker(
        spec(journal_path, world, mode="idempotent_key",
             cls=EffectDurabilityClass.IDEMPOTENT_WITH_KEY, lease_s=60,
             lease_grace_s=0),
        result=tmp_path / "b.json")
    assert second["owned"] is True
    assert second["reservation"] == ReservationOutcome.RECLAIMED.value
    assert second["committed"] is True
    assert world.attempt_count == 2, "the replay did not happen"
    assert world.effect_count == 1, (
        "the idempotency key did not deduplicate the external effect")
    assert second["effect_result"]["status"] == "deduplicated_by_external_system"


def test_the_idempotency_key_is_identical_across_the_restart(journal_path,
                                                             world, tmp_path):
    """§14/§55 — a key that changed across a restart would defeat external
    deduplication at exactly the moment it is needed."""
    payload = spec(journal_path, world, mode="idempotent_key",
                   cls=EffectDurabilityClass.IDEMPOTENT_WITH_KEY,
                   crash_at="C3", lease_s=1, lease_grace_s=0)
    run_worker(payload, expect_crash=True)
    first_key = open_journal(journal_path).get(effect_id()).idempotency_key
    time.sleep(1.2)
    run_worker(spec(journal_path, world, mode="idempotent_key",
                    cls=EffectDurabilityClass.IDEMPOTENT_WITH_KEY, lease_s=60,
                    lease_grace_s=0), result=tmp_path / "b.json")
    assert open_journal(journal_path).get(effect_id()).idempotency_key == first_key
    assert first_key == derive_idempotency_key(effect_id())
    # And the external system saw exactly that one key.
    keys = [p.name for p in world.effects_dir.iterdir()]
    assert keys == [f"key-{first_key}"]


def test_p3_reconcilable_recovers_without_replaying(journal_path, world,
                                                    tmp_path):
    """§16 — restart, reconcile, and because it is CONFIRMED_COMMITTED, do not
    replay. One external effect and one invocation."""
    payload = spec(journal_path, world, mode="reconcilable",
                   cls=EffectDurabilityClass.RECONCILABLE, crash_at="C3",
                   lease_s=1, lease_grace_s=0)
    run_worker(payload, expect_crash=True)
    assert world.effect_count == 1 and world.attempt_count == 1
    time.sleep(1.2)

    journal = open_journal(journal_path, lease_s=60, lease_grace_s=0)
    reservation = journal.reserve(
        effect_id=effect_id(), tool_id=TOOL, surface="native",
        durability_class=EffectDurabilityClass.RECONCILABLE, tool_input=ARGS)
    assert reservation.outcome is ReservationOutcome.RECONCILE_REQUIRED
    assert not reservation.owned

    probe = make_reconciler(str(world.root))
    verdict = probe(effect_id(), reservation.record.idempotency_key)
    assert verdict is ReconciliationVerdict.CONFIRMED_COMMITTED
    journal.apply_reconciliation(effect_id(), verdict)

    after = journal.reserve(
        effect_id=effect_id(), tool_id=TOOL, surface="native",
        durability_class=EffectDurabilityClass.RECONCILABLE, tool_input=ARGS)
    assert after.outcome is ReservationOutcome.ALREADY_COMMITTED
    assert world.effect_count == 1 and world.attempt_count == 1, (
        "a reconciled-committed effect was replayed")


def test_reconciliation_confirming_absence_permits_exactly_one_run(journal_path,
                                                                   world,
                                                                   tmp_path):
    """The C2 half of the ambiguity: the effect really did NOT happen, the probe
    proves it, and one run follows."""
    payload = spec(journal_path, world, mode="reconcilable",
                   cls=EffectDurabilityClass.RECONCILABLE, crash_at="C2",
                   lease_s=1, lease_grace_s=0)
    run_worker(payload, expect_crash=True)
    assert world.effect_count == 0
    time.sleep(1.2)

    journal = open_journal(journal_path, lease_s=60, lease_grace_s=0)
    reservation = journal.reserve(
        effect_id=effect_id(), tool_id=TOOL, surface="native",
        durability_class=EffectDurabilityClass.RECONCILABLE, tool_input=ARGS)
    # A stale PRE-effect reservation is reclaimable for every class.
    assert reservation.outcome is ReservationOutcome.RECLAIMED
    probe = make_reconciler(str(world.root))
    assert probe(effect_id(), reservation.record.idempotency_key) is \
        ReconciliationVerdict.CONFIRMED_NOT_EXECUTED


def test_an_unreadable_world_reconciles_to_unknown(journal_path, world,
                                                   tmp_path):
    """§15 — UNKNOWN is a real answer and must survive as one."""
    import shutil

    probe = make_reconciler(str(world.root))
    shutil.rmtree(world.reconcile_dir)
    assert probe("eid", "key") is ReconciliationVerdict.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
#  SIGKILL — §33
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_a_sigkilled_owner_leaves_a_recoverable_journal(journal_path, world,
                                                        tmp_path):
    """§33 — SIGKILL from the parent to its OWN child, not os._exit.

    A signal the process cannot handle is the closest thing to power loss that a
    test can arrange, and it is categorically different from an exception: no
    handler runs, no buffer flushes, no reservation is released.
    """
    park = tmp_path / "park"
    payload = spec(journal_path, world, park_dir=str(park), worker_tag="victim",
                   lease_s=1, lease_grace_s=0)
    proc = start_worker(payload)
    try:
        # The child is parked holding a RESERVED row and has invoked nothing.
        # It is never released: it dies where it stands.
        await_parked(park, 1)
        os.kill(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=JOIN_S)
    finally:
        if proc.poll() is None:          # never leave a child of ours behind
            proc.kill()
            proc.communicate()
    assert proc.returncode == -signal.SIGKILL

    record = open_journal(journal_path).get(effect_id())
    assert record is not None and record.state is EffectState.RESERVED
    assert world.effect_count == 0
    time.sleep(1.2)
    second = run_worker(spec(journal_path, world, lease_s=60, lease_grace_s=0),
                        result=tmp_path / "b.json")
    assert second["reservation"] == ReservationOutcome.RECLAIMED.value
    assert world.effect_count == 1


# ══════════════════════════════════════════════════════════════════════════════
#  STALE_OWNER — §38/§39
# ══════════════════════════════════════════════════════════════════════════════
def test_a_stale_executing_owner_never_implies_not_executed(journal_path, world,
                                                            tmp_path):
    """§39/§55 — the highest-value invariant in the milestone."""
    run_worker(spec(journal_path, world, crash_at="C3", lease_s=1,
                    lease_grace_s=0), expect_crash=True)
    time.sleep(1.2)
    journal = open_journal(journal_path, lease_s=60, lease_grace_s=0)
    record = journal.get(effect_id())
    assert journal.lease_expired(record) is True
    reservation = journal.reserve(
        effect_id=effect_id(), tool_id=TOOL, surface="native",
        durability_class=EffectDurabilityClass.NON_REPLAYABLE, tool_input=ARGS)
    assert not reservation.owned
    assert journal.get(effect_id()).state is EffectState.INDETERMINATE


def test_a_reservation_is_never_permanently_poisoned(journal_path, world,
                                                     tmp_path):
    """§38 — a dead pre-effect owner must not lock the identity forever."""
    run_worker(spec(journal_path, world, crash_at="C1", lease_s=1,
                    lease_grace_s=0), expect_crash=True)
    time.sleep(1.2)
    for _ in range(2):
        journal = open_journal(journal_path, lease_s=60, lease_grace_s=0)
        record = journal.get(effect_id())
        if record.state is EffectState.RESERVED:
            break
    second = run_worker(spec(journal_path, world, lease_s=60, lease_grace_s=0),
                        result=tmp_path / "b.json")
    assert second["owned"] is True
    assert world.effect_count == 1


def test_startup_recovery_after_a_crash_classifies_without_executing(
        journal_path, world, tmp_path):
    """§49 — boot inspects and classifies. It never re-runs a stale effect."""
    run_worker(spec(journal_path, world, crash_at="C3", lease_s=1,
                    lease_grace_s=0), expect_crash=True)
    assert world.effect_count == 1
    time.sleep(1.2)

    journal = open_journal(journal_path, lease_s=60, lease_grace_s=0)
    report = journal.startup_recovery()
    assert report["classified_indeterminate"] == 1
    assert world.effect_count == 1, "startup recovery executed something"
    assert journal.status()["recovery_required"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMA INITIALISATION RACE — §22
# ══════════════════════════════════════════════════════════════════════════════
def test_many_processes_creating_the_journal_at_once_all_succeed(journal_path,
                                                                 world, tmp_path):
    """§22 — an absent journal, six processes, one valid schema, no partial one.

    The CREATE statements run inside one transaction rather than through
    ``executescript``, which commits any open transaction before it runs and
    would let a racer observe a half-built schema.
    """
    n = 6
    results = [tmp_path / f"w{i}.json" for i in range(n)]
    payloads = [
        spec(journal_path, world, args={"target": f"t{i}"},
             barrier_dir=str(tmp_path / "gate"), barrier_count=n,
             worker_tag=f"w{i}")
        for i in range(n)
    ]
    race(payloads, results)
    journal = open_journal(journal_path)
    journal.assert_healthy()
    assert journal.status()["committed"] == n
    assert world.effect_count == n


def test_contention_is_bounded_and_reported(journal_path, world, tmp_path):
    """§24 — a busy timeout that is finite, and lock pressure that resolves."""
    n = 6
    results = [tmp_path / f"w{i}.json" for i in range(n)]
    payloads = [
        spec(journal_path, world, args={"target": f"t{i}"},
             busy_timeout_ms=3000, barrier_dir=str(tmp_path / "gate"),
             barrier_count=n, worker_tag=f"w{i}")
        for i in range(n)
    ]
    started = time.monotonic()
    race(payloads, results)
    assert time.monotonic() - started < JOIN_S


# ══════════════════════════════════════════════════════════════════════════════
#  PRIVACY ACROSS PROCESSES — §52
# ══════════════════════════════════════════════════════════════════════════════
def test_no_secret_from_a_worker_reaches_the_journal_bytes(journal_path, world,
                                                           tmp_path):
    secret = "AKIAM65CSECRETACCESSKEYVALUE0001"
    run_worker(spec(journal_path, world, args={"token": secret}),
               result=tmp_path / "a.json")
    blob = b"".join(p.read_bytes() for p in journal_path.parent.iterdir()
                    if p.is_file() and p.name.startswith("effects.db"))
    assert secret.encode() not in blob, "a worker's secret argument was persisted"
