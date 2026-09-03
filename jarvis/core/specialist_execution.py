"""
core/specialist_execution.py — V69 M65A: the specialist execution core.

M64 built the mesh: a registry of role contracts, typed contracts to speak in, a
deterministic router, a preflight gate and ARGUS. M64.1 wired it onto the live
turn — but it wired the mesh's JUDGEMENT, not its work. ``MeshTurn`` has carried
a ``support_results`` list since M64.1 and nothing has ever appended to it,
because there was no component whose job was to run one specialist and come back
with a typed result.

This module is that component, and only that. It is the spine, not a swarm:

    SpecialistExecutionRequest -> SpecialistExecutor.run() -> SpecialistExecutionResult

WHAT IT REUSES (everything)
===========================
    who a specialist is         core.cognitive_mesh.REGISTRY
    how far it may go           core.cognitive_mesh.AutonomyLevel / permits
    which model it reasons on   core.model_role_router.ModelRoleRouter
    whether a tool is warranted core.mesh_orchestrator.preflight
    which tools it may reach    core.specialist_runtime.ToolBroker
    the ONE effect path         tools.executor.ToolExecutor.aexecute
    exactly-once                that executor's own effect ledger
    whether the work holds up   core.mesh_verifier.verify (ARGUS)
    what it may say             core.mesh_contracts.SpecialistResult

There is no second registry, no second broker, no second ledger, no second
verifier and no subprocess. A grep of this file for ``subprocess``, ``popen``,
``os.system`` or ``socket`` returns nothing, and a test asserts that.

THE LOAD-BEARING ASYMMETRY
==========================
A specialist supplies *intent*: what it wants to look at and why. Every question
of PERMISSION is answered by this executor reading the registry and the policy —
never by reading the specialist's own output.

Concretely, the model's text is parsed for exactly one thing: proposed tool
intents. It is never parsed for an autonomy level, a capability, a scope, an
approval, a verification verdict or a model role. Those fields do not appear in
:class:`ToolIntent` at all, so a specialist has no shape in which to ask for
them, and :meth:`SpecialistExecutor._effective_ceiling` recomputes the ceiling
from the registry on every call rather than trusting the one on the request.

That is what makes "a specialist cannot grant itself authority" a structural
property. It is not that we check the specialist's claim and reject it; it is
that there is nowhere for the claim to live.

BODY-SAFE BY CONSTRUCTION (§24)
===============================
:class:`SpecialistExecutionResult` carries ids, statuses, counters, durations and
bounded human-readable summaries. It carries no chain-of-thought, no system
prompt, no raw tool payload and no secret. ``body_safe_trace`` is a tuple of
short decision strings — "capability denied", "fell back to fast" — assembled by
this module, never by a model.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.cognitive_mesh import (
    REGISTRY,
    AutonomyLevel,
    MeshBudget,
    SpecialistId,
    permits,
)
from core.mesh_contracts import (
    ActionRequest,
    EvidenceGraph,
    EvidenceRef,
    Provenance,
    ResultStatus,
    SpecialistResult,
    ToolCallStatus,
    ToolOutcome,
    VerifierVerdict,
)
from core.mesh_context import screen_evidence
from core.mesh_orchestrator import ToolPreflight, evidence_from_tool, preflight
from core.mesh_verifier import VerificationInput, verify
from core.model_role_router import ModelRoleRouter, RoleSelection
from core.model_role_router import router as _default_role_router
from core.risk_classes import RiskClass, classify_tool
from core.security_scope import ActivityClass, SecurityScopeDecision
from core.specialist_runtime import ModelTier, ToolBroker, spec_for

logger = logging.getLogger("jarvis.specialist_execution")

# ── bounds. Everything here is capped; nothing is a transcript. ─────────────
MAX_OBJECTIVE = 800
MAX_SUMMARY = 1_500
MAX_FINDINGS = 8
MAX_FINDING_CHARS = 400
MAX_INTENTS = 4
MAX_TRACE = 24
MAX_TRACE_CHARS = 200
MAX_WARNINGS = 12
MAX_TOOL_OUTPUT = 1_200

#: Hard ceiling on one specialist execution, whatever the request asks for.
#: A request may ask for LESS; it may never ask for more (§32).
MAX_DEADLINE_S = 60.0
DEFAULT_DEADLINE_S = 30.0

#: Retries are finite, observable and exactly-once aware (§32). A retry re-enters
#: the SAME effect identity, so the ledger — not this number — is what keeps the
#: effect count at one.
MAX_ATTEMPTS = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _canonical_args(tool_input: dict) -> str:
    """The same canonicalisation ``ToolExecutor._effect_key`` uses.

    Deliberately identical: an effect identity computed here that did not match
    the ledger's would let a duplicate through while looking careful, which is
    worse than not computing one at all.
    """
    try:
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(sorted(tool_input.items())) if tool_input else "{}"


# ══════════════════════════════════════════════════════════════════════════════
#  Status vocabulary (§31)
# ══════════════════════════════════════════════════════════════════════════════
class ExecutionStatus(str, Enum):
    """What happened to one specialist execution.

    Mapped onto the repository's existing ``ResultStatus`` by
    :attr:`SpecialistExecutionResult.result_status` so a specialist's outcome
    reaches ARGUS and synthesis in the vocabulary they already read. There is no
    member meaning "probably fine": an execution that did not reach a conclusion
    is PARTIAL or FAILED, and JARVIS is told which.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def succeeded(self) -> bool:
        return self is ExecutionStatus.SUCCESS


_RESULT_STATUS: "dict[ExecutionStatus, ResultStatus]" = {
    ExecutionStatus.SUCCESS: ResultStatus.COMPLETE,
    ExecutionStatus.PARTIAL: ResultStatus.PARTIAL,
    ExecutionStatus.FAILED: ResultStatus.FAILED,
    ExecutionStatus.DENIED: ResultStatus.REFUSED,
    ExecutionStatus.TIMED_OUT: ResultStatus.PARTIAL,
    ExecutionStatus.CANCELLED: ResultStatus.BLOCKED,
}


# ══════════════════════════════════════════════════════════════════════════════
#  Tool intent and receipt (§19)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ToolIntent:
    """What a specialist wants to do. Constructing one performs nothing.

    Note the fields that are ABSENT and cannot be added by a specialist:
    autonomy, capability, scope id, approval, verification. A specialist's only
    inputs are the tool, its arguments, and why. Everything else is decided for
    it from the registry.
    """

    tool: str
    tool_input: "dict" = field(default_factory=dict)
    why: str = ""
    hypothesis: str = ""
    target: "str | None" = None
    expected_evidence: str = ""
    read_only_alternative: str = ""
    activity: "ActivityClass | None" = None

    def effect_identity(self, epoch: str) -> str:
        """This intent's identity in the ONE effect ledger.

        Same shape as ``ToolExecutor._effect_key(epoch, tool, args)``, so an
        approval bound to this string is bound to exactly the effect the
        executor would deduplicate.
        """
        return f"{epoch}|{self.tool}|{_canonical_args(self.tool_input)}"

    def digest(self) -> str:
        return "ti:" + hashlib.sha256(
            f"{self.tool}|{_canonical_args(self.tool_input)}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "tool": self.tool, "digest": self.digest(),
            "why": _clip(self.why, 300), "hypothesis": _clip(self.hypothesis, 300),
            "target": self.target,
            "activity": self.activity.value if self.activity else None,
        }


