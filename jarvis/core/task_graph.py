"""
core/task_graph.py — V63 Milestone 3: bounded, dependency-aware task graph.

A *bounded* execution graph for the small number of turns that genuinely need
multi-step planning (the fast path — simple turn → TaskDecision → direct
inference — is never routed here). The graph is the "Executive Runtime" layer of
the V63 architecture: it sequences REASON / TOOL / AGENT / VERIFY / SYNTHESIZE /
WAIT / HUMAN_APPROVAL nodes under hard safety limits.

Safety invariants (every one has a test):
  * cycle rejection at validation time (Kahn's algorithm);
  * dependency ordering — a node runs only after all its deps complete;
  * bounded fan-out — at most ``budget.max_fan_out`` nodes run concurrently;
  * caps on total nodes and graph depth, rejected at validation time;
  * global timeout + per-node timeout, both enforced;
  * cancellation propagation — a set CancelToken stops scheduling and cancels
    in-flight nodes;
  * retry limits — per-node AND a global runaway-retry ceiling;
  * partial failure — a permanently failed node skips only its transitive
    dependents; independent branches still complete;
  * the TOOL/AGENT node handlers delegate to the SAME protected ToolExecutor /
    controlled team runtime — there is no execution path here that bypasses the
    risk-class / HITL / scope / audit gates. HUMAN_APPROVAL fails closed.

Handlers are dependency-injected (``NodeHandler`` registry), so the executor is
fully unit-testable with fakes and needs no LLM/tools at test time. Production
handlers are built by :func:`build_default_handlers`.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from loguru import logger


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Node / status taxonomy ────────────────────────────────────────────────────
class NodeType(str, Enum):
    REASON = "reason"              # single-model reasoning step
    TOOL = "tool"                  # a protected ToolExecutor call
    AGENT = "agent"               # a controlled specialist-team run (M4)
    VERIFY = "verify"             # verifier pass over a prior node's output
    SYNTHESIZE = "synthesize"      # fan-in integration of dependency outputs
    WAIT = "wait"                 # bounded barrier / delay
    HUMAN_APPROVAL = "human_approval"  # HITL gate — fails closed by default


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"       # a dependency failed → this never runs
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"       # HUMAN_APPROVAL denied / pending


_TERMINAL = frozenset({
    NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.SKIPPED,
    NodeStatus.CANCELLED, NodeStatus.TIMED_OUT, NodeStatus.BLOCKED,
})
_UNSUCCESSFUL = frozenset({
    NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.CANCELLED,
    NodeStatus.TIMED_OUT, NodeStatus.BLOCKED,
})


class GraphValidationError(ValueError):
    """Raised when a graph violates a structural safety bound (cycle, cap)."""


# ── Policies / budgets ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 1          # attempts = max_retries + 1
    backoff_s: float = 0.0

    def total_attempts(self) -> int:
        return max(1, self.max_retries + 1)


@dataclass(frozen=True)
class ExecutionBudget:
    """Hard ceilings for a whole graph run. Conservative for the 15W host."""
    max_nodes: int = 32
    max_depth: int = 8
    max_fan_out: int = 4          # max concurrently-running nodes
    global_timeout_s: float = 300.0
    node_timeout_s: float = 60.0
    max_total_retries: int = 24   # global runaway-retry guard across all nodes


# The result a NodeHandler returns. `ok=False` triggers retry/failure handling.
@dataclass
class NodeOutcome:
    ok: bool
    output: Any = None
    error: str | None = None
    blocked: bool = False         # HUMAN_APPROVAL denied/pending → BLOCKED, no retry


CompletionCheck = Callable[[Any], bool]


@dataclass
class TaskNode:
    node_id: str
    type: NodeType
    description: str = ""
    payload: dict = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_s: float | None = None          # per-node override of budget.node_timeout_s
    parallel_safe: bool = True
    completion_check: CompletionCheck | None = None
    # ── runtime state ──
    status: NodeStatus = NodeStatus.PENDING
    output: Any = None
    error: str | None = None
    attempts: int = 0
    started_at: str | None = None
    ended_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id, "type": self.type.value,
            "description": self.description, "depends_on": list(self.depends_on),
            "status": self.status.value, "attempts": self.attempts,
            "error": self.error, "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class CancelToken:
    """Cooperative cancellation signal propagated into the executor."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass
class GraphContext:
    """Read-only-ish view handed to every handler: the outputs of already-
    completed nodes (for data flow) plus the cancel token."""
    outputs: dict[str, Any] = field(default_factory=dict)
    cancel: CancelToken = field(default_factory=CancelToken)

    def dep_outputs(self, node: TaskNode) -> dict[str, Any]:
        return {d: self.outputs.get(d) for d in node.depends_on}


NodeHandler = Callable[[TaskNode, GraphContext], Awaitable[NodeOutcome]]


# ── The graph ─────────────────────────────────────────────────────────────────
class TaskGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, TaskNode] = {}

    def add_node(self, node: TaskNode) -> TaskNode:
        if node.node_id in self.nodes:
            raise GraphValidationError(f"duplicate node id {node.node_id!r}")
        self.nodes[node.node_id] = node
        return node

    def add(
        self, node_id: str, ntype: NodeType, *,
        depends_on: "list[str] | tuple[str, ...]" = (),
        description: str = "", payload: dict | None = None,
        retry_policy: RetryPolicy | None = None, timeout_s: float | None = None,
        parallel_safe: bool = True, completion_check: CompletionCheck | None = None,
    ) -> TaskNode:
        return self.add_node(TaskNode(
            node_id=node_id, type=ntype, depends_on=tuple(depends_on),
            description=description, payload=payload or {},
            retry_policy=retry_policy or RetryPolicy(), timeout_s=timeout_s,
            parallel_safe=parallel_safe, completion_check=completion_check,
        ))

    # ── validation (structural safety bounds) ────────────────────────────────
    def validate(self, budget: ExecutionBudget) -> None:
        if len(self.nodes) > budget.max_nodes:
            raise GraphValidationError(
                f"graph has {len(self.nodes)} nodes > cap {budget.max_nodes}")
        # All dependencies must reference existing nodes.
        for n in self.nodes.values():
            for dep in n.depends_on:
                if dep not in self.nodes:
                    raise GraphValidationError(
                        f"node {n.node_id!r} depends on unknown node {dep!r}")
                if dep == n.node_id:
                    raise GraphValidationError(f"node {n.node_id!r} depends on itself")
        self._reject_cycles()
        depth = self._max_depth()
        if depth > budget.max_depth:
            raise GraphValidationError(
                f"graph depth {depth} > cap {budget.max_depth}")

    def _reject_cycles(self) -> None:
        """Kahn's algorithm — if not every node is consumable, a cycle exists."""
        indeg = {nid: len(n.depends_on) for nid, n in self.nodes.items()}
        dependents = self._dependents_map()
        queue = deque(nid for nid, d in indeg.items() if d == 0)
        seen = 0
        while queue:
            nid = queue.popleft()
            seen += 1
            for dep in dependents.get(nid, ()):
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    queue.append(dep)
        if seen != len(self.nodes):
            raise GraphValidationError("dependency cycle detected")

    def _dependents_map(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for nid, n in self.nodes.items():
            for dep in n.depends_on:
                out[dep].append(nid)
        return out

    def _max_depth(self) -> int:
        """Longest dependency chain length (memoized DFS; graph is a DAG here)."""
        memo: dict[str, int] = {}

        def depth(nid: str) -> int:
            if nid in memo:
                return memo[nid]
            deps = self.nodes[nid].depends_on
            memo[nid] = 1 + (max((depth(d) for d in deps), default=0))
            return memo[nid]

        return max((depth(nid) for nid in self.nodes), default=0)

    def critical_path(self) -> list[str]:
        """The longest dependency chain (by node count) — the min possible depth
        of any schedule. Deterministic tie-break by node id."""
        memo: dict[str, list[str]] = {}

        def longest(nid: str) -> list[str]:
            if nid in memo:
                return memo[nid]
            deps = self.nodes[nid].depends_on
            best: list[str] = []
            for d in sorted(deps):
                cand = longest(d)
                if len(cand) > len(best):
                    best = cand
            memo[nid] = best + [nid]
            return memo[nid]

        return max((longest(nid) for nid in sorted(self.nodes)),
                   key=len, default=[])

    def snapshot(self) -> dict:
        return {"nodes": [n.to_dict() for n in self.nodes.values()]}


@dataclass
class GraphRunResult:
    status: str                              # completed | partial | failed | cancelled | timed_out
    nodes: dict[str, dict]
    outputs: dict[str, Any]
    completed: list[str]
    failed: list[str]
    skipped: list[str]
    elapsed_s: float
    total_retries: int
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "status": self.status, "completed": self.completed,
            "failed": self.failed, "skipped": self.skipped,
            "elapsed_s": self.elapsed_s, "total_retries": self.total_retries,
            "nodes": self.nodes, "timestamp": self.timestamp,
        }


