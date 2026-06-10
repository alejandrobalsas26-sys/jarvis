"""
core/cognitive_engine.py — V58.0 COGNITIVE CORE planner / executor / critic loop.

A deterministic, LLM-optional agentic engine. It plans with heuristics first,
optionally enriches planning through an injected LLM client, executes steps
*exclusively* through the existing ToolExecutor.aexecute() guardrail path, then
self-evaluates via CriticEngine and the reflection heuristic. Tasks are bounded
by max_steps / max_retries / max_wall_seconds and fail closed on risky or
ambiguous actions.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from core.cognitive_types import (
    CognitivePlan,
    PlanStep,
    ExecutionTrace,
    ReflectionResult,
    RiskLevel,
    CompletionStatus,
    risk_rank,
)
from core.critic import CriticEngine, _DESTRUCTIVE_RE, _HIGH_RISK_TOOLS

# Heuristic objective-keyword → tool hints (defensive Blue Team verbs).
_TOOL_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("scan", "nmap", "port", "discover"), "network_scan"),
    (("isolate", "quarantine", "contain", "block ip"), "network_quarantine"),
    (("whois", "domain owner"), "whois_lookup"),
    (("read", "open file", "inspect file"), "read_file"),
    (("search", "look up", "research"), "web_search"),
    (("capture", "pcap", "packet"), "forensic_capture"),
]


class CognitiveEngine:
    """Bounded planner→executor→critic loop on top of the hardened ToolExecutor."""

    def __init__(
        self,
        tool_executor=None,
        llm_client=None,
        critic: CriticEngine | None = None,
        context_manager=None,
        memory=None,
        max_steps: int = 8,
        max_retries: int = 2,
        max_wall_seconds: int = 120,
    ) -> None:
        self.tool_executor = tool_executor
        self.llm_client = llm_client
        self.critic = critic or CriticEngine()
        self.context_manager = context_manager
        self.memory = memory
        self.max_steps = max(1, int(max_steps))
        self.max_retries = max(0, int(max_retries))
        self.max_wall_seconds = max(1, int(max_wall_seconds))

    # ── Planning ──────────────────────────────────────────────────────────────

    def create_plan(self, objective: str, context: dict | None = None) -> CognitivePlan:
        """Build a bounded deterministic plan; enrich via LLM only if available."""
        context = context or {}
        constraints = list(context.get("constraints", []))
        plan = CognitivePlan(
            objective=objective,
            constraints=constraints,
            completion_status=CompletionStatus.PENDING,
        )

        required = self._infer_tools(objective)
        plan.required_tools = required

        steps: list[PlanStep] = []
        idx = 0
        # 1) Always assess first (no tool, pure reasoning).
        steps.append(PlanStep(
            index=idx, action="assess objective and constraints",
            rationale="Deterministic triage before any tool use.",
            risk_level=RiskLevel.LOW,
        ))
        idx += 1

        # 2) One step per inferred tool, risk-tagged + approval-gated.
        for tool in required:
            risk = self._tool_risk(tool, objective)
            steps.append(PlanStep(
                index=idx,
                action=f"execute {tool}",
                tool=tool,
                tool_input=dict(context.get("tool_input", {}).get(tool, {})),
                rationale=f"Tool inferred from objective for '{tool}'.",
                risk_level=risk,
                requires_approval=(risk_rank(risk) >= risk_rank(RiskLevel.HIGH)
                                   or tool in _HIGH_RISK_TOOLS),
            ))
            idx += 1

        # 3) Always verify last.
        steps.append(PlanStep(
            index=idx, action="verify outcome and summarize evidence",
            rationale="Quality gate / evidence check before completion.",
            risk_level=RiskLevel.LOW,
        ))

        # Bound the plan length.
        plan.plan_steps = steps[: self.max_steps]
        plan.risk_level = self._aggregate_risk(plan.plan_steps)

        # Optional LLM enrichment — never required, never trusted to bypass gates.
        if self.llm_client is not None:
            try:
                self._enrich_with_llm(plan, context)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"COGNITIVE: LLM enrichment skipped: {e}")

        # Critic gate at plan time (advisory; recorded on the plan).
        verdict = self.critic.score_plan(plan)
        plan.confidence = verdict.get("overall", 0.0)
        if verdict.get("flags"):
            plan.errors.extend(f"plan_flag:{f}" for f in verdict["flags"])
        plan.touch()
        return plan

    def _infer_tools(self, objective: str) -> list[str]:
        low = objective.lower()
        tools: list[str] = []
        for keywords, tool in _TOOL_HINTS:
            if any(k in low for k in keywords) and tool not in tools:
                tools.append(tool)
        return tools

    @staticmethod
    def _tool_risk(tool: str, objective: str) -> RiskLevel:
        if _DESTRUCTIVE_RE.search(objective) or _DESTRUCTIVE_RE.search(tool):
            return RiskLevel.CRITICAL
        if tool in _HIGH_RISK_TOOLS:
            return RiskLevel.HIGH
        return RiskLevel.LOW

    @staticmethod
    def _aggregate_risk(steps: list[PlanStep]) -> RiskLevel:
        if not steps:
            return RiskLevel.LOW
        return max(steps, key=lambda s: risk_rank(s.risk_level)).risk_level

    def _enrich_with_llm(self, plan: CognitivePlan, context: dict) -> None:
        """Hook for optional LLM-driven step refinement (dependency-injected)."""
        # Intentionally conservative: only annotate rationale, never add tools
        # or relax approval gates from model output.
        enrich = getattr(self.llm_client, "enrich_plan", None)
        if callable(enrich):
            notes = enrich(plan.objective, [s.action for s in plan.plan_steps])
            if isinstance(notes, str) and notes:
                plan.observations.append({"llm_plan_notes": notes[:500]})

    # ── Step selection ──────────────────────────────────────────────────────-

    def select_next_step(self, plan: CognitivePlan) -> PlanStep | None:
        """Return the next PENDING step, or None when the plan is exhausted."""
        for step in plan.plan_steps:
            if step.status == CompletionStatus.PENDING:
                return step
        return None

    # ── Execution ───────────────────────────────────────────────────────────-

    async def execute_step(
        self, plan: CognitivePlan, step: PlanStep, tool_executor=None
    ) -> ExecutionTrace:
        """
        Execute a single step. Tool steps route through ToolExecutor.aexecute()
        ONLY — the engine never invokes handlers directly and never bypasses the
        guardrail/NATO path. Fails closed on ambiguous/destructive steps.
        """
        executor = tool_executor or self.tool_executor
        trace = ExecutionTrace(step_index=step.index, tool=step.tool)
        started = time.monotonic()
        step.status = CompletionStatus.IN_PROGRESS

        # Reasoning-only step (no tool) — record observation, succeed.
        if not step.tool:
            trace.observation = {"reasoning": step.action}
            trace.status = CompletionStatus.COMPLETED
            trace.duration_ms = (time.monotonic() - started) * 1000
            step.status = CompletionStatus.COMPLETED
            return trace

        # Fail closed: destructive intent is never auto-executed by the engine.
        blob = f"{step.action} {step.tool} {step.tool_input}"
        if _DESTRUCTIVE_RE.search(blob):
            trace.error = "blocked: destructive action requires operator-run step"
            trace.status = CompletionStatus.BLOCKED
            trace.duration_ms = (time.monotonic() - started) * 1000
            step.status = CompletionStatus.BLOCKED
            plan.errors.append(f"blocked_destructive:step_{step.index}")
            return trace

        # Fail closed: no executor means we cannot honor guardrails — do not run.
        if executor is None or not hasattr(executor, "aexecute"):
            trace.error = "blocked: no guarded ToolExecutor available"
            trace.status = CompletionStatus.BLOCKED
            trace.duration_ms = (time.monotonic() - started) * 1000
            step.status = CompletionStatus.BLOCKED
            return trace

        try:
            result = await executor.aexecute(
                tool_name=step.tool,
                tool_input=step.tool_input,
                reasoning=f"[COGNITIVE task={plan.task_id} step={step.index}] {step.rationale}",
            )
            trace.observation = result
            if isinstance(result, dict) and "error" in result:
                trace.error = str(result.get("error"))
                trace.status = CompletionStatus.FAILED
                step.status = CompletionStatus.FAILED
            else:
                trace.status = CompletionStatus.COMPLETED
                step.status = CompletionStatus.COMPLETED
        except Exception as e:
            trace.error = str(e)
            trace.status = CompletionStatus.FAILED
            step.status = CompletionStatus.FAILED

        trace.duration_ms = (time.monotonic() - started) * 1000
        plan.observations.append({"step": step.index, "observation": trace.observation})
        plan.touch()
        return trace

    # ── Reflection ──────────────────────────────────────────────────────────-

    def reflect(self, plan: CognitivePlan,
                traces: list[ExecutionTrace]) -> ReflectionResult:
        """Self-evaluate the executed batch; decide whether a retry is warranted."""
        issues: list[str] = []
        recommendations: list[str] = []
        failed = [t for t in traces if t.status in (
            CompletionStatus.FAILED, CompletionStatus.BLOCKED
        )]

        for t in failed:
            modes = self.critic.detect_failure_modes(t)
            issues.extend(f"step_{t.step_index}:{m}" for m in modes)
            recommendations.extend(self.critic.recommend_repair(modes))

        total = max(1, len(traces))
        confidence = round(1.0 - len(failed) / total, 2)
        success = not failed and plan.completion_status not in (
            CompletionStatus.ABORTED, CompletionStatus.FAILED
        )

        # Retry only transient/fixable failures — never operator-denied/blocked.
        retryable = any(
            i.endswith((":tool_error", ":timeout", ":unhandled_exception"))
            for i in issues
        )
        blocked = any(i.endswith((":operator_denied", ":blocked_by_guardrail"))
                      for i in issues)

        return ReflectionResult(
            success=success,
            confidence=confidence,
            issues=list(dict.fromkeys(issues)),
            recommendations=list(dict.fromkeys(recommendations)),
            should_retry=retryable and not blocked,
        )

    def should_retry(self, reflection: ReflectionResult) -> bool:
        return bool(reflection.should_retry)

    # ── Orchestration ──────────────────────────────────────────────────────-

    async def run_task(self, objective: str, context: dict | None = None) -> dict:
        """
        Run a full bounded task: plan → (execute → reflect → maybe retry)*.
        Respects max_steps, max_retries, and max_wall_seconds. Returns an
        audit-friendly dict and records the outcome to task memory if wired.
        """
        plan = self.create_plan(objective, context)
        plan.completion_status = CompletionStatus.IN_PROGRESS
        traces: list[ExecutionTrace] = []
        reflection = ReflectionResult()
        start = time.monotonic()
        retries = 0
        steps_run = 0

        while steps_run < self.max_steps:
            if time.monotonic() - start > self.max_wall_seconds:
                plan.completion_status = CompletionStatus.ABORTED
                plan.errors.append("wall_clock_timeout")
                break

            step = self.select_next_step(plan)
            if step is None:
                break

            trace = await self.execute_step(plan, step)
            traces.append(trace)
            steps_run += 1

            if trace.status in (CompletionStatus.FAILED, CompletionStatus.BLOCKED):
                reflection = self.reflect(plan, traces)
                if self.should_retry(reflection) and retries < self.max_retries:
                    retries += 1
                    step.status = CompletionStatus.PENDING  # re-arm for one retry
                    logger.debug(
                        f"COGNITIVE: retry {retries}/{self.max_retries} on step {step.index}"
                    )
                    continue
                if trace.status == CompletionStatus.BLOCKED:
                    # Fail closed — do not proceed past a blocked guardrail step.
                    plan.completion_status = CompletionStatus.BLOCKED
                    break

        if plan.completion_status == CompletionStatus.IN_PROGRESS:
            unfinished = self.select_next_step(plan)
            plan.completion_status = (
                CompletionStatus.COMPLETED if unfinished is None
                else CompletionStatus.FAILED
            )

        reflection = self.reflect(plan, traces)
        plan.confidence = reflection.confidence
        plan.touch()

        result = {
            "task_id": plan.task_id,
            "objective": objective,
            "status": plan.completion_status.value,
            "plan": plan.to_dict(),
            "traces": [t.to_dict() for t in traces],
            "reflection": reflection.to_dict(),
            "errors": list(plan.errors),
            "retries": retries,
            "steps_run": steps_run,
        }
        result["result_score"] = self.critic.score_result(objective, result)

        if self.memory is not None:
            try:
                self.memory.record_task(plan, traces, reflection)
            except Exception as e:  # pragma: no cover - best effort
                logger.debug(f"COGNITIVE: task memory write failed: {e}")

        return result
