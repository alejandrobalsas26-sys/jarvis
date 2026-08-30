"""
core/mesh_contracts.py — V69 M64: the typed shapes specialists speak in.

Everything that crosses a specialist boundary is one of these. Nothing crosses
as prose, and nothing crosses as an untyped blob the orchestrator then trusts.

    SpecialistHandoff   what one specialist asks another to do (§14)
    EvidenceRef         one piece of evidence, with its provenance (§16)
    Claim               one assertion, bound to the evidence that supports it
    EvidenceGraph       the claim -> evidence bindings for one task
    SpecialistResult    what a specialist returns (§50)
    ActionRequest       an effectful proposal, never an act (§51)
    VerifierVerdict     ARGUS's answer (§32)

Three properties are load-bearing, and each closes a gap the M64 audit found in
the runtime as it stands:

  * **Provenance is structural, not narrative.** :class:`EvidenceRef` carries a
    :class:`Provenance` that says *how the evidence came to exist*. Evidence a
    model merely asserted is ``MODEL_ASSERTED`` and can never be
    ``VERIFIED`` — the existing ``AgentReport.evidence`` is parsed out of a
    specialist's own bullet points and is indistinguishable from a real tool
    result, which is exactly the confusion this type refuses to allow.

  * **A tool result that did not happen has no status.** :class:`ToolCallStatus`
    has no default and no "probably fine" member; :class:`ToolOutcome` requires
    one. A command that never ran cannot be described as SUCCESS because there is
    nothing to construct the outcome from.

  * **A handoff narrows and never widens.** :meth:`SpecialistHandoff.narrow_to`
    can only intersect scope and shrink budget. There is deliberately no method
    that adds a target, adds an activity class, raises an autonomy ceiling or
    grows a budget, so scope cannot creep along a delegation chain.

Pure and deterministic: no model, no tool, no socket, no filesystem.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from core.cognitive_mesh import (
    AutonomyLevel,
    MeshBudget,
    SpecialistId,
    permits,
)
from core.security_scope import ActivityClass

# ── bounds. Every list in this module is capped; none is a transcript. ────────
MAX_TEXT = 1_200
MAX_SUMMARY = 1_500
MAX_LIST = 24
MAX_EVIDENCE_PER_CLAIM = 8
MAX_REF_ID = 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return (text or "").strip()[:limit]


def _clip_all(items, limit: int = MAX_LIST, width: int = MAX_TEXT) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or ():
        value = _clip(str(item), width)
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return tuple(out)


# ══════════════════════════════════════════════════════════════════════════════
#  Evidence
# ══════════════════════════════════════════════════════════════════════════════
class Provenance(str, Enum):
    """How a piece of evidence came to exist.

    Ordered by how much it can bear, and deliberately mirroring
    ``core.world_state.ObservationTrust``: provenance is decided by WHO produced
    the evidence, never by what the evidence says about itself.
    """

    OPERATOR = "operator"              # the human stated it
    TOOL_RESULT = "tool_result"        # a tool we ran actually returned it
    WORLD_STATE = "world_state"        # the situational world model holds it
    TELEMETRY = "telemetry"            # a sensor/connector reported it
    DOCUMENT = "document"              # a file or page we read
    EXTERNAL_REPORT = "external_report"  # a third party published it
    MODEL_ASSERTED = "model_asserted"  # a model said it and nothing corroborates it


#: Provenances that can support a VERIFIED claim. ``MODEL_ASSERTED`` is absent by
#: design: a model asserting its own conclusion is not evidence for it.
CORROBORATING_PROVENANCE: frozenset[Provenance] = frozenset({
    Provenance.OPERATOR, Provenance.TOOL_RESULT, Provenance.WORLD_STATE,
    Provenance.TELEMETRY, Provenance.DOCUMENT, Provenance.EXTERNAL_REPORT,
})


class ToolCallStatus(str, Enum):
    """What actually happened to a tool invocation (§35). No default member."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


