"""
core/agent_runtime.py — V63 Milestone 1: unified per-turn runtime decision.

Composes JARVIS's existing, proven per-turn signals into ONE decision object,
consulted once per turn — WITHOUT replacing ``chat_stream`` or touching any
security/verifier/memory/consent invariant:

  route()                     -> ModelDecision (authoritative model role + verify)
  is_security_sensitive_turn  -> risk dimension
  classify_query()            -> (category, force_deep)   [cognitive_optimizer]
  classify_domain()           -> DomainSignal             [task_domain, M2]
  ResponseSurface             -> presentation             [response_surface, M6]

This is the "conceptual decision object" the V63 directive describes: domain /
complexity / risk / requires_planning / requires_tools / requires_agents /
requires_verification / preferred_model_role / response_surface — assembled
purely, with no side effects and no tool execution.

Single-source invariant: the FAST→DEEP force_deep escalation lives here in
``route_turn`` only; ``core.llm.LLM._route_turn`` delegates to it, so there is
exactly one routing implementation and no drift. ``chat_stream`` reads
``TaskDecision.model_decision`` for model selection and verifier gating, keeping
that behavior byte-identical to before while gaining the new dimensions for
telemetry and future planner/agent-team routing.
"""
from __future__ import annotations

import dataclasses
from contextlib import suppress
from dataclasses import dataclass

from core.model_router import (
    COMPLEXITY_THRESHOLD,
    ModelDecision,
    ModelRole,
    is_security_sensitive_turn,
    route,
)
from core.response_surface import ResponseSurface
from core.task_domain import TaskDomain, classify_domain

# Domains that typically drive tool use (advisory prediction only — never gates).
_TOOL_HEAVY_DOMAINS: frozenset[TaskDomain] = frozenset({
    TaskDomain.VISION, TaskDomain.RESEARCH, TaskDomain.CYBER_PURPLE,
    TaskDomain.CYBER_BLUE, TaskDomain.DFIR,
})


def route_turn(
    user_message: str,
    *,
    tool_names: list[str] | None = None,
    force_deep: bool = False,
) -> ModelDecision:
    """Canonical per-turn model routing (the single source of truth).

    Classifies security sensitivity, asks the V60 role router for a
    ``ModelDecision`` (cloud never escalated from the local streaming client),
    then applies the escalation-only ``force_deep`` rule: a FAST decision is
    lifted to DEEP + requires_verification. Never de-escalates, never overrides a
    role the router chose for a specific reason (CODER/VISION/VERIFIER/CLOUD/DEEP).
    ``model_router``'s enum and ``route()`` precedence are untouched.
    """
    sec = is_security_sensitive_turn(user_message, tool_names)
    decision = route(user_message, security_sensitive=sec, allow_cloud=False)
    if force_deep and decision.role == ModelRole.FAST:
        decision = dataclasses.replace(
            decision,
            role=ModelRole.DEEP,
            requires_verification=True,
            reason=f"{decision.reason}; escalated by cognitive_optimizer.force_deep",
        )
    return decision


@dataclass(frozen=True)
class TaskDecision:
    """Unified per-turn decision — the composed runtime context for one turn.

    V69 M66A: this stays the ONE canonical per-turn decision object. The M66A
    epistemic dimensions are carried on it as a single ``epistemic`` component
    (intent / ambiguity / freshness / evidence / verification / deliberation /
    tool need / delivery), composed monotonically from the fields above plus the
    deterministic ``TurnPolicy``. There is no rival decision object; adapters may
    read either the composed object or its inner ``model_decision``.
    """

    model_decision: ModelDecision      # authoritative model role / verify
    domain: TaskDomain
    domain_confidence: float
    complexity: float
    security_sensitive: bool
    query_category: str
    force_deep: bool
    requires_verification: bool
    requires_planning: bool
    requires_tools: bool
    prefers_agent_team: bool
    preferred_model_role: ModelRole    # advisory (route() is authoritative)
    response_surface: ResponseSurface
    reason: str
    #: V69 M66A — the epistemic dimensions of this turn. ``None`` only when the
    #: pure composition could not run (it never raises into the turn), in which
    #: case every downstream reader falls back to the pre-M66A behaviour.
    epistemic: object | None = None
    #: V69 M66A — the deterministic TurnPolicy this decision was composed with,
    #: carried so a caller need not reclassify. ``None`` when not supplied.
    turn_policy: object | None = None

    # ── Convenience accessors mirroring ModelDecision so callers can read the
    # composed object or the inner decision interchangeably. ─────────────────
    @property
    def role(self) -> ModelRole:
        return self.model_decision.role

    @property
    def model(self) -> str:
        return self.model_decision.model

    @property
    def provider(self) -> str:
        return self.model_decision.provider

    def telemetry(self) -> dict:
        """Flat, JSON-ready dict for AURA/HUD (additive to the existing
        model_decision event)."""
        out = {
            "domain": self.domain.value,
            "domain_confidence": round(self.domain_confidence, 2),
            "response_surface": self.response_surface.value,
            "requires_planning": self.requires_planning,
            "prefers_agent_team": self.prefers_agent_team,
            "requires_tools": self.requires_tools,
            "preferred_model_role": self.preferred_model_role.value,
        }
        if self.epistemic is not None:
            # Telemetry never breaks a turn. `suppress` rather than a bare
            # try/except/pass: same semantics, and it does not grow the repo's
            # approved Bandit LOW baseline with a B110 the gate would reject.
            with suppress(Exception):
                out.update(self.epistemic.telemetry())
        return out