@dataclass(frozen=True)
class ToolReceipt:
    """Proof of what the ONE executor actually did with one intent.

    Built only from an outcome that came back from ``ToolExecutor.aexecute``, or
    from a gate that refused before it. A receipt is never constructed from a
    specialist's description of what it did, which is why ARGUS can read
    receipts instead of prose.
    """

    receipt_id: str
    tool: str
    specialist_id: SpecialistId
    status: ToolCallStatus
    effect_identity: str
    executed: bool
    deduplicated: bool = False
    risk: str = RiskClass.READ_ONLY.value
    required_autonomy: AutonomyLevel = AutonomyLevel.HITL_EXECUTE
    denial_reason: str = ""
    summary: str = ""
    elapsed_s: float = 0.0
    scope_decision: "SecurityScopeDecision | None" = None
    hitl_approval_id: "str | None" = None
    timestamp: str = field(default_factory=_now_iso)

    @property
    def outcome(self) -> ToolOutcome:
        return ToolOutcome(
            tool=self.tool, status=self.status, summary=self.summary,
            specialist=self.specialist_id, denial_reason=self.denial_reason,
            elapsed_s=self.elapsed_s)

    @property
    def identity_digest(self) -> str:
        return "ei:" + hashlib.sha256(self.effect_identity.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id, "tool": self.tool,
            "specialist_id": self.specialist_id.value, "status": self.status.value,
            "effect_identity_digest": self.identity_digest,
            "executed": self.executed, "deduplicated": self.deduplicated,
            "risk": self.risk, "required_autonomy": int(self.required_autonomy),
            "denial_reason": _clip(self.denial_reason, 300),
            "summary": _clip(self.summary, 400),
            "elapsed_s": round(self.elapsed_s, 3),
            "scope_decision": (self.scope_decision.to_dict()
                               if self.scope_decision else None),
            "hitl_approval_id": self.hitl_approval_id,
            "timestamp": self.timestamp,
        }


def _receipt_id(specialist_id: SpecialistId, effect_identity: str) -> str:
    return "rcpt:" + hashlib.sha256(
        f"{specialist_id.value}|{effect_identity}".encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
#  HITL approval (§21)
# ══════════════════════════════════════════════════════════════════════════════
class ApprovalDenial(str, Enum):
    NO_APPROVAL = "no_approval"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


@dataclass(frozen=True)
class HitlApproval:
    """One human decision, bound to one effect.

    This does NOT replace ``ToolExecutor``'s NATO challenge, and it does not
    replace ``ContainmentAuthorization``. It is the earlier, narrower question
    §21 asks: may this SPECIFIC intent from this SPECIFIC specialist be handed to
    the executor at all? The executor's own gate still runs afterwards, so an
    approval here is a necessary condition and never a sufficient one.

    Binding is by ``effect_identity`` — epoch, tool and canonical arguments —
    which is precisely what the effect ledger keys on. Change the target, change
    the arguments, change the tool, and the identity changes, so approval for A
    cannot approve a modified B. There is no field that widens it.
    """

    approval_id: str
    specialist_id: SpecialistId
    effect_identity: str
    granted_by: str = "operator"
    single_use: bool = True
    expires_at: str = ""
    reason: str = ""
    granted_at: str = field(default_factory=_now_iso)

    def is_expired(self, now: "datetime | None" = None) -> bool:
        """No expiry means EXPIRED, and an unparseable one means EXPIRED.

        Both are the fail-closed reading ``ContainmentAuthorization.is_expired``
        adopted after the M64.1 CASE E finding, and for the same reason: an
        approval whose deadline cannot be read is not an approval.
        """
        raw = (self.expires_at or "").strip()
        if not raw:
            return True
        try:
            deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) >= deadline

    def covers(self, specialist_id: SpecialistId, effect_identity: str) -> bool:
        """Exact match on both. No prefix, no substring, no family."""
        return (self.specialist_id is specialist_id
                and self.effect_identity == effect_identity)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "specialist_id": self.specialist_id.value,
            "effect_identity_digest": "ei:" + hashlib.sha256(
                self.effect_identity.encode()).hexdigest()[:16],
            "granted_by": self.granted_by, "single_use": self.single_use,
            "expires_at": self.expires_at, "reason": _clip(self.reason, 200),
            "granted_at": self.granted_at,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    allowed: bool
    approval_id: "str | None" = None
    denial: "ApprovalDenial | None" = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "approval_id": self.approval_id,
                "denial": self.denial.value if self.denial else None,
                "reason": _clip(self.reason, 300)}


@dataclass
class HitlApprovalRegistry:
    """Operator-held approvals. Mutated only by :meth:`grant` and :meth:`revoke`.

    Nothing in the specialist path calls ``grant``; that absence is the control,
    and a test asserts it. Single-use consumption is recorded HERE rather than on
    the frozen approval, so an approval object a specialist somehow held a
    reference to could not un-consume itself.
    """

    approvals: "list[HitlApproval]" = field(default_factory=list)
    consumed: "set[str]" = field(default_factory=set)
    max_approvals: int = 32

    def grant(self, approval: HitlApproval) -> HitlApproval:
        self.approvals = [a for a in self.approvals
                          if a.approval_id != approval.approval_id]
        self.approvals.append(approval)
        if len(self.approvals) > self.max_approvals:
            self.approvals = self.approvals[-self.max_approvals:]
        return approval

    def revoke(self, approval_id: str) -> bool:
        before = len(self.approvals)
        self.approvals = [a for a in self.approvals if a.approval_id != approval_id]
        return len(self.approvals) != before

    def clear(self) -> None:
        self.approvals = []
        self.consumed = set()

    def decide(self, specialist_id: SpecialistId, effect_identity: str, *,
               now: "datetime | None" = None,
               consume: bool = True) -> ApprovalDecision:
        """Whether a human approved exactly this effect for exactly this
        specialist, and consume it if single-use.

        Checked in this order so the operator is told the most specific truth: a
        matching-but-expired approval reads EXPIRED, not "no approval".
        """
        matches = [a for a in self.approvals
                   if a.covers(specialist_id, effect_identity)]
        if not matches:
            return ApprovalDecision(
                False, denial=ApprovalDenial.NO_APPROVAL,
                reason=("no human approval is bound to this exact action, target "
                        "and tool; an approval for a different one is not this one"))
        approval = matches[-1]
        if approval.is_expired(now):
            return ApprovalDecision(
                False, approval_id=approval.approval_id,
                denial=ApprovalDenial.EXPIRED,
                reason="the approval bound to this action has expired")
        if approval.single_use and approval.approval_id in self.consumed:
            return ApprovalDecision(
                False, approval_id=approval.approval_id,
                denial=ApprovalDenial.ALREADY_USED,
                reason="this single-use approval has already been spent")
        if consume and approval.single_use:
            self.consumed.add(approval.approval_id)
        return ApprovalDecision(True, approval_id=approval.approval_id,
                                reason="bound human approval")

    def to_dict(self) -> dict:
        return {"approvals": len(self.approvals), "consumed": len(self.consumed)}