#: Statuses whose output may be cited as evidence. A DENIED or UNAVAILABLE call
#: produced no observation of the world, and PARTIAL output is citable but is
#: never complete — the caller must say so.
CITABLE_STATUS: frozenset[ToolCallStatus] = frozenset({
    ToolCallStatus.SUCCESS, ToolCallStatus.PARTIAL,
})


@dataclass(frozen=True)
class ToolOutcome:
    """One tool invocation and what it actually did.

    ``status`` has no default: a caller that never ran a tool cannot build one of
    these, so there is no shape in which a fabricated result looks real.
    """

    tool: str
    status: ToolCallStatus
    summary: str = ""
    specialist: SpecialistId | None = None
    denial_reason: str = ""
    elapsed_s: float = 0.0
    timestamp: str = field(default_factory=_now_iso)

    @property
    def citable(self) -> bool:
        return self.status in CITABLE_STATUS

    @property
    def complete(self) -> bool:
        return self.status is ToolCallStatus.SUCCESS

    def to_dict(self) -> dict:
        return {
            "tool": self.tool, "status": self.status.value,
            "summary": _clip(self.summary, 400),
            "specialist": self.specialist.value if self.specialist else None,
            "denial_reason": _clip(self.denial_reason, 300),
            "elapsed_s": round(self.elapsed_s, 3), "timestamp": self.timestamp,
            "citable": self.citable, "complete": self.complete,
        }


@dataclass(frozen=True)
class EvidenceRef:
    """One piece of evidence, addressed by a content-derived id.

    The id is a digest of provenance + source + content, so the same observation
    recorded twice is one reference and two specialists citing the same fact do
    not inflate the evidence count.
    """

    content: str
    provenance: Provenance
    source: str = ""
    specialist: SpecialistId | None = None
    tool_outcome: ToolOutcome | None = None
    confidence: float = 0.5
    #: Set by the ingestion path when the injection firewall replaced this
    #: content with a neutral stub. Quarantined content is still RECORDED -- the
    #: attempt is itself an observation worth keeping -- but it supports nothing,
    #: because the text a claim would rest on is precisely what was removed.
    quarantined: bool = False
    timestamp: str = field(default_factory=_now_iso)

    @property
    def ref_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.provenance.value}|{self.source}|{_clip(self.content)}".encode()
        ).hexdigest()
        return f"ev:{digest[:16]}"

    @property
    def corroborating(self) -> bool:
        """Whether this reference can support a VERIFIED claim.

        A tool-sourced reference must additionally name a citable outcome: a
        DENIED call is a fact about the request, never about the world.
        """
        if self.quarantined:
            return False
        if self.provenance not in CORROBORATING_PROVENANCE:
            return False
        if self.provenance is Provenance.TOOL_RESULT:
            return self.tool_outcome is not None and self.tool_outcome.citable
        return True

    def to_dict(self) -> dict:
        return {
            "ref_id": self.ref_id, "content": _clip(self.content, 600),
            "provenance": self.provenance.value, "source": self.source,
            "specialist": self.specialist.value if self.specialist else None,
            "tool_outcome": self.tool_outcome.to_dict() if self.tool_outcome else None,
            "confidence": round(max(0.0, min(1.0, self.confidence)), 2),
            "quarantined": self.quarantined,
            "corroborating": self.corroborating, "timestamp": self.timestamp,
        }


class ClaimStatus(str, Enum):
    """The epistemic state of one claim (§15).

    ``UNVERIFIED`` is the default and the honest answer for a high-impact
    conclusion with nothing under it. There is no member meaning "probably".
    """

    UNVERIFIED = "unverified"
    OBSERVED = "observed"
    INFERRED = "inferred"
    DISPUTED = "disputed"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Claim:
    """One assertion bound to the evidence that supports it (§16)."""

    statement: str
    author: SpecialistId
    evidence_ids: tuple[str, ...] = ()
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    inference_rule: str = ""
    confidence: float = 0.5
    high_impact: bool = False
    timestamp: str = field(default_factory=_now_iso)

    @property
    def claim_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.author.value}|{_clip(self.statement)}".encode()).hexdigest()
        return f"cl:{digest[:16]}"

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id, "statement": _clip(self.statement, 600),
            "author": self.author.value, "evidence_ids": list(self.evidence_ids),
            "status": self.status.value, "inference_rule": _clip(self.inference_rule, 300),
            "confidence": round(max(0.0, min(1.0, self.confidence)), 2),
            "high_impact": self.high_impact, "timestamp": self.timestamp,
        }


