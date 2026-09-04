"""
tests/test_effect_journal_v69_m65c.py — V69 M65C DURABLE EFFECT JOURNAL.

This file covers the journal AS A DURABLE STORE: schema, migration, the state
machine, atomic reservation and body-safety. Cross-process behaviour, crash
recovery and the executor protocol live in their own files, because a store that
is correct in one process proves nothing about two.

Every journal here is built on a per-test ``tmp_path``. Nothing in this file
touches the configured production journal.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.effect_journal import (
    SCHEMA_VERSION,
    DurableEffectJournal,
    EffectDurabilityClass,
    EffectState,
    InvalidTransition,
    JournalUnhealthy,
    ReconciliationVerdict,
    ReservationOutcome,
    args_digest,
    canonical_json,
    compute_effect_id,
    derive_idempotency_key,
    durability_class,
    may_auto_retry,
    opaque_digest,
    register_durability,
    register_reconciler,
    runtime_instance_id,
    unregister_durability,
)

TOOL = "code_execute"
ARGS = {"code": "print(1)"}
SCOPE = "turn:m65c"


class Clock:
    """An injectable clock. Every lease test moves time deliberately rather
    than sleeping, so nothing here can pass or fail on machine speed."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_journal(tmp_path, **kw) -> DurableEffectJournal:
    return DurableEffectJournal(tmp_path / "effects.db", **kw)


def effect_id(tool: str = TOOL, args=None, scope: str = SCOPE,
              surface: str = "native") -> str:
    return compute_effect_id(surface=surface, tool_id=tool,
                             identity_scope=scope,
                             tool_input=ARGS if args is None else args)


def reserve(journal, *, eid=None, tool=TOOL, args=None,
            cls=EffectDurabilityClass.NON_REPLAYABLE, surface="native", **kw):
    payload = ARGS if args is None else args
    return journal.reserve(
        effect_id=eid or effect_id(tool, payload, surface=surface), tool_id=tool,
        surface=surface, durability_class=cls, tool_input=payload, **kw)


# ══════════════════════════════════════════════════════════════════════════════
#  JOURNAL_SCHEMA
# ══════════════════════════════════════════════════════════════════════════════
def test_a_fresh_journal_creates_its_schema_and_records_the_version(tmp_path):
    j = make_journal(tmp_path)
    assert (tmp_path / "effects.db").exists()
    row = j._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert int(row["value"]) == SCHEMA_VERSION


def test_reopening_an_existing_journal_preserves_its_rows(tmp_path):
    """The whole point. A journal that forgets on reopen is not durable."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.commit(eid, receipt={"stdout": "1"})
    j.close()

    reopened = make_journal(tmp_path)
    record = reopened.get(eid)
    assert record is not None and record.state is EffectState.COMMITTED


def test_a_newer_schema_is_refused_rather_than_rewritten(tmp_path):
    """§21 — fail closed. A newer layout may carry states this build cannot
    honour, and guessing at one is how INDETERMINATE becomes a safe retry."""
    j = make_journal(tmp_path)
    j._db.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                  (str(SCHEMA_VERSION + 7),))
    j.close()

    with pytest.raises(JournalUnhealthy) as exc:
        make_journal(tmp_path)
    assert "newer" in str(exc.value)


def test_a_newer_schema_is_not_deleted_or_recreated(tmp_path):
    """§25 — refusing must not be implemented by starting over."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j._db.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                  (str(SCHEMA_VERSION + 1),))
    j.close()
    before = (tmp_path / "effects.db").read_bytes()

    with pytest.raises(JournalUnhealthy):
        make_journal(tmp_path)
    assert (tmp_path / "effects.db").read_bytes() == before, (
        "the journal was modified while being refused")


def test_an_unknown_migration_step_fails_closed(tmp_path):
    """A gap in the migration table is refused, never bridged by guessing."""
    j = make_journal(tmp_path)
    j._db.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    j.close()
    with pytest.raises(JournalUnhealthy) as exc:
        make_journal(tmp_path)
    assert "migration" in str(exc.value)


def test_a_missing_schema_version_row_is_unhealthy(tmp_path):
    j = make_journal(tmp_path)
    j._db.execute("DELETE FROM meta WHERE key='schema_version'")
    with pytest.raises(JournalUnhealthy):
        j.assert_healthy()


