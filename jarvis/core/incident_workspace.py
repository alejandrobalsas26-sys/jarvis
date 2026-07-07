"""
core/incident_workspace.py — V66 Milestone 22: evidence-backed incident case.

JARVIS already has CompoundIncident (core.correlator), incident_reporter,
ir_reporter, incident persistence (intel_fusion) and alert persistence
(db_manager). This module is NOT a second incident system — it is the canonical
**Incident Case facade** that ingests those existing objects and holds the
structured *investigation state* they never modeled: a timeline, hypotheses,
open questions, decisions, containment proposals, executed actions and
verification results, all with provenance.

The central safety law: **a containment proposal NEVER executes because an
incident exists.** Creating a case or a proposal has zero world-effect. The only
execution path is :meth:`IncidentWorkspace.execute_proposal`, which drives the
canonical guarded flow:

    correlation finding → case → hypothesis → proposal
        → authority check → scope check → risk classification
        → HITL (when required) → ToolExecutor → re-observation
        → verification → timeline update

Every world-effect delegates to ``ToolExecutor.aexecute`` (which itself performs
the authority/scope/risk/HITL/audit gate) — there is no second executor here.

Compatibility adapters map CompoundIncident → IncidentCase, IncidentCase →
incident_reporter input, and IncidentCase → intel_fusion ingestion; IOC storage
reuses intel_fusion rather than duplicating it. Local-first JSON persistence.

Pure state + explicit async execution. Sinks/executors are dependency-injected,
so the workspace is unit-testable with fakes (no DB, no Ollama, no tools).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from loguru import logger

SCHEMA_VERSION = "incident-case-1"
_EVIDENCE_CAP = 1200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(prefix: str, *parts: str) -> str:
    blob = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


# ══════════════════════════════════════════════════════════════════════════════
#  Lifecycle
# ══════════════════════════════════════════════════════════════════════════════
class IncidentStatus(str, Enum):
    NEW = "new"
    TRIAGE = "triage"
    INVESTIGATING = "investigating"
    CONTAINMENT_PROPOSED = "containment_proposed"
    CONTAINED = "contained"
    ERADICATION = "eradication"
    RECOVERY = "recovery"
    CLOSED = "closed"
    FALSE_POSITIVE = "false_positive"


_ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.NEW: frozenset({IncidentStatus.TRIAGE, IncidentStatus.INVESTIGATING,
                                   IncidentStatus.FALSE_POSITIVE, IncidentStatus.CLOSED}),
    IncidentStatus.TRIAGE: frozenset({IncidentStatus.INVESTIGATING,
                                      IncidentStatus.FALSE_POSITIVE, IncidentStatus.CLOSED}),
    IncidentStatus.INVESTIGATING: frozenset({
        IncidentStatus.CONTAINMENT_PROPOSED, IncidentStatus.CONTAINED,
        IncidentStatus.ERADICATION, IncidentStatus.RECOVERY,
        IncidentStatus.FALSE_POSITIVE, IncidentStatus.CLOSED}),
    IncidentStatus.CONTAINMENT_PROPOSED: frozenset({
        IncidentStatus.CONTAINED, IncidentStatus.INVESTIGATING,
        IncidentStatus.FALSE_POSITIVE, IncidentStatus.CLOSED}),
    IncidentStatus.CONTAINED: frozenset({IncidentStatus.ERADICATION,
                                         IncidentStatus.RECOVERY,
                                         IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}),
    IncidentStatus.ERADICATION: frozenset({IncidentStatus.RECOVERY,
                                           IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}),
    IncidentStatus.RECOVERY: frozenset({IncidentStatus.CLOSED, IncidentStatus.INVESTIGATING}),
    IncidentStatus.CLOSED: frozenset({IncidentStatus.INVESTIGATING}),          # reopen
    IncidentStatus.FALSE_POSITIVE: frozenset({IncidentStatus.INVESTIGATING}),   # reopen
}

_OPEN_STATUSES = frozenset(set(IncidentStatus) - {IncidentStatus.CLOSED,
                                                  IncidentStatus.FALSE_POSITIVE})


class IncidentSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 1, "low": 2, "medium": 3, "high": 4, "critical": 5}[self.value]


def severity_from(value) -> IncidentSeverity:
    """Coerce a numeric (0–10) or string severity to an IncidentSeverity."""
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return IncidentSeverity.INFO
        if v < 3:
            return IncidentSeverity.LOW
        if v < 5:
            return IncidentSeverity.MEDIUM
        if v < 8:
            return IncidentSeverity.HIGH
        return IncidentSeverity.CRITICAL
    word = str(value or "").strip().lower()
    try:
        return IncidentSeverity(word)
    except ValueError:
        return {"warning": IncidentSeverity.HIGH, "critical": IncidentSeverity.CRITICAL,
                "unknown": IncidentSeverity.LOW}.get(word, IncidentSeverity.MEDIUM)


def _sev_to_score(sev: IncidentSeverity) -> float:
    return {IncidentSeverity.INFO: 1.0, IncidentSeverity.LOW: 2.5,
            IncidentSeverity.MEDIUM: 4.5, IncidentSeverity.HIGH: 7.5,
            IncidentSeverity.CRITICAL: 9.5}[sev]


# ══════════════════════════════════════════════════════════════════════════════
#  Case sub-objects
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class TimelineEntry:
    ts: str
    kind: str                    # e.g. "status", "evidence", "hypothesis", "action"
    message: str
    actor: str = "system"
    ref: str = ""

    def to_dict(self) -> dict:
        return {"ts": self.ts, "kind": self.kind, "message": self.message,
                "actor": self.actor, "ref": self.ref}

    @classmethod
    def from_dict(cls, d: dict) -> "TimelineEntry":
        return cls(ts=d["ts"], kind=d["kind"], message=d["message"],
                   actor=d.get("actor", "system"), ref=d.get("ref", ""))


@dataclass
class IncidentTimeline:
    entries: list[TimelineEntry] = field(default_factory=list)

    def append(self, kind: str, message: str, *, actor: str = "system", ref: str = "",
               ts: str | None = None) -> TimelineEntry:
        e = TimelineEntry(ts=ts or _now_iso(), kind=kind, message=message,
                          actor=actor, ref=ref)
        self.entries.append(e)
        return e

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self.entries]


class HypothesisStatus(str, Enum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"


@dataclass
class IncidentHypothesis:
    id: str
    statement: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    confidence: float = 0.5
    created_by: str = "system"
    evidence_refs: list[str] = field(default_factory=list)
    created_ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "statement": self.statement, "status": self.status.value,
                "confidence": round(self.confidence, 3), "created_by": self.created_by,
                "evidence_refs": list(self.evidence_refs), "created_ts": self.created_ts}


@dataclass
class EvidenceItem:
    id: str
    kind: str                    # e.g. "correlation_finding", "event", "observation"
    content: str
    source: str = "system"
    trusted: bool = True
    event_refs: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    ts: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        self.content = (self.content or "")[:_EVIDENCE_CAP]

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "content": self.content,
                "source": self.source, "trusted": self.trusted,
                "event_refs": list(self.event_refs), "provenance": dict(self.provenance),
                "ts": self.ts}


@dataclass(frozen=True)
class IOCReference:
    type: str                    # ip / domain / hash / url
    value: str
    source: str = "incident"
    first_seen: str = ""

    def key(self) -> str:
        return f"{self.type}:{self.value.strip().lower()}"

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value, "source": self.source,
                "first_seen": self.first_seen}


class QuestionStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"


@dataclass
class InvestigationQuestion:
    id: str
    question: str
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "question": self.question, "status": self.status.value,
                "answer": self.answer}


@dataclass
class IncidentDecision:
    id: str
    decision: str
    rationale: str = ""
    actor: str = "operator"
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "decision": self.decision, "rationale": self.rationale,
                "actor": self.actor, "ts": self.ts}


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class ContainmentProposal:
    """A PROPOSED world-effect. Creating one has ZERO effect — it only becomes an
    action through IncidentWorkspace.execute_proposal and the guarded gate."""
    id: str
    action_tool: str
    action_args: dict
    target: str = ""
    rationale: str = ""
    risk_class: str = ""
    requires_hitl: bool = True
    status: ProposalStatus = ProposalStatus.PROPOSED
    created_ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "action_tool": self.action_tool,
                "action_args": dict(self.action_args), "target": self.target,
                "rationale": self.rationale, "risk_class": self.risk_class,
                "requires_hitl": self.requires_hitl, "status": self.status.value,
                "created_ts": self.created_ts}


@dataclass
class IncidentAction:
    id: str
    tool: str
    args: dict
    target: str = ""
    status: str = "completed"        # completed / failed / blocked
    result_summary: str = ""
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": dict(self.args),
                "target": self.target, "status": self.status,
                "result_summary": self.result_summary, "ts": self.ts}


@dataclass
class VerificationResultRef:
    id: str
    verified: bool
    method: str = ""
    confidence: float = 0.0
    note: str = ""
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "verified": self.verified, "method": self.method,
                "confidence": round(self.confidence, 3), "note": self.note, "ts": self.ts}


# ══════════════════════════════════════════════════════════════════════════════
#  The case
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class IncidentCase:
    incident_id: str
    title: str = ""
    summary: str = ""
    status: IncidentStatus = IncidentStatus.NEW
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    confidence: float = 0.5
    affected_assets: list[str] = field(default_factory=list)
    related_events: list[str] = field(default_factory=list)
    correlation_findings: list[str] = field(default_factory=list)
    timeline: IncidentTimeline = field(default_factory=IncidentTimeline)
    evidence: list[EvidenceItem] = field(default_factory=list)
    hypotheses: list[IncidentHypothesis] = field(default_factory=list)
    open_questions: list[InvestigationQuestion] = field(default_factory=list)
    iocs: list[IOCReference] = field(default_factory=list)
    mitre_techniques: list[str] = field(default_factory=list)
    decisions: list[IncidentDecision] = field(default_factory=list)
    proposed_actions: list[ContainmentProposal] = field(default_factory=list)
    completed_actions: list[IncidentAction] = field(default_factory=list)
    verification_results: list[VerificationResultRef] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def transition(self, new_status: IncidentStatus, *, actor: str = "system",
                   note: str = "", ts: str | None = None) -> None:
        if new_status == self.status:
            return
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"illegal transition {self.status.value} → {new_status.value}")
        old = self.status
        self.status = new_status
        self._touch(ts)
        self.timeline.append("status", f"{old.value} → {new_status.value}"
                             + (f": {note}" if note else ""), actor=actor, ts=ts)

    def _touch(self, ts: str | None = None) -> None:
        self.updated_at = ts or _now_iso()

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATUSES

    # ── content builders (never any world-effect) ────────────────────────────
    def add_evidence(self, kind: str, content: str, *, source: str = "system",
                     trusted: bool = True, event_refs=None, provenance=None,
                     ts: str | None = None) -> EvidenceItem:
        item = EvidenceItem(
            id=_short_id("ev", self.incident_id, kind, content[:60]),
            kind=kind, content=content, source=source, trusted=trusted,
            event_refs=list(event_refs or []), provenance=dict(provenance or {}),
            ts=ts or _now_iso())
        self.evidence.append(item)
        self.timeline.append("evidence", f"[{kind}] {content[:120]}", ref=item.id, ts=ts)
        self._touch(ts)
        return item

    def add_hypothesis(self, statement: str, *, confidence: float = 0.5,
                       created_by: str = "system", evidence_refs=None) -> IncidentHypothesis:
        h = IncidentHypothesis(
            id=_short_id("hy", self.incident_id, statement),
            statement=statement, confidence=confidence, created_by=created_by,
            evidence_refs=list(evidence_refs or []))
        self.hypotheses.append(h)
        self.timeline.append("hypothesis", f"proposed: {statement[:120]}", ref=h.id)
        self._touch()
        return h

    def update_hypothesis(self, hypothesis_id: str, status: HypothesisStatus,
                          *, confidence: float | None = None) -> bool:
        for h in self.hypotheses:
            if h.id == hypothesis_id:
                h.status = status
                if confidence is not None:
                    h.confidence = confidence
                self.timeline.append("hypothesis", f"{status.value}: {h.statement[:100]}",
                                     ref=h.id)
                self._touch()
                return True
        return False

    def add_question(self, question: str) -> InvestigationQuestion:
        q = InvestigationQuestion(id=_short_id("q", self.incident_id, question),
                                  question=question)
        self.open_questions.append(q)
        self._touch()
        return q

    def answer_question(self, question_id: str, answer: str) -> bool:
        for q in self.open_questions:
            if q.id == question_id:
                q.status = QuestionStatus.ANSWERED
                q.answer = answer
                self.timeline.append("question", f"answered: {q.question[:80]}", ref=q.id)
                self._touch()
                return True
        return False

    def add_ioc(self, ioc_type: str, value: str, *, source: str = "incident",
                ts: str | None = None) -> IOCReference:
        ioc = IOCReference(type=ioc_type, value=str(value), source=source,
                           first_seen=ts or _now_iso())
        if all(existing.key() != ioc.key() for existing in self.iocs):
            self.iocs.append(ioc)
            self._touch(ts)
        return ioc

    def add_decision(self, decision: str, *, rationale: str = "",
                     actor: str = "operator") -> IncidentDecision:
        d = IncidentDecision(id=_short_id("dec", self.incident_id, decision),
                             decision=decision, rationale=rationale, actor=actor)
        self.decisions.append(d)
        self.timeline.append("decision", f"{decision}", actor=actor, ref=d.id)
        self._touch()
        return d

    def propose_containment(self, action_tool: str, action_args: dict, *,
                            target: str = "", rationale: str = "") -> ContainmentProposal:
        """Record a containment PROPOSAL. This performs NO world-effect — it only
        classifies the action's risk for the operator and files it for explicit,
        gated execution via IncidentWorkspace.execute_proposal."""
        risk_class, needs_hitl = _classify(action_tool)
        p = ContainmentProposal(
            id=_short_id("prop", self.incident_id, action_tool, str(action_args)),
            action_tool=action_tool, action_args=dict(action_args), target=target,
            rationale=rationale, risk_class=risk_class, requires_hitl=needs_hitl)
        self.proposed_actions.append(p)
        self.timeline.append("proposal", f"containment proposed: {action_tool} "
                             f"(risk={risk_class}, hitl={needs_hitl}) — NOT executed",
                             ref=p.id)
        self._touch()
        return p

    def record_action(self, action: IncidentAction) -> None:
        self.completed_actions.append(action)
        self.timeline.append("action", f"{action.tool} → {action.status}: "
                             f"{action.result_summary[:100]}", ref=action.id)
        self._touch()

    def record_verification(self, ref: VerificationResultRef) -> None:
        self.verification_results.append(ref)
        self.timeline.append("verification",
                             f"{'verified' if ref.verified else 'unverified'}: {ref.note[:100]}",
                             ref=ref.id)
        self._touch()

    def add_affected_asset(self, asset_id: str) -> None:
        if asset_id and asset_id not in self.affected_assets:
            self.affected_assets.append(asset_id)
            self._touch()

    def add_techniques(self, techniques) -> None:
        for t in techniques or ():
            if t and t not in self.mitre_techniques:
                self.mitre_techniques.append(t)
        self._touch()

    def add_finding(self, finding) -> None:
        """Attach a CorrelationFinding (M21) as evidence + IOCs + assets + MITRE."""
        fd = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding)
        fid = fd.get("finding_id", "")
        if fid and fid not in self.correlation_findings:
            self.correlation_findings.append(fid)
        for ev_id in fd.get("matched_event_ids", []):
            if ev_id not in self.related_events:
                self.related_events.append(ev_id)
        for aref in fd.get("asset_refs", []):
            self.add_affected_asset(aref)
        self.add_techniques(fd.get("mitre_techniques", []))
        for ent in fd.get("entities", []):
            etype, _, value = str(ent).partition(":")
            if etype == "ip" and value:
                self.add_ioc("ip", value, source="correlation")
            elif etype == "domain" and value:
                self.add_ioc("domain", value, source="correlation")
        expl = fd.get("explanation", {})
        self.add_evidence("correlation_finding",
                          f"{fd.get('rule','')}: {expl.get('reason','')}",
                          source="correlator_v2", event_refs=fd.get("matched_event_ids", []),
                          provenance={"finding_id": fid, "confidence": fd.get("confidence")})

    # ── compatibility projections ─────────────────────────────────────────────
    def to_reporter_input(self) -> dict:
        """Shape expected by core.incident_reporter.generate_incident_report."""
        sub_events = [
            {"type": e.kind, "process": e.provenance.get("process", ""),
             "pid": e.provenance.get("pid", ""),
             "technique": ", ".join(self.mitre_techniques[:3])}
            for e in self.evidence[:10]
        ]
        return {
            "incident_id": self.incident_id,
            "mitre_techniques": list(self.mitre_techniques),
            "involved_hosts": list(self.affected_assets),
            "kill_chain_phase": self.provenance.get("kill_chain_phase", ""),
            "severity_score": _sev_to_score(self.severity),
            "sub_events": sub_events,
        }

    def to_intel_fusion_incident(self) -> dict:
        """Shape expected by core.intel_fusion.ingest_incident."""
        hosts = [a.split(":", 1)[-1] if ":" in a else a for a in self.affected_assets]
        return {
            "incident_id": self.incident_id,
            "severity_score": _sev_to_score(self.severity),
            "kill_chain_phase": self.provenance.get("kill_chain_phase", ""),
            "mitre_techniques": list(self.mitre_techniques),
            "involved_hosts": hosts,
            "status": self.status.value,
        }

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "incident_id": self.incident_id, "title": self.title,
            "summary": self.summary, "status": self.status.value,
            "severity": self.severity.value, "confidence": round(self.confidence, 3),
            "affected_assets": list(self.affected_assets),
            "related_events": list(self.related_events),
            "correlation_findings": list(self.correlation_findings),
            "timeline": self.timeline.to_list(),
            "evidence": [e.to_dict() for e in self.evidence],
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "iocs": [i.to_dict() for i in self.iocs],
            "mitre_techniques": list(self.mitre_techniques),
            "decisions": [d.to_dict() for d in self.decisions],
            "proposed_actions": [p.to_dict() for p in self.proposed_actions],
            "completed_actions": [a.to_dict() for a in self.completed_actions],
            "verification_results": [v.to_dict() for v in self.verification_results],
            "provenance": dict(self.provenance),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IncidentCase":
        case = cls(
            incident_id=d["incident_id"], title=d.get("title", ""),
            summary=d.get("summary", ""),
            status=IncidentStatus(d.get("status", "new")),
            severity=IncidentSeverity(d.get("severity", "medium")),
            confidence=float(d.get("confidence", 0.5)),
            affected_assets=list(d.get("affected_assets", [])),
            related_events=list(d.get("related_events", [])),
            correlation_findings=list(d.get("correlation_findings", [])),
            iocs=[IOCReference(**i) for i in d.get("iocs", [])],
            mitre_techniques=list(d.get("mitre_techniques", [])),
            provenance=dict(d.get("provenance", {})),
            created_at=d.get("created_at", _now_iso()),
            updated_at=d.get("updated_at", _now_iso()),
        )
        case.timeline = IncidentTimeline(
            [TimelineEntry.from_dict(t) for t in d.get("timeline", [])])
        for e in d.get("evidence", []):
            case.evidence.append(EvidenceItem(
                id=e["id"], kind=e["kind"], content=e["content"], source=e.get("source", ""),
                trusted=e.get("trusted", True), event_refs=list(e.get("event_refs", [])),
                provenance=dict(e.get("provenance", {})), ts=e.get("ts", "")))
        for h in d.get("hypotheses", []):
            case.hypotheses.append(IncidentHypothesis(
                id=h["id"], statement=h["statement"],
                status=HypothesisStatus(h.get("status", "open")),
                confidence=float(h.get("confidence", 0.5)),
                created_by=h.get("created_by", "system"),
                evidence_refs=list(h.get("evidence_refs", [])),
                created_ts=h.get("created_ts", "")))
        for q in d.get("open_questions", []):
            case.open_questions.append(InvestigationQuestion(
                id=q["id"], question=q["question"],
                status=QuestionStatus(q.get("status", "open")), answer=q.get("answer", "")))
        for dec in d.get("decisions", []):
            case.decisions.append(IncidentDecision(
                id=dec["id"], decision=dec["decision"], rationale=dec.get("rationale", ""),
                actor=dec.get("actor", "operator"), ts=dec.get("ts", "")))
        for p in d.get("proposed_actions", []):
            case.proposed_actions.append(ContainmentProposal(
                id=p["id"], action_tool=p["action_tool"], action_args=dict(p.get("action_args", {})),
                target=p.get("target", ""), rationale=p.get("rationale", ""),
                risk_class=p.get("risk_class", ""), requires_hitl=p.get("requires_hitl", True),
                status=ProposalStatus(p.get("status", "proposed")),
                created_ts=p.get("created_ts", "")))
        for a in d.get("completed_actions", []):
            case.completed_actions.append(IncidentAction(
                id=a["id"], tool=a["tool"], args=dict(a.get("args", {})),
                target=a.get("target", ""), status=a.get("status", "completed"),
                result_summary=a.get("result_summary", ""), ts=a.get("ts", "")))
        for v in d.get("verification_results", []):
            case.verification_results.append(VerificationResultRef(
                id=v["id"], verified=v["verified"], method=v.get("method", ""),
                confidence=float(v.get("confidence", 0.0)), note=v.get("note", ""),
                ts=v.get("ts", "")))
        return case

    # ── CompoundIncident adapter ──────────────────────────────────────────────
    @classmethod
    def from_compound_incident(cls, compound: dict, *, now_iso: str | None = None
                               ) -> "IncidentCase":
        """Ingest a legacy correlator CompoundIncident (its to_dict() shape)."""
        now_iso = now_iso or _now_iso()
        inc_id = str(compound.get("incident_id") or _short_id("inc", str(compound)))
        hosts = compound.get("involved_hosts") or []
        if isinstance(hosts, (set, frozenset)):
            hosts = list(hosts)
        case = cls(
            incident_id=inc_id,
            title=f"Compound incident {inc_id} ({compound.get('rule','')})",
            summary=f"Kill-chain phase: {compound.get('kill_chain_phase','?')}",
            status=IncidentStatus.NEW,
            severity=severity_from(compound.get("severity_score", 0)),
            affected_assets=[str(h) for h in hosts if h],
            mitre_techniques=list(compound.get("mitre_techniques", [])),
            provenance={"source": "compound_incident", "rule": compound.get("rule", ""),
                        "kill_chain_phase": compound.get("kill_chain_phase", "")},
            created_at=now_iso, updated_at=now_iso,
        )
        case.timeline.append("ingest", f"ingested CompoundIncident {inc_id}",
                             ts=now_iso, ref=inc_id)
        for h in hosts:
            if h:
                case.add_ioc("ip", str(h), source="compound_incident", ts=now_iso)
        return case


def _classify(tool_name: str) -> tuple[str, bool]:
    try:
        from core.risk_classes import classify_tool, requires_hitl
        rc = classify_tool(tool_name)
        return rc.value, requires_hitl(rc)
    except Exception:  # noqa: BLE001 — classification is display-only; fail-closed to HITL
        return "high_impact", True


# ══════════════════════════════════════════════════════════════════════════════
#  Workspace
# ══════════════════════════════════════════════════════════════════════════════
class IncidentWorkspace:
    """Holds incident cases; opens/appends from findings and CompoundIncidents;
    executes containment ONLY through the canonical guarded path."""

    def __init__(self) -> None:
        self.cases: dict[str, IncidentCase] = {}
        self._by_group: dict[str, str] = {}   # group_entity -> incident_id (open)

    def add_case(self, case: IncidentCase) -> IncidentCase:
        self.cases[case.incident_id] = case
        return case

    def get(self, incident_id: str) -> IncidentCase | None:
        return self.cases.get(incident_id)

    def open_cases(self) -> list[IncidentCase]:
        return [c for c in self.cases.values() if c.is_open]

    def from_compound_incident(self, compound: dict, *, now_iso: str | None = None
                               ) -> IncidentCase:
        case = IncidentCase.from_compound_incident(compound, now_iso=now_iso)
        return self.add_case(case)

    def ingest_finding(self, finding, *, now_iso: str | None = None) -> IncidentCase:
        """Open a new case for a finding, or append to the open case that shares its
        primary group entity. Creating/appending a case has NO world-effect."""
        now_iso = now_iso or _now_iso()
        fd = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding)
        group = fd.get("group_entity", "")
        existing_id = self._by_group.get(group)
        case = self.cases.get(existing_id) if existing_id else None
        if case is None or not case.is_open:
            base = _short_id("inc", group, fd.get("finding_id", ""))
            inc_id, n = base, 1
            while inc_id in self.cases:      # a prior (closed) case collides → new id
                n += 1
                inc_id = f"{base}_{n}"
            case = IncidentCase(
                incident_id=inc_id,
                title=f"{fd.get('rule','correlation')} on {group}",
                summary=fd.get("explanation", {}).get("summary", ""),
                status=IncidentStatus.NEW,
                severity=severity_from(fd.get("severity", "medium")),
                confidence=float(fd.get("confidence", 0.5)),
                provenance={"source": "correlation_finding", "group_entity": group},
                created_at=now_iso, updated_at=now_iso,
            )
            case.timeline.append("open", f"case opened from finding "
                                 f"{fd.get('finding_id','')}", ts=now_iso)
            self.add_case(case)
            if group:
                self._by_group[group] = inc_id
        case.add_finding(finding)
        return case

    async def execute_proposal(
        self, case: IncidentCase, proposal: ContainmentProposal, tool_executor, *,
        reasoning: str = "", reobserve_fn=None, verify_fn=None,
    ) -> IncidentAction:
        """The ONLY path from a proposal to a world-effect. Drives the canonical
        guarded flow: authority/scope check → risk classification → ToolExecutor
        (which itself enforces scope/risk/HITL/audit) → re-observation →
        verification → timeline update. NEVER auto-runs; the caller invokes this
        explicitly, and ToolExecutor still challenges when the risk class requires."""
        if proposal.status is ProposalStatus.EXECUTED:
            raise ValueError(f"proposal {proposal.id} already executed")
        # 1. authority + scope preflight (informational — aexecute re-checks it).
        try:
            from core.authority import authorize_action
            decision = authorize_action(getattr(tool_executor, "authority", None),
                                        proposal.action_tool, proposal.action_args)
            if not decision.allowed:
                proposal.status = ProposalStatus.REJECTED
                case.timeline.append("proposal", f"proposal {proposal.id} refused: "
                                     f"{decision.reason}", ref=proposal.id)
                act = IncidentAction(id=_short_id("act", proposal.id), tool=proposal.action_tool,
                                     args=proposal.action_args, target=proposal.target,
                                     status="blocked", result_summary=decision.reason)
                case.record_action(act)
                return act
        except Exception as e:  # noqa: BLE001 — authority probe never crashes the flow
            logger.debug(f"INCIDENT_WS: authority probe skipped: {e}")

        if case.status is IncidentStatus.INVESTIGATING:
            case.transition(IncidentStatus.CONTAINMENT_PROPOSED, actor="system",
                            note=f"executing {proposal.action_tool}")

        # 2. execute through the SAME guarded gate every action passes.
        result = await tool_executor.aexecute(
            proposal.action_tool, proposal.action_args,
            reasoning or f"[incident:{case.incident_id}] {proposal.rationale}")
        ok = not (isinstance(result, dict) and "error" in result)
        proposal.status = ProposalStatus.EXECUTED if ok else ProposalStatus.FAILED
        act = IncidentAction(
            id=_short_id("act", proposal.id), tool=proposal.action_tool,
            args=proposal.action_args, target=proposal.target,
            status="completed" if ok else "failed",
            result_summary=(json.dumps(result, default=str)[:200] if ok
                            else str(result.get("error", ""))[:200]))
        case.record_action(act)

        # 3. re-observation (optional hook — evidence, not truth).
        if reobserve_fn is not None:
            try:
                obs = await reobserve_fn(case, proposal)
                if obs:
                    case.add_evidence("re_observation", str(obs)[:400], source="reobserve")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"INCIDENT_WS: reobserve failed: {e}")

        # 4. verification (optional hook) → recorded, may drive CONTAINED.
        if verify_fn is not None:
            try:
                verified, note, conf = await verify_fn(case, proposal, result)
            except Exception as e:  # noqa: BLE001 — a throwing verifier fails closed
                verified, note, conf = False, f"verifier error: {e}", 0.0
            vref = VerificationResultRef(id=_short_id("ver", proposal.id),
                                         verified=bool(verified), method="post_action",
                                         confidence=float(conf), note=str(note)[:200])
            case.record_verification(vref)
            if ok and verified and case.status is IncidentStatus.CONTAINMENT_PROPOSED:
                case.transition(IncidentStatus.CONTAINED, actor="system",
                                note="post-action verification confirmed containment")
        return act

    # ── intel_fusion / reporter bridges (reuse, do not duplicate) ─────────────
    async def sync_to_intel_fusion(self, case: IncidentCase, *,
                                   ingest_incident_fn=None, ingest_ioc_fn=None) -> None:
        """Push a case into the existing intel_fusion engine (IOC/campaign
        intelligence). Reuses intel_fusion — does not create a second IOC store.
        Functions are injectable so tests need no DB; production wires the real
        intel_fusion.ingest_incident / ingest_ioc."""
        if ingest_incident_fn is None or ingest_ioc_fn is None:
            try:
                from core.intel_fusion import ingest_incident, ingest_ioc
                ingest_incident_fn = ingest_incident_fn or ingest_incident
                ingest_ioc_fn = ingest_ioc_fn or ingest_ioc
            except Exception as e:  # noqa: BLE001
                logger.debug(f"INCIDENT_WS: intel_fusion unavailable: {e}")
                return
        try:
            await ingest_incident_fn(case.to_intel_fusion_incident())
            for ioc in case.iocs:
                await ingest_ioc_fn(ioc.type, ioc.value, 0, f"incident:{case.incident_id}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"INCIDENT_WS: intel_fusion sync failed: {e}")

    # ── persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION,
                "cases": [c.to_dict() for c in self.cases.values()]}

    def save(self, path: "str | Path") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")

    @classmethod
    def load(cls, path: "str | Path") -> "IncidentWorkspace":
        ws = cls()
        p = Path(path)
        if not p.exists():
            return ws
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            for cd in d.get("cases", []):
                case = IncidentCase.from_dict(cd)
                ws.cases[case.incident_id] = case
                grp = case.provenance.get("group_entity")
                if grp and case.is_open:
                    ws._by_group[grp] = case.incident_id
        except Exception as e:  # noqa: BLE001
            logger.warning(f"INCIDENT_WS: load failed ({e}) — starting empty")
        return ws


# Module-level singleton + the M21 finding sink bound to it.
workspace = IncidentWorkspace()


async def incident_finding_sink(finding) -> None:
    """CorrelatorV2 sink: open/append an incident case from a finding. Pure state —
    NO containment executes here; a finding never triggers a world-effect."""
    try:
        workspace.ingest_finding(finding)
    except Exception as e:  # noqa: BLE001 — a bad finding never breaks correlation emit
        logger.debug(f"INCIDENT_WS: finding ingest failed: {e}")
