"""
core/security_effects.py — V69 M64.1: the ONE gate for autonomous security effects.

M64 built the offensive half of this discipline: ``core.security_scope`` decides
whether one :class:`~core.security_scope.ActivityClass` is authorized against one
target, and refuses by default. It had no production consumer, and it has no
vocabulary for the *defensive* half — blocking an address, isolating a host,
killing a process, planting a credential are not members of ``ActivityClass`` and
deliberately never will be.

This module supplies that half, on exactly the same machinery, and is the single
place any automated security effect in JARVIS may be authorized.

WHAT IT REPLACES
================
Before M64.1, six independent code paths reached a real world effect from a
detection signal with no operator in the loop. The worst — recorded as D-M64-1 —
constructed its own RBAC clearance immediately before calling the guarded
function, so the guard resolved the caller's self-assertion and passed::

    _rbac_mgr.set_current_actor(ActorContext("jarvis-system", L3_Hunter, ...))
    await network_quarantine.quarantine(ip, ...)          # netsh block

That is not an authorization check. It is a caller writing its own permit. The
rule this module exists to enforce is the one that failure violates:

    **A SEVERITY SCORE IS NOT AN AUTHORITY.**

Severity, threat score, alert count, model confidence, a blackboard claim, a
memory entry, a World State fact and a verifier verdict all raise *urgency*.
None of them creates *permission*. Permission comes from one place: an operator
who registered a :class:`ContainmentAuthorization` — explicit, target-scoped,
action-class-specific, time-bound, auditable and revocable — before the incident.

THE CONTRACT
============
Every automated effect goes::

    detection -> evidence -> ActionRequest -> capability -> scope -> authority
              -> risk -> HITL -> ToolExecutor -> world effect

:func:`propose_effect` builds the typed :class:`~core.mesh_contracts.ActionRequest`.
:func:`authorize_effect` decides it, fail-closed at every branch.
:func:`execute_effect` is the *only* function here that can reach a world effect,
and it refuses anything the gate did not approve, then hands the call to the one
:class:`~tools.executor.ToolExecutor` so the existing capability, risk, LAB_ONLY
and NATO HITL gates all still run underneath it.

Nothing in this module executes anything itself. There is no ``subprocess``, no
``os.system``, no socket and no firewall command anywhere in it.

REUSED, NOT REBUILT
===================
``ScopePolicy`` (target membership *and* expiry — one matcher, one clock),
``AuthorityState``, ``RiskClass``/``requires_hitl``, ``ActionRequest`` /
``ActionDisposition`` / ``dispose_action``, ``EvidenceGraph``,
``authorize_security_activity`` and ``SecurityScopeRegistry``. This module adds a
vocabulary and a decision order; it adds no second clock, no second matcher and
no second executor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.authority import AuthorityMode, AuthorityState, ScopePolicy
from core.mesh_contracts import (
    ActionDisposition,
    ActionRequest,
    EvidenceGraph,
    dispose_action,
)
from core.cognitive_mesh import AutonomyLevel, SpecialistId
from core.risk_classes import RiskClass, requires_hitl
from core.security_scope import (
    ActivityClass,
    SecurityScopeDecision,
    SecurityScopeRegistry,
    authorize_security_activity,
    risk_rank,
)

logger = logging.getLogger("jarvis.security_effects")

#: Bounds. A registry that can grow without limit is a registry nobody audits.
MAX_AUTHORIZATIONS = 16
MAX_REASON_CHARS = 400


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
#  The vocabulary — what an automated defence may be permitted to do
# ══════════════════════════════════════════════════════════════════════════════
class DefensiveActionClass(str, Enum):
    """What an automated response is actually about to do to the world.

    Deliberately finer-grained than "contain": an operator who pre-authorized
    blocking a hostile address has NOT thereby authorized killing processes on
    their own host, and an operator who authorized isolating a lab VM has not
    authorized reconfiguring a switch.
    """

    #: Add a host-firewall rule blocking an address. Reversible by rule delete.
    FIREWALL_BLOCK_ADDRESS = "firewall_block_address"
    #: Add a host-firewall rule blocking a local port. Reversible by rule delete.
    FIREWALL_BLOCK_PORT = "firewall_block_port"
    #: Isolate a host at the network fabric (NAC, switch ACL, MAC blackhole).
    NETWORK_ISOLATE_HOST = "network_isolate_host"
    #: Terminate a process as an incident response. NOT reversible.
    PROCESS_TERMINATE = "process_terminate"
    #: Write or alter a credential store entry (decoys included).
    CREDENTIAL_MUTATE = "credential_mutate"
    #: Change a service's run state or start type as a hardening action.
    SERVICE_STATE_CHANGE = "service_state_change"


#: The risk each class carries, used against a scope's ``maximum_risk`` ceiling
#: and to decide whether the executor's HITL gate must fire.
ACTION_RISK: dict[DefensiveActionClass, RiskClass] = {
    DefensiveActionClass.FIREWALL_BLOCK_ADDRESS: RiskClass.REVERSIBLE,
    DefensiveActionClass.FIREWALL_BLOCK_PORT: RiskClass.REVERSIBLE,
    DefensiveActionClass.NETWORK_ISOLATE_HOST: RiskClass.HIGH_IMPACT,
    DefensiveActionClass.PROCESS_TERMINATE: RiskClass.HIGH_IMPACT,
    DefensiveActionClass.CREDENTIAL_MUTATE: RiskClass.HIGH_IMPACT,
    DefensiveActionClass.SERVICE_STATE_CHANGE: RiskClass.HIGH_IMPACT,
}

#: Whether the effect can be undone by a documented inverse operation. This
#: drives ``ActionRequest.reversible``; it never relaxes a gate.
ACTION_REVERSIBLE: dict[DefensiveActionClass, bool] = {
    DefensiveActionClass.FIREWALL_BLOCK_ADDRESS: True,
    DefensiveActionClass.FIREWALL_BLOCK_PORT: True,
    DefensiveActionClass.NETWORK_ISOLATE_HOST: True,
    DefensiveActionClass.PROCESS_TERMINATE: False,
    DefensiveActionClass.CREDENTIAL_MUTATE: True,
    DefensiveActionClass.SERVICE_STATE_CHANGE: True,
}

#: The capability an approved request must name. Mirrors the executor's own
#: vocabulary so a request is legible to the gate that finally runs it.
ACTION_CAPABILITY: dict[DefensiveActionClass, str] = {
    DefensiveActionClass.FIREWALL_BLOCK_ADDRESS: "network_quarantine",
    DefensiveActionClass.FIREWALL_BLOCK_PORT: "host_firewall_rule",
    DefensiveActionClass.NETWORK_ISOLATE_HOST: "network_isolate",
    DefensiveActionClass.PROCESS_TERMINATE: "kill_process",
    DefensiveActionClass.CREDENTIAL_MUTATE: "credential_store_write",
    DefensiveActionClass.SERVICE_STATE_CHANGE: "service_control",
}


class EffectDenial(str, Enum):
    """Why an effect was refused. Every value is a DENY; there is no member that
    means "allowed with a caveat", because a caveat is not an authorization."""

    NO_AUTHORIZATION_REGISTERED = "no_authorization_registered"
    ALL_AUTHORIZATIONS_EXPIRED = "all_authorizations_expired"
    TARGET_OUT_OF_SCOPE = "target_out_of_scope"
    ACTION_CLASS_NOT_PERMITTED = "action_class_not_permitted"
    ACTION_CLASS_PROHIBITED = "action_class_prohibited"
    RISK_ABOVE_MAXIMUM = "risk_above_maximum"
    MISSING_TARGET = "missing_target"
    UNRECOGNISED_ACTION = "unrecognised_action"
    NO_CORROBORATING_EVIDENCE = "no_corroborating_evidence"
    ABOVE_AUTONOMY_CEILING = "above_autonomy_ceiling"
    REQUIRES_HUMAN_APPROVAL = "requires_human_approval"
    NOT_APPROVED_BY_GATE = "not_approved_by_gate"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"


# ══════════════════════════════════════════════════════════════════════════════
#  Pre-authorization — the ONLY thing that can permit an automated effect
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ContainmentAuthorization:
    """An operator's standing permission for a bounded class of automated defence.

    It is the defensive twin of
    :class:`~core.security_scope.AuthorizedSecurityScope` and is built on the
    same :class:`~core.authority.ScopePolicy`, so target membership and expiry
    have exactly one implementation and one clock across the whole system.

    Every property §21 requires is a field here and is checked in
    :func:`authorize_effect`:

    ==================  ==========================================================
    explicit            an operator constructs and registers it; nothing else can
    target-scoped       ``policy`` — exact / CIDR / subdomain, never a substring
    action-class        ``permitted_actions`` — a frozenset of typed classes
    time-bound          ``policy.expires_at`` — and an unparseable one reads as
                        already expired (``ScopePolicy._parse_ts``)
    auditable           ``to_dict`` renders it; every decision logs the id
    revocable           :meth:`ContainmentRegistry.revoke`
    ==================  ==========================================================
    """

    authorization_id: str
    policy: ScopePolicy
    permitted_actions: frozenset[DefensiveActionClass] = frozenset()
    prohibited_actions: frozenset[DefensiveActionClass] = frozenset()
    maximum_risk: str = RiskClass.REVERSIBLE.value
    #: When False (the default) an approved request still stops at
    #: REQUIRES_HUMAN_APPROVAL and the executor's NATO challenge decides. Setting
    #: it True is the operator saying "and you may do it without asking me each
    #: time" — for that one action class, on those targets, until it expires.
    unattended: bool = False
    issued_at: str = ""
    issued_by: str = "operator"
    reference: str = ""

    @property
    def expires_at(self) -> str | None:
        return self.policy.expires_at

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.policy.is_expired(now)

    def permits_action(self, action: DefensiveActionClass) -> bool:
        """Prohibition beats permission, always. An authorization that both
        grants and prohibits a class denies it: the narrower statement governs."""
        if action in self.prohibited_actions:
            return False
        return action in self.permitted_actions

    def contains_target(self, target: str, *, now: datetime | None = None) -> bool:
        return self.policy.contains(target, now=now)

    def to_dict(self) -> dict:
        return {
            "authorization_id": self.authorization_id,
            "policy": self.policy.to_dict(),
            "permitted_actions": sorted(a.value for a in self.permitted_actions),
            "prohibited_actions": sorted(a.value for a in self.prohibited_actions),
            "maximum_risk": self.maximum_risk,
            "unattended": self.unattended,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "expires_at": self.expires_at,
            "reference": self.reference,
        }


@dataclass
class ContainmentRegistry:
    """The operator's live containment authorizations. Bounded and revocable.

    Nothing in the detection, correlation, specialist or verifier path calls
    :meth:`register`. That absence is the control, and it is asserted by test.
    """

    authorizations: list[ContainmentAuthorization] = field(default_factory=list)

    def register(self, auth: ContainmentAuthorization) -> bool:
        if len(self.authorizations) >= MAX_AUTHORIZATIONS:
            logger.warning("SECURITY_EFFECTS: authorization registry full; refused %s",
                           auth.authorization_id)
            return False
        if any(a.authorization_id == auth.authorization_id for a in self.authorizations):
            return False
        self.authorizations.append(auth)
        logger.warning("SECURITY_EFFECTS: operator registered containment authorization "
                       "%s (%s) expiring %s", auth.authorization_id,
                       ",".join(sorted(a.value for a in auth.permitted_actions)),
                       auth.expires_at)
        return True

    def revoke(self, authorization_id: str) -> bool:
        before = len(self.authorizations)
        self.authorizations = [a for a in self.authorizations
                               if a.authorization_id != authorization_id]
        revoked = len(self.authorizations) != before
        if revoked:
            logger.warning("SECURITY_EFFECTS: revoked containment authorization %s",
                           authorization_id)
        return revoked

    def active(self, now: datetime | None = None) -> list[ContainmentAuthorization]:
        now = now or _now()
        return [a for a in self.authorizations if not a.is_expired(now)]

    def to_dict(self) -> dict:
        return {"authorizations": [a.to_dict() for a in self.authorizations],
                "active": len(self.active())}


#: The ONE production containment registry. Operator-facing surfaces register
#: here; the correlator, playbooks, auditors and monitors only ever read it
#: through :func:`authorize_effect`.
CONTAINMENT = ContainmentRegistry()

#: The ONE production security-scope registry, shared by the mesh orchestrator
#: and by any offensive path, so there is exactly one scope truth (§24).
SCOPES = SecurityScopeRegistry()


# ══════════════════════════════════════════════════════════════════════════════
#  The decision
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EffectDecision:
    """The gate's answer. ``allowed`` never means "already executed"."""

    allowed: bool
    action: DefensiveActionClass
    target: str
    request: ActionRequest
    reason: str
    denial: EffectDenial | None = None
    authorization_id: str | None = None
    #: True when the operator pre-authorized unattended execution for this class
    #: and target. False means a human still confirms at the executor.
    unattended: bool = False

    @property
    def requires_human(self) -> bool:
        return self.allowed and not self.unattended

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action": self.action.value,
            "target": self.target,
            "reason": self.reason,
            "denial": self.denial.value if self.denial else None,
            "authorization_id": self.authorization_id,
            "unattended": self.unattended,
            "requires_human": self.requires_human,
            "request": self.request.to_dict(),
        }


