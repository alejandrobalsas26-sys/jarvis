"""
tests/test_effect_semantics_v69_m65d.py — V69 M65D TRUTHFUL UNCERTAIN-EFFECT
SEMANTICS.

M65C proved the journal is a correct durable STORE. This file proves the thing
the store was missing: that JARVIS distinguishes

    "I observed an error"          (a fact about the software)

from

    "nothing happened"             (a claim about the world)

after the effect boundary has been durably entered.

The file is organised by the four axes M65D separated — execution phase, local
observation, external-effect knowledge and retry authority — and every section
that makes a safety claim exercises the pure decision function DIRECTLY as well
as end to end, so a green suite is evidence about the rule and not only about
one path through it.

The flagship regression (a real localhost server that applies the effect and
then loses the response) and the crash-window matrix live in
``test_uncertain_effect_live_v69_m65d.py``: a policy that is correct in one
process proves nothing about two.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.effect_journal import (
    SCHEMA_VERSION,
    DeclaredEffectOutcome,
    DurableEffectJournal,
    EffectDurabilityClass,
    EffectOutcomeEvidence,
    EffectState,
    ExecutionDisposition,
    ExternalOutcome,
    InvalidTransition,
    ReconciliationVerdict,
    ReservationOutcome,
    RetryAuthority,
    compute_effect_id,
    derive_idempotency_key,
    durability_class,
    external_outcome_of,
    may_auto_retry,
    register_durability,
    register_reconciler,
    retry_authority,
    unregister_durability,
)

from tools.executor import _IDEMPOTENCY_ARG

TOOL = "code_execute"
ARGS = {"code": "print(1)"}
SCOPE = "turn:m65d"
MCP_TOOL = "generar_laboratorio_red"
MCP_ARGS = {"tema": "vlan"}
EPOCH = "turn:m65d-protocol"
DEADLINE_S = 5.0

NON_REPLAYABLE = EffectDurabilityClass.NON_REPLAYABLE
RECONCILABLE = EffectDurabilityClass.RECONCILABLE
IDEMPOTENT = EffectDurabilityClass.IDEMPOTENT
IDEMPOTENT_KEY = EffectDurabilityClass.IDEMPOTENT_WITH_KEY
READ_ONLY = EffectDurabilityClass.READ_ONLY

EFFECTFUL_CLASSES = (NON_REPLAYABLE, RECONCILABLE, IDEMPOTENT, IDEMPOTENT_KEY)


class Clock:
    def __init__(self, start=None) -> None:
        self.now = start or datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_journal(tmp_path, name="effects.db", **kw) -> DurableEffectJournal:
    return DurableEffectJournal(tmp_path / name, **kw)


def effect_id(tool=TOOL, args=None, scope=SCOPE, surface="native") -> str:
    return compute_effect_id(surface=surface, tool_id=tool,
                             identity_scope=scope,
                             tool_input=ARGS if args is None else args)


def reserve(journal, *, eid=None, tool=TOOL, args=None, cls=NON_REPLAYABLE,
            surface="native", **kw):
    payload = ARGS if args is None else args
    return journal.reserve(
        effect_id=eid or effect_id(tool, payload, surface=surface),
        tool_id=tool, surface=surface, durability_class=cls,
        tool_input=payload, **kw)


def observed_failure(journal, *, cls=NON_REPLAYABLE, eid=None, evidence=None):
    """Drive one identity to FAILED_OBSERVED the way the executor does."""
    eid = eid or effect_id()
    reserve(journal, eid=eid, cls=cls)
    journal.mark_executing(eid)
    journal.fail_observed(eid, "tool_returned_error", evidence=evidence)
    return eid


class Harness:
    """The REAL ToolExecutor with a counting handler and a per-test journal.

    Same shape as the M65C protocol harness on purpose: the point of these
    tests is that the policy changed, not that the surface did.
    """

    def __init__(self, tmp_path, *, journal=None, instance_id="inst-a",
                 name="effects.db"):
        from tools.executor import ToolExecutor

        self.journal = journal if journal is not None else DurableEffectJournal(
            tmp_path / name, instance_id=instance_id)
        self.executor = ToolExecutor(journal=self.journal)
        self.executor.begin_effect_epoch(EPOCH)
        self.effects: dict[str, int] = {}
        self.mcp_calls: dict[str, int] = {}
        self.mcp_behaviour = None

    def add_tool(self, name=TOOL, result=None, *, fails=False, hook=None):
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

    async def mcp_call(self, tool_name, tool_input):
        self.mcp_calls[tool_name] = self.mcp_calls.get(tool_name, 0) + 1
        if self.mcp_behaviour is not None:
            return self.mcp_behaviour(self.mcp_calls[tool_name])
        return {"lab": "built"}

    def count(self, name=TOOL) -> int:
        return self.effects.get(name, 0)

    def mcp_count(self, name=MCP_TOOL) -> int:
        return self.mcp_calls.get(name, 0)

    def effect_id(self, tool=TOOL, args=None, surface="native"):
        return compute_effect_id(surface=surface, tool_id=tool,
                                 identity_scope=EPOCH,
                                 tool_input=ARGS if args is None else args)

    async def call(self, args=None, reasoning="caller", note=None):
        return await self.executor.aexecute(
            TOOL, dict(args or ARGS), reasoning, effect_note=note)

    async def call_mcp(self, args=None, note=None):
        return await self.executor.aexecute_mcp(
            MCP_TOOL, dict(args or MCP_ARGS), self.mcp_call, "caller",
            effect_note=note)


@pytest.fixture
def h(tmp_path, monkeypatch):
    from core.security_effects import SCOPES

    SCOPES.scopes = []

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    harness = Harness(tmp_path)

    async def _granted(tool_name, preview):
        return True, "test:granted"

    harness.executor._challenge = _granted
    return harness


# ══════════════════════════════════════════════════════════════════════════════
#  EPISTEMIC_MODEL — the four axes are four things (§7)
# ══════════════════════════════════════════════════════════════════════════════
def test_every_state_has_a_declared_external_outcome():
    """No state may be silent about the world; silence would be read as a guess."""
    from core.effect_journal import _STATE_OUTCOME

    assert set(_STATE_OUTCOME) == set(EffectState), (
        "a state was added without saying what it implies about the world")


def test_exactly_one_state_leaves_the_external_outcome_open():
    """The shape of the whole milestone.

    Everywhere else the execution phase and the knowledge coincide. Only AFTER
    the boundary, where a local error and a lost response are indistinguishable,
    do they come apart — so exactly one state consults the stored column, and a
    second one would mean a second place uncertainty could hide.
    """
    from core.effect_journal import _STATE_OUTCOME

    free = [s for s, v in _STATE_OUTCOME.items() if v is None]
    assert free == [EffectState.FAILED_OBSERVED], free


@pytest.mark.parametrize("state,expected", [
    (EffectState.RESERVED, ExternalOutcome.PROVEN_NOT_EXECUTED),
    (EffectState.EXECUTING, ExternalOutcome.UNKNOWN),
    (EffectState.COMMITTED, ExternalOutcome.PROVEN_COMMITTED),
    (EffectState.FAILED_BEFORE_EFFECT, ExternalOutcome.PROVEN_NOT_EXECUTED),
    (EffectState.INDETERMINATE, ExternalOutcome.UNKNOWN),
    (EffectState.RECONCILED_COMMITTED, ExternalOutcome.PROVEN_COMMITTED),
    (EffectState.RECONCILED_NOT_EXECUTED, ExternalOutcome.PROVEN_NOT_EXECUTED),
])
def test_a_settled_state_ignores_whatever_the_column_says(state, expected):
    """A stored value can never contradict a state that already settles it.

    Defence against the most direct attack on the design: writing
    PROVEN_NOT_EXECUTED into the column of a COMMITTED row and asking for a
    retry.
    """
    for recorded in ExternalOutcome:
        assert external_outcome_of(state, recorded) is expected


def test_an_unreadable_stored_outcome_reads_as_unknown():
    """Fail-closed. Garbage costs a reconciliation, not a duplicate."""
    for junk in (None, "", "PROVEN", "proven_not_executed", 7, object()):
        assert external_outcome_of(EffectState.FAILED_OBSERVED,
                                   junk) is ExternalOutcome.UNKNOWN


def test_the_observed_failure_state_reads_its_recorded_outcome():
    for recorded in ExternalOutcome:
        assert external_outcome_of(EffectState.FAILED_OBSERVED,
                                   recorded) is recorded


# ══════════════════════════════════════════════════════════════════════════════
#  TRUTH_TABLE — §8, exercised on the pure function
# ══════════════════════════════════════════════════════════════════════════════
#: (state, recorded_outcome, class, verdict) -> authority.
#: Written out rather than computed, so a mutation to the implementation cannot
#: also mutate the expectation.
_TRUTH_TABLE = [
    # CASE 1 — failure before EXECUTING is durable: proven not started.
    (EffectState.FAILED_BEFORE_EFFECT, None, NON_REPLAYABLE, None,
     RetryAuthority.SAFE_TO_RETRY),
    (EffectState.FAILED_BEFORE_EFFECT, None, RECONCILABLE, None,
     RetryAuthority.SAFE_TO_RETRY),
    (EffectState.RESERVED, None, NON_REPLAYABLE, None,
     RetryAuthority.SAFE_TO_RETRY),

    # CASE 2-6 — anything generic after EXECUTING: UNKNOWN, and the class decides.
    (EffectState.FAILED_OBSERVED, ExternalOutcome.UNKNOWN, NON_REPLAYABLE, None,
     RetryAuthority.BLOCKED_INDETERMINATE),
    (EffectState.FAILED_OBSERVED, ExternalOutcome.UNKNOWN, RECONCILABLE, None,
     RetryAuthority.REQUIRES_RECONCILIATION),
    (EffectState.FAILED_OBSERVED, ExternalOutcome.UNKNOWN, IDEMPOTENT, None,
     RetryAuthority.REPLAY_SAFE_BY_CONTRACT),
    (EffectState.FAILED_OBSERVED, ExternalOutcome.UNKNOWN, IDEMPOTENT_KEY, None,
     RetryAuthority.REPLAY_SAFE_BY_CONTRACT),
    (EffectState.EXECUTING, None, NON_REPLAYABLE, None,
     RetryAuthority.BLOCKED_INDETERMINATE),
    (EffectState.EXECUTING, None, RECONCILABLE, None,
     RetryAuthority.REQUIRES_RECONCILIATION),
    (EffectState.EXECUTING, None, IDEMPOTENT, None,
     RetryAuthority.REPLAY_SAFE_BY_CONTRACT),
    (EffectState.INDETERMINATE, None, NON_REPLAYABLE, None,
     RetryAuthority.BLOCKED_INDETERMINATE),
    (EffectState.INDETERMINATE, None, IDEMPOTENT_KEY, None,
     RetryAuthority.REPLAY_SAFE_BY_CONTRACT),

    # CASE 7 — explicit authoritative no-effect evidence.
    (EffectState.FAILED_OBSERVED, ExternalOutcome.PROVEN_NOT_EXECUTED,
     NON_REPLAYABLE, None, RetryAuthority.SAFE_TO_RETRY),

    # CASE 8 — explicit committed evidence outranks every class.
    (EffectState.FAILED_OBSERVED, ExternalOutcome.PROVEN_COMMITTED,
     NON_REPLAYABLE, None, RetryAuthority.ALREADY_COMMITTED),
    (EffectState.FAILED_OBSERVED, ExternalOutcome.PROVEN_COMMITTED,
     IDEMPOTENT, None, RetryAuthority.ALREADY_COMMITTED),
    (EffectState.COMMITTED, None, IDEMPOTENT, None,
     RetryAuthority.ALREADY_COMMITTED),

    # CASE 9 — reconciliation says committed.
    (EffectState.INDETERMINATE, None, RECONCILABLE,
     ReconciliationVerdict.CONFIRMED_COMMITTED, RetryAuthority.ALREADY_COMMITTED),
    # CASE 10 — reconciliation says not executed.
    (EffectState.INDETERMINATE, None, RECONCILABLE,
     ReconciliationVerdict.CONFIRMED_NOT_EXECUTED, RetryAuthority.SAFE_TO_RETRY),
    # CASE 11 — reconciliation says unknown: asked and unanswered is not unasked.
    (EffectState.INDETERMINATE, None, RECONCILABLE,
     ReconciliationVerdict.UNKNOWN, RetryAuthority.BLOCKED_INDETERMINATE),
    (EffectState.FAILED_OBSERVED, ExternalOutcome.UNKNOWN, RECONCILABLE,
     ReconciliationVerdict.UNKNOWN, RetryAuthority.BLOCKED_INDETERMINATE),
    # ...but a durability contract still speaks over an unhelpful reconciler.
    (EffectState.INDETERMINATE, None, IDEMPOTENT,
     ReconciliationVerdict.UNKNOWN, RetryAuthority.REPLAY_SAFE_BY_CONTRACT),

    # READ_ONLY: no external effect exists by contract (§9), kept separate.
    (EffectState.EXECUTING, None, READ_ONLY, None, RetryAuthority.SAFE_TO_RETRY),
]


@pytest.mark.parametrize("state,recorded,cls,verdict,expected", _TRUTH_TABLE)
def test_the_retry_authority_truth_table(state, recorded, cls, verdict, expected):
    assert retry_authority(state=state, durability_class=cls,
                           recorded_outcome=recorded, verdict=verdict) is expected


def test_the_decision_is_total_over_every_combination():
    """No input reaches a fall-through. A missing branch would return None."""
    verdicts = (None, *ReconciliationVerdict)
    for state in EffectState:
        for cls in EffectDurabilityClass:
            for recorded in (None, *ExternalOutcome):
                for verdict in verdicts:
                    got = retry_authority(state=state, durability_class=cls,
                                          recorded_outcome=recorded,
                                          verdict=verdict)
                    assert isinstance(got, RetryAuthority), (
                        state, cls, recorded, verdict, got)


def test_an_unknown_outcome_never_authorises_a_non_replayable_execution():
    """The single load-bearing property, over every way of reaching UNKNOWN."""
    for state in EffectState:
        for verdict in (None, *ReconciliationVerdict):
            if external_outcome_of(state) is not ExternalOutcome.UNKNOWN:
                continue
            if verdict in (ReconciliationVerdict.CONFIRMED_COMMITTED,
                           ReconciliationVerdict.CONFIRMED_NOT_EXECUTED):
                continue
            got = retry_authority(state=state, durability_class=NON_REPLAYABLE,
                                  verdict=verdict)
            assert got is RetryAuthority.BLOCKED_INDETERMINATE, (state, verdict)


def test_may_auto_retry_and_the_authority_table_cannot_disagree():
    """M65C carried two policies and the live one was the unsound one."""
    from core.effect_journal import _MAY_EXECUTE_AGAIN

    for state in EffectState:
        for cls in EffectDurabilityClass:
            for recorded in (None, *ExternalOutcome):
                expected = retry_authority(
                    state=state, durability_class=cls,
                    recorded_outcome=recorded) in _MAY_EXECUTE_AGAIN
                assert may_auto_retry(state, cls, recorded) is expected


def test_a_lease_expiry_is_not_an_input_to_the_decision():
    """§22 — lease expiration is NEVER evidence that the effect did not happen.

    Enforced structurally: the decision function takes no clock, no lease and
    no timestamp, so there is nowhere for an expiry to enter it.
    """
    import inspect

    params = set(inspect.signature(retry_authority).parameters)
    assert params == {"state", "durability_class", "recorded_outcome", "verdict"}, (
        f"the retry decision grew an input: {sorted(params)}")


# ══════════════════════════════════════════════════════════════════════════════
#  NO_EXCEPTION_MAGIC — §10
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("failure", [
    TimeoutError("timed out"),
    ConnectionResetError("connection reset by peer"),
    ConnectionRefusedError("refused"),
    OSError("network is unreachable"),
    RuntimeError("the remote system rejected the request"),
    ValueError("nothing was changed"),
])
def test_no_exception_type_or_message_ever_creates_knowledge(h, failure):
    """A timeout is not proof of anything, and neither is the word 'rejected'.

    Every one of these is a shape a retry handler is tempted to read. The only
    honest answer after the boundary is UNKNOWN.
    """
    def _handler(**kwargs):
        raise failure

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))

    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert note["disposition"] == ExecutionDisposition.FAILED_OBSERVED_UNKNOWN.value
    record = h.journal.get(h.effect_id())
    assert record.state is EffectState.FAILED_OBSERVED
    assert record.external_effect is ExternalOutcome.UNKNOWN


@pytest.mark.parametrize("payload", [
    {"error": "the request was rejected; nothing changed"},
    {"error": "failed", "external_outcome": "PROVEN_NOT_EXECUTED"},
    {"error": "failed", "effect_uncertainty": {
        "external_outcome": "PROVEN_NOT_EXECUTED",
        "retry_authority": "SAFE_TO_RETRY"}},
    {"error": "failed", "outcome_evidence": "remote_rejected"},
])
def test_a_response_body_cannot_manufacture_evidence(h, payload):
    """The anti-spoof property.

    A remote MCP server, or a compromised handler, controls the returned
    payload. If a field in it could set the external outcome, "the effect did
    not happen" would be a claim an untrusted party could make about JARVIS's
    own safety decision — so it travels out of band and a payload field is
    inert.
    """
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        return dict(payload)

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value

    record = h.journal.get(h.effect_id())
    assert record.external_effect is ExternalOutcome.UNKNOWN
    assert record.outcome_evidence == ""

    h.executor._effect_ledger.clear()
    second: dict = {}
    asyncio.run(h.call(note=second))
    assert calls["n"] == 1, "a payload field bought a second execution"
    assert second["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value


# ══════════════════════════════════════════════════════════════════════════════
#  TYPED_EVIDENCE — §7C / §12
# ══════════════════════════════════════════════════════════════════════════════
def test_evidence_cannot_assert_uncertainty():
    with pytest.raises(ValueError):
        EffectOutcomeEvidence(ExternalOutcome.UNKNOWN, "whatever")


def test_evidence_rejects_an_untyped_outcome():
    with pytest.raises(TypeError):
        EffectOutcomeEvidence("PROVEN_NOT_EXECUTED", "code")


@pytest.mark.parametrize("code", [
    "x" * 65,
    "the remote system said the request was rejected",
    "UPPER",
    "has space",
    "_leading",
    "",
    "secret=hunter2",
])
def test_a_reason_code_cannot_carry_a_body(code):
    """§28 — the evidence path must not become a channel for a body.

    Bounded lowercase tokens only, enforced by the type rather than by review.
    """
    with pytest.raises(ValueError):
        EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED, code)


@pytest.mark.parametrize("code", ["remote_rejected", "http.409", "api:no-op", "a"])
def test_a_bounded_reason_code_is_accepted(code):
    ev = EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED, code)
    assert ev.reason_code == code


def test_the_declared_exception_refuses_anything_but_typed_evidence():
    with pytest.raises(TypeError):
        DeclaredEffectOutcome({"outcome": "PROVEN_NOT_EXECUTED"})


def test_evidence_cannot_be_declared_before_the_boundary():
    """Evidence about an effect that provably never started is meaningless."""
    from tools.executor import _EffectHooks

    hooks = _EffectHooks(None)
    with pytest.raises(RuntimeError):
        hooks.declare_outcome(
            EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED, "x"))


def test_the_journal_refuses_untyped_evidence(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    with pytest.raises(TypeError):
        j.fail_observed(eid, "boom", evidence="PROVEN_NOT_EXECUTED")


def test_typed_no_effect_evidence_unblocks_a_non_replayable_retry(h):
    """CASE 7. The ONLY route from a post-boundary failure to a safe retry
    that does not go through a durability contract or a reconciler."""
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeclaredEffectOutcome(
                EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED,
                                      "remote_rejected_before_apply"),
                "the remote system rejected the request")
        return {"stdout": "ok"}

    h.executor._tool_code_execute = _handler
    first: dict = {}
    asyncio.run(h.call(note=first))
    assert first["external_outcome"] == ExternalOutcome.PROVEN_NOT_EXECUTED.value
    assert first["disposition"] == (
        ExecutionDisposition.FAILED_OBSERVED_NOT_EXECUTED.value)
    assert h.journal.get(h.effect_id()).outcome_evidence == \
        "remote_rejected_before_apply"

    h.executor._effect_ledger.clear()
    second: dict = {}
    result = asyncio.run(h.call(note=second))
    assert calls["n"] == 2
    assert result == {"stdout": "ok"}
    assert second["disposition"] == ExecutionDisposition.EXECUTED_NOW.value


def test_typed_committed_evidence_deduplicates_instead_of_replaying(h):
    """CASE 8 — the effect exists, the result body does not."""
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        raise DeclaredEffectOutcome(
            EffectOutcomeEvidence(ExternalOutcome.PROVEN_COMMITTED,
                                  "signed_receipt_seen"),
            "the effect was applied but the response was not usable")

    h.executor._tool_code_execute = _handler
    first: dict = {}
    asyncio.run(h.call(note=first))
    assert first["disposition"] == (
        ExecutionDisposition.FAILED_OBSERVED_COMMITTED.value)
    record = h.journal.get(h.effect_id())
    assert record.proven_committed is True
    assert record.state is EffectState.FAILED_OBSERVED

    h.executor._effect_ledger.clear()
    second: dict = {}
    result = asyncio.run(h.call(note=second))
    assert calls["n"] == 1, "a proven-committed effect was executed again"
    assert second["disposition"] == ExecutionDisposition.RECOVERED_COMMITTED.value
    assert result["status"] == "recovered"


def test_committed_evidence_outranks_a_replayable_contract(h):
    """An IDEMPOTENT tool proven to have landed has nothing to gain from a
    replay, and a second external call made for no reason is still a second
    external call."""
    register_durability(TOOL, IDEMPOTENT)
    try:
        calls = {"n": 0}

        def _handler(**kwargs):
            calls["n"] += 1
            raise DeclaredEffectOutcome(
                EffectOutcomeEvidence(ExternalOutcome.PROVEN_COMMITTED, "receipt"))

        h.executor._tool_code_execute = _handler
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())
        assert calls["n"] == 1
    finally:
        unregister_durability(TOOL)


def test_a_new_attempt_never_inherits_the_previous_attempts_evidence(tmp_path):
    """A durable row must not lend a later attempt a fact it did not establish."""
    j = make_journal(tmp_path)
    eid = observed_failure(
        j, evidence=EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED,
                                          "proven"))
    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert again.owned is True
    assert again.record.external_outcome == ExternalOutcome.UNKNOWN.value
    assert again.record.outcome_evidence == ""


# ══════════════════════════════════════════════════════════════════════════════
#  FAILED_OBSERVED — §11, at the store
# ══════════════════════════════════════════════════════════════════════════════
def test_an_observed_failure_defaults_to_an_unknown_outcome(tmp_path):
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    record = j.get(eid)
    assert record.state is EffectState.FAILED_OBSERVED
    assert record.external_effect is ExternalOutcome.UNKNOWN
    assert record.proven_committed is False


@pytest.mark.parametrize("cls,outcome,owned", [
    (NON_REPLAYABLE, ReservationOutcome.INDETERMINATE, False),
    (RECONCILABLE, ReservationOutcome.RECONCILE_REQUIRED, False),
    (IDEMPOTENT, ReservationOutcome.RECLAIMED, True),
    (IDEMPOTENT_KEY, ReservationOutcome.RECLAIMED, True),
])
def test_the_durability_class_decides_what_follows_an_observed_failure(
        tmp_path, cls, outcome, owned):
    """§9, the matrix, at the reservation boundary."""
    j = make_journal(tmp_path)
    eid = observed_failure(j, cls=cls)
    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=cls, tool_input=ARGS)
    assert again.outcome is outcome
    assert again.owned is owned


def test_a_blocked_observed_failure_becomes_visible_as_indeterminate(tmp_path):
    """Uncertainty gets ONE home. A second one would be a second thing to
    keep correct, and the reconciliation machinery only looks in one place."""
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
              durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert j.get(eid).state is EffectState.INDETERMINATE
    assert [r.effect_id for r in j.indeterminate_effects()] == [eid]


def test_asking_again_is_never_evidence(tmp_path):
    """Ten callers asking is not one caller proving."""
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    for _ in range(10):
        again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                          durability_class=NON_REPLAYABLE, tool_input=ARGS)
        assert again.owned is False


def test_taking_over_an_uncertain_observed_failure_is_refused(tmp_path):
    """Defence in depth: the guard is derived from the decision, so a caller
    that reaches the helper directly is refused too.

    Under M65C the guard compared `frm is EXECUTING`, which is exactly why a
    FAILED_OBSERVED caller walked past it.
    """
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    record = j.get(eid)
    with pytest.raises(InvalidTransition):
        j._take_over(record, EffectState.FAILED_OBSERVED, "2026-09-10T12:00:00+00:00",
                     "2026-09-10T12:15:00+00:00", "", "", "",
                     ReservationOutcome.OWNED, "forced")


def test_taking_over_an_executing_non_replayable_effect_is_refused(tmp_path):
    """The M65C guard's own property, preserved."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    record = j.get(eid)
    with pytest.raises(InvalidTransition):
        j._take_over(record, EffectState.EXECUTING, "2026-09-10T12:00:00+00:00",
                     "2026-09-10T12:15:00+00:00", "", "", "",
                     ReservationOutcome.RECLAIMED, "forced")


