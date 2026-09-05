"""
tests/test_effect_journal_argus_v69_m65c.py — V69 M65C DISPOSITIONS, ARGUS AND
THE MCP GATE.

Three things that do not fit the store, the protocol or the crash suites, and
that the mutation campaign needs a home for:

  * ARGUS must not turn an effect whose outcome is unknown into a verified
    success (§46);
  * a receipt's disposition must be COPIED from the executor, never re-derived
    from a status — re-deriving is exactly the M65A mistake that let two
    specialists claim one effect (§18);
  * the MCP gate now enforces authorized scope, which it did not before this
    milestone, and a hardening nobody tests is a hardening that will be removed.
"""
from __future__ import annotations

import asyncio
import inspect
import json


from core.cognitive_mesh import SpecialistId
from core.effect_journal import (
    DurableEffectJournal,
    EffectDurabilityClass,
    EffectState,
    ExecutionDisposition,
    compute_effect_id,
)
from core.mesh_contracts import ResultStatus, SpecialistResult, Verdict
from core.mesh_verifier import VerificationInput, verify
from core.specialist_execution import ToolCallStatus, ToolReceipt
from core.security_scope import SecurityScopeDecision  # noqa: F401
from core.cognitive_mesh import AutonomyLevel
from core.mesh_contracts import EvidenceGraph

TOOL = "code_execute"
ARGS = {"code": "print(1)"}
SCOPE = "turn:m65c-argus"


#: ATLAS is the one registered specialist whose evidence policy is
#: ``none_required``. Using it keeps these fixtures about the INDETERMINATE
#: check rather than about the evidence check — a control that fails for an
#: unrelated reason would make the assertion above prove nothing.
_SPEAKER = SpecialistId.ATLAS


def _result(status=ResultStatus.COMPLETE) -> SpecialistResult:
    return SpecialistResult(
        status=status, specialist_id=_SPEAKER, task_id="t1",
        summary="did the thing", findings=(), evidence_ids=(), confidence=0.7)


def _input(**kw) -> VerificationInput:
    base = dict(task_id="t1", objective="do the thing", graph=EvidenceGraph(),
                results=(_result(),), autonomy_ceiling=AutonomyLevel.OBSERVE)
    base.update(kw)
    return VerificationInput(**base)


# ══════════════════════════════════════════════════════════════════════════════
#  ARGUS AND UNKNOWN OUTCOMES (§46)
# ══════════════════════════════════════════════════════════════════════════════
def test_an_indeterminate_effect_stops_argus_verifying():
    """The rule: ARGUS may not turn INDETERMINATE into VERIFIED.

    Not because the action failed — it may well have succeeded — but because
    nothing here can demonstrate that it did, and a verdict is a claim about
    evidence.
    """
    verdict = verify(_input(indeterminate_effects=(
        "host_firewall_rule: a previous attempt may have taken effect",)))
    assert verdict.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert not verdict.passing
    assert any("could not be determined" in r for r in verdict.reasons)


def test_without_an_indeterminate_effect_the_same_input_verifies():
    """The positive control. Without it, the test above could be passing because
    the fixture never verifies anything."""
    verdict = verify(_input())
    assert verdict.passing, (
        "the control does not verify, so the assertion above proves nothing")


def test_argus_says_the_effect_was_not_retried():
    verdict = verify(_input(indeterminate_effects=("kill_process: unknown",)))
    blob = " ".join(verdict.reasons)
    assert "NOT retried" in blob
    assert "reconciliation" in blob or "reconcil" in blob


def test_several_unknown_effects_are_all_named_but_bounded():
    verdict = verify(_input(indeterminate_effects=tuple(
        f"tool_{n}: unknown" for n in range(9))))
    assert not verdict.passing
    assert len(verdict.reasons) <= 5, "an unbounded reason list reached the operator"


