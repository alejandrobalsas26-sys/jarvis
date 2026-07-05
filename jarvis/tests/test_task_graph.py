"""
tests/test_task_graph.py — V63 Milestone 3 coverage.

Proves the bounded task graph:
  * cycle rejection at validation;
  * dependency ordering;
  * bounded fan-out (concurrency cap);
  * node-count / depth caps;
  * global + per-node timeout;
  * cancellation propagation;
  * per-node retry limit + global runaway-retry ceiling;
  * partial-failure cascade (only transitive dependents skipped);
  * TOOL nodes delegate to the ToolExecutor (no bypass);
  * AGENT nodes delegate to the controlled team runtime;
  * HUMAN_APPROVAL fails closed.
"""
from __future__ import annotations

import asyncio

import pytest

from core.task_graph import (
    CancelToken,
    ExecutionBudget,
    GraphValidationError,
    NodeOutcome,
    NodeStatus,
    NodeType,
    RetryPolicy,
    TaskGraph,
    TaskGraphExecutor,
    build_default_handlers,
)


# ── helpers ───────────────────────────────────────────────────────────────────
def _ok_handlers(order: list[str] | None = None, peak=None, active=None):
    """A REASON handler that records execution order + concurrency."""
    order = order if order is not None else []
    active = active if active is not None else {"n": 0}
    peak = peak if peak is not None else {"n": 0}

    async def _reason(node, ctx):
        active["n"] += 1
        peak["n"] = max(peak["n"], active["n"])
        try:
            await asyncio.sleep(0.02)
            order.append(node.node_id)
            return NodeOutcome(ok=True, output=f"out:{node.node_id}")
        finally:
            active["n"] -= 1

    return {NodeType.REASON: _reason}


# ── validation ────────────────────────────────────────────────────────────────
def test_cycle_is_rejected():
    g = TaskGraph()
    g.add("a", NodeType.REASON, depends_on=["b"])
    g.add("b", NodeType.REASON, depends_on=["a"])
    with pytest.raises(GraphValidationError):
        g.validate(ExecutionBudget())


def test_unknown_dependency_rejected():
    g = TaskGraph()
    g.add("a", NodeType.REASON, depends_on=["ghost"])
    with pytest.raises(GraphValidationError):
        g.validate(ExecutionBudget())


def test_node_cap_rejected():
    g = TaskGraph()
    for i in range(5):
        g.add(f"n{i}", NodeType.REASON)
    with pytest.raises(GraphValidationError):
        g.validate(ExecutionBudget(max_nodes=4))


def test_depth_cap_rejected():
    g = TaskGraph()
    prev = None
    for i in range(6):
        g.add(f"n{i}", NodeType.REASON, depends_on=[prev] if prev else [])
        prev = f"n{i}"
    with pytest.raises(GraphValidationError):
        g.validate(ExecutionBudget(max_depth=3))


def test_critical_path_is_longest_chain():
    g = TaskGraph()
    g.add("a", NodeType.REASON)
    g.add("b", NodeType.REASON, depends_on=["a"])
    g.add("c", NodeType.REASON, depends_on=["b"])
    g.add("x", NodeType.REASON)  # independent
    assert g.critical_path() == ["a", "b", "c"]


# ── ordering & fan-out ────────────────────────────────────────────────────────
def test_dependency_ordering_is_respected():
    order: list[str] = []
    g = TaskGraph()
    g.add("a", NodeType.REASON)
    g.add("b", NodeType.REASON, depends_on=["a"])
    g.add("c", NodeType.REASON, depends_on=["b"])
    ex = TaskGraphExecutor(_ok_handlers(order), ExecutionBudget())
    res = asyncio.run(ex.run(g))
    assert res.status == "completed"
    assert order == ["a", "b", "c"]
    # data flows: c saw b's output through the context (outputs recorded)
    assert res.outputs["c"] == "out:c"


def test_fan_out_is_bounded():
    peak = {"n": 0}
    g = TaskGraph()
    for i in range(8):
        g.add(f"n{i}", NodeType.REASON)   # all independent → all ready at once
    ex = TaskGraphExecutor(
        _ok_handlers(peak=peak, active={"n": 0}),
        ExecutionBudget(max_fan_out=3),
    )
    res = asyncio.run(ex.run(g))
    assert res.status == "completed"
    assert peak["n"] <= 3