def propose_effect(
    *,
    action: DefensiveActionClass,
    target: str,
    justification: str,
    requested_by: SpecialistId = SpecialistId.GUARDIAN,
    evidence_ids: "tuple[str, ...]" = (),
    rollback_plan: str = "",
) -> ActionRequest:
    """Build the typed proposal. Constructing one performs nothing.

    Note what is *not* a parameter: severity. A detector that believes something
    is a 10 builds the same request as one that believes it is a 9, because the
    number changes how urgently a human should look, not whether the system may
    act.
    """
    risk = ACTION_RISK.get(action, RiskClass.HIGH_IMPACT)
    return ActionRequest(
        action=action.value,
        target=(target or "").strip(),
        justification=justification,
        requested_by=requested_by,
        evidence_ids=tuple(evidence_ids),
        risk=risk.value,
        reversible=ACTION_REVERSIBLE.get(action, False),
        rollback_plan=rollback_plan,
        required_capability=ACTION_CAPABILITY.get(action, ""),
        required_autonomy=AutonomyLevel.HITL_EXECUTE,
    )


def authorize_effect(
    request: ActionRequest,
    *,
    registry: ContainmentRegistry | None = None,
    graph: EvidenceGraph | None = None,
    ceiling: AutonomyLevel = AutonomyLevel.HITL_EXECUTE,
    now: datetime | None = None,
) -> EffectDecision:
    """Decide one automated effect. Fail-closed at every branch.

    The branch order is deliberate: what cannot be decided is refused before any
    authorization is read, so a malformed or over-broad authorization never even
    gets the question.
    """
    now = now or _now()
    registry = registry if registry is not None else CONTAINMENT

    # 1. A vocabulary we do not recognise is not a permission we can check.
    try:
        action = DefensiveActionClass(request.action)
    except ValueError:
        return EffectDecision(
            False, DefensiveActionClass.PROCESS_TERMINATE, request.target,
            request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                           f"unrecognised action {request.action!r}"),
            f"{request.action!r} is not a recognised defensive action class — "
            f"refused (fail-closed)",
            EffectDenial.UNRECOGNISED_ACTION)

    target = (request.target or "").strip()
    if not target:
        return EffectDecision(
            False, action, target,
            request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE, "no target named"),
            f"{action.value} names no target, so scope membership is undecidable "
            f"— refused", EffectDenial.MISSING_TARGET)

    # 2. No standing authorization at all is the normal, correct state.
    if not registry.authorizations:
        return EffectDecision(
            False, action, target,
            request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                           "no containment authorization is registered"),
            "no ContainmentAuthorization is registered; automated security "
            "effects are refused by default. The finding stands as a "
            "recommendation for the operator.",
            EffectDenial.NO_AUTHORIZATION_REGISTERED)

    live = registry.active(now)
    if not live:
        return EffectDecision(
            False, action, target,
            request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                           "every containment authorization has expired"),
            "every registered containment authorization has expired; an expired "
            "authorization is not an authorization",
            EffectDenial.ALL_AUTHORIZATIONS_EXPIRED)

    # 3. Walk the live authorizations, keeping the most specific denial so the
    #    operator is told what was actually wrong.
    best: EffectDecision | None = None
    risk = ACTION_RISK.get(action, RiskClass.HIGH_IMPACT)
    for auth in live:
        if not auth.contains_target(target, now=now):
            best = best or EffectDecision(
                False, action, target,
                request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                               f"target {target!r} outside every authorization"),
                f"target {target!r} is outside every registered containment "
                f"authorization", EffectDenial.TARGET_OUT_OF_SCOPE)
            continue
        if action in auth.prohibited_actions:
            best = EffectDecision(
                False, action, target,
                request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                               f"{auth.authorization_id} prohibits {action.value}"),
                f"authorization {auth.authorization_id!r} explicitly prohibits "
                f"{action.value}", EffectDenial.ACTION_CLASS_PROHIBITED,
                auth.authorization_id)
            continue
        if not auth.permits_action(action):
            best = EffectDecision(
                False, action, target,
                request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                               f"{auth.authorization_id} does not grant {action.value}"),
                f"authorization {auth.authorization_id!r} covers this target but "
                f"does not grant {action.value}; authorizing a firewall block is "
                f"not authorizing a process kill",
                EffectDenial.ACTION_CLASS_NOT_PERMITTED, auth.authorization_id)
            continue
        if risk_rank(risk.value) > risk_rank(auth.maximum_risk):
            best = EffectDecision(
                False, action, target,
                request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                               f"risk {risk.value} above {auth.maximum_risk}"),
                f"risk {risk.value!r} exceeds authorization "
                f"{auth.authorization_id!r} maximum {auth.maximum_risk!r}",
                EffectDenial.RISK_ABOVE_MAXIMUM, auth.authorization_id)
            continue

        # 4. In scope and in class. Now the evidence and autonomy gates, which
        #    are the mesh's and are applied unchanged.
        decided = dispose_action(request, ceiling=ceiling, graph=graph, scope_ok=True)
        if decided.disposition is ActionDisposition.REFUSED_NO_EVIDENCE:
            return EffectDecision(
                False, action, target, decided,
                "an effect on the world requires corroborating evidence; this "
                "request cites none that a tool, the operator or the world model "
                "produced", EffectDenial.NO_CORROBORATING_EVIDENCE,
                auth.authorization_id)
        if decided.disposition is ActionDisposition.REFUSED_ABOVE_AUTONOMY:
            return EffectDecision(
                False, action, target, decided, decided.disposition_reason,
                EffectDenial.ABOVE_AUTONOMY_CEILING, auth.authorization_id)

        # 5. Allowed. Whether a human still confirms is the operator's call,
        #    made in advance, per action class and per target — never the
        #    detector's and never a consequence of how bad the alert looked.
        if auth.unattended:
            approved = decided.decide(
                ActionDisposition.APPROVED_FOR_EXECUTOR,
                f"pre-authorized unattended by {auth.authorization_id}; the "
                f"executor's own risk/HITL gate still applies")
            return EffectDecision(
                True, action, target, approved,
                f"{action.value} authorized against {target!r} by "
                f"{auth.authorization_id!r} (unattended)",
                None, auth.authorization_id, unattended=True)
        approved = decided.decide(
            ActionDisposition.REQUIRES_HUMAN_APPROVAL,
            f"in scope under {auth.authorization_id}; a human confirms before "
            f"ToolExecutor is reached")
        return EffectDecision(
            True, action, target, approved,
            f"{action.value} is in scope under {auth.authorization_id!r} and "
            f"awaits human confirmation", None, auth.authorization_id,
            unattended=False)

    return best or EffectDecision(
        False, action, target,
        request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                       f"target {target!r} outside every authorization"),
        f"target {target!r} is outside every registered containment authorization",
        EffectDenial.TARGET_OUT_OF_SCOPE)