# ══════════════════════════════════════════════════════════════════════════════
#  DISPOSITIONS ARE COPIED, NEVER RE-DERIVED (§18)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_recovered_receipt_is_not_reported_as_executed_now():
    """M65A inferred the disposition from what it could see and got it wrong the
    moment two callers overlapped. A receipt must carry the executor's own
    answer, so a SUCCESS that was actually a RECOVERY says so."""
    receipt = ToolReceipt(
        receipt_id="r1", tool=TOOL, specialist_id=_SPEAKER,
        status=ToolCallStatus.SUCCESS, effect_identity="eid", executed=True,
        deduplicated=True,
        disposition=ExecutionDisposition.RECOVERED_COMMITTED.value)
    assert receipt.disposition != ExecutionDisposition.EXECUTED_NOW.value
    assert receipt.to_dict()["disposition"] == \
        ExecutionDisposition.RECOVERED_COMMITTED.value


def test_a_blocked_receipt_carries_recovery_required():
    receipt = ToolReceipt(
        receipt_id="r1", tool=TOOL, specialist_id=_SPEAKER,
        status=ToolCallStatus.FAILURE, effect_identity="eid", executed=False,
        disposition=ExecutionDisposition.BLOCKED_INDETERMINATE.value,
        recovery_required=True)
    assert receipt.to_dict()["recovery_required"] is True


def test_the_execution_result_counts_recovered_and_indeterminate_separately():
    """Recovered and deduplicated are different guarantees: one was suppressed by
    a ledger that dies with the interpreter, the other by a journal that
    outlived the process that wrote it."""
    from core.specialist_execution import ExecutionStatus, SpecialistExecutionResult

    receipts = (
        ToolReceipt(receipt_id="a", tool=TOOL, specialist_id=_SPEAKER,
                    status=ToolCallStatus.SUCCESS, effect_identity="e1",
                    executed=True, deduplicated=True,
                    disposition=ExecutionDisposition.RECOVERED_COMMITTED.value),
        ToolReceipt(receipt_id="b", tool=TOOL, specialist_id=_SPEAKER,
                    status=ToolCallStatus.FAILURE, effect_identity="e2",
                    executed=False, recovery_required=True,
                    disposition=ExecutionDisposition.BLOCKED_INDETERMINATE.value),
        ToolReceipt(receipt_id="c", tool=TOOL, specialist_id=_SPEAKER,
                    status=ToolCallStatus.SUCCESS, effect_identity="e3",
                    executed=True, deduplicated=True,
                    disposition=ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value),
    )
    result = SpecialistExecutionResult(
        execution_id="x", specialist_id=_SPEAKER,
        status=ExecutionStatus.SUCCESS, tool_receipts=receipts)
    assert result.recovered_effects == 1
    assert result.indeterminate_effects == 1
    assert result.deduplicated_effects == 2


def test_the_receipt_disposition_is_copied_from_the_effect_note():
    """A source-level guard on the one line that must not become a derivation."""
    source = inspect.getsource(
        __import__("core.specialist_execution", fromlist=["x"]))
    assert 'disposition=str(effect_note.get("disposition") or "")' in source, (
        "the receipt's disposition is no longer copied from the executor")


# ══════════════════════════════════════════════════════════════════════════════
#  OWNERSHIP GUARDS
# ══════════════════════════════════════════════════════════════════════════════
def test_a_lost_owner_cannot_commit_over_the_new_owner(tmp_path):
    """The late-waking-owner bug. A process that has since lost the reservation
    must not be able to write COMMITTED over the new owner's work."""
    clock_a = DurableEffectJournal(tmp_path / "e.db", instance_id="a",
                                   lease_s=1, lease_grace_s=0)
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    kw = dict(effect_id=eid, tool_id=TOOL, surface="native",
              durability_class=EffectDurabilityClass.IDEMPOTENT,
              tool_input=ARGS)
    clock_a.reserve(**kw)
    clock_a.mark_executing(eid)

    import time as _t
    _t.sleep(1.1)
    b = DurableEffectJournal(tmp_path / "e.db", instance_id="b",
                             lease_s=60, lease_grace_s=0)
    taken = b.reserve(**kw)
    assert taken.owned and taken.record.owner_instance_id == "b"

    # 'a' wakes up and tries to finish. It no longer owns anything, and its
    # write is a silent no-op rather than an exception: a late-waking owner is a
    # race, not a programming mistake, and raising would surface as a failure of
    # a call whose effect already happened.
    assert clock_a.commit(eid, receipt={"stale": True}) is False
    assert b.get(eid).state is EffectState.RESERVED, (
        "a lost owner committed over the new owner's reservation")