def test_diamond_dependency_runs_middle_in_parallel():
    peak = {"n": 0}
    g = TaskGraph()
    g.add("root", NodeType.REASON)
    g.add("l", NodeType.REASON, depends_on=["root"])
    g.add("r", NodeType.REASON, depends_on=["root"])
    g.add("join", NodeType.REASON, depends_on=["l", "r"])
    ex = TaskGraphExecutor(_ok_handlers(peak=peak, active={"n": 0}),
                           ExecutionBudget(max_fan_out=4))
    res = asyncio.run(ex.run(g))
    assert res.status == "completed"
    assert peak["n"] >= 2  # l and r overlapped


# ── timeouts ──────────────────────────────────────────────────────────────────
def test_node_timeout_fails_the_node():
    async def _slow(node, ctx):
        await asyncio.sleep(1.0)
        return NodeOutcome(ok=True)

    g = TaskGraph()
    g.add("slow", NodeType.REASON, timeout_s=0.05, retry_policy=RetryPolicy(max_retries=0))
    ex = TaskGraphExecutor({NodeType.REASON: _slow}, ExecutionBudget(node_timeout_s=0.05))
    res = asyncio.run(ex.run(g))
    assert res.status == "failed"
    assert g.nodes["slow"].status in (NodeStatus.TIMED_OUT, NodeStatus.FAILED)


def test_global_timeout_cancels_run():
    async def _slow(node, ctx):
        await asyncio.sleep(5.0)
        return NodeOutcome(ok=True)

    g = TaskGraph()
    g.add("slow", NodeType.REASON, timeout_s=5.0)
    ex = TaskGraphExecutor({NodeType.REASON: _slow},
                           ExecutionBudget(global_timeout_s=0.1, node_timeout_s=5.0))
    res = asyncio.run(ex.run(g))
    assert res.status == "timed_out"


# ── cancellation ──────────────────────────────────────────────────────────────
def test_cancellation_propagates():
    tok = CancelToken()

    async def _watch(node, ctx):
        # cancel partway through the run
        if node.node_id == "a":
            tok.cancel()
        await asyncio.sleep(0.02)
        return NodeOutcome(ok=True)

    g = TaskGraph()
    g.add("a", NodeType.REASON)
    g.add("b", NodeType.REASON, depends_on=["a"])
    ex = TaskGraphExecutor({NodeType.REASON: _watch}, ExecutionBudget(), cancel=tok)
    res = asyncio.run(ex.run(g))
    assert res.status in ("cancelled", "partial")
    assert g.nodes["b"].status in (NodeStatus.CANCELLED, NodeStatus.SKIPPED, NodeStatus.PENDING)


# ── retries ───────────────────────────────────────────────────────────────────
def test_per_node_retry_limit():
    calls = {"n": 0}

    async def _flaky(node, ctx):
        calls["n"] += 1
        return NodeOutcome(ok=False, error="always fails")

    g = TaskGraph()
    g.add("f", NodeType.REASON, retry_policy=RetryPolicy(max_retries=2))
    ex = TaskGraphExecutor({NodeType.REASON: _flaky}, ExecutionBudget())
    res = asyncio.run(ex.run(g))
    assert res.status == "failed"
    assert calls["n"] == 3           # 1 initial + 2 retries, never more
    assert g.nodes["f"].attempts == 3


def test_global_retry_ceiling_caps_runaway():
    async def _flaky(node, ctx):
        return NodeOutcome(ok=False, error="fail")

    g = TaskGraph()
    for i in range(4):
        g.add(f"n{i}", NodeType.REASON, retry_policy=RetryPolicy(max_retries=10))
    ex = TaskGraphExecutor({NodeType.REASON: _flaky}, ExecutionBudget(max_total_retries=5))
    res = asyncio.run(ex.run(g))
    assert res.total_retries <= 5


def test_retry_then_succeed():
    calls = {"n": 0}

    async def _eventually(node, ctx):
        calls["n"] += 1
        return NodeOutcome(ok=calls["n"] >= 2, output="ok")

    g = TaskGraph()
    g.add("e", NodeType.REASON, retry_policy=RetryPolicy(max_retries=3))
    ex = TaskGraphExecutor({NodeType.REASON: _eventually}, ExecutionBudget())
    res = asyncio.run(ex.run(g))
    assert res.status == "completed"


