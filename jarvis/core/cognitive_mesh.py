"""
core/cognitive_mesh.py — V69 M64: the specialist registry and autonomy ladder.

ONE JARVIS, many disciplined expert perspectives. A *specialist* here is a
**role contract**, never a resident model and never a second assistant: a
mission, a competency set, a context policy, a capability policy, a risk
posture, handoff rules, an evidence requirement and a completion contract.

This module is the registry and nothing else. It executes nothing, calls no
model and touches no tool. It is the typed, frozen answer to *"what is this
specialist allowed to be?"*, and every runtime component reads it rather than
carrying its own copy of the answer.

Reuse, not replacement — every axis binds to machinery that already exists:

    which model runs        → ``core.model_router.ModelRole``   (advisory only)
    which tools are reachable → ``core.specialist_runtime.ToolCategory``
                                and its ``ToolBroker`` allowlist
    how dangerous an action is → ``core.risk_classes.RiskClass``
    which security activities  → ``core.security_scope.ActivityClass``
    who actually runs the turn → ``core.specialist_runtime.SpecialistRole``

That last binding is the load-bearing one: every M64 specialist maps onto an
existing ``SpecialistRole``, so ``SpecialistTeamRuntime`` executes the mesh with
no second executive, no second blackboard and no second tool path.

**A specialist cannot raise its own autonomy.** The registry is module-level,
frozen, and built from code — not from configuration a prompt could reach and
not from any field a model emits. :func:`registry` returns the one instance;
constructing a second one is possible only in a test sandbox and grants nothing,
because every enforcement point reads the module singleton.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

from core.model_router import ModelRole
from core.risk_classes import RiskClass
from core.security_scope import ActivityClass
from core.specialist_runtime import SpecialistRole, ToolCategory


# ══════════════════════════════════════════════════════════════════════════════
#  Autonomy
# ══════════════════════════════════════════════════════════════════════════════
class AutonomyLevel(IntEnum):
    """How far a specialist may act on its own (§8).

    Ordered so that "within the ceiling" is an integer comparison — except for
    :attr:`PROHIBITED`, which :func:`permits` refuses on both sides. Making
    PROHIBITED the largest member and then refusing it explicitly is deliberate:
    a ceiling set to PROHIBITED would otherwise read as "everything is allowed",
    which is precisely the bug this ladder exists to prevent.
    """

    ADVISE = 0        # reason only; no tool execution of any kind
    OBSERVE = 1       # read-only tools, World State, logs, files, status
    SAFE_EXECUTE = 2  # reversible low-risk actions the capability policy permits
    HITL_EXECUTE = 3  # effectful / security-impacting; human confirmation required
    PROHIBITED = 4    # never executable by the agent runtime, at any ceiling


def permits(ceiling: AutonomyLevel, required: AutonomyLevel) -> bool:
    """Whether an action needing *required* may run under *ceiling*.

    Fail-closed on both sides: a PROHIBITED requirement is never permitted, and a
    PROHIBITED ceiling permits nothing rather than everything.
    """
    if ceiling is AutonomyLevel.PROHIBITED or required is AutonomyLevel.PROHIBITED:
        return False
    return int(required) <= int(ceiling)


#: The autonomy each risk class demands.
#:
#: NOTE, and this is a deliberate divergence from the directive's L2 wording:
#: ``RiskClass.REVERSIBLE`` maps to HITL_EXECUTE, not SAFE_EXECUTE.
#: ``core.risk_classes`` already requires a HITL challenge for REVERSIBLE and
#: says so in its own docstring ("HITL is never removed where it already
#: applied"). M64 is an architecture milestone; it does not get to relax a
#: control that predates it. SAFE_EXECUTE therefore covers LOW_IMPACT only —
#: actions that mutate JARVIS's own notes and vector store and nothing else.
RISK_AUTONOMY: dict[RiskClass, AutonomyLevel] = {
    RiskClass.READ_ONLY: AutonomyLevel.OBSERVE,
    RiskClass.LOW_IMPACT: AutonomyLevel.SAFE_EXECUTE,
    RiskClass.REVERSIBLE: AutonomyLevel.HITL_EXECUTE,
    RiskClass.HIGH_IMPACT: AutonomyLevel.HITL_EXECUTE,
    RiskClass.LAB_ONLY: AutonomyLevel.HITL_EXECUTE,
}


def autonomy_for_risk(risk: RiskClass) -> AutonomyLevel:
    """Autonomy demanded by *risk*. An unrecognised class demands HITL_EXECUTE,
    matching ``classify_tool``'s own fail-closed default of HIGH_IMPACT."""
    return RISK_AUTONOMY.get(risk, AutonomyLevel.HITL_EXECUTE)


