"""
tests/test_durable_effect_protocol_v69_m65c.py — V69 M65C DURABLE EFFECT
PROTOCOL, at the ``ToolExecutor`` boundary.

The journal's own file proves the STORE is correct. This file proves the
EXECUTOR uses it: that both surfaces route through one protocol, that a gate
refusal is recorded as provably pre-effect while a lost owner is not, that
dispositions are stated rather than inferred, and that the MCP path no longer
carries the weaker semantics M65B left behind.

Cross-process and crash behaviour live in
``test_effect_journal_crash_recovery_v69_m65c.py`` — a store that is correct in
one process proves nothing about two.
"""
from __future__ import annotations

import asyncio

import pytest

from core.effect_journal import (
    DurableEffectJournal,
    EffectDurabilityClass,
    EffectState,
    ExecutionDisposition,
    JournalUnhealthy,
    ReconciliationVerdict,
    compute_effect_id,
    register_durability,
    register_reconciler,
    unregister_durability,
)

TOOL = "code_execute"
ARGS = {"code": "print(1)"}
MCP_TOOL = "generar_laboratorio_red"
MCP_ARGS = {"tema": "vlan"}
DEADLINE_S = 5.0
EPOCH = "turn:m65c-protocol"


class Harness:
    """The REAL ToolExecutor with a counting handler and a per-test journal."""

    def __init__(self, tmp_path, *, journal=None, instance_id="inst-a"):
        from tools.executor import ToolExecutor

        self.journal = journal if journal is not None else DurableEffectJournal(
            tmp_path / "effects.db", instance_id=instance_id)
        self.executor = ToolExecutor(journal=self.journal)
        self.executor.begin_effect_epoch(EPOCH)
        self.effects: dict[str, int] = {}
        self.mcp_calls: dict[str, int] = {}

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
        return {"lab": "built"}

    def count(self, name=TOOL) -> int:
        return self.effects.get(name, 0)

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
#  SAME_PROCESS_COMPATIBILITY — M64.1/M65B behaviour must be unchanged
# ══════════════════════════════════════════════════════════════════════════════
def test_one_effect_runs_once_and_is_journalled(h):
    h.add_tool()
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert h.count() == 1
    assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert note["committed"] is True and note["durable"] is True
    assert h.journal.get(h.effect_id()).state is EffectState.COMMITTED


def test_a_sequential_repeat_is_deduplicated_in_process(h):
    h.add_tool()
    note: dict = {}

    async def scenario():
        await h.call()
        return await h.call(note=note)

    asyncio.run(scenario())
    assert h.count() == 1
    assert note["disposition"] == ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value
    assert note["deduplicated"] is True


def test_a_read_only_call_is_never_journalled(h):
    """§20 — the journal is an effect mechanism. Repeating a read is not an
    effect and must not cost a disk write or a reservation."""

    def _handler(**kwargs):
        h.effects["read"] = h.effects.get("read", 0) + 1
        return {"ok": True}

    h.executor._tool_get_system_status = _handler

    async def scenario():
        await h.executor.aexecute("get_system_status", {}, "r")
        await h.executor.aexecute("get_system_status", {}, "r")

    asyncio.run(scenario())
    assert h.effects["read"] == 2, "a read was deduplicated"
    assert h.journal.status()["total"] == 0, "a read reached the journal"


def test_a_gate_refusal_is_recorded_as_proven_pre_effect(h):
    """A denied HITL challenge never invoked anything, so the identity is
    FAILED_BEFORE_EFFECT and a later attempt is legitimate."""
    h.add_tool()

    async def _denied(tool_name, preview):
        return False, "test:denied"

    h.executor._challenge = _denied
    note: dict = {}
    result = asyncio.run(h.call(note=note))
    assert "error" in result and h.count() == 0
    assert note["disposition"] == ExecutionDisposition.FAILED_BEFORE_EFFECT.value
    assert h.journal.get(h.effect_id()).state is EffectState.FAILED_BEFORE_EFFECT


def test_a_refused_effect_can_be_attempted_again(h):
    h.add_tool()
    verdicts = [False, True]

    async def _challenge(tool_name, preview):
        return (verdicts.pop(0), "test")

    h.executor._challenge = _challenge

    async def scenario():
        await h.call()
        return await h.call()

    asyncio.run(scenario())
    assert h.count() == 1, "a pre-effect refusal poisoned the identity"


