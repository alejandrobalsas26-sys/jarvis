"""V69 M68A — structural tests for ABSENT runtime-integrity controls.

WHY THESE ARE NOT MUTATION TESTS
--------------------------------
M66B ran 131 mutations with 0 survivors and M66A.1 ran 75 with 0. Every one of the
four defects an external audit found here survived all of them — because a mutation
campaign can only break a line that exists. Break `if self._journal_ready:` and a
test fails, which teaches you the line is load-bearing. It teaches you nothing
about the state that was never representable, the eligibility check that was
skipped rather than failed, or the terminal event a cancelled run never emitted.

So each test here asks *"what required control could be completely missing?"* and
checks the property over the source, not over a branch. They are written against
the AST rather than against text wherever a comment or a docstring could otherwise
satisfy them: M68A's own first draft of one of these probes passed because the word
it searched for appeared in a comment explaining the bug.

THE FOUR FINDINGS, GENERALISED
------------------------------
  §A  a state machine where an ERROR is observationally equal to a DISABLED
      -> every lifecycle state has a producer, and READY is dominated by verification
  §B  an empty collection normalised into a wildcard
      -> NO module in core/ may end a comprehension with `or None`
  §C  a failure handler returning a success sentinel
      -> no `except` handler anywhere in core/ may produce the RESOLVED sentinel
  §D  an identity that is a string constant, and a deadline checked between steps
      -> every long await in the run is deadline-bounded; every outcome has a producer
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

CORE = PACKAGE_ROOT / "core"
EXECUTOR = PACKAGE_ROOT / "tools" / "executor.py"
AGENTIC_LOOP = CORE / "agentic_loop.py"
CANCEL_BUS = CORE / "cancel_bus.py"
TOOL_LOOP = CORE / "tool_loop.py"
DECISION = CORE / "agentic_decision.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — the control it carries may be absent")


def _calls(node) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _identifiers(tree) -> set[str]:
    """Every NAME the code actually uses. Docstrings and comments are not code.

    M68A's own first draft of three probes in this file searched raw text and
    passed on prose that described the bug. Anything that must be absent from the
    CODE is asked of the AST.
    """
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
    return out


def _body_without_docstring(node):
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _core_modules() -> list[Path]:
    return sorted(p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)


# ══════════════════════════════════════════════════════════════════════════════
# §A — the journal lifecycle could have been a boolean again
# ══════════════════════════════════════════════════════════════════════════════
def test_the_journal_lifecycle_is_an_enum_not_a_boolean():
    """A boolean cannot express four states. That is how an initialisation ERROR
    became observationally equal to an operator DISABLED."""
    from tools.executor import JournalLifecycle

    assert issubclass(JournalLifecycle, __import__("enum").Enum)
    assert len(JournalLifecycle) >= 4


def test_every_journal_lifecycle_state_has_a_producer():
    """The M67A lesson: `RejectReason.CONTRADICTORY_TRACE` was in a closed enum and
    NOTHING could emit it — a state no mutation campaign would ever flag. A
    lifecycle state that cannot be reached is a control that is not there."""
    from tools.executor import JournalLifecycle

    src = EXECUTOR.read_text(encoding="utf-8")
    tree = ast.parse(src)

    produced: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "JournalLifecycle"):
            produced.add(node.attr)

    missing = {s.name for s in JournalLifecycle} - produced
    assert not missing, f"lifecycle states with no producer in executor.py: {missing}"


def test_the_boolean_readiness_flag_is_gone_entirely():
    """A leftover `_journal_ready` would be a second, disagreeing source of truth.

    Asked of the AST: the name appears in this module's own prose describing the
    defect, and prose is not a second source of truth.
    """
    assert "_journal_ready" not in _identifiers(_tree(EXECUTOR)), (
        "the collapsed boolean survives alongside the state machine")


def test_ready_is_never_assigned_before_verification_in_the_accessor():
    """The control that was ABSENT: nothing forced the READY publication to come
    after construction AND verification. Checked over statement order in the AST,
    so a comment claiming it cannot satisfy this test."""
    fn = _function(_tree(EXECUTOR), "_effect_journal")

    verify_at: list[int] = []
    assigned_at: list[int] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "assert_healthy":
            verify_at.append(node.lineno)
        # An ASSIGNMENT of READY is a publication. A READ of it (the fast-path
        # guard at the top of the accessor) is not, and must stay allowed.
        if isinstance(node, ast.Assign):
            val = node.value
            if (isinstance(val, ast.Attribute) and val.attr == "READY"
                    and isinstance(val.value, ast.Name)
                    and val.value.id == "JournalLifecycle"):
                assigned_at.append(node.lineno)

    assert verify_at, "_effect_journal never calls assert_healthy"
    assert assigned_at, "_effect_journal never publishes READY"
    assert min(assigned_at) > max(verify_at), (
        "READY is published at line {} but verification only happens at {} — "
        "so READY is observable over an unverified journal".format(
            min(assigned_at), max(verify_at)))


def test_the_accessor_returns_none_only_on_the_disabled_path():
    """`None` MEANS operator-disabled to `_durable_effect`. Any other `return None`
    in this accessor is the audited fail-open, whatever comment sits above it."""
    fn = _function(_tree(EXECUTOR), "_effect_journal")

    for node in ast.walk(fn):
        if not isinstance(node, ast.Return):
            continue
        val = node.value
        is_bare_none = val is None or (isinstance(val, ast.Constant)
                                       and val.value is None)
        if not is_bare_none:
            continue
        # A bare `return None` is only legitimate where DISABLED was just set.
        window = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Attribute) and n.attr == "DISABLED"
                  and abs(n.lineno - node.lineno) <= 3]
        assert window, (
            f"line {node.lineno}: `return None` outside the DISABLED branch — "
            "an initialisation failure would read as operator-disabled")


def test_the_failed_state_raises_rather_than_returning(monkeypatch):
    """A FAILED journal that RETURNED anything — even a sentinel object — would
    re-enter `_durable_effect`'s success path."""
    from tools.executor import JournalLifecycle, ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "1")
    ex = ToolExecutor()
    ex._journal_state = JournalLifecycle.FAILED
    ex._journal_error = "synthetic"
    with pytest.raises(Exception):   # noqa: PT011 — the point is that it raises
        ex._effect_journal()