# ══════════════════════════════════════════════════════════════════════════════
#  Identity
# ══════════════════════════════════════════════════════════════════════════════
class SpecialistId(str, Enum):
    """The mesh roster. Codenames are stable identifiers, not personalities —
    the operator sees one assistant."""

    JARVIS = "jarvis"        # orchestrator
    ATLAS = "atlas"          # generalist / chief of staff
    FORGE = "forge"          # software engineering
    HELIOS = "helios"        # systems / devops / SRE
    MESH = "mesh"            # network engineering
    GUARDIAN = "guardian"    # blue team / SOC
    TRACE = "trace"          # DFIR
    ORACLE = "oracle"        # threat intelligence / OSINT
    SPECTER = "specter"      # authorized red team
    VIOLET = "violet"        # purple team
    CIRRUS = "cirrus"        # cloud
    CIRCUIT = "circuit"      # embedded / IoT
    ARCHIVIST = "archivist"  # research
    ARGUS = "argus"          # verifier


class ContextSlice(str, Enum):
    """What a specialist's compiled context may contain (§48). A slice absent
    from ``preferred_context`` is not assembled for that specialist at all."""

    OPERATOR_REQUEST = "operator_request"
    TASK_OBJECTIVE = "task_objective"
    WORLD_STATE = "world_state"
    MEMORY = "memory"
    EVIDENCE = "evidence"
    BLACKBOARD = "blackboard"
    SECURITY_SCOPE = "security_scope"
    CODE = "code"
    TELEMETRY = "telemetry"


class EvidencePolicy(str, Enum):
    """How strictly a specialist's claims must be bound to evidence (§16)."""

    NONE_REQUIRED = "none_required"      # ordinary help; assertions stand as advice
    CITE_ON_CLAIM = "cite_on_claim"      # a factual claim about the world needs a ref
    EVIDENCE_REQUIRED = "evidence_required"  # every finding needs a ref, or it is UNVERIFIED