# ══════════════════════════════════════════════════════════════════════════════
#  STATE_MACHINE — §25
# ══════════════════════════════════════════════════════════════════════════════
def test_there_is_no_edge_from_executing_to_a_proven_no_effect_state():
    """Once EXECUTING is durable, nothing local can prove the effect did not
    start, so the edge that would say otherwise does not exist."""
    from core.effect_journal import _ALLOWED_EDGES

    forbidden = {EffectState.FAILED_BEFORE_EFFECT,
                 EffectState.RECONCILED_NOT_EXECUTED}
    for frm, to in _ALLOWED_EDGES:
        if frm is EffectState.EXECUTING:
            assert to not in forbidden, f"EXECUTING -> {to.value} exists"


def test_the_journal_refuses_the_edge_at_runtime(tmp_path):
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    with pytest.raises(InvalidTransition):
        j.fail_before_effect(eid, "pretending nothing happened")


def test_every_post_boundary_state_leaves_only_through_evidence_or_contract():
    """A structural reading of the edge set.

    Any edge OUT of a state whose external outcome is UNKNOWN, and INTO a state
    a caller could execute from, must be one `_take_over` guards.
    """
    from core.effect_journal import _ALLOWED_EDGES

    executable = {EffectState.RESERVED, EffectState.EXECUTING}
    for frm, to in _ALLOWED_EDGES:
        if frm is to or to not in executable:
            continue
        if external_outcome_of(frm) is not ExternalOutcome.UNKNOWN:
            continue
        assert retry_authority(state=frm, durability_class=IDEMPOTENT) is \
            RetryAuthority.REPLAY_SAFE_BY_CONTRACT, (frm, to)
        assert retry_authority(state=frm, durability_class=NON_REPLAYABLE) is \
            RetryAuthority.BLOCKED_INDETERMINATE, (frm, to)


