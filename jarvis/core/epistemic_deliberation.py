"""
core/epistemic_deliberation.py — V69 M66A: the one epistemic decision plane.

M66A does not add a second brain. It adds the missing DIMENSIONS to the one
per-turn decision the runtime already computes, and it makes one rule true of
all of them: **within a turn, policy may only strengthen.**

Everything here is a pure function of typed facts already resolved elsewhere —
``TaskDecision`` (domain / role / complexity / security), the deterministic
``TurnPolicy`` (request class / verify matrix / vault gate), and, when it has
run, the deterministic ``MeshRoute`` (owner / autonomy ceiling / effectful /
offensive intent / target). No model is called here, nothing is fetched, and
nothing is persisted. The output, :class:`EpistemicDecision`, is carried on the
canonical ``TaskDecision`` and read by ``chat_stream`` at exactly the points
where the turn would otherwise present a claim it has not earned.

WHAT M66A OWNS, AND WHAT IT REUSES
==================================
It OWNS the composition and these new typed axes: intent, ambiguity, freshness,
premise state, deliberation mode, tool need, verification status, delivery
policy, success-claim permission, and the answer contract. It REUSES, unchanged:

  * ``skill_profiles.EvidencePolicy`` / ``VerificationPolicy`` — the lattices.
  * ``task_domain.TaskDomain`` / ``model_router.ModelRole`` — subject and role.
  * ``injection_firewall.TrustOrigin`` — who may influence control.
  * ``mesh_contracts.Provenance`` / ``ClaimStatus`` / ``Verdict`` — evidence.
  * ``effect_journal.ExternalOutcome`` — M65D's effect truth, never rewritten.
  * ``verification.VerificationResult`` — the model verifier's own verdict.

THE RATCHET
===========
:class:`PolicyRatchet` is the structural guarantee. It has a method that RAISES a
policy and no method that lowers one. A proposal below the current level is a
recorded no-op; a proposal from untrusted content that would lower a level is
recorded as a blocked downgrade. Monotonicity is therefore a property of the
type, not a convention every caller must remember (mirroring
``RoleSelection.grants_authority`` and ``VerifierVerdict.grants_authority``).

Body-safe by construction: every field names an enum, a bounded reason code or a
count. There is no prompt text, no completion, no secret, and no unbounded label
anywhere in this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from core.injection_firewall import TrustOrigin
from core.response_surface import ResponseSurface
from core.skill_profiles import EvidencePolicy, VerificationPolicy
from core.task_domain import TaskDomain

# ── bounds. Nothing here grows unbounded. ─────────────────────────────────────
MAX_TRACE = 24
MAX_REASON = 64
MAX_ASSUMPTIONS = 4
MAX_QUESTION = 240
MAX_CORRECTION_PASSES = 1


# ══════════════════════════════════════════════════════════════════════════════
#  Turn phase state machine (§6)
# ══════════════════════════════════════════════════════════════════════════════
class TurnPhase(str, Enum):
    """The smallest useful typed phase model. Not every turn visits every phase;
    the legal transitions below say which sequences exist at all."""

    INTAKE = "intake"
    GROUNDING = "grounding"
    PLANNING = "planning"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    DELIVERY = "delivery"
    COMPLETE = "complete"


#: The DAG of legal phase transitions. It is a forward-only order with skips
#: allowed (a greeting goes INTAKE -> DELIVERY), so a turn can never revisit an
#: earlier phase and a phase log can never loop.
_PHASE_ORDER: dict[TurnPhase, int] = {
    TurnPhase.INTAKE: 0, TurnPhase.GROUNDING: 1, TurnPhase.PLANNING: 2,
    TurnPhase.EXECUTION: 3, TurnPhase.VERIFICATION: 4, TurnPhase.DELIVERY: 5,
    TurnPhase.COMPLETE: 6,
}


class PhaseMachine:
    """Bounded, observable, forward-only phase tracker (§6, §35).

    A transition to an earlier or equal phase is refused, so a turn's phase log
    is a strictly increasing sequence with a hard length cap. There is no
    free-form thought log: only the phases visited and a bounded reason per
    step."""

    def __init__(self) -> None:
        self._phase = TurnPhase.INTAKE
        self._log: list[tuple[str, str]] = [(TurnPhase.INTAKE.value, "turn opened")]

    @property
    def phase(self) -> TurnPhase:
        return self._phase

    def advance(self, to: TurnPhase, reason: str = "") -> bool:
        """Move forward to *to*. Returns False (and records nothing) if *to* is
        not strictly ahead of the current phase — a phase cannot be revisited."""
        if _PHASE_ORDER[to] <= _PHASE_ORDER[self._phase]:
            return False
        self._phase = to
        if len(self._log) < MAX_TRACE:
            self._log.append((to.value, (reason or "")[:MAX_REASON]))
        return True

    def log(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._log)


# ══════════════════════════════════════════════════════════════════════════════
#  Intent (§9) — WHAT OUTCOME the operator wants
# ══════════════════════════════════════════════════════════════════════════════
class IntentConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IntentGoal(str, Enum):
    """The outcome shape, distinct from the subject (TaskDomain) and the
    interaction class (TurnPolicy.request_class)."""

    CONVERSE = "converse"
    EXPLAIN = "explain"
    MODIFY_CODE = "modify_code"
    DESIGN = "design"
    RESEARCH = "research"
    DIAGNOSE = "diagnose"
    TRANSLATE = "translate"
    SUMMARIZE = "summarize"
    RECALL = "recall"
    STATUS = "status"
    EXECUTE_EFFECT = "execute_effect"
    SECURITY_ANALYSIS = "security_analysis"


@dataclass(frozen=True)
class IntentFrame:
    """WHAT the operator wants — deterministic, never a calibrated probability."""

    goal: IntentGoal
    deliverable: str                      # bounded code, not prose
    effect_requested: bool
    target: str = ""
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    confidence: IntentConfidence = IntentConfidence.MEDIUM

    def to_dict(self) -> dict:
        return {
            "goal": self.goal.value, "deliverable": self.deliverable[:MAX_REASON],
            "effect_requested": self.effect_requested, "target": self.target[:MAX_REASON],
            "constraints": [c[:MAX_REASON] for c in self.constraints[:MAX_ASSUMPTIONS]],
            "assumptions": [a[:MAX_REASON] for a in self.assumptions[:MAX_ASSUMPTIONS]],
            "confidence": self.confidence.value,
        }


# request-class -> (goal, deliverable). Deterministic, no model.
_REQUEST_GOAL: dict[str, tuple[IntentGoal, str]] = {
    "ordinary_conversation": (IntentGoal.CONVERSE, "a direct reply"),
    "general_educational": (IntentGoal.EXPLAIN, "an explanation"),
    "coding_explanation": (IntentGoal.EXPLAIN, "a code explanation"),
    "private_document": (IntentGoal.RESEARCH, "an answer grounded in the operator's documents"),
    "memory_recall": (IntentGoal.RECALL, "a recollection of prior context"),
    "operational_status": (IntentGoal.STATUS, "a current status report"),
    "current_time": (IntentGoal.STATUS, "the current time/date"),
    "effectful_tool": (IntentGoal.EXECUTE_EFFECT, "a world-effect action"),
    "cyber_sensitive": (IntentGoal.SECURITY_ANALYSIS, "a security analysis"),
}
# domain refinements when the request class is generic.
_DOMAIN_GOAL: dict[TaskDomain, tuple[IntentGoal, str]] = {
    TaskDomain.CODER: (IntentGoal.MODIFY_CODE, "a code change"),
    TaskDomain.ARCHITECT: (IntentGoal.DESIGN, "an architecture/design"),
    TaskDomain.RESEARCH: (IntentGoal.RESEARCH, "an evidence-backed synthesis"),
    TaskDomain.DFIR: (IntentGoal.DIAGNOSE, "an incident analysis"),
    TaskDomain.LANGUAGE: (IntentGoal.TRANSLATE, "a language transformation"),
    TaskDomain.MATHEMATICS: (IntentGoal.EXPLAIN, "a worked solution"),
}
_MODIFY_MARKERS = ("fix", "refactor", "change", "modify", "implement", "add ",
                   "rewrite", "patch", "arregla", "cambia", "modifica", "implementa")
#: Code-context tokens that make a modify marker mean "change CODE" rather than
#: "change my schedule". A modify marker is only promoted to MODIFY_CODE when one
#: of these is present, so "add two numbers" is not mistaken for a code change.
_CODE_CONTEXT_TOKENS = ("bug", "code", "función", "funcion", "function", "class ",
                        "method", "método", "metodo", "refactor", "api", "regex",
                        "traceback", "stacktrace", "endpoint", "módulo", "modulo",
                        "module", ".py", ".js", ".ts", "test", "compile")
_PRESERVE_MARKERS = ("preserve the api", "preserve the public api", "without breaking",
                     "keep the api", "backwards compatible", "backward compatible",
                     "no rompas", "sin romper")
_SUMMARIZE_MARKERS = ("summarize", "summary", "tl;dr", "resume", "resumen", "resumir")
#: Lightweight target extraction: a filename-like token or an explicit "named X".
_FILENAME_RE = re.compile(r"\b([\w./-]+\.[a-z0-9]{1,4})\b", re.IGNORECASE)
_NAMED_RE = re.compile(r"\bnamed\s+([\w./-]+)|\bllamad[oa]\s+([\w./-]+)", re.IGNORECASE)
_DIAGNOSE_MARKERS = ("diagnose", "diagnostica", "troubleshoot", "por qué no",
                     "porque no", "why does", "why is", "why won't", "why wont",
                     "will not start", "won't start", "wont start", "is failing",
                     "keeps failing", "returns none", "throws", "no arranca",
                     "no inicia", "falla")


def _looks_diagnostic(text: str) -> bool:
    return any(m in text for m in _DIAGNOSE_MARKERS)


def extract_intent(user_message: str, *, task_decision, turn_policy=None,
                   mesh_route=None) -> IntentFrame:
    """Deterministic intent extraction. NEVER a model call (§10).

    A greeting yields CONVERSE without any model-assisted step; the runtime that
    calls this is on the fast path and must not pay for an intent model.
    """
    text = (user_message or "").lower()
    domain = getattr(task_decision, "domain", TaskDomain.GENERAL)
    request_class = getattr(getattr(turn_policy, "request_class", None), "value", "")

    goal, deliverable = _REQUEST_GOAL.get(
        request_class, (IntentGoal.CONVERSE, "a direct reply"))
    # A generic interaction class defers to the subject: "refactor this" is a
    # code MODIFICATION, not merely a coding explanation.
    if goal in (IntentGoal.CONVERSE, IntentGoal.EXPLAIN) and domain in _DOMAIN_GOAL:
        d_goal, d_deliverable = _DOMAIN_GOAL[domain]
        if d_goal is IntentGoal.MODIFY_CODE and not any(m in text for m in _MODIFY_MARKERS):
            pass  # a coding QUESTION stays EXPLAIN
        else:
            goal, deliverable = d_goal, d_deliverable
    # A modify request that names code, even when the domain classifier missed
    # it ("fix this bug"), is a code change — not conversation (§9 example).
    if goal is IntentGoal.CONVERSE and any(m in text for m in _MODIFY_MARKERS) \
            and any(t in text for t in _CODE_CONTEXT_TOKENS):
        goal, deliverable = IntentGoal.MODIFY_CODE, "a code change"
    # A diagnostic question ("diagnose why X won't start") is an analysis, not
    # conversation — even when the domain classifier saw no keyword.
    if goal is IntentGoal.CONVERSE and _looks_diagnostic(text):
        goal, deliverable = IntentGoal.DIAGNOSE, "a diagnosis"
    if any(m in text for m in _SUMMARIZE_MARKERS):
        goal, deliverable = IntentGoal.SUMMARIZE, "a summary"

    effect_requested = bool(
        getattr(mesh_route, "effectful", False)
        or request_class == "effectful_tool"
        or goal is IntentGoal.EXECUTE_EFFECT)

    target = ""
    if mesh_route is not None:
        scope = getattr(mesh_route, "target_scope", ())
        if scope:
            target = str(scope[0])[:MAX_REASON]
    if not target:
        m_named = _NAMED_RE.search(user_message or "")
        if m_named:
            target = (m_named.group(1) or m_named.group(2) or "")[:MAX_REASON]
        else:
            m_file = _FILENAME_RE.search(user_message or "")
            if m_file:
                target = m_file.group(1)[:MAX_REASON]

    constraints: list[str] = []
    if any(m in text for m in _PRESERVE_MARKERS):
        constraints.append("preserve the public API")

    # Confidence is a bounded label, never a fake probability (§9).
    conf = getattr(task_decision, "domain_confidence", 0.4)
    signalled = bool(getattr(mesh_route, "authorization_signalled", False)) or bool(constraints)
    if conf >= 0.7 or request_class in ("current_time", "effectful_tool") or signalled:
        confidence = IntentConfidence.HIGH
    elif conf >= 0.45:
        confidence = IntentConfidence.MEDIUM
    else:
        confidence = IntentConfidence.LOW

    return IntentFrame(
        goal=goal, deliverable=deliverable, effect_requested=effect_requested,
        target=target, constraints=tuple(constraints[:MAX_ASSUMPTIONS]),
        confidence=confidence)


# ══════════════════════════════════════════════════════════════════════════════
#  Ambiguity (§11)
# ══════════════════════════════════════════════════════════════════════════════
class AmbiguityDisposition(str, Enum):
    PROCEED = "proceed"
    PROCEED_WITH_ASSUMPTION = "proceed_with_assumption"
    CLARIFICATION_REQUIRED = "clarification_required"


@dataclass(frozen=True)
class AmbiguityAssessment:
    disposition: AmbiguityDisposition
    question: str = ""          # the SMALLEST blocking question, or ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {"disposition": self.disposition.value,
                "question": self.question[:MAX_QUESTION], "reason": self.reason[:MAX_REASON]}


def assess_ambiguity(*, intent: IntentFrame, task_decision, mesh_route=None
                     ) -> AmbiguityAssessment:
    """Typed ambiguity disposition (§11, §42).

    Clarification is reserved for ambiguity that touches security scope, a
    target, a destructive/irreversible/external effect, or critical semantics.
    Harmless ambiguity proceeds with ONE bounded assumption; a clear request
    proceeds. The question, when one is required, is the SMALLEST blocking one.
    """
    offensive = bool(getattr(mesh_route, "offensive_intent", False))
    authorized_shape = bool(getattr(mesh_route, "authorization_signalled", False)
                            and getattr(mesh_route, "target_scope", ()))
    security_sensitive = bool(getattr(task_decision, "security_sensitive", False))

    # A clear, low-stakes conversational turn is never ambiguous.
    if intent.goal is IntentGoal.CONVERSE and not intent.effect_requested \
            and not offensive and not security_sensitive:
        return AmbiguityAssessment(AmbiguityDisposition.PROCEED, reason="ordinary_turn")

    # Active security with no authorization signal + named target: the one place
    # a wrong guess is unacceptable. Ask which target the authorization covers.
    if offensive and not authorized_shape:
        return AmbiguityAssessment(
            AmbiguityDisposition.CLARIFICATION_REQUIRED,
            question="Which host is the authorized target, and what activity does "
                     "your authorization cover?",
            reason="active_security_no_scope")

    # An irreversible/destructive effect requested with no target named.
    if intent.effect_requested and not intent.target and _looks_destructive(intent):
        return AmbiguityAssessment(
            AmbiguityDisposition.CLARIFICATION_REQUIRED,
            question="Which exact target should this action run against?",
            reason="destructive_effect_no_target")

    # Low interpretation confidence on a non-trivial request: one assumption,
    # stated, rather than a question — unless it is effectful or security work.
    if intent.confidence is IntentConfidence.LOW:
        if intent.effect_requested or security_sensitive:
            return AmbiguityAssessment(
                AmbiguityDisposition.CLARIFICATION_REQUIRED,
                question="What outcome do you want — an explanation, or a change to "
                         "something that is running?",
                reason="low_confidence_high_stakes")
        return AmbiguityAssessment(
            AmbiguityDisposition.PROCEED_WITH_ASSUMPTION,
            reason="low_confidence_low_stakes")

    return AmbiguityAssessment(AmbiguityDisposition.PROCEED, reason="clear_request")


_DESTRUCTIVE_GOALS = frozenset({IntentGoal.EXECUTE_EFFECT})
_DESTRUCTIVE_MARKERS = ("delete", "kill", "drop ", "remove", "wipe", "format ",
                        "borra", "elimina", "destruye", "rm -")


def _looks_destructive(intent: IntentFrame) -> bool:
    if intent.goal in _DESTRUCTIVE_GOALS:
        return True
    d = intent.deliverable.lower()
    return any(m in d for m in _DESTRUCTIVE_MARKERS)


# ══════════════════════════════════════════════════════════════════════════════
#  Freshness (§14)
# ══════════════════════════════════════════════════════════════════════════════
class FreshnessRequirement(str, Enum):
    STABLE = "stable"
    FRESHNESS_PREFERRED = "freshness_preferred"
    FRESHNESS_REQUIRED = "freshness_required"


_FRESHNESS_ORDER: dict[FreshnessRequirement, int] = {
    FreshnessRequirement.STABLE: 0, FreshnessRequirement.FRESHNESS_PREFERRED: 1,
    FreshnessRequirement.FRESHNESS_REQUIRED: 2,
}


@dataclass(frozen=True)
class FreshnessStatus:
    """Whether current state was actually observed this turn."""

    requirement: FreshnessRequirement
    satisfied: bool = False
    source: str = ""            # bounded: "repository" / "tool" / "world_state" / ""

    @property
    def blocks_current_claim(self) -> bool:
        """True when the turn requires current knowledge it does not have."""
        return (self.requirement is FreshnessRequirement.FRESHNESS_REQUIRED
                and not self.satisfied)

    def to_dict(self) -> dict:
        return {"requirement": self.requirement.value, "satisfied": self.satisfied,
                "source": self.source[:MAX_REASON],
                "blocks_current_claim": self.blocks_current_claim}


# Terms that make a truth materially time-sensitive (EN + ES). Freshness is NOT
# web-only: a repository/host/API observation satisfies it just as well (§14).
_FRESHNESS_REQUIRED_MARKERS = (
    "latest", "current", "currently", "right now", "today", "as of now",
    "up to date", "up-to-date", "newest", "most recent", "this version",
    "current branch", "current head", "running", "live ", "real-time", "real time",
    "actual", "actualmente", "ahora mismo", "hoy", "más reciente", "mas reciente",
    "última versión", "ultima version", "en ejecución", "en ejecucion",
)
_FRESHNESS_REQUIRED_STRONG = (
    "latest version", "current version", "newest version", "most recent version",
    "current branch", "current head", "current commit", "latest release",
    "current price", "current cve", "currently running", "current state",
    "última versión", "ultima version", "versión actual", "version actual",
)
_FRESHNESS_PREFERRED_MARKERS = (
    "recent", "recently", "update", "new ", "status of", "state of",
    "reciente", "recientemente", "estado de", "novedades",
)


def assess_freshness(user_message: str, *, task_decision, mesh_route=None
                     ) -> FreshnessRequirement:
    """Classify how time-sensitive the answer's truth is (§14). Deterministic."""
    text = (user_message or "").lower()
    if any(m in text for m in _FRESHNESS_REQUIRED_STRONG):
        return FreshnessRequirement.FRESHNESS_REQUIRED
    if any(m in text for m in _FRESHNESS_REQUIRED_MARKERS):
        return FreshnessRequirement.FRESHNESS_REQUIRED
    if any(m in text for m in _FRESHNESS_PREFERRED_MARKERS):
        return FreshnessRequirement.FRESHNESS_PREFERRED
    return FreshnessRequirement.STABLE