@dataclass(frozen=True)
class SpecialistRecord:
    """One typed, immutable role contract (§7).

    Frozen on purpose. Capability, autonomy and scope are the three things a
    compromised or confused specialist would most want to edit, and the only way
    to change any of them is to change this file and land a commit.
    """

    specialist_id: SpecialistId
    codename: str
    official_role: str
    mission: str
    competencies: tuple[str, ...]
    preferred_context: tuple[ContextSlice, ...]
    allowed_capabilities: frozenset[ToolCategory]
    prohibited_capabilities: frozenset[ToolCategory]
    preferred_model_roles: tuple[ModelRole, ...]
    default_autonomy: AutonomyLevel
    maximum_risk: RiskClass
    evidence_policy: EvidencePolicy
    allowed_handoffs: frozenset[SpecialistId]
    escalation_rules: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    completion_contract: tuple[str, ...]
    runtime_role: SpecialistRole                     # the executing role, reused
    security_activities: frozenset[ActivityClass] = frozenset()
    requires_security_scope: bool = False
    memory_scope: str = "session"
    #: The ceiling this specialist may reach ONLY while an operator-registered
    #: AuthorizedSecurityScope actually covers the target and grants the
    #: activity. Defaulting to ``None`` means "no lift exists" -- most
    #: specialists simply have one ceiling. This is not self-granting: the lift
    #: is conditional on operator data the specialist cannot write, and
    #: :meth:`ceiling_with_scope` is the only way to obtain it.
    scoped_autonomy: "AutonomyLevel | None" = None

    # ── policy questions, answered here so no caller re-derives them ─────────
    def capability_allowed(self, category: ToolCategory) -> bool:
        """Prohibition beats permission. A category in both lists is denied."""
        if category in self.prohibited_capabilities:
            return False
        return category in self.allowed_capabilities

    def may_handoff_to(self, other: SpecialistId) -> bool:
        return other in self.allowed_handoffs

    def permits_activity(self, activity: ActivityClass) -> bool:
        return activity in self.security_activities

    def ceiling_with_scope(self, scope_valid: bool) -> AutonomyLevel:
        """The effective ceiling given whether a real scope decision allowed the
        activity. Without one the registry default stands; the lift is never
        larger than :attr:`scoped_autonomy`, and never exists unless declared."""
        if not scope_valid or self.scoped_autonomy is None:
            return self.default_autonomy
        return max(self.default_autonomy, self.scoped_autonomy, key=int)

    def to_dict(self) -> dict:
        return {
            "specialist_id": self.specialist_id.value,
            "codename": self.codename,
            "official_role": self.official_role,
            "mission": self.mission,
            "competencies": list(self.competencies),
            "preferred_context": [c.value for c in self.preferred_context],
            "allowed_capabilities": sorted(c.value for c in self.allowed_capabilities),
            "prohibited_capabilities":
                sorted(c.value for c in self.prohibited_capabilities),
            "preferred_model_roles": [r.value for r in self.preferred_model_roles],
            "default_autonomy": int(self.default_autonomy),
            "maximum_risk": self.maximum_risk.value,
            "evidence_policy": self.evidence_policy.value,
            "allowed_handoffs": sorted(h.value for h in self.allowed_handoffs),
            "escalation_rules": list(self.escalation_rules),
            "stop_conditions": list(self.stop_conditions),
            "completion_contract": list(self.completion_contract),
            "runtime_role": self.runtime_role.value,
            "security_activities": sorted(a.value for a in self.security_activities),
            "requires_security_scope": self.requires_security_scope,
            "memory_scope": self.memory_scope,
            "scoped_autonomy":
                int(self.scoped_autonomy) if self.scoped_autonomy is not None else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Budgets (§13, §37, §47)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class MeshBudget:
    """Finite ceilings for one task. Every field is a hard stop; exhausting one
    yields a PARTIAL result naming what remains, never an extra attempt.

    ``max_specialists`` is 4 rather than the directive's suggested 4-with-verifier
    because ``specialist_runtime._MAX_TOTAL_AGENTS`` is already 4 and is enforced
    by ``TeamExecutionPolicy`` on the CPU-bound target host. The repository's own
    stricter convention wins, exactly as §13 instructs.
    """

    max_specialists: int = 4
    max_handoff_depth: int = 3
    max_handoffs: int = 6
    max_verifier_retries: int = 2
    max_tool_calls: int = 12
    max_runtime_s: float = 180.0
    max_context_chars: int = 6_000
    max_evidence_items: int = 60
    max_claims: int = 40

    def to_dict(self) -> dict:
        return {
            "max_specialists": self.max_specialists,
            "max_handoff_depth": self.max_handoff_depth,
            "max_handoffs": self.max_handoffs,
            "max_verifier_retries": self.max_verifier_retries,
            "max_tool_calls": self.max_tool_calls,
            "max_runtime_s": self.max_runtime_s,
            "max_context_chars": self.max_context_chars,
            "max_evidence_items": self.max_evidence_items,
            "max_claims": self.max_claims,
        }


DEFAULT_BUDGET = MeshBudget()

#: The fast path's budget (§38): one specialist, no tools, no verifier retries.
#: "What is 2+2?" must not spawn a team, and this is the shape that guarantees it.
FAST_PATH_BUDGET = MeshBudget(
    max_specialists=1, max_handoff_depth=0, max_handoffs=0,
    max_verifier_retries=0, max_tool_calls=0, max_runtime_s=30.0,
    max_context_chars=1_500,
)


# ══════════════════════════════════════════════════════════════════════════════
#  The roster (§9)
# ══════════════════════════════════════════════════════════════════════════════
_C = ToolCategory
_S = SpecialistId
_A = ActivityClass
_CS = ContextSlice

#: Every specialist may hand off to the verifier; stated once so no record can
#: forget it and quietly become unverifiable.
_ALWAYS = frozenset({_S.ARGUS})

_RECORDS: tuple[SpecialistRecord, ...] = (
    SpecialistRecord(
        specialist_id=_S.JARVIS, codename="JARVIS",
        official_role="Cognitive orchestrator",
        mission="Understand operator intent, choose the right experts, preserve "
                "context, integrate results, and return ONE coherent answer.",
        competencies=("intent classification", "delegation", "context budgeting",
                      "conflict resolution", "final synthesis"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.BLACKBOARD,
                           _CS.EVIDENCE, _CS.MEMORY),
        allowed_capabilities=frozenset({_C.READ}),
        prohibited_capabilities=frozenset({_C.RECON, _C.CODE, _C.SYSTEM, _C.FORENSIC}),
        preferred_model_roles=(ModelRole.DEEP, ModelRole.FAST),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.CITE_ON_CLAIM,
        allowed_handoffs=frozenset(set(_S) - {_S.JARVIS}),
        escalation_rules=("a domain expert exists -> delegate rather than answer",
                          "specialists disagree -> collect discriminating evidence, "
                          "then ARGUS, then say uncertainty remains"),
        stop_conditions=("the operator's question is answered",
                         "a budget is exhausted -> return PARTIAL"),
        completion_contract=("one coherent answer", "verification status stated",
                             "unresolved questions listed"),
        runtime_role=SpecialistRole.PLANNER, memory_scope="session",
    ),
    SpecialistRecord(
        specialist_id=_S.ATLAS, codename="ATLAS",
        official_role="Generalist / chief of staff",
        mission="Everyday assistance: studying, planning, organisation, writing, "
                "decision support, project management and general knowledge.",
        competencies=("study support", "planning", "writing", "summarisation",
                      "decision support", "project management", "general knowledge",
                      "mathematics", "language"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.MEMORY),
        allowed_capabilities=frozenset({_C.READ}),
        prohibited_capabilities=frozenset({_C.RECON, _C.SYSTEM, _C.FORENSIC}),
        preferred_model_roles=(ModelRole.FAST, ModelRole.DEEP),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.NONE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.FORGE, _S.ARCHIVIST, _S.HELIOS, _S.MESH},
        escalation_rules=("the question turns out to be a domain problem -> hand off",
                          "a claim about the operator's live environment is needed -> "
                          "HELIOS or MESH, never a guess"),
        stop_conditions=("the request is satisfied",
                         "the answer needs facts ATLAS cannot obtain"),
        completion_contract=("a direct, usable answer", "uncertainty flagged, not hidden"),
        runtime_role=SpecialistRole.GENERAL, memory_scope="session",
    ),
    SpecialistRecord(
        specialist_id=_S.FORGE, codename="FORGE",
        official_role="Software engineering",
        mission="Build and debug software against the code that actually exists.",
        competencies=("python", "javascript", "typescript", "android", "backend",
                      "frontend", "apis", "databases", "tests", "architecture",
                      "performance", "secure coding", "refactoring"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.CODE,
                           _CS.EVIDENCE, _CS.MEMORY),
        allowed_capabilities=frozenset({_C.READ, _C.CODE}),
        prohibited_capabilities=frozenset({_C.RECON, _C.FORENSIC}),
        preferred_model_roles=(ModelRole.CODER, ModelRole.DEEP),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.LOW_IMPACT,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.HELIOS, _S.MESH, _S.CIRRUS, _S.CIRCUIT},
        escalation_rules=("the fault is environmental, not in the code -> HELIOS",
                          "a fix touches security posture -> ARGUS before claiming done"),
        stop_conditions=("the bug is reproduced, fixed and the fix is tested",
                         "reproduction fails -> say so; do not guess a fix"),
        completion_contract=("reproduce -> localize -> fix -> focused test -> "
                             "regression test -> verifier",
                             "never modify a test expectation to hide a bug",
                             "a changed assertion is explained by which state it belonged to"),
        runtime_role=SpecialistRole.CODE, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.HELIOS, codename="HELIOS",
        official_role="Systems / DevOps / SRE",
        mission="Keep systems healthy and diagnose them before touching them.",
        competencies=("linux", "windows", "processes", "services", "containers",
                      "virtualization", "storage", "observability", "deployment",
                      "runtime diagnosis", "performance"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.WORLD_STATE,
                           _CS.TELEMETRY, _CS.EVIDENCE),
        allowed_capabilities=frozenset({_C.READ, _C.SYSTEM}),
        prohibited_capabilities=frozenset({_C.RECON}),
        preferred_model_roles=(ModelRole.FAST, ModelRole.DEEP),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.MESH, _S.FORGE, _S.GUARDIAN, _S.CIRRUS},
        escalation_rules=("the fault is connectivity -> MESH",
                          "the fault looks adversarial -> GUARDIAN",
                          "any remediation is effectful -> ActionRequest, never a "
                          "direct restart"),
        stop_conditions=("the failing component and its cause are identified",
                         "World State and logs disagree -> report the disagreement"),
        completion_contract=("World State -> service state -> dependencies -> "
                             "resources -> logs -> remediation",
                             "no blind restart: a restart is a proposal, not a first step"),
        runtime_role=SpecialistRole.OPERATIONAL, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.MESH, codename="MESH",
        official_role="Network engineering",
        mission="Understand and troubleshoot connectivity from the bottom up.",
        competencies=("ethernet", "arp", "vlan", "stp", "routing", "switching",
                      "dns", "dhcp", "tcp/ip", "nat", "firewalling", "vpn",
                      "packet analysis", "topology"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.WORLD_STATE,
                           _CS.EVIDENCE),
        allowed_capabilities=frozenset({_C.READ, _C.SYSTEM}),
        prohibited_capabilities=frozenset({_C.RECON}),
        preferred_model_roles=(ModelRole.FAST, ModelRole.DEEP),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.HELIOS, _S.GUARDIAN, _S.CIRRUS},
        escalation_rules=("the topology is unknown -> read World State before probing",
                          "an active probe is needed against a non-owned target -> "
                          "that is SPECTER's question, and it needs a scope"),
        stop_conditions=("the layer at which connectivity breaks is identified",
                         "World State already answers the question -> stop, do not probe"),
        completion_contract=("configuration -> interface/link -> addressing -> L2 -> "
                             "routing -> DNS -> transport -> application",
                             "no random ping/traceroute spam"),
        runtime_role=SpecialistRole.OPERATIONAL, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.GUARDIAN, codename="GUARDIAN",
        official_role="Blue team / SOC",
        mission="DEFEND: triage, correlate, hunt, and recommend containment.",
        competencies=("soc triage", "siem", "wazuh", "sysmon", "zeek", "sigma",
                      "mitre att&ck", "detection engineering", "alert correlation",
                      "threat hunting", "incident response", "containment planning"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.WORLD_STATE,
                           _CS.EVIDENCE, _CS.TELEMETRY, _CS.BLACKBOARD),
        allowed_capabilities=frozenset({_C.READ, _C.FORENSIC}),
        prohibited_capabilities=frozenset({_C.RECON, _C.CODE}),
        preferred_model_roles=(ModelRole.DEEP,),
        # L1 OBSERVE by directive. GUARDIAN recommends containment; it never
        # performs it. Every containment leaves here as an ActionRequest.
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.TRACE, _S.ORACLE, _S.HELIOS, _S.MESH, _S.VIOLET},
        escalation_rules=("evidence may be destroyed by the next step -> TRACE first",
                          "the indicator needs external context -> ORACLE",
                          "containment is warranted -> ActionRequest + HITL, always"),
        stop_conditions=("the alert is confirmed or refuted with evidence",
                         "containment is recommended and awaiting a human"),
        completion_contract=("triage -> verify -> assets -> evidence -> correlate -> "
                             "timeline -> ATT&CK -> hypotheses -> verify -> "
                             "containment recommendation -> HITL -> recovery validation",
                             "severity and confidence are reported separately"),
        runtime_role=SpecialistRole.CYBER_BLUE, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.TRACE, codename="TRACE",
        official_role="DFIR",
        mission="Preserve and reconstruct evidence. Preserve before modify.",
        competencies=("forensic timelines", "hashes", "process artefacts", "logs",
                      "filesystem evidence", "persistence artefacts",
                      "network evidence", "chain of custody"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.EVIDENCE,
                           _CS.WORLD_STATE, _CS.TELEMETRY),
        allowed_capabilities=frozenset({_C.READ, _C.FORENSIC}),
        prohibited_capabilities=frozenset({_C.RECON, _C.CODE, _C.SYSTEM}),
        preferred_model_roles=(ModelRole.DEEP,),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.GUARDIAN, _S.ORACLE},
        escalation_rules=("an action would destroy unacquired evidence -> return "
                          "EVIDENCE_PRESERVATION_REQUIRED and stop",
                          "attribution is asked for -> ORACLE, marked REPORTED"),
        stop_conditions=("the timeline is reconstructed and its gaps are named",
                         "acquisition is incomplete -> say which artefact is missing"),
        completion_contract=("preserve -> hash -> acquire -> timeline -> correlate -> "
                             "analyze -> report",
                             "every conclusion cites the artefact it rests on"),
        runtime_role=SpecialistRole.DFIR, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.ORACLE, codename="ORACLE",
        official_role="Threat intelligence / OSINT",
        mission="Understand external threats and public evidence, and label how "
                "each claim is known.",
        competencies=("cves", "iocs", "ttps", "campaigns", "malware families",
                      "att&ck mapping", "source evaluation", "public-source intelligence"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.EVIDENCE),
        allowed_capabilities=frozenset({_C.READ, _C.WEB}),
        prohibited_capabilities=frozenset({_C.RECON, _C.SYSTEM, _C.CODE}),
        preferred_model_roles=(ModelRole.DEEP,),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.GUARDIAN, _S.TRACE, _S.ARCHIVIST},
        escalation_rules=("a source cannot be corroborated -> label it REPORTED",
                          "the request targets a private individual -> refuse"),
        stop_conditions=("the indicator is enriched or explicitly unenrichable",),
        completion_contract=("every claim labelled OBSERVED, REPORTED or INFERRED",
                             "no fabricated sources; no doxxing; no invasive targeting"),
        runtime_role=SpecialistRole.RESEARCH, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.SPECTER, codename="SPECTER",
        official_role="Authorized red team",
        mission="Safely emulate an adversary against explicitly authorized "
                "environments, proving the minimum necessary and no more.",
        competencies=("reconnaissance", "service enumeration", "attack surface analysis",
                      "vulnerability validation", "exploitability verification",
                      "safe web testing", "api testing", "privilege-path analysis",
                      "credential-control testing", "adversary emulation", "att&ck mapping"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.SECURITY_SCOPE,
                           _CS.WORLD_STATE, _CS.EVIDENCE),
        allowed_capabilities=frozenset({_C.READ, _C.RECON}),
        prohibited_capabilities=frozenset({_C.FORENSIC}),
        preferred_model_roles=(ModelRole.DEEP,),
        # ADVISE by default: with no scope in hand SPECTER reasons and nothing
        # more. A valid scope is what lifts it, and only to what the scope grants.
        default_autonomy=AutonomyLevel.ADVISE,
        maximum_risk=RiskClass.LAB_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.FORGE, _S.VIOLET, _S.ORACLE},
        security_activities=frozenset({
            _A.PASSIVE_RECON, _A.READ_ONLY_ENUMERATION, _A.ACTIVE_SERVICE_VALIDATION,
            _A.WEB_VULNERABILITY_VALIDATION, _A.AUTH_CONTROL_VALIDATION,
            _A.PRIVILEGE_PATH_VALIDATION, _A.EXPLOIT_PROOF_MINIMAL,
        }),
        requires_security_scope=True,
        # With a live scope SPECTER may OBSERVE -- read-only enumeration through
        # the ToolBroker. It never reaches SAFE_EXECUTE or above: an active
        # validation or a proof is an ActionRequest and a human decision.
        scoped_autonomy=AutonomyLevel.OBSERVE,
        escalation_rules=("no valid scope -> reason only; propose the scope needed",
                          "a rung is only climbed with evidence and a hypothesis",
                          "the proof is sufficient -> stop"),
        stop_conditions=("the hypothesis is settled",
                         "the next step is not necessary to settle it",
                         "scope expired, target out of scope, or activity not granted"),
        completion_contract=("understand -> read-only enumeration -> active safe "
                             "validation -> minimum exploit proof",
                             "harmless marker over destructive effect, always",
                             "never destructive wiping, ransomware, real exfiltration, "
                             "mass exploitation, internet-wide scanning, covert "
                             "persistence or uncontrolled credential theft"),
        runtime_role=SpecialistRole.CYBER_PURPLE, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.VIOLET, codename="VIOLET",
        official_role="Purple team",
        mission="Make red and blue improve each other, and measure whether the "
                "detection actually fired.",
        competencies=("adversary emulation", "detection validation", "telemetry analysis",
                      "detection gap analysis", "att&ck technique mapping", "retesting"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.SECURITY_SCOPE,
                           _CS.EVIDENCE, _CS.TELEMETRY, _CS.BLACKBOARD),
        allowed_capabilities=frozenset({_C.READ, _C.FORENSIC}),
        prohibited_capabilities=frozenset({_C.CODE}),
        preferred_model_roles=(ModelRole.DEEP,),
        default_autonomy=AutonomyLevel.ADVISE,
        maximum_risk=RiskClass.LAB_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.SPECTER, _S.GUARDIAN, _S.TRACE},
        security_activities=frozenset({_A.PURPLE_EMULATION, _A.PASSIVE_RECON}),
        requires_security_scope=True,
        scoped_autonomy=AutonomyLevel.OBSERVE,
        escalation_rules=("emulation needs a scope -> SPECTER's scope check, not a "
                          "second one",
                          "a detection is claimed to work -> require test evidence"),
        stop_conditions=("the detection status is measured, or the gap is named",),
        completion_contract=("technique -> hypothesis -> authorized emulation -> "
                             "expected telemetry -> actual telemetry -> detection "
                             "result -> gap -> remediation -> retest",
                             "a detection is never claimed to work without a retest"),
        runtime_role=SpecialistRole.CYBER_PURPLE, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.CIRRUS, codename="CIRRUS",
        official_role="Cloud",
        mission="Analyse cloud configuration and posture; mutations stay under "
                "canonical authority.",
        competencies=("aws", "azure", "gcp", "iam", "cloud networking", "storage",
                      "compute", "logging", "container security", "posture management",
                      "incident investigation", "cost awareness"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.WORLD_STATE,
                           _CS.EVIDENCE),
        allowed_capabilities=frozenset({_C.READ, _C.WEB}),
        prohibited_capabilities=frozenset({_C.RECON, _C.FORENSIC}),
        preferred_model_roles=(ModelRole.DEEP,),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.HELIOS, _S.MESH, _S.GUARDIAN},
        escalation_rules=("an IAM policy, security group, network or instance would "
                          "change -> ActionRequest under canonical authority",),
        stop_conditions=("the posture question is answered from configuration",),
        completion_contract=("configuration analysis is separated from cloud mutation",
                             "every mutation is a proposal, never an act"),
        runtime_role=SpecialistRole.ARCHITECT, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.CIRCUIT, codename="CIRCUIT",
        official_role="Embedded / IoT",
        mission="Reason about firmware, MCUs, sensors and embedded protocols.",
        competencies=("mcu", "firmware", "arduino", "serial", "ble", "wifi", "lora",
                      "sensors", "embedded protocols", "iot security",
                      "hardware/software integration"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.CODE,
                           _CS.EVIDENCE),
        allowed_capabilities=frozenset({_C.READ, _C.CODE}),
        prohibited_capabilities=frozenset({_C.RECON, _C.FORENSIC}),
        preferred_model_roles=(ModelRole.CODER, ModelRole.DEEP),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.CITE_ON_CLAIM,
        allowed_handoffs=_ALWAYS | {_S.FORGE, _S.MESH},
        escalation_rules=("a hardware effect is requested -> explicit device scope, "
                          "as an ActionRequest",),
        stop_conditions=("the firmware or sensor behaviour is explained",),
        completion_contract=("hardware-effect execution requires an explicit device scope",),
        runtime_role=SpecialistRole.CODE, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.ARCHIVIST, codename="ARCHIVIST",
        official_role="Research",
        mission="Investigate difficult questions with corroborated, cited evidence.",
        competencies=("source quality", "corroboration", "uncertainty quantification",
                      "literature review", "evidence-backed synthesis"),
        preferred_context=(_CS.OPERATOR_REQUEST, _CS.TASK_OBJECTIVE, _CS.EVIDENCE,
                           _CS.MEMORY),
        allowed_capabilities=frozenset({_C.READ, _C.WEB}),
        prohibited_capabilities=frozenset({_C.RECON, _C.SYSTEM, _C.FORENSIC}),
        preferred_model_roles=(ModelRole.DEEP,),
        default_autonomy=AutonomyLevel.OBSERVE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=_ALWAYS | {_S.ORACLE, _S.ATLAS},
        escalation_rules=("sources conflict -> report both with their quality",),
        stop_conditions=("the question is answered, or the evidence is insufficient "
                         "and that is the answer",),
        completion_contract=("never fabricate a source",
                             "corroboration count and source quality are reported"),
        runtime_role=SpecialistRole.RESEARCH, memory_scope="project",
    ),
    SpecialistRecord(
        specialist_id=_S.ARGUS, codename="ARGUS",
        official_role="Verifier",
        mission="Distrust everyone constructively: check claims against evidence, "
                "scope against authority, and completion against the request.",
        competencies=("claim/evidence binding", "scope audit", "authority audit",
                      "contradiction detection", "completion audit"),
        preferred_context=(_CS.TASK_OBJECTIVE, _CS.EVIDENCE, _CS.BLACKBOARD,
                           _CS.SECURITY_SCOPE),
        allowed_capabilities=frozenset({_C.READ}),
        prohibited_capabilities=frozenset({_C.RECON, _C.SYSTEM, _C.CODE, _C.FORENSIC,
                                           _C.WEB}),
        preferred_model_roles=(ModelRole.VERIFIER, ModelRole.FAST),
        # ARGUS audits. It never acts, and it can never grant what it audits.
        default_autonomy=AutonomyLevel.ADVISE,
        maximum_risk=RiskClass.READ_ONLY,
        evidence_policy=EvidencePolicy.EVIDENCE_REQUIRED,
        allowed_handoffs=frozenset(),
        escalation_rules=("evidence is missing -> INSUFFICIENT_EVIDENCE, never a pass",
                          "a specialist acted outside scope -> SCOPE_VIOLATION"),
        stop_conditions=("a verdict is reached",
                         "retries are exhausted -> the last verdict stands"),
        completion_contract=("cannot grant authority",
                             "cannot rewrite evidence",
                             "cannot execute a high-risk action"),
        runtime_role=SpecialistRole.VERIFIER, memory_scope="session",
    ),
)