# ══════════════════════════════════════════════════════════════════════════════
#  OLD_JOURNAL_COMPATIBILITY — §17
# ══════════════════════════════════════════════════════════════════════════════
#: The v1 (M65C) table, verbatim. Written out rather than imported, because
#: importing it from the current module would make this test agree with
#: whatever the schema happens to be.
_V1_EFFECTS_DDL = """CREATE TABLE effects (
    effect_id               TEXT PRIMARY KEY,
    schema_version          INTEGER NOT NULL,
    tool_id                 TEXT NOT NULL,
    surface                 TEXT NOT NULL,
    durability_class        TEXT NOT NULL,
    canonical_action_digest TEXT NOT NULL,
    canonical_args_digest   TEXT NOT NULL,
    authority_digest        TEXT NOT NULL DEFAULT '',
    scope_digest            TEXT NOT NULL DEFAULT '',
    approval_digest         TEXT NOT NULL DEFAULT '',
    plan_id                 TEXT NOT NULL DEFAULT '',
    task_id                 TEXT NOT NULL DEFAULT '',
    idempotency_key         TEXT NOT NULL DEFAULT '',
    owner_instance_id       TEXT NOT NULL,
    owner_attempt           INTEGER NOT NULL,
    state                   TEXT NOT NULL,
    reservation_created_at  TEXT NOT NULL,
    lease_expires_at        TEXT NOT NULL,
    state_changed_at        TEXT NOT NULL,
    committed_at            TEXT NOT NULL DEFAULT '',
    receipt_digest          TEXT NOT NULL DEFAULT '',
    failure_class           TEXT NOT NULL DEFAULT '',
    recovery_note           TEXT NOT NULL DEFAULT ''
)"""

_V1_TRANSITIONS_DDL = """CREATE TABLE transitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id         TEXT NOT NULL REFERENCES effects(effect_id) ON DELETE CASCADE,
    from_state        TEXT NOT NULL,
    to_state          TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL,
    owner_attempt     INTEGER NOT NULL,
    at                TEXT NOT NULL,
    note              TEXT NOT NULL DEFAULT ''
)"""


def write_m65c_journal(path, *, eid, state="FAILED_OBSERVED",
                       cls=NON_REPLAYABLE):
    """A REAL v1 journal, built with the M65C DDL and no M65D code."""
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute(_V1_EFFECTS_DDL)
    db.execute(_V1_TRANSITIONS_DDL)
    db.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    db.execute(
        "INSERT INTO effects(effect_id, schema_version, tool_id, surface, "
        "durability_class, canonical_action_digest, canonical_args_digest, "
        "idempotency_key, owner_instance_id, owner_attempt, state, "
        "reservation_created_at, lease_expires_at, state_changed_at, "
        "failure_class, recovery_note) "
        "VALUES(?,1,?,?,?,'act','args',?,?,1,?,?,?,?,?,?)",
        (eid, TOOL, "native", cls.value, derive_idempotency_key(eid),
         "inst-m65c", state, "2026-09-04T12:00:00+00:00",
         "2026-09-04T12:15:00+00:00", "2026-09-04T12:00:30+00:00",
         "tool_returned_error", "the tool returned an error to a live caller"))
    db.execute(
        "INSERT INTO transitions(effect_id, from_state, to_state, "
        "owner_instance_id, owner_attempt, at, note) "
        "VALUES(?,'EXECUTING',?,?,1,?,'observed failure')",
        (eid, state, "inst-m65c", "2026-09-04T12:00:30+00:00"))
    db.commit()
    db.close()


def test_an_m65c_journal_migrates_forward_without_losing_anything(tmp_path):
    path = tmp_path / "m65c.db"
    eid = effect_id()
    write_m65c_journal(path, eid=eid)

    j = DurableEffectJournal(path)
    assert int(j._db.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]) == SCHEMA_VERSION

    record = j.get(eid)
    assert record is not None, "the migration lost a row"
    assert record.state is EffectState.FAILED_OBSERVED
    assert record.failure_class == "tool_returned_error"
    assert record.owner_attempt == 1
    assert len(j.transitions(eid)) == 1, "the migration lost transition history"


def test_a_historical_observed_failure_is_never_a_free_retry(tmp_path):
    """THE §17 property.

    These rows were written by code that treated them as proven harmless. They
    read as UNKNOWN after the upgrade, because the migration's constant default
    makes historical rows MORE conservative without needing to know anything
    about them.
    """
    path = tmp_path / "m65c.db"
    eid = effect_id()
    write_m65c_journal(path, eid=eid)

    j = DurableEffectJournal(path)
    assert j.get(eid).external_effect is ExternalOutcome.UNKNOWN
    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert again.outcome is ReservationOutcome.INDETERMINATE
    assert again.owned is False


def test_a_historical_committed_row_still_deduplicates(tmp_path):
    """The migration must not disturb what M65C already got right."""
    path = tmp_path / "m65c.db"
    eid = effect_id()
    write_m65c_journal(path, eid=eid, state="COMMITTED")
    j = DurableEffectJournal(path)
    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert again.outcome is ReservationOutcome.ALREADY_COMMITTED


def test_a_newer_schema_is_still_refused(tmp_path):
    """§27 — forward compatibility by guessing is how an INDETERMINATE effect
    gets read as a safe retry."""
    from core.effect_journal import JournalUnhealthy

    path = tmp_path / "future.db"
    j = make_journal(tmp_path, name="future.db")
    j._db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    j.close()
    with pytest.raises(JournalUnhealthy):
        DurableEffectJournal(path)


def test_a_missing_migration_step_refuses_rather_than_guessing(tmp_path, monkeypatch):
    from core.effect_journal import JournalUnhealthy

    path = tmp_path / "m65c.db"
    write_m65c_journal(path, eid=effect_id())
    monkeypatch.setattr("core.effect_journal._MIGRATIONS", {})
    with pytest.raises(JournalUnhealthy):
        DurableEffectJournal(path)


def test_a_failed_migration_leaves_the_journal_at_its_old_version(tmp_path):
    """Transactional. A half-migrated file is the one thing worse than an old one."""
    from core.effect_journal import JournalUnhealthy

    path = tmp_path / "m65c.db"
    eid = effect_id()
    write_m65c_journal(path, eid=eid)

    def _explode(db):
        db.execute("ALTER TABLE effects ADD COLUMN external_outcome "
                   "TEXT NOT NULL DEFAULT 'UNKNOWN'")
        db.execute("ALTER TABLE nonexistent ADD COLUMN x TEXT")

    import core.effect_journal as mod
    original = dict(mod._MIGRATIONS)
    mod._MIGRATIONS.clear()
    mod._MIGRATIONS[1] = _explode
    try:
        with pytest.raises(JournalUnhealthy):
            DurableEffectJournal(path)
    finally:
        mod._MIGRATIONS.clear()
        mod._MIGRATIONS.update(original)

    db = sqlite3.connect(str(path))
    version = db.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    rows = db.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
    db.close()
    assert version == "1", "a failed migration bumped the version anyway"
    assert rows == 1, "a failed migration lost data"


# ══════════════════════════════════════════════════════════════════════════════
#  BODY_SAFE — §28
# ══════════════════════════════════════════════════════════════════════════════
def test_the_new_columns_never_contain_a_body(tmp_path):
    secret = "hunter2-CORRECT-HORSE-BATTERY"
    j = make_journal(tmp_path)
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE,
                            tool_input={"code": secret})
    j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
              durability_class=NON_REPLAYABLE, tool_input={"code": secret})
    j.mark_executing(eid)
    j.fail_observed(eid, "tool_returned_error",
                    evidence=EffectOutcomeEvidence(
                        ExternalOutcome.PROVEN_NOT_EXECUTED, "remote_rejected"))
    j.close()

    blob = (tmp_path / "effects.db").read_bytes()
    for side in ("-wal", "-shm"):
        p = tmp_path / f"effects.db{side}"
        if p.exists():
            blob += p.read_bytes()
    assert secret.encode() not in blob, "the journal file contains a body"


def test_the_record_dict_stays_body_free(tmp_path):
    j = make_journal(tmp_path)
    eid = observed_failure(
        j, evidence=EffectOutcomeEvidence(ExternalOutcome.PROVEN_COMMITTED,
                                          "signed_receipt"))
    d = j.get(eid).to_dict()
    assert d["external_outcome"] == ExternalOutcome.PROVEN_COMMITTED.value
    assert d["outcome_evidence"] == "signed_receipt"
    assert all(isinstance(v, (str, int, bool)) for v in d.values())


def test_the_uncertainty_block_carries_no_body(h):
    secret = "s3cr3t-token-value"

    def _handler(**kwargs):
        raise RuntimeError(f"failed talking to the API with {secret}")

    h.executor._tool_code_execute = _handler
    result = asyncio.run(h.call(args={"code": secret}))
    block = result["effect_uncertainty"]
    import json as _json
    assert secret not in _json.dumps(block)
    assert set(block) == {"effect_id", "durability_class", "external_outcome",
                          "retry_authority", "reconciliation_available", "detail"}


# ══════════════════════════════════════════════════════════════════════════════
#  OBSERVABILITY — §32
# ══════════════════════════════════════════════════════════════════════════════
def test_the_counters_name_each_meaningful_outcome(tmp_path):
    j = make_journal(tmp_path)
    observed_failure(j, eid=effect_id(args={"code": "a"}))
    observed_failure(j, eid=effect_id(args={"code": "b"}),
                     evidence=EffectOutcomeEvidence(
                         ExternalOutcome.PROVEN_NOT_EXECUTED, "proven"))
    observed_failure(j, eid=effect_id(args={"code": "c"}),
                     evidence=EffectOutcomeEvidence(
                         ExternalOutcome.PROVEN_COMMITTED, "receipt"))
    c = j.counters
    assert c["failed_observed"] == 3
    assert c["failed_observed_unknown"] == 1
    assert c["failed_observed_proven_not_executed"] == 1
    assert c["failed_observed_proven_committed"] == 1
    assert all(isinstance(v, int) for v in c.values())


def test_a_blocked_observed_failure_is_counted(tmp_path):
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
              durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert j.counters["failed_observed_blocked"] == 1