# ══════════════════════════════════════════════════════════════════════════════
#  The only path from a decision to a world effect
# ══════════════════════════════════════════════════════════════════════════════
async def execute_effect(
    decision: EffectDecision,
    *,
    tool_executor,
    tool_name: str,
    tool_input: dict,
    reasoning: str = "",
) -> dict:
    """Hand an APPROVED effect to the one :class:`ToolExecutor`.

    This function performs no effect of its own. It refuses anything the gate did
    not approve, and everything it forwards still passes the executor's own
    preflight, guardrail, ``authorize_action``, risk-class, LAB_ONLY and NATO
    HITL gates. There is no argument that skips them.
    """
    if not decision.allowed:
        return {"ok": False, "executed": False,
                "denial": decision.denial.value if decision.denial else "denied",
                "error": decision.reason}
    if decision.request.disposition is not ActionDisposition.APPROVED_FOR_EXECUTOR:
        return {"ok": False, "executed": False,
                "denial": EffectDenial.REQUIRES_HUMAN_APPROVAL.value,
                "error": decision.request.disposition_reason
                         or "the request awaits human approval"}
    if tool_executor is None:
        return {"ok": False, "executed": False,
                "denial": EffectDenial.EXECUTOR_UNAVAILABLE.value,
                "error": "no ToolExecutor is attached; there is no other path to "
                         "an effect"}
    logger.critical(
        "SECURITY_EFFECT: executing %s against %s under authorization %s",
        decision.action.value, decision.target, decision.authorization_id)
    result = await tool_executor.aexecute(
        tool_name, dict(tool_input),
        reasoning or f"{decision.action.value}: {decision.reason}")
    return {"ok": True, "executed": True, "result": result,
            "authorization_id": decision.authorization_id}