# ══════════════════════════════════════════════════════════════════════════════
# §B — an empty collection normalised into a wildcard
# ══════════════════════════════════════════════════════════════════════════════
def test_no_core_module_normalises_a_comprehension_into_none():
    """THE GENERALISATION of finding B. `{...} or None` is the shape that turned an
    exhausted tool set into an unrestricted one. This test does not ask whether
    llm.py still does it — it asks whether ANY module in core/ does, including ones
    written after M68A."""
    offenders: list[str] = []
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
                continue
            if len(node.values) != 2:
                continue
            left, right = node.values
            collection = (ast.SetComp, ast.ListComp, ast.DictComp,
                          ast.GeneratorExp, ast.Set, ast.List, ast.Dict, ast.Tuple)
            if isinstance(left, collection) and isinstance(right, ast.Constant) \
                    and right.value is None:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")
    assert offenders == [], (
        "an empty collection is normalised into None (unrestricted/unknown) at: "
        + ", ".join(offenders))


def test_the_eligibility_helper_cannot_return_none():
    """Checked by annotation AND by exhaustion, because an annotation is a claim."""
    import inspect

    from core.tool_loop import eligible_tool_names

    sig = inspect.signature(eligible_tool_names)
    assert "None" not in str(sig.return_annotation), (
        f"the helper's own contract admits None: {sig.return_annotation}")

    for candidate in (None, [], (), {}, set(), 0, "", [None], [{}], [[]],
                      [{"function": None}], [{"function": {"name": None}}]):
        got = eligible_tool_names(candidate)
        assert got is not None and isinstance(got, frozenset)