def test_status_reports_uncertain_effects_as_work_to_do(tmp_path):
    j = make_journal(tmp_path)
    assert j.status()["recovery_required"] is False
    observed_failure(j)
    status = j.status()
    assert status["uncertain_observed"] == 1
    assert status["failed_observed"] == 1
    assert status["recovery_required"] is True


def test_a_replayable_observed_failure_is_not_operator_work(tmp_path):
    """An IDEMPOTENT ambiguity resolves itself on the next call."""
    j = make_journal(tmp_path)
    observed_failure(j, cls=IDEMPOTENT)
    assert j.status()["uncertain_observed"] == 0
    assert j.status()["recovery_required"] is False


def test_startup_recovery_reports_but_never_resolves(tmp_path):
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    report = j.startup_recovery()
    assert report["uncertain_observed"] == 1
    assert j.get(eid).state is EffectState.FAILED_OBSERVED, (
        "a boot changed the meaning of a row nobody asked about")


def test_the_disposition_tables_cannot_drift_from_the_enums():
    """The executor keeps two string-keyed tables so it can import the journal
    lazily. Strings drift silently; this re-derives both sides."""
    from tools.executor import (
        _OBSERVED_FAILURE_DETAIL, _OBSERVED_FAILURE_DISPOSITION,
    )

    assert set(_OBSERVED_FAILURE_DISPOSITION) == {o.value for o in ExternalOutcome}
    assert set(_OBSERVED_FAILURE_DETAIL) == {o.value for o in ExternalOutcome}
    for value in _OBSERVED_FAILURE_DISPOSITION.values():
        assert ExecutionDisposition(value)


# ══════════════════════════════════════════════════════════════════════════════
#  PRODUCTION_INVENTORY — §29
# ══════════════════════════════════════════════════════════════════════════════
def test_no_production_tool_was_reclassified_by_this_milestone():
    """M65D is allowed to leave every production tool NON_REPLAYABLE, and does.

    A milestone about truthful uncertainty must not pay for its own tests by
    declaring a real tool replayable. The audited table is pinned.
    """
    from core.effect_journal import _TOOL_DURABILITY

    assert _TOOL_DURABILITY == {"set_clipboard": EffectDurabilityClass.IDEMPOTENT}


def test_an_unclassified_effectful_tool_is_non_replayable():
    from core.risk_classes import RiskClass, classify_tool

    for tool in ("kill_process", "network_scan", "write_file", "http_request",
                 "network_quarantine", "desplegar_webapp"):
        assert classify_tool(tool) is not RiskClass.READ_ONLY
        assert durability_class(tool, classify_tool(tool)) is NON_REPLAYABLE


def test_no_production_tool_declares_a_reconciler():
    """Both protocols are proven against test-owned synthetic tools only."""
    from core.effect_journal import _RECONCILERS

    assert _RECONCILERS == {}


# ══════════════════════════════════════════════════════════════════════════════
#  DURABILITY_MATRIX — §9, end to end through the real protocol
# ══════════════════════════════════════════════════════════════════════════════
def failing_once(calls, exc=None):
    """A handler that fails the first time and succeeds after."""
    def _handler(**kwargs):
        calls["n"] = calls.get("n", 0) + 1
        if calls["n"] == 1:
            if exc is not None:
                raise exc
            return {"error": "lost the response"}
        return {"stdout": "second"}
    return _handler


def test_non_replayable_ambiguity_blocks_and_says_why(h):
    """§22 — the fail-closed default, and the operator message it produces."""
    calls: dict = {}
    h.executor._tool_code_execute = failing_once(calls)
    asyncio.run(h.call())
    h.executor._effect_ledger.clear()

    note: dict = {}
    result = asyncio.run(h.call(note=note))
    assert calls["n"] == 1
    assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    assert note["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value
    assert note["reconciliation_required"] is False
    # §31 — the message names the identity, the uncertainty, the class and
    # whether anything other than a human can settle it. Never "Retrying".
    assert result["error_class"] == "indeterminate_effect"
    assert result["effect_id"] == h.effect_id()
    assert result["durability_class"] == NON_REPLAYABLE.value
    assert result["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert result["reconciliation_available"] is False
    assert "no se puede probar" in result["error"]
    assert NON_REPLAYABLE.value in result["error"]
    assert "retry" not in result["error"].lower()


def test_the_uncertainty_block_reaches_the_caller_on_the_failing_call(h):
    """§16 — the caller must be able to tell "did nothing" from "cannot say"
    on the call that failed, not only on the one that is refused afterwards."""
    def _handler(**kwargs):
        raise TimeoutError("no response")

    h.executor._tool_code_execute = _handler
    result = asyncio.run(h.call())
    block = result["effect_uncertainty"]
    assert block["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert block["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value
    assert block["reconciliation_available"] is False
    assert "cannot prove" in block["detail"]


def test_a_pre_effect_refusal_still_says_it_did_nothing(h):
    """The distinction has to cut both ways or it is not a distinction."""
    async def _refused(tool_name, preview):
        return False, "test:denied"

    h.add_tool()
    h.executor._challenge = _refused
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert h.count() == 0
    assert note["disposition"] == ExecutionDisposition.FAILED_BEFORE_EFFECT.value
    assert note["external_outcome"] == ExternalOutcome.PROVEN_NOT_EXECUTED.value
    assert note["retry_authority"] == RetryAuthority.SAFE_TO_RETRY.value
    assert h.journal.get(h.effect_id()).state is EffectState.FAILED_BEFORE_EFFECT

    # ...and the identity is not poisoned.
    h.executor._effect_ledger.clear()

    async def _granted(tool_name, preview):
        return True, "test:granted"

    h.executor._challenge = _granted
    asyncio.run(h.call())
    assert h.count() == 1


def test_a_successful_call_is_proven_committed(h):
    h.add_tool()
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["external_outcome"] == ExternalOutcome.PROVEN_COMMITTED.value
    assert note["retry_authority"] == RetryAuthority.ALREADY_COMMITTED.value


# ══════════════════════════════════════════════════════════════════════════════
#  IDEMPOTENT — §20
# ══════════════════════════════════════════════════════════════════════════════
def test_an_idempotent_tool_replays_after_an_ambiguous_failure(h):
    """Permission comes from the CONTRACT. The ambiguity is unchanged."""
    register_durability(TOOL, IDEMPOTENT)
    try:
        calls: dict = {}
        h.executor._tool_code_execute = failing_once(calls, TimeoutError("lost"))
        note1: dict = {}
        asyncio.run(h.call(note=note1))
        assert note1["external_outcome"] == ExternalOutcome.UNKNOWN.value

        h.executor._effect_ledger.clear()
        note2: dict = {}
        result = asyncio.run(h.call(note=note2))
        assert calls["n"] == 2
        assert result == {"stdout": "second"}
        assert note2["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    finally:
        unregister_durability(TOOL)


def test_an_idempotent_replay_converges_to_one_external_state(h):
    """The contract is 'repeating converges', so the fixture must converge."""
    register_durability(TOOL, IDEMPOTENT)
    try:
        world = {"value": None, "writes": 0}

        def _handler(**kwargs):
            world["writes"] += 1
            world["value"] = kwargs.get("code")       # a SET, not an append
            if world["writes"] == 1:
                raise ConnectionResetError("response lost")
            return {"stdout": "ok"}

        h.executor._tool_code_execute = _handler
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())
        assert world["writes"] == 2
        assert world["value"] == ARGS["code"], "the replay did not converge"
    finally:
        unregister_durability(TOOL)


def test_the_idempotency_key_is_stable_across_attempts(tmp_path):
    j = make_journal(tmp_path)
    eid = observed_failure(j, cls=IDEMPOTENT_KEY)
    first = j.get(eid).idempotency_key
    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=IDEMPOTENT_KEY, tool_input=ARGS)
    assert again.owned is True
    assert again.record.owner_attempt == 2, "this was not a new attempt"
    assert again.record.idempotency_key == first, (
        "a new attempt regenerated the key; the external system would see two")


def test_the_idempotency_key_survives_a_process_replacement(tmp_path):
    """§20 — restart, new instance id, new connection, SAME durable key."""
    j = make_journal(tmp_path, instance_id="inst-one")
    eid = observed_failure(j, cls=IDEMPOTENT_KEY)
    before = j.get(eid).idempotency_key
    j.close()

    replacement = make_journal(tmp_path, instance_id="inst-two")
    again = replacement.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                                durability_class=IDEMPOTENT_KEY, tool_input=ARGS)
    assert again.owned is True
    assert again.record.owner_instance_id == "inst-two"
    assert again.record.idempotency_key == before
    assert before == derive_idempotency_key(eid)


def test_an_idempotent_with_key_replay_is_deduplicated_by_the_far_side(h, tmp_path):
    """The whole contract: JARVIS replays, the EXTERNAL system deduplicates,
    and exactly one logical effect exists — across a process replacement."""
    register_durability(TOOL, IDEMPOTENT_KEY)
    try:
        seen: set = set()
        applied: list = []

        def make_handler(journal, eid):
            def _handler(**kwargs):
                key = journal.get(eid).idempotency_key
                if key not in seen:                  # the far side's dedupe
                    seen.add(key)
                    applied.append(key)
                raise TimeoutError("response lost") if len(applied) == 1 \
                    and len(seen) == 1 and not kwargs.get("_ok") else None
            return _handler

        eid = h.effect_id()

        def _first(**kwargs):
            key = h.journal.get(eid).idempotency_key
            if key not in seen:
                seen.add(key)
                applied.append(key)
            raise TimeoutError("the response was lost")

        h.executor._tool_code_execute = _first
        asyncio.run(h.call())
        assert len(applied) == 1
        h.journal.close()

        # A replacement process: new executor, new journal instance, same file.
        second = Harness(tmp_path, instance_id="inst-two")
        second.executor._challenge = h.executor._challenge

        def _second(**kwargs):
            key = second.journal.get(eid).idempotency_key
            if key not in seen:
                seen.add(key)
                applied.append(key)
            return {"stdout": "ok"}

        second.executor._tool_code_execute = _second
        note: dict = {}
        asyncio.run(second.call(note=note))
        assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
        assert len(applied) == 1, (
            f"the external system saw {len(applied)} distinct keys")
    finally:
        unregister_durability(TOOL)


# ══════════════════════════════════════════════════════════════════════════════
#  RECONCILABLE — §21, scenarios A-E
# ══════════════════════════════════════════════════════════════════════════════
def reconcilable(verdict_or_exc, *, record=None):
    """Register TOOL as RECONCILABLE with a probe that answers deterministically."""
    def _probe(effect_id, idempotency_key):
        if record is not None:
            record.append((effect_id, idempotency_key))
        if isinstance(verdict_or_exc, BaseException):
            raise verdict_or_exc
        return verdict_or_exc

    register_durability(TOOL, RECONCILABLE)
    register_reconciler(TOOL, _probe)


def test_reconciliation_a_committed_recovers_without_replaying(h):
    calls: dict = {}
    reconcilable(ReconciliationVerdict.CONFIRMED_COMMITTED)
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        note: dict = {}
        result = asyncio.run(h.call(note=note))
        assert calls["n"] == 1, "a reconciled-committed effect was replayed"
        assert note["disposition"] == (
            ExecutionDisposition.RECONCILED_COMMITTED.value)
        assert result["status"] == "recovered"
        assert h.journal.get(h.effect_id()).state is \
            EffectState.RECONCILED_COMMITTED
    finally:
        unregister_durability(TOOL)


def test_reconciliation_b_not_executed_allows_exactly_one_new_attempt(h):
    calls: dict = {}
    reconcilable(ReconciliationVerdict.CONFIRMED_NOT_EXECUTED)
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        note: dict = {}
        result = asyncio.run(h.call(note=note))
        assert calls["n"] == 2
        assert result == {"stdout": "second"}
        assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    finally:
        unregister_durability(TOOL)


def test_reconciliation_c_unknown_never_replays(h):
    """Scenario C, and the corrected answer to "what should the caller be told?"

    This asserted ``reconciliation_available is True`` after the probe had
    already answered UNKNOWN. A red team named that as a defect and it is one:
    the runtime published REQUIRES_RECONCILIATION on every subsequent attempt,
    inviting the caller to keep asking a reconciler that has already failed to
    answer. The table always said BLOCKED_INDETERMINATE for asked-and-unanswered
    — the runtime computed the field by hand instead of asking the table.
    """
    calls: dict = {}
    reconcilable(ReconciliationVerdict.UNKNOWN)
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        note: dict = {}
        result = asyncio.run(h.call(note=note))
        assert calls["n"] == 1, "an UNKNOWN verdict authorised a replay"
        assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
        assert result["error_class"] == "indeterminate_effect"
        assert note["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value
        assert result["reconciliation_available"] is False, (
            "asked and unanswered was published as though it were unasked")
        assert note["reconciliation_required"] is False
        assert h.journal.get(h.effect_id()).state is EffectState.INDETERMINATE
    finally:
        unregister_durability(TOOL)


def test_a_reconciler_that_cannot_answer_is_not_asked_forever(h):
    """D4's regression. The runtime must stop inviting a settled non-answer."""
    asked: list = []
    calls: dict = {}

    def _probe(effect_id, idempotency_key):
        asked.append(effect_id)
        return ReconciliationVerdict.UNKNOWN

    register_durability(TOOL, RECONCILABLE)
    register_reconciler(TOOL, _probe)
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        for _ in range(4):
            h.executor._effect_ledger.clear()
            note: dict = {}
            asyncio.run(h.call(note=note))
            assert note["retry_authority"] == (
                RetryAuthority.BLOCKED_INDETERMINATE.value)
            assert note["reconciliation_required"] is False
        assert calls["n"] == 1
    finally:
        unregister_durability(TOOL)


def test_a_reconciler_that_never_answers_does_not_hang_the_call(h):
    """D5. `reconcile`'s docstring said "bounded"; the call had no deadline."""
    import threading

    calls: dict = {}
    release = threading.Event()

    def _hangs(effect_id, idempotency_key):
        release.wait(timeout=30.0)
        return ReconciliationVerdict.CONFIRMED_NOT_EXECUTED

    register_durability(TOOL, RECONCILABLE)
    register_reconciler(TOOL, _hangs)
    original = h.executor.RECONCILE_TIMEOUT_S
    h.executor.RECONCILE_TIMEOUT_S = 0.3
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        note: dict = {}
        asyncio.run(asyncio.wait_for(h.call(note=note), timeout=DEADLINE_S))
        assert note["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value
        assert calls["n"] == 1, "a probe that never answered authorised a replay"
    finally:
        release.set()
        h.executor.RECONCILE_TIMEOUT_S = original
        unregister_durability(TOOL)


@pytest.mark.parametrize("failure", [
    RuntimeError("the reconciler broke"),
    TimeoutError("the reconciler timed out"),
])
def test_reconciliation_d_a_broken_probe_never_replays(h, failure):
    """A reconciler that cannot answer has not answered. It has certainly not
    said 'nothing happened'."""
    calls: dict = {}
    reconcilable(failure)
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        note: dict = {}
        asyncio.run(h.call(note=note))
        assert calls["n"] == 1
        assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    finally:
        unregister_durability(TOOL)


def test_a_probe_returning_junk_is_read_as_unknown(h):
    calls: dict = {}
    reconcilable("CONFIRMED_NOT_EXECUTED")       # a bare string, not a verdict
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())
        assert calls["n"] == 1
    finally:
        unregister_durability(TOOL)


def test_reconciliation_e_two_recoverers_produce_at_most_one_effect(tmp_path):
    """§21E / §26 — two processes reading the same uncertainty must not both
    read it as permission."""
    j_one = make_journal(tmp_path, instance_id="inst-one")
    eid = observed_failure(j_one, cls=RECONCILABLE)
    # Classify it the way a caller asking again does. Reconciliation is only
    # reachable from INDETERMINATE — the journal refuses a direct
    # FAILED_OBSERVED -> RECONCILED_NOT_EXECUTED edge, so a recoverer cannot
    # skip the step that makes the uncertainty visible.
    with pytest.raises(InvalidTransition):
        j_one.apply_reconciliation(
            eid, ReconciliationVerdict.CONFIRMED_NOT_EXECUTED)
    j_one.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                  durability_class=RECONCILABLE, tool_input=ARGS)
    assert j_one.get(eid).state is EffectState.INDETERMINATE
    j_one.close()

    a = make_journal(tmp_path, instance_id="inst-a")
    b = make_journal(tmp_path, instance_id="inst-b")
    a.apply_reconciliation(eid, ReconciliationVerdict.CONFIRMED_NOT_EXECUTED)

    # Both now reserve. The compare-and-swap decides one winner.
    r_a = a.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                    durability_class=RECONCILABLE, tool_input=ARGS)
    r_b = b.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                    durability_class=RECONCILABLE, tool_input=ARGS)
    assert [r_a.owned, r_b.owned].count(True) == 1, (
        "two callers both took ownership of one effect identity")