def test_an_observed_failure_is_not_marked_committed(h):
    """§8 — the journal represents reality. A tool that returned an error is
    FAILED_OBSERVED, which is neither 'proven done' nor 'proven not done'."""
    h.add_tool(fails=True)
    note: dict = {}
    asyncio.run(h.call(note=note))
    assert note["committed"] is False and note.get("observed_failure") is True
    assert h.journal.get(h.effect_id()).state is EffectState.FAILED_OBSERVED


def test_an_observed_failure_can_be_retried(h):
    """The M64.1 policy, inherited unchanged and named as a limitation."""
    outcomes = [True, False]
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        return {"error": "boom"} if outcomes.pop(0) else {"stdout": "ok"}

    h.executor._tool_code_execute = _handler

    async def scenario():
        await h.call()
        h.executor._effect_ledger.clear()
        await h.call()

    asyncio.run(scenario())
    assert calls["n"] == 2


def test_the_disposition_is_stated_not_inferred_from_a_counter(h):
    """§18 — M65A sampled the ledger count before and after, which is wrong the
    instant two callers overlap. Both concurrent callers here must report the
    branch they actually took."""
    h.add_tool()
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _parked(tool_name, preview):
        entered.set()
        await asyncio.wait_for(gate.wait(), timeout=DEADLINE_S)
        return True, "test:granted"

    h.executor._challenge = _parked
    owner_note: dict = {}
    dup_note: dict = {}

    async def scenario():
        owner = asyncio.create_task(h.call(note=owner_note))
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE_S)
        dup = asyncio.create_task(h.call(note=dup_note))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(owner, dup), timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.count() == 1
    assert owner_note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert dup_note["disposition"] == \
        ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value


def test_the_ordering_is_journal_first_then_tool(h):
    """§42 — identify, consult durable state, then decide whether the tool may
    run. Never run and then check."""
    seen: list[str] = []

    def _handler(**kwargs):
        seen.append("tool")
        return {"ok": True}

    h.executor._tool_code_execute = _handler
    real_reserve = h.journal.reserve

    def _spy(**kw):
        seen.append("reserve")
        return real_reserve(**kw)

    h.journal.reserve = _spy
    asyncio.run(h.call())
    assert seen == ["reserve", "tool"]


def test_executing_is_durable_before_the_tool_is_invoked(h):
    """The ordering that makes recovery possible at all. If EXECUTING were
    written after the call, every crash would look like 'never ran'."""
    observed: list[str] = []

    def _handler(**kwargs):
        observed.append(h.journal.get(h.effect_id()).state.value)
        return {"ok": True}

    h.executor._tool_code_execute = _handler
    asyncio.run(h.call())
    assert observed == ["EXECUTING"]


def test_a_journal_that_refuses_executing_aborts_the_call(h):
    """A tool must never run unrecorded: if the journal cannot mark EXECUTING,
    the crash that follows would be unrecoverable."""
    h.add_tool()
    h.journal.mark_executing = lambda effect_id: False
    note: dict = {}
    result = asyncio.run(h.call(note=note))
    assert h.count() == 0, "the tool ran without a durable EXECUTING record"
    assert result["error_class"] == "journal_unhealthy"
    assert note["disposition"] == ExecutionDisposition.FAILED_BEFORE_EFFECT.value
    assert h.journal.get(h.effect_id()).state is EffectState.FAILED_BEFORE_EFFECT


# ══════════════════════════════════════════════════════════════════════════════
#  MCP_EXECUTION — §19/§62, the path M65B left weaker
# ══════════════════════════════════════════════════════════════════════════════
def test_an_mcp_effect_is_journalled(h):
    note: dict = {}
    asyncio.run(h.call_mcp(note=note))
    assert h.mcp_calls[MCP_TOOL] == 1
    assert note["durable"] is True
    assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    record = h.journal.get(h.effect_id(MCP_TOOL, MCP_ARGS, surface="mcp"))
    assert record is not None and record.state is EffectState.COMMITTED
    assert record.surface == "mcp"