def test_the_validator_has_no_unrestricted_mode_at_all():
    """The control that was ABSENT: there was no value of `eligible_names` meaning
    "nothing is eligible" that the validator would honour — `None` and `set()`
    took different paths. Now NO input to this parameter grants a wildcard."""
    from core.tool_loop import validate_tool_call

    for permissive in (None, set(), frozenset(), [], (), {}, 0, "", False):
        ok, args, reason = validate_tool_call("run_shell_command", "{}", permissive)
        assert ok is False, f"{permissive!r} was treated as unrestricted eligibility"
        assert args == {}
        assert reason == "no_eligible_tools"


def test_every_bound_in_the_tool_budget_is_finite_and_positive():
    """A bound that defaults to zero or to infinity is a bound that is not there."""
    from core.tool_loop import ToolLoopBudget

    budget = ToolLoopBudget()
    for field in ("max_rounds", "max_retries", "max_repairs",
                  "max_calls_per_response"):
        value = getattr(budget, field)
        assert isinstance(value, int), field
        assert 0 < value < 10_000, f"{field}={value} is not a usable bound"


def test_the_per_response_call_ceiling_exists_as_a_control():
    """The round budget bounded how many times the model is ASKED. Nothing bounded
    how many tools one answer could request, so "4 rounds" described a quantity
    that was not what it sounded like."""
    from core.tool_loop import ToolLoopBudget

    budget = ToolLoopBudget()
    assert hasattr(budget, "admit_response_calls")
    assert budget.admit_response_calls(10_000) <= budget.max_calls_per_response


def test_the_response_ceiling_is_enforced_where_history_is_written():
    """Enforcing it AFTER the assistant turn reaches history would leave announced
    tool_calls without results and break the pairing invariant."""
    src = (CORE / "llm.py").read_text(encoding="utf-8")
    assert "admit_response_calls" in src, (
        "the per-response ceiling exists but nothing calls it")
    admit = src.index("admit_response_calls")
    history = src.index('"tool_calls": tool_calls_list', admit)
    assert admit < history, "the ceiling is applied after history is written"


# ══════════════════════════════════════════════════════════════════════════════
# §C — a failure handler producing a success sentinel
# ══════════════════════════════════════════════════════════════════════════════
def test_no_except_handler_in_core_produces_the_resolved_sentinel():
    """THE GENERALISATION of finding C. The defect was a `return {"tool":
    "RESOLVED"}` reached from a parse failure. This asks the general question: does
    any error handler anywhere in core/ manufacture the terminal success sentinel?"""
    from core.agentic_decision import RESOLVED_SENTINEL

    offenders: list[str] = []
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
            for node in ast.walk(handler):
                if (isinstance(node, ast.Constant)
                        and node.value == RESOLVED_SENTINEL):
                    offenders.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")
    assert offenders == [], (
        "an exception handler manufactures the RESOLVED sentinel at: "
        + ", ".join(offenders))


def test_decide_next_action_has_no_path_that_returns_the_sentinel_literally():
    """`decide_next_action` may only REPORT a resolution the model expressed; it may
    never author one."""
    fn = _function(_tree(CORE / "llm.py"), "decide_next_action")
    literals = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and n.value == "RESOLVED"]
    assert literals == [], (
        f"decide_next_action writes the RESOLVED sentinel itself at {literals}")


def test_every_decision_status_has_a_producer():
    """A status nothing can emit is a distinction that does not exist. This is the
    test that would have caught the missing INVALID_DECISION in the first place."""
    from core.agentic_decision import DecisionStatus

    searched = [DECISION, CORE / "llm.py", AGENTIC_LOOP]
    produced: set[str] = set()
    for path in searched:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "DecisionStatus"):
                produced.add(node.attr)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in {s.name for s in DecisionStatus}:
                    produced.add(node.value)

    missing = {s.name for s in DecisionStatus} - produced
    assert not missing, f"decision statuses with no producer: {missing}"