# ── partial failure ───────────────────────────────────────────────────────────
def test_partial_failure_skips_only_dependents():
    async def _handler(node, ctx):
        if node.node_id == "bad":
            return NodeOutcome(ok=False, error="boom")
        return NodeOutcome(ok=True, output="ok")

    g = TaskGraph()
    g.add("bad", NodeType.REASON, retry_policy=RetryPolicy(max_retries=0))
    g.add("child", NodeType.REASON, depends_on=["bad"])
    g.add("indep", NodeType.REASON)           # independent branch
    ex = TaskGraphExecutor({NodeType.REASON: _handler}, ExecutionBudget())
    res = asyncio.run(ex.run(g))
    assert res.status == "partial"
    assert g.nodes["bad"].status == NodeStatus.FAILED
    assert g.nodes["child"].status == NodeStatus.SKIPPED
    assert g.nodes["indep"].status == NodeStatus.COMPLETED


# ── production handlers: no gateway bypass ────────────────────────────────────
class _RecordingExecutor:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def aexecute(self, name, args, reasoning=""):
        self.calls.append((name, args, reasoning))
        return {"error": "denied"} if self.fail else {"ok": True}


def test_tool_node_delegates_to_executor():
    ex_rec = _RecordingExecutor()
    handlers = build_default_handlers(tool_executor=ex_rec)
    g = TaskGraph()
    g.add("t", NodeType.TOOL, payload={"tool": "read_file", "args": {"path": "x"}})
    res = asyncio.run(TaskGraphExecutor(handlers, ExecutionBudget()).run(g))
    assert res.status == "completed"
    assert ex_rec.calls and ex_rec.calls[0][0] == "read_file"
    assert "taskgraph" in ex_rec.calls[0][2]


def test_tool_node_failure_propagates():
    ex_rec = _RecordingExecutor(fail=True)
    handlers = build_default_handlers(tool_executor=ex_rec)
    g = TaskGraph()
    g.add("t", NodeType.TOOL, payload={"tool": "network_scan", "args": {}},
          retry_policy=RetryPolicy(max_retries=0))
    res = asyncio.run(TaskGraphExecutor(handlers, ExecutionBudget()).run(g))
    assert res.status == "failed"


def test_agent_node_delegates_to_team_runtime():
    class _FakeTeam:
        def __init__(self):
            self.calls = []

        async def run_team(self, objective, roles, context, verify=False):
            self.calls.append((objective, roles))

            class _R:
                summary = "team synthesized"

                def to_dict(self):
                    return {"summary": "team synthesized"}
            return _R()

    team = _FakeTeam()
    handlers = build_default_handlers(team_runtime=team)
    g = TaskGraph()
    g.add("a", NodeType.AGENT, payload={"objective": "investigate", "roles": ["dfir"]})
    res = asyncio.run(TaskGraphExecutor(handlers, ExecutionBudget()).run(g))
    assert res.status == "completed"
    assert team.calls and team.calls[0][0] == "investigate"


def test_human_approval_fails_closed_by_default():
    handlers = build_default_handlers()   # no approval_fn
    g = TaskGraph()
    g.add("gate", NodeType.HUMAN_APPROVAL, retry_policy=RetryPolicy(max_retries=3))
    g.add("after", NodeType.REASON, depends_on=["gate"])
    ex = TaskGraphExecutor({**handlers, **_ok_handlers()}, ExecutionBudget())
    asyncio.run(ex.run(g))
    assert g.nodes["gate"].status == NodeStatus.BLOCKED
    assert g.nodes["after"].status == NodeStatus.SKIPPED
    # blocked gate is terminal — it must not have been retried
    assert g.nodes["gate"].attempts == 1


def test_human_approval_granted_proceeds():
    async def _approve(node):
        return True

    handlers = build_default_handlers(approval_fn=_approve)
    g = TaskGraph()
    g.add("gate", NodeType.HUMAN_APPROVAL)
    g.add("after", NodeType.REASON, depends_on=["gate"])
    ex = TaskGraphExecutor({**handlers, **_ok_handlers()}, ExecutionBudget())
    res = asyncio.run(ex.run(g))
    assert res.status == "completed"
    assert g.nodes["gate"].status == NodeStatus.COMPLETED
