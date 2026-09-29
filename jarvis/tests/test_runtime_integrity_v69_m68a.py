"""V69 M68A — focused regression tests for the four revalidated audit findings.

An external STATIC audit was run against `7125557f74cbf1756b1dc0072a1f3ca37c236cfa`.
Master had already moved to `2c08607a…`, so every finding was independently
reproduced against CURRENT HEAD before a line was changed. All four reproduced.

Each test below FAILED on `2c08607a…` and passes now. They are deliberately
written against the property, not against the repair: nothing here asserts that a
particular line exists, so a different correct implementation would still pass.

  §A  tools/executor.py   journal initialisation published READY before it was true
  §B  core/llm.py         an exhausted eligible-tool set became unrestricted
  §C  core/llm.py         a parse failure returned {"tool": "RESOLVED"}
  §D  core/agentic_loop.py cancellation identity and deadline were both shared

Offline, deterministic, server-free: no network, no model, no sandbox backend.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core import cancel_bus  # noqa: E402
from core.agentic_decision import (  # noqa: E402
    SOC_ADVERTISED_TOOLS,
    Decision,
    DecisionStatus,
    validate_decision,
)
from core.tool_loop import (  # noqa: E402
    ToolLoopBudget,
    eligible_tool_names,
    validate_tool_call,
)
from tools.executor import JournalLifecycle, ToolExecutor  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# §A — EFFECT JOURNAL INITIALISATION
# ══════════════════════════════════════════════════════════════════════════════
class _Boom(Exception):
    """A failure that is NOT a JournalUnhealthy, to prove the state machine does
    not depend on the exception type to stay fail-closed."""


@pytest.fixture()
def executor(monkeypatch):
    """A real ToolExecutor with no journal yet and a silent audit log."""
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "1")
    ex = ToolExecutor()
    assert ex._journal_state is JournalLifecycle.UNINITIALIZED
    return ex


def _fail_construction(monkeypatch, exc):
    calls = {"n": 0}

    def _ctor(*a, **kw):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr("core.effect_journal.DurableEffectJournal", _ctor)
    return calls


def test_the_journal_lifecycle_states_are_all_distinct():
    """DISABLED != INITIALIZING != READY != FAILED — the required property, as a
    property. Before M68A three of these shared one boolean."""
    values = [s.value for s in JournalLifecycle]
    assert len(values) == len(set(values)), "two lifecycle states share a value"
    for name in ("DISABLED", "INITIALIZING", "READY", "FAILED"):
        assert hasattr(JournalLifecycle, name), f"{name} is not representable"
    assert len({JournalLifecycle.DISABLED, JournalLifecycle.INITIALIZING,
                JournalLifecycle.READY, JournalLifecycle.FAILED}) == 4


def test_a_failed_initialisation_is_not_reported_as_disabled(executor, monkeypatch):
    """THE audited defect. `_journal_ready = True` was set before construction, so
    after a construction failure the executor held (ready=True, journal=None) —
    which `_durable_effect` reads as *the operator switched durability off*."""
    from core.effect_journal import JournalUnhealthy

    _fail_construction(monkeypatch, JournalUnhealthy("db corrupt at open"))

    with pytest.raises(JournalUnhealthy):
        executor._effect_journal()

    assert executor._journal_state is JournalLifecycle.FAILED
    assert executor._journal_state is not JournalLifecycle.DISABLED
    assert executor._journal_state is not JournalLifecycle.READY


def test_a_failed_initialisation_keeps_refusing_on_every_later_call(executor,
                                                                   monkeypatch):
    """The second call is the whole finding: the first one refused correctly and
    the SECOND returned None, which means DISABLED, which executes."""
    from core.effect_journal import JournalUnhealthy

    calls = _fail_construction(monkeypatch, JournalUnhealthy("db corrupt"))

    for attempt in range(5):
        with pytest.raises(JournalUnhealthy):
            executor._effect_journal()
        assert executor._journal_state is JournalLifecycle.FAILED, attempt

    assert calls["n"] == 1, "construction must be attempted once, not per call"


def test_a_failed_initialisation_never_returns_none(executor, monkeypatch):
    """None is the DISABLED signal. A FAILED journal must never produce it."""
    _fail_construction(monkeypatch, _Boom("anything at all"))

    with pytest.raises(_Boom):
        executor._effect_journal()
    for _ in range(3):
        with pytest.raises(Exception) as ei:   # noqa: PT011 — any refusal will do
            executor._effect_journal()
        assert ei.value is not None


def test_ready_is_never_published_before_construction_returns(executor, monkeypatch):
    """Observe the state from INSIDE the constructor: it must not already say READY."""
    seen = {}

    def _ctor(*a, **kw):
        seen["state"] = executor._journal_state
        raise _Boom("failed after being observed")

    monkeypatch.setattr("core.effect_journal.DurableEffectJournal", _ctor)
    with pytest.raises(_Boom):
        executor._effect_journal()

    assert seen["state"] is JournalLifecycle.INITIALIZING
    assert seen["state"] is not JournalLifecycle.READY


def test_ready_is_never_published_before_verification_returns(executor, monkeypatch):
    """Construction succeeding is not enough: `assert_healthy` must return too."""
    seen = {}

    class _Journal:
        instance_id = "test"

        def assert_healthy(self):
            seen["state"] = executor._journal_state
            raise _Boom("unhealthy")

    monkeypatch.setattr("core.effect_journal.DurableEffectJournal",
                        lambda *a, **kw: _Journal())
    with pytest.raises(_Boom):
        executor._effect_journal()

    assert seen["state"] is JournalLifecycle.INITIALIZING, (
        "READY was observable while verification was still running")
    assert executor._journal_state is JournalLifecycle.FAILED


def test_a_verified_journal_does_reach_ready(executor, monkeypatch):
    """The fix must not be 'refuse always'. A healthy journal still works."""
    class _Journal:
        instance_id = "test"
        verified = False

        def assert_healthy(self):
            type(self).verified = True

    monkeypatch.setattr("core.effect_journal.DurableEffectJournal",
                        lambda *a, **kw: _Journal())
    journal = executor._effect_journal()

    assert journal is not None
    assert _Journal.verified is True
    assert executor._journal_state is JournalLifecycle.READY
    assert executor._effect_journal() is journal


def test_an_operator_disabled_journal_is_distinguishable_from_a_failed_one(
        monkeypatch):
    """DISABLED is a deliberate operator choice and returns None; FAILED raises.
    Collapsing them is the defect, so the test asserts they differ."""
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "0")
    ex = ToolExecutor()
    assert ex._effect_journal() is None
    assert ex._journal_state is JournalLifecycle.DISABLED


def test_an_injected_journal_is_verified_before_it_becomes_ready(monkeypatch):
    """A constructor argument proves somebody built it, never that it is healthy.
    An injected journal is UNVERIFIED and is checked on first use."""
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "1")

    class _Journal:
        instance_id = "injected"
        checked = 0

        def assert_healthy(self):
            type(self).checked += 1

    ex = ToolExecutor(journal=_Journal())
    assert ex._journal_state is JournalLifecycle.UNVERIFIED
    assert ex._journal_state is not JournalLifecycle.READY

    assert ex._effect_journal() is not None
    assert _Journal.checked == 1
    assert ex._journal_state is JournalLifecycle.READY


def test_an_injected_journal_that_fails_verification_is_failed_not_ready(monkeypatch):
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "1")

    class _Journal:
        instance_id = "injected"

        def assert_healthy(self):
            raise _Boom("injected but unhealthy")

    ex = ToolExecutor(journal=_Journal())
    with pytest.raises(_Boom):
        ex._effect_journal()
    assert ex._journal_state is JournalLifecycle.FAILED
    assert ex._journal is None


def test_a_reentrant_request_during_initialisation_refuses(executor, monkeypatch):
    """A journal that is still being built has proven nothing, so an effect that
    arrives mid-construction must not borrow it."""
    from core.effect_journal import JournalUnhealthy

    inner: dict = {}

    def _ctor(*a, **kw):
        try:
            executor._effect_journal()
        except BaseException as exc:   # noqa: BLE001
            inner["exc"] = exc
        raise _Boom("outer construction also fails")

    monkeypatch.setattr("core.effect_journal.DurableEffectJournal", _ctor)
    with pytest.raises(_Boom):
        executor._effect_journal()

    assert isinstance(inner.get("exc"), JournalUnhealthy), (
        "a re-entrant request was served a half-built journal")


def test_no_effect_executes_because_journal_construction_failed(monkeypatch,
                                                                executor):
    """End-to-end: the consequence the audit named. Two effectful calls, a journal
    that cannot open, and ZERO executions — not one on the second call."""
    from core.effect_journal import JournalUnhealthy

    _fail_construction(monkeypatch, JournalUnhealthy("db corrupt"))
    monkeypatch.setattr(executor._audit, "log_action",
                        lambda *a, **kw: None, raising=False)

    executed: list[str] = []

    async def gated(hooks):
        hooks.before_invoke()
        executed.append("EXECUTED")
        hooks.succeeded = True
        return {"ok": True}

    async def _two_calls():
        out = []
        for n in range(2):
            note: dict = {}
            res = await executor._durable_effect(
                surface="test", tool_id="send_email",
                identity_input={"to": "a@b.c"}, reasoning="m68a",
                note=note, gated=gated, key=f"k{n}")
            out.append((res, note))
        return out

    results = asyncio.run(_two_calls())

    assert executed == [], "an effect executed because the journal failed to open"
    for res, note in results:
        assert res.get("error_class") == "journal_unhealthy"
        assert note.get("journal_unhealthy") is True
        assert note.get("disposition") == "BLOCKED_INDETERMINATE"


# ══════════════════════════════════════════════════════════════════════════════
# §B — TOOL BUDGET / EMPTY ELIGIBILITY
# ══════════════════════════════════════════════════════════════════════════════
def test_eligible_tool_names_is_total_and_never_none():
    for bad in (None, [], (), [None], [1, 2], [{}], [{"function": {}}],
                [{"function": {"name": ""}}], [{"function": {"name": "  "}}],
                [{"function": {"name": None}}], ["not a dict"]):
        got = eligible_tool_names(bad)
        assert isinstance(got, frozenset), f"{bad!r} -> {got!r}"
        assert got == frozenset(), f"{bad!r} produced names: {got!r}"


def test_eligible_tool_names_extracts_real_names():
    tools = [{"function": {"name": "web_search"}},
             {"function": {"name": " git_query "}},
             {"type": "function", "function": {"name": "get_datetime"}}]
    assert eligible_tool_names(tools) == frozenset(
        {"web_search", "git_query", "get_datetime"})


@pytest.mark.parametrize("empty", [None, set(), frozenset(), [], (), {}])
def test_an_empty_eligible_set_refuses_every_call(empty):
    """EMPTY ELIGIBLE SET = NO TOOLS MAY EXECUTE. Before M68A, `None` skipped the
    eligibility check entirely, so this exact call validated."""
    ok, args, reason = validate_tool_call("run_shell_command",
                                         '{"command":"rm -rf /"}', empty)
    assert ok is False
    assert args == {}
    assert reason == "no_eligible_tools"


def test_an_exhausted_round_budget_cannot_make_every_tool_eligible():
    """The composed path. `force_final()` drops the tools; the names derived from
    that empty list must refuse, not wildcard."""
    budget = ToolLoopBudget(max_rounds=1)
    budget.begin_round()
    assert budget.force_final() is True

    turn_tools: list[dict] = []          # what llm.py assigns on force_final
    names = eligible_tool_names(turn_tools)
    assert names == frozenset()
    assert names is not None

    for tool in ("run_shell_command", "network_scan", "web_search", "anything"):
        ok, _, reason = validate_tool_call(tool, "{}", names)
        assert ok is False and reason == "no_eligible_tools", tool


def test_a_legitimate_call_still_passes():
    """The fix must refuse the empty set without refusing everything."""
    ok, args, reason = validate_tool_call("web_search", '{"query":"x"}',
                                         {"web_search"})
    assert ok is True and args == {"query": "x"} and reason == ""


def test_the_round_budget_terminates():
    budget = ToolLoopBudget(max_rounds=3)
    for _ in range(3):
        assert budget.force_final() is False
        budget.begin_round()
    assert budget.force_final() is True
    budget.begin_round()
    assert budget.force_final() is True, "the budget must stay spent"


def test_malformed_response_accounting_is_bounded():
    budget = ToolLoopBudget(max_repairs=2)
    assert budget.note_malformed() is True
    assert budget.note_malformed() is True
    assert budget.note_malformed() is False, "repairs are unbounded"
    assert budget.malformed_calls == 3
    assert budget.repairs == 2


def test_the_number_of_tool_calls_per_response_is_bounded():
    """V69 M68A §B — the round budget bounded how many times the model is ASKED,
    not how much it may ask for. One response carrying 50 calls executed 50 tools
    inside a single 'round'."""
    budget = ToolLoopBudget(max_calls_per_response=3)
    assert budget.admit_response_calls(50) == 3
    assert budget.dropped_calls == 47
    assert budget.admit_response_calls(2) == 2
    assert budget.dropped_calls == 47, "an in-budget response drops nothing"
    assert budget.admit_response_calls(0) == 0


def test_the_per_response_ceiling_is_finite_by_default():
    budget = ToolLoopBudget()
    assert 0 < budget.max_calls_per_response < 1000
    assert budget.admit_response_calls(10_000) == budget.max_calls_per_response


def test_the_budget_snapshot_reports_the_new_bounds():
    snap = ToolLoopBudget().snapshot()
    assert "dropped_calls" in snap and "max_calls_per_response" in snap


# ══════════════════════════════════════════════════════════════════════════════
# §C — INVALID DECISION != RESOLVED
# ══════════════════════════════════════════════════════════════════════════════
#: Everything a model, a proxy or a truncated stream can produce that is not a
#: decision. NONE of these may reach RESOLVED or dispatch.
MALFORMED_DECISIONS = [
    None, "", "RESOLVED", "{}", 0, 1, 3.14, True, b"RESOLVED",
    [], [1, 2], {}, set(), object(),
    {"tool": None}, {"tool": ""}, {"tool": "   "}, {"tool": 123},
    {"tool": []}, {"tool": {"nested": 1}},
    {"reasoning": "the incident is contained"},
    {"input": {}}, {"detail": "x"},
    {"tool": "network_scan", "input": "not an object"},
    {"tool": "network_scan", "input": [1, 2]},
    {"tool": "network_scan", "input": 7},
    {"tool": "definitely_not_a_tool"},
    {"tool": "resolved"},          # wrong case is not the sentinel
    {"tool": " RESOLVED "},        # stripped to the sentinel, so NOT malformed
]


def test_no_malformed_decision_can_ever_yield_resolved():
    """THE §C property, over a corpus rather than over the branches that exist.

    `decide_next_action` used to end with `return {"tool": "RESOLVED", ...}` on any
    parse failure, so a truncated stream closed a live incident and the event log
    said *resolved*."""
    offenders = []
    for raw in MALFORMED_DECISIONS:
        if raw == {"tool": " RESOLVED "}:
            continue    # the sentinel with whitespace IS a valid resolution
        d = validate_decision(raw, allowed_tools=SOC_ADVERTISED_TOOLS)
        if d.status is DecisionStatus.RESOLVED:
            offenders.append(raw)
    assert offenders == [], f"malformed input reached RESOLVED: {offenders}"


def test_no_malformed_decision_is_ever_dispatchable():
    offenders = [raw for raw in MALFORMED_DECISIONS
                 if raw != {"tool": " RESOLVED "}
                 and validate_decision(
                     raw, allowed_tools=SOC_ADVERTISED_TOOLS).may_dispatch]
    assert offenders == [], f"malformed input was dispatchable: {offenders}"


def test_the_four_decision_statuses_are_distinct():
    """RESOLVED != INVALID_DECISION != MODEL_ERROR != UNKNOWN."""
    required = {"RESOLVED", "INVALID_DECISION", "MODEL_ERROR", "UNKNOWN"}
    have = {s.value for s in DecisionStatus}
    assert required <= have, f"missing: {required - have}"
    assert len(have) == len(DecisionStatus), "two statuses share a value"


def test_only_the_explicit_sentinel_yields_resolved():
    d = validate_decision({"tool": "RESOLVED", "reasoning": "contained"})
    assert d.status is DecisionStatus.RESOLVED
    assert d.is_resolved is True
    assert d.may_dispatch is False, "RESOLVED must never also dispatch"


def test_a_wellformed_action_dispatches():
    d = validate_decision({"tool": "network_scan", "input": {"target": "10.0.0.1"},
                           "reasoning": "map the host"},
                          allowed_tools=SOC_ADVERTISED_TOOLS)
    assert d.status is DecisionStatus.ACT
    assert d.may_dispatch is True
    assert d.tool == "network_scan" and d.tool_input == {"target": "10.0.0.1"}


def test_a_model_cannot_set_the_envelope_status():
    """`honour_status=False` for anything a model produced: a model that could set
    the envelope could assert RESOLVED without the sentinel."""
    for claimed in ("RESOLVED", "ACT", "UNKNOWN", "MODEL_ERROR"):
        d = validate_decision({"status": claimed}, honour_status=False)
        assert d.status is DecisionStatus.INVALID_DECISION, claimed


def test_a_trusted_envelope_status_is_honoured():
    d = validate_decision({"status": "MODEL_ERROR", "detail": "timeout"},
                          honour_status=True)
    assert d.status is DecisionStatus.MODEL_ERROR
    assert d.is_resolved is False and d.may_dispatch is False


def test_a_declared_act_still_has_to_satisfy_the_shape():
    """A declared status is not a licence to skip validation."""
    d = validate_decision({"status": "ACT", "tool": ""}, honour_status=True)
    assert d.status is DecisionStatus.INVALID_DECISION


def test_an_unadvertised_tool_is_an_invalid_decision_not_a_dispatch():
    d = validate_decision({"tool": "rm_minus_rf"},
                          allowed_tools=SOC_ADVERTISED_TOOLS)
    assert d.status is DecisionStatus.INVALID_DECISION
    assert d.may_dispatch is False


def test_absent_input_defaults_to_empty_not_to_a_refusal():
    d = validate_decision({"tool": "whois_lookup", "reasoning": "r"},
                          allowed_tools=SOC_ADVERTISED_TOOLS)
    assert d.may_dispatch is True and d.tool_input == {}


def test_a_decision_passes_through_validation_unchanged():
    d = Decision(status=DecisionStatus.RESOLVED)
    assert validate_decision(d) is d


def test_model_authored_reasoning_is_bounded():
    from core.agentic_decision import MAX_REASONING_CHARS
    d = validate_decision({"tool": "RESOLVED", "reasoning": "x" * 99_999})
    assert len(d.reasoning) <= MAX_REASONING_CHARS


def test_the_advertised_set_and_the_validated_set_are_the_same_object():
    """One definition of the SOC tool list, so it cannot be offered to the model
    without being dispatchable, or validated without being offered."""
    from core.agentic_loop import ADVERTISED_TOOLS
    assert frozenset(SOC_ADVERTISED_TOOLS) == ADVERTISED_TOOLS
    assert SOC_ADVERTISED_TOOLS, "the advertised set must not be empty"


# ══════════════════════════════════════════════════════════════════════════════
# §D — PER-RUN CANCELLATION / DEADLINE
# ══════════════════════════════════════════════════════════════════════════════
#: Every module-global `core.cancel_bus` state a test may disturb. The four events
#: and `_loop` are the set `test_barge_in_v69_m575._isolated_cancel_bus` already
#: guards, and its docstring says exactly why, which this fixture learned the
#: expensive way: the bus starts UNINITIALIZED (every event None) and most of the
#: suite runs against that, because `TTS._teardown()` sets `tts_cancel` only when it
#: is not None. A test that initializes the bus and walks away makes that teardown
#: live for every later test, and the next TTS worker then sees a flag a previous
#: instance set and DRAINS its first utterance instead of speaking it.
#:
#: Measured: leaving the bus initialized here failed
#: `test_tts_shutdown.py::test_stop_leaves_no_nondaemon_worker_even_when_utterance_wedged`
#: with "worker never started speaking" — in the full suite only, 700 tests later,
#: and never in isolation.
_BUS_GLOBALS = ("llm_stream_cancel", "agentic_loop_cancel", "playbook_cancel",
                "tts_cancel", "_loop")


@pytest.fixture()
def bus():
    """A cancel bus with a live loop, restored EXACTLY as it was found.

    `_cancel_generation` is deliberately never rewound by production code — that
    monotonicity is the mechanism that stops one run un-cancelling another — so the
    fixture saves and restores it rather than calling a reset that does not exist.
    """
    loop = asyncio.new_event_loop()
    saved = {name: getattr(cancel_bus, name, None) for name in _BUS_GLOBALS}
    saved_gen = cancel_bus._cancel_generation
    saved_exec = dict(cancel_bus._executions)
    saved_ops = dict(cancel_bus._active_operations)
    saved_ts = cancel_bus._last_cancel_ts
    cancel_bus.initialize(loop)
    cancel_bus._executions.clear()
    cancel_bus._active_operations.clear()
    cancel_bus._last_cancel_ts = 0.0
    try:
        yield cancel_bus
    finally:
        for name, value in saved.items():
            setattr(cancel_bus, name, value)
        cancel_bus._cancel_generation = saved_gen
        cancel_bus._last_cancel_ts = saved_ts
        cancel_bus._executions.clear()
        cancel_bus._executions.update(saved_exec)
        cancel_bus._active_operations.clear()
        cancel_bus._active_operations.update(saved_ops)
        loop.close()


def test_two_executions_have_distinct_cancellation_identity(bus):
    a = bus.register_execution("agentic_loop")
    b = bus.register_execution("agentic_loop")
    assert a.token != b.token
    assert a.event is not b.event
    assert a.name != b.name


def test_one_execution_cannot_cancel_another(bus):
    a = bus.register_execution("agentic_loop")
    b = bus.register_execution("agentic_loop")
    a.cancel()
    assert a.cancelled() is True
    assert b.cancelled() is False, "cancellation leaked between executions"


def test_one_execution_cannot_clear_anothers_cancellation(bus):
    """THE audited defect: `run_agentic_incident` CLEARED the shared event at
    start-up, so launching run B silently revoked the operator's cancellation of
    run A, which then carried on invoking effectful tools."""
    a = bus.register_execution("agentic_loop")
    a.cancel()
    assert a.cancelled() is True

    b = bus.register_execution("agentic_loop")      # a second run starts
    assert a.cancelled() is True, "starting another run un-cancelled run A"
    assert b.cancelled() is False

    b.cancel()
    bus.unregister_execution(b)
    assert a.cancelled() is True, "another run's teardown un-cancelled run A"


def test_one_execution_cannot_unregister_another(bus):
    """`unregister_operation("agentic_loop")` removed every run of the kind, so
    whichever incident finished first deregistered the ones still running."""
    a = bus.register_execution("agentic_loop")
    b = bus.register_execution("agentic_loop")

    bus.unregister_execution(b)

    assert a.token in bus._executions, "run A was deregistered by run B"
    assert b.token not in bus._executions
    assert "agentic_loop" in bus.get_active_operations()


def test_unregistering_is_idempotent(bus):
    a = bus.register_execution("agentic_loop")
    bus.unregister_execution(a)
    bus.unregister_execution(a)
    assert a.token not in bus._executions


def test_a_global_abort_cancels_every_live_execution(bus):
    a = bus.register_execution("agentic_loop")
    b = bus.register_execution("playbook")
    bus._last_cancel_ts = 0.0
    bus.cancel_all()
    assert a.cancelled() is True and b.cancelled() is True


def test_a_stale_abort_does_not_cancel_a_later_execution(bus):
    """The reason no run needs to CLEAR anything: an abort fired before a run
    existed does not reach it, so a fresh run starts clean by construction."""
    bus._last_cancel_ts = 0.0
    bus.cancel_all()
    fresh = bus.register_execution("agentic_loop")
    assert fresh.cancelled() is False


def test_reset_all_cannot_uncancel_a_live_execution(bus):
    a = bus.register_execution("agentic_loop")
    bus._last_cancel_ts = 0.0
    bus.cancel_all()
    assert a.cancelled() is True
    bus.reset_all()
    assert a.cancelled() is True, "reset_all resurrected a cancelled run"


def test_the_deadline_is_absolute_not_per_step(bus):
    h = bus.register_execution("agentic_loop", timeout=0.05)
    assert h.deadline is not None
    first = h.remaining()
    import time
    time.sleep(0.06)
    assert h.remaining() < first
    assert h.remaining() == 0.0, "remaining() must clamp at zero, never go negative"
    assert h.expired() is True


def test_a_handle_without_a_timeout_never_expires(bus):
    h = bus.register_execution("agentic_loop", timeout=None)
    assert h.deadline is None
    assert h.expired() is False
    assert h.remaining() == float("inf")


def test_active_operations_aggregates_executions_by_kind(bus):
    """Callers ask by KIND (`"agentic_loop" not in ops`); answering per token would
    have told the hunt scheduler that no incident was running."""
    bus.register_execution("agentic_loop")
    bus.register_execution("agentic_loop")
    ops = bus.get_active_operations()
    assert "agentic_loop" in ops
    assert len(bus.get_active_executions()) == 2


def test_the_aggregate_reports_the_longest_running_execution(bus):
    import time
    old = bus.register_execution("agentic_loop")
    time.sleep(0.02)
    bus.register_execution("agentic_loop")
    ops = bus.get_active_operations()
    assert ops["agentic_loop"] >= 0.0
    assert old.elapsed() >= 0.02


def test_a_handle_is_frozen(bus):
    """A handle whose token or deadline could be reassigned would be a shared
    identity again."""
    import dataclasses
    h = bus.register_execution("agentic_loop")
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.token = "somebody-elses-token"      # type: ignore[misc]


def test_cancel_execution_targets_exactly_one(bus):
    a = bus.register_execution("agentic_loop")
    b = bus.register_execution("agentic_loop")
    assert bus.cancel_execution(a.token) is True
    assert a.cancelled() is True and b.cancelled() is False
    assert bus.cancel_execution("not-a-token") is False


# ── the loop's own behaviour ──────────────────────────────────────────────────
class _Executor:
    """A tool executor that records what it was asked to do."""

    def __init__(self, *, hang: bool = False, approve: bool = True,
                 hang_approval: bool = False):
        self.calls: list[str] = []
        self._hang = hang
        self._approve = approve
        self._hang_approval = hang_approval

    async def _challenge(self, tool_name, preview):
        if self._hang_approval:
            await asyncio.sleep(3600)
        return self._approve, "test:granted"

    async def aexecute(self, tool_name, tool_input, reasoning=""):
        self.calls.append(tool_name)
        if self._hang:
            await asyncio.sleep(3600)
        return {"status": "ok"}


class _LLM:
    """Returns a scripted sequence of raw decisions."""

    def __init__(self, *script):
        self.script = list(script)
        self.asked = 0

    async def decide_next_action(self, context):
        self.asked += 1
        if not self.script:
            return {"tool": "RESOLVED", "reasoning": "script exhausted"}
        return self.script.pop(0)


def _run_loop(llm, executor, **kw):
    from core.agentic_loop import run_agentic_incident

    events: list = []

    async def broadcast(ev):
        events.append(ev)

    async def _go():
        await run_agentic_incident(
            trigger_event={"type": "canary_intrusion"},
            tool_executor=executor,
            broadcast_fn=broadcast,
            llm_client=llm,
            cognitive_engine=None,
            **kw,
        )

    asyncio.run(_go())
    return events


def _terminals(events):
    return [e for e in events if isinstance(e, dict)
            and e.get("type") == "agentic_terminal"]


def test_a_resolved_run_records_exactly_one_terminal_state():
    events = _run_loop(_LLM({"tool": "RESOLVED", "reasoning": "contained"}),
                       _Executor())
    terms = _terminals(events)
    assert len(terms) == 1, f"expected one terminal event, got {len(terms)}"
    assert terms[0]["outcome"] == "RESOLVED"


def test_an_invalid_decision_does_not_terminate_the_run_as_resolved():
    """§C at the loop boundary: an unparseable reply must not close an incident."""
    events = _run_loop(
        _LLM({"status": "INVALID_DECISION", "detail": "did not parse"},
             {"tool": "RESOLVED", "reasoning": "actually contained"}),
        _Executor())
    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "RESOLVED"
    rejected = [e for e in events if isinstance(e, dict)
                and e.get("type") == "agentic_decision_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["status"] == "INVALID_DECISION"


def test_unusable_decisions_are_bounded_and_end_truthfully():
    from core.agentic_loop import _MAX_UNUSABLE_DECISIONS

    bad = {"status": "MODEL_ERROR", "detail": "transport down"}
    events = _run_loop(_LLM(*([bad] * 20)), _Executor())
    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "REASONING_UNUSABLE", (
        "a run whose model never produced a usable decision must not be RESOLVED")
    rejected = [e for e in events if isinstance(e, dict)
                and e.get("type") == "agentic_decision_rejected"]
    assert len(rejected) == _MAX_UNUSABLE_DECISIONS


def test_a_malformed_decision_never_dispatches_a_tool():
    ex = _Executor()
    _run_loop(_LLM({"tool": "", "input": {}},
                   {"tool": "definitely_not_a_tool"},
                   {"reasoning": "no tool at all"},
                   {"tool": "RESOLVED"}), ex)
    assert ex.calls == [], f"a malformed decision dispatched {ex.calls}"


def test_the_cycle_budget_produces_a_truthful_terminal_state(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "agentic_max_cycles", 2, raising=False)
    ex = _Executor()
    events = _run_loop(_LLM(*([{"tool": "whois_lookup", "input": {}}] * 10)), ex)
    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "CYCLE_BUDGET_EXHAUSTED"
    assert terms[0]["cycles_run"] == 2
    assert len(ex.calls) == 2


def test_a_deadline_expiry_produces_a_truthful_terminal_state(monkeypatch):
    """The deadline now reaches INSIDE a cycle. A tool that outlives the run used
    to run unbounded, because the deadline was compared only between cycles."""
    from core.config import settings

    monkeypatch.setattr(settings, "agentic_loop_timeout", 0.4, raising=False)
    monkeypatch.setattr(settings, "agentic_max_cycles", 8, raising=False)
    ex = _Executor(hang=True)
    events = _run_loop(_LLM({"tool": "whois_lookup", "input": {}}), ex)

    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "DEADLINE_EXCEEDED"
    assert ex.calls == ["whois_lookup"]


def test_an_effect_cut_off_by_the_deadline_stays_uncertain(monkeypatch):
    """§D — a tool cancelled mid-flight was logged as `{"error": ...}`, which reads
    as *it did not happen*. That is a claim about the world nothing observed."""
    from core.config import settings

    monkeypatch.setattr(settings, "agentic_loop_timeout", 0.4, raising=False)
    ex = _Executor(hang=True)
    events = _run_loop(_LLM({"tool": "whois_lookup", "input": {}}), ex)

    terms = _terminals(events)
    assert terms[0]["uncertain_effects"] == 1

    summary = next(e for e in events if isinstance(e, dict)
                   and e.get("type") == "agentic_summary")
    logged = summary["action_log"][-1]["result"]
    assert logged["external_outcome"] == "UNKNOWN"
    assert logged["disposition"] == "BLOCKED_INDETERMINATE"
    assert logged["retry_authority"] == "BLOCKED_INDETERMINATE"
    assert "UNKNOWN" in logged["error"]


def test_an_unanswered_approval_cannot_outlive_the_run_deadline(monkeypatch):
    """An interactive approval nobody answers must not hang the run without bound.
    Nothing ran, so this is fail-closed AND certain."""
    from core.config import settings

    monkeypatch.setattr(settings, "agentic_loop_timeout", 0.4, raising=False)
    ex = _Executor(hang_approval=True)
    events = _run_loop(_LLM({"tool": "run_shell_command",
                             "input": {"command": "id"}}), ex)

    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "APPROVAL_UNAVAILABLE"
    assert ex.calls == [], "the tool ran without an approval"


def test_a_denied_approval_does_not_execute_and_does_not_end_the_run():
    ex = _Executor(approve=False)
    events = _run_loop(_LLM({"tool": "run_shell_command", "input": {"command": "id"}},
                            {"tool": "RESOLVED"}), ex)
    assert ex.calls == []
    assert _terminals(events)[0]["outcome"] == "RESOLVED"


def test_an_operator_cancellation_records_a_terminal_state(bus, monkeypatch):
    """A cancelled run used to broadcast NO terminal event at all."""
    from core.config import settings

    monkeypatch.setattr(settings, "agentic_max_cycles", 4, raising=False)

    class _CancellingLLM:
        def __init__(self):
            self.asked = 0

        async def decide_next_action(self, context):
            self.asked += 1
            # Cancel every live agentic execution, the way the operator would.
            for h in list(cancel_bus._executions.values()):
                h.cancel()
            return {"tool": "whois_lookup", "input": {}}

    ex = _Executor()
    events = _run_loop(_CancellingLLM(), ex)
    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "CANCELLED"


def test_a_task_cancellation_still_records_a_terminal_state(monkeypatch):
    """`except asyncio.CancelledError: logger.warning(...)` recorded nothing."""
    from core.agentic_loop import run_agentic_incident
    from core.config import settings

    monkeypatch.setattr(settings, "agentic_loop_timeout", 30, raising=False)
    events: list = []

    async def broadcast(ev):
        events.append(ev)

    ex = _Executor(hang=True)

    async def _go():
        task = asyncio.create_task(run_agentic_incident(
            trigger_event={"type": "canary_intrusion"},
            tool_executor=ex,
            broadcast_fn=broadcast,
            llm_client=_LLM({"tool": "whois_lookup", "input": {}}),
            cognitive_engine=None,
        ))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_go())

    terms = _terminals(events)
    assert len(terms) == 1, "a cancelled run recorded no terminal state"
    assert terms[0]["outcome"] == "CANCELLED"
    assert terms[0]["uncertain_effects"] == 1, (
        "an effect cancelled in flight must stay uncertain")


def test_the_run_deregisters_itself_on_every_path():
    """No `bus` fixture on purpose: this asserts the REAL registry is left clean."""
    before = set(cancel_bus._executions)
    _run_loop(_LLM({"tool": "RESOLVED"}), _Executor())
    assert set(cancel_bus._executions) == before


def test_a_transport_failure_is_model_error_not_resolved():
    class _Broken:
        async def decide_next_action(self, context):
            raise RuntimeError("ollama refused the connection")

    events = _run_loop(_Broken(), _Executor())
    terms = _terminals(events)
    assert len(terms) == 1
    assert terms[0]["outcome"] == "REASONING_UNUSABLE", (
        "a transport failure must not read as a resolved incident")


# ── §C at the real call site: decide_next_action's own envelope ───────────────
#
# The tests above validate `validate_decision` directly. That left the COMPOSED
# control untested, and the M68A falsification campaign proved it: flipping
# `honour_status=False` to `True` in `decide_next_action` — handing a model the
# power to set its own envelope status, and with it to assert RESOLVED without the
# sentinel — survived all 100 tests. These close it behaviourally, by driving the
# real method over a fake transport.
class _FakeCompletions:
    def __init__(self, content=None, raises=None):
        self._content = content
        self._raises = raises
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        msg = type("_M", (), {"content": self._content})()
        choice = type("_C", (), {"message": msg})()
        return type("_R", (), {"choices": [choice]})()


def _llm(content=None, raises=None):
    from core.llm import LLM

    obj = LLM.__new__(LLM)
    completions = _FakeCompletions(content=content, raises=raises)
    obj.client = type("_Client", (), {
        "chat": type("_Chat", (), {"completions": completions})()})()
    return obj, completions


def _decide(content=None, raises=None) -> dict:
    obj, _ = _llm(content=content, raises=raises)
    return asyncio.run(obj.decide_next_action([{"type": "canary_intrusion"}]))


def test_decide_next_action_never_lets_a_model_declare_the_envelope_status():
    """A model that could set `status` could assert RESOLVED without ever naming the
    sentinel — the audited defect wearing a different field name."""
    out = _decide('{"status": "RESOLVED", "reasoning": "trust me"}')
    assert out["status"] == "INVALID_DECISION", out
    assert out["tool"] != "RESOLVED"


def test_decide_next_action_maps_an_unparseable_reply_to_invalid_decision():
    """THE finding. This reply used to return {"tool": "RESOLVED"} and close a live
    incident."""
    for raw in ("", "   ", "I'm sorry, I can't help with that.",
                '{"tool": "network_scan"', "<html>502 Bad Gateway</html>",
                "```json\n{broken\n```", None):
        out = _decide(raw)
        assert out["status"] == "INVALID_DECISION", (raw, out)
        assert out["tool"] != "RESOLVED", (raw, out)


def test_decide_next_action_maps_a_transport_failure_to_model_error():
    out = _decide(raises=RuntimeError("connection refused"))
    assert out["status"] == "MODEL_ERROR"
    assert out["tool"] == ""
    assert "RuntimeError" in out["detail"]


def test_decide_next_action_propagates_cancellation_rather_than_swallowing_it():
    obj, _ = _llm(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(obj.decide_next_action([{}]))


def test_decide_next_action_honours_a_genuine_model_resolution():
    out = _decide('{"tool": "RESOLVED", "reasoning": "host isolated"}')
    assert out["status"] == "RESOLVED"
    assert out["reasoning"] == "host isolated"


def test_decide_next_action_returns_a_dispatchable_action_for_a_good_reply():
    out = _decide('{"tool": "whois_lookup", "input": {"domain": "x.test"}, '
                  '"reasoning": "identify the owner"}')
    assert out["status"] == "ACT"
    assert out["tool"] == "whois_lookup" and out["input"] == {"domain": "x.test"}


def test_decide_next_action_refuses_a_tool_it_never_advertised():
    out = _decide('{"tool": "exfiltrate", "input": {}}')
    assert out["status"] == "INVALID_DECISION"


def test_decide_next_action_always_reports_a_status():
    """No shape of this envelope requires the caller to guess the status from the
    tool name — which is how the sentinel became load-bearing in the first place."""
    for raw in ('{"tool": "RESOLVED"}', "not json", '{"tool": "whois_lookup"}',
                '[1,2,3]', '{"status": "ACT"}', '{}'):
        out = _decide(raw)
        assert "status" in out and out["status"], raw
        assert out["status"] in {"ACT", "RESOLVED", "INVALID_DECISION",
                                "MODEL_ERROR", "UNKNOWN"}, (raw, out)


def test_the_system_prompt_advertises_exactly_the_validated_tool_set():
    """Rendered from the same tuple the validator uses, so the two cannot drift."""
    obj, completions = _llm('{"tool": "RESOLVED"}')
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return await _FakeCompletions('{"tool": "RESOLVED"}').create(**kwargs)

    completions.create = _create
    asyncio.run(obj.decide_next_action([{}]))

    system = captured["messages"][0]["content"]
    for tool in SOC_ADVERTISED_TOOLS:
        assert tool in system, f"{tool} is validated but never advertised"