#: The one registry the live path reads. Operator-only, exactly like
#: ``security_effects.CONTAINMENT`` and ``security_scope.SCOPES``.
APPROVALS = HitlApprovalRegistry()


# ══════════════════════════════════════════════════════════════════════════════
#  The execution contract (§7)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SpecialistExecutionRequest:
    """One bounded unit of work handed to one specialist.

    Immutable, and deliberately NOT the source of truth for authority. The
    ``autonomy_level`` and ``allowed_tools`` fields say what the CALLER is
    willing to permit; :class:`SpecialistExecutor` intersects them with the
    registry and takes the lower of the two. A request asking for more than the
    registry allows gets the registry's answer, so a forged or mutated request
    can narrow an execution but can never widen one.
    """

    execution_id: str
    plan_id: str
    specialist_id: SpecialistId
    objective: str
    capability: str = ""
    model_role: "str | None" = None
    autonomy_level: AutonomyLevel = AutonomyLevel.ADVISE
    authorized_scope: "tuple[str, ...]" = ()
    allowed_tools: "frozenset[str]" = frozenset()
    activity: "ActivityClass | None" = None
    budget: MeshBudget = field(default_factory=MeshBudget)
    deadline_s: float = DEFAULT_DEADLINE_S
    evidence_requirements: "tuple[str, ...]" = ()
    effect_epoch: str = ""
    task_class: str = ""
    context: str = ""
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", _clip(self.objective, MAX_OBJECTIVE))
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        # §32 — bounded, always. A request cannot buy itself more wall clock than
        # the module ceiling, and a nonsensical deadline becomes the default
        # rather than an unbounded run.
        deadline = float(self.deadline_s or 0.0)
        if deadline <= 0.0:
            deadline = DEFAULT_DEADLINE_S
        object.__setattr__(self, "deadline_s", min(deadline, MAX_DEADLINE_S))

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id, "plan_id": self.plan_id,
            "specialist_id": self.specialist_id.value,
            "objective": self.objective, "capability": self.capability,
            "model_role": self.model_role,
            "autonomy_level": int(self.autonomy_level),
            "authorized_scope": list(self.authorized_scope),
            "allowed_tools": sorted(self.allowed_tools),
            "activity": self.activity.value if self.activity else None,
            "budget": self.budget.to_dict(), "deadline_s": self.deadline_s,
            "evidence_requirements": list(self.evidence_requirements),
            "task_class": self.task_class, "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SpecialistExecutionResult:
    """What a specialist execution returns (§7).

    Every field is either a bounded summary a human can read or a fact the
    executor observed. Nothing here is a claim the specialist made about its own
    permissions, and ``verification`` is written by ARGUS or is ``None`` — a
    specialist has no way to set it, which is what makes "a specialist cannot
    self-verify" structural rather than policed.
    """

    execution_id: str
    specialist_id: SpecialistId
    status: ExecutionStatus
    summary: str = ""
    findings: "tuple[str, ...]" = ()
    evidence_ids: "tuple[str, ...]" = ()
    proposals: "tuple[ActionRequest, ...]" = ()
    tool_receipts: "tuple[ToolReceipt, ...]" = ()
    verification: "VerifierVerdict | None" = None
    warnings: "tuple[str, ...]" = ()
    effective_autonomy: AutonomyLevel = AutonomyLevel.ADVISE
    model_selection: "RoleSelection | None" = None
    attempts: int = 1
    duration_ms: float = 0.0
    body_safe_trace: "tuple[str, ...]" = ()
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _clip(self.summary, MAX_SUMMARY))
        object.__setattr__(self, "findings", tuple(
            _clip(f, MAX_FINDING_CHARS) for f in self.findings[:MAX_FINDINGS] if f))
        object.__setattr__(self, "warnings", tuple(
            _clip(w, MAX_TRACE_CHARS) for w in self.warnings[:MAX_WARNINGS] if w))
        object.__setattr__(self, "body_safe_trace", tuple(
            _clip(t, MAX_TRACE_CHARS) for t in self.body_safe_trace[:MAX_TRACE] if t))

    # ── derived facts. Never stored, so they cannot be misreported. ─────────
    @property
    def result_status(self) -> ResultStatus:
        return _RESULT_STATUS[self.status]

    @property
    def executed_effects(self) -> int:
        """Effects that actually changed the world, counted from receipts."""
        return sum(1 for r in self.tool_receipts
                   if r.executed and not r.deduplicated
                   and r.risk != RiskClass.READ_ONLY.value)

    @property
    def deduplicated_effects(self) -> int:
        return sum(1 for r in self.tool_receipts if r.deduplicated)

    @property
    def denied_tools(self) -> int:
        return sum(1 for r in self.tool_receipts
                   if r.status is ToolCallStatus.DENIED)

    @property
    def verified(self) -> bool:
        """Whether ARGUS passed this execution.

        Derived from ``verification`` and nothing else. A specialist that writes
        "VERIFIED" in its summary changes this by exactly nothing.
        """
        return self.verification is not None and self.verification.passing

    def as_specialist_result(self, task_id: str) -> SpecialistResult:
        """Project onto the repository's existing contract, so this result flows
        into ``orchestrator.finish`` and ARGUS unchanged."""
        return SpecialistResult(
            status=self.result_status, specialist_id=self.specialist_id,
            task_id=task_id, summary=self.summary, findings=self.findings,
            evidence_ids=self.evidence_ids,
            confidence=0.6 if self.status.succeeded else 0.2,
            uncertainty=self.warnings,
            effectful_action_requests=self.proposals,
            tool_outcomes=tuple(r.outcome for r in self.tool_receipts),
            limitations=self.warnings)

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "specialist_id": self.specialist_id.value,
            "status": self.status.value, "summary": self.summary,
            "findings": list(self.findings), "evidence_ids": list(self.evidence_ids),
            "proposals": [p.to_dict() for p in self.proposals],
            "tool_receipts": [r.to_dict() for r in self.tool_receipts],
            "verification": self.verification.to_dict() if self.verification else None,
            "verified": self.verified,
            "warnings": list(self.warnings),
            "effective_autonomy": int(self.effective_autonomy),
            "model_selection": (self.model_selection.to_dict()
                                if self.model_selection else None),
            "attempts": self.attempts, "duration_ms": round(self.duration_ms, 1),
            "executed_effects": self.executed_effects,
            "deduplicated_effects": self.deduplicated_effects,
            "denied_tools": self.denied_tools,
            "body_safe_trace": list(self.body_safe_trace),
            "timestamp": self.timestamp,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Observability (§33) — counters only, never a payload
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class ExecutionCounters:
    executions: int = 0
    succeeded: int = 0
    denied: int = 0
    failed: int = 0
    timed_out: int = 0
    cancelled: int = 0
    policy_denials: int = 0
    tool_intents: int = 0
    effects_executed: int = 0
    effects_deduplicated: int = 0
    hitl_requested: int = 0
    hitl_denied: int = 0
    model_fallbacks: int = 0
    argus_verdicts: "dict[str, int]" = field(default_factory=dict)

    def record(self, result: SpecialistExecutionResult) -> None:
        self.executions += 1
        if result.status is ExecutionStatus.SUCCESS:
            self.succeeded += 1
        elif result.status is ExecutionStatus.DENIED:
            self.denied += 1
        elif result.status is ExecutionStatus.TIMED_OUT:
            self.timed_out += 1
        elif result.status is ExecutionStatus.CANCELLED:
            self.cancelled += 1
        elif result.status is ExecutionStatus.FAILED:
            self.failed += 1
        self.tool_intents += len(result.tool_receipts)
        self.effects_executed += result.executed_effects
        self.effects_deduplicated += result.deduplicated_effects
        self.policy_denials += result.denied_tools
        if result.model_selection is not None and result.model_selection.fallback_used:
            self.model_fallbacks += 1
        if result.verification is not None:
            key = result.verification.verdict.value
            self.argus_verdicts[key] = self.argus_verdicts.get(key, 0) + 1

    def to_dict(self) -> dict:
        return {
            "executions": self.executions, "succeeded": self.succeeded,
            "denied": self.denied, "failed": self.failed,
            "timed_out": self.timed_out, "cancelled": self.cancelled,
            "policy_denials": self.policy_denials,
            "tool_intents": self.tool_intents,
            "effects_executed": self.effects_executed,
            "effects_deduplicated": self.effects_deduplicated,
            "hitl_requested": self.hitl_requested, "hitl_denied": self.hitl_denied,
            "model_fallbacks": self.model_fallbacks,
            "argus_verdicts": dict(sorted(self.argus_verdicts.items())),
        }

    def reset(self) -> None:
        for name, value in list(vars(self).items()):
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                setattr(self, name, 0)
            elif isinstance(value, dict):
                value.clear()