def test_a_reclaim_replaces_the_authority_digest(tmp_path):
    """§43/§44 — a durable row must not lend a later attempt an approval it did
    not obtain. The new attempt's digests overwrite, never inherit."""
    a = DurableEffectJournal(tmp_path / "e.db", instance_id="a", lease_s=1,
                             lease_grace_s=0)
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    a.reserve(effect_id=eid, tool_id=TOOL, surface="native",
              durability_class=EffectDurabilityClass.NON_REPLAYABLE,
              tool_input=ARGS, authority_digest="a" * 64, scope_digest="b" * 64,
              approval_digest="c" * 64)
    import time as _t
    _t.sleep(1.1)

    b = DurableEffectJournal(tmp_path / "e.db", instance_id="b", lease_s=60,
                             lease_grace_s=0)
    taken = b.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=EffectDurabilityClass.NON_REPLAYABLE,
                      tool_input=ARGS, authority_digest="d" * 64,
                      scope_digest="e" * 64, approval_digest="")
    assert taken.owned
    record = b.get(eid)
    assert record.authority_digest == "d" * 64
    assert record.scope_digest == "e" * 64
    assert record.approval_digest == "", (
        "a reclaimed reservation inherited the previous attempt's approval")


# ══════════════════════════════════════════════════════════════════════════════
#  MCP GATE HARDENING
# ══════════════════════════════════════════════════════════════════════════════
def test_the_mcp_gate_enforces_authorized_scope(tmp_path, monkeypatch):
    """New in M65C. Before this milestone the MCP gate checked the allowlist,
    filename traversal, LAB_ONLY and HITL — but NOT the operator's authorized
    scope, which the native gate had checked since V63. A hardening nobody tests
    is a hardening that gets removed."""
    from core.authority import AuthorityDecision
    from tools.executor import ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "e.db"))

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    monkeypatch.setattr(
        "tools.executor.authorize_action",
        lambda authority, tool, args: AuthorityDecision(
            allowed=False, reason="target outside the authorized scope",
            target="10.0.0.1"))

    ex = ToolExecutor()
    ex.begin_effect_epoch(SCOPE)
    calls = {"n": 0}

    async def _call(name, args):
        calls["n"] += 1
        return {"lab": "built"}

    result = asyncio.run(ex.aexecute_mcp("generar_laboratorio_red",
                                         {"tema": "vlan"}, _call, "r"))
    assert calls["n"] == 0, "an out-of-scope MCP tool reached the server"
    assert "alcance" in result["error"]


def test_the_recovery_envelope_exposes_only_body_safe_fields(tmp_path,
                                                             monkeypatch):
    """§52 — the envelope is an identity and a disposition, never a payload."""
    from tools.executor import ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "e.db"))

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    ex = ToolExecutor()
    ex.begin_effect_epoch(SCOPE)

    async def _granted(tool_name, preview):
        return True, "test:granted"

    ex._challenge = _granted
    secret = "sk-live-m65c-envelope-secret"
    ex._tool_code_execute = lambda **kw: {"token": secret}

    async def scenario():
        await ex.aexecute(TOOL, dict(ARGS), "r")
        ex._effect_ledger.clear()
        return await ex.aexecute(TOOL, dict(ARGS), "r")

    envelope = asyncio.run(scenario())
    assert envelope["status"] == "recovered"
    assert secret not in json.dumps(envelope)
    assert set(envelope) == {
        "status", "disposition", "effect_id", "tool", "committed_at",
        "receipt_digest", "detail"}, (
        f"the recovery envelope grew fields: {sorted(envelope)}")