# ══════════════════════════════════════════════════════════════════════════════
#  Premise epistemics (§12) — a MAPPING over the evidence types, not a new store
# ══════════════════════════════════════════════════════════════════════════════
class PremiseState(str, Enum):
    OBSERVED = "observed"
    SUPPORTED = "supported"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"
    CONTRADICTED = "contradicted"


def premise_from_claim(claim_status, *, has_corroboration: bool) -> PremiseState:
    """Map a ``mesh_contracts.ClaimStatus`` (+ whether it rests on corroborating
    evidence) onto a premise state. Reuses the EvidenceGraph's own semantics; it
    does not re-adjudicate."""
    name = getattr(claim_status, "value", str(claim_status)).lower()
    if name == "observed":
        return PremiseState.OBSERVED
    if name == "verified":
        return PremiseState.SUPPORTED if has_corroboration else PremiseState.INFERRED
    if name == "inferred":
        return PremiseState.INFERRED
    if name == "disputed":
        return PremiseState.CONTRADICTED
    if name == "rejected":
        return PremiseState.CONTRADICTED
    return PremiseState.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
#  Evidence quality over provenance (§17) — trust != quality (§16)
# ══════════════════════════════════════════════════════════════════════════════
#: How reliable a piece of evidence is AS A FACT, ranked by provenance. This is
#: a DIFFERENT axis from TrustOrigin (which decides control influence): a tool
#: result may be forbidden from granting authority yet be a strong observation,
#: and a trusted memory item may be permitted context yet be a weak, stale fact.
_EVIDENCE_QUALITY: dict[str, int] = {
    "tool_result": 5, "world_state": 5, "telemetry": 4, "operator": 4,
    "document": 3, "external_report": 2, "model_asserted": 0,
}