def test_the_reconciler_is_asked_about_the_right_identity(h):
    """A probe asked about the wrong effect is answering a different question."""
    seen: list = []
    calls: dict = {}
    reconcilable(ReconciliationVerdict.UNKNOWN, record=seen)
    try:
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())
        assert seen == [(h.effect_id(), derive_idempotency_key(h.effect_id()))]
    finally:
        unregister_durability(TOOL)


def test_a_tool_with_no_reconciler_is_blocked_not_replayed(h):
    """RECONCILABLE is a claim that the system CAN be asked. If nothing
    answers, the answer is UNKNOWN, not 'go ahead'."""
    register_durability(TOOL, RECONCILABLE)
    try:
        calls: dict = {}
        h.executor._tool_code_execute = failing_once(calls)
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())
        assert calls["n"] == 1
    finally:
        unregister_durability(TOOL)


# ══════════════════════════════════════════════════════════════════════════════
#  NATIVE_MCP_PARITY — §14
# ══════════════════════════════════════════════════════════════════════════════
def test_there_is_still_exactly_one_effect_protocol():
    """M65C's unification is a precondition for M65D's parity claim."""
    import inspect

    from tools.executor import ToolExecutor

    src = inspect.getsource(ToolExecutor)
    assert src.count("await self._execute_effect_protocol(") == 2, (
        "the native and MCP surfaces no longer share one protocol")
    assert src.count("def _durable_effect(") == 1
    assert src.count("def _settle_observed_failure(") == 1


def test_the_mcp_surface_blocks_an_ambiguous_non_replayable_effect(h):
    """The same window, on the other surface, with the same answer."""
    def _behaviour(n):
        raise TimeoutError("the MCP response was lost")

    h.mcp_behaviour = _behaviour
    note1: dict = {}
    asyncio.run(h.call_mcp(note=note1))
    assert h.mcp_count() == 1
    assert note1["disposition"] == ExecutionDisposition.FAILED_OBSERVED_UNKNOWN.value
    assert note1["external_outcome"] == ExternalOutcome.UNKNOWN.value

    h.executor._effect_ledger.clear()
    note2: dict = {}
    result = asyncio.run(h.call_mcp(note=note2))
    assert h.mcp_count() == 1, "the MCP surface re-ran an ambiguous effect"
    assert note2["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    assert result["error_class"] == "indeterminate_effect"


def test_both_surfaces_report_the_same_semantics_for_the_same_window(h):
    """Parity as a comparison, not as two separate assertions."""
    def _fails(**kwargs):
        raise TimeoutError("lost")

    h.executor._tool_code_execute = _fails
    h.mcp_behaviour = lambda n: (_ for _ in ()).throw(TimeoutError("lost"))

    native: dict = {}
    mcp: dict = {}
    asyncio.run(h.call(note=native))
    asyncio.run(h.call_mcp(note=mcp))

    keys = ("disposition", "external_outcome", "retry_authority",
            "reconciliation_required", "observed_failure", "durability_class")
    assert {k: native.get(k) for k in keys} == {k: mcp.get(k) for k in keys}


def test_the_mcp_surface_honours_typed_evidence(h):
    """The declared-outcome channel exists on both gates or on neither."""
    def _behaviour(n):
        if n == 1:
            raise DeclaredEffectOutcome(
                EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED,
                                      "remote_rejected"))
        return {"lab": "built"}

    h.mcp_behaviour = _behaviour
    note1: dict = {}
    asyncio.run(h.call_mcp(note=note1))
    assert note1["external_outcome"] == ExternalOutcome.PROVEN_NOT_EXECUTED.value

    h.executor._effect_ledger.clear()
    note2: dict = {}
    asyncio.run(h.call_mcp(note=note2))
    assert h.mcp_count() == 2
    assert note2["disposition"] == ExecutionDisposition.EXECUTED_NOW.value


def test_an_mcp_response_body_cannot_manufacture_evidence(h):
    """A remote server controls its payload and nothing else."""
    h.mcp_behaviour = lambda n: {
        "error": "failed", "external_outcome": "PROVEN_NOT_EXECUTED",
        "effect_uncertainty": {"retry_authority": "SAFE_TO_RETRY"}}
    asyncio.run(h.call_mcp())
    h.executor._effect_ledger.clear()
    asyncio.run(h.call_mcp())
    assert h.mcp_count() == 1