# ── The executor ──────────────────────────────────────────────────────────────
class TaskGraphExecutor:
    """Runs a validated :class:`TaskGraph` under an :class:`ExecutionBudget`.

    Concurrency is capped by ``budget.max_fan_out``; the whole run is wrapped in
    ``budget.global_timeout_s``; each node attempt is wrapped in its own timeout;
    retries are bounded per-node and globally; cancellation is cooperative."""

    def __init__(
        self,
        handlers: dict[NodeType, NodeHandler],
        budget: ExecutionBudget | None = None,
        *,
        cancel: CancelToken | None = None,
        broadcast_fn: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self.handlers = handlers
        self.budget = budget or ExecutionBudget()
        self.cancel = cancel or CancelToken()
        self._broadcast = broadcast_fn
        self._total_retries = 0

    async def _emit(self, event: dict) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast({**event, "timestamp": _now_iso()})
        except Exception:
            pass

    async def run(self, graph: TaskGraph) -> GraphRunResult:
        import time
        graph.validate(self.budget)
        start = time.monotonic()
        try:
            await asyncio.wait_for(self._run_inner(graph), timeout=self.budget.global_timeout_s)
            overall = self._overall_status(graph)
        except asyncio.TimeoutError:
            logger.warning("TASKGRAPH: global timeout — cancelling in-flight nodes")
            self.cancel.cancel()
            for n in graph.nodes.values():
                if n.status in (NodeStatus.RUNNING, NodeStatus.READY, NodeStatus.PENDING):
                    n.status = NodeStatus.TIMED_OUT
            overall = "timed_out"
        elapsed = round(time.monotonic() - start, 2)
        completed = [nid for nid, n in graph.nodes.items() if n.status == NodeStatus.COMPLETED]
        failed = [nid for nid, n in graph.nodes.items()
                  if n.status in (NodeStatus.FAILED, NodeStatus.TIMED_OUT, NodeStatus.BLOCKED)]
        skipped = [nid for nid, n in graph.nodes.items()
                   if n.status in (NodeStatus.SKIPPED, NodeStatus.CANCELLED)]
        return GraphRunResult(
            status=overall,
            nodes={nid: n.to_dict() for nid, n in graph.nodes.items()},
            outputs={nid: n.output for nid, n in graph.nodes.items()
                     if n.status == NodeStatus.COMPLETED},
            completed=completed, failed=failed, skipped=skipped,
            elapsed_s=elapsed, total_retries=self._total_retries,
        )

    async def _run_inner(self, graph: TaskGraph) -> None:
        indeg = {nid: len(n.depends_on) for nid, n in graph.nodes.items()}
        dependents = graph._dependents_map()
        ctx = GraphContext(outputs={}, cancel=self.cancel)
        ready: deque[str] = deque(nid for nid, d in indeg.items() if d == 0)
        for nid in ready:
            graph.nodes[nid].status = NodeStatus.READY
        running: dict[str, asyncio.Task] = {}

        while ready or running:
            if self.cancel.cancelled:
                for t in running.values():
                    t.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                for nid, n in graph.nodes.items():
                    if n.status in (NodeStatus.READY, NodeStatus.PENDING, NodeStatus.RUNNING):
                        n.status = NodeStatus.CANCELLED
                return

            # Schedule ready nodes up to the fan-out cap.
            while ready and len(running) < self.budget.max_fan_out:
                nid = ready.popleft()
                node = graph.nodes[nid]
                node.status = NodeStatus.RUNNING
                node.started_at = _now_iso()
                await self._emit({"type": "graph_node_running", "node": nid, "kind": node.type.value})
                running[nid] = asyncio.ensure_future(self._run_node(node, ctx))

            if not running:
                break
            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            # Map finished tasks back to node ids.
            finished = [nid for nid, t in running.items() if t in done]
            for nid in finished:
                task = running.pop(nid)
                node = graph.nodes[nid]
                node.ended_at = _now_iso()
                try:
                    outcome: NodeOutcome = task.result()
                except asyncio.CancelledError:
                    node.status = NodeStatus.CANCELLED
                    outcome = NodeOutcome(ok=False, error="cancelled")
                except Exception as e:  # noqa: BLE001 — handler crash is a node failure, not a run crash
                    node.status = NodeStatus.FAILED
                    node.error = str(e)[:200]
                    outcome = NodeOutcome(ok=False, error=node.error)

                if outcome.ok:
                    node.status = NodeStatus.COMPLETED
                    node.output = outcome.output
                    ctx.outputs[nid] = outcome.output
                    await self._emit({"type": "graph_node_complete", "node": nid})
                    for dep in dependents.get(nid, ()):
                        indeg[dep] -= 1
                        if indeg[dep] == 0 and graph.nodes[dep].status == NodeStatus.PENDING:
                            graph.nodes[dep].status = NodeStatus.READY
                            ready.append(dep)
                else:
                    if node.status not in _TERMINAL:
                        node.status = NodeStatus.BLOCKED if outcome.blocked else NodeStatus.FAILED
                    node.error = node.error or outcome.error
                    await self._emit({"type": "graph_node_failed", "node": nid,
                                      "status": node.status.value, "error": node.error})
                    self._cascade_skip(graph, dependents, nid, indeg)

    def _cascade_skip(
        self, graph: TaskGraph, dependents: dict[str, list[str]],
        failed_id: str, indeg: dict[str, int],
    ) -> None:
        """Mark the transitive dependents of a failed node SKIPPED — partial
        failure: independent branches are untouched."""
        stack = list(dependents.get(failed_id, ()))
        while stack:
            nid = stack.pop()
            n = graph.nodes[nid]
            if n.status in _TERMINAL:
                continue
            n.status = NodeStatus.SKIPPED
            stack.extend(dependents.get(nid, ()))

    async def _run_node(self, node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        handler = self.handlers.get(node.type)
        if handler is None:
            return NodeOutcome(ok=False, error=f"no handler for {node.type.value}")
        node_timeout = node.timeout_s or self.budget.node_timeout_s
        attempts = node.retry_policy.total_attempts()
        last_err: str | None = None
        for attempt in range(attempts):
            if self.cancel.cancelled:
                return NodeOutcome(ok=False, error="cancelled")
            node.attempts = attempt + 1
            try:
                outcome = await asyncio.wait_for(handler(node, ctx), timeout=node_timeout)
            except asyncio.TimeoutError:
                node.status = NodeStatus.TIMED_OUT
                last_err = "node timeout"
                outcome = NodeOutcome(ok=False, error=last_err)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:200]
                outcome = NodeOutcome(ok=False, error=last_err)

            # A HUMAN_APPROVAL denial is terminal — never retried.
            if outcome.blocked:
                return outcome
            if outcome.ok and self._passes_completion(node, outcome):
                return outcome
            last_err = outcome.error or "completion condition not met"

            # Retry budget: stop on the global runaway ceiling.
            if attempt + 1 < attempts:
                if self._total_retries >= self.budget.max_total_retries:
                    logger.warning("TASKGRAPH: global retry ceiling hit — no more retries")
                    break
                self._total_retries += 1
                if node.retry_policy.backoff_s > 0:
                    await asyncio.sleep(node.retry_policy.backoff_s)
        return NodeOutcome(ok=False, error=last_err)

    @staticmethod
    def _passes_completion(node: TaskNode, outcome: NodeOutcome) -> bool:
        if node.completion_check is None:
            return True
        try:
            return bool(node.completion_check(outcome.output))
        except Exception:
            return False

    @staticmethod
    def _overall_status(graph: TaskGraph) -> str:
        statuses = {n.status for n in graph.nodes.values()}
        if statuses <= {NodeStatus.COMPLETED}:
            return "completed"
        if NodeStatus.CANCELLED in statuses:
            return "cancelled"
        any_ok = any(s == NodeStatus.COMPLETED for s in statuses)
        any_bad = bool(statuses & _UNSUCCESSFUL)
        if any_ok and any_bad:
            return "partial"
        if any_bad:
            return "failed"
        return "completed"


# ════════════════════════════════════════════════════════════════════════════
#  Production handler registry (delegates to the protected executor / team)
# ════════════════════════════════════════════════════════════════════════════
def build_default_handlers(
    *,
    infer: Callable[..., Awaitable[str]] | None = None,
    tool_executor=None,
    team_runtime=None,
    verifier: Callable[[str, str], Awaitable[bool]] | None = None,
    approval_fn: Callable[[TaskNode], Awaitable[bool]] | None = None,
) -> dict[NodeType, NodeHandler]:
    """Build node handlers wired to the live subsystems. Every side-effecting
    node delegates to an already-gated path:

      * TOOL  → ``tool_executor.aexecute`` (risk-class / HITL / scope / audit).
      * AGENT → ``team_runtime.run_team`` / ``run_team_for_decision`` (M4).
      * HUMAN_APPROVAL → ``approval_fn`` (default: **deny**, fail-closed).

    Any subsystem left ``None`` yields a handler that fails its node cleanly
    (never a bypass, never a crash)."""

    async def _reason(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        if infer is None:
            return NodeOutcome(ok=False, error="no inference backend")
        prompt = node.payload.get("prompt") or node.description
        dep_ctx = "\n".join(str(v) for v in ctx.dep_outputs(node).values() if v)
        text = await infer(
            node.payload.get("system", "You are a precise reasoning step."),
            f"{prompt}\n\n{dep_ctx}".strip(),
        )
        return NodeOutcome(ok=bool(text), output=text, error=None if text else "empty")

    async def _tool(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        if tool_executor is None:
            return NodeOutcome(ok=False, error="no tool gateway")
        name = node.payload.get("tool")
        args = node.payload.get("args", {})
        if not name:
            return NodeOutcome(ok=False, error="tool node missing 'tool'")
        # Delegate to the SAME protected gate the live turn uses — no bypass.
        result = await tool_executor.aexecute(name, args, f"[taskgraph:{node.node_id}]")
        ok = not (isinstance(result, dict) and "error" in result)
        return NodeOutcome(ok=ok, output=result,
                           error=None if ok else str(result.get("error"))[:200])

    async def _agent(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        if team_runtime is None:
            return NodeOutcome(ok=False, error="no team runtime")
        objective = node.payload.get("objective") or node.description
        roles = node.payload.get("roles")
        context = {"dep_" + k: v for k, v in ctx.dep_outputs(node).items() if v}
        context.update(node.payload.get("context", {}))
        if roles:
            from core.specialist_runtime import SpecialistRole
            role_objs = [SpecialistRole(r) if not isinstance(r, SpecialistRole) else r
                         for r in roles]
            res = await team_runtime.run_team(objective, role_objs, context,
                                              verify=bool(node.payload.get("verify")))
        else:
            td = node.payload.get("task_decision")
            res = await team_runtime.run_team_for_decision(td, objective, context) if td else None
        if res is None:
            return NodeOutcome(ok=False, error="no team formed")
        return NodeOutcome(ok=bool(res.summary), output=res.to_dict(),
                           error=None if res.summary else "empty synthesis")

    async def _verify(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        if verifier is None:
            return NodeOutcome(ok=False, error="no verifier")
        deps = ctx.dep_outputs(node)
        draft = node.payload.get("draft") or "\n".join(str(v) for v in deps.values() if v)
        prompt = node.payload.get("prompt", node.description)
        ok = await verifier(prompt, draft)
        return NodeOutcome(ok=bool(ok), output={"verified": bool(ok)},
                           error=None if ok else "verification failed")

    async def _synthesize(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        parts = [f"[{k}] {v}" for k, v in ctx.dep_outputs(node).items() if v]
        joined = "\n\n".join(parts)
        if infer is not None and joined:
            try:
                joined = await infer(
                    "You synthesize prior step outputs into one coherent result.",
                    f"OBJECTIVE: {node.description}\n\nSTEP OUTPUTS:\n{joined}",
                )
            except Exception:
                pass
        return NodeOutcome(ok=bool(joined), output=joined, error=None if joined else "nothing to synthesize")

    async def _wait(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        delay = float(node.payload.get("seconds", 0.0))
        await asyncio.sleep(min(delay, 5.0))   # bounded — never an open-ended wait
        return NodeOutcome(ok=True, output={"waited_s": min(delay, 5.0)})

    async def _human_approval(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
        # Fail closed: without an explicit approval callback, the gate BLOCKS.
        if approval_fn is None:
            return NodeOutcome(ok=False, blocked=True, error="human approval required (no approver)")
        granted = await approval_fn(node)
        return NodeOutcome(ok=bool(granted), blocked=not granted,
                           output={"approved": bool(granted)},
                           error=None if granted else "human approval denied")

    return {
        NodeType.REASON: _reason,
        NodeType.TOOL: _tool,
        NodeType.AGENT: _agent,
        NodeType.VERIFY: _verify,
        NodeType.SYNTHESIZE: _synthesize,
        NodeType.WAIT: _wait,
        NodeType.HUMAN_APPROVAL: _human_approval,
    }
