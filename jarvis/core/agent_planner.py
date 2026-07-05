"""
core/agent_planner.py — V63 Milestone 3 bridge: TaskDecision → bounded graph.

The thin, production-wired layer between the per-turn ``TaskDecision`` (M1) and
the bounded :mod:`core.task_graph` executor (M3). It decides *whether* a turn
warrants planning at all (the fast path never plans), builds a **small** bounded
graph for the ones that do, and runs it against the live subsystems:

  * REASON / SYNTHESIZE nodes → the shared inference client;
  * AGENT nodes             → the controlled specialist team runtime (M4);
  * VERIFY nodes            → the fail-closed verifier;
  * TOOL / HUMAN_APPROVAL   → the protected ToolExecutor / HITL gate.

No node bypasses an existing gate. Everything is dependency-injected, so the
bridge is unit-testable with fakes and needs no Ollama/tools at test time.
``attach()`` wires the production subsystems; the module singleton
``agent_planner`` is attached in ``main`` and driven by the AURA ``plan_task``
command.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from core.task_graph import (
    ExecutionBudget,
    GraphRunResult,
    NodeType,
    RetryPolicy,
    TaskGraph,
    TaskGraphExecutor,
    build_default_handlers,
)

# Complexity at/above which a turn is planning-worthy even without a planning
# domain — mirrors the runtime's escalation threshold.
_PLAN_COMPLEXITY = 0.75


def should_plan(task_decision, *, explicit: bool = False) -> bool:
    """True only when a turn genuinely needs multi-step orchestration. Simple
    chat (no planning domain, low complexity, no explicit request) stays on the
    direct single-inference fast path and returns False."""
    if explicit:
        return True
    return bool(
        getattr(task_decision, "requires_planning", False)
        or getattr(task_decision, "complexity", 0.0) >= _PLAN_COMPLEXITY
    )


def build_graph_for_objective(objective: str, task_decision) -> TaskGraph:
    """A deliberately small, bounded graph for one planning turn.

    Shape:  [analyze] → [synthesize] → (optional) [verify]

    ``analyze`` is an AGENT node (controlled team) when the decision prefers a
    team, else a single REASON node — so the fast-ish DEEP path is not forced
    through a full team when one specialist suffices."""
    g = TaskGraph()
    prefers_team = getattr(task_decision, "prefers_agent_team", False)
    requires_verification = getattr(task_decision, "requires_verification", False)

    if prefers_team:
        g.add(
            "analyze", NodeType.AGENT,
            description=objective,
            payload={"objective": objective, "task_decision": task_decision},
            retry_policy=RetryPolicy(max_retries=0),
        )
    else:
        g.add(
            "analyze", NodeType.REASON,
            description=objective,
            payload={"prompt": objective,
                     "system": "You are a rigorous analyst. Decompose and solve "
                               "the task step by step; state assumptions."},
            retry_policy=RetryPolicy(max_retries=1),
        )

    g.add(
        "synthesize", NodeType.SYNTHESIZE, description=objective,
        depends_on=["analyze"], retry_policy=RetryPolicy(max_retries=0),
    )
    if requires_verification:
        g.add(
            "verify", NodeType.VERIFY, description="verify the synthesis",
            depends_on=["synthesize"],
            payload={"prompt": objective},
            retry_policy=RetryPolicy(max_retries=0),
        )
    return g


class AgentPlanner:
    """Production planner: builds and runs bounded graphs against live subsystems."""

    def __init__(
        self,
        *,
        infer: Callable[..., Awaitable[str]] | None = None,
        tool_executor=None,
        team_runtime=None,
        verifier: Callable[[str, str], Awaitable[bool]] | None = None,
        approval_fn: Callable[..., Awaitable[bool]] | None = None,
        broadcast_fn: Callable[[dict], Awaitable[None]] | None = None,
        budget: ExecutionBudget | None = None,
    ) -> None:
        self._infer = infer
        self._tool_executor = tool_executor
        self._team_runtime = team_runtime
        self._verifier = verifier
        self._approval_fn = approval_fn
        self._broadcast = broadcast_fn
        self.budget = budget or ExecutionBudget()

    def attach(
        self,
        *,
        ollama_client=None,
        fast_model: str = "",
        deep_model: str = "",
        llm_client=None,
        tool_executor=None,
        team_runtime=None,
        broadcast_fn=None,
        approval_fn=None,
    ) -> None:
        """Wire production subsystems. ``infer`` reuses the same shared-client
        inference the team runtime uses; ``verifier`` wraps the fail-closed
        core.verification pass."""
        if ollama_client is not None and fast_model and deep_model:
            from core.specialist_runtime import _make_ollama_infer, ModelTier

            base = _make_ollama_infer(ollama_client, fast_model, deep_model)

            async def _infer(system: str, user: str) -> str:
                return await base(
                    system, user, tier=ModelTier.DEEP,
                    timeout_s=self.budget.node_timeout_s,
                    num_ctx=2048, temperature=0.2,
                )
            self._infer = _infer

        if llm_client is not None:
            from core.verification import verify_answer

            async def _verifier(prompt: str, draft: str) -> bool:
                res = await verify_answer(llm_client, prompt, draft)
                return bool(res.verified)
            self._verifier = _verifier

        if tool_executor is not None:
            self._tool_executor = tool_executor
        if team_runtime is not None:
            self._team_runtime = team_runtime
        if broadcast_fn is not None:
            self._broadcast = broadcast_fn
        if approval_fn is not None:
            self._approval_fn = approval_fn
        logger.info("V63 M3: agent_planner attached")

    def _handlers(self):
        return build_default_handlers(
            infer=self._infer,
            tool_executor=self._tool_executor,
            team_runtime=self._team_runtime,
            verifier=self._verifier,
            approval_fn=self._approval_fn,
        )

    async def plan_and_run(
        self, objective: str, task_decision, *, cancel=None,
    ) -> GraphRunResult:
        """Build the bounded graph for *objective* and execute it. The caller is
        responsible for having already decided planning is warranted (see
        :func:`should_plan`)."""
        graph = build_graph_for_objective(objective, task_decision)
        executor = TaskGraphExecutor(
            self._handlers(), self.budget,
            cancel=cancel, broadcast_fn=self._broadcast,
        )
        logger.info(
            f"PLANNER: running graph ({len(graph.nodes)} nodes, "
            f"critical_path={graph.critical_path()}) for {objective[:60]!r}"
        )
        return await executor.run(graph)


# Module singleton — attached in main.py, driven by the AURA `plan_task` command.
agent_planner = AgentPlanner()
