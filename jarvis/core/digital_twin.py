"""
core/digital_twin.py — V66 Milestone 23: operational digital twin & drift model.

NOT a visual toy and NOT a physics simulator. The twin is three views of an
asset's state and the *difference* between them:

    Observed State   — what telemetry / the asset graph actually shows.
    Expected State   — what we expect (a baseline, or an operator declaration).
    Desired State    — what we want (a target configuration).

A :class:`DriftFinding` is the typed gap between Expected/Desired and Observed —
e.g. an expected-active ssh service observed absent → ``SERVICE_MISSING``; an
expected-running container observed stopped → ``WORKLOAD_STOPPED``; a service
expected on localhost observed on the test subnet → ``NETWORK_EXPOSURE_DRIFT``;
an expected-connected sensor observed disconnected → ``SENSOR_COVERAGE_DRIFT``; a
version mismatch → ``VERSION_DRIFT``.

Trust invariants (V66):
  * **Drift is not automatically an attack**, and never automatically triggers
    remediation — a DriftFinding is a signal that *requires investigation and
    verification*, nothing more (``verification_required`` is always True).
  * **Unknown stays unknown.** If the observed state has no fact for an expected
    key, the twin emits ``STATE_UNKNOWN`` (a coverage gap to re-observe) — it does
    NOT claim the service is missing. "Observed absent" and "not observed" are
    different.
  * Every fact and finding carries provenance, confidence, and evidence refs.
  * The network baseline (``core.network_baseline``) is a *signal source*, folded
    in as observed behavior — its beacon/z-score logic is untouched.

Drift connects (as signals, never as automatic action) to the Presence Engine,
Incident Workspace, Asset Graph, the Improvement Loop, and Runbook recommendation.

Pure state + deterministic comparison. Unit-testable with injected clocks.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from loguru import logger

SCHEMA_VERSION = "digital-twin-1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(v) -> str:
    return str(v).strip().lower() if v is not None else ""


# Values that mean "the thing is not there / not running / not reachable".
_ABSENT_VALUES = frozenset({
    "absent", "missing", "inactive", "stopped", "down", "dead", "disconnected",
    "offline", "not_running", "unreachable", "closed", "false", "0", "none",
})


def _is_absent(value) -> bool:
    return _norm(value) in _ABSENT_VALUES


# ══════════════════════════════════════════════════════════════════════════════
#  Taxonomy
# ══════════════════════════════════════════════════════════════════════════════
class FactKind(str, Enum):
    SERVICE = "service"
    WORKLOAD = "workload"
    EXPOSURE = "exposure"
    SENSOR = "sensor"
    VERSION = "version"
    GENERIC = "generic"


class DriftType(str, Enum):
    SERVICE_MISSING = "service_missing"
    SERVICE_UNEXPECTED = "service_unexpected"
    WORKLOAD_STOPPED = "workload_stopped"
    NETWORK_EXPOSURE_DRIFT = "network_exposure_drift"
    SENSOR_COVERAGE_DRIFT = "sensor_coverage_drift"
    VERSION_DRIFT = "version_drift"
    CONFIG_DRIFT = "config_drift"
    STATE_UNKNOWN = "state_unknown"


class DriftSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 1, "low": 2, "medium": 3, "high": 4, "critical": 5}[self.value]


_DRIFT_SEVERITY: dict[DriftType, DriftSeverity] = {
    DriftType.SERVICE_MISSING: DriftSeverity.HIGH,
    DriftType.SERVICE_UNEXPECTED: DriftSeverity.MEDIUM,
    DriftType.WORKLOAD_STOPPED: DriftSeverity.MEDIUM,
    DriftType.NETWORK_EXPOSURE_DRIFT: DriftSeverity.HIGH,
    DriftType.SENSOR_COVERAGE_DRIFT: DriftSeverity.HIGH,
    DriftType.VERSION_DRIFT: DriftSeverity.LOW,
    DriftType.CONFIG_DRIFT: DriftSeverity.MEDIUM,
    DriftType.STATE_UNKNOWN: DriftSeverity.LOW,
}

# Which existing/M24 runbook to recommend for each drift type (advisory only).
_DRIFT_RUNBOOK: dict[DriftType, str] = {
    DriftType.SERVICE_MISSING: "SERVICE_DIAGNOSIS",
    DriftType.SERVICE_UNEXPECTED: "NEW_SERVICE_EXPOSURE_REVIEW",
    DriftType.WORKLOAD_STOPPED: "CONTAINER_HEALTH_CHECK",
    DriftType.NETWORK_EXPOSURE_DRIFT: "NEW_SERVICE_EXPOSURE_REVIEW",
    DriftType.SENSOR_COVERAGE_DRIFT: "HOST_CONNECTIVITY_DIAGNOSIS",
    DriftType.VERSION_DRIFT: "SERVICE_DIAGNOSIS",
    DriftType.CONFIG_DRIFT: "SERVICE_DIAGNOSIS",
    DriftType.STATE_UNKNOWN: "HOST_CONNECTIVITY_DIAGNOSIS",
}


# ══════════════════════════════════════════════════════════════════════════════
#  State facts + states
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class StateFact:
    """One evidence-bearing fact about an asset's state. ``value=None`` means the
    state is unknown (unobserved) — distinct from an observed absent value."""
    key: str
    value: str | None
    kind: FactKind = FactKind.GENERIC
    source: str = "internal"
    confidence: float = 0.5
    observed_at: str = ""
    evidence_refs: tuple[str, ...] = ()
    note: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "kind": self.kind.value,
                "source": self.source, "confidence": round(self.confidence, 3),
                "observed_at": self.observed_at, "evidence_refs": list(self.evidence_refs),
                "note": self.note}


@dataclass
class _AssetState:
    """A named collection of facts for one asset (observed / expected / desired)."""
    facts: dict[str, StateFact] = field(default_factory=dict)

    def set(self, fact: StateFact) -> None:
        self.facts[fact.key] = fact

    def get(self, key: str) -> StateFact | None:
        return self.facts.get(key)

    def to_dict(self) -> dict:
        return {k: f.to_dict() for k, f in self.facts.items()}


# Named wrappers so the three views are self-documenting.
class OperationalState(_AssetState):
    """The OBSERVED state."""


class ExpectedState(_AssetState):
    """The EXPECTED state (baseline / operator declaration)."""


class DesiredState(_AssetState):
    """The DESIRED state (target configuration)."""


# ══════════════════════════════════════════════════════════════════════════════
#  Drift finding
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class DriftFinding:
    """A typed gap between expected/desired and observed state. A SIGNAL, not an
    incident and not an instruction to remediate — always requires verification."""
    finding_id: str
    asset: str
    drift_type: DriftType
    severity: DriftSeverity
    expected_fact: dict | None
    observed_fact: dict | None
    confidence: float
    evidence_refs: tuple[str, ...]
    recommended_investigation: str
    verification_required: bool
    timestamp: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id, "asset": self.asset,
            "drift_type": self.drift_type.value, "severity": self.severity.value,
            "expected_fact": self.expected_fact, "observed_fact": self.observed_fact,
            "confidence": round(self.confidence, 3),
            "evidence_refs": list(self.evidence_refs),
            "recommended_investigation": self.recommended_investigation,
            "verification_required": self.verification_required,
            "timestamp": self.timestamp, "note": self.note,
        }


@dataclass(frozen=True)
class StateDifference:
    """A single (key-level) difference used to build a DriftFinding."""
    asset: str
    key: str
    expected: StateFact | None
    observed: StateFact | None
    drift_type: DriftType


@dataclass(frozen=True)
class TwinSnapshot:
    taken_at: str
    findings: tuple[DriftFinding, ...]
    by_severity: dict
    by_type: dict
    assets_with_drift: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "findings": [f.to_dict() for f in self.findings],
            "by_severity": dict(self.by_severity), "by_type": dict(self.by_type),
            "assets_with_drift": list(self.assets_with_drift),
            "drift_count": len(self.findings),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Comparator
# ══════════════════════════════════════════════════════════════════════════════
def _finding_id(asset: str, key: str, drift_type: DriftType) -> str:
    return "df_" + hashlib.sha256(f"{asset}|{key}|{drift_type.value}".encode()).hexdigest()[:14]


def _type_for(kind: FactKind, observed_absent: bool) -> DriftType:
    if kind is FactKind.SERVICE:
        return DriftType.SERVICE_MISSING if observed_absent else DriftType.CONFIG_DRIFT
    if kind is FactKind.WORKLOAD:
        return DriftType.WORKLOAD_STOPPED if observed_absent else DriftType.CONFIG_DRIFT
    if kind is FactKind.EXPOSURE:
        return DriftType.NETWORK_EXPOSURE_DRIFT
    if kind is FactKind.SENSOR:
        return DriftType.SENSOR_COVERAGE_DRIFT if observed_absent else DriftType.CONFIG_DRIFT
    if kind is FactKind.VERSION:
        return DriftType.VERSION_DRIFT
    return DriftType.CONFIG_DRIFT


class StateComparator:
    """Deterministic expected/desired-vs-observed comparison → DriftFindings."""

    @staticmethod
    def compare(asset: str, expected: _AssetState, observed: _AssetState,
                *, now_iso: str) -> list[DriftFinding]:
        findings: list[DriftFinding] = []
        for key, exp in expected.facts.items():
            obs = observed.get(key)
            if obs is None or not obs.known:
                # Unknown stays unknown — a coverage gap to re-observe, NOT a claim.
                findings.append(_build(
                    asset, key, DriftType.STATE_UNKNOWN, exp, obs, now_iso,
                    note="expected fact could not be verified — state unobserved"))
                continue
            if _norm(exp.value) == _norm(obs.value):
                continue                                  # match — no drift
            drift_type = _type_for(exp.kind, _is_absent(obs.value))
            findings.append(_build(asset, key, drift_type, exp, obs, now_iso))

        # An observed exposure/service NOT present in expected is a new exposure.
        for key, obs in observed.facts.items():
            if key in expected.facts or not obs.known:
                continue
            if obs.kind in (FactKind.EXPOSURE, FactKind.SERVICE) and not _is_absent(obs.value):
                findings.append(_build(
                    asset, key, DriftType.NETWORK_EXPOSURE_DRIFT
                    if obs.kind is FactKind.EXPOSURE else DriftType.SERVICE_UNEXPECTED,
                    None, obs, now_iso,
                    note="observed but not in expected baseline"))
        return findings


def _build(asset: str, key: str, drift_type: DriftType, exp: StateFact | None,
           obs: StateFact | None, now_iso: str, *, note: str = "") -> DriftFinding:
    confs = [f.confidence for f in (exp, obs) if f is not None]
    confidence = min(confs) if confs else 0.3
    if obs is None or not obs.known:
        confidence = min(confidence, 0.4)   # can't verify → lower confidence
    evidence = tuple(dict.fromkeys(
        r for f in (exp, obs) if f is not None for r in f.evidence_refs))
    return DriftFinding(
        finding_id=_finding_id(asset, key, drift_type), asset=asset,
        drift_type=drift_type, severity=_DRIFT_SEVERITY[drift_type],
        expected_fact=exp.to_dict() if exp else None,
        observed_fact=obs.to_dict() if obs else None,
        confidence=confidence, evidence_refs=evidence,
        recommended_investigation=_DRIFT_RUNBOOK[drift_type],
        verification_required=True, timestamp=now_iso, note=note)


# ══════════════════════════════════════════════════════════════════════════════
#  The twin
# ══════════════════════════════════════════════════════════════════════════════
class DigitalTwin:
    """Holds observed/expected/desired state per asset and computes drift. Never
    remediates, never claims attack — it produces investigation signals."""

    def __init__(self) -> None:
        self._observed: dict[str, OperationalState] = {}
        self._expected: dict[str, ExpectedState] = {}
        self._desired: dict[str, DesiredState] = {}

    # ── declarations (expected/desired are operator/config-owned) ─────────────
    def set_expected(self, asset: str, key: str, value: str, *, kind: FactKind,
                     source: str = "operator_declaration", confidence: float = 0.9,
                     now_iso: str | None = None, evidence_refs=()) -> None:
        self._expected.setdefault(asset, ExpectedState()).set(StateFact(
            key=key, value=value, kind=kind, source=source, confidence=confidence,
            observed_at=now_iso or _now_iso(), evidence_refs=tuple(evidence_refs)))

    def set_desired(self, asset: str, key: str, value: str, *, kind: FactKind,
                    source: str = "trusted_config", confidence: float = 0.9,
                    now_iso: str | None = None) -> None:
        self._desired.setdefault(asset, DesiredState()).set(StateFact(
            key=key, value=value, kind=kind, source=source, confidence=confidence,
            observed_at=now_iso or _now_iso()))

    # ── observations ──────────────────────────────────────────────────────────
    def observe(self, asset: str, key: str, value: str | None, *, kind: FactKind,
                source: str = "canonical_event", confidence: float = 0.6,
                now_iso: str | None = None, evidence_refs=()) -> None:
        self._observed.setdefault(asset, OperationalState()).set(StateFact(
            key=key, value=value, kind=kind, source=source, confidence=confidence,
            observed_at=now_iso or _now_iso(), evidence_refs=tuple(evidence_refs)))

    def ingest_event(self, event, *, now_iso: str | None = None) -> None:
        """Fold a canonical OperationalEvent's state signal into observed state
        (sensor connect/disconnect, service observation). Best-effort; unknown
        event shapes are ignored (they simply add no state fact)."""
        try:
            from core.ops_events import EventCategory, EventSource
        except Exception:  # noqa: BLE001
            return
        now_iso = now_iso or getattr(event, "timestamp", None) or _now_iso()
        host = getattr(event, "host", None) or getattr(event, "src_ip", None)
        if event.category is EventCategory.SENSOR and host:
            connected = "connected" if "connect" in _norm(event.signature) and \
                "disconnect" not in _norm(event.signature) else "disconnected"
            self.observe(host, "sensor:mesh", connected, kind=FactKind.SENSOR,
                         source=event.source.value, confidence=0.7,
                         now_iso=now_iso, evidence_refs=(event.event_id,))
        elif event.source is EventSource.SENSOR_MESH and host:
            connected = "disconnected" if "disconnect" in _norm(event.signature) else "connected"
            self.observe(host, "sensor:mesh", connected, kind=FactKind.SENSOR,
                         source=event.source.value, confidence=0.7,
                         now_iso=now_iso, evidence_refs=(event.event_id,))

    def observe_from_asset_graph(self, graph, *, now_iso: str | None = None) -> None:
        """Fold observed service exposures from the M20 asset graph into observed
        state (exposure drift signal source). Reads only — never mutates the graph."""
        now_iso = now_iso or _now_iso()
        try:
            for svc in graph.exposed_services(only_reachable=False):
                host = svc.get("host") or ""
                port = svc.get("port")
                if not host or port is None:
                    continue
                self.observe(host, f"exposure:{port}", svc.get("exposure") or "unknown",
                             kind=FactKind.EXPOSURE, source="asset_graph",
                             confidence=0.6, now_iso=now_iso)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"DIGITAL_TWIN: asset-graph fold failed: {e}")

    # ── drift computation ─────────────────────────────────────────────────────
    def compute_drift(self, *, now_iso: str | None = None) -> TwinSnapshot:
        now_iso = now_iso or _now_iso()
        findings: list[DriftFinding] = []
        assets = set(self._expected) | set(self._observed)
        for asset in sorted(assets):
            exp = self._expected.get(asset, ExpectedState())
            obs = self._observed.get(asset, OperationalState())
            findings.extend(StateComparator.compare(asset, exp, obs, now_iso=now_iso))
            # desired-vs-observed compliance drift (config), only where they differ
            des = self._desired.get(asset)
            if des is not None:
                for key, dfact in des.facts.items():
                    if key in exp.facts:
                        continue                  # already compared vs expected
                    obs_fact = obs.get(key)
                    if obs_fact is None or not obs_fact.known:
                        continue                  # unknown — no compliance claim
                    if _norm(dfact.value) != _norm(obs_fact.value):
                        findings.append(_build(asset, key, DriftType.CONFIG_DRIFT,
                                               dfact, obs_fact, now_iso,
                                               note="desired configuration not met"))
        by_sev: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for f in findings:
            by_sev[f.severity.value] = by_sev.get(f.severity.value, 0) + 1
            by_type[f.drift_type.value] = by_type.get(f.drift_type.value, 0) + 1
        assets_with_drift = tuple(sorted({f.asset for f in findings
                                          if f.drift_type is not DriftType.STATE_UNKNOWN}))
        return TwinSnapshot(taken_at=now_iso, findings=tuple(findings),
                            by_severity=by_sev, by_type=by_type,
                            assets_with_drift=assets_with_drift)

    # ── connectors (signals only — never automatic action) ────────────────────
    @staticmethod
    def drift_to_presence_event(finding: DriftFinding):
        """Build a PresenceEvent for the Presence Engine ladder. SUGGEST-level (a
        recommendation to investigate), never an ACT — drift proposes no action."""
        try:
            from core.presence import PresenceEvent, PresenceLevel, Urgency
        except Exception:  # noqa: BLE001
            return None
        urgency = {
            DriftSeverity.INFO: Urgency.ROUTINE, DriftSeverity.LOW: Urgency.ROUTINE,
            DriftSeverity.MEDIUM: Urgency.ELEVATED, DriftSeverity.HIGH: Urgency.HIGH,
            DriftSeverity.CRITICAL: Urgency.CRITICAL,
        }[finding.severity]
        return PresenceEvent(
            key=f"drift:{finding.finding_id}", urgency=urgency,
            message=f"{finding.drift_type.value} on {finding.asset} — "
                    f"recommend {finding.recommended_investigation}",
            desired_level=PresenceLevel.SUGGEST, requires_work=True)

    @staticmethod
    def drift_to_incident_evidence(finding: DriftFinding, case) -> None:
        """Attach a drift finding to an existing incident case as evidence. Does
        NOT open a case — drift is not automatically an incident."""
        try:
            case.add_evidence(
                "drift", f"{finding.drift_type.value} on {finding.asset} "
                f"(expected {finding.expected_fact}, observed {finding.observed_fact})",
                source="digital_twin", event_refs=list(finding.evidence_refs),
                provenance={"drift_type": finding.drift_type.value,
                            "severity": finding.severity.value})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"DIGITAL_TWIN: incident evidence attach failed: {e}")

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "observed": {a: s.to_dict() for a, s in self._observed.items()},
            "expected": {a: s.to_dict() for a, s in self._expected.items()},
            "desired": {a: s.to_dict() for a, s in self._desired.items()},
        }


# Module-level singleton. Expected/desired state is operator/config-owned; observed
# state is folded from canonical events + the asset graph. M25 (Situation Engine)
# is the first live consumer of compute_drift(); M26 serializes DriftFindings.
twin = DigitalTwin()
