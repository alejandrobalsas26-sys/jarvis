"""
core/runbook_engine.py — V66 Milestone 24: guarded runbook orchestration.

Modernizes the existing ``core.playbook_engine`` (YAML SOAR playbooks) into typed,
**guarded, TaskGraph-backed** runbooks — WITHOUT a second executor and without
weakening any gate. It preserves legacy YAML compatibility (``from_legacy_playbook``)
and the legacy incident-matching semantics, but every WORLD-EFFECT action is
compiled to a TaskGraph node whose handler routes through the canonical path:

    RunbookStep → TaskGraph node → ToolExecutor.aexecute
        → Authority / ScopePolicy → RiskClass → HITL → Audit

There is exactly one execution primitive here — ``ToolExecutor.aexecute`` — the
same one the live turn, the task graph, and the incident workspace all use.
Legacy playbooks that called a side-effect function directly (isolate_ip,
snapshot_vm, run_nmap, …) are *migrated*: their effect is routed through the
guarded tool gate, so an un-registered effect fails closed instead of bypassing.

Every runbook supports: dry-run (plan without execution), parameter validation,
preconditions, scope check, risk classification, per-step + global timeouts,
cooperative cancellation, HITL preservation (both an optional explicit approval
gate AND ToolExecutor's own NATO challenge), postcondition verification, rollback
hints, and a full audit trail. SelfDebugRuntime wraps only retryable failures
(bounded retries; a destructive step gets exactly one attempt; scope/auth failures
escalate; unknown failures stop and report — no scope/authority/HITL expansion).

Dependency-injected executor + self-debug + clock → unit-testable with fakes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable

import yaml
from loguru import logger

from core.self_debug import SelfDebugRuntime
from core.task_graph import (
    CancelToken,
    ExecutionBudget,
    GraphContext,
    NodeOutcome,
    NodeType,
    RetryPolicy,
    TaskGraph,
    TaskGraphExecutor,
    TaskNode,
)

SCHEMA_VERSION = "runbook-1"
_TARGET_RE = re.compile(r"^[A-Za-z0-9._:\-/\[\]]{1,120}$")   # no shell metacharacters


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
#  Parameters / conditions
# ══════════════════════════════════════════════════════════════════════════════
class ParamType(str, Enum):
    STRING = "string"
    INT = "int"
    TARGET = "target"          # ip / hostname / url — validated, no metacharacters
    BOOL = "bool"


@dataclass(frozen=True)
class RunbookParameter:
    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    default: object = None
    description: str = ""

    def validate(self, value) -> tuple[bool, object, str]:
        """Return (ok, coerced_value, error)."""
        if value is None or value == "":
            if self.required and self.default is None:
                return False, None, f"missing required parameter '{self.name}'"
            value = self.default
            if value is None:
                return True, None, ""
        if self.type is ParamType.INT:
            try:
                return True, int(value), ""
            except (TypeError, ValueError):
                return False, None, f"parameter '{self.name}' must be an integer"
        if self.type is ParamType.BOOL:
            return True, bool(value), ""
        if self.type is ParamType.TARGET:
            if not _TARGET_RE.match(str(value)):
                return False, None, f"parameter '{self.name}' has an invalid target format"
            return True, str(value), ""
        return True, str(value), ""


# A precondition/postcondition is a pure predicate over the run context.
Condition = Callable[[dict], "tuple[bool, str]"]


@dataclass(frozen=True)
class RunbookPrecondition:
    name: str
    predicate: Condition
    description: str = ""


@dataclass(frozen=True)
class RunbookPostcondition:
    name: str
    predicate: Condition
    description: str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  Steps / definition
# ══════════════════════════════════════════════════════════════════════════════
class StepKind(str, Enum):
    DIAGNOSTIC = "diagnostic"    # read-only tool
    ACTION = "action"            # world-effect tool (guarded)
    REASON = "reason"            # no world-effect; note / narration


@dataclass(frozen=True)
class RunbookStep:
    id: str
    description: str
    kind: StepKind = StepKind.DIAGNOSTIC
    action: str | None = None                    # ToolExecutor tool name
    params: dict = field(default_factory=dict)   # {{param}}-interpolated
    precondition: RunbookPrecondition | None = None
    postcondition: RunbookPostcondition | None = None
    rollback_hint: str = ""
    destructive: bool = False
    requires_approval: bool = False
    optional: bool = False                       # a failed optional step does not fail the run
    timeout_s: float = 60.0


@dataclass(frozen=True)
class RunbookDefinition:
    name: str
    description: str
    parameters: tuple[RunbookParameter, ...] = ()
    steps: tuple[RunbookStep, ...] = ()
    # legacy-compat trigger (preserved matching behavior)
    trigger: dict = field(default_factory=dict)

    def validate_params(self, params: dict) -> tuple[bool, dict, list[str]]:
        out: dict = {}
        errors: list[str] = []
        for spec in self.parameters:
            ok, val, err = spec.validate((params or {}).get(spec.name))
            if not ok:
                errors.append(err)
            elif val is not None:
                out[spec.name] = val
        return (not errors), out, errors

    def matches(self, incident: dict) -> bool:
        """Preserved legacy matching semantics (incident_type / severity_min /
        mitre_any). Selection ≠ execution — matching never runs anything."""
        t = self.trigger or {}
        if not t:
            return False
        if t.get("incident_type") and t["incident_type"] != incident.get("rule"):
            return False
        if incident.get("severity_score", 0) < t.get("severity_min", 0):
            return False
        if t.get("mitre_any"):
            if not set(incident.get("mitre_techniques", [])) & set(t["mitre_any"]):
                return False
        return True

    # ── legacy YAML compatibility ─────────────────────────────────────────────
    @classmethod
    def from_legacy_playbook(cls, data: dict) -> "RunbookDefinition":
        """Adapt a legacy core.playbook_engine YAML playbook. Actions that had a
        direct side-effect are MIGRATED onto the guarded tool gate; diagnostics/
        notifications become REASON steps. No effect bypasses ToolExecutor."""
        steps: list[RunbookStep] = []
        for i, raw in enumerate(data.get("steps", [])):
            action = raw.get("action", "")
            kind, tool, destructive, approval = _map_legacy_action(action)
            steps.append(RunbookStep(
                id=f"{action or 'step'}_{i}", description=action,
                kind=kind, action=tool, params=dict(raw.get("params", {})),
                destructive=destructive, requires_approval=approval,
                rollback_hint=raw.get("rollback_hint", "")))
        return cls(
            name=str(data.get("name", "legacy_playbook")),
            description=str(data.get("description", "")),
            steps=tuple(steps), trigger=dict(data.get("trigger", {})),
        )


# Legacy action → (kind, guarded tool name, destructive, requires_approval).
# A world-effect legacy action is routed through the guarded tool gate; a
# notification/telemetry action becomes a non-effecting REASON step.
_LEGACY_ACTION_MAP: dict[str, tuple[StepKind, str | None, bool, bool]] = {
    "broadcast_alert": (StepKind.REASON, None, False, False),
    "store_episode": (StepKind.REASON, None, False, False),
    "run_binary_inversion": (StepKind.REASON, None, False, False),
    "run_nmap": (StepKind.ACTION, "network_scan", False, True),
    "run_volatility": (StepKind.ACTION, "forensic_capture", True, True),
    "snapshot_vm": (StepKind.ACTION, "forensic_capture", False, True),
    "isolate_ip": (StepKind.ACTION, "isolate_ip", True, True),
}


def _map_legacy_action(action: str):
    return _LEGACY_ACTION_MAP.get(action, (StepKind.ACTION, action or None, False, True))


# ══════════════════════════════════════════════════════════════════════════════
#  Audit / result / plan
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class RunbookAuditEntry:
    step_id: str
    action: str | None
    kind: str
    risk_class: str
    requires_hitl: bool
    status: str                  # completed / failed / blocked / skipped / dry_run
    summary: str = ""
    rollback_hint: str = ""
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"step_id": self.step_id, "action": self.action, "kind": self.kind,
                "risk_class": self.risk_class, "requires_hitl": self.requires_hitl,
                "status": self.status, "summary": self.summary,
                "rollback_hint": self.rollback_hint, "ts": self.ts}


@dataclass
class RunbookPlan:
    """A dry-run plan: exactly what WOULD run, with per-step risk/HITL/scope — and
    zero execution."""
    runbook: str
    params: dict
    steps: list[dict]
    requires_hitl_steps: list[str]
    scope_targets: list[str]

    def to_dict(self) -> dict:
        return {"runbook": self.runbook, "params": dict(self.params),
                "steps": self.steps, "requires_hitl_steps": self.requires_hitl_steps,
                "scope_targets": self.scope_targets, "dry_run": True}


@dataclass
class RunbookResult:
    runbook: str
    status: str                  # completed / partial / failed / blocked / cancelled / dry_run
    params: dict
    audit: list[RunbookAuditEntry] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    rollback_hints: list[str] = field(default_factory=list)
    plan: RunbookPlan | None = None
    elapsed_s: float = 0.0
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"runbook": self.runbook, "status": self.status, "params": dict(self.params),
                "audit": [a.to_dict() for a in self.audit], "outputs": dict(self.outputs),
                "rollback_hints": list(self.rollback_hints),
                "plan": self.plan.to_dict() if self.plan else None,
                "elapsed_s": self.elapsed_s, "timestamp": self.timestamp}


def _classify(tool_name: str | None) -> tuple[str, bool]:
    if not tool_name:
        return "read_only", False
    try:
        from core.risk_classes import classify_tool, requires_hitl
        rc = classify_tool(tool_name)
        return rc.value, requires_hitl(rc)
    except Exception:  # noqa: BLE001 — fail-closed to HITL
        return "high_impact", True


def _scope_target(tool_name: str | None, params: dict) -> str | None:
    try:
        from core.authority import _SCOPE_BOUND_TOOLS  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    for f in _SCOPE_BOUND_TOOLS.get(tool_name or "", ()):
        v = (params or {}).get(f)
        if v:
            return str(v)
    return None


def _interp(params: dict, ctx: dict) -> dict:
    def sub(v):
        if not isinstance(v, str):
            return v
        return re.sub(r"\{\{(.*?)\}\}", lambda m: str(ctx.get(m.group(1).strip(), "")), v)
    return {k: sub(v) for k, v in (params or {}).items()}


# ══════════════════════════════════════════════════════════════════════════════
#  Registry + engine
# ══════════════════════════════════════════════════════════════════════════════
class RunbookRegistry:
    def __init__(self) -> None:
        self.runbooks: dict[str, RunbookDefinition] = {}

    def register(self, rb: RunbookDefinition) -> None:
        self.runbooks[rb.name] = rb

    def get(self, name: str) -> RunbookDefinition | None:
        return self.runbooks.get(name)

    def names(self) -> list[str]:
        return sorted(self.runbooks)

    def match_for_incident(self, incident: dict) -> list[str]:
        return sorted(n for n, rb in self.runbooks.items() if rb.matches(incident))

    def load_legacy_dir(self, directory) -> int:
        from pathlib import Path
        count = 0
        for path in sorted(Path(directory).glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.register(RunbookDefinition.from_legacy_playbook(data))
                count += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"RUNBOOK: failed to load legacy {path.name}: {e}")
        return count


ExecFn = Callable[[str, dict, str], "Awaitable[object]"]


class RunbookEngine:
    """Compiles runbooks to guarded TaskGraphs and runs them through the single
    ToolExecutor gate. No second executor; no world-effect bypass."""

    def __init__(self, *, tool_executor=None, exec_fn: ExecFn | None = None,
                 self_debug: SelfDebugRuntime | None = None, broadcast_fn=None,
                 registry: RunbookRegistry | None = None) -> None:
        self._tool_executor = tool_executor
        self._exec_fn = exec_fn
        self._self_debug = self_debug or SelfDebugRuntime(max_retries=1)
        self._broadcast_fn = broadcast_fn
        self.registry = registry or RunbookRegistry()
        if not self.registry.runbooks:
            for rb in _default_runbooks():
                self.registry.register(rb)

    def attach(self, *, tool_executor=None, broadcast_fn=None) -> None:
        """Wire the live guarded ToolExecutor + HUD broadcast. Until wired, world-
        effect actions fail closed (there is no direct-effect path)."""
        if tool_executor is not None:
            self._tool_executor = tool_executor
        if broadcast_fn is not None:
            self._broadcast_fn = broadcast_fn

    async def _exec(self, tool: str, args: dict, reasoning: str) -> object:
        """The ONLY world-effect primitive: the guarded ToolExecutor gate."""
        if self._exec_fn is not None:
            return await self._exec_fn(tool, args, reasoning)
        if self._tool_executor is not None:
            return await self._tool_executor.aexecute(tool, args, reasoning)
        return {"error": "no tool executor wired — runbook cannot effect the world"}

    # ── dry run (no execution) ────────────────────────────────────────────────
    def dry_run(self, name: str, params: dict | None = None) -> RunbookResult:
        rb = self.registry.get(name)
        if rb is None:
            return RunbookResult(runbook=name, status="failed", params=params or {},
                                 audit=[RunbookAuditEntry(step_id="-", action=None,
                                        kind="-", risk_class="-", requires_hitl=False,
                                        status="failed", summary=f"unknown runbook '{name}'")])
        ok, coerced, errors = rb.validate_params(params or {})
        if not ok:
            return RunbookResult(runbook=name, status="failed", params=params or {},
                                 audit=[RunbookAuditEntry(step_id="params", action=None,
                                        kind="-", risk_class="-", requires_hitl=False,
                                        status="blocked", summary="; ".join(errors))])
        step_views: list[dict] = []
        hitl_steps: list[str] = []
        targets: list[str] = []
        for step in rb.steps:
            args = _interp(step.params, coerced)
            risk, hitl = _classify(step.action)
            tgt = _scope_target(step.action, args)
            if hitl or step.requires_approval:
                hitl_steps.append(step.id)
            if tgt:
                targets.append(tgt)
            step_views.append({
                "id": step.id, "description": step.description, "kind": step.kind.value,
                "action": step.action, "args": args, "risk_class": risk,
                "requires_hitl": hitl or step.requires_approval, "scope_target": tgt,
                "rollback_hint": step.rollback_hint, "destructive": step.destructive,
            })
        plan = RunbookPlan(runbook=name, params=coerced, steps=step_views,
                           requires_hitl_steps=hitl_steps, scope_targets=targets)
        return RunbookResult(runbook=name, status="dry_run", params=coerced, plan=plan)

    # ── guarded execution ─────────────────────────────────────────────────────
    async def execute(
        self, name: str, params: dict | None = None, *, dry_run: bool = False,
        cancel: CancelToken | None = None, approval_fn=None,
        precondition_ctx: dict | None = None, global_timeout_s: float = 300.0,
    ) -> RunbookResult:
        if dry_run:
            return self.dry_run(name, params)
        rb = self.registry.get(name)
        if rb is None:
            return RunbookResult(runbook=name, status="failed", params=params or {})
        ok, coerced, errors = rb.validate_params(params or {})
        if not ok:
            return RunbookResult(
                runbook=name, status="blocked", params=params or {},
                audit=[RunbookAuditEntry("params", None, "-", "-", False, "blocked",
                                         "; ".join(errors))])

        result = RunbookResult(runbook=name, status="running", params=coerced)
        run_ctx: dict = {**coerced, **(precondition_ctx or {})}

        graph, handlers = self._compile(rb, coerced, run_ctx, result, approval_fn)
        budget = ExecutionBudget(max_nodes=len(rb.steps) * 2 + 4, max_depth=len(rb.steps) * 2 + 2,
                                 max_fan_out=1, global_timeout_s=global_timeout_s)
        executor = TaskGraphExecutor(handlers, budget, cancel=cancel or CancelToken(),
                                     broadcast_fn=self._broadcast_fn)
        run = await executor.run(graph)
        # Derive runbook status from the graph run + optional-step tolerance.
        blocked = any(a.status == "blocked" for a in result.audit)
        failed_required = any(a.status == "failed" for a in result.audit)
        if run.status == "cancelled":
            result.status = "cancelled"
        elif run.status == "timed_out":
            result.status = "failed"
        elif blocked:
            result.status = "blocked"
        elif failed_required:
            result.status = "partial" if any(a.status == "completed" for a in result.audit) else "failed"
        else:
            result.status = "completed"
        result.elapsed_s = run.elapsed_s
        result.rollback_hints = [a.rollback_hint for a in result.audit
                                 if a.rollback_hint and a.status == "completed"]
        return result

    # ── graph compilation ─────────────────────────────────────────────────────
    def _compile(self, rb: RunbookDefinition, coerced: dict, run_ctx: dict,
                 result: RunbookResult, approval_fn):
        graph = TaskGraph()
        prev: str | None = None
        for step in rb.steps:
            approval_node: str | None = None
            if step.requires_approval:
                approval_node = f"approve_{step.id}"
                graph.add(approval_node, NodeType.HUMAN_APPROVAL,
                          depends_on=[prev] if prev else [],
                          description=f"approve {step.description}",
                          payload={"step": step})
            deps = [approval_node] if approval_node else ([prev] if prev else [])
            ntype = NodeType.REASON if step.kind is StepKind.REASON else NodeType.TOOL
            graph.add(step.id, ntype, depends_on=deps, description=step.description,
                      payload={"step": step}, timeout_s=step.timeout_s,
                      retry_policy=RetryPolicy(max_retries=0))  # self-debug owns retries
            prev = step.id

        async def _tool_handler(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
            step: RunbookStep = node.payload["step"]
            # precondition
            if step.precondition is not None:
                ok, why = step.precondition.predicate(run_ctx)
                if not ok:
                    result.audit.append(_audit(step, "blocked", f"precondition: {why}"))
                    return NodeOutcome(ok=step.optional, error=f"precondition failed: {why}")
            if step.action is None:
                result.audit.append(_audit(step, "completed", "reason-only step"))
                return NodeOutcome(ok=True, output={"note": step.description})
            args = _interp(step.params, run_ctx)
            holder: dict = {}

            async def attempt(a: dict):
                res = await self._exec(step.action, a, f"[runbook:{rb.name}:{step.id}]")
                holder["result"] = res
                ok_ = not (isinstance(res, dict) and "error" in res)
                return ok_, res

            outcome = await self._self_debug.run_with_repair(
                step.action, attempt, args, destructive=step.destructive)
            last = holder.get("result")
            run_ctx[step.id] = last
            result.outputs[step.id] = _summary(last)
            if not outcome.success:
                result.audit.append(_audit(step, "failed",
                                           str(outcome.final_error)[:150]))
                return NodeOutcome(ok=step.optional, output=last,
                                   error=str(outcome.final_error)[:150])
            # postcondition
            if step.postcondition is not None:
                ok, why = step.postcondition.predicate(run_ctx)
                if not ok:
                    result.audit.append(_audit(step, "failed", f"postcondition: {why}"))
                    return NodeOutcome(ok=step.optional, output=last,
                                       error=f"postcondition failed: {why}")
            result.audit.append(_audit(step, "completed", _summary(last)))
            return NodeOutcome(ok=True, output=last)

        async def _reason_handler(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
            step: RunbookStep = node.payload["step"]
            result.audit.append(_audit(step, "completed", "reason-only step"))
            return NodeOutcome(ok=True, output={"note": step.description})

        async def _approval_handler(node: TaskNode, ctx: GraphContext) -> NodeOutcome:
            step: RunbookStep = node.payload["step"]
            if approval_fn is None:
                # Fail-closed: no explicit approver → the guarded step is blocked.
                result.audit.append(_audit(step, "blocked", "explicit approval required"))
                return NodeOutcome(ok=False, blocked=True, error="approval required")
            granted = await approval_fn(step)
            if not granted:
                result.audit.append(_audit(step, "blocked", "approval denied"))
            return NodeOutcome(ok=bool(granted), blocked=not granted,
                               output={"approved": bool(granted)})

        handlers = {NodeType.TOOL: _tool_handler, NodeType.REASON: _reason_handler,
                    NodeType.HUMAN_APPROVAL: _approval_handler}
        return graph, handlers


def _audit(step: RunbookStep, status: str, summary: str) -> RunbookAuditEntry:
    risk, hitl = _classify(step.action)
    return RunbookAuditEntry(
        step_id=step.id, action=step.action, kind=step.kind.value, risk_class=risk,
        requires_hitl=hitl or step.requires_approval, status=status,
        summary=summary[:180], rollback_hint=step.rollback_hint)


def _summary(result) -> str:
    import json
    try:
        return json.dumps(result, default=str)[:200]
    except Exception:  # noqa: BLE001
        return str(result)[:200]


# ══════════════════════════════════════════════════════════════════════════════
#  Built-in runbook classes (guarded; read-only diagnostics + gated actions)
# ══════════════════════════════════════════════════════════════════════════════
def _host_present(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("host") or ctx.get("target")), "no host/target provided")


def _default_runbooks() -> list[RunbookDefinition]:
    host_param = RunbookParameter("host", ParamType.TARGET, description="host/IP under review")
    target_param = RunbookParameter("target", ParamType.TARGET, description="alert target")
    pre_host = RunbookPrecondition("host_present", _host_present, "a host must be provided")

    return [
        RunbookDefinition(
            name="SERVICE_DIAGNOSIS",
            description="Diagnose a degraded/absent service on a host (read-only).",
            parameters=(host_param,),
            steps=(
                RunbookStep("connectivity", "check host reachability",
                            StepKind.DIAGNOSTIC, "check_connectivity",
                            {"host": "{{host}}"}, precondition=pre_host),
                RunbookStep("host_info", "collect host/system status",
                            StepKind.DIAGNOSTIC, "system_info", {}),
            ),
        ),
        RunbookDefinition(
            name="CONTAINER_HEALTH_CHECK",
            description="Check a container/workload's health (read-only).",
            parameters=(host_param,),
            steps=(
                RunbookStep("host_info", "collect host status", StepKind.DIAGNOSTIC,
                            "system_info", {}),
                RunbookStep("processes", "enumerate running processes",
                            StepKind.DIAGNOSTIC, "list_processes", {}),
            ),
        ),
        RunbookDefinition(
            name="HOST_CONNECTIVITY_DIAGNOSIS",
            description="Diagnose host connectivity / sensor reachability (read-only).",
            parameters=(host_param,),
            steps=(
                RunbookStep("connectivity", "check host reachability",
                            StepKind.DIAGNOSTIC, "check_connectivity",
                            {"host": "{{host}}"}, precondition=pre_host),
            ),
        ),
        RunbookDefinition(
            name="AUTH_FAILURE_TRIAGE",
            description="Triage an authentication-failure sequence (read-only).",
            parameters=(host_param,),
            steps=(
                RunbookStep("processes", "enumerate processes for suspicious logons",
                            StepKind.DIAGNOSTIC, "list_processes", {}),
                RunbookStep("host_info", "collect host status", StepKind.DIAGNOSTIC,
                            "system_info", {}),
            ),
        ),
        RunbookDefinition(
            name="IDS_ALERT_INVESTIGATION",
            description="Investigate an IDS/network alert; active scan is gated.",
            parameters=(target_param,),
            steps=(
                RunbookStep("connectivity", "check target reachability",
                            StepKind.DIAGNOSTIC, "check_connectivity",
                            {"host": "{{target}}"}),
                RunbookStep("scan", "active service scan of the alert target",
                            StepKind.ACTION, "network_scan",
                            {"target": "{{target}}", "scan_type": "-sV --top-ports 100"},
                            requires_approval=True,
                            rollback_hint="No host change; a scan is observation only."),
            ),
        ),
        RunbookDefinition(
            name="NEW_SERVICE_EXPOSURE_REVIEW",
            description="Review a newly exposed service; active scan is gated.",
            parameters=(host_param,),
            steps=(
                RunbookStep("connectivity", "check host reachability",
                            StepKind.DIAGNOSTIC, "check_connectivity",
                            {"host": "{{host}}"}, precondition=pre_host),
                RunbookStep("scan", "confirm the exposed service surface",
                            StepKind.ACTION, "network_scan",
                            {"target": "{{host}}", "scan_type": "-sV --top-ports 100"},
                            requires_approval=True,
                            rollback_hint="No host change; a scan is observation only."),
            ),
        ),
        RunbookDefinition(
            name="INCIDENT_EVIDENCE_COLLECTION",
            description="Collect host evidence for an incident case (read-only).",
            parameters=(host_param,),
            steps=(
                RunbookStep("host_info", "collect host status", StepKind.DIAGNOSTIC,
                            "system_info", {}),
                RunbookStep("processes", "capture the process list", StepKind.DIAGNOSTIC,
                            "list_processes", {}),
            ),
        ),
    ]


# Module-level singleton. main.py wires the live ToolExecutor so every world-effect
# runs through the guarded gate; until wired, actions fail closed (no bypass).
engine = RunbookEngine()