class EvidenceGraph:
    """Claim-to-evidence bindings for one task.

    Bounded and append-mostly. Two specialists that disagree produce two claims;
    neither overwrites the other, and :meth:`disputed` names the pairs — §33's
    "specialists cannot overwrite disagreement" is enforced by there being no
    method that replaces a claim's statement.
    """

    def __init__(self, budget: MeshBudget | None = None) -> None:
        self._budget = budget or MeshBudget()
        self._evidence: dict[str, EvidenceRef] = {}
        self._claims: dict[str, Claim] = {}

    # ── writes ───────────────────────────────────────────────────────────────
    def add_evidence(self, ref: EvidenceRef) -> str | None:
        """Record *ref*; returns its id, or ``None`` when the bound is full."""
        rid = ref.ref_id
        if rid in self._evidence:
            return rid
        if len(self._evidence) >= self._budget.max_evidence_items:
            return None
        self._evidence[rid] = ref
        return rid

    def add_claim(self, claim: Claim) -> str | None:
        """Record *claim*, re-deriving its status from the evidence actually
        bound to it. A caller cannot assert VERIFIED into existence."""
        cid = claim.claim_id
        if cid in self._claims:
            return cid
        if len(self._claims) >= self._budget.max_claims:
            return None
        bound = tuple(e for e in claim.evidence_ids[:MAX_EVIDENCE_PER_CLAIM]
                      if e in self._evidence)
        self._claims[cid] = replace(claim, evidence_ids=bound,
                                    status=self._derive_status(claim, bound))
        return cid

    def _derive_status(self, claim: Claim, bound: tuple[str, ...]) -> ClaimStatus:
        """The status the evidence supports — never the one the author wanted.

        REJECTED is the only status an author may keep unchanged: withdrawing a
        claim needs no evidence. Everything else is re-derived here.
        """
        if claim.status is ClaimStatus.REJECTED:
            return ClaimStatus.REJECTED
        corroborated = [r for r in (self._evidence[b] for b in bound) if r.corroborating]
        if not corroborated:
            return ClaimStatus.UNVERIFIED
        if claim.status is ClaimStatus.INFERRED or claim.inference_rule.strip():
            return ClaimStatus.INFERRED
        return ClaimStatus.OBSERVED

    def mark_verified(self, claim_id: str, *, by: SpecialistId) -> bool:
        """Promote a claim to VERIFIED. Only ARGUS may, and only when the claim
        already rests on corroborating evidence."""
        claim = self._claims.get(claim_id)
        if claim is None or by is not SpecialistId.ARGUS:
            return False
        if not any(self._evidence[e].corroborating for e in claim.evidence_ids):
            return False
        self._claims[claim_id] = replace(claim, status=ClaimStatus.VERIFIED)
        return True

    def mark_disputed(self, claim_id: str) -> bool:
        claim = self._claims.get(claim_id)
        if claim is None:
            return False
        self._claims[claim_id] = replace(claim, status=ClaimStatus.DISPUTED)
        return True

    # ── reads ────────────────────────────────────────────────────────────────
    def evidence(self, ref_id: str) -> EvidenceRef | None:
        return self._evidence.get(ref_id)

    def claim(self, claim_id: str) -> Claim | None:
        return self._claims.get(claim_id)

    def claims(self) -> tuple[Claim, ...]:
        return tuple(self._claims.values())

    def all_evidence(self) -> tuple[EvidenceRef, ...]:
        return tuple(self._evidence.values())

    @property
    def evidence_count(self) -> int:
        return len(self._evidence)

    @property
    def corroborated_evidence_count(self) -> int:
        return sum(1 for r in self._evidence.values() if r.corroborating)

    def unsupported_claims(self) -> tuple[Claim, ...]:
        """Claims resting on no corroborating evidence. A high-impact member of
        this tuple is what ARGUS refuses to let through as a finding."""
        return tuple(c for c in self._claims.values()
                     if c.status is ClaimStatus.UNVERIFIED)

    def unsupported_high_impact(self) -> tuple[Claim, ...]:
        return tuple(c for c in self.unsupported_claims() if c.high_impact)

    def disputed(self) -> tuple[tuple[Claim, Claim], ...]:
        """Pairs of claims by different authors that contradict each other.

        Contradiction is structural: the same normalised statement asserted by
        one author and negated by another. It is never inferred from tone, and
        never from a substring of a specialist's prose — that is the failure the
        existing ``_parse_report`` verdict scan already demonstrates.
        """
        pairs: list[tuple[Claim, Claim]] = []
        claims = list(self._claims.values())
        for i, a in enumerate(claims):
            for b in claims[i + 1:]:
                if a.author is b.author:
                    continue
                if _contradicts(a.statement, b.statement):
                    pairs.append((a, b))
        return tuple(pairs)

    def to_dict(self) -> dict:
        return {
            "evidence": [r.to_dict() for r in self._evidence.values()],
            "claims": [c.to_dict() for c in self._claims.values()],
            "evidence_count": self.evidence_count,
            "corroborated_evidence_count": self.corroborated_evidence_count,
            "unsupported_claims": len(self.unsupported_claims()),
            "disputed_pairs": len(self.disputed()),
        }