def test_dispatch_is_gated_on_a_positive_predicate_not_on_not_resolved():
    """`status != RESOLVED` is the formulation that let a FAILURE reach a tool call.
    The loop must consult a predicate that positively means "this is an action"."""
    src = AGENTIC_LOOP.read_text(encoding="utf-8")
    assert "may_dispatch" in src, "the loop does not consult may_dispatch"

    from core.agentic_decision import Decision, DecisionStatus

    for status in DecisionStatus:
        d = Decision(status=status, tool="network_scan")
        assert d.may_dispatch is (status is DecisionStatus.ACT), status


def test_a_resolved_decision_is_never_also_dispatchable():
    from core.agentic_decision import Decision, DecisionStatus

    d = Decision(status=DecisionStatus.RESOLVED, tool="run_shell_command")
    assert d.may_dispatch is False


def test_the_validator_is_total_over_arbitrary_input():
    """A validator that raises on an unexpected shape is a validator the caller will
    wrap in a try/except and default — which is where the last default came from."""
    from core.agentic_decision import validate_decision

    class _Hostile:
        def __getattr__(self, name):
            raise RuntimeError("no introspection for you")

        def __eq__(self, other):
            raise RuntimeError("no comparison either")

        __hash__ = None   # type: ignore[assignment]

    for raw in (None, 0, "", b"", [], {}, set(), _Hostile(), object(),
                {"tool": _Hostile()}, {None: 1}, float("nan")):
        d = validate_decision(raw)
        assert d.status.name in {"INVALID_DECISION", "MODEL_ERROR", "UNKNOWN"}, raw


# ══════════════════════════════════════════════════════════════════════════════
# §D — shared identity, and a deadline that does not propagate
# ══════════════════════════════════════════════════════════════════════════════
def test_the_agentic_loop_never_registers_under_a_shared_name():
    """The identity WAS the literal string "agentic_loop", so two incidents were one
    registry slot and whichever finished first deregistered the other."""
    called = _calls(_tree(AGENTIC_LOOP))
    assert "register_operation" not in called
    assert "unregister_operation" not in called
    assert "register_execution" in called
    assert "unregister_execution" in called


def test_the_agentic_loop_never_touches_the_shared_cancellation_event():
    names = {n.attr for n in ast.walk(_tree(AGENTIC_LOOP))
             if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(_tree(AGENTIC_LOOP)) if isinstance(n, ast.Name)}
    assert "agentic_loop_cancel" not in names, (
        "the loop still reads the process-wide event two runs shared")


def test_no_run_can_clear_cancellation_state():
    """The revocation path. `agentic_loop_cancel.clear()` at start-up is how run B
    un-cancelled run A."""
    assert "clear" not in _calls(_tree(AGENTIC_LOOP)), (
        "the loop calls .clear() on something")


def test_the_execution_handle_is_immutable():
    """A reassignable token is a shared identity with extra steps."""
    import dataclasses

    from core.cancel_bus import ExecutionHandle

    assert dataclasses.is_dataclass(ExecutionHandle)
    assert ExecutionHandle.__dataclass_params__.frozen is True


def test_the_cancel_generation_is_never_decremented():
    """Monotonicity is the whole mechanism: it is what makes 'no run can un-cancel
    another' true without anyone having to remember not to clear."""
    tree = _tree(CANCEL_BUS)
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "_cancel_generation":
            assert isinstance(node.op, ast.Add), (
                f"line {node.lineno}: _cancel_generation is decremented")
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_cancel_generation":
                    # Only the module-level initialiser may assign it outright.
                    assert node.col_offset == 0, (
                        f"line {node.lineno}: _cancel_generation is reassigned "
                        "inside a function")


def test_reset_all_does_not_rewind_the_cancel_generation():
    fn = _function(_tree(CANCEL_BUS), "reset_all")
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                assert not (isinstance(t, ast.Name)
                            and t.id == "_cancel_generation"), (
                    "reset_all touches the generation counter, so it can "
                    "un-cancel a live run")


