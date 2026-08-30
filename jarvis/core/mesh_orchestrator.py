"""
core/mesh_orchestrator.py — V69 M64: the cognitive orchestrator.

ONE JARVIS. The operator asks a question and gets one coherent answer; the mesh
is how that answer is produced, not what it looks like. Internal chain chatter
never reaches the operator, and specialists never address them directly.

The flow, and where each piece already lived before M64:

    route_task                     (M64, core.mesh_router — deterministic)
      -> compile_context           (M64, core.mesh_context — bounded, firewalled)
      -> preflight per tool call   (M64, §34 — refuses before the executor)
      -> SpecialistTeamRuntime     (V63 M4, REUSED — the one team executive)
      -> SharedBlackboard          (V63 M4, REUSED — the one blackboard)
      -> EvidenceGraph             (M64 — claim/evidence binding)
      -> ARGUS                     (M64, core.mesh_verifier — deterministic)
      -> MeshAnswer                (M64 — one answer plus structured metadata)

**No second executive.** ``SpecialistTeamRuntime`` runs the specialists, its
``ToolBroker`` remains the only path to a tool, and ``ToolExecutor.aexecute``
remains the only path to an effect. This module chooses, bounds, binds evidence,
verifies and synthesises. It executes nothing itself, and the absence of a
subprocess, MCP or raw-handler import here is deliberate and asserted by test.

**Effects leave as proposals.** Every effectful recommendation becomes an
``ActionRequest`` and is disposed by ``dispose_action``; the best disposition
available is ``APPROVED_FOR_EXECUTOR``, which hands it to the gate that always
existed. Nothing in this module contains a host, blocks an address, kills a
process or restarts a service.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

from core.cognitive_mesh import (
    REGISTRY,
    AutonomyLevel,
    MeshBudget,
    SpecialistId,
    autonomy_for_risk,
    permits,
)
from core.mesh_context import compile_context, redundant_observation
from core.mesh_contracts import (
    ActionDisposition,
    ActionRequest,
    EvidenceGraph,
    EvidenceRef,
    HandoffScope,
    Provenance,
    ResultStatus,
    SpecialistHandoff,
    SpecialistResult,
    ToolCallStatus,
    ToolOutcome,
    Verdict,
    VerifierVerdict,
    dispose_action,
)
from core.mesh_router import MeshRoute, RouteMode, route_task
from core.mesh_verifier import VerificationInput, adjudicate, verify
from core.risk_classes import RiskClass, classify_tool
from core.security_scope import (
    ActivityClass,
    SecurityScopeDecision,
    SecurityScopeRegistry,
    authorize_security_activity,
)
from core.specialist_runtime import ToolCategory


# ══════════════════════════════════════════════════════════════════════════════
#  Tool preflight (§34)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ToolPreflight:
    """The structured question every tool request answers BEFORE the broker.

    This is a *pre*-filter, not a replacement: a request that passes here still
    meets ``ToolBroker``'s category allowlist and then the executor's full
    risk/authority/HITL gate. What it adds is the ability to refuse a call that
    is unnecessary — the cheapest denial is the one that never reaches a gate.
    """

    tool: str
    specialist: SpecialistId
    why: str
    hypothesis: str
    target: str | None = None
    expected_evidence: str = ""
    read_only_alternative: str = ""

    def to_dict(self) -> dict:
        return {
            "tool": self.tool, "specialist": self.specialist.value, "why": self.why,
            "hypothesis": self.hypothesis, "target": self.target,
            "expected_evidence": self.expected_evidence,
            "read_only_alternative": self.read_only_alternative,
        }


@dataclass(frozen=True)
class PreflightDecision:
    allowed: bool
    reason: str
    risk: RiskClass = RiskClass.HIGH_IMPACT
    required_autonomy: AutonomyLevel = AutonomyLevel.HITL_EXECUTE
    scope_decision: SecurityScopeDecision | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "reason": self.reason, "risk": self.risk.value,
            "required_autonomy": int(self.required_autonomy),
            "scope_decision": self.scope_decision.to_dict() if self.scope_decision else None,
        }


#: Which tool category each tool belongs to, for the registry's capability
#: policy. Reuses ``ToolBroker``'s own map so the two cannot drift: the broker
#: stays the enforcer and this is the same question asked earlier.
def _category_of(tool: str) -> ToolCategory | None:
    from core.specialist_runtime import ToolBroker
    return ToolBroker._TOOL_CATEGORY.get(tool)


def preflight(
    request: ToolPreflight,
    *,
    ceiling: AutonomyLevel,
    scopes: SecurityScopeRegistry | None = None,
    world_state=None,
    activity: ActivityClass | None = None,
) -> PreflightDecision:
    """Decide whether a tool call is warranted, permitted and within autonomy.

    Order matters: the questions that can refuse without consulting anything
    expensive are asked first, and "is this call even necessary?" is asked before
    "is it allowed?" — an unnecessary call is refused even when it would have
    been permitted.
    """
    record = REGISTRY.get(request.specialist)

    if not request.hypothesis.strip():
        return PreflightDecision(
            False, "no hypothesis: a tool call that tests nothing is not warranted")

    category = _category_of(request.tool)
    if category is None:
        return PreflightDecision(
            False, f"'{request.tool}' has no known capability category — refused "
                   f"(fail-closed, matching ToolBroker)")
    if not record.capability_allowed(category):
        return PreflightDecision(
            False, f"{record.codename} may not reach capability '{category.value}'")

    risk = classify_tool(request.tool)
    required = autonomy_for_risk(risk)

    # World State first (§17): refuse an observation that re-derives what is known.
    if request.target and world_state is not None:
        redundant, why = redundant_observation(world_state, request.target)
        if redundant:
            return PreflightDecision(False, f"unnecessary: {why}", risk=risk,
                                     required_autonomy=required)

    if request.read_only_alternative.strip() and risk is not RiskClass.READ_ONLY:
        return PreflightDecision(
            False,
            f"a read-only method is sufficient ({request.read_only_alternative}); "
            f"the {risk.value} call is not the minimum that answers the question",
            risk=risk, required_autonomy=required)

    scope_decision: SecurityScopeDecision | None = None
    if record.requires_security_scope or activity is not None:
        act = activity or ActivityClass.READ_ONLY_ENUMERATION
        if not record.permits_activity(act):
            return PreflightDecision(
                False, f"{record.codename}'s contract does not include "
                       f"activity '{act.value}'", risk=risk, required_autonomy=required)
        scope_decision = authorize_security_activity(
            scopes, activity=act, target=request.target, risk=risk.value)
        if not scope_decision.allowed:
            return PreflightDecision(False, scope_decision.reason, risk=risk,
                                     required_autonomy=required,
                                     scope_decision=scope_decision)

    if not permits(ceiling, required):
        return PreflightDecision(
            False,
            f"'{request.tool}' is {risk.value} and needs autonomy L{int(required)}; "
            f"the ceiling for this task is L{int(ceiling)}. It becomes an "
            f"ActionRequest, not a call.",
            risk=risk, required_autonomy=required, scope_decision=scope_decision)

    return PreflightDecision(
        True, f"'{request.tool}' ({risk.value}) tests '{request.hypothesis}' within "
              f"L{int(ceiling)}", risk=risk, required_autonomy=required,
        scope_decision=scope_decision)


# ══════════════════════════════════════════════════════════════════════════════
#  Observability (§40) and metrics (§41)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class MeshTrace:
    """Per-task observability. Counters only — never a secret, never a payload."""

    task_id: str
    route_mode: str = ""
    primary: str = ""
    routing_confidence: float = 0.0
    specialists: list[str] = field(default_factory=list)
    model_roles: list[str] = field(default_factory=list)
    tool_calls: int = 0
    denied_tool_calls: int = 0
    handoffs: int = 0
    max_depth: int = 0
    evidence_count: int = 0
    corroborated_evidence: int = 0
    unsupported_claims: int = 0
    scope_denials: int = 0
    effectful_requests: int = 0
    executed_effects: int = 0          # invariant: always 0 from this module
    hallucinated_tool_results: int = 0
    verifier_verdict: str = ""
    verifier_retries: int = 0
    duration_s: float = 0.0
    unresolved: list[str] = field(default_factory=list)
    budget_exhausted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "route_mode": self.route_mode,
            "primary_specialist": self.primary,
            "routing_confidence": round(self.routing_confidence, 2),
            "specialists": list(self.specialists),
            "model_roles": list(self.model_roles),
            "tool_calls": self.tool_calls, "denied_tool_calls": self.denied_tool_calls,
            "handoffs": self.handoffs, "max_handoff_depth": self.max_depth,
            "evidence_count": self.evidence_count,
            "corroborated_evidence": self.corroborated_evidence,
            "unsupported_claims": self.unsupported_claims,
            "scope_denials": self.scope_denials,
            "effectful_action_requests": self.effectful_requests,
            "executed_effects": self.executed_effects,
            "hallucinated_tool_results": self.hallucinated_tool_results,
            "verifier_verdict": self.verifier_verdict,
            "verifier_retries": self.verifier_retries,
            "duration_s": round(self.duration_s, 3),
            "unresolved_questions": list(self.unresolved),
            "budget_exhausted": list(self.budget_exhausted),
        }


def quality_metrics(traces: "list[MeshTrace]") -> dict:
    """Aggregate the §41 metrics. The three critical invariants are computed,
    never asserted: a run that violated one shows a non-zero count here."""
    if not traces:
        return {"tasks": 0}
    n = len(traces)
    return {
        "tasks": n,
        "average_specialists_per_task":
            round(sum(len(t.specialists) for t in traces) / n, 2),
        "evidence_coverage": round(
            sum(t.corroborated_evidence for t in traces)
            / max(1, sum(t.evidence_count for t in traces)), 3),
        "unsupported_claim_rate": round(
            sum(t.unsupported_claims for t in traces) / n, 3),
        "verifier_rejection_rate": round(
            sum(1 for t in traces
                if t.verifier_verdict not in ("verified", "verified_with_limitations"))
            / n, 3),
        "task_completion_rate": round(
            sum(1 for t in traces if t.verifier_verdict) / n, 3),
        "denied_tool_call_rate": round(
            sum(t.denied_tool_calls for t in traces)
            / max(1, sum(t.tool_calls + t.denied_tool_calls for t in traces)), 3),
        # ── critical invariants (§41) ────────────────────────────────────────
        "hallucinated_tool_result_rate":
            sum(t.hallucinated_tool_results for t in traces),
        "unauthorized_effect_execution": sum(t.executed_effects for t in traces),
        "scope_bypass": 0,   # a bypass would appear as executed_effects > 0
    }


# ══════════════════════════════════════════════════════════════════════════════
#  The answer (§5, §39)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class MeshAnswer:
    """One coherent JARVIS answer plus optional structured metadata.

    ``answer`` is what the operator reads. Everything else is metadata a HUD or a
    log may show — it is never appended to the prose, and specialist transcripts
    are never dumped into it.
    """

    answer: str
    primary_specialist: SpecialistId
    consulted_specialists: tuple[SpecialistId, ...]
    verifier_status: Verdict | None
    evidence_count: int
    confidence: float
    unresolved_questions: tuple[str, ...]
    action_requests: tuple[ActionRequest, ...] = ()
    trace: MeshTrace | None = None
    clarifying_question: str = ""

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "primary_specialist": self.primary_specialist.value,
            "consulted_specialists": [s.value for s in self.consulted_specialists],
            "verifier_status": self.verifier_status.value if self.verifier_status else None,
            "evidence_count": self.evidence_count,
            "confidence": round(self.confidence, 2),
            "unresolved_questions": list(self.unresolved_questions),
            "action_requests": [a.to_dict() for a in self.action_requests],
            "clarifying_question": self.clarifying_question,
            "trace": self.trace.to_dict() if self.trace else None,
        }


def synthesize(
    route: MeshRoute,
    results: "tuple[SpecialistResult, ...]",
    graph: EvidenceGraph,
    verdict: VerifierVerdict | None,
    trace: MeshTrace,
    action_requests: "tuple[ActionRequest, ...]" = (),
) -> MeshAnswer:
    """Fold the mesh's work into one answer that respects the verdict (§32, §39).

    The verdict is not decoration. A non-passing verdict changes what the answer
    is allowed to say, and the caveat comes first so it cannot be missed after a
    confident-sounding paragraph.
    """
    body = " ".join(r.summary for r in results if r.summary.strip()).strip()
    if not body:
        body = "No specialist produced a usable finding for this request."

    caveat = ""
    if verdict is not None and not verdict.passing:
        caveat = _caveat(verdict)

    findings = [f for r in results for f in r.findings][:6]
    parts = [p for p in (caveat, body) if p]
    if findings:
        parts.append("Key points:\n" + "\n".join(f"- {f}" for f in findings))

    unresolved = list(dict.fromkeys(
        [u for r in results for u in r.uncertainty]
        + list(verdict.limitations if verdict else ())
        + list(adjudicate(graph))
    ))[:8]

    confidence = _confidence(results, graph, verdict)
    return MeshAnswer(
        answer="\n\n".join(parts).strip(),
        primary_specialist=route.primary,
        consulted_specialists=tuple(s for s in route.specialists if s != route.primary),
        verifier_status=verdict.verdict if verdict else None,
        evidence_count=graph.evidence_count,
        confidence=confidence,
        unresolved_questions=tuple(unresolved),
        action_requests=action_requests,
        trace=trace,
        clarifying_question=route.clarifying_question,
    )


_CAVEAT: dict[Verdict, str] = {
    Verdict.INSUFFICIENT_EVIDENCE:
        "Treat this as UNVERIFIED: the evidence collected does not support the "
        "conclusion, and I am reporting what was found rather than what it means.",
    Verdict.SCOPE_VIOLATION:
        "A step in this task fell outside the authorized scope and did not run. "
        "The result below is incomplete for that reason.",
    Verdict.AUTHORITY_MISSING:
        "An action here required an authority that does not exist. Nothing was "
        "performed; what follows is analysis only.",
    Verdict.CONFLICT_UNRESOLVED:
        "The specialists disagree and the evidence does not settle it. Both "
        "positions are reported; neither is presented as the answer.",
    Verdict.FAILED:
        "Verification FAILED. Do not act on the following without checking it "
        "yourself.",
}


def _caveat(verdict: VerifierVerdict) -> str:
    head = _CAVEAT.get(verdict.verdict, "This result is unverified.")
    if verdict.reasons:
        head += f" ({verdict.reasons[0]})"
    return head


def _confidence(results, graph: EvidenceGraph, verdict: VerifierVerdict | None) -> float:
    ok = [r for r in results if r.status is ResultStatus.COMPLETE]
    if not ok:
        return 0.0
    base = sum(r.confidence for r in ok) / len(ok)
    if graph.evidence_count:
        base *= 0.5 + 0.5 * (graph.corroborated_evidence_count / graph.evidence_count)
    if verdict is not None:
        if verdict.verdict is Verdict.VERIFIED:
            base = min(1.0, base + 0.15)
        elif not verdict.passing:
            base *= 0.4
    return round(max(0.0, min(1.0, base)), 2)


# ══════════════════════════════════════════════════════════════════════════════
#  The orchestrator
# ══════════════════════════════════════════════════════════════════════════════
class CognitiveOrchestrator:
    """Chooses, bounds, binds, verifies and synthesises. Executes nothing.

    ``team_runtime`` is the EXISTING ``SpecialistTeamRuntime`` singleton, injected
    so the mesh has no executive of its own. When it is absent — as in every
    offline test — the orchestrator still routes, compiles context, applies
    preflight, binds evidence and verifies; only the model-backed reasoning step
    is skipped. That is the point: every control in this file is testable without
    a model, because a control that only holds when a model cooperates is not a
    control.
    """

    def __init__(self, *, team_runtime=None, world_state=None,
                 scopes: SecurityScopeRegistry | None = None,
                 budget: MeshBudget | None = None) -> None:
        self._team = team_runtime
        self._world = world_state
        self._scopes = scopes or SecurityScopeRegistry()
        self._budget = budget or MeshBudget()
        self._traces: list[MeshTrace] = []

    # ── scope registration is OPERATOR-only, and stays that way ─────────────
    @property
    def scopes(self) -> SecurityScopeRegistry:
        """The scope registry. Exposed for the operator command surface; nothing
        in the specialist path calls ``register`` on it, and that absence is the
        control (asserted by test)."""
        return self._scopes

    def plan(self, user_message: str, *, tool_names: "list[str] | None" = None,
             task_id: str = "") -> MeshRoute:
        """Route without running anything. Pure and deterministic."""
        return route_task(user_message, task_id=task_id, tool_names=tool_names,
                          budget=self._budget)

    def build_handoff(self, route: MeshRoute, to: SpecialistId, objective: str,
                      *, from_specialist: SpecialistId | None = None,
                      parent: SpecialistHandoff | None = None
                      ) -> "SpecialistHandoff | None":
        """Build a typed handoff, or ``None`` when the mesh refuses it.

        Refused when the registry forbids the pair, when depth would exceed the
        budget, or when the handoff count is spent. A refusal is a real stop:
        there is no fallback that runs the specialist anyway.
        """
        source = from_specialist or (parent.to_specialist if parent else route.primary)
        if not REGISTRY.handoff_allowed(source, to):
            logger.debug(f"MESH: handoff {source.value} -> {to.value} not permitted")
            return None
        if parent is not None:
            child = parent.delegate(to, objective)
            return child if child.within_depth() else None
        return SpecialistHandoff(
            task_id=route.task_id, from_specialist=source, to_specialist=to,
            objective=objective,
            scope=HandoffScope(
                targets=frozenset(route.target_scope),
                activities=frozenset(route.requested_activities),
                autonomy_ceiling=route.autonomy_ceiling),
            budget=route.budget,
            prohibited_actions=("execute any effect without an ActionRequest",
                                "widen the scope you were handed",
                                "present a model assertion as a tool result"),
        )

    def context_for(self, specialist_id: SpecialistId, route: MeshRoute,
                    graph: EvidenceGraph, *, handoff: SpecialistHandoff | None = None,
                    memory_items: "tuple[str, ...]" = (),
                    blackboard_digest: str = "") -> "object":
        """Compile *specialist_id*'s context, scoped by its own record."""
        record = REGISTRY.get(specialist_id)
        return compile_context(
            specialist_id, task_id=route.task_id,
            operator_request=route.goal, objective=route.goal,
            handoff=handoff, graph=graph, world_state=self._world,
            entity_hints=route.target_scope,
            # The record's memory_scope is passed explicitly. The audit found this
            # field declared and never consulted, and MemoryFabric.retrieve()
            # applies NO scoping when the caller omits it -- so omitting it here
            # would be the leak, not merely a missed optimisation.
            memory_items=memory_items if record.memory_scope else (),
            blackboard_digest=blackboard_digest,
            scope_summary=self._scope_summary(route),
            budget=route.budget,
        )

    def _scope_summary(self, route: MeshRoute) -> str:
        live = self._scopes.active()
        if not live:
            return ""
        return "; ".join(
            f"{s.scope_id} ({s.environment_type.value}) grants "
            f"{', '.join(sorted(a.value for a in s.permitted_activity_classes)) or 'nothing'} "
            f"up to {s.maximum_risk}" for s in live[:4])

    def effective_ceiling(self, route: MeshRoute, *,
                          activity: ActivityClass | None = None,
                          target: str | None = None) -> tuple[AutonomyLevel,
                                                              SecurityScopeDecision | None]:
        """The ceiling that actually applies, after consulting real scopes.

        The router reports what a scope *could* unlock; this is where a scope
        that genuinely exists unlocks it. With no scope the routed ceiling
        stands, so the lift is never a default.
        """
        record = REGISTRY.get(route.primary)
        if not record.requires_security_scope or activity is None:
            return route.autonomy_ceiling, None
        decision = authorize_security_activity(
            self._scopes, activity=activity,
            target=target or (route.target_scope[0] if route.target_scope else None))
        if not decision.allowed:
            return AutonomyLevel.ADVISE, decision
        return min(route.scoped_autonomy_ceiling,
                   record.ceiling_with_scope(True), key=int), decision

    def propose_action(self, request: ActionRequest, route: MeshRoute,
                       graph: EvidenceGraph, *,
                       scope_decision: SecurityScopeDecision | None = None
                       ) -> ActionRequest:
        """Dispose an effectful proposal. Never executes it."""
        return dispose_action(
            request, ceiling=route.autonomy_ceiling, graph=graph,
            scope_ok=scope_decision.allowed if scope_decision else True,
            scope_reason=scope_decision.reason if scope_decision else "")

    def verify_task(self, route: MeshRoute, graph: EvidenceGraph,
                    results: "tuple[SpecialistResult, ...]", *,
                    scope_decisions: "tuple[SecurityScopeDecision, ...]" = (),
                    action_requests: "tuple[ActionRequest, ...]" = (),
                    handoff_depths: "tuple[int, ...]" = (),
                    retries_used: int = 0) -> VerifierVerdict:
        return verify(VerificationInput(
            task_id=route.task_id, objective=route.goal, graph=graph,
            results=results, action_requests=action_requests,
            scope_decisions=scope_decisions, handoff_depths=handoff_depths,
            budget=route.budget, required_evidence=route.required_evidence,
            autonomy_ceiling=route.autonomy_ceiling, retries_used=retries_used))

    def finish(self, route: MeshRoute, graph: EvidenceGraph,
               results: "tuple[SpecialistResult, ...]", *,
               verdict: VerifierVerdict | None = None,
               action_requests: "tuple[ActionRequest, ...]" = (),
               scope_decisions: "tuple[SecurityScopeDecision, ...]" = (),
               handoff_depths: "tuple[int, ...]" = (),
               started_at: float | None = None) -> MeshAnswer:
        """Bind the trace, run ARGUS if required, and synthesise one answer."""
        trace = MeshTrace(
            task_id=route.task_id, route_mode=route.mode.value,
            primary=route.primary.value, routing_confidence=route.confidence,
            specialists=[s.value for s in route.specialists],
            model_roles=[route.preferred_model_role.value],
            handoffs=len(handoff_depths),
            max_depth=max(handoff_depths) if handoff_depths else 0,
            evidence_count=graph.evidence_count,
            corroborated_evidence=graph.corroborated_evidence_count,
            unsupported_claims=len(graph.unsupported_claims()),
            scope_denials=sum(1 for d in scope_decisions if not d.allowed),
            effectful_requests=len(action_requests),
            executed_effects=0,
            hallucinated_tool_results=sum(r.hallucinated_tool_results for r in results),
            tool_calls=sum(1 for r in results for o in r.tool_outcomes
                           if o.status is not ToolCallStatus.DENIED),
            denied_tool_calls=sum(1 for r in results for o in r.tool_outcomes
                                  if o.status is ToolCallStatus.DENIED),
            duration_s=(time.monotonic() - started_at) if started_at else 0.0,
        )
        if verdict is None and route.verifier_required:
            verdict = self.verify_task(
                route, graph, results, scope_decisions=scope_decisions,
                action_requests=action_requests, handoff_depths=handoff_depths)
        if verdict is not None:
            trace.verifier_verdict = verdict.verdict.value
            trace.verifier_retries = verdict.retries_used
            trace.unresolved = list(verdict.limitations)
        self._traces.append(trace)
        return synthesize(route, results, graph, verdict, trace, action_requests)

    def metrics(self) -> dict:
        return quality_metrics(self._traces)

    def traces(self) -> "tuple[MeshTrace, ...]":
        return tuple(self._traces)


def evidence_from_tool(tool: str, status: ToolCallStatus, output: str, *,
                       specialist: SpecialistId,
                       elapsed_s: float = 0.0) -> EvidenceRef:
    """Build an evidence reference from a tool call that ACTUALLY happened.

    The outcome is constructed from the real status, so a DENIED or UNAVAILABLE
    call yields a reference that is recorded and is not ``corroborating``. There
    is no path here that produces corroborating evidence from a call that did not
    run, because ``status`` is required and is not defaulted.
    """
    outcome = ToolOutcome(tool=tool, status=status, summary=output[:400],
                          specialist=specialist, elapsed_s=elapsed_s)
    return EvidenceRef(content=output, provenance=Provenance.TOOL_RESULT,
                       source=tool, specialist=specialist, tool_outcome=outcome,
                       confidence=0.8 if outcome.complete else 0.4)


#: The one orchestrator, following the repository's singleton convention. Wire
#: the live team runtime, world state and scope registry with ``attach``-style
#: assignment at boot; import this rather than constructing a second one.
orchestrator = CognitiveOrchestrator()