_NEGATIONS = (" is not ", " does not ", " did not ", " was not ", " cannot ", " no ")
_AFFIRMATIONS = (" is ", " does ", " did ", " was ", " can ", " ")


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def _contradicts(a: str, b: str) -> bool:
    """Whether two statements are the same assertion, one negated.

    Deliberately narrow. A false negative leaves a disagreement for a human to
    notice; a false positive would manufacture one, and manufactured conflict is
    how a mesh talks itself out of a correct answer.
    """
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb or na == nb:
        return False
    for neg, aff in zip(_NEGATIONS, _AFFIRMATIONS):
        if neg in na and na.replace(neg, aff, 1) == nb:
            return True
        if neg in nb and nb.replace(neg, aff, 1) == na:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  Handoff (§14)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class HandoffScope:
    """The bounded envelope a handoff carries. Intersect-only by construction."""

    targets: frozenset[str] = frozenset()
    activities: frozenset[ActivityClass] = frozenset()
    autonomy_ceiling: AutonomyLevel = AutonomyLevel.ADVISE
    security_scope_id: str | None = None

    def narrow(self, *, targets: "frozenset[str] | None" = None,
               activities: "frozenset[ActivityClass] | None" = None,
               autonomy_ceiling: AutonomyLevel | None = None) -> "HandoffScope":
        """Return a scope no wider than this one, on every axis.

        Targets and activities intersect; the ceiling takes the minimum. There is
        no ``widen``: adding a target or an activity to a delegated scope is not
        an operation this type supports, so no call chain can perform it.
        """
        new_targets = (self.targets & targets) if targets is not None else self.targets
        new_acts = (self.activities & activities) if activities is not None else self.activities
        ceiling = self.autonomy_ceiling
        if autonomy_ceiling is not None:
            ceiling = min(ceiling, autonomy_ceiling, key=int)
        return HandoffScope(targets=new_targets, activities=new_acts,
                            autonomy_ceiling=ceiling,
                            security_scope_id=self.security_scope_id)

    def permits_target(self, target: str) -> bool:
        return (target or "").strip() in self.targets

    def permits_activity(self, activity: ActivityClass) -> bool:
        return activity in self.activities

    def to_dict(self) -> dict:
        return {
            "targets": sorted(self.targets),
            "activities": sorted(a.value for a in self.activities),
            "autonomy_ceiling": int(self.autonomy_ceiling),
            "security_scope_id": self.security_scope_id,
        }