def test_the_journal_file_is_owner_only(tmp_path):
    """§27 — restrictive by default. Defence in depth: the journal stores no
    secret, so its confidentiality is deliberately not load-bearing."""
    make_journal(tmp_path)
    mode = (tmp_path / "effects.db").stat().st_mode & 0o777
    assert mode == 0o600, f"journal is {oct(mode)}"


def test_an_unopenable_journal_raises_rather_than_going_in_memory(tmp_path):
    """§25 — the operational store's fail-OPEN degradation is exactly what this
    module must not do: a volatile journal claiming durability authorises a
    duplicate effect."""
    blocker = tmp_path / "blocked"
    blocker.mkdir()
    with pytest.raises(JournalUnhealthy):
        DurableEffectJournal(blocker)  # a directory is not a database


def test_pragmas_are_configured_as_documented(tmp_path):
    """Each of these exists for a stated reason; a silent regression to a
    SQLite default would remove a guarantee without failing anything else."""
    j = make_journal(tmp_path, busy_timeout_ms=1234)
    assert j._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert j._db.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    # synchronous=FULL is 2. NORMAL (1) would not fsync the WAL on commit, so a
    # power loss could lose a COMMITTED row for an effect that really happened.
    assert j._db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert j._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# ══════════════════════════════════════════════════════════════════════════════