def test_unregistering_is_keyed_by_token_not_by_kind():
    fn = _function(_tree(CANCEL_BUS), "unregister_execution")
    body = "\n".join(ast.unparse(stmt) for stmt in _body_without_docstring(fn))
    assert "token" in body, "unregister_execution does not key on the token"
    assert "kind" not in body, "unregister_execution still keys on the kind"


def test_every_long_await_in_the_run_is_deadline_bounded():
    """The control that was ABSENT: the deadline was compared between cycles and
    never reached the reasoning call, the approval or the tool. Any of those three
    could outlive the whole run without bound."""
    fn = _function(_tree(AGENTIC_LOOP), "run_agentic_incident")

    UNBOUNDED = {"decide_next_action", "_challenge", "aexecute"}
    bounded: set[str] = set()
    naked: list[str] = []

    for node in ast.walk(fn):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        fname = call.func.id if isinstance(call.func, ast.Name) else \
            getattr(call.func, "attr", "")
        inner = _calls(call)
        if fname in ("_bounded", "wait_for"):
            bounded |= inner & UNBOUNDED
            continue
        hit = inner & UNBOUNDED
        if hit:
            naked.append(f"line {node.lineno}: {sorted(hit)}")

    assert naked == [], f"unbounded awaits of effectful/model calls: {naked}"
    assert bounded == UNBOUNDED, (
        f"these calls are never deadline-bounded anywhere: {UNBOUNDED - bounded}")


def test_the_bounded_helper_refuses_a_spent_deadline_without_starting_the_call():
    """Starting a call with no budget left is what the deadline exists to prevent."""
    import asyncio

    from core.agentic_loop import _bounded

    started = {"n": 0}

    async def _never():
        started["n"] += 1
        await asyncio.sleep(3600)

    async def _go():
        coro = _never()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await _bounded(coro, 0.0, what="test")
        finally:
            coro.close()

    asyncio.run(_go())
    assert started["n"] == 0, "the call was started despite a spent deadline"


def test_every_terminal_outcome_has_a_producer():
    """An outcome nothing can record is a terminal state the run cannot report."""
    from core.agentic_loop import TerminalOutcome

    declared = {k for k in vars(TerminalOutcome) if k.isupper()}
    tree = _tree(AGENTIC_LOOP)
    produced = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "TerminalOutcome"}
    missing = declared - produced
    assert not missing, f"terminal outcomes with no producer: {missing}"


def test_the_terminal_recorder_latches_before_it_broadcasts():
    """If the flag were set AFTER the broadcast, a broadcast that raised would let a
    second terminal state be recorded — the mirror of the audited defect."""
    fn = _function(_tree(AGENTIC_LOOP), "record")

    latch_line = None
    broadcast_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "recorded":
                    latch_line = node.lineno if latch_line is None else latch_line
        if isinstance(node, ast.Await) and broadcast_line is None:
            broadcast_line = node.lineno

    assert latch_line is not None, "the recorder never latches"
    assert broadcast_line is not None, "the recorder never broadcasts"
    assert latch_line < broadcast_line, (
        "the latch is set after the broadcast, so a failing broadcast permits a "
        "second terminal record")


def test_no_timeout_or_cancellation_handler_claims_the_effect_did_not_happen():
    """§D's uncertainty requirement, as a source property: in the loop's timeout and
    cancellation handlers, the external outcome may only ever be UNKNOWN."""
    fn = _function(_tree(AGENTIC_LOOP), "run_agentic_incident")

    CERTAIN = {"PROVEN_NOT_EXECUTED", "PROVEN_COMMITTED", "SAFE_TO_RETRY",
               "ALREADY_COMMITTED"}
    offenders: list[str] = []
    for handler in (n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)):
        exc = ast.unparse(handler.type) if handler.type else ""
        if "TimeoutError" not in exc and "CancelledError" not in exc:
            continue
        for node in ast.walk(handler):
            if isinstance(node, ast.Constant) and node.value in CERTAIN:
                offenders.append(f"line {node.lineno}: {node.value}")
    assert offenders == [], (
        "a timeout/cancellation handler makes a CERTAIN claim about an effect "
        f"nothing observed: {offenders}")