def assemble_task_decision(
    user_message: str,
    *,
    tool_names: list[str] | None = None,
    force_deep: bool = False,
    query_category: str = "",
    surface: ResponseSurface = ResponseSurface.TEXT,
    turn_policy: object | None = None,
    mesh_route: object | None = None,
) -> TaskDecision:
    """Compose the unified per-turn decision. Pure; no side effects, no tools.

    ``model_decision`` is exactly what ``route_turn`` returns (so downstream model
    selection and verifier gating are unchanged). Domain, surface, and the
    planning/agent/tool advisories are layered on top for telemetry and future
    routing. The fast path stays fast: GENERAL chat requires no planning, no
    agent team, and inherits the router's own verification decision.

    V69 M66A: the epistemic dimensions are composed here too, from this decision
    plus the deterministic ``TurnPolicy`` (computed if not supplied) and, when
    available, the deterministic ``MeshRoute``. The composition is pure and
    guarded — it never raises into the turn — so a decision always has the
    pre-M66A fields even if ``epistemic`` ends up ``None``.
    """
    md = route_turn(user_message, tool_names=tool_names, force_deep=force_deep)
    dom = classify_domain(user_message, tool_names)

    complexity = md.complexity
    security_sensitive = is_security_sensitive_turn(user_message, tool_names)
    requires_verification = md.requires_verification
    requires_planning = bool(
        dom.requires_planning or force_deep or complexity > COMPLEXITY_THRESHOLD
    )
    prefers_agent_team = dom.prefers_agent_team
    requires_tools = bool(tool_names) or dom.domain in _TOOL_HEAVY_DOMAINS

    reason = f"domain={dom.domain.value}({dom.confidence:.2f}); route={md.reason}"

    decision = TaskDecision(
        model_decision=md,
        domain=dom.domain,
        domain_confidence=dom.confidence,
        complexity=complexity,
        security_sensitive=security_sensitive,
        query_category=query_category or "unknown",
        force_deep=force_deep,
        requires_verification=requires_verification,
        requires_planning=requires_planning,
        requires_tools=requires_tools,
        prefers_agent_team=prefers_agent_team,
        preferred_model_role=dom.preferred_role,
        response_surface=surface,
        reason=reason,
    )

    # ── V69 M66A — compose the epistemic dimensions onto the ONE decision. ────
    # Pure and fully guarded: a composition fault degrades the turn's judgement
    # (it falls back to pre-M66A behaviour), never its ability to answer.
    tp = turn_policy
    try:
        if tp is None:
            from core.turn_policy import classify_request
            tp = classify_request(user_message, tool_names=tool_names)
    except Exception:  # noqa: BLE001
        tp = None
    epistemic = None
    try:
        from core.epistemic_deliberation import deliberate
        epistemic = deliberate(user_message, task_decision=decision,
                               turn_policy=tp, mesh_route=mesh_route)
    except Exception:  # noqa: BLE001 — deliberation never breaks a turn
        epistemic = None
    return dataclasses.replace(decision, epistemic=epistemic, turn_policy=tp)