#  JOURNAL_STATE_MACHINE
# ══════════════════════════════════════════════════════════════════════════════
def test_the_happy_path_walks_reserved_executing_committed(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    assert reserve(j, eid=eid).outcome is ReservationOutcome.OWNED
    assert j.get(eid).state is EffectState.RESERVED
    assert j.mark_executing(eid)
    assert j.get(eid).state is EffectState.EXECUTING
    assert j.commit(eid, receipt={"ok": True})
    assert j.get(eid).state is EffectState.COMMITTED


def test_executing_can_never_become_failed_before_effect(tmp_path):
    """THE most dangerous edge in the module. Once EXECUTING is durable nothing
    local can prove the effect did not start, so the edge does not exist."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    with pytest.raises(InvalidTransition):
        j.fail_before_effect(eid, "pretend it never ran")
    assert j.get(eid).state is EffectState.EXECUTING


def test_committed_is_terminal_and_cannot_be_reopened(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.commit(eid, receipt={"ok": True})
    for attempt in (lambda: j.mark_executing(eid),
                    lambda: j.fail_before_effect(eid, "x"),
                    lambda: j.mark_indeterminate(eid, "x")):
        with pytest.raises(InvalidTransition):
            attempt()
    assert j.get(eid).state is EffectState.COMMITTED


def test_a_transition_from_the_wrong_state_is_refused(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    with pytest.raises(InvalidTransition):
        j.commit(eid, receipt={"ok": True})   # never went EXECUTING
    assert j.get(eid).state is EffectState.RESERVED


def test_every_transition_is_appended_to_the_audit_trail(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.commit(eid, receipt={"ok": True})
    edges = [(t["from_state"], t["to_state"]) for t in j.transitions(eid)]
    assert edges == [("RESERVED", "RESERVED"), ("RESERVED", "EXECUTING"),
                     ("EXECUTING", "COMMITTED")]


def test_failed_before_effect_permits_a_fresh_reservation(tmp_path):
    """Proven pre-effect, so a retry is legitimate and takes ownership again."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    assert j.fail_before_effect(eid, "hitl_denied")
    again = reserve(j, eid=eid)
    assert again.outcome is ReservationOutcome.OWNED and again.owned
    assert again.record.owner_attempt == 2


def test_a_committed_effect_is_never_owned_again(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.commit(eid, receipt={"ok": True})
    again = reserve(j, eid=eid)
    assert again.outcome is ReservationOutcome.ALREADY_COMMITTED
    assert not again.owned, "a committed effect was handed out for re-execution"


def test_indeterminate_blocks_a_later_caller(tmp_path):
    """§5 — 'the journal lacks COMMITTED' is not 'it did not happen'."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner vanished")
    again = reserve(j, eid=eid)
    assert again.outcome is ReservationOutcome.INDETERMINATE
    assert not again.owned


def test_reconciliation_unknown_writes_nothing(tmp_path):
    """§15 — uncertainty is never rounded into an outcome."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner vanished")
    assert j.apply_reconciliation(eid, ReconciliationVerdict.UNKNOWN) is False
    assert j.get(eid).state is EffectState.INDETERMINATE


def test_reconciliation_confirmed_committed_records_a_commit_time(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner vanished")
    assert j.apply_reconciliation(eid, ReconciliationVerdict.CONFIRMED_COMMITTED)
    record = j.get(eid)
    assert record.state is EffectState.RECONCILED_COMMITTED
    assert record.committed_at and record.proven_committed


def test_reconciliation_confirmed_absent_permits_a_fresh_run(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner vanished")
    assert j.apply_reconciliation(eid, ReconciliationVerdict.CONFIRMED_NOT_EXECUTED)
    again = reserve(j, eid=eid)
    assert again.owned and again.outcome is ReservationOutcome.OWNED


# ══════════════════════════════════════════════════════════════════════════════
#  ATOMIC_RESERVATION (single process; the cross-process proof is its own file)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_second_reservation_of_a_live_owner_is_refused(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    assert reserve(j, eid=eid).owned
    second = reserve(j, eid=eid)
    assert second.outcome is ReservationOutcome.OWNED_ELSEWHERE
    assert not second.owned


def test_the_owner_recorded_is_this_runtime_instance(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    assert reserve(j, eid=eid).record.owner_instance_id == runtime_instance_id()


def test_a_runtime_instance_id_is_not_the_pid(tmp_path):
    """§10 — PIDs are reused, so a PID alone would let an unrelated process
    'confirm' a dead owner alive after a restart."""
    import os
    ident = runtime_instance_id()
    assert ident != str(os.getpid())
    assert ident.endswith(f".{os.getpid()}")
    assert len(ident.split(".")[0]) == 32, "the unique part is not a UUID"


def test_two_journals_on_one_file_produce_exactly_one_owner(tmp_path):
    """Two independent handles with DIFFERENT instance identities — the
    in-process shape of the cross-process race."""
    a = DurableEffectJournal(tmp_path / "effects.db", instance_id="inst-a")
    b = DurableEffectJournal(tmp_path / "effects.db", instance_id="inst-b")
    eid = effect_id()
    first = reserve(a, eid=eid)
    second = reserve(b, eid=eid)
    assert first.owned and not second.owned
    assert second.outcome is ReservationOutcome.OWNED_ELSEWHERE
    assert a.get(eid).owner_instance_id == "inst-a"


def test_different_identities_do_not_block_each_other(tmp_path):
    """§9 — the journal serialises one identity, never the whole executor."""
    j = make_journal(tmp_path)
    one = reserve(j, eid=effect_id(args={"code": "a"}))
    two = reserve(j, eid=effect_id(args={"code": "b"}))
    assert one.owned and two.owned


def test_no_write_transaction_is_left_open_after_a_reservation(tmp_path):
    """§9/§23 — a transaction held across a tool call would serialise every
    other process behind an external network round trip."""
    j = make_journal(tmp_path)
    reserve(j, eid=effect_id())
    assert not j._db.in_transaction


def test_a_refused_transition_leaves_no_open_transaction(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    with pytest.raises(InvalidTransition):
        j.commit(eid, receipt={})
    assert not j._db.in_transaction
    assert reserve(j, eid=effect_id(args={"code": "other"})).owned


# ══════════════════════════════════════════════════════════════════════════════
#  LEASES / STALE OWNERS / CLOCK
# ══════════════════════════════════════════════════════════════════════════════
def test_a_live_lease_is_not_reclaimable(tmp_path):
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=10)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=10)
    eid = effect_id()
    reserve(a, eid=eid)
    clock.advance(50)
    assert reserve(b, eid=eid).outcome is ReservationOutcome.OWNED_ELSEWHERE


def test_a_stale_pre_effect_reservation_is_reclaimable(tmp_path):
    """§38 — P1. EXECUTING is durable before the tool runs, so a RESERVED owner
    provably never invoked anything and reclaiming is safe for every class."""
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=10)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=10)
    eid = effect_id()
    reserve(a, eid=eid)
    clock.advance(200)
    taken = reserve(b, eid=eid)
    assert taken.outcome is ReservationOutcome.RECLAIMED and taken.owned
    assert taken.record.owner_instance_id == "b"
    assert taken.record.owner_attempt == 2


def test_a_stale_executing_owner_is_never_silently_reclaimed(tmp_path):
    """§39 — the milestone's central rule. An expired lease over an EXECUTING
    row does NOT mean the effect did not happen."""
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=10)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=10)
    eid = effect_id()
    reserve(a, eid=eid)
    a.mark_executing(eid)
    clock.advance(200)
    taken = reserve(b, eid=eid)
    assert not taken.owned, "a stale EXECUTING owner was blindly reclaimed"
    assert taken.outcome is ReservationOutcome.INDETERMINATE
    assert b.get(eid).state is EffectState.INDETERMINATE