@dataclass(frozen=True)
class SpecialistHandoff:
    """One typed delegation (§14). Never the whole conversation."""

    task_id: str
    from_specialist: SpecialistId
    to_specialist: SpecialistId
    objective: str
    depth: int = 0
    world_state_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    known_facts: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    scope: HandoffScope = field(default_factory=HandoffScope)
    prohibited_actions: tuple[str, ...] = ()
    requested_output: str = ""
    budget: MeshBudget = field(default_factory=MeshBudget)
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        for name in ("world_state_refs", "evidence_refs", "known_facts", "hypotheses",
                     "assumptions", "uncertainty", "prohibited_actions"):
            object.__setattr__(self, name, _clip_all(getattr(self, name)))
        object.__setattr__(self, "objective", _clip(self.objective))
        object.__setattr__(self, "requested_output", _clip(self.requested_output))

    def delegate(self, to: SpecialistId, objective: str, *,
                 scope: HandoffScope | None = None,
                 budget: MeshBudget | None = None) -> "SpecialistHandoff":
        """Build the next handoff in the chain.

        Depth increments, scope narrows (never widens), and the budget is the
        smaller of the two on every axis. A caller passing a *larger* budget gets
        the current one back — the argument can tighten a delegation, never
        loosen it.
        """
        narrowed = self.scope.narrow(
            targets=scope.targets if scope else None,
            activities=scope.activities if scope else None,
            autonomy_ceiling=scope.autonomy_ceiling if scope else None,
        )
        return SpecialistHandoff(
            task_id=self.task_id, from_specialist=self.to_specialist,
            to_specialist=to, objective=objective, depth=self.depth + 1,
            world_state_refs=self.world_state_refs, evidence_refs=self.evidence_refs,
            known_facts=self.known_facts, hypotheses=self.hypotheses,
            assumptions=self.assumptions, uncertainty=self.uncertainty,
            scope=narrowed,
            prohibited_actions=self.prohibited_actions,
            requested_output=self.requested_output,
            budget=_min_budget(self.budget, budget),
        )

    def within_depth(self) -> bool:
        return self.depth <= self.budget.max_handoff_depth

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "from": self.from_specialist.value,
            "to": self.to_specialist.value, "objective": self.objective,
            "depth": self.depth,
            "world_state_refs": list(self.world_state_refs),
            "evidence_refs": list(self.evidence_refs),
            "known_facts": list(self.known_facts),
            "hypotheses": list(self.hypotheses),
            "assumptions": list(self.assumptions),
            "uncertainty": list(self.uncertainty),
            "scope": self.scope.to_dict(),
            "prohibited_actions": list(self.prohibited_actions),
            "requested_output": self.requested_output,
            "budget": self.budget.to_dict(), "timestamp": self.timestamp,
        }