def evidence_quality(provenance) -> int:
    """A 0-5 factual-reliability rank for a ``mesh_contracts.Provenance``.

    Direct/current observation outranks a secondary report, which outranks an
    uncorroborated model prior. Used to adjudicate conflicting evidence by
    quality rather than by model confidence (§17, §18)."""
    name = getattr(provenance, "value", str(provenance)).lower()
    return _EVIDENCE_QUALITY.get(name, 1)


def evidence_beats_prior(observation_provenance, prior_provenance="model_asserted") -> bool:
    """Load-bearing invariant (§18): world/repository/tool evidence outranks an
    unsupported model prior. Returns True when the observation wins."""
    return evidence_quality(observation_provenance) > evidence_quality(prior_provenance)


# ══════════════════════════════════════════════════════════════════════════════
#  Deliberation mode (§20) — planning is NOT team (§21)
# ══════════════════════════════════════════════════════════════════════════════
class DeliberationMode(str, Enum):
    DIRECT = "direct"
    SINGLE_ANALYSIS = "single_analysis"
    PLAN_SINGLE = "plan_single"
    TEAM = "team"


_MODE_ORDER: dict[DeliberationMode, int] = {
    DeliberationMode.DIRECT: 0, DeliberationMode.SINGLE_ANALYSIS: 1,
    DeliberationMode.PLAN_SINGLE: 2, DeliberationMode.TEAM: 3,
}