def test_a_stale_executing_idempotent_owner_may_replay(tmp_path):
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=10)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=10)
    eid = effect_id()
    reserve(a, eid=eid, cls=EffectDurabilityClass.IDEMPOTENT)
    a.mark_executing(eid)
    clock.advance(200)
    taken = reserve(b, eid=eid, cls=EffectDurabilityClass.IDEMPOTENT)
    assert taken.owned and taken.outcome is ReservationOutcome.RECLAIMED


def test_a_stale_executing_reconcilable_owner_asks_for_reconciliation(tmp_path):
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=10)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=10)
    eid = effect_id()
    reserve(a, eid=eid, cls=EffectDurabilityClass.RECONCILABLE)
    a.mark_executing(eid)
    clock.advance(200)
    taken = reserve(b, eid=eid, cls=EffectDurabilityClass.RECONCILABLE)
    assert not taken.owned
    assert taken.outcome is ReservationOutcome.RECONCILE_REQUIRED


def test_a_clock_that_moves_backward_never_expires_a_lease(tmp_path):
    """§40 — a clock anomaly must not manufacture a reclaimable owner."""
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=10)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=10)
    eid = effect_id()
    reserve(a, eid=eid)
    clock.advance(-3600)
    assert reserve(b, eid=eid).outcome is ReservationOutcome.OWNED_ELSEWHERE


def test_a_small_forward_clock_jump_is_absorbed_by_the_grace(tmp_path):
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=100, lease_grace_s=60)
    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=100, lease_grace_s=60)
    eid = effect_id()
    reserve(a, eid=eid)
    clock.advance(130)          # past the lease, inside the grace
    assert reserve(b, eid=eid).outcome is ReservationOutcome.OWNED_ELSEWHERE
    clock.advance(60)           # now past lease + grace
    assert reserve(b, eid=eid).owned


def test_a_malformed_lease_timestamp_is_treated_as_live(tmp_path):
    """Guessing wrong here costs a duplicate effect, so an unreadable expiry
    means 'not expired', never 'expired'."""
    j = make_journal(tmp_path, instance_id="a")
    eid = effect_id()
    reserve(j, eid=eid)
    j._db.execute("UPDATE effects SET lease_expires_at='not-a-time' WHERE effect_id=?",
                  (eid,))
    assert j.lease_expired(j.get(eid)) is False


# ══════════════════════════════════════════════════════════════════════════════
#  IDEMPOTENCY KEY + CANONICAL IDENTITY
# ══════════════════════════════════════════════════════════════════════════════
def test_argument_order_cannot_manufacture_a_second_identity(tmp_path):
    assert compute_effect_id(surface="native", tool_id=TOOL, identity_scope=SCOPE,
                             tool_input={"a": 1, "b": 2}) == \
           compute_effect_id(surface="native", tool_id=TOOL, identity_scope=SCOPE,
                             tool_input={"b": 2, "a": 1})


def test_nested_argument_order_is_also_canonical(tmp_path):
    left = {"outer": {"x": 1, "y": [1, {"p": 0, "q": 1}]}}
    right = {"outer": {"y": [1, {"q": 1, "p": 0}], "x": 1}}
    assert args_digest(left) == args_digest(right)