def _min_budget(current: MeshBudget, other: MeshBudget | None) -> MeshBudget:
    """Field-wise minimum. A delegated budget can only shrink."""
    if other is None:
        return current
    return MeshBudget(
        max_specialists=min(current.max_specialists, other.max_specialists),
        max_handoff_depth=min(current.max_handoff_depth, other.max_handoff_depth),
        max_handoffs=min(current.max_handoffs, other.max_handoffs),
        max_verifier_retries=min(current.max_verifier_retries, other.max_verifier_retries),
        max_tool_calls=min(current.max_tool_calls, other.max_tool_calls),
        max_runtime_s=min(current.max_runtime_s, other.max_runtime_s),
        max_context_chars=min(current.max_context_chars, other.max_context_chars),
        max_evidence_items=min(current.max_evidence_items, other.max_evidence_items),
        max_claims=min(current.max_claims, other.max_claims),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Action request (§51)
# ══════════════════════════════════════════════════════════════════════════════
class ActionDisposition(str, Enum):
    """What the mesh decided to do with a proposed effect. None of these is an
    execution: even ``APPROVED_FOR_EXECUTOR`` only means the proposal may now be
    handed to ``ToolExecutor``, which applies its own gate afterwards."""

    PROPOSED = "proposed"
    REQUIRES_HUMAN_APPROVAL = "requires_human_approval"
    APPROVED_FOR_EXECUTOR = "approved_for_executor"
    REFUSED_OUT_OF_SCOPE = "refused_out_of_scope"
    REFUSED_ABOVE_AUTONOMY = "refused_above_autonomy"
    REFUSED_NO_EVIDENCE = "refused_no_evidence"


@dataclass(frozen=True)
class ActionRequest:
    """An effectful recommendation, typed so it can be judged (§51).

    Constructing one performs nothing. It is a *request*: the existing global
    action controls — ``authorize_action``, ``classify_tool``, the NATO HITL
    challenge in ``ToolExecutor.aexecute`` — remain the only path to an effect,
    and this type exists so what reaches them is legible rather than parsed out
    of prose.
    """

    action: str
    target: str
    justification: str
    requested_by: SpecialistId
    evidence_ids: tuple[str, ...] = ()
    risk: str = "high_impact"
    reversible: bool = False
    rollback_plan: str = ""
    required_capability: str = ""
    required_scope_id: str | None = None
    required_autonomy: AutonomyLevel = AutonomyLevel.HITL_EXECUTE
    disposition: ActionDisposition = ActionDisposition.PROPOSED
    disposition_reason: str = ""
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids[:MAX_LIST]))

    @property
    def executed(self) -> bool:
        """Always ``False``. An ActionRequest is a proposal; there is no field
        that could make it otherwise, and this property exists so a caller
        asking the question gets the same answer every time."""
        return False

    def decide(self, disposition: ActionDisposition, reason: str) -> "ActionRequest":
        return replace(self, disposition=disposition,
                       disposition_reason=_clip(reason, 400))

    def to_dict(self) -> dict:
        return {
            "action": self.action, "target": self.target,
            "justification": _clip(self.justification, 600),
            "requested_by": self.requested_by.value,
            "evidence_ids": list(self.evidence_ids), "risk": self.risk,
            "reversible": self.reversible,
            "rollback_plan": _clip(self.rollback_plan, 400),
            "required_capability": self.required_capability,
            "required_scope_id": self.required_scope_id,
            "required_autonomy": int(self.required_autonomy),
            "disposition": self.disposition.value,
            "disposition_reason": self.disposition_reason,
            "executed": self.executed, "timestamp": self.timestamp,
        }


def dispose_action(request: ActionRequest, *, ceiling: AutonomyLevel,
                   graph: EvidenceGraph | None = None,
                   scope_ok: bool = True, scope_reason: str = "") -> ActionRequest:
    """Decide an :class:`ActionRequest` against autonomy, evidence and scope.

    Refusals are checked before approvals, and the strongest reason wins. Nothing
    here executes; the best outcome available is ``APPROVED_FOR_EXECUTOR``, which
    is a hand-off to the gate that has always existed.
    """
    if not scope_ok:
        return request.decide(ActionDisposition.REFUSED_OUT_OF_SCOPE,
                              scope_reason or "target outside authorized scope")
    supported = graph is not None and any(
        (ref := graph.evidence(e)) is not None and ref.corroborating
        for e in request.evidence_ids)
    if not supported:
        return request.decide(
            ActionDisposition.REFUSED_NO_EVIDENCE,
            "an effect on the world requires corroborating evidence; this request "
            "cites none that a tool, the operator or the world model produced")
    if not permits(ceiling, request.required_autonomy):
        return request.decide(
            ActionDisposition.REFUSED_ABOVE_AUTONOMY,
            f"action needs autonomy L{int(request.required_autonomy)} and the "
            f"specialist's ceiling is L{int(ceiling)}")
    if request.required_autonomy >= AutonomyLevel.HITL_EXECUTE:
        return request.decide(
            ActionDisposition.REQUIRES_HUMAN_APPROVAL,
            "effectful action: a human confirms before ToolExecutor is reached")
    return request.decide(ActionDisposition.APPROVED_FOR_EXECUTOR,
                          "within autonomy, in scope and evidence-backed; the "
                          "executor's own risk/HITL gate still applies")


