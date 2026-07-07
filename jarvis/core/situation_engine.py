"""
core/situation_engine.py — V66 Milestone 25: unified operational situation model.

One compact, DETERMINISTIC model over the state the other V66 milestones produce:

    Asset Graph (M20) + Correlation Findings (M21) + Incident Cases (M22)
    + Drift Findings (M23) + Sensor/Collector health + Presence state (M7)
    + Resource pressure

It is NOT another agent and NOT another correlator. Its only job is to answer, at
a glance and from evidence:

    What is happening now?   What changed?   What matters most?
    What is uncertain?       What needs investigation?   What can wait?

Hard rule (V66 trust): **facts come from evidence-backed state.** The engine never
invents an asset, an incident, a technique, or a status — it only counts, ranks,
and summarizes what the inputs already assert. An LLM may *explain* a snapshot,
but the grounding context handed to it (:meth:`SituationEngine.llm_grounding`) is
strictly the snapshot's facts, and priority ordering is deterministic (stable
tie-breaks), so the same state always yields the same situation.

Recommendations are advisory and always propose a runbook **dry run** first — the
situation model proposes investigation, never a world-effect.

Pure function of its inputs (all injectable as objects or plain dicts), so it is
unit-testable with fakes — no live graph/incidents/twin required.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

SCHEMA_VERSION = "situation-1"

_SEV_RANK = {"info": 1, "low": 2, "medium": 3, "high": 4, "critical": 5, "unknown": 0}
_DOWN_WORDS = frozenset({"down", "disconnected", "offline", "inactive", "stopped", "0/"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rank(sev) -> int:
    return _SEV_RANK.get(str(sev or "").strip().lower(), 0)


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            return {}
    return {}


# Map a correlation rule / drift type to the recommended runbook (advisory).
_RULE_RUNBOOK: dict[str, str] = {
    "auth_failures_then_success": "AUTH_FAILURE_TRIAGE",
    "ids_alert_with_host_activity": "IDS_ALERT_INVESTIGATION",
    "sensor_plus_network_anomaly": "IDS_ALERT_INVESTIGATION",
    "new_service_exposure_then_connection": "NEW_SERVICE_EXPOSURE_REVIEW",
    "suspicious_process_then_network": "INCIDENT_EVIDENCE_COLLECTION",
    "high_severity_sequence": "INCIDENT_EVIDENCE_COLLECTION",
    "same_ioc_multiple_assets": "INCIDENT_EVIDENCE_COLLECTION",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Taxonomy
# ══════════════════════════════════════════════════════════════════════════════
class SituationSeverity(str, Enum):
    CALM = "calm"
    NOTABLE = "notable"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"calm": 0, "notable": 1, "elevated": 2, "high": 3, "critical": 4}[self.value]


def _situation_severity_from_rank(rank: int) -> SituationSeverity:
    return {0: SituationSeverity.CALM, 1: SituationSeverity.CALM,
            2: SituationSeverity.NOTABLE, 3: SituationSeverity.ELEVATED,
            4: SituationSeverity.HIGH, 5: SituationSeverity.CRITICAL}.get(
                rank, SituationSeverity.CALM)


# ══════════════════════════════════════════════════════════════════════════════
#  Model
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PriorityItem:
    id: str
    kind: str                    # incident / correlation / drift / sensor
    title: str
    severity: str
    confidence: float
    asset: str
    evidence: tuple[str, ...]
    recommended_runbook: str
    uncertain: bool
    source_ref: str = ""

    def score(self) -> tuple:
        weight = {"incident": 3, "correlation": 2, "drift": 1, "sensor": 1}.get(self.kind, 0)
        return (_rank(self.severity), round(self.confidence, 3), weight, self.id)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "severity": self.severity, "confidence": round(self.confidence, 3),
                "asset": self.asset, "evidence": list(self.evidence),
                "recommended_runbook": self.recommended_runbook,
                "uncertain": self.uncertain, "source_ref": self.source_ref}


@dataclass(frozen=True)
class OperationalRecommendation:
    title: str
    runbook: str
    mode: str                    # always "dry_run" — investigation, never a world-effect
    rationale: str
    priority_ref: str

    def to_dict(self) -> dict:
        return {"title": self.title, "runbook": self.runbook, "mode": self.mode,
                "rationale": self.rationale, "priority_ref": self.priority_ref}


@dataclass(frozen=True)
class SituationSummary:
    known_assets: int
    healthy_assets: int
    degraded_assets: int
    unknown_assets: int
    open_incidents: int
    critical_incidents: int
    critical_drift: int
    sensors: dict
    top_priority: dict | None
    confidence: float
    recommended_next_step: str

    def to_dict(self) -> dict:
        return {
            "known_assets": self.known_assets, "healthy_assets": self.healthy_assets,
            "degraded_assets": self.degraded_assets, "unknown_assets": self.unknown_assets,
            "open_incidents": self.open_incidents, "critical_incidents": self.critical_incidents,
            "critical_drift": self.critical_drift, "sensors": dict(self.sensors),
            "top_priority": self.top_priority, "confidence": round(self.confidence, 3),
            "recommended_next_step": self.recommended_next_step,
        }


@dataclass(frozen=True)
class SituationSnapshot:
    taken_at: str
    severity: SituationSeverity
    summary: SituationSummary
    priorities: tuple[PriorityItem, ...]
    recommendations: tuple[OperationalRecommendation, ...]
    uncertainties: tuple[str, ...]
    can_wait: tuple[str, ...]
    what_changed: dict
    resource: dict
    presence: dict
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version, "taken_at": self.taken_at,
            "severity": self.severity.value, "summary": self.summary.to_dict(),
            "priorities": [p.to_dict() for p in self.priorities],
            "recommendations": [r.to_dict() for r in self.recommendations],
            "uncertainties": list(self.uncertainties), "can_wait": list(self.can_wait),
            "what_changed": dict(self.what_changed), "resource": dict(self.resource),
            "presence": dict(self.presence),
        }

    def narrative(self) -> str:
        """A deterministic, fact-only summary (no LLM, no invention)."""
        s = self.summary
        lines = [
            f"Situation: {self.severity.value.upper()}",
            f"Assets: {s.known_assets} known, {s.healthy_assets} healthy, "
            f"{s.degraded_assets} degraded, {s.unknown_assets} unknown.",
            f"Open incidents: {s.open_incidents} ({s.critical_incidents} critical); "
            f"critical drift: {s.critical_drift}.",
        ]
        if s.top_priority:
            tp = s.top_priority
            lines.append(f"Top priority: {tp['title']} "
                         f"(severity={tp['severity']}, confidence={tp['confidence']}).")
            lines.append(f"Recommended next step: {s.recommended_next_step} (dry run).")
        else:
            lines.append("No active priorities — nothing requires investigation now.")
        if self.uncertainties:
            lines.append(f"Uncertain: {len(self.uncertainties)} item(s) need observation.")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Engine
# ══════════════════════════════════════════════════════════════════════════════
class SituationEngine:
    """Deterministic situation synthesis. Pure; inputs injectable as objects/dicts."""

    def __init__(self, *, max_priorities: int = 8) -> None:
        self.max_priorities = max(1, max_priorities)

    def build(
        self, *, asset_graph=None, incidents=None, drift=None,
        correlation_findings=None, sensor_health=None, presence=None,
        resource=None, previous: SituationSnapshot | None = None,
        now_iso: str | None = None,
    ) -> SituationSnapshot:
        now_iso = now_iso or _now_iso()
        incidents = list(incidents or [])
        findings = list(correlation_findings or [])
        drift_d = _as_dict(drift)
        sensors = dict(sensor_health or {})
        presence_d = dict(presence or {})
        resource_d = dict(resource or {})

        priorities: list[PriorityItem] = []
        degraded_assets: set[str] = set()

        # ── incidents ─────────────────────────────────────────────────────────
        open_incidents = 0
        critical_incidents = 0
        for inc in incidents:
            d = _as_dict(inc)
            status = str(d.get("status", "")).lower()
            if status in ("closed", "false_positive"):
                continue
            open_incidents += 1
            sev = d.get("severity", "medium")
            if _rank(sev) >= 5:
                critical_incidents += 1
            assets = d.get("affected_assets", []) or []
            degraded_assets.update(assets)
            asset0 = assets[0] if assets else ""
            conf = float(d.get("confidence", 0.5) or 0.5)
            priorities.append(PriorityItem(
                id=f"inc:{d.get('incident_id','?')}", kind="incident",
                title=d.get("title") or f"incident {d.get('incident_id','?')}",
                severity=str(sev), confidence=conf, asset=asset0,
                evidence=(f"{len(d.get('correlation_findings', []))} correlation finding(s)",
                          f"{len(d.get('evidence', []))} evidence item(s)"),
                recommended_runbook="INCIDENT_EVIDENCE_COLLECTION",
                uncertain=conf < 0.5, source_ref=str(d.get("incident_id", ""))))

        # ── correlation findings ──────────────────────────────────────────────
        for f in findings:
            d = _as_dict(f)
            sev = d.get("severity", "medium")
            conf = float(d.get("confidence", 0.5) or 0.5)
            assets = d.get("asset_refs", []) or []
            degraded_assets.update(assets)
            rule = d.get("rule", "")
            expl = d.get("explanation", {}) or {}
            priorities.append(PriorityItem(
                id=f"finding:{d.get('finding_id','?')}", kind="correlation",
                title=f"{rule} on {d.get('group_entity','')}",
                severity=str(sev), confidence=conf,
                asset=(assets[0] if assets else d.get("group_entity", "")),
                evidence=(expl.get("reason", "")[:120],
                          f"{len(d.get('matched_event_ids', []))} matched event(s)"),
                recommended_runbook=_RULE_RUNBOOK.get(rule, "INCIDENT_EVIDENCE_COLLECTION"),
                uncertain=conf < 0.5, source_ref=str(d.get("finding_id", ""))))

        # ── drift findings ────────────────────────────────────────────────────
        critical_drift = 0
        for df in drift_d.get("findings", []):
            sev = df.get("severity", "low")
            if _rank(sev) >= 4:
                critical_drift += 1
            drift_type = df.get("drift_type", "")
            uncertain = drift_type == "state_unknown"
            if not uncertain:
                degraded_assets.add(df.get("asset", ""))
            priorities.append(PriorityItem(
                id=f"drift:{df.get('finding_id','?')}", kind="drift",
                title=f"{drift_type} on {df.get('asset','')}",
                severity=str(sev), confidence=float(df.get("confidence", 0.4) or 0.4),
                asset=df.get("asset", ""),
                evidence=(f"expected={_compact(df.get('expected_fact'))}",
                          f"observed={_compact(df.get('observed_fact'))}"),
                recommended_runbook=df.get("recommended_investigation", "HOST_CONNECTIVITY_DIAGNOSIS"),
                uncertain=uncertain, source_ref=str(df.get("finding_id", ""))))

        # ── sensor health ─────────────────────────────────────────────────────
        for name, status in sensors.items():
            if _is_down(status):
                priorities.append(PriorityItem(
                    id=f"sensor:{name}", kind="sensor",
                    title=f"sensor {name} degraded ({status})", severity="high",
                    confidence=0.7, asset=name,
                    evidence=(f"{name}={status}",),
                    recommended_runbook="HOST_CONNECTIVITY_DIAGNOSIS",
                    uncertain=False, source_ref=name))

        # ── deterministic ranking ─────────────────────────────────────────────
        priorities.sort(key=lambda p: p.score(), reverse=True)
        priorities = priorities[: self.max_priorities]

        # ── asset accounting (from evidence-backed graph only) ────────────────
        known, healthy, degraded_n, unknown_n = self._assess_assets(
            asset_graph, degraded_assets)

        # ── uncertainties + can-wait ──────────────────────────────────────────
        uncertainties = tuple(p.title for p in priorities if p.uncertain)
        can_wait = tuple(p.title for p in priorities
                         if not p.uncertain and _rank(p.severity) <= 2)

        # ── top priority + recommendation ─────────────────────────────────────
        actionable = [p for p in priorities if not p.uncertain]
        top = actionable[0] if actionable else (priorities[0] if priorities else None)
        recommendations: tuple[OperationalRecommendation, ...] = ()
        rec_step = "monitor"
        if top is not None:
            rec = OperationalRecommendation(
                title=f"Investigate {top.title}", runbook=top.recommended_runbook,
                mode="dry_run",
                rationale=f"highest-priority {top.kind} (severity={top.severity}, "
                          f"confidence={round(top.confidence, 2)})",
                priority_ref=top.id)
            recommendations = (rec,)
            rec_step = f"{top.recommended_runbook}"

        overall_rank = max((_rank(p.severity) for p in priorities), default=0)
        # sensor-down or open incidents lift the floor
        if critical_incidents:
            overall_rank = max(overall_rank, 5)
        severity = _situation_severity_from_rank(overall_rank)

        summary = SituationSummary(
            known_assets=known, healthy_assets=healthy, degraded_assets=degraded_n,
            unknown_assets=unknown_n, open_incidents=open_incidents,
            critical_incidents=critical_incidents, critical_drift=critical_drift,
            sensors=sensors, top_priority=top.to_dict() if top else None,
            confidence=(top.confidence if top else 1.0),
            recommended_next_step=rec_step)

        what_changed = self._diff(previous, priorities)

        return SituationSnapshot(
            taken_at=now_iso, severity=severity, summary=summary,
            priorities=tuple(priorities), recommendations=recommendations,
            uncertainties=uncertainties, can_wait=can_wait,
            what_changed=what_changed, resource=resource_d, presence=presence_d)

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _assess_assets(asset_graph, degraded_assets: set[str]) -> tuple[int, int, int, int]:
        if asset_graph is None or not getattr(asset_graph, "assets", None):
            return 0, 0, 0, 0
        try:
            from core.asset_graph import AssetType
        except Exception:  # noqa: BLE001
            AssetType = None  # type: ignore
        assets = list(asset_graph.assets.values())
        total = len(assets)
        unknown_ids: set[str] = set()
        for a in assets:
            is_unknown = False
            if AssetType is not None and a.current_type() is AssetType.UNKNOWN:
                is_unknown = True
            if a.conflicts():
                is_unknown = True
            if is_unknown:
                unknown_ids.add(a.id)
        degraded_ids = {aid for aid in degraded_assets
                        if aid in asset_graph.assets and aid not in unknown_ids}
        healthy = max(0, total - len(unknown_ids) - len(degraded_ids))
        return total, healthy, len(degraded_ids), len(unknown_ids)

    @staticmethod
    def _diff(previous: SituationSnapshot | None,
              priorities: list[PriorityItem]) -> dict:
        current_ids = {p.id for p in priorities}
        if previous is None:
            return {"new": sorted(current_ids), "resolved": [], "baseline": True}
        prev_ids = {p.id for p in previous.priorities}
        return {"new": sorted(current_ids - prev_ids),
                "resolved": sorted(prev_ids - current_ids), "baseline": False}

    def llm_grounding(self, snapshot: SituationSnapshot) -> dict:
        """The ONLY facts an LLM explainer may use — a bounded, evidence-only view.
        An explanation must not introduce any operational fact outside this."""
        return {
            "instruction": "Explain this situation using ONLY the facts below. "
                           "Do not invent assets, incidents, techniques, or status.",
            "severity": snapshot.severity.value,
            "summary": snapshot.summary.to_dict(),
            "priorities": [p.to_dict() for p in snapshot.priorities[:5]],
            "uncertainties": list(snapshot.uncertainties),
        }


def _is_down(status) -> bool:
    s = str(status or "").strip().lower()
    return any(w in s for w in _DOWN_WORDS)


def _compact(fact) -> str:
    if not fact:
        return "?"
    if isinstance(fact, dict):
        return f"{fact.get('key','')}={fact.get('value','')}"
    return str(fact)[:60]


# Module-level singleton. M26 serializes SituationSnapshots to the HUD; main/voice
# build one on demand from the live graph / incidents / twin / findings.
engine = SituationEngine()