def test_two_concurrent_mcp_callers_produce_one_effect(h):
    """THE M65B gap. Before this milestone the MCP path read the ledger, awaited
    a challenge and an RPC, then wrote the ledger — so two concurrent callers
    both saw an empty ledger and both ran."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _parked(tool_name, preview):
        entered.set()
        await asyncio.wait_for(gate.wait(), timeout=DEADLINE_S)
        return True, "test:granted"

    h.executor._challenge = _parked

    async def scenario():
        owner = asyncio.create_task(h.call_mcp())
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE_S)
        dup = asyncio.create_task(h.call_mcp())
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.wait_for(asyncio.gather(owner, dup),
                                      timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.mcp_calls[MCP_TOOL] == 1, "the MCP path still races"


def test_an_mcp_duplicate_receives_the_owners_result(h):
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _parked(tool_name, preview):
        entered.set()
        await asyncio.wait_for(gate.wait(), timeout=DEADLINE_S)
        return True, "test:granted"

    h.executor._challenge = _parked
    dup_note: dict = {}

    async def scenario():
        owner = asyncio.create_task(h.call_mcp())
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE_S)
        dup = asyncio.create_task(h.call_mcp(note=dup_note))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.wait_for(asyncio.gather(owner, dup),
                                      timeout=DEADLINE_S)

    results = asyncio.run(scenario())
    assert results[0] == results[1]
    assert dup_note["deduplicated"] is True


def test_both_surfaces_use_the_same_protocol_implementation(h):
    """§19 — there must be ONE implementation, not two kept in step by hand."""
    from tools.executor import ToolExecutor
    import inspect

    native = inspect.getsource(ToolExecutor.aexecute)
    mcp = inspect.getsource(ToolExecutor.aexecute_mcp)
    for body in (native, mcp):
        assert "_execute_effect_protocol" in body
    # The journal vocabulary must appear in the shared protocol and NOT be
    # duplicated into either surface.
    for body in (native, mcp):
        assert "journal.reserve" not in body
        assert "mark_executing" not in body


def test_an_mcp_refusal_is_proven_pre_effect(h):
    """A non-allowlisted MCP tool never reaches the RPC."""
    note: dict = {}

    async def scenario():
        return await h.executor.aexecute_mcp(
            "generar_laboratorio_red", {"tema": "x"}, h.mcp_call, "r",
            effect_note=note)

    # Allowlisted but denied at the challenge.
    async def _denied(tool_name, preview):
        return False, "test:denied"

    h.executor._challenge = _denied
    result = asyncio.run(scenario())
    assert "error" in result and not h.mcp_calls
    assert note["disposition"] == ExecutionDisposition.FAILED_BEFORE_EFFECT.value


def test_the_native_and_mcp_surfaces_have_distinct_identities(h):
    """Documented boundary: a local handler and a remote server are different
    code, so fusing their identities would claim an equivalence nothing has
    established."""
    assert h.effect_id(TOOL, ARGS, surface="native") != \
        h.effect_id(TOOL, ARGS, surface="mcp")


def test_one_journal_serves_both_surfaces(h):
    h.add_tool()

    async def scenario():
        await h.call()
        await h.call_mcp()

    asyncio.run(scenario())
    assert h.journal.status()["committed"] == 2


# ══════════════════════════════════════════════════════════════════════════════
#  INDETERMINATE + RECOVERY at the executor boundary
# ══════════════════════════════════════════════════════════════════════════════
def test_a_committed_identity_is_recovered_not_re_executed(h):
    """§17 — the P4 window, in one process. The ledger is cleared to simulate
    the RAM that a restart would not have."""
    h.add_tool()
    note: dict = {}

    async def scenario():
        await h.call()
        h.executor._effect_ledger.clear()
        h.executor._effect_inflight.clear()
        return await h.call(note=note)

    result = asyncio.run(scenario())
    assert h.count() == 1, "a committed effect was executed again"
    assert note["disposition"] == ExecutionDisposition.RECOVERED_COMMITTED.value
    assert result["status"] == "recovered"
    assert result["receipt_digest"]


def test_the_recovery_envelope_carries_no_result_body(h):
    """§52 — the journal keeps a digest, not the response."""
    h.add_tool(result={"token": "sk-live-m65c-not-persisted"})

    async def scenario():
        await h.call()
        h.executor._effect_ledger.clear()
        return await h.call()

    result = asyncio.run(scenario())
    assert "sk-live-m65c-not-persisted" not in str(result)
    assert "not retained" in result["detail"]


def test_an_indeterminate_identity_blocks_and_does_not_retry(h):
    """§12/§61 — the absolute rule at the caller boundary."""
    h.add_tool()
    eid = h.effect_id()
    h.journal.reserve(effect_id=eid, tool_id=TOOL, surface="native",
                      durability_class=EffectDurabilityClass.NON_REPLAYABLE,
                      tool_input=ARGS)
    h.journal.mark_executing(eid)
    h.journal.mark_indeterminate(eid, "owner vanished")

    note: dict = {}
    result = asyncio.run(h.call(note=note))
    assert h.count() == 0, "an INDETERMINATE effect was automatically retried"
    assert result["error_class"] == "indeterminate_effect"
    assert note["disposition"] == ExecutionDisposition.BLOCKED_INDETERMINATE.value
    assert note["recovery_required"] is True


def test_a_reconcilable_effect_confirmed_committed_is_recovered(h):
    """§16 — restart, reconcile, no replay."""
    tool = "m65c_reconcilable"
    try:
        register_durability(tool, EffectDurabilityClass.RECONCILABLE)
        register_reconciler(
            tool, lambda eid, key: ReconciliationVerdict.CONFIRMED_COMMITTED)
        calls = {"n": 0}

        def _handler(**kwargs):
            calls["n"] += 1
            return {"ok": True}

        h.executor._tool_m65c_reconcilable = _handler
        eid = compute_effect_id(surface="native", tool_id=tool,
                                identity_scope=EPOCH, tool_input=ARGS)
        h.journal.reserve(effect_id=eid, tool_id=tool, surface="native",
                          durability_class=EffectDurabilityClass.RECONCILABLE,
                          tool_input=ARGS)
        h.journal.mark_executing(eid)
        h.journal.mark_indeterminate(eid, "owner vanished")

        note: dict = {}
        result = asyncio.run(h.executor.aexecute(tool, dict(ARGS), "r",
                                                 effect_note=note))
        assert calls["n"] == 0, "a reconciled-committed effect was replayed"
        assert note["disposition"] == \
            ExecutionDisposition.RECONCILED_COMMITTED.value
        assert result["status"] == "recovered"
    finally:
        unregister_durability(tool)


def test_a_reconcilable_effect_confirmed_absent_may_run(h):
    tool = "m65c_reconcilable_absent"
    try:
        register_durability(tool, EffectDurabilityClass.RECONCILABLE)
        register_reconciler(
            tool, lambda eid, key: ReconciliationVerdict.CONFIRMED_NOT_EXECUTED)
        calls = {"n": 0}

        def _handler(**kwargs):
            calls["n"] += 1
            return {"ok": True}

        setattr(h.executor, f"_tool_{tool}", _handler)
        eid = compute_effect_id(surface="native", tool_id=tool,
                                identity_scope=EPOCH, tool_input=ARGS)
        h.journal.reserve(effect_id=eid, tool_id=tool, surface="native",
                          durability_class=EffectDurabilityClass.RECONCILABLE,
                          tool_input=ARGS)
        h.journal.mark_executing(eid)
        h.journal.mark_indeterminate(eid, "owner vanished")

        note: dict = {}
        asyncio.run(h.executor.aexecute(tool, dict(ARGS), "r", effect_note=note))
        assert calls["n"] == 1, "a proven-absent effect was refused"
        assert note["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    finally:
        unregister_durability(tool)


def test_a_reconciler_answering_unknown_stays_blocked(h):
    """§15 — UNKNOWN is never rounded to either certainty."""
    tool = "m65c_reconcilable_unknown"
    try:
        register_durability(tool, EffectDurabilityClass.RECONCILABLE)
        register_reconciler(tool, lambda eid, key: ReconciliationVerdict.UNKNOWN)
        calls = {"n": 0}

        def _handler(**kwargs):
            calls["n"] += 1
            return {"ok": True}

        setattr(h.executor, f"_tool_{tool}", _handler)
        eid = compute_effect_id(surface="native", tool_id=tool,
                                identity_scope=EPOCH, tool_input=ARGS)
        h.journal.reserve(effect_id=eid, tool_id=tool, surface="native",
                          durability_class=EffectDurabilityClass.RECONCILABLE,
                          tool_input=ARGS)
        h.journal.mark_executing(eid)
        h.journal.mark_indeterminate(eid, "owner vanished")

        note: dict = {}
        result = asyncio.run(h.executor.aexecute(tool, dict(ARGS), "r",
                                                 effect_note=note))
        assert calls["n"] == 0, "an UNKNOWN reconciliation permitted a replay"
        assert result["error_class"] == "indeterminate_effect"
        assert h.journal.get(eid).state is EffectState.INDETERMINATE
    finally:
        unregister_durability(tool)


# ══════════════════════════════════════════════════════════════════════════════
#  CANCELLATION (§41)
# ══════════════════════════════════════════════════════════════════════════════
def test_cancelling_before_the_effect_leaves_no_poison(h):
    h.add_tool()
    entered = asyncio.Event()

    async def _parked(tool_name, preview):
        entered.set()
        await asyncio.sleep(3600)
        return True, "test"

    h.executor._challenge = _parked

    async def scenario():
        task = asyncio.create_task(h.call())
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.journal.get(h.effect_id()).state is \
            EffectState.FAILED_BEFORE_EFFECT
        # And a later caller may legitimately run it.
        h.executor._challenge = _granted_challenge
        await asyncio.wait_for(h.call(), timeout=DEADLINE_S)

    asyncio.run(scenario())
    assert h.count() == 1


async def _granted_challenge(tool_name, preview):
    return True, "test:granted"


def test_cancelling_after_the_effect_records_indeterminate(h):
    """§41 — a caller changing its mind is not evidence about the world. The
    handler is on a thread pool and keeps going, so the outcome is unknown."""
    started = asyncio.Event()
    release = asyncio.Event()
    loop_box: dict = {}

    def _handler(**kwargs):
        h.effects[TOOL] = h.effects.get(TOOL, 0) + 1
        loop_box["loop"].call_soon_threadsafe(started.set)
        import time as _t
        _t.sleep(0.2)
        return {"ok": True}

    h.executor._tool_code_execute = _handler
    h.executor._challenge = _granted_challenge

    async def scenario():
        loop_box["loop"] = asyncio.get_running_loop()
        task = asyncio.create_task(h.call())
        await asyncio.wait_for(started.wait(), timeout=DEADLINE_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(scenario())
    record = h.journal.get(h.effect_id())
    assert record.state is EffectState.INDETERMINATE, (
        "a cancellation after the tool ran claimed the effect never happened")


def test_a_cancelled_committed_record_is_not_erased(h):
    """§41 — never delete a committed record because the caller gave up."""
    h.add_tool()
    h.executor._challenge = _granted_challenge
    asyncio.run(h.call())
    eid = h.effect_id()
    assert h.journal.get(eid).state is EffectState.COMMITTED
    # A later cancellation of a NEW attempt cannot rewrite the committed row.
    with pytest.raises(Exception):
        h.journal.fail_before_effect(eid, "cancelled")
    assert h.journal.get(eid).state is EffectState.COMMITTED


# ══════════════════════════════════════════════════════════════════════════════
#  JOURNAL HEALTH AT THE BOUNDARY (§25)
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unhealthy_journal_refuses_the_effect(h):
    """Fail closed. Proceeding without the component that prevents duplication
    would drop the guarantee at the exact moment it matters."""
    h.add_tool()

    def _boom(**kw):
        raise JournalUnhealthy("the journal is corrupt")

    h.journal.reserve = _boom
    result = asyncio.run(h.call())
    assert result["error_class"] == "journal_unhealthy"
    assert h.count() == 0, "an effect ran with an unhealthy journal"


def test_a_disabled_journal_says_so_rather_than_implying_durability(tmp_path,
                                                                    monkeypatch):
    """§48 — the switch exists so an operator with a broken journal can keep
    working deliberately, not so a code path can quietly opt out."""
    from tools.executor import ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "0")

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    ex = ToolExecutor()
    ex.begin_effect_epoch(EPOCH)
    ex._challenge = _granted_challenge
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        return {"ok": True}

    ex._tool_code_execute = _handler
    note: dict = {}
    asyncio.run(ex.aexecute(TOOL, dict(ARGS), "r", effect_note=note))
    assert calls["n"] == 1
    assert note["durable"] is False, "a disabled journal claimed durability"


def test_the_journal_records_the_authority_it_was_reserved_under(h):
    """§43/§44 — the journal BINDS an authority identity so a later attempt can
    be compared against it. It never becomes a place to read one back out."""
    h.add_tool()
    asyncio.run(h.call())
    record = h.journal.get(h.effect_id())
    assert record.authority_digest and len(record.authority_digest) == 64
    assert record.scope_digest and len(record.scope_digest) == 64