# ══════════════════════════════════════════════════════════════════════════════
#  Specialist result (§50)
# ══════════════════════════════════════════════════════════════════════════════
class ResultStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class SpecialistResult:
    """What a specialist returns. There is no free-form blob the orchestrator
    then treats as truth (§50)."""

    status: ResultStatus
    specialist_id: SpecialistId
    task_id: str
    summary: str = ""
    findings: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    assumptions: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    requested_handoffs: tuple[SpecialistId, ...] = ()
    recommended_actions: tuple[str, ...] = ()
    effectful_action_requests: tuple[ActionRequest, ...] = ()
    tool_outcomes: tuple[ToolOutcome, ...] = ()
    limitations: tuple[str, ...] = ()
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _clip(self.summary, MAX_SUMMARY))
        for name in ("findings", "assumptions", "uncertainty", "recommended_actions",
                     "limitations"):
            object.__setattr__(self, name, _clip_all(getattr(self, name)))
        object.__setattr__(self, "confidence",
                           round(max(0.0, min(1.0, float(self.confidence))), 2))

    @property
    def hallucinated_tool_results(self) -> int:
        """Tool outcomes claiming success while carrying no evidence.

        Always derived, never stored: a specialist cannot report zero here by
        saying so.
        """
        return sum(1 for o in self.tool_outcomes
                   if o.status is ToolCallStatus.SUCCESS and not o.summary.strip())

    def to_dict(self) -> dict:
        return {
            "status": self.status.value, "specialist_id": self.specialist_id.value,
            "task_id": self.task_id, "summary": self.summary,
            "findings": list(self.findings), "claim_ids": list(self.claim_ids),
            "evidence_ids": list(self.evidence_ids), "confidence": self.confidence,
            "assumptions": list(self.assumptions),
            "uncertainty": list(self.uncertainty),
            "requested_handoffs": [h.value for h in self.requested_handoffs],
            "recommended_actions": list(self.recommended_actions),
            "effectful_action_requests":
                [a.to_dict() for a in self.effectful_action_requests],
            "tool_outcomes": [o.to_dict() for o in self.tool_outcomes],
            "limitations": list(self.limitations), "timestamp": self.timestamp,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Verifier verdict (§32)
# ══════════════════════════════════════════════════════════════════════════════
class Verdict(str, Enum):
    VERIFIED = "verified"
    VERIFIED_WITH_LIMITATIONS = "verified_with_limitations"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SCOPE_VIOLATION = "scope_violation"
    AUTHORITY_MISSING = "authority_missing"
    CONFLICT_UNRESOLVED = "conflict_unresolved"
    FAILED = "failed"


#: Verdicts under which the final answer may state a conclusion as established.
#: Everything else forces the answer to carry its own caveat.
PASSING_VERDICTS: frozenset[Verdict] = frozenset({
    Verdict.VERIFIED, Verdict.VERIFIED_WITH_LIMITATIONS,
})


@dataclass(frozen=True)
class VerifierVerdict:
    """ARGUS's answer. It reports; it never grants."""

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    scope_violations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    retries_used: int = 0
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        for name in ("reasons", "unsupported_claims", "scope_violations", "limitations"):
            object.__setattr__(self, name, _clip_all(getattr(self, name)))

    @property
    def passing(self) -> bool:
        return self.verdict in PASSING_VERDICTS

    @property
    def grants_authority(self) -> bool:
        """Always ``False``. ARGUS cannot grant what it audits, and there is no
        field on this type that could express a grant."""
        return False

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value, "reasons": list(self.reasons),
            "unsupported_claims": list(self.unsupported_claims),
            "scope_violations": list(self.scope_violations),
            "limitations": list(self.limitations),
            "retries_used": self.retries_used, "passing": self.passing,
            "grants_authority": self.grants_authority, "timestamp": self.timestamp,
        }