# ══════════════════════════════════════════════════════════════════════════════
#  MUTATION-CAMPAIGN CLOSURES
# ══════════════════════════════════════════════════════════════════════════════
def test_a_reclaimed_stale_reservation_executes_through_the_executor(tmp_path,
                                                                     monkeypatch):
    """Closes `auth-recovery-widened-past-committed`.

    Every executor-level test reached either OWNED or ALREADY_COMMITTED, so a
    mutation widening the recovery branch from "already committed" to "anything
    but a fresh reservation" changed nothing observable. RECLAIMED is the third
    outcome: the previous owner died BEFORE the tool ran, so the effect must
    actually EXECUTE, not come back as a recovered receipt.
    """
    from tools.executor import ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("JARVIS_EFFECT_LEASE_S", "1")
    monkeypatch.setenv("JARVIS_EFFECT_LEASE_GRACE_S", "0")

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)

    # A dead owner leaves a RESERVED row and never reaches the tool.
    dead = DurableEffectJournal(tmp_path / "e.db", instance_id="dead",
                                lease_s=1, lease_grace_s=0)
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    dead.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                 durability_class=EffectDurabilityClass.NON_REPLAYABLE,
                 tool_input=ARGS)
    import time as _t
    _t.sleep(1.1)

    ex = ToolExecutor()
    ex.begin_effect_epoch(SCOPE)

    async def _granted(tool_name, preview):
        return True, "test:granted"

    ex._challenge = _granted
    calls = {"n": 0}
    ex._tool_code_execute = lambda **kw: calls.__setitem__("n", calls["n"] + 1) or {"ok": 1}

    note: dict = {}
    asyncio.run(ex.aexecute(TOOL, dict(ARGS), "r", effect_note=note))
    assert calls["n"] == 1, (
        "a reclaimed PRE-EFFECT reservation was treated as already committed")
    assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value


def test_the_doctor_reports_counters_not_records(tmp_path, monkeypatch):
    """Closes `priv-doctor-leaks-arguments`.

    The journal holds only digests, so embedding the whole status dict in a
    finding leaks no secret and no content assertion can see it. The property
    that matters is that a finding is a SENTENCE for an operator, not a dump —
    so the shape is asserted: bounded, and carrying no record structure.
    """
    from core.runtime_doctor import check_effect_journal

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "e.db"))
    journal = DurableEffectJournal(tmp_path / "e.db")
    journal.reserve(effect_id=compute_effect_id(
        surface="native", tool_id=TOOL, identity_scope=SCOPE, tool_input=ARGS),
        tool_id=TOOL, surface="native",
        durability_class=EffectDurabilityClass.NON_REPLAYABLE, tool_input=ARGS)

    for finding in check_effect_journal():
        assert len(finding.evidence) < 400, (
            f"{finding.check_id} evidence is a dump, not a sentence")
        for token in ("effect_id", "canonical_args_digest", "owner_instance_id",
                      "'by_state'", "counters'"):
            assert token not in finding.evidence, (
                f"{finding.check_id} leaked record structure: {token}")