COUNTERS = ExecutionCounters()


# ══════════════════════════════════════════════════════════════════════════════
#  Parsing a specialist's proposal — the ONLY thing its text is read for
# ══════════════════════════════════════════════════════════════════════════════
#: A specialist proposes tools in one strict shape. Anything else in its output
#: is prose and is treated as prose. There is deliberately no key for autonomy,
#: scope, approval or verification: a specialist cannot write a field that does
#: not exist in the grammar its output is read through.
_INTENT_MARKER = re.compile(r"TOOL_INTENT\s*:\s*(?=\{)", re.IGNORECASE)

_ALLOWED_INTENT_KEYS = frozenset({
    "tool", "tool_input", "why", "hypothesis", "target",
    "expected_evidence", "read_only_alternative",
})


def _intent_blocks(text: str, limit: int) -> "tuple[tuple[str, int, int], ...]":
    r"""Find each ``TOOL_INTENT:`` object by BALANCING braces, not by regex.

    A non-greedy ``\{.*?\}`` stops at the first closing brace, which for a
    perfectly ordinary intent — every one that carries a ``tool_input`` object —
    is the INNER one. The block then fails to parse and the request is silently
    dropped, so the specialist appears to have asked for nothing. Measured, not
    theorised: it is what the first end-to-end run of this module did.

    String-aware, so a brace inside a quoted value does not close the object.
    """
    out: list[tuple[str, int, int]] = []
    for marker in _INTENT_MARKER.finditer(text or ""):
        start = marker.end()
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    out.append((text[start:index + 1], marker.start(), index + 1))
                    break
        if len(out) >= limit:
            break
    return tuple(out)


def _strip_intents(text: str) -> str:
    """The specialist's prose with every intent block removed."""
    blocks = _intent_blocks(text or "", limit=MAX_INTENTS)
    if not blocks:
        return (text or "").strip()
    kept: list[str] = []
    cursor = 0
    for _, begin, end in blocks:
        kept.append((text or "")[cursor:begin])
        cursor = end
    kept.append((text or "")[cursor:])
    return "".join(kept).strip()


def parse_tool_intents(text: str, *, limit: int = MAX_INTENTS
                       ) -> "tuple[tuple[ToolIntent, ...], tuple[str, ...]]":
    """Extract tool intents from a specialist's output.

    Returns the intents and any warnings. Unknown keys are DROPPED rather than
    passed through, so a model that emits ``"autonomy_level": 4`` alongside a
    legitimate tool request gets its tool request and nothing else — the field
    never reaches a constructor that has a place for it.
    """
    intents: list[ToolIntent] = []
    warnings: list[str] = []
    for raw, _, _ in _intent_blocks(text or "", limit):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            warnings.append("a malformed TOOL_INTENT block was discarded")
            continue
        if not isinstance(payload, dict):
            warnings.append("a non-object TOOL_INTENT was discarded")
            continue
        rejected = sorted(set(payload) - _ALLOWED_INTENT_KEYS)
        if rejected:
            warnings.append(
                "ignored field(s) a specialist may not set: " + ",".join(rejected))
        tool = str(payload.get("tool", "")).strip()
        if not tool:
            warnings.append("a TOOL_INTENT naming no tool was discarded")
            continue
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        intents.append(ToolIntent(
            tool=tool, tool_input=tool_input,
            why=str(payload.get("why", ""))[:300],
            hypothesis=str(payload.get("hypothesis", ""))[:300],
            target=(str(payload["target"])[:200] if payload.get("target") else None),
            expected_evidence=str(payload.get("expected_evidence", ""))[:200],
            read_only_alternative=str(payload.get("read_only_alternative", ""))[:200],
        ))
    return tuple(intents), tuple(warnings)


def summarise_output(text: str) -> "tuple[str, tuple[str, ...]]":
    """Split a specialist's output into a summary and bounded findings.

    Pure string work. It never interprets the text as an instruction — content
    that reached the specialist from an untrusted source was already screened by
    the injection firewall on the way IN, and what comes back out is recorded as
    a MODEL_ASSERTED observation, which by construction corroborates nothing.
    """
    body = _strip_intents(text)
    lines = [ln.strip(" -*\t") for ln in body.splitlines()]
    findings = tuple(ln for ln in lines if len(ln) > 12)[:MAX_FINDINGS]
    return _clip(body, MAX_SUMMARY), findings