# ══════════════════════════════════════════════════════════════════════════════
#  Offensive side — one scope truth for active security work
# ══════════════════════════════════════════════════════════════════════════════
def authorize_active_security(
    *,
    activity: "ActivityClass | str",
    target: str | None,
    risk: str = RiskClass.READ_ONLY.value,
    registry: SecurityScopeRegistry | None = None,
    application: str | None = None,
    now: datetime | None = None,
) -> SecurityScopeDecision:
    """The single entry point for "may I touch this target?".

    A thin, deliberate delegation to
    :func:`~core.security_scope.authorize_security_activity` against the shared
    :data:`SCOPES` registry. It exists so that every offensive path in the
    repository — SPECTER, VIOLET and the legacy ARES operator — resolves the
    same registry through the same function, rather than each keeping its own
    idea of what "authorized" means.
    """
    return authorize_security_activity(
        registry if registry is not None else SCOPES,
        activity=activity, target=target, risk=risk,
        application=application, now=now)


def containment_authorization(
    *,
    authorization_id: str,
    targets: "tuple[str, ...]" = (),
    cidrs: "tuple[str, ...]" = (),
    actions: "tuple[DefensiveActionClass, ...]" = (),
    expires_at: str | None = None,
    maximum_risk: str = RiskClass.REVERSIBLE.value,
    unattended: bool = False,
    issued_by: str = "operator",
    reference: str = "",
) -> ContainmentAuthorization:
    """Operator-facing builder. Convenience only — it grants nothing on its own;
    the authorization must still be registered, and registering is an operator
    act that no detector performs."""
    return ContainmentAuthorization(
        authorization_id=authorization_id,
        policy=ScopePolicy(
            scope_id=authorization_id,
            name=reference or authorization_id,
            mode=AuthorityMode.INCIDENT_RESPONSE,
            targets=frozenset(targets),
            cidrs=tuple(cidrs),
            expires_at=expires_at,
            created_by=issued_by,
            notes=reference,
        ),
        permitted_actions=frozenset(actions),
        maximum_risk=maximum_risk,
        unattended=unattended,
        issued_at=_now().isoformat(),
        issued_by=issued_by,
        reference=reference,
    )


def effect_requires_hitl(action: DefensiveActionClass) -> bool:
    """Whether the executor will challenge for this class. Reported so a caller
    can tell the operator what will happen, never so it can avoid it."""
    return requires_hitl(ACTION_RISK.get(action, RiskClass.HIGH_IMPACT))


def authority_snapshot(state: AuthorityState | None = None) -> dict:
    """A bounded, redaction-safe view of what is currently authorized, for the
    HUD and the audit log."""
    return {
        "containment": CONTAINMENT.to_dict(),
        "security_scopes": SCOPES.to_dict(),
        "authority_mode": (state.mode.value if state is not None else None),
    }