def test_a_meaningful_argument_change_is_a_different_identity(tmp_path):
    assert effect_id(args={"code": "a"}) != effect_id(args={"code": "b"})


def test_distinct_types_are_not_over_normalised(tmp_path):
    """§53 — '1' and 1 are different executable requests."""
    assert args_digest({"n": 1}) != args_digest({"n": "1"})


def test_a_different_tool_is_a_different_identity(tmp_path):
    assert effect_id(tool="code_execute") != effect_id(tool="write_file")


def test_a_different_surface_is_a_different_identity(tmp_path):
    assert effect_id(surface="native") != effect_id(surface="mcp")


def test_the_idempotency_key_is_stable_and_derived_only_from_identity(tmp_path):
    """§14 — no timestamp, no randomness; the same across every attempt and
    every restart, which is the only reason external deduplication works."""
    eid = effect_id()
    assert derive_idempotency_key(eid) == derive_idempotency_key(eid)
    assert derive_idempotency_key(eid) != derive_idempotency_key(effect_id(args={"code": "z"}))


def test_a_user_supplied_idempotency_key_argument_is_not_the_key(tmp_path):
    """§14 — never let model-authored text choose the canonical key. An
    argument by that name feeds the identity like any other and cannot BE it."""
    forged = {"code": "print(1)", "idempotency_key": "attacker-chosen"}
    eid = effect_id(args=forged)
    assert derive_idempotency_key(eid) != "attacker-chosen"
    assert "attacker-chosen" not in derive_idempotency_key(eid)


