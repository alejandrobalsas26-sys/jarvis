"""
core/mesh_workflows.py — V69 M64: the disciplined workflows specialists follow.

A specialist's competence is not only what it knows; it is the ORDER in which it
finds out. Each workflow here is a small, deterministic state machine that says
what step comes next and — more usefully — which step must NOT come next.

    GuardianWorkflow  triage -> ... -> containment recommendation -> HITL (§22)
    TraceWorkflow     preserve -> hash -> acquire -> timeline -> ... (§23)
    VioletLoop        technique -> emulation -> telemetry -> gap -> retest (§24)
    diagnostic ladders for HELIOS (§27) and MESH (§28)

Four properties are enforced here rather than merely described:

  * **GUARDIAN never contains.** :meth:`GuardianWorkflow.recommend_containment`
    returns an :class:`ActionRequest` whose disposition is
    ``REQUIRES_HUMAN_APPROVAL``, and there is no method on this class that
    executes anything. Severity and confidence are separate fields throughout: a
    critical-severity alert nobody has corroborated is exactly the case that must
    not auto-act.

  * **TRACE preserves before it modifies.** :func:`preservation_gate` returns
    ``EVIDENCE_PRESERVATION_REQUIRED`` for any step that would destroy an
    unacquired artefact. It is a refusal, not a warning.

  * **No blind restart, no ping spam.** The HELIOS and MESH ladders are ordered,
    and :func:`next_diagnostic_step` refuses to hand back a remediation while an
    earlier rung is unexamined.

  * **A detection is not "working" until it is retested.** :class:`VioletLoop`
    cannot report ``DETECTED`` without both an expected and an observed telemetry
    record, and cannot close a gap without a retest.

Pure and deterministic: no model, no tool, no socket.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum

from core.cognitive_mesh import AutonomyLevel, SpecialistId
from core.mesh_contracts import (
    ActionDisposition,
    ActionRequest,
    Claim,
    ClaimStatus,
    EvidenceGraph,
)

MAX_STEPS = 32
MAX_NOTE = 400


def _clip(text: str, limit: int = MAX_NOTE) -> str:
    return (text or "").strip()[:limit]


# ══════════════════════════════════════════════════════════════════════════════
#  GUARDIAN — blue team incident workflow (§22)
# ══════════════════════════════════════════════════════════════════════════════
class IncidentStage(IntEnum):
    """The ordered stages of a defensive investigation."""

    TRIAGE = 0
    VERIFY_ALERT = 1
    IDENTIFY_ASSETS = 2
    COLLECT_EVIDENCE = 3
    CORRELATE = 4
    TIMELINE = 5
    ATTCK_MAP = 6
    HYPOTHESES = 7
    VERIFY = 8
    CONTAINMENT_RECOMMENDATION = 9
    HUMAN_DECISION = 10
    RECOVERY_VALIDATION = 11


class Severity(IntEnum):
    """How bad it would be IF true. Independent of whether it is."""

    INFORMATIONAL = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class Confidence(IntEnum):
    """How well supported the claim is. Independent of how bad it would be.

    Kept as a separate ladder from :class:`Severity` on purpose (§22). The
    failure this prevents is the one the audit found live in ``correlator.py``,
    where a severity score alone triggers a real firewall block: severity is a
    consequence estimate, and acting on it without confidence is acting on a
    guess about something expensive.
    """

    UNCONFIRMED = 0
    WEAK = 1
    MODERATE = 2
    STRONG = 3
    CONFIRMED = 4


@dataclass(frozen=True)
class IncidentStep:
    stage: IncidentStage
    note: str = ""
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"stage": self.stage.name, "note": _clip(self.note),
                "evidence_ids": list(self.evidence_ids)}


@dataclass
class GuardianWorkflow:
    """One defensive investigation, advanced one stage at a time.

    There is deliberately no ``contain()``, no ``isolate()`` and no ``block()``.
    The most this class can do about a threat is *ask*, and asking produces an
    :class:`ActionRequest` that a human must approve before it reaches the
    executor's own gate.
    """

    incident_id: str
    severity: Severity = Severity.INFORMATIONAL
    confidence: Confidence = Confidence.UNCONFIRMED
    steps: list[IncidentStep] = field(default_factory=list)
    attck_techniques: tuple[str, ...] = ()

    @property
    def stage(self) -> IncidentStage:
        return self.steps[-1].stage if self.steps else IncidentStage.TRIAGE

    @property
    def completed(self) -> frozenset[IncidentStage]:
        return frozenset(s.stage for s in self.steps)

    def advance(self, stage: IncidentStage, note: str = "",
                evidence_ids: "tuple[str, ...]" = ()) -> tuple[bool, str]:
        """Record *stage*. Stages are ordered and none is skipped: an
        investigation that jumps from triage to containment has not investigated.
        """
        if len(self.steps) >= MAX_STEPS:
            return False, "step budget exhausted"
        missing = [s for s in IncidentStage if s < stage and s not in self.completed]
        if missing:
            return False, (f"{stage.name} cannot follow {self.stage.name}: "
                           f"{', '.join(m.name for m in missing)} not done")
        self.steps.append(IncidentStep(stage, _clip(note), tuple(evidence_ids[:8])))
        return True, f"{stage.name} recorded"

    def assess(self, severity: Severity, confidence: Confidence) -> None:
        """Set severity and confidence. Two fields, always, never a blended score."""
        self.severity = severity
        self.confidence = confidence

    def containment_ready(self) -> tuple[bool, str]:
        """Whether a containment recommendation is warranted YET.

        Both ladders must be high: a CRITICAL severity at UNCONFIRMED confidence
        is the false positive that takes a production host offline.
        """
        if IncidentStage.VERIFY not in self.completed:
            return False, "the alert has not been verified against evidence"
        if int(self.confidence) < int(Confidence.MODERATE):
            return False, (f"confidence is {self.confidence.name}; severity "
                           f"{self.severity.name} alone does not justify containment")
        if int(self.severity) < int(Severity.MEDIUM):
            return False, f"severity is {self.severity.name}; containment is disproportionate"
        return True, (f"severity {self.severity.name} at confidence "
                      f"{self.confidence.name}, verified against evidence")

    def recommend_containment(
        self, *, action: str, target: str, justification: str,
        evidence_ids: "tuple[str, ...]" = (), rollback_plan: str = "",
    ) -> ActionRequest:
        """Produce a containment PROPOSAL.

        Always ``REQUIRES_HUMAN_APPROVAL`` when the evidence supports it, and
        ``REFUSED_NO_EVIDENCE`` when it does not. There is no branch of this
        method that executes, and none that returns ``APPROVED_FOR_EXECUTOR``:
        containment is effectful and a human decides, every time.
        """
        ready, reason = self.containment_ready()
        request = ActionRequest(
            action=action, target=target,
            justification=_clip(justification, 600),
            requested_by=SpecialistId.GUARDIAN,
            evidence_ids=tuple(evidence_ids), risk="high_impact",
            reversible=bool(rollback_plan.strip()),
            rollback_plan=_clip(rollback_plan),
            required_capability="containment",
            required_autonomy=AutonomyLevel.HITL_EXECUTE,
        )
        if not ready:
            return request.decide(ActionDisposition.REFUSED_NO_EVIDENCE, reason)
        return request.decide(ActionDisposition.REQUIRES_HUMAN_APPROVAL, reason)

    def to_dict(self) -> dict:
        ready, reason = self.containment_ready()
        return {
            "incident_id": self.incident_id, "stage": self.stage.name,
            "severity": self.severity.name, "confidence": self.confidence.name,
            "steps": [s.to_dict() for s in self.steps],
            "attck_techniques": list(self.attck_techniques),
            "containment_ready": ready, "containment_reason": reason,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  TRACE — DFIR workflow (§23)
# ══════════════════════════════════════════════════════════════════════════════
class ForensicStage(IntEnum):
    PRESERVE = 0
    HASH = 1
    ACQUIRE = 2
    TIMELINE = 3
    CORRELATE = 4
    ANALYZE = 5
    REPORT = 6


EVIDENCE_PRESERVATION_REQUIRED = "EVIDENCE_PRESERVATION_REQUIRED"

#: Actions that destroy or alter an artefact a later step would have needed.
#: Matched as whole words against a proposed action, so "restart" fires and
#: "restart_policy_review" does not.
_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "reboot", "restart", "shutdown", "poweroff", "kill", "terminate", "delete",
    "remove", "wipe", "format", "reimage", "reinstall", "clear", "flush",
    "truncate", "rotate", "quarantine", "isolate", "disable", "uninstall",
    "clean", "remediate", "patch", "update",
})


@dataclass
class TraceWorkflow:
    """One evidence-preservation workflow. Preserve before modify, always."""

    case_id: str
    acquired: set[str] = field(default_factory=set)
    stages: list[ForensicStage] = field(default_factory=list)

    @property
    def stage(self) -> ForensicStage:
        return self.stages[-1] if self.stages else ForensicStage.PRESERVE

    def advance(self, stage: ForensicStage) -> tuple[bool, str]:
        missing = [s for s in ForensicStage if s < stage and s not in set(self.stages)]
        if missing:
            return False, (f"{stage.name} cannot follow {self.stage.name}: "
                           f"{', '.join(m.name for m in missing)} not done")
        if len(self.stages) >= MAX_STEPS:
            return False, "stage budget exhausted"
        self.stages.append(stage)
        return True, f"{stage.name} recorded"

    def record_acquisition(self, artefact: str) -> bool:
        if not artefact or len(self.acquired) >= 256:
            return False
        self.acquired.add(artefact.strip().lower())
        return True

    def acquisition_complete(self, required: "tuple[str, ...]") -> bool:
        return all(a.strip().lower() in self.acquired for a in required)

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "stage": self.stage.name,
                "acquired": sorted(self.acquired),
                "stages": [s.name for s in self.stages]}


def preservation_gate(
    action: str, *, workflow: TraceWorkflow | None = None,
    required_artefacts: "tuple[str, ...]" = (),
) -> tuple[bool, str]:
    """Whether *action* may proceed without destroying unacquired evidence (§23).

    Returns ``(False, EVIDENCE_PRESERVATION_REQUIRED: ...)`` for a destructive
    action taken before acquisition is complete. A refusal, not a warning: the
    caller cannot proceed by ignoring a log line.
    """
    words = {w.strip("_-").lower() for w in (action or "").replace("-", " ")
             .replace("_", " ").split()}
    destructive = sorted(words & _DESTRUCTIVE_ACTIONS)
    if not destructive:
        return True, "action does not destroy evidence"
    if workflow is None:
        return False, (f"{EVIDENCE_PRESERVATION_REQUIRED}: '{action}' is destructive "
                       f"({', '.join(destructive)}) and no acquisition has been recorded")
    if not required_artefacts:
        if ForensicStage.ACQUIRE not in set(workflow.stages):
            return False, (f"{EVIDENCE_PRESERVATION_REQUIRED}: '{action}' is destructive "
                           f"({', '.join(destructive)}) and acquisition has not run")
        return True, "acquisition stage complete"
    missing = [a for a in required_artefacts
               if a.strip().lower() not in workflow.acquired]
    if missing:
        return False, (f"{EVIDENCE_PRESERVATION_REQUIRED}: '{action}' would destroy "
                       f"{', '.join(missing)}, which has not been acquired")
    return True, "every required artefact is acquired"


# ══════════════════════════════════════════════════════════════════════════════
#  VIOLET — purple-team feedback loop (§24)
# ══════════════════════════════════════════════════════════════════════════════
class DetectionStatus(str, Enum):
    NOT_TESTED = "not_tested"
    DETECTED = "detected"
    MISSED = "missed"
    PARTIAL = "partial"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class PurpleCycle:
    """One technique put through the loop, with its measured outcome."""

    technique: str                      # e.g. "T1021.002"
    hypothesis: str
    emulation_scope_id: str | None = None
    expected_telemetry: tuple[str, ...] = ()
    observed_telemetry: tuple[str, ...] = ()
    detection_rule: str = ""
    status: DetectionStatus = DetectionStatus.NOT_TESTED
    alert_latency_s: float | None = None
    false_positive_notes: str = ""
    gap: str = ""
    remediation: str = ""
    retested: bool = False
    retest_status: DetectionStatus = DetectionStatus.NOT_TESTED

    def measure(self) -> "PurpleCycle":
        """Derive the detection status from the telemetry actually recorded.

        Never from a claim that the rule works. With no expectation there is
        nothing to measure against, and with no observation there is nothing
        measured — both are INCONCLUSIVE, which is a real answer.
        """
        if not self.expected_telemetry or not self.observed_telemetry:
            return replace(self, status=DetectionStatus.INCONCLUSIVE)
        expected = {t.strip().lower() for t in self.expected_telemetry}
        observed = {t.strip().lower() for t in self.observed_telemetry}
        hit = expected & observed
        if hit == expected:
            return replace(self, status=DetectionStatus.DETECTED)
        if hit:
            return replace(self, status=DetectionStatus.PARTIAL)
        return replace(self, status=DetectionStatus.MISSED)

    def gap_closed(self) -> tuple[bool, str]:
        """Whether the improvement may be reported as working (§25).

        A detection is not "working" because a rule was written. It is working
        when a retest observed it fire.
        """
        if self.status is DetectionStatus.DETECTED and not self.gap:
            return True, "no gap: the technique was detected on the first pass"
        if not self.remediation.strip():
            return False, "a gap is recorded and no remediation is proposed"
        if not self.retested:
            return False, ("a remediation is proposed but not retested; a detection "
                           "is not claimed to work until a retest observed it fire")
        if self.retest_status is not DetectionStatus.DETECTED:
            return False, (f"the retest returned {self.retest_status.value}; "
                           f"the gap remains open")
        return True, "the retest observed the detection fire"

    def to_dict(self) -> dict:
        closed, reason = self.gap_closed()
        return {
            "technique": self.technique, "hypothesis": _clip(self.hypothesis),
            "emulation_scope_id": self.emulation_scope_id,
            "expected_telemetry": list(self.expected_telemetry),
            "observed_telemetry": list(self.observed_telemetry),
            "detection_rule": _clip(self.detection_rule),
            "status": self.status.value, "alert_latency_s": self.alert_latency_s,
            "false_positive_notes": _clip(self.false_positive_notes),
            "gap": _clip(self.gap), "remediation": _clip(self.remediation),
            "retested": self.retested, "retest_status": self.retest_status.value,
            "gap_closed": closed, "gap_closed_reason": reason,
        }


@dataclass
class VioletLoop:
    """A bounded set of purple cycles for one exercise."""

    exercise_id: str
    cycles: list[PurpleCycle] = field(default_factory=list)

    def add(self, cycle: PurpleCycle) -> bool:
        if len(self.cycles) >= MAX_STEPS:
            return False
        self.cycles.append(cycle.measure())
        return True

    def coverage(self) -> dict:
        total = len(self.cycles)
        detected = sum(1 for c in self.cycles if c.status is DetectionStatus.DETECTED)
        missed = sum(1 for c in self.cycles if c.status is DetectionStatus.MISSED)
        return {
            "techniques": total, "detected": detected, "missed": missed,
            "partial": sum(1 for c in self.cycles
                           if c.status is DetectionStatus.PARTIAL),
            "inconclusive": sum(1 for c in self.cycles
                                if c.status is DetectionStatus.INCONCLUSIVE),
            "open_gaps": sum(1 for c in self.cycles if not c.gap_closed()[0]),
        }

    def to_dict(self) -> dict:
        return {"exercise_id": self.exercise_id,
                "cycles": [c.to_dict() for c in self.cycles],
                "coverage": self.coverage()}


# ══════════════════════════════════════════════════════════════════════════════
#  HELIOS and MESH — ordered diagnostic ladders (§27, §28)
# ══════════════════════════════════════════════════════════════════════════════
#: HELIOS: World State first, remediation last. "No blind restart" is enforced by
#: REMEDIATION being unreachable while an earlier rung is unexamined.
HELIOS_LADDER: tuple[str, ...] = (
    "world_state", "service_state", "dependency_state", "resource_state",
    "logs", "remediation",
)

#: MESH: bottom-up. "Avoid random ping/traceroute spam" is enforced the same way
#: — TRANSPORT is not reachable while ADDRESSING is unexamined.
MESH_LADDER: tuple[str, ...] = (
    "configuration", "interface_link", "addressing", "l2", "routing", "dns",
    "transport", "application",
)


def next_diagnostic_step(ladder: "tuple[str, ...]",
                         examined: "frozenset[str] | set[str]") -> str | None:
    """The next rung to examine, or ``None`` when the ladder is exhausted."""
    for rung in ladder:
        if rung not in examined:
            return rung
    return None


def diagnostic_gate(ladder: "tuple[str, ...]", step: str,
                    examined: "frozenset[str] | set[str]") -> tuple[bool, str]:
    """Whether *step* may be taken now (§27, §28).

    Refuses a step that skips an unexamined earlier rung. This is what turns "no
    blind restart" and "no ping spam" from advice into behaviour: the remediation
    is simply not reachable until the diagnosis under it has been done.
    """
    if step not in ladder:
        return False, f"'{step}' is not a rung of this ladder"
    index = ladder.index(step)
    missing = [r for r in ladder[:index] if r not in examined]
    if missing:
        return False, (f"'{step}' skips {', '.join(missing)}; diagnose in order "
                       f"rather than acting on the first plausible cause")
    return True, f"'{step}' is the next unexamined rung"


def record_severity_claim(graph: EvidenceGraph, workflow: GuardianWorkflow,
                          statement: str, evidence_ids: "tuple[str, ...]") -> str | None:
    """Bind a GUARDIAN finding to the graph with its confidence carried across.

    The claim's confidence comes from the workflow's :class:`Confidence` ladder,
    not from its :class:`Severity`. Two specialists reading the resulting claim
    therefore see how well supported it is, never how alarming it would be.
    """
    return graph.add_claim(Claim(
        statement=statement, author=SpecialistId.GUARDIAN,
        evidence_ids=tuple(evidence_ids),
        status=ClaimStatus.UNVERIFIED,
        confidence=round(int(workflow.confidence) / 4.0, 2),
        high_impact=int(workflow.severity) >= int(Severity.HIGH),
    ))