def _receipt_from_a_real_execution(tmp_path, monkeypatch, *, twice: bool):
    """Drive the REAL SpecialistExecutor tool path and return its result.

    The setup mirrors the M65B cross-specialist proof, because the gates it has
    to clear are the same ones: the registry ceiling (FORGE is L1 by default and
    the registry wins over the request), a HITL approval bound to the effect
    identity, and the preflight, which refuses a tool call carrying no
    hypothesis.
    """
    import dataclasses

    from core.cognitive_mesh import REGISTRY
    from core.model_role_router import ModelRoleRouter
    from core.security_effects import SCOPES
    from core.specialist_execution import (
        HitlApproval, HitlApprovalRegistry, SpecialistExecutionRequest,
        SpecialistExecutor, ToolIntent,
    )
    from tools.executor import ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "e.db"))
    SCOPES.scopes = []

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)

    record = REGISTRY.get(SpecialistId.FORGE)
    monkeypatch.setitem(REGISTRY._by_id, SpecialistId.FORGE,
                        dataclasses.replace(
                            record, default_autonomy=AutonomyLevel.HITL_EXECUTE))

    executor = ToolExecutor()
    executor.begin_effect_epoch(SCOPE)

    async def _granted(tool_name, preview):
        return True, "test:granted"

    executor._challenge = _granted
    executor._tool_code_execute = lambda **kw: {"stdout": "1"}

    # Reusable, deliberately: a single-use approval would refuse the SECOND call
    # before the ledger was ever consulted, and the dedupe assertion would pass
    # while proving nothing.
    from datetime import datetime, timedelta, timezone

    approvals = HitlApprovalRegistry()
    approvals.grant(HitlApproval(
        approval_id="appr:forge", specialist_id=SpecialistId.FORGE,
        effect_identity=ToolIntent(tool=TOOL, tool_input=ARGS)
        .effect_identity(SCOPE),
        single_use=False,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        reason="test-only bound approval"))

    intent = json.dumps({"tool": TOOL, "tool_input": ARGS,
                         "why": "the objective needs it",
                         "hypothesis": "the bounded change applies cleanly"})

    async def _infer(system, user, **kw):
        return "Analysis: one action settles this.\nTOOL_INTENT: " + intent

    engine = SpecialistExecutor(
        infer=_infer, tool_executor=executor, scopes=SCOPES, approvals=approvals,
        role_router=ModelRoleRouter(availability=ModelRoleRouter.probe()))

    def _request(n):
        return SpecialistExecutionRequest(
            execution_id=f"exec:{n}", plan_id="plan:m65c",
            specialist_id=SpecialistId.FORGE,
            objective="Apply the bounded change.",
            autonomy_level=AutonomyLevel.HITL_EXECUTE,
            allowed_tools=frozenset({TOOL}), effect_epoch=SCOPE)

    async def _drive():
        first = await engine.run(_request(1))
        if not twice:
            return first
        return await engine.run(_request(2))

    return asyncio.run(_drive())


def test_a_deduplicated_execution_reports_a_deduplicated_disposition(
        tmp_path, monkeypatch):
    """Closes `disp-inferred-from-status`.

    Every disposition assertion until now built a ToolReceipt by hand, so a
    mutation replacing the COPY with a derivation from the status changed
    nothing any test could see. This drives the real specialist path twice: the
    second call succeeds AND is a duplicate, which is exactly the pair a
    status-derived disposition cannot express.
    """
    result = _receipt_from_a_real_execution(tmp_path, monkeypatch, twice=True)
    receipts = [r for r in result.tool_receipts if r.tool == TOOL]
    assert receipts, "the specialist path executed no tool"
    receipt = receipts[0]
    assert receipt.status is ToolCallStatus.SUCCESS
    assert receipt.disposition == \
        ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value, (
        f"a successful duplicate reported {receipt.disposition!r}")
    assert receipt.disposition != ExecutionDisposition.EXECUTED_NOW.value


def test_a_first_execution_reports_executed_now(tmp_path, monkeypatch):
    """The positive control for the test above."""
    result = _receipt_from_a_real_execution(tmp_path, monkeypatch, twice=False)
    receipts = [r for r in result.tool_receipts if r.tool == TOOL]
    assert receipts and receipts[0].disposition == \
        ExecutionDisposition.EXECUTED_NOW.value


def test_an_indeterminate_execution_marks_recovery_required(tmp_path,
                                                            monkeypatch):
    """Closes `disp-recovery-required-dropped`.

    Drives the real specialist path against an identity the journal has already
    classified INDETERMINATE, and asserts the flag survives all the way to the
    receipt — and from there stops ARGUS verifying.
    """
    from core.effect_journal import DurableEffectJournal as _J

    journal = _J(tmp_path / "e.db")
    eid = compute_effect_id(surface="native", tool_id=TOOL,
                            identity_scope=SCOPE, tool_input=ARGS)
    journal.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                    durability_class=EffectDurabilityClass.NON_REPLAYABLE,
                    tool_input=ARGS)
    journal.mark_executing(eid)
    journal.mark_indeterminate(eid, "owner vanished")
    journal.close()

    result = _receipt_from_a_real_execution(tmp_path, monkeypatch, twice=False)
    receipts = [r for r in result.tool_receipts if r.tool == TOOL]
    assert receipts, "the specialist path produced no receipt"
    assert receipts[0].recovery_required is True, (
        "an INDETERMINATE effect did not reach the receipt")
    assert result.indeterminate_effects == 1
    assert not result.verified, (
        "ARGUS verified an execution whose effect outcome is unknown")