def test_the_stored_key_survives_a_reopen(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    key = reserve(j, eid=eid).record.idempotency_key
    j.close()
    assert make_journal(tmp_path).get(eid).idempotency_key == key
    assert key == derive_idempotency_key(eid)


# ══════════════════════════════════════════════════════════════════════════════
#  DURABILITY POLICY
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unclassified_effectful_tool_is_non_replayable(tmp_path):
    """§13 — the fail-closed default. Misclassifying an irreversible action as
    replayable costs a duplicate; the reverse costs a manual reconciliation."""
    assert durability_class("some_tool_invented_tomorrow") is \
        EffectDurabilityClass.NON_REPLAYABLE


def test_every_reachable_effectful_tool_has_a_class(tmp_path):
    """The audit is real: resolve the class for every tool the risk taxonomy
    actually knows, and assert none of them lands somewhere unexpected."""
    from core.risk_classes import TOOL_RISK_CLASS

    for name, risk in TOOL_RISK_CLASS.items():
        cls = durability_class(name, risk)
        if risk.value == "read_only":
            assert cls is EffectDurabilityClass.READ_ONLY, name
        else:
            assert cls is not EffectDurabilityClass.READ_ONLY, name


def test_write_file_is_not_classified_idempotent(tmp_path):
    """It accepts mode='a'. An append repeated is an append duplicated, and a
    per-tool table must hold for every argument the tool accepts."""
    assert durability_class("write_file") is EffectDurabilityClass.NON_REPLAYABLE


def test_a_registration_can_declare_a_class(tmp_path):
    try:
        register_durability("m65c_synthetic", EffectDurabilityClass.IDEMPOTENT_WITH_KEY)
        assert durability_class("m65c_synthetic") is \
            EffectDurabilityClass.IDEMPOTENT_WITH_KEY
    finally:
        unregister_durability("m65c_synthetic")
    assert durability_class("m65c_synthetic") is EffectDurabilityClass.NON_REPLAYABLE


def test_a_non_class_registration_is_refused(tmp_path):
    with pytest.raises(TypeError):
        register_durability("m65c_bogus", "IDEMPOTENT")


def test_indeterminate_is_not_safe_to_retry(tmp_path):
    """§12 — the absolute rule, as a pure function."""
    assert not may_auto_retry(EffectState.INDETERMINATE,
                              EffectDurabilityClass.NON_REPLAYABLE)
    assert not may_auto_retry(EffectState.INDETERMINATE,
                              EffectDurabilityClass.RECONCILABLE)
    assert may_auto_retry(EffectState.INDETERMINATE,
                          EffectDurabilityClass.IDEMPOTENT)
    assert may_auto_retry(EffectState.INDETERMINATE,
                          EffectDurabilityClass.IDEMPOTENT_WITH_KEY)


def test_a_committed_effect_is_never_retryable(tmp_path):
    for cls in EffectDurabilityClass:
        assert not may_auto_retry(EffectState.COMMITTED, cls)
        assert not may_auto_retry(EffectState.RECONCILED_COMMITTED, cls)


def test_proven_pre_effect_is_always_retryable(tmp_path):
    for cls in EffectDurabilityClass:
        assert may_auto_retry(EffectState.FAILED_BEFORE_EFFECT, cls)
        assert may_auto_retry(EffectState.RECONCILED_NOT_EXECUTED, cls)


def test_a_reconciler_that_raises_is_read_as_unknown(tmp_path):
    from core.effect_journal import reconcile

    def _explode(effect_id, key):
        raise RuntimeError("the probe broke")

    try:
        register_reconciler("m65c_probe", _explode)
        assert reconcile("m65c_probe", "eid", "key") is ReconciliationVerdict.UNKNOWN
    finally:
        unregister_durability("m65c_probe")


def test_a_reconciler_returning_junk_is_read_as_unknown(tmp_path):
    from core.effect_journal import reconcile

    try:
        register_reconciler("m65c_probe", lambda e, k: "CONFIRMED_COMMITTED")
        assert reconcile("m65c_probe", "eid", "key") is ReconciliationVerdict.UNKNOWN
    finally:
        unregister_durability("m65c_probe")


def test_a_tool_with_no_reconciler_is_unknown(tmp_path):
    from core.effect_journal import reconcile

    assert reconcile("nothing_registered", "eid", "key") is \
        ReconciliationVerdict.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
#  JOURNAL_PRIVACY (§52)
# ══════════════════════════════════════════════════════════════════════════════
SECRET_ARG = "sk-live-51H9zzzQQQZZZ-m65c-secret-argument"
SECRET_RESULT = "ghp_m65cSECRETRESULTvaluenobodyshouldpersist"


def test_no_raw_argument_reaches_the_database_bytes(tmp_path):
    j = make_journal(tmp_path)
    args = {"code": f"login('{SECRET_ARG}')"}
    eid = effect_id(args=args)
    reserve(j, eid=eid, args=args)
    j.mark_executing(eid)
    j.commit(eid, receipt={"token": SECRET_RESULT})
    j.close()

    blob = b"".join(
        p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert SECRET_ARG.encode() not in blob, "a raw argument was persisted"
    assert SECRET_RESULT.encode() not in blob, "a raw result was persisted"


def test_the_record_projection_carries_no_body(tmp_path):
    j = make_journal(tmp_path)
    args = {"code": SECRET_ARG}
    eid = effect_id(args=args)
    reserve(j, eid=eid, args=args)
    rendered = canonical_json(j.get(eid).to_dict())
    assert SECRET_ARG not in rendered


def test_the_status_surface_carries_no_body(tmp_path):
    """§30/§51 — counters and sizes, never a payload."""
    j = make_journal(tmp_path)
    args = {"code": SECRET_ARG}
    eid = effect_id(args=args)
    reserve(j, eid=eid, args=args)
    assert SECRET_ARG not in canonical_json(j.status())


def test_a_digest_is_not_reversible_to_the_body(tmp_path):
    d = args_digest({"code": SECRET_ARG})
    assert SECRET_ARG not in d and len(d) == 64


def test_digests_are_domain_separated(tmp_path):
    """Two digests over the same bytes in different roles must differ, or a
    value could be replayed from one field into another."""
    same = {"v": 1}
    assert opaque_digest(same) != args_digest(same)


# ══════════════════════════════════════════════════════════════════════════════
#  CORRUPTION (§25)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_corrupt_journal_fails_closed_and_is_not_deleted(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.close()
    path = tmp_path / "effects.db"
    for side in ("-wal", "-shm"):
        p = tmp_path / f"effects.db{side}"
        if p.exists():
            p.unlink()
    raw = bytearray(path.read_bytes())
    # Shred the b-tree pages AFTER page 1, leaving the file header intact so
    # sqlite still opens it — the point is that a structurally damaged journal is
    # caught and refused, not that an unopenable file is.
    page = int.from_bytes(raw[16:18], "big") or 4096
    raw[page:page * 4] = b"\xff" * (page * 3)
    path.write_bytes(bytes(raw))
    size_before = path.stat().st_size

    with pytest.raises(JournalUnhealthy):
        DurableEffectJournal(path).assert_healthy()
    assert path.exists(), "a corrupt journal was deleted"
    assert path.stat().st_size == size_before, "a corrupt journal was rewritten"


def test_a_missing_table_is_unhealthy(tmp_path):
    j = make_journal(tmp_path)
    j._db.execute("DROP TABLE transitions")
    with pytest.raises(JournalUnhealthy) as exc:
        j.assert_healthy()
    assert "transitions" in str(exc.value)


def test_a_healthy_journal_passes_its_own_check(tmp_path):
    j = make_journal(tmp_path)
    reserve(j, eid=effect_id())
    j.assert_healthy()
    assert j.status()["healthy"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  RETENTION / GROWTH / STARTUP (§29, §30, §49, §50)
# ══════════════════════════════════════════════════════════════════════════════
def test_committed_identities_are_retained(tmp_path):
    """§29 — deleting dedupe history reopens replay risk, so nothing prunes."""
    j = make_journal(tmp_path)
    for n in range(5):
        eid = effect_id(args={"code": f"c{n}"})
        reserve(j, eid=eid, args={"code": f"c{n}"})
        j.mark_executing(eid)
        j.commit(eid, receipt={"n": n})
    j.close()
    assert make_journal(tmp_path).status()["committed"] == 5


def test_startup_recovery_executes_nothing(tmp_path):
    """§49 — it CLASSIFIES. A startup that re-ran stale effects would turn every
    crash into a duplicate."""
    clock = Clock()
    a = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="a",
                             lease_s=10, lease_grace_s=1)
    pre = effect_id(args={"code": "pre"})
    mid = effect_id(args={"code": "mid"})
    reserve(a, eid=pre, args={"code": "pre"})
    reserve(a, eid=mid, args={"code": "mid"})
    a.mark_executing(mid)
    clock.advance(500)

    b = DurableEffectJournal(tmp_path / "e.db", clock=clock, instance_id="b",
                             lease_s=10, lease_grace_s=1)
    report = b.startup_recovery()
    assert report["reclaimable_pre_effect"] == 1
    assert report["classified_indeterminate"] == 1
    assert b.get(pre).state is EffectState.RESERVED, "startup touched a P1 row"
    assert b.get(mid).state is EffectState.INDETERMINATE


def test_startup_recovery_is_bounded(tmp_path):
    """§50 — a large journal must not block boot behind a full scan."""
    clock = Clock()
    j = DurableEffectJournal(tmp_path / "e.db", clock=clock, lease_s=1,
                             lease_grace_s=0)
    for n in range(12):
        args = {"code": f"x{n}"}
        reserve(j, eid=effect_id(args=args), args=args)
    clock.advance(500)
    report = j.startup_recovery(limit=5)
    assert report["scanned"] == 5 and report["truncated"] is True


def test_status_reports_growth_without_a_payload(tmp_path):
    j = make_journal(tmp_path)
    args = {"code": SECRET_ARG}
    reserve(j, eid=effect_id(args=args), args=args)
    st = j.status()
    assert st["db_bytes"] > 0 and st["total"] == 1 and st["reserved"] == 1
    assert st["schema_version"] == SCHEMA_VERSION
    assert SECRET_ARG not in str(st)


def test_counters_are_body_safe_and_countable(tmp_path):
    """§51 — observability is counters, never arguments."""
    j = make_journal(tmp_path, instance_id="a")
    eid = effect_id()
    reserve(j, eid=eid)
    reserve(j, eid=eid)          # conflict
    j.mark_executing(eid)
    j.commit(eid, receipt={"ok": 1})
    reserve(j, eid=eid)          # durable dedupe
    c = j.counters
    assert c["ownership_wins"] == 1
    assert c["reservation_conflicts"] == 1
    assert c["durable_dedupe_hits"] == 1
    assert c["commits"] == 1