# ══════════════════════════════════════════════════════════════════════════════
#  The engine (§8)
# ══════════════════════════════════════════════════════════════════════════════
class SpecialistExecutor:
    """Runs ONE specialist, once, under policy. Runs nothing else.

    Deliberately not a scheduler, not a team executive and not a planner. It has
    no concept of a second specialist, no queue, no DAG and no delegation — those
    are M65B's, and their absence here is what keeps M65A a spine.

    Dependency-injected end to end (``infer``, ``tool_executor``, ``scopes``,
    ``approvals``, ``role_router``) so every control in this class is testable
    without a model, a socket or an Ollama host. A control that only holds when a
    model cooperates is not a control.
    """

    def __init__(self, *, infer=None, tool_executor=None, scopes=None,
                 world_state=None, approvals: "HitlApprovalRegistry | None" = None,
                 role_router: "ModelRoleRouter | None" = None,
                 counters: "ExecutionCounters | None" = None) -> None:
        self._infer = infer
        self._tool_executor = tool_executor
        self._scopes = scopes
        self._world = world_state
        self._approvals = approvals if approvals is not None else APPROVALS
        self._router = role_router if role_router is not None else _default_role_router
        self._counters = counters if counters is not None else COUNTERS

    # ── production wiring, mirroring SpecialistTeamRuntime.attach ───────────
    def attach(self, *, infer=None, tool_executor=None, scopes=None,
               world_state=None, role_router=None) -> None:
        if infer is not None:
            self._infer = infer
        if tool_executor is not None:
            self._tool_executor = tool_executor
        if scopes is not None:
            self._scopes = scopes
        if world_state is not None:
            self._world = world_state
        if role_router is not None:
            self._router = role_router
        logger.info("M65A: SpecialistExecutor attached (infer=%s executor=%s)",
                    self._infer is not None, self._tool_executor is not None)

    @property
    def available(self) -> bool:
        """Whether a specialist execution can actually run here (§34)."""
        return self._infer is not None

    # ── authority, recomputed from the registry on every call ───────────────
    def _effective_ceiling(self, request: SpecialistExecutionRequest
                           ) -> "tuple[AutonomyLevel, tuple[str, ...]]":
        """The ceiling that actually applies, and why.

        The MINIMUM of what the caller asked for and what the registry allows.
        Taking the minimum rather than the request's value is the whole control:
        a request is data, and data cannot raise a ceiling. A caller may lower
        one — a read-only sub-task legitimately runs at OBSERVE even for a
        specialist whose contract permits more.
        """
        record = REGISTRY.get(request.specialist_id)
        registry_ceiling = record.default_autonomy
        trace: list[str] = [
            f"registry ceiling L{int(registry_ceiling)} for {record.codename}"]
        if registry_ceiling is AutonomyLevel.PROHIBITED:
            # §9 L4 — PROHIBITED denies on BOTH sides in ``permits``; naming it
            # here makes the refusal legible rather than merely arithmetic.
            trace.append("registry ceiling is PROHIBITED: nothing is executable")
            return AutonomyLevel.PROHIBITED, tuple(trace)
        requested = request.autonomy_level
        if requested is AutonomyLevel.PROHIBITED:
            trace.append("request asked for PROHIBITED: refused")
            return AutonomyLevel.PROHIBITED, tuple(trace)
        effective = min(registry_ceiling, requested, key=int)
        if int(requested) > int(registry_ceiling):
            trace.append(
                f"request asked L{int(requested)}; the registry allows "
                f"L{int(registry_ceiling)} and the registry wins")
        trace.append(f"effective ceiling L{int(effective)}")
        return effective, tuple(trace)

    def _scope_lift(self, request: SpecialistExecutionRequest,
                    ceiling: AutonomyLevel
                    ) -> "tuple[AutonomyLevel, SecurityScopeDecision | None, tuple[str, ...]]":
        """Apply an operator-registered security scope, if one genuinely covers
        this activity and target.

        With no scope the routed ceiling stands, so the lift is never a default,
        and ``ceiling_with_scope`` is the only way to obtain one — a specialist
        cannot write the scope registry.
        """
        record = REGISTRY.get(request.specialist_id)
        if not record.requires_security_scope or request.activity is None:
            return ceiling, None, ()
        from core.security_scope import authorize_security_activity

        target = request.authorized_scope[0] if request.authorized_scope else None
        if not record.permits_activity(request.activity):
            decision = SecurityScopeDecision(
                allowed=False, activity=request.activity.value, target=target,
                reason=(f"{record.codename}'s contract does not include activity "
                        f"'{request.activity.value}'"))
            return AutonomyLevel.ADVISE, decision, (
                "specialist contract excludes the requested activity",)
        decision = authorize_security_activity(
            self._scopes, activity=request.activity, target=target)
        if not decision.allowed:
            # §11 — no security scope means analysis / defensive advice only.
            return AutonomyLevel.ADVISE, decision, (
                f"no authorized scope: {decision.denial.value if decision.denial else 'refused'}"
                " — analysis only",)
        # The lift is clamped by what the CALLER asked for, not by the pre-lift
        # ceiling. Clamping by the pre-lift value would be self-defeating: for
        # every specialist that actually declares a ``scoped_autonomy`` the
        # pre-lift ceiling IS ``default_autonomy``, so the min would always
        # return it and a registered scope would lift nothing. Measured, not
        # theorised — it is what the first run of the scope tests showed.
        # This mirrors ``CognitiveOrchestrator.effective_ceiling`` exactly.
        lifted = min(record.ceiling_with_scope(True), request.autonomy_level,
                     key=int)
        return lifted, decision, (
            f"scope '{decision.scope_id}' grants {request.activity.value}; "
            f"ceiling L{int(lifted)}",)

    # ── the one tool path (§19) ─────────────────────────────────────────────
    async def _run_intent(self, intent: ToolIntent, request: SpecialistExecutionRequest,
                          *, ceiling: AutonomyLevel, graph: EvidenceGraph,
                          scope_decision: "SecurityScopeDecision | None",
                          ) -> ToolReceipt:
        """Take one intent from proposal to receipt.

        The order is the canonical pipeline and there is no branch that skips a
        step:

            allowlist -> preflight (capability/scope/risk/autonomy)
                      -> HITL approval when L3
                      -> ToolBroker -> ToolExecutor.aexecute
                      -> receipt

        Every early return is a DENIED receipt, which is itself evidence: a
        refusal is a fact about the request and is recorded as one.
        """
        epoch = request.effect_epoch or f"exec:{request.execution_id}"
        identity = intent.effect_identity(epoch)
        rid = _receipt_id(request.specialist_id, identity)
        risk = classify_tool(intent.tool)

        def _denied(reason: str, *, scope=None) -> ToolReceipt:
            return ToolReceipt(
                receipt_id=rid, tool=intent.tool,
                specialist_id=request.specialist_id,
                status=ToolCallStatus.DENIED, effect_identity=identity,
                executed=False, risk=risk.value,
                required_autonomy=AutonomyLevel.HITL_EXECUTE,
                denial_reason=reason, scope_decision=scope or scope_decision)

        # 1. The caller's allowlist. A tool the execution was not given is not
        #    reachable even if the registry's capability policy would permit it.
        if request.allowed_tools and intent.tool not in request.allowed_tools:
            return _denied(
                f"'{intent.tool}' is not in this execution's allowed tools; a "
                f"specialist cannot add one")

        # 2. The canonical preflight: capability, scope, risk, autonomy — the
        #    same function the mesh already uses. Not a copy of its logic.
        try:
            decision = preflight(
                ToolPreflight(
                    tool=intent.tool, specialist=request.specialist_id,
                    why=intent.why, hypothesis=intent.hypothesis,
                    target=intent.target,
                    expected_evidence=intent.expected_evidence,
                    read_only_alternative=intent.read_only_alternative),
                ceiling=ceiling, scopes=self._scopes, world_state=self._world,
                activity=intent.activity or request.activity)
        except Exception as exc:  # noqa: BLE001 — a broken gate denies.
            logger.warning("M65A: preflight raised (%s) — intent denied", exc)
            return _denied(f"preflight failed closed: {type(exc).__name__}")
        if not decision.allowed:
            return _denied(decision.reason, scope=decision.scope_decision)

        # 3. HITL. An action needing L3 needs a human decision bound to THIS
        #    effect — not a general willingness to approve things.
        approval_id: str | None = None
        if decision.required_autonomy >= AutonomyLevel.HITL_EXECUTE:
            self._counters.hitl_requested += 1
            verdict = self._approvals.decide(request.specialist_id, identity)
            if not verdict.allowed:
                self._counters.hitl_denied += 1
                return _denied(
                    f"human approval required and not present: {verdict.reason}",
                    scope=decision.scope_decision)
            approval_id = verdict.approval_id

        # 4. The ONE effect path. ToolBroker enforces the specialist's own tool
        #    category allowlist and then delegates to ToolExecutor.aexecute,
        #    which re-applies authority, risk, LAB_ONLY, the NATO challenge and
        #    the effect ledger. Nothing here calls a handler directly.
        if self._tool_executor is None:
            return ToolReceipt(
                receipt_id=rid, tool=intent.tool,
                specialist_id=request.specialist_id,
                status=ToolCallStatus.UNAVAILABLE, effect_identity=identity,
                executed=False, risk=risk.value,
                required_autonomy=decision.required_autonomy,
                denial_reason="no tool executor is wired; nothing was run",
                scope_decision=decision.scope_decision)

        broker = ToolBroker(self._tool_executor,
                            spec_for(REGISTRY.runtime_role(request.specialist_id)))
        before = self._effect_count(intent.tool)
        started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                broker.call(intent.tool, dict(intent.tool_input),
                            f"specialist execution {request.execution_id}"),
                timeout=request.deadline_s)
        except asyncio.TimeoutError:
            return ToolReceipt(
                receipt_id=rid, tool=intent.tool,
                specialist_id=request.specialist_id,
                status=ToolCallStatus.TIMEOUT, effect_identity=identity,
                executed=False, risk=risk.value,
                required_autonomy=decision.required_autonomy,
                denial_reason="the tool call exceeded this execution's deadline",
                elapsed_s=time.monotonic() - started,
                scope_decision=decision.scope_decision,
                hitl_approval_id=approval_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("M65A: tool '%s' raised (%s)", intent.tool,
                           type(exc).__name__)
            return ToolReceipt(
                receipt_id=rid, tool=intent.tool,
                specialist_id=request.specialist_id,
                status=ToolCallStatus.FAILURE, effect_identity=identity,
                executed=False, risk=risk.value,
                required_autonomy=decision.required_autonomy,
                denial_reason=f"{type(exc).__name__}",
                elapsed_s=time.monotonic() - started,
                scope_decision=decision.scope_decision,
                hitl_approval_id=approval_id)
        elapsed = time.monotonic() - started

        # 5. The receipt reflects what the EXECUTOR did, measured from its own
        #    ledger rather than from the tool's return value. An effect the
        #    ledger suppressed is reported as deduplicated and — crucially —
        #    still returns the recorded result, which is what makes a retry
        #    safe rather than merely refused.
        after = self._effect_count(intent.tool)
        deduped = (risk is not RiskClass.READ_ONLY and after == before)
        status, denial = self._classify_output(output)
        summary = self._body_safe_summary(output)
        return ToolReceipt(
            receipt_id=rid, tool=intent.tool,
            specialist_id=request.specialist_id, status=status,
            effect_identity=identity,
            executed=status in (ToolCallStatus.SUCCESS, ToolCallStatus.PARTIAL),
            deduplicated=deduped and status is ToolCallStatus.SUCCESS,
            risk=risk.value, required_autonomy=decision.required_autonomy,
            denial_reason=denial, summary=summary, elapsed_s=elapsed,
            scope_decision=decision.scope_decision, hitl_approval_id=approval_id)

    def _effect_count(self, tool: str) -> int:
        """The executor's own effect count for *tool*, or -1 when unknowable.

        Derived from ``ToolExecutor.effect_count``, which reads the ledger. A
        caller cannot under-report by forgetting to increment, and a wired
        executor that does not expose the method simply yields -1 — which never
        compares equal to a later count, so an unknown count is never mistaken
        for "deduplicated".
        """
        counter = getattr(self._tool_executor, "effect_count", None)
        if not callable(counter):
            return -1
        try:
            return int(counter(tool))
        except Exception:  # noqa: BLE001
            return -1

    @staticmethod
    def _classify_output(output) -> "tuple[ToolCallStatus, str]":
        """What actually happened, read from the executor's own envelope.

        ``ToolExecutor`` signals refusal and failure by returning a dict with an
        ``error`` key; ``core.tool_result.make_failure`` adds a typed envelope.
        Neither is ever a SUCCESS, and there is no branch that treats an
        unrecognised shape as one.
        """
        if isinstance(output, dict):
            err = output.get("error")
            if err:
                text = str(err)
                lowered = text.lower()
                if "cancel" in lowered or "alcance" in lowered or "denied" in lowered:
                    return ToolCallStatus.DENIED, text[:300]
                return ToolCallStatus.FAILURE, text[:300]
            if output.get("ok") is False:
                return ToolCallStatus.FAILURE, "tool reported ok=false"
            return ToolCallStatus.SUCCESS, ""
        if output is None:
            return ToolCallStatus.FAILURE, "the tool returned nothing"
        return ToolCallStatus.SUCCESS, ""

    @staticmethod
    def _body_safe_summary(output) -> str:
        """A bounded, structural rendering of a tool result.

        Keys and short scalar values only. It is what a receipt may carry into a
        log and an evidence graph without becoming a place a payload or a secret
        could hide.
        """
        try:
            text = json.dumps(output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(output)
        return _clip(text, MAX_TOOL_OUTPUT)

    # ── the run (§8) ────────────────────────────────────────────────────────
    async def run(self, request: SpecialistExecutionRequest, *,
                  graph: "EvidenceGraph | None" = None,
                  verify_with_argus: bool = False,
                  cancelled=None) -> SpecialistExecutionResult:
        """Execute one specialist under policy and return a typed result.

        Never raises. Every failure shape — an unregistered specialist, no
        backend, a model timeout, a broken gate — becomes a status in the
        vocabulary §31 defines, because a caller that has to catch exceptions to
        find out whether a specialist ran will eventually forget to.
        """
        started = time.monotonic()
        graph = graph if graph is not None else EvidenceGraph()
        trace: list[str] = []
        warnings: list[str] = []

        def _finish(status: ExecutionStatus, **kw) -> SpecialistExecutionResult:
            result = SpecialistExecutionResult(
                execution_id=request.execution_id,
                specialist_id=request.specialist_id, status=status,
                duration_ms=(time.monotonic() - started) * 1000.0,
                body_safe_trace=tuple(trace),
                warnings=tuple(warnings), **kw)
            self._counters.record(result)
            logger.info(
                "M65A: execution %s specialist=%s status=%s effects=%d "
                "deduped=%d denied=%d ms=%.0f",
                request.execution_id, request.specialist_id.value, status.value,
                result.executed_effects, result.deduplicated_effects,
                result.denied_tools, result.duration_ms)
            return result

        # 0. Identity. An unregistered specialist executes nothing.
        if request.specialist_id not in REGISTRY:
            trace.append("specialist is not in the registry")
            return _finish(ExecutionStatus.DENIED,
                           summary="This specialist is not registered.")

        # 1. Authority, from the registry — never from the request alone.
        ceiling, ceiling_trace = self._effective_ceiling(request)
        trace.extend(ceiling_trace)
        ceiling, scope_decision, scope_trace = self._scope_lift(request, ceiling)
        trace.extend(scope_trace)
        if ceiling is AutonomyLevel.PROHIBITED:
            # §9 L4 — always denied, before any model is asked anything.
            return _finish(ExecutionStatus.DENIED, effective_autonomy=ceiling,
                           summary=("This specialist is prohibited from executing; "
                                    "no model was consulted."))

        # 2. Model role. Real, observable, and privilege-free.
        selection = self._router.select(
            request.specialist_id, preferred=request.model_role,
            task_class=request.task_class)
        trace.append(
            f"model role: {selection.selected_role.value if selection.selected_role else 'none'}"
            f" on '{selection.backend or 'unavailable'}'"
            + (f" (fallback: {selection.fallback_reason[:80]})"
               if selection.fallback_used else ""))
        if not selection.allowed:
            warnings.append(selection.fallback_reason)
            return _finish(ExecutionStatus.FAILED, effective_autonomy=ceiling,
                           model_selection=selection,
                           summary=("No model backend is available for this "
                                    "specialist; nothing was executed."))
        if selection.fallback_used:
            warnings.append(selection.fallback_reason)

        if cancelled is not None and cancelled():
            return _finish(ExecutionStatus.CANCELLED, effective_autonomy=ceiling,
                           model_selection=selection,
                           summary="Cancelled before execution.")

        # 3. Reason. Bounded, retried finitely, and never unbounded.
        if self._infer is None:
            trace.append("no inference backend wired")
            return _finish(ExecutionStatus.FAILED, effective_autonomy=ceiling,
                           model_selection=selection,
                           summary="No inference backend is wired.")
        text, attempts, infer_status = await self._infer_bounded(
            request, selection, ceiling)
        trace.append(f"inference attempts={attempts} status={infer_status.value}")
        if infer_status is ExecutionStatus.TIMED_OUT:
            return _finish(ExecutionStatus.TIMED_OUT, attempts=attempts,
                           effective_autonomy=ceiling, model_selection=selection,
                           summary="The specialist did not answer within its deadline.")
        if infer_status is ExecutionStatus.FAILED:
            return _finish(ExecutionStatus.FAILED, attempts=attempts,
                           effective_autonomy=ceiling, model_selection=selection,
                           summary="The specialist's backend failed.")

        # 4. Read the output for intents ONLY. Its prose is an observation.
        summary, findings = summarise_output(text)
        intents, parse_warnings = parse_tool_intents(text)
        warnings.extend(parse_warnings)
        for w in parse_warnings:
            trace.append(w)

        evidence_ids: list[str] = []
        assertion = EvidenceRef(
            content=summary, provenance=Provenance.MODEL_ASSERTED,
            source=f"specialist:{request.specialist_id.value}",
            specialist=request.specialist_id)
        ref_id = graph.add_evidence(screen_evidence(assertion))
        if ref_id:
            evidence_ids.append(ref_id)

        # 5. Effects, one at a time, each through the canonical pipeline.
        receipts: list[ToolReceipt] = []
        for intent in intents:
            if cancelled is not None and cancelled():
                trace.append("cancelled before the remaining intents ran")
                break
            if not permits(ceiling, AutonomyLevel.OBSERVE):
                # §9 L0 — ADVISE is analysis only. No tool of any risk class.
                receipts.append(ToolReceipt(
                    receipt_id=_receipt_id(
                        request.specialist_id,
                        intent.effect_identity(request.effect_epoch or "")),
                    tool=intent.tool, specialist_id=request.specialist_id,
                    status=ToolCallStatus.DENIED,
                    effect_identity=intent.effect_identity(request.effect_epoch or ""),
                    executed=False,
                    denial_reason=("autonomy L0 ADVISE: this execution may analyse "
                                   "and may not act")))
                trace.append(f"L0 denied '{intent.tool}'")
                continue
            receipt = await self._run_intent(
                intent, request, ceiling=ceiling, graph=graph,
                scope_decision=scope_decision)
            receipts.append(receipt)
            trace.append(
                f"{receipt.tool}: {receipt.status.value}"
                + (" (deduplicated)" if receipt.deduplicated else ""))
            # Evidence from a call that ACTUALLY ran. A DENIED or TIMEOUT
            # outcome is recorded and is not corroborating, exactly as
            # ``mesh_live.record_tool_outcome`` records the primary's.
            try:
                ref = evidence_from_tool(
                    receipt.tool, receipt.status, receipt.summary,
                    specialist=request.specialist_id, elapsed_s=receipt.elapsed_s)
                rid = graph.add_evidence(screen_evidence(ref))
                if rid:
                    evidence_ids.append(rid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("M65A: evidence binding skipped (%s)", exc)

        # 6. Status, derived from what happened — never from what was said.
        status = self._status_for(receipts, bool(intents))

        # 7. ARGUS. It reports; it never grants, and it runs AFTER every effect
        #    decision has already been made, so no verdict can retroactively
        #    permit one.
        verdict: VerifierVerdict | None = None
        if verify_with_argus:
            verdict = self._verify(request, graph, receipts, status, summary,
                                   findings, evidence_ids, scope_decision)
            trace.append(f"argus: {verdict.verdict.value}")

        return _finish(
            status, summary=summary, findings=findings,
            evidence_ids=tuple(evidence_ids), tool_receipts=tuple(receipts),
            verification=verdict, effective_autonomy=ceiling,
            model_selection=selection, attempts=attempts)

    async def _infer_bounded(self, request: SpecialistExecutionRequest,
                             selection: RoleSelection, ceiling: AutonomyLevel
                             ) -> "tuple[str, int, ExecutionStatus]":
        """Call the model with a hard deadline and a finite retry count (§32).

        A retry re-enters the same objective, and any effect it proposes carries
        the same effect identity — so the ledger, not this loop, is what holds
        the effect count at one. That separation is deliberate: a retry policy
        that also had to be the deduplication policy would eventually get one of
        the two wrong.
        """
        record = REGISTRY.get(request.specialist_id)
        spec = spec_for(record.runtime_role)
        system = self._system_prompt(request, record, spec, ceiling)
        user = self._user_prompt(request)
        tier = ModelTier.DEEP if spec.is_deep else ModelTier.FAST
        attempts = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            try:
                text = await asyncio.wait_for(
                    self._infer(system, user, tier=tier,
                                timeout_s=request.deadline_s,
                                num_ctx=min(spec.context_budget,
                                            request.budget.max_context_chars),
                                temperature=spec.temperature),
                    timeout=request.deadline_s)
                return (text or ""), attempts, ExecutionStatus.SUCCESS
            except asyncio.TimeoutError:
                logger.warning("M65A: specialist %s timed out (attempt %d)",
                               request.specialist_id.value, attempt)
                if attempt >= MAX_ATTEMPTS:
                    return "", attempts, ExecutionStatus.TIMED_OUT
            except Exception as exc:  # noqa: BLE001
                logger.warning("M65A: specialist %s inference failed: %s",
                               request.specialist_id.value, type(exc).__name__)
                if attempt >= MAX_ATTEMPTS:
                    return "", attempts, ExecutionStatus.FAILED
        return "", attempts, ExecutionStatus.FAILED

    @staticmethod
    def _system_prompt(request: SpecialistExecutionRequest, record, spec,
                       ceiling: AutonomyLevel) -> str:
        """The specialist's role instructions plus its actual bounds.

        The bounds are STATED so the specialist can plan within them, and
        ENFORCED elsewhere so that stating them is not how they hold. Telling a
        model its ceiling is a courtesy; the ceiling is a property of
        ``_effective_ceiling``.
        """
        tools = ", ".join(sorted(request.allowed_tools)) or "none"
        return (
            f"{spec.system_prompt}\n\n"
            f"You are {record.codename}, {record.official_role}, working as a "
            f"supporting specialist inside JARVIS. The operator never sees you "
            f"directly; JARVIS speaks.\n"
            f"AUTONOMY: L{int(ceiling)}. "
            f"{'Analysis only — you may not act.' if int(ceiling) < 1 else ''}"
            f"{'Read-only observation is permitted.' if int(ceiling) == 1 else ''}"
            f"{'Bounded safe effects are permitted.' if int(ceiling) == 2 else ''}"
            f"{'Effects require a human approval bound to the exact action.' if int(ceiling) >= 3 else ''}\n"
            f"TOOLS YOU MAY REQUEST: {tools}\n"
            f"COMPLETION: {'; '.join(record.completion_contract[:3])}\n"
            f"STOP IF: {'; '.join(record.stop_conditions[:3])}\n\n"
            "To request a tool, emit a line of exactly this form and nothing "
            'else on it:\nTOOL_INTENT: {"tool": "<name>", "tool_input": {...}, '
            '"why": "...", "hypothesis": "..."}\n'
            "You cannot set your own autonomy, scope, capabilities or "
            "verification: those fields do not exist in what you emit, and any "
            "you invent are discarded. Report findings and their evidence; do "
            "not claim a tool result you did not receive."
        )

    @staticmethod
    def _user_prompt(request: SpecialistExecutionRequest) -> str:
        ctx = f"\n\nCONTEXT:\n{_clip(request.context, 2000)}" if request.context else ""
        return (f"OBJECTIVE: {request.objective}{ctx}\n\n"
                "Give your expert analysis. Separate observation from inference, "
                "state assumptions, and name what you could not determine.")

    @staticmethod
    def _status_for(receipts: "list[ToolReceipt]", asked_for_tools: bool
                    ) -> ExecutionStatus:
        """Derive the execution status from receipts.

        A specialist whose every tool request was refused did not succeed, even
        though it produced text — reporting SUCCESS there is exactly how a
        failed specialist would let JARVIS claim success (§31).
        """
        if not receipts:
            return ExecutionStatus.SUCCESS if not asked_for_tools \
                else ExecutionStatus.PARTIAL
        if any(r.status is ToolCallStatus.TIMEOUT for r in receipts):
            return ExecutionStatus.TIMED_OUT
        if all(r.status is ToolCallStatus.DENIED for r in receipts):
            return ExecutionStatus.DENIED
        if any(r.status in (ToolCallStatus.DENIED, ToolCallStatus.FAILURE,
                            ToolCallStatus.UNAVAILABLE) for r in receipts):
            return ExecutionStatus.PARTIAL
        return ExecutionStatus.SUCCESS

    def _verify(self, request: SpecialistExecutionRequest, graph: EvidenceGraph,
                receipts: "list[ToolReceipt]", status: ExecutionStatus,
                summary: str, findings, evidence_ids,
                scope_decision) -> VerifierVerdict:
        """Run ARGUS over this execution (§22).

        ARGUS is handed the SAME kind of input the mesh already assembles: the
        evidence graph, the typed result, the scope decisions and the receipts.
        It is not handed the specialist's prose as authority, and it is not
        asked whether the action was allowed — that was decided before it ran.
        """
        result = SpecialistResult(
            status=_RESULT_STATUS[status], specialist_id=request.specialist_id,
            task_id=request.plan_id, summary=summary, findings=tuple(findings),
            evidence_ids=tuple(evidence_ids),
            confidence=0.6 if status.succeeded else 0.2,
            tool_outcomes=tuple(r.outcome for r in receipts))
        try:
            return verify(VerificationInput(
                task_id=request.plan_id, objective=request.objective, graph=graph,
                results=(result,),
                scope_decisions=tuple(
                    d for d in ([scope_decision] if scope_decision else [])
                    + [r.scope_decision for r in receipts if r.scope_decision]),
                budget=request.budget,
                required_evidence=request.evidence_requirements,
                autonomy_ceiling=request.autonomy_level))
        except Exception as exc:  # noqa: BLE001 — a verifier that cannot run has
            # not verified anything, and must not be read as if it had.
            from core.mesh_contracts import Verdict

            logger.warning("M65A: ARGUS failed (%s)", type(exc).__name__)
            return VerifierVerdict(
                Verdict.FAILED,
                reasons=(f"the verifier could not run: {type(exc).__name__}",))

    def status(self) -> dict:
        """Body-safe runtime status (§34). Answerable without an LLM."""
        return {
            "available": self.available,
            "tool_executor_wired": self._tool_executor is not None,
            "scopes_wired": self._scopes is not None,
            "world_state_wired": self._world is not None,
            "registered_specialists": len(REGISTRY),
            "model_roles": sorted(
                r.value for r in self._router.availability.backends),
            "approvals": self._approvals.to_dict(),
            "counters": self._counters.to_dict(),
        }


#: The module singleton, bound at boot by ``mesh_live.attach_live_runtime``.
executor = SpecialistExecutor()


__all__ = [
    "APPROVALS", "COUNTERS", "MAX_ATTEMPTS", "MAX_DEADLINE_S",
    "ApprovalDecision", "ApprovalDenial", "ExecutionCounters", "ExecutionStatus",
    "HitlApproval", "HitlApprovalRegistry", "SpecialistExecutionRequest",
    "SpecialistExecutionResult", "SpecialistExecutor", "ToolIntent", "ToolReceipt",
    "executor", "parse_tool_intents", "summarise_output",
]