# Domains that are genuinely cross-domain / benefit from independent roles.
_TEAM_DOMAINS: frozenset[TaskDomain] = frozenset({
    TaskDomain.RESEARCH, TaskDomain.DFIR, TaskDomain.CYBER_PURPLE,
})
# Domains/goals that benefit from decomposition but NOT from multiple agents.
_PLAN_DOMAINS: frozenset[TaskDomain] = frozenset({
    TaskDomain.ARCHITECT, TaskDomain.PLANNER,
})


def choose_deliberation_mode(*, task_decision, intent: IntentFrame,
                             mesh_route=None) -> DeliberationMode:
    """The closed deliberation mode (§20). PLANNING DOES NOT IMPLY TEAM (§21).

    A substantial code change or an architecture task is PLAN_SINGLE — it needs
    decomposition, not several agents. TEAM is reserved for genuinely
    cross-domain work (research synthesis, DFIR, purple) where independent roles
    clearly improve quality.
    """
    domain = getattr(task_decision, "domain", TaskDomain.GENERAL)
    complexity = float(getattr(task_decision, "complexity", 0.0))
    security_sensitive = bool(getattr(task_decision, "security_sensitive", False))

    # TEAM: cross-domain value must be real. A single planning need never reaches
    # here, which is the whole point of separating this from PLAN_SINGLE.
    if domain in _TEAM_DOMAINS and (complexity >= 0.6 or security_sensitive
                                    or intent.goal in (IntentGoal.RESEARCH,
                                                       IntentGoal.DIAGNOSE)):
        return DeliberationMode.TEAM

    # PLAN_SINGLE: decomposition helps, one worker suffices.
    if domain in _PLAN_DOMAINS:
        return DeliberationMode.PLAN_SINGLE
    if intent.goal in (IntentGoal.MODIFY_CODE, IntentGoal.DESIGN):
        return DeliberationMode.PLAN_SINGLE
    if complexity >= 0.6:
        return DeliberationMode.PLAN_SINGLE

    # SINGLE_ANALYSIS: explanation, mathematics, comparison, moderate diagnosis.
    if intent.goal in (IntentGoal.EXPLAIN, IntentGoal.DIAGNOSE,
                       IntentGoal.SECURITY_ANALYSIS, IntentGoal.RESEARCH,
                       IntentGoal.SUMMARIZE):
        return DeliberationMode.SINGLE_ANALYSIS
    if domain is TaskDomain.MATHEMATICS:
        return DeliberationMode.SINGLE_ANALYSIS

    return DeliberationMode.DIRECT