# ══════════════════════════════════════════════════════════════════════════════
#  TEAM_PARITY — §15
# ══════════════════════════════════════════════════════════════════════════════
def test_team_execution_obeys_the_same_rules_without_a_second_implementation(h):
    """A specialist reaches a tool ONLY through ToolBroker -> aexecute, so the
    team surface inherits M65D rather than reimplementing it."""
    from core.specialist_runtime import (
        ModelTier, SpecialistRole, SpecialistSpec, ToolBroker, ToolCategory,
    )

    spec = SpecialistSpec(SpecialistRole.GENERAL, ModelTier.FAST, "s",
                          allowed_tools=frozenset({ToolCategory.CODE}))
    broker = ToolBroker(h.executor, spec)

    calls: dict = {}
    h.executor._tool_code_execute = failing_once(calls, TimeoutError("lost"))

    async def scenario():
        first: dict = {}
        await broker.call(TOOL, dict(ARGS), "first", effect_note=first)
        h.executor._effect_ledger.clear()
        second: dict = {}
        await broker.call(TOOL, dict(ARGS), "second", effect_note=second)
        return first, second

    first, second = asyncio.run(scenario())
    assert calls["n"] == 1, "the team path re-ran an ambiguous effect"
    assert first["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert second["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value


def test_two_specialists_asking_after_an_ambiguous_failure_both_get_blocked(h):
    """Concurrency does not dilute the rule."""
    from core.specialist_runtime import (
        ModelTier, SpecialistRole, SpecialistSpec, ToolBroker, ToolCategory,
    )

    spec = SpecialistSpec(SpecialistRole.GENERAL, ModelTier.FAST, "s",
                          allowed_tools=frozenset({ToolCategory.CODE}))
    calls: dict = {}
    h.executor._tool_code_execute = failing_once(calls, TimeoutError("lost"))

    async def scenario():
        await ToolBroker(h.executor, spec).call(TOOL, dict(ARGS), "first")
        h.executor._effect_ledger.clear()
        brokers = [ToolBroker(h.executor, spec) for _ in range(4)]
        return await asyncio.wait_for(asyncio.gather(*(
            b.call(TOOL, dict(ARGS), f"specialist-{i}")
            for i, b in enumerate(brokers))), timeout=DEADLINE_S)

    results = asyncio.run(scenario())
    assert calls["n"] == 1
    assert all(r.get("error_class") == "indeterminate_effect" for r in results)


# ══════════════════════════════════════════════════════════════════════════════
#  CANCELLATION — §23
# ══════════════════════════════════════════════════════════════════════════════
def test_cancellation_after_the_boundary_is_unknown_not_no_effect(h):
    """The handler is on a thread pool and keeps going. A caller changing its
    mind is not evidence about the world."""
    started = asyncio.Event()

    def _handler(**kwargs):
        h.executor._loop.call_soon_threadsafe(started.set)
        import time as _t
        _t.sleep(0.5)
        return {"stdout": "late"}

    h.executor._tool_code_execute = _handler

    async def scenario():
        note: dict = {}
        task = asyncio.create_task(h.call(note=note))
        await asyncio.wait_for(started.wait(), timeout=DEADLINE_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return note

    note = asyncio.run(scenario())
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    assert h.journal.get(h.effect_id()).state is EffectState.INDETERMINATE


def test_cancellation_before_the_boundary_is_proven_no_effect(h):
    """The other half. Cancelling inside the gate, before mark_executing, IS
    provably harmless — and must stay so, or every cancellation becomes a task
    for a human."""
    entered = asyncio.Event()
    release = asyncio.Event()
    h.add_tool()

    async def _parked(tool_name, preview):
        entered.set()
        await release.wait()
        return True, "test:granted"

    h.executor._challenge = _parked

    async def scenario():
        note: dict = {}
        task = asyncio.create_task(h.call(note=note))
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return note

    note = asyncio.run(scenario())
    assert h.count() == 0
    assert note["external_outcome"] == ExternalOutcome.PROVEN_NOT_EXECUTED.value
    assert h.journal.get(h.effect_id()).state is EffectState.FAILED_BEFORE_EFFECT


# ══════════════════════════════════════════════════════════════════════════════
#  POSTPROCESSING — §24
# ══════════════════════════════════════════════════════════════════════════════
def test_a_postprocessing_failure_after_a_real_effect_is_unknown(h):
    """The remote action succeeded; the local wrapper broke parsing it.

    Nothing local can tell this apart from a call that never reached the
    server, so the honest answer is UNKNOWN — and the effect is not repeated.
    """
    world = {"applied": 0}

    def _handler(**kwargs):
        world["applied"] += 1                       # the effect really happens
        raise ValueError("could not parse the response")

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert world["applied"] == 1
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value

    h.executor._effect_ledger.clear()
    asyncio.run(h.call())
    assert world["applied"] == 1, "a postprocessing failure duplicated an effect"


def test_a_postprocessing_failure_with_a_trusted_ack_is_committed(h):
    """§24 — when the architecture CAN retain a trusted acknowledgement from
    before postprocessing, it is used, and the outcome stops being unknown."""
    world = {"applied": 0}

    def _handler(**kwargs):
        world["applied"] += 1
        try:
            raise ValueError("could not parse the response")
        except ValueError as exc:
            raise DeclaredEffectOutcome(
                EffectOutcomeEvidence(ExternalOutcome.PROVEN_COMMITTED,
                                      "acked_before_parse"),
                "the server acknowledged before the parse failed") from exc

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["external_outcome"] == ExternalOutcome.PROVEN_COMMITTED.value

    h.executor._effect_ledger.clear()
    second: dict = {}
    asyncio.run(h.call(note=second))
    assert world["applied"] == 1
    assert second["disposition"] == ExecutionDisposition.RECOVERED_COMMITTED.value


# ══════════════════════════════════════════════════════════════════════════════
#  FAIL_CLOSED — §27
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unhealthy_journal_never_authorises_an_execution(h, monkeypatch):
    from core.effect_journal import JournalUnhealthy

    h.add_tool()

    def _boom(**kwargs):
        raise JournalUnhealthy("integrity check failed")

    monkeypatch.setattr(h.journal, "reserve", _boom)
    note: dict = {}
    result = asyncio.run(h.call(note=note))
    assert h.count() == 0
    assert result["error_class"] == "journal_unhealthy"
    assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value


def test_a_journal_that_cannot_record_the_failure_still_blocks(h, monkeypatch):
    """§18 F8. If the observed-failure write is lost the row stays EXECUTING,
    which reads as UNKNOWN — so the next caller is blocked either way."""
    from core.effect_journal import JournalUnhealthy

    calls: dict = {}
    h.executor._tool_code_execute = failing_once(calls, TimeoutError("lost"))

    def _boom(*a, **kw):
        raise JournalUnhealthy("locked")

    monkeypatch.setattr(h.journal, "fail_observed", _boom)
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["journal_write_failed"] is True
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert h.journal.get(h.effect_id()).state is EffectState.EXECUTING

    monkeypatch.undo()
    h.executor._effect_ledger.clear()
    asyncio.run(h.call())
    assert calls["n"] == 1, "a lost journal write bought a second execution"


def test_disabling_the_journal_does_not_make_a_failure_look_harmless(h,
                                                                     monkeypatch):
    """Switching the protection off removes the protection, not the physics.

    M65C reported FAILED_BEFORE_EFFECT on this branch too — the same false
    statement, in the one place where nothing is left to catch it.
    """
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "0")
    from tools.executor import ToolExecutor

    executor = ToolExecutor()
    executor.begin_effect_epoch(EPOCH)

    async def _granted(tool_name, preview):
        return True, "test:granted"

    executor._challenge = _granted

    def _handler(**kwargs):
        raise TimeoutError("lost")

    executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(executor.aexecute(TOOL, dict(ARGS), "caller", effect_note=note))
    assert note["durable"] is False
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert note["disposition"] == ExecutionDisposition.FAILED_OBSERVED_UNKNOWN.value


def test_the_production_durability_inventory():
    """§29 — derived from the reachable surface, never copied from a document.

    A tool added without a classification moves these counts, which is the
    point: the fail-closed default should be visible as a number that someone
    has to look at, not as a silent fallback.
    """
    from collections import Counter

    from tools.executor import MCP_TOOL_ALLOWLIST, ToolExecutor
    from core.risk_classes import RiskClass, classify_tool

    native = {n[len("_tool_"):] for n in dir(ToolExecutor) if n.startswith("_tool_")}
    reachable = native | set(MCP_TOOL_ALLOWLIST)
    counts = Counter(durability_class(t, classify_tool(t)).value
                     for t in reachable)

    assert counts == {"READ_ONLY": 24, "IDEMPOTENT": 1, "NON_REPLAYABLE": 23}, (
        f"the production durability inventory moved: {dict(counts)}")
    effectful = [t for t in reachable
                 if classify_tool(t) is not RiskClass.READ_ONLY]
    assert len(effectful) == 24
    assert sum(1 for t in effectful
               if classify_tool(t) is RiskClass.HIGH_IMPACT) == 15


# ══════════════════════════════════════════════════════════════════════════════
#  EFFECT_IDENTITY — the layer underneath the decision (red team round 1, D1)
#
#  `retry_authority` was never wrong. It was being asked about the wrong thing:
#  the identity was hashed from the caller's dict AS GIVEN, and the dict that
#  reaches the handler is a different object. Two spellings of one action were
#  two identities, so the block on a NON_REPLAYABLE uncertain effect was walked
#  around by adding a parameter that changes nothing.
# ══════════════════════════════════════════════════════════════════════════════
def test_spelling_out_a_default_argument_is_the_same_effect(h):
    """MEASURED at 2 external effects, one epoch, one process, no operator."""
    world: dict = {"n": 0}
    seen: list = []

    def _handler(ip, reason="containment"):
        world["n"] += 1
        seen.append((ip, reason))
        raise TimeoutError("effect applied, response lost")

    h.executor._tool_code_execute = _handler

    async def scenario():
        notes = []
        for args in ({"ip": "10.0.0.1"},
                     {"ip": "10.0.0.1"},
                     {"ip": "10.0.0.1", "reason": "containment"}):
            note: dict = {}
            await h.executor.aexecute("code_execute", dict(args), "c",
                                      effect_note=note)
            h.executor._effect_ledger.clear()
            notes.append(note)
        return notes

    notes = asyncio.run(scenario())
    assert len(set(seen)) == 1, f"the handler saw different calls: {seen}"
    assert world["n"] == 1, (
        f"one action ran {world['n']} times because a default was spelled out")
    assert notes[1]["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    assert notes[2]["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    assert notes[0]["effect_id"] == notes[2]["effect_id"]


def test_the_flag_jarvis_deletes_cannot_mint_a_new_identity(h):
    """FORCE_OVERRIDE is stripped as a probable injection attempt — and used to
    survive just long enough to be hashed into the effect identity."""
    world: dict = {"n": 0}

    def _handler(**kwargs):
        world["n"] += 1
        raise TimeoutError("effect applied, response lost")

    h.executor._tool_code_execute = _handler

    async def scenario():
        first: dict = {}
        await h.executor.aexecute("code_execute", {"t": "x"}, "c", effect_note=first)
        h.executor._effect_ledger.clear()
        second: dict = {}
        await h.executor.aexecute("code_execute", {"t": "x", "FORCE_OVERRIDE": True},
                                  "c", effect_note=second)
        return first, second

    first, second = asyncio.run(scenario())
    assert world["n"] == 1, "a stripped injection flag bought a second execution"
    assert first["effect_id"] == second["effect_id"]
    assert second["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value


def test_a_stripped_flag_does_not_turn_a_success_into_an_unknown(h):
    """D2. The ledger READ key and WRITE key were computed independently, so a
    call that plainly succeeded was journalled as an uncertain failure."""
    def _handler(**kwargs):
        return {"stdout": "ok"}

    h.executor._tool_code_execute = _handler

    async def scenario():
        note: dict = {}
        await h.executor.aexecute("code_execute", {"t": "x", "FORCE_OVERRIDE": True},
                                  "c", effect_note=note)
        return note

    note = asyncio.run(scenario())
    assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert note["committed"] is True
    assert note["external_outcome"] == ExternalOutcome.PROVEN_COMMITTED.value
    assert h.journal.get(note["effect_id"]).state is EffectState.COMMITTED


def test_the_identity_is_never_computed_twice():
    """Structural. Three independent derivations of one identity is how the
    read key and the write key came to disagree."""
    import inspect as _inspect

    from tools.executor import ToolExecutor

    src = _inspect.getsource(ToolExecutor)
    derivations = src.count("self._effect_key(self._effect_epoch")
    assert derivations <= 3, derivations
    assert "effect_hooks.ledger_key if effect_hooks is not None" in src, (
        "a gate went back to deriving its own ledger key")


@pytest.mark.parametrize("payload,expected", [
    ({"stdout": "ok"}, True),
    ([1, 2, 3], True),
    ("applied", True),
    ({"error": "boom"}, False),
])
def test_a_success_is_committed_whatever_shape_the_result_has(h, payload, expected):
    """D3. `committed` was read back from the in-process ledger, which can only
    hold a dict — so an ordinary MCP content list made a fully-committed effect
    read UNKNOWN forever and poisoned its identity. The ledger's shape is a
    dedup detail; it is not evidence about the external world."""
    def _handler(**kwargs):
        return payload if not isinstance(payload, dict) else dict(payload)

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["committed"] is expected
    record = h.journal.get(h.effect_id())
    assert (record.state is EffectState.COMMITTED) is expected
    if expected:
        assert note["external_outcome"] == ExternalOutcome.PROVEN_COMMITTED.value


def test_the_mcp_surface_strips_before_it_computes_identity(h):
    """Parity on the one input class the surfaces actually diverged on."""
    async def scenario():
        first: dict = {}
        await h.call_mcp(args={"tema": "vlan"}, note=first)
        h.executor._effect_ledger.clear()
        second: dict = {}
        await h.executor.aexecute_mcp(
            MCP_TOOL, {"tema": "vlan", "FORCE_OVERRIDE": True}, h.mcp_call,
            "caller", effect_note=second)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["effect_id"] == second["effect_id"]
    assert h.mcp_count() == 1


def test_the_idempotency_key_reaches_a_tool_that_asks_for_it(h):
    """D6. The key was derived, stored, and handed to nothing, so an authorised
    IDEMPOTENT_WITH_KEY replay was an undeduplicated second external effect —
    the class's entire safety argument is that the far side dedupes on it."""
    register_durability(TOOL, IDEMPOTENT_KEY)
    try:
        seen: list = []

        def _handler(code, idempotency_key=""):
            seen.append(idempotency_key)
            raise TimeoutError("response lost")

        h.executor._tool_code_execute = _handler
        asyncio.run(h.call())
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())

        assert len(seen) == 2, "the contract permits this replay"
        assert seen[0] and seen[0] == seen[1], (
            f"the far side saw {len(set(seen))} distinct key(s): {seen}")
        assert seen[0] == derive_idempotency_key(h.effect_id())
    finally:
        unregister_durability(TOOL)


def test_a_tool_that_does_not_ask_for_the_key_never_sees_it(h):
    """Opt-in by signature: no existing handler gains a surprise argument."""
    seen: list = []

    def _handler(**kwargs):
        seen.append(sorted(kwargs))
        return {"stdout": "ok"}

    h.executor._tool_code_execute = _handler
    asyncio.run(h.call())
    assert seen == [["code"]], seen


def test_an_indeterminate_idempotent_effect_is_not_stuck_forever(tmp_path):
    """D8. `_resolve_existing`'s INDETERMINATE branch read the durability class
    by hand and blocked every class — a second retry policy that contradicted
    the table, which says an IDEMPOTENT unknown is REPLAY_SAFE_BY_CONTRACT."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid, cls=IDEMPOTENT)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "cancelled after the tool was invoked")
    assert j.get(eid).state is EffectState.INDETERMINATE

    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=IDEMPOTENT, tool_input=ARGS)
    assert again.owned is True
    assert again.outcome is ReservationOutcome.RECLAIMED
    assert again.record.idempotency_key == derive_idempotency_key(eid)


def test_an_indeterminate_non_replayable_effect_stays_blocked(tmp_path):
    """...and the branch above must not have widened anything else."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner lost")
    for _ in range(3):
        again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                          durability_class=NON_REPLAYABLE, tool_input=ARGS)
        assert again.owned is False
        assert again.outcome is ReservationOutcome.INDETERMINATE