class SpecialistRegistry:
    """The typed roster. Read-only after construction; there is no setter, no
    ``update`` and no path from text to a record."""

    def __init__(self, records: "tuple[SpecialistRecord, ...]" = _RECORDS) -> None:
        self._by_id: dict[SpecialistId, SpecialistRecord] = {}
        for record in records:
            if record.specialist_id in self._by_id:
                raise ValueError(f"duplicate specialist {record.specialist_id.value}")
            self._by_id[record.specialist_id] = record

    def __contains__(self, specialist_id: object) -> bool:
        return specialist_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, specialist_id: SpecialistId) -> SpecialistRecord:
        """The record for *specialist_id*. Raises rather than returning a
        permissive default — an unknown specialist is a bug, not a wildcard."""
        try:
            return self._by_id[specialist_id]
        except KeyError:
            raise KeyError(f"no such specialist: {specialist_id!r}") from None

    def all(self) -> "tuple[SpecialistRecord, ...]":
        return tuple(self._by_id[s] for s in SpecialistId if s in self._by_id)

    def ids(self) -> "tuple[SpecialistId, ...]":
        return tuple(self._by_id)

    # ── policy questions ─────────────────────────────────────────────────────
    def autonomy_ceiling(self, specialist_id: SpecialistId) -> AutonomyLevel:
        """The specialist's ceiling. There is deliberately no ``raise_autonomy``:
        a specialist cannot lift its own ceiling, and neither can a caller."""
        return self.get(specialist_id).default_autonomy

    def capability_allowed(self, specialist_id: SpecialistId,
                           category: ToolCategory) -> bool:
        return self.get(specialist_id).capability_allowed(category)

    def handoff_allowed(self, source: SpecialistId, target: SpecialistId) -> bool:
        if source == target:
            return False
        return self.get(source).may_handoff_to(target)

    def requires_scope(self, specialist_id: SpecialistId) -> bool:
        return self.get(specialist_id).requires_security_scope

    def runtime_role(self, specialist_id: SpecialistId) -> SpecialistRole:
        return self.get(specialist_id).runtime_role

    def to_dict(self) -> dict:
        return {"specialists": [r.to_dict() for r in self.all()], "count": len(self)}


#: The one registry. Import it; do not construct a second one — every
#: enforcement point reads this instance, so a private copy grants nothing.
REGISTRY = SpecialistRegistry()


def registry() -> SpecialistRegistry:
    return REGISTRY


def record(specialist_id: SpecialistId) -> SpecialistRecord:
    return REGISTRY.get(specialist_id)