# ══════════════════════════════════════════════════════════════════════════════
#  Tool need (§19) — availability != need != execution
# ══════════════════════════════════════════════════════════════════════════════
class ToolNeed(str, Enum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


_TOOL_NEED_ORDER: dict[ToolNeed, int] = {
    ToolNeed.NONE: 0, ToolNeed.OPTIONAL: 1, ToolNeed.REQUIRED: 2,
}


class ToolExecutionState(str, Enum):
    """What actually happened to a tool this turn (§19). Never inferred from a
    tool merely being registered."""

    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    INDETERMINATE = "indeterminate"


def assess_tool_need(*, intent: IntentFrame, freshness: FreshnessRequirement,
                     task_decision) -> ToolNeed:
    """Whether the turn NEEDS a tool — derived from intent/freshness/domain, and
    NEVER from the mere presence of a tool in the runtime (§19)."""
    if intent.effect_requested:
        return ToolNeed.REQUIRED
    if freshness is FreshnessRequirement.FRESHNESS_REQUIRED:
        return ToolNeed.REQUIRED
    if intent.goal in (IntentGoal.STATUS, IntentGoal.RECALL):
        return ToolNeed.REQUIRED
    domain = getattr(task_decision, "domain", TaskDomain.GENERAL)
    if intent.goal is IntentGoal.RESEARCH or domain is TaskDomain.RESEARCH:
        return ToolNeed.OPTIONAL
    if freshness is FreshnessRequirement.FRESHNESS_PREFERRED:
        return ToolNeed.OPTIONAL
    return ToolNeed.NONE


# ══════════════════════════════════════════════════════════════════════════════
#  Lattices (§15, §25) — monotone composition only
# ══════════════════════════════════════════════════════════════════════════════
_EVIDENCE_ORDER: dict[EvidencePolicy, int] = {
    EvidencePolicy.NONE: 0, EvidencePolicy.RECOMMENDED: 1,
    EvidencePolicy.REQUIRED: 2, EvidencePolicy.REQUIRED_AUTHORITATIVE: 3,
}
_VERIFY_ORDER: dict[VerificationPolicy, int] = {
    VerificationPolicy.NONE: 0, VerificationPolicy.OPTIONAL: 1,
    VerificationPolicy.REQUIRED: 2, VerificationPolicy.REQUIRED_FAIL_CLOSED: 3,
}


def _stronger(a, b, order):
    return a if order[a] >= order[b] else b


# ── the M54.6 verify matrix -> the M66A verification lattice floor ────────────
_TURN_VERIFY_FLOOR: dict[str, VerificationPolicy] = {
    "SKIP_LLM_VERIFIER": VerificationPolicy.NONE,
    "DETERMINISTIC_CHECKS_ONLY": VerificationPolicy.OPTIONAL,
    "GROUNDING_CHECK": VerificationPolicy.REQUIRED,
    "EVIDENCE_REFERENCE_CHECK": VerificationPolicy.REQUIRED,
    "BOUNDED_MODEL_VERIFIER": VerificationPolicy.REQUIRED,
    "FULL_VERIFICATION": VerificationPolicy.REQUIRED_FAIL_CLOSED,
}
class RatchetField(str, Enum):
    EVIDENCE = "evidence"
    VERIFICATION = "verification"
    FRESHNESS = "freshness"
    TOOL_NEED = "tool_need"
    DELIBERATION = "deliberation"


_FIELD_ORDER = {
    RatchetField.EVIDENCE: _EVIDENCE_ORDER,
    RatchetField.VERIFICATION: _VERIFY_ORDER,
    RatchetField.FRESHNESS: _FRESHNESS_ORDER,
    RatchetField.TOOL_NEED: _TOOL_NEED_ORDER,
    RatchetField.DELIBERATION: _MODE_ORDER,
}


class PolicyRatchet:
    """Monotone policy accumulator (§7, §35, §37).

    There is a method that RAISES a policy and no method that lowers one. A
    proposal at or below the current level is a recorded no-op; a proposal that
    would lower a level and comes from content that is not the operator or the
    system prompt is recorded as a BLOCKED downgrade. Because there is no
    lowering method at all, no caller — trusted or not — can weaken policy within
    a turn; untrusted content additionally cannot even strengthen it.
    """

    def __init__(self, **initial) -> None:
        self._levels: dict[RatchetField, object] = {}
        self._trace: list[tuple[str, str, str, str]] = []  # field, prev, new, reason
        self.strengthen_events = 0
        self.downgrade_attempts_blocked = 0
        for field_name, value in initial.items():
            self._levels[RatchetField(field_name)] = value

    def get(self, field: RatchetField):
        return self._levels.get(field)

    def consider(self, field: RatchetField, proposed, *, reason: str = "",
                 origin: TrustOrigin = TrustOrigin.OPERATOR_INPUT) -> None:
        """Consider raising *field* to *proposed*. Never lowers.

        Untrusted content (any origin other than OPERATOR_INPUT / TRUSTED_SYSTEM)
        may not strengthen either — its proposal is ignored entirely — so a tool
        result or web page cannot drive the ratchet in any direction.
        """
        order = _FIELD_ORDER[field]
        current = self._levels.get(field)
        # Only the operator/system may drive policy from content (§33/§34).
        if origin not in (TrustOrigin.OPERATOR_INPUT, TrustOrigin.TRUSTED_SYSTEM):
            if current is not None and order[proposed] < order[current]:
                self.downgrade_attempts_blocked += 1
                self._record(field, current, current, f"blocked_untrusted_downgrade:{reason}")
            return
        if current is None:
            self._levels[field] = proposed
            self._record(field, "unset", proposed, reason)
            return
        if order[proposed] > order[current]:
            self._levels[field] = proposed
            self.strengthen_events += 1
            self._record(field, current, proposed, reason)
        elif order[proposed] < order[current]:
            # A same-turn proposal to weaken is refused and recorded, never applied.
            self.downgrade_attempts_blocked += 1
            self._record(field, current, current, f"blocked_downgrade:{reason}")

    def _record(self, field, prev, new, reason) -> None:
        if len(self._trace) < MAX_TRACE:
            self._trace.append((
                field.value if isinstance(field, RatchetField) else str(field),
                getattr(prev, "value", str(prev)), getattr(new, "value", str(new)),
                (reason or "")[:MAX_REASON]))

    def trace(self) -> tuple[tuple[str, str, str, str], ...]:
        return tuple(self._trace)


# ══════════════════════════════════════════════════════════════════════════════
#  Verification status (§26) and delivery (§27, §28)
# ══════════════════════════════════════════════════════════════════════════════
class VerificationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    VERIFIED = "verified"
    VERIFIED_WITH_LIMITATIONS = "verified_with_limitations"
    NOT_VERIFIED = "not_verified"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    FALLBACK_AUDITED = "fallback_audited"
    BLOCKED = "blocked"


#: Statuses under which a substantive conclusion may be stated as established.
PASSING_STATUS: frozenset[VerificationStatus] = frozenset({
    VerificationStatus.NOT_REQUIRED, VerificationStatus.VERIFIED,
    VerificationStatus.VERIFIED_WITH_LIMITATIONS, VerificationStatus.FALLBACK_AUDITED,
})


def status_from_argus(verdict) -> VerificationStatus:
    """Map a ``mesh_contracts.Verdict`` onto an explicit status (§26). NEVER
    inferred from string equality of draft vs final."""
    name = getattr(verdict, "value", str(verdict)).lower()
    if name == "verified":
        return VerificationStatus.VERIFIED
    if name == "verified_with_limitations":
        return VerificationStatus.VERIFIED_WITH_LIMITATIONS
    if name in ("scope_violation", "authority_missing", "failed"):
        return VerificationStatus.BLOCKED
    return VerificationStatus.NOT_VERIFIED


def status_from_result(result) -> VerificationStatus:
    """Map a ``verification.VerificationResult`` onto an explicit status (§26)."""
    if result is None:
        return VerificationStatus.NOT_REQUIRED
    if getattr(result, "verified", False):
        return VerificationStatus.VERIFIED
    if getattr(result, "needs_human_review", False):
        return VerificationStatus.HUMAN_REVIEW_REQUIRED
    return VerificationStatus.NOT_VERIFIED


class DeliveryPolicy(str, Enum):
    STREAM_DIRECT = "stream_direct"
    STAGED_VERIFY = "staged_verify"
    BUFFER_UNTIL_VERIFIED = "buffer_until_verified"


def choose_delivery(verification_policy: VerificationPolicy) -> DeliveryPolicy:
    """Delivery policy from the (composed) verification policy (§27, §28).

    REQUIRED_FAIL_CLOSED buffers: substantive output waits for the verdict.
    REQUIRED/OPTIONAL stream and stage the verdict as a suffix (the established
    M64.1 contract). NONE streams directly.
    """
    if verification_policy is VerificationPolicy.REQUIRED_FAIL_CLOSED:
        return DeliveryPolicy.BUFFER_UNTIL_VERIFIED
    if verification_policy in (VerificationPolicy.REQUIRED, VerificationPolicy.OPTIONAL):
        return DeliveryPolicy.STAGED_VERIFY
    return DeliveryPolicy.STREAM_DIRECT


# ══════════════════════════════════════════════════════════════════════════════
#  Effect truth (§30) — M65D is authoritative and never rewritten
# ══════════════════════════════════════════════════════════════════════════════
class EffectTruth(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    PROVEN_COMMITTED = "proven_committed"
    PROVEN_NOT_EXECUTED = "proven_not_executed"
    INDETERMINATE = "indeterminate"


def effect_truth_from_outcome(external_outcome) -> EffectTruth:
    """Map an ``effect_journal.ExternalOutcome`` (or its string) onto EffectTruth.
    UNKNOWN maps to INDETERMINATE and is NEVER rounded to a certainty (§30)."""
    if external_outcome is None:
        return EffectTruth.NOT_APPLICABLE
    name = getattr(external_outcome, "value", str(external_outcome)).upper()
    if name == "PROVEN_COMMITTED":
        return EffectTruth.PROVEN_COMMITTED
    if name == "PROVEN_NOT_EXECUTED":
        return EffectTruth.PROVEN_NOT_EXECUTED
    return EffectTruth.INDETERMINATE


def aggregate_effect_truth(outcomes) -> EffectTruth:
    """The turn's overall effect truth. INDETERMINATE dominates: if any effect's
    outcome is unknown, the turn's effect truth is INDETERMINATE (§30)."""
    truths = [effect_truth_from_outcome(o) for o in (outcomes or [])]
    truths = [t for t in truths if t is not EffectTruth.NOT_APPLICABLE]
    if not truths:
        return EffectTruth.NOT_APPLICABLE
    if any(t is EffectTruth.INDETERMINATE for t in truths):
        return EffectTruth.INDETERMINATE
    if all(t is EffectTruth.PROVEN_NOT_EXECUTED for t in truths):
        return EffectTruth.PROVEN_NOT_EXECUTED
    if all(t is EffectTruth.PROVEN_COMMITTED for t in truths):
        return EffectTruth.PROVEN_COMMITTED
    return EffectTruth.INDETERMINATE


# ══════════════════════════════════════════════════════════════════════════════
#  Success-claim gate (§29)
# ══════════════════════════════════════════════════════════════════════════════
# Bounded lexical detection of success/completion vocabulary (EN + ES). The gate
# does not rewrite prose blindly; it detects an unearned claim and appends a
# correcting status line so the operator reads the honest state.
_SUCCESS_TERMS = (
    "fixed", "resolved", "passed", "verified", "completed", "done",
    "deployed", "applied", "successful", "succeeded", "secure", "confirmed",
    "it works", "working now", "all good", "up and running",
    "arreglado", "resuelto", "completado", "hecho", "desplegado", "aplicado",
    "verificado", "confirmado", "funcionando", "exitoso", "listo",
)
_SUCCESS_RE = re.compile("|".join(re.escape(t) for t in _SUCCESS_TERMS),
                         re.IGNORECASE)


class SuccessClaimPermission(str, Enum):
    ALLOWED = "allowed"
    QUALIFIED = "qualified"   # may state findings but must caveat certainty
    BLOCKED = "blocked"       # may not claim success at all


def success_claim_permission(*, verification_status: VerificationStatus,
                             effect_truth: EffectTruth,
                             freshness: FreshnessStatus,
                             any_tool_denied: bool = False) -> SuccessClaimPermission:
    """Whether the turn may emit a success/completion claim (§29).

    A claim of success requires that verification did not fail, the effect truth
    is not unknown, current-state claims are not blocked by an unmet freshness
    requirement, and no tool the answer relies on was denied.
    """
    if verification_status in (VerificationStatus.NOT_VERIFIED,
                               VerificationStatus.BLOCKED,
                               VerificationStatus.HUMAN_REVIEW_REQUIRED):
        return SuccessClaimPermission.BLOCKED
    if effect_truth is EffectTruth.INDETERMINATE:
        return SuccessClaimPermission.BLOCKED
    if freshness.blocks_current_claim:
        return SuccessClaimPermission.BLOCKED
    if any_tool_denied:
        return SuccessClaimPermission.QUALIFIED
    if verification_status is VerificationStatus.VERIFIED_WITH_LIMITATIONS:
        return SuccessClaimPermission.QUALIFIED
    return SuccessClaimPermission.ALLOWED


def draft_makes_success_claim(draft: str) -> bool:
    """Whether *draft* contains success/completion vocabulary (bounded lexical)."""
    return bool(_SUCCESS_RE.search(draft or ""))


# ══════════════════════════════════════════════════════════════════════════════
#  Epistemic markers for lossy surfaces (§32)
# ══════════════════════════════════════════════════════════════════════════════
_MARKER: dict[VerificationStatus, str] = {
    VerificationStatus.NOT_VERIFIED: "[UNVERIFIED]",
    VerificationStatus.HUMAN_REVIEW_REQUIRED: "[HUMAN REVIEW]",
    VerificationStatus.BLOCKED: "[UNVERIFIED]",
}


def epistemic_marker(*, verification_status: VerificationStatus,
                     effect_truth: EffectTruth,
                     freshness: FreshnessStatus) -> str:
    """The single most important warning that must survive surface compression
    (§32). Effect uncertainty and blocked freshness outrank a soft caveat, and
    a missing marker is the empty string (nothing to warn about)."""
    if effect_truth is EffectTruth.INDETERMINATE:
        return "[UNCERTAIN OUTCOME]"
    if freshness.blocks_current_claim:
        return "[CURRENT STATE NOT VERIFIED]"
    return _MARKER.get(verification_status, "")


# ══════════════════════════════════════════════════════════════════════════════
#  Answer contract (§31)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AnswerContract:
    """Body-safe rendering contract, built once before the answer is shown."""

    surface: ResponseSurface
    freshness: FreshnessStatus
    evidence_policy: EvidencePolicy
    evidence_satisfied: bool
    verification_policy: VerificationPolicy
    verification_status: VerificationStatus
    effect_truth: EffectTruth
    delivery_policy: DeliveryPolicy
    success_permission: SuccessClaimPermission
    uncertainty_required: bool
    citation_required: bool
    marker: str = ""
    clarifying_question: str = ""

    def to_dict(self) -> dict:
        return {
            "surface": self.surface.value,
            "freshness": self.freshness.to_dict(),
            "evidence_policy": self.evidence_policy.value,
            "evidence_satisfied": self.evidence_satisfied,
            "verification_policy": self.verification_policy.value,
            "verification_status": self.verification_status.value,
            "effect_truth": self.effect_truth.value,
            "delivery_policy": self.delivery_policy.value,
            "success_permission": self.success_permission.value,
            "uncertainty_required": self.uncertainty_required,
            "citation_required": self.citation_required,
            "marker": self.marker[:MAX_REASON],
            "clarifying_question": self.clarifying_question[:MAX_QUESTION],
        }


# ══════════════════════════════════════════════════════════════════════════════
#  The composed decision (§4) — carried on the canonical TaskDecision
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EpistemicDecision:
    """The M66A dimensions of one turn. A COMPONENT of TaskDecision, not a rival
    to it. Immutable; a mid-turn strengthening produces a new snapshot via
    :meth:`strengthened`."""

    intent: IntentFrame
    ambiguity: AmbiguityAssessment
    freshness_requirement: FreshnessRequirement
    evidence_policy: EvidencePolicy
    verification_policy: VerificationPolicy
    deliberation_mode: DeliberationMode
    tool_need: ToolNeed
    delivery_policy: DeliveryPolicy
    trace: tuple[tuple[str, str, str, str], ...] = ()
    strengthen_events: int = 0
    downgrade_attempts_blocked: int = 0

    @property
    def buffers_delivery(self) -> bool:
        return self.delivery_policy is DeliveryPolicy.BUFFER_UNTIL_VERIFIED

    def strengthened(self, *, verification_policy: VerificationPolicy | None = None,
                     evidence_policy: EvidencePolicy | None = None,
                     freshness_requirement: FreshnessRequirement | None = None,
                     reason: str = "") -> "EpistemicDecision":
        """Return a new decision with the given fields raised (never lowered).

        A proposal at or below the current level is ignored, so this method can
        never weaken the decision — the ratchet is a property of the type."""
        vp = self.verification_policy
        if verification_policy is not None:
            vp = _stronger(vp, verification_policy, _VERIFY_ORDER)
        ep = self.evidence_policy
        if evidence_policy is not None:
            ep = _stronger(ep, evidence_policy, _EVIDENCE_ORDER)
        fr = self.freshness_requirement
        if freshness_requirement is not None:
            fr = _stronger(fr, freshness_requirement, _FRESHNESS_ORDER)
        moved = (vp, ep, fr) != (self.verification_policy, self.evidence_policy,
                                 self.freshness_requirement)
        if not moved:
            # Nothing rose. Return SELF rather than a new snapshot with an
            # inflated counter: the live turn calls this on every routed turn,
            # so counting a no-op would make `policy_strengthen_events` report
            # movement that never happened, and a telemetry number that
            # overstates is the kind of thing §43 exists to prevent.
            return self
        new_trace = self.trace
        if len(new_trace) < MAX_TRACE:
            new_trace = new_trace + (("strengthened", "", "", (reason or "")[:MAX_REASON]),)
        return replace(self, verification_policy=vp, evidence_policy=ep,
                       freshness_requirement=fr, delivery_policy=choose_delivery(vp),
                       trace=new_trace,
                       strengthen_events=self.strengthen_events + 1)

    def telemetry(self) -> dict:
        """Bounded, body-safe counters and codes (§43)."""
        return {
            "intent_goal": self.intent.goal.value,
            "intent_confidence": self.intent.confidence.value,
            "ambiguity": self.ambiguity.disposition.value,
            "freshness": self.freshness_requirement.value,
            "evidence_policy": self.evidence_policy.value,
            "verification_policy": self.verification_policy.value,
            "deliberation_mode": self.deliberation_mode.value,
            "tool_need": self.tool_need.value,
            "delivery_policy": self.delivery_policy.value,
            "strengthen_events": self.strengthen_events,
            "downgrade_attempts_blocked": self.downgrade_attempts_blocked,
        }


def deliberate(user_message: str, *, task_decision, turn_policy=None,
               mesh_route=None) -> EpistemicDecision:
    """Compose the one epistemic decision (§4, §5). Pure and deterministic.

    Reads the canonical ``TaskDecision``, the deterministic ``TurnPolicy`` and,
    when present, the deterministic ``MeshRoute``. Every load-bearing policy is
    built by RAISING through the ratchet from typed floors, so the result is
    monotone by construction and the same inputs always give the same policy.
    """
    intent = extract_intent(user_message, task_decision=task_decision,
                            turn_policy=turn_policy, mesh_route=mesh_route)
    ambiguity = assess_ambiguity(intent=intent, task_decision=task_decision,
                                 mesh_route=mesh_route)
    freshness_req = assess_freshness(user_message, task_decision=task_decision,
                                     mesh_route=mesh_route)
    mode = choose_deliberation_mode(task_decision=task_decision, intent=intent,
                                    mesh_route=mesh_route)
    tool_need = assess_tool_need(intent=intent, freshness=freshness_req,
                                 task_decision=task_decision)

    ratchet = PolicyRatchet(
        evidence=EvidencePolicy.NONE, verification=VerificationPolicy.NONE,
        freshness=freshness_req, tool_need=tool_need, deliberation=mode)

    # ── verification floor: the M54.6 turn matrix ────────────────────────────
    verify_code = getattr(getattr(turn_policy, "verify_policy", None), "value", "")
    if verify_code in _TURN_VERIFY_FLOOR:
        ratchet.consider(RatchetField.VERIFICATION, _TURN_VERIFY_FLOOR[verify_code],
                         reason=f"turn:{verify_code}")
    # the router's own requires_verification is at least REQUIRED
    if getattr(task_decision, "requires_verification", False):
        ratchet.consider(RatchetField.VERIFICATION, VerificationPolicy.REQUIRED,
                         reason="router_requires_verification")
    # security sensitivity is at least REQUIRED
    if getattr(task_decision, "security_sensitive", False):
        ratchet.consider(RatchetField.VERIFICATION, VerificationPolicy.REQUIRED,
                         reason="security_sensitive")

    # ── evidence floor: intent + freshness + domain ──────────────────────────
    if intent.goal in (IntentGoal.RESEARCH, IntentGoal.DIAGNOSE):
        ratchet.consider(RatchetField.EVIDENCE, EvidencePolicy.RECOMMENDED,
                         reason="research_or_diagnosis")
    if intent.goal is IntentGoal.SECURITY_ANALYSIS:
        ratchet.consider(RatchetField.EVIDENCE, EvidencePolicy.REQUIRED,
                         reason="security_analysis")
    if freshness_req is FreshnessRequirement.FRESHNESS_REQUIRED:
        ratchet.consider(RatchetField.EVIDENCE, EvidencePolicy.REQUIRED,
                         reason="freshness_required_needs_evidence")

    # ── mesh strengthening (deterministic, trusted control) ──────────────────
    if mesh_route is not None:
        if getattr(mesh_route, "verifier_required", False):
            ratchet.consider(RatchetField.VERIFICATION, VerificationPolicy.REQUIRED,
                             reason="mesh_verifier_required")
        if getattr(mesh_route, "effectful", False):
            ratchet.consider(RatchetField.VERIFICATION,
                             VerificationPolicy.REQUIRED_FAIL_CLOSED, reason="mesh_effectful")
        if getattr(mesh_route, "offensive_intent", False):
            ratchet.consider(RatchetField.VERIFICATION,
                             VerificationPolicy.REQUIRED_FAIL_CLOSED, reason="mesh_offensive")
        for req in getattr(mesh_route, "required_evidence", ()) or ():
            # A mesh route that declares required evidence (its own primary's
            # evidence contract, computed deterministically in mesh_router) pulls
            # the turn's evidence policy up to REQUIRED.
            ratchet.consider(RatchetField.EVIDENCE, EvidencePolicy.REQUIRED,
                             reason="mesh_required_evidence")
            break

    verification_policy = ratchet.get(RatchetField.VERIFICATION)
    evidence_policy = ratchet.get(RatchetField.EVIDENCE)

    return EpistemicDecision(
        intent=intent, ambiguity=ambiguity, freshness_requirement=freshness_req,
        evidence_policy=evidence_policy, verification_policy=verification_policy,
        deliberation_mode=mode, tool_need=tool_need,
        delivery_policy=choose_delivery(verification_policy),
        trace=ratchet.trace(), strengthen_events=ratchet.strengthen_events,
        downgrade_attempts_blocked=ratchet.downgrade_attempts_blocked)


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded, body-safe counters (§43)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class DeliberationCounters:
    direct_turns: int = 0
    single_analysis_turns: int = 0
    plan_single_turns: int = 0
    team_turns: int = 0
    clarification_required: int = 0
    proceeded_with_assumption: int = 0
    freshness_required: int = 0
    freshness_unsatisfied: int = 0
    evidence_required: int = 0
    verification_required: int = 0
    verification_failed: int = 0
    fail_closed_delivery: int = 0
    success_claim_blocked: int = 0
    correction_passes: int = 0
    policy_strengthen_events: int = 0
    policy_downgrade_attempts_blocked: int = 0

    def observe(self, decision: EpistemicDecision) -> None:
        m = decision.deliberation_mode
        if m is DeliberationMode.DIRECT:
            self.direct_turns += 1
        elif m is DeliberationMode.SINGLE_ANALYSIS:
            self.single_analysis_turns += 1
        elif m is DeliberationMode.PLAN_SINGLE:
            self.plan_single_turns += 1
        elif m is DeliberationMode.TEAM:
            self.team_turns += 1
        if decision.ambiguity.disposition is AmbiguityDisposition.CLARIFICATION_REQUIRED:
            self.clarification_required += 1
        elif decision.ambiguity.disposition is AmbiguityDisposition.PROCEED_WITH_ASSUMPTION:
            self.proceeded_with_assumption += 1
        if decision.freshness_requirement is FreshnessRequirement.FRESHNESS_REQUIRED:
            self.freshness_required += 1
        if _EVIDENCE_ORDER[decision.evidence_policy] >= _EVIDENCE_ORDER[EvidencePolicy.REQUIRED]:
            self.evidence_required += 1
        if _VERIFY_ORDER[decision.verification_policy] >= _VERIFY_ORDER[VerificationPolicy.REQUIRED]:
            self.verification_required += 1
        if decision.buffers_delivery:
            self.fail_closed_delivery += 1
        self.policy_strengthen_events += decision.strengthen_events
        self.policy_downgrade_attempts_blocked += decision.downgrade_attempts_blocked

    def to_dict(self) -> dict:
        return dict(self.__dict__)


COUNTERS = DeliberationCounters()


__all__ = [
    "TurnPhase", "PhaseMachine",
    "IntentConfidence", "IntentGoal", "IntentFrame", "extract_intent",
    "AmbiguityDisposition", "AmbiguityAssessment", "assess_ambiguity",
    "FreshnessRequirement", "FreshnessStatus", "assess_freshness",
    "PremiseState", "premise_from_claim",
    "evidence_quality", "evidence_beats_prior",
    "DeliberationMode", "choose_deliberation_mode",
    "ToolNeed", "ToolExecutionState", "assess_tool_need",
    "PolicyRatchet", "RatchetField",
    "VerificationStatus", "PASSING_STATUS", "status_from_argus", "status_from_result",
    "DeliveryPolicy", "choose_delivery",
    "EffectTruth", "effect_truth_from_outcome", "aggregate_effect_truth",
    "SuccessClaimPermission", "success_claim_permission", "draft_makes_success_claim",
    "epistemic_marker", "AnswerContract",
    "EpistemicDecision", "deliberate",
    "DeliberationCounters", "COUNTERS",
    "MAX_CORRECTION_PASSES",
]