def test_the_interactive_approval_has_an_interactivity_guard_before_input():
    """The control that was ABSENT: `input()` on a non-interactive stdin blocks
    forever, the await above it had no timeout, and the loop only consulted its
    deadline between cycles — so a headless run hung with an incident still open."""
    fn = _function(_tree(EXECUTOR), "_challenge")

    guard_line = None
    input_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == "_approval_input_is_interactive" and guard_line is None:
                guard_line = node.lineno
            if name == "input" and input_line is None:
                input_line = node.lineno

    assert input_line is not None, "the keyboard fallback vanished entirely"
    assert guard_line is not None, (
        "nothing checks whether a human can answer before blocking on input()")
    assert guard_line < input_line, (
        "the interactivity guard runs after input() has already blocked")


def test_the_headless_approval_guard_is_fail_closed():
    from tools.executor import ToolExecutor

    class _NotATty:
        closed = False

        def isatty(self):
            return False

    class _Detached:
        closed = False

        def isatty(self):
            raise ValueError("I/O operation on closed file")

    class _Closed:
        closed = True

        def isatty(self):
            return True

    import io

    saved = sys.stdin
    try:
        for stream in (None, _NotATty(), _Detached(), _Closed(),
                       io.StringIO("y\n")):
            sys.stdin = stream   # type: ignore[assignment]
            assert ToolExecutor._approval_input_is_interactive() is False, stream
    finally:
        sys.stdin = saved


def test_the_approval_guard_does_not_abandon_a_thread():
    """§D says explicitly: do NOT solve blocked input by walking away from an
    uncontrolled thread. A `wait_for` around `input()` would return while the
    executor thread stayed blocked, still owning stdin, so the NEXT prompt would
    consume the keystroke this one waited for."""
    fn = _function(_tree(EXECUTOR), "_challenge")

    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "wait_for":
            inner = _calls(node)
            assert "run_in_executor" not in inner and "input" not in inner, (
                f"line {node.lineno}: wait_for wraps a blocking input() call, "
                "abandoning the thread that holds stdin")


def test_decide_next_action_never_trusts_a_model_supplied_envelope_status():
    """The M68A falsification campaign's one SURVIVOR, closed structurally as well
    as behaviourally.

    Flipping `honour_status=False` to `True` at this one call site hands the model
    the power to set its own envelope status — and therefore to assert RESOLVED
    without naming the sentinel, which is finding §C restored under a different
    field name. It survived all 100 tests, because every test of `honour_status`
    exercised `validate_decision` DIRECTLY and none asserted what the real caller
    passes. That is the absent-control pattern exactly: a parameter defended
    everywhere except where it is actually supplied.
    """
    fn = _function(_tree(CORE / "llm.py"), "decide_next_action")

    seen = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "honour_status":
                continue
            seen += 1
            assert isinstance(kw.value, ast.Constant), (
                f"line {kw.value.lineno}: honour_status is computed, not a literal "
                "— a model-reachable value could enable it")
            assert kw.value.value is False, (
                f"line {kw.value.lineno}: decide_next_action passes "
                f"honour_status={kw.value.value!r} over MODEL-AUTHORED output")
    assert seen >= 1, (
        "decide_next_action no longer states honour_status explicitly; the default "
        "is not allowed to carry this decision")


def test_only_trusted_callers_may_honour_a_declared_status():
    """`honour_status=True` is legitimate exactly once — the loop, over an envelope
    its own LLM client built. Any new site is a finding."""
    allowed = {AGENTIC_LOOP.name}
    offenders: list[str] = []
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "honour_status" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True and path.name not in allowed:
                    offenders.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{kw.value.lineno}")
    assert offenders == [], (
        "honour_status=True outside the trusted caller: " + ", ".join(offenders))