def test_an_unreadable_outcome_column_is_counted_as_work_to_do(tmp_path):
    """D9. The reader mapped anything unparseable to UNKNOWN and BLOCKED; the
    counter compared the literal 'UNKNOWN' and saw nothing, so the doctor
    reported "nothing to do" about an effect that needed a human."""
    j = make_journal(tmp_path)
    eid = observed_failure(j)
    j._db.execute("UPDATE effects SET external_outcome='unknown' WHERE effect_id=?",
                  (eid,))
    assert j.get(eid).external_effect is ExternalOutcome.UNKNOWN
    status = j.status()
    assert status["uncertain_observed"] == 1, (
        "a blocking row was invisible to the operator count")
    assert status["recovery_required"] is True
    assert [r.effect_id for r in j.uncertain_observed_failures()] == [eid]


def test_the_note_always_carries_the_two_keys(h, monkeypatch):
    """D10. Two branches left them None, contradicting "always present"."""
    from core.effect_journal import JournalUnhealthy

    h.add_tool()

    def _boom(**kwargs):
        raise JournalUnhealthy("integrity check failed")

    monkeypatch.setattr(h.journal, "reserve", _boom)
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert note["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value


def test_an_uncertain_effect_is_recovery_required_on_the_call_that_made_it(h):
    """D7. Only the BLOCKED branch set this, so the specialist receipt and ARGUS
    never saw the single-attempt case — which is the common one."""
    def _handler(**kwargs):
        raise TimeoutError("response lost")

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["observed_failure"] is True
    assert note["recovery_required"] is True


def test_a_proven_no_effect_failure_is_not_recovery_required(h):
    """...and the flag must not fire for a failure that proved it did nothing."""
    def _handler(**kwargs):
        raise DeclaredEffectOutcome(
            EffectOutcomeEvidence(ExternalOutcome.PROVEN_NOT_EXECUTED, "rejected"))

    h.executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["recovery_required"] is False


def test_a_corrupt_journal_refuses_rather_than_raising_sqlite(h, monkeypatch, tmp_path):
    """D11. `assert_healthy` existed and was never called on the effect path, so
    a corrupt-but-openable database raised a raw sqlite3 error past §25's
    refusal envelope."""
    import sqlite3 as _sqlite3

    from core.effect_journal import JournalUnhealthy

    h.add_tool()

    def _unhealthy():
        raise JournalUnhealthy("failed its integrity check")

    monkeypatch.setattr(h.journal, "assert_healthy", _unhealthy)
    monkeypatch.setattr(h.executor, "_journal_ready", False)
    monkeypatch.setattr(h.executor, "_journal", None)
    monkeypatch.setattr("core.effect_journal.DurableEffectJournal",
                        lambda *a, **kw: h.journal)
    result = asyncio.run(h.call())
    assert h.count() == 0
    assert result["error_class"] == "journal_unhealthy"

    def _raises(**kwargs):
        raise _sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(h.journal, "assert_healthy", lambda: None)
    monkeypatch.setattr(h.journal, "reserve", _raises)
    monkeypatch.setattr(h.executor, "_journal_ready", True)
    monkeypatch.setattr(h.executor, "_journal", h.journal)
    result = asyncio.run(h.call())
    assert h.count() == 0
    assert result["error_class"] == "journal_unhealthy", (
        "a raw sqlite3 error escaped the refusal envelope")


def test_the_journal_disabled_branch_reports_the_retry_authority(h, monkeypatch):
    """A mutation that made this branch publish SAFE_TO_RETRY unconditionally
    survived the whole M65D suite. It does not now."""
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "0")
    from tools.executor import ToolExecutor

    executor = ToolExecutor()
    executor.begin_effect_epoch(EPOCH)

    async def _granted(tool_name, preview):
        return True, "test:granted"

    executor._challenge = _granted

    def _handler(**kwargs):
        raise TimeoutError("lost")

    executor._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(executor.aexecute(TOOL, dict(ARGS), "caller", effect_note=note))
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert note["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value


def test_the_in_process_ledger_is_the_layer_that_answers_a_repeat(h):
    """M37, a surviving mutation. The gate deriving its OWN ledger key instead
    of carrying the protocol's made layer 1 miss — and the durable journal
    masked it, so the effect count stayed right and nothing went red.

    It is not cosmetic. `DEDUPLICATED_IN_PROCESS` is the cheap answer, and with
    the journal switched off it is the ONLY one. See the test below.
    """
    h.add_tool()
    first: dict = {}
    second: dict = {}
    asyncio.run(h.call(note=first))
    asyncio.run(h.call(note=second))
    assert h.count() == 1
    assert first["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert second["disposition"] == (
        ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value), (
        "the durable layer answered a repeat the in-process ledger should have")
    assert len(h.executor._effect_ledger) == 1


def test_with_the_journal_off_the_ledger_still_stops_a_repeat(monkeypatch):
    """The same mutation, where nothing is left to mask it: MEASURED at 2
    external effects with JARVIS_EFFECT_JOURNAL=0, which is an operator-
    supported configuration and not a test artefact."""
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "0")
    from tools.executor import ToolExecutor

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    executor = ToolExecutor()
    executor.begin_effect_epoch("turn:journal-off")

    async def _granted(tool_name, preview):
        return True, "test:granted"

    executor._challenge = _granted
    runs: dict = {"n": 0}

    def _handler(code, timeout=15):
        runs["n"] += 1
        return {"stdout": "ok"}

    executor._tool_code_execute = _handler

    async def scenario():
        notes = []
        for _ in range(2):
            note: dict = {}
            await executor.aexecute(TOOL, dict(ARGS), "caller", effect_note=note)
            notes.append(note)
        return notes

    notes = asyncio.run(scenario())
    assert notes[0]["durable"] is False, "this test proves nothing with a journal"
    assert runs["n"] == 1, (
        f"one action ran {runs['n']} times with the durable journal switched off")
    assert notes[1]["disposition"] == (
        ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value)


# ══════════════════════════════════════════════════════════════════════════════
#  RED TEAM ROUND 2 — the repaired tree, attacked again
# ══════════════════════════════════════════════════════════════════════════════
def test_a_value_the_handler_normalises_is_one_identity(h):
    """B1. D58 closed the STRUCTURAL split; this is the VALUE split. Whether
    "post" and "POST" are one request is a fact about the handler, and only
    the handler can declare it — under the same evidence rule as durability."""
    from core.effect_journal import (register_identity_normaliser,
                                     unregister_identity_normaliser)

    def _upper_method(call):
        out = dict(call)
        if isinstance(out.get("method"), str):
            out["method"] = out["method"].upper()
        return out

    register_identity_normaliser(TOOL, _upper_method)
    try:
        world: dict = {"n": 0}

        def _handler(code, method="get"):
            world["n"] += 1
            raise TimeoutError("response lost")

        h.executor._tool_code_execute = _handler

        async def scenario():
            notes = []
            for args in ({"code": "x", "method": "post"},
                         {"code": "x", "method": "POST"},
                         {"code": "x", "method": "Post"}):
                note: dict = {}
                await h.executor.aexecute(TOOL, dict(args), "c", effect_note=note)
                h.executor._effect_ledger.clear()
                notes.append(note)
            return notes

        notes = asyncio.run(scenario())
        assert world["n"] == 1, f"three spellings of one request ran {world['n']} times"
        assert len({n["effect_id"] for n in notes}) == 1
    finally:
        unregister_identity_normaliser(TOOL)


def test_a_normaliser_never_merges_genuinely_different_calls(h):
    """The failure direction of a WRONG normaliser is a real effect SUPPRESSED,
    which is worse than the duplicate it prevents. A declared one must only
    touch what it can point at."""
    from core.effect_journal import (register_identity_normaliser,
                                     unregister_identity_normaliser)

    register_identity_normaliser(TOOL, lambda c: {**c, "method": c.get("method", "").upper()})
    try:
        world: dict = {"n": 0}

        def _handler(code, method="get", timeout=10):
            world["n"] += 1
            return {"stdout": "ok"}

        h.executor._tool_code_execute = _handler

        async def scenario():
            await h.executor.aexecute(TOOL, {"code": "x", "method": "POST", "timeout": 10}, "c")
            h.executor._effect_ledger.clear()
            await h.executor.aexecute(TOOL, {"code": "x", "method": "POST", "timeout": 30}, "c")

        asyncio.run(scenario())
        assert world["n"] == 2, "a different timeout was merged into one identity"
    finally:
        unregister_identity_normaliser(TOOL)


def test_a_broken_normaliser_leaves_the_identity_as_bound(h):
    from core.effect_journal import (normalise_identity, register_identity_normaliser,
                                     unregister_identity_normaliser)

    def _explodes(call):
        raise RuntimeError("bad contract")

    register_identity_normaliser(TOOL, _explodes)
    try:
        assert normalise_identity(TOOL, {"code": "x"}) == {"code": "x"}
    finally:
        unregister_identity_normaliser(TOOL)


def test_the_audited_normaliser_table_is_exactly_what_the_handlers_prove():
    """Pinned like the durability table: each entry cites the handler line that
    proves it, and nothing may be added on a guess."""
    from core.effect_journal import _TOOL_IDENTITY_NORMALISER, normalise_identity

    assert set(_TOOL_IDENTITY_NORMALISER) == {"http_request", "project_note", "save_note"}
    assert normalise_identity("http_request", {"url": "u", "method": "post", "headers": None}) == \
        {"url": "u", "method": "POST", "headers": {}}
    assert normalise_identity("project_note", {"kind": " Decision ", "text": "t"}) == \
        {"kind": "decision", "text": "t"}
    assert normalise_identity("save_note", {"title": "t", "content": "c", "tags": None}) == \
        {"title": "t", "content": "c", "tags": []}
    # ...and the two things it must NOT touch: timeout is a real local
    # difference, and text is recorded as given.
    assert normalise_identity("http_request", {"url": "u", "timeout": 11})["timeout"] == 11
    assert normalise_identity("project_note", {"kind": "d", "text": "x "})["text"] == "x "


def test_every_hitl_exempt_normalisation_is_declared():
    """The genuinely AUTOMATIC surface: the LOW_IMPACT tools run without a
    human. Each value their handlers normalise must have a declared contract,
    or a re-spelled call is a second identity nobody approves."""
    from core.effect_journal import _TOOL_IDENTITY_NORMALISER
    from core.risk_classes import RiskClass, classify_tool, requires_hitl
    from tools.executor import ToolExecutor

    exempt = sorted(
        n[6:] for n in dir(ToolExecutor) if n.startswith("_tool_")
        and classify_tool(n[6:]) is not RiskClass.READ_ONLY
        and not requires_hitl(classify_tool(n[6:])))
    assert exempt == ["estudiar_tema", "ingest_docs", "project_note", "save_note"]
    # project_note lower-cases `kind`; save_note treats tags None as []. The
    # other two normalise nothing about their arguments — asserted by reading.
    assert {"project_note", "save_note"} <= set(_TOOL_IDENTITY_NORMALISER)


def test_a_caller_supplied_idempotency_key_is_discarded(h):
    """M5. The first draft yielded to a caller-supplied key — a model-authored
    argument choosing the key the far side deduplicates on."""
    register_durability(TOOL, IDEMPOTENT_KEY)
    try:
        seen: list = []

        def _handler(code, idempotency_key=""):
            seen.append(idempotency_key)
            raise TimeoutError("lost")

        h.executor._tool_code_execute = _handler
        asyncio.run(h.executor.aexecute(TOOL, {**ARGS, "idempotency_key": "model-chosen"}, "c"))
        h.executor._effect_ledger.clear()
        asyncio.run(h.call())
        assert "model-chosen" not in seen
        assert seen[0] == seen[1] == derive_idempotency_key(h.effect_id())
    finally:
        unregister_durability(TOOL)


def test_an_unbindable_call_is_refused_before_the_boundary(h):
    """M6. A call that cannot bind never crossed EXECUTING before; it raised
    TypeError inside the handler and was journalled as an UNKNOWN post-boundary
    failure needing a human — about a call that provably never ran."""
    entered: dict = {"n": 0}

    def _handler(code):
        entered["n"] += 1
        return {"stdout": "ok"}

    h.executor._tool_code_execute = _handler
    note: dict = {}
    result = asyncio.run(h.executor.aexecute(TOOL, {"cod": "typo"}, "c", effect_note=note))
    assert entered["n"] == 0
    assert result["error_class"] == "invalid_arguments"
    assert note["disposition"] == ExecutionDisposition.FAILED_BEFORE_EFFECT.value
    assert note["external_outcome"] == ExternalOutcome.PROVEN_NOT_EXECUTED.value
    assert h.journal.status()["uncertain_observed"] == 0


def test_a_handler_that_did_not_apply_its_effect_is_not_committed():
    """M3a. `host_firewall_rule` returned {"blocked": False} with no error key
    when the rule was not applied; the protocol read that as a success."""
    from tools.executor import ToolExecutor
    import core.security_auditor as sa

    ex = ToolExecutor()
    original = sa._block_port_firewall
    sa._block_port_firewall = lambda port, proto: False
    try:
        out = ex._tool_host_firewall_rule(port=22, proto="TCP", authorization_id="auth")
    finally:
        sa._block_port_firewall = original
    assert "error" in out and out["blocked"] is False


def test_the_mcp_adapter_propagates_the_failure_signal():
    """M3b. `isError` was dropped, so every remote failure was a SUCCESS."""
    from core.llm import mcp_result_envelope

    class _C:
        def __init__(self, t): self.text = t

    class _R:
        def __init__(self, t, e): self.content = [_C(t)]; self.isError = e

    assert mcp_result_envelope(_R("nothing was written", True)) == {
        "error": "nothing was written", "error_class": "mcp_tool_error"}
    assert mcp_result_envelope(_R("built", False)) == {"result": "built"}


def test_an_mcp_error_envelope_is_an_observed_failure_not_a_commit(h):
    """M4 — the vacuity the reviewer found: the only MCP-failure assertion was
    `mcp_count() == 1`, which a wrong COMMIT satisfies exactly as well as the
    correct UNKNOWN. This one pins the state."""
    h.mcp_behaviour = lambda n: {"error": "remote failed", "error_class": "mcp_tool_error"}
    note: dict = {}
    asyncio.run(h.call_mcp(note=note))
    assert note["committed"] is False
    assert note["disposition"] == ExecutionDisposition.FAILED_OBSERVED_UNKNOWN.value
    assert note["external_outcome"] == ExternalOutcome.UNKNOWN.value
    assert h.journal.get(h.effect_id(tool=MCP_TOOL, args=MCP_ARGS, surface="mcp")).state \
        is EffectState.FAILED_OBSERVED


def test_a_stale_reconciliation_verdict_cannot_land_on_a_later_attempt(tmp_path):
    """M2. A probe answers about the attempt it was asked about. The timeout
    bounds how LONG it may take, not WHICH attempt its answer is about."""
    j = make_journal(tmp_path)
    eid = observed_failure(j, cls=RECONCILABLE)
    first = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=RECONCILABLE, tool_input=ARGS)
    assert first.outcome is ReservationOutcome.RECONCILE_REQUIRED
    asked_about = first.record.owner_attempt            # 1

    # Attempt 2 takes over and lands while the probe is still thinking.
    j.apply_reconciliation(eid, ReconciliationVerdict.CONFIRMED_NOT_EXECUTED,
                           expect_attempt=asked_about)
    second = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                       durability_class=RECONCILABLE, tool_input=ARGS)
    assert second.owned and second.record.owner_attempt == 2
    j.mark_executing(eid)
    j.commit(eid, receipt={"ok": 1})

    # ...and now the SLOW probe's answer about attempt 1 arrives.
    assert j.apply_reconciliation(
        eid, ReconciliationVerdict.CONFIRMED_NOT_EXECUTED,
        expect_attempt=asked_about) is False, "a stale verdict was applied"
    assert j.get(eid).state is EffectState.COMMITTED
    assert j.counters.get("reconciliations_stale") == 1


def test_a_journal_that_cannot_record_a_commit_does_not_lose_the_result(h, monkeypatch):
    """M7. The owner HAS the receipt. A locked journal must not turn that
    into a raw exception and a disposition of None."""
    from core.effect_journal import JournalUnhealthy

    h.add_tool()

    def _locked(*a, **kw):
        raise JournalUnhealthy("locked")

    monkeypatch.setattr(h.journal, "commit", _locked)
    note: dict = {}
    result = asyncio.run(h.call(note=note))
    assert result == {"stdout": "1"}
    assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert note["journal_write_failed"] is True
    assert note["durably_recorded"] is False
    assert h.journal.get(h.effect_id()).state is EffectState.EXECUTING   # conservative


def test_a_late_owner_with_a_receipt_may_still_commit(tmp_path):
    """M7, journal half. A concurrent caller classified the lost lease as
    INDETERMINATE; the original owner comes back with the tool's own receipt."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner lost")          # a classifier's view
    assert j.commit(eid, receipt={"ok": 1}) is True   # the owner's receipt wins
    assert j.get(eid).state is EffectState.COMMITTED
    assert j.get(eid).proven_committed


def test_a_late_owner_cannot_commit_over_a_new_attempt(tmp_path):
    """...but not if someone has since taken over (the CAS on owner_attempt)."""
    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid, cls=IDEMPOTENT)
    j.mark_executing(eid)
    j.mark_indeterminate(eid, "owner lost")
    again = j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=IDEMPOTENT, tool_input=ARGS)
    assert again.owned and again.record.owner_attempt == 2
    late = make_journal(tmp_path, instance_id=j.instance_id)
    stale = late.get(eid)
    assert stale.owner_attempt == 2
    # the late owner's write carries attempt 1's view and must not land
    assert late._apply(eid, EffectState.COMMITTED, expect_owner=j.instance_id,
                       receipt="r", stamp_committed=True, expect_attempt=1) is False


def test_an_undeclared_epoch_is_scoped_to_this_process(tmp_path, monkeypatch):
    """M1. Callers that never open an epoch — containment, runbooks, the task
    graph — shared identity scope "" forever, so a legitimate quarantine of the
    same address on a LATER BOOT was answered "recovered" and never applied."""
    from tools.executor import ToolExecutor

    async def _no_broadcast(_p):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    applied: list = []

    def boot(instance_id):
        journal = DurableEffectJournal(tmp_path / "effects.db", instance_id=instance_id)
        ex = ToolExecutor(journal=journal)           # NO begin_effect_epoch

        async def _granted(tool_name, preview):
            return True, "test:granted"

        ex._challenge = _granted

        def _quarantine(ip):
            applied.append((instance_id, ip))
            return {"blocked": True}

        ex._tool_code_execute = _quarantine
        note: dict = {}
        asyncio.run(ex.aexecute(TOOL, {"ip": "10.0.0.5"}, "containment", effect_note=note))
        journal.close()
        return note

    one = boot("boot-1")
    two = boot("boot-2")
    assert one["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert two["disposition"] == ExecutionDisposition.EXECUTED_NOW.value, (
        "a legitimate containment on a later boot was SUPPRESSED")
    assert len(applied) == 2
    assert one["effect_id"] != two["effect_id"]


def test_a_corrupt_enum_reports_unhealthy_instead_of_wedging(tmp_path):
    """m1. An unreadable value raised past `reserve` with BEGIN IMMEDIATE still
    open, wedging this process and locking the file for every other."""
    from core.effect_journal import JournalUnhealthy

    j = make_journal(tmp_path)
    eid = effect_id()
    reserve(j, eid=eid)
    j._db.execute("UPDATE effects SET durability_class='NOT_A_CLASS' WHERE effect_id=?", (eid,))
    with pytest.raises(JournalUnhealthy):
        j.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                  durability_class=NON_REPLAYABLE, tool_input=ARGS)
    assert not j._db.in_transaction, "the failed reserve left a transaction open"
    # ...and a different identity in the same process still works.
    other = j.reserve(effect_id=effect_id(args={"code": "other"}), tool_id=TOOL,
                      surface="native", durability_class=NON_REPLAYABLE,
                      tool_input={"code": "other"})
    assert other.owned


def test_the_blocked_message_reports_the_rows_class_not_a_reregistration(h):
    """m2. A runtime re-registration must not make the message disagree with
    the decision the journal actually took."""
    calls: dict = {}
    h.executor._tool_code_execute = failing_once(calls, TimeoutError("lost"))
    asyncio.run(h.call())                                  # NON_REPLAYABLE row
    h.executor._effect_ledger.clear()
    register_durability(TOOL, IDEMPOTENT)                  # someone re-declares
    try:
        note: dict = {}
        result = asyncio.run(h.call(note=note))
    finally:
        unregister_durability(TOOL)
    assert calls["n"] == 1, "the stored class governs the decision"
    assert result["durability_class"] == NON_REPLAYABLE.value
    assert note["retry_authority"] == RetryAuthority.BLOCKED_INDETERMINATE.value


def test_a_kwargs_handler_never_sees_a_caller_supplied_key(h):
    """M47, a surviving mutation, and the shape that actually needs the strip.

    With a handler declaring ``idempotency_key`` explicitly, the protocol's key
    wins by dict-merge order alone — which is why the first test of this
    property could not tell whether the strip was doing anything. A ``**kwargs``
    handler absorbs the caller's key silently: ``accepts`` is False (the
    parameter is ``kwargs``), the bind-check passes, and a model-authored value
    reaches the tool as the key the far side deduplicates on.
    """
    register_durability(TOOL, IDEMPOTENT_KEY)
    try:
        seen: list = []

        def _handler(**kwargs):
            seen.append(kwargs.get(_IDEMPOTENCY_ARG, "<none>"))
            return {"stdout": "ok"}

        h.executor._tool_code_execute = _handler
        asyncio.run(h.executor.aexecute(
            TOOL, {**ARGS, _IDEMPOTENCY_ARG: "model-chosen"}, "c"))
        assert seen == ["<none>"], (
            f"a model-authored idempotency key reached the tool: {seen}")
    finally:
        unregister_durability(TOOL)


def test_a_handler_that_cannot_take_the_key_refuses_rather_than_receiving_it(h):
    """The other half: a caller-supplied key to a handler with no such
    parameter is refused before the boundary, not silently passed through."""
    register_durability(TOOL, IDEMPOTENT_KEY)
    try:
        entered: dict = {"n": 0}

        def _handler(code):
            entered["n"] += 1
            return {"stdout": "ok"}

        h.executor._tool_code_execute = _handler
        result = asyncio.run(h.executor.aexecute(
            TOOL, {**ARGS, _IDEMPOTENCY_ARG: "model-chosen"}, "c"))
        assert entered["n"] == 1, "the key was not stripped before the bind check"
        assert "error" not in result
    finally:
        unregister_durability(TOOL)


def test_the_executor_passes_the_attempt_a_verdict_is_about(h, tmp_path):
    """M52, a surviving mutation. The journal-level guard was pinned; the
    executor PASSING it was not, so deleting `expect_attempt=` at the call site
    was invisible.

    The probe here does what a slow probe does: while it is thinking, the row
    moves on — a new attempt takes over, its effect LANDS, and its outcome is
    unknown too. The probe then returns a truthful answer about attempt 1. It
    must not be written against attempt 2.
    """
    world: list = []
    calls: dict = {}
    eid = h.effect_id()

    def _probe(effect_id, idempotency_key):
        # attempt 2 takes over and lands while this probe is in flight
        h.journal.apply_reconciliation(
            effect_id, ReconciliationVerdict.CONFIRMED_NOT_EXECUTED)
        h.journal.reserve(effect_id=effect_id, tool_id=TOOL, surface="native",
                          durability_class=RECONCILABLE, tool_input=ARGS)
        h.journal.mark_executing(effect_id)
        world.append("attempt-2 effect landed")
        h.journal.mark_indeterminate(effect_id, "attempt 2 outcome unknown")
        return ReconciliationVerdict.CONFIRMED_NOT_EXECUTED      # about attempt 1

    register_durability(TOOL, RECONCILABLE)
    register_reconciler(TOOL, _probe)
    try:
        h.executor._tool_code_execute = failing_once(calls, TimeoutError("lost"))
        asyncio.run(h.call())
        assert h.journal.get(eid).state is EffectState.FAILED_OBSERVED
        h.executor._effect_ledger.clear()

        note: dict = {}
        asyncio.run(h.call(note=note))
        assert h.journal.get(eid).state is EffectState.INDETERMINATE, (
            "a verdict about attempt 1 was applied to attempt 2")
        assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
        assert calls["n"] == 1, (
            f"the handler ran {calls['n']} times after an effect had already landed")
        assert len(world) == 1
        assert h.journal.counters.get("reconciliations_stale") == 1
    finally:
        unregister_durability(TOOL)
