"""
core/correlation_v2.py — V66 Milestone 21: evidence-linked correlation findings.

JARVIS already has ``core.correlator.TemporalCorrelator`` (sliding-window
buffering, ``CorrelationRule``, ``CompoundIncident``, MITRE hints, kill-chain
inference, severity scoring). This module does **not** replace it. It is a
compatibility-preserving V2 *layer* that:

  1. accepts canonical :class:`~core.ops_events.OperationalEvent` AND legacy dicts
     (legacy dicts still flow into the untouched ``TemporalCorrelator`` — its
     CompoundIncident / MITRE / playbook behavior is preserved byte-for-byte);
  2. matches on **structured entities** (M19 ``EntityReference`` keys) instead of
     ad-hoc per-source attribute names;
  3. attaches explicit **evidence references** to every finding and *explains why
     a rule matched*;
  4. preserves bounded windows and buffers (Rule of Silicon);
  5. **deduplicates** repeated findings per (rule, entity) burst;
  6. connects involved entities to the M20 **Asset Graph**;
  7. keeps correlation **evidence separate from incident truth** — a
     :class:`CorrelationFinding` is a *signal*, not an incident. It bridges into
     the blackboard / presence / incident workspace, which decide truth.

Transparent, deterministic patterns only — **no fake ML anomaly detector.** Each
rule is an explainable predicate over grouped canonical events.

Pure evaluation core (``ingest_event`` is synchronous and side-effect-free over
the window); the async ``ingest`` wrapper adds legacy feed + emission. Sinks and
the asset graph are dependency-injected, so the engine is unit-testable with fakes.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from loguru import logger

from core.asset_graph import AssetType, ObservationSource, RelationshipType
from core.ops_events import (
    EntityType,
    EventCategory,
    EventSeverity,
    EventSource,
    OperationalEvent,
    normalize_event,
)

# Bounds.
_MAX_WINDOW_EVENTS = 512         # hard backstop on the canonical window
_MAX_RULE_WINDOW_S = 300.0       # no rule window may exceed this
_DEDUP_CAP = 2048

# Legacy event `type`s the V2 layer treats as operational telemetry worth
# normalizing. HUD/model noise (telemetry, model_decision, …) is ignored.
OPERATIONAL_EVENT_TYPES: frozenset[str] = frozenset({
    "sysmon_event", "dpi_alert", "network_anomaly", "sensor_connected",
    "sensor_disconnected", "compound_incident", "canary_intrusion",
    "etw_threat_event", "ebpf_alert", "deception_tripped",
})

# Entity types that map to an asset-graph node (others are attributes, not assets).
_ENTITY_ASSET_TYPE: dict[EntityType, AssetType] = {
    EntityType.HOST: AssetType.PHYSICAL_HOST,
    EntityType.AGENT: AssetType.SECURITY_SENSOR,
    EntityType.IP: AssetType.UNKNOWN,
    EntityType.SERVICE: AssetType.SERVICE,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        # accept unix float strings (Zeek ts) or ISO-8601
        return float(iso)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  Rule model (transparent predicates)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class StepPredicate:
    """A predicate matching a canonical event by structure (never free-text ML)."""
    label: str
    categories: frozenset[EventCategory] | None = None
    sources: frozenset[EventSource] | None = None
    min_severity: EventSeverity | None = None
    signature_contains: tuple[str, ...] | None = None
    min_count: int = 1

    def matches(self, ev: OperationalEvent) -> bool:
        if self.categories and ev.category not in self.categories:
            return False
        if self.sources and ev.source not in self.sources:
            return False
        if self.min_severity and ev.severity.rank < self.min_severity.rank:
            return False
        if self.signature_contains:
            hay = " ".join(str(x) for x in (ev.signature, ev.rule_id,
                                            *ev.mitre_techniques) if x).lower()
            if not any(s.lower() in hay for s in self.signature_contains):
                return False
        return True


@dataclass(frozen=True)
class CorrelationRuleV2:
    """A transparent, entity-grouped correlation pattern."""
    name: str
    description: str
    window_sec: float
    group_by: EntityType
    steps: tuple[StepPredicate, ...] = ()
    ordered: bool = False
    min_group_events: int = 2
    techniques: tuple[str, ...] = ()
    # Special "same value across ≥K distinct partner entities" mode (same-IOC).
    cross_partner: EntityType | None = None
    cross_min: int = 2


# ── the 7 initial transparent patterns ────────────────────────────────────────
_HIGH = EventSeverity.HIGH
_C = EventCategory

DEFAULT_RULES: tuple[CorrelationRuleV2, ...] = (
    CorrelationRuleV2(
        name="suspicious_process_then_network",
        description="A process-creation event on a host followed by a network connection.",
        window_sec=60, group_by=EntityType.HOST, ordered=True,
        steps=(
            StepPredicate("process_activity", categories=frozenset({_C.PROCESS})),
            StepPredicate("network_activity",
                          categories=frozenset({_C.NETWORK, _C.DNS, _C.HTTP})),
        ),
        techniques=("T1059", "T1071"),
    ),
    CorrelationRuleV2(
        name="ids_alert_with_host_activity",
        description="A network/IDS alert correlated with other activity on the same IP.",
        window_sec=90, group_by=EntityType.IP, ordered=False,
        steps=(
            StepPredicate("ids_or_anomaly",
                          categories=frozenset({_C.ANOMALY, _C.DNS, _C.HTTP, _C.IDS_ALERT})),
            StepPredicate("host_or_network",
                          categories=frozenset({_C.PROCESS, _C.NETWORK})),
        ),
        techniques=("T1071",),
    ),
    CorrelationRuleV2(
        name="new_service_exposure_then_connection",
        description="A newly observed service/exposure followed by a connection attempt.",
        window_sec=120, group_by=EntityType.IP, ordered=True,
        steps=(
            StepPredicate("service_exposure",
                          signature_contains=("expos", "service", "listen", "bind")),
            StepPredicate("connection",
                          categories=frozenset({_C.NETWORK, _C.HTTP, _C.DNS})),
        ),
    ),
    CorrelationRuleV2(
        name="auth_failures_then_success",
        description="Repeated authentication failures followed by a success on one host.",
        window_sec=120, group_by=EntityType.HOST, ordered=True,
        steps=(
            StepPredicate("auth_failure", categories=frozenset({_C.AUTH}),
                          signature_contains=("fail", "4625", "denied", "invalid"),
                          min_count=3),
            StepPredicate("auth_success", categories=frozenset({_C.AUTH}),
                          signature_contains=("success", "4624", "accepted", "logon")),
        ),
        techniques=("T1110",),
    ),
    CorrelationRuleV2(
        name="sensor_plus_network_anomaly",
        description="A sensor-reported event and a network anomaly on the same asset.",
        window_sec=90, group_by=EntityType.IP, ordered=False,
        steps=(
            StepPredicate("sensor", categories=frozenset({_C.SENSOR, _C.PROCESS})),
            StepPredicate("network_anomaly", categories=frozenset({_C.ANOMALY})),
        ),
    ),
    CorrelationRuleV2(
        name="high_severity_sequence",
        description="A sequence of high-severity events on the same entity within a window.",
        window_sec=90, group_by=EntityType.IP, ordered=False, min_group_events=3,
        steps=(
            StepPredicate("high_sev", min_severity=_HIGH, min_count=3),
        ),
    ),
    CorrelationRuleV2(
        name="same_ioc_multiple_assets",
        description="The same IOC (IP) observed across multiple distinct assets.",
        window_sec=180, group_by=EntityType.IP, cross_partner=EntityType.HOST,
        cross_min=2,
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  Finding model (evidence, not incident truth)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CorrelationEvidence:
    event_id: str
    source: str
    category: str
    observed_at: str | None
    locator: str

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "source": self.source,
                "category": self.category, "observed_at": self.observed_at,
                "locator": self.locator}


@dataclass(frozen=True)
class CorrelationExplanation:
    rule: str
    summary: str
    matched_steps: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "summary": self.summary,
                "matched_steps": list(self.matched_steps), "reason": self.reason}


@dataclass(frozen=True)
class CorrelationFinding:
    """One evidence-linked correlation signal. NOT an incident — the incident
    workspace (M22) decides truth. Immutable and fully explainable."""
    finding_id: str
    rule: str
    matched_event_ids: tuple[str, ...]
    window_start: str
    window_end: str
    group_entity: str
    entities: tuple[str, ...]
    asset_refs: tuple[str, ...]
    confidence: float
    severity: str
    mitre_techniques: tuple[str, ...]
    explanation: CorrelationExplanation
    evidence: tuple[CorrelationEvidence, ...]
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id, "rule": self.rule,
            "matched_event_ids": list(self.matched_event_ids),
            "window_start": self.window_start, "window_end": self.window_end,
            "group_entity": self.group_entity, "entities": list(self.entities),
            "asset_refs": list(self.asset_refs), "confidence": round(self.confidence, 3),
            "severity": self.severity, "mitre_techniques": list(self.mitre_techniques),
            "explanation": self.explanation.to_dict(),
            "evidence": [e.to_dict() for e in self.evidence],
            "created_at": self.created_at,
        }

    def to_aura_event(self) -> dict:
        return {"type": "correlation_finding", **self.to_dict()}


# ── internal windowed record ──────────────────────────────────────────────────
@dataclass
class _WEvent:
    seq: int
    time: float
    event: OperationalEvent


# ══════════════════════════════════════════════════════════════════════════════
#  The engine
# ══════════════════════════════════════════════════════════════════════════════
FindingSink = Callable[[CorrelationFinding], "Awaitable[None]"]


class CorrelatorV2:
    """Structured, evidence-linked correlation over canonical events. Wraps (never
    replaces) the legacy TemporalCorrelator for backward compatibility."""

    def __init__(self, rules: tuple[CorrelationRuleV2, ...] = DEFAULT_RULES,
                 *, legacy=None, asset_graph=None) -> None:
        self.rules = tuple(r for r in rules if r.window_sec <= _MAX_RULE_WINDOW_S)
        self._legacy = legacy                     # TemporalCorrelator | None
        self._graph = asset_graph                 # AssetGraph | None (populated by findings)
        self._window: deque[_WEvent] = deque()
        self._seq = 0
        self._fired: dict[str, float] = {}        # (rule,group) -> last anchor epoch
        self._fired_order: deque[str] = deque()
        self._broadcast_fn: Callable[[dict], Awaitable[None]] | None = None
        self._sinks: list[FindingSink] = []
        self._max_window = max((r.window_sec for r in self.rules), default=90.0)

    # ── wiring ────────────────────────────────────────────────────────────────
    def attach(self, *, legacy=None, asset_graph=None, broadcast_fn=None,
               sinks: "list[FindingSink] | None" = None) -> None:
        if legacy is not None:
            self._legacy = legacy
        if asset_graph is not None:
            self._graph = asset_graph
        if broadcast_fn is not None:
            self._broadcast_fn = broadcast_fn
        if sinks:
            self._sinks.extend(sinks)

    def add_sink(self, sink: FindingSink) -> None:
        self._sinks.append(sink)

    # ── pure evaluation core ──────────────────────────────────────────────────
    def ingest_event(self, event: OperationalEvent) -> list[CorrelationFinding]:
        """Add one canonical event and return any NEW findings (deduped). Pure and
        synchronous — no legacy feed, no emission. This is the deterministic core."""
        self._seq += 1
        anchor = _epoch(event.observed_at or event.timestamp) or float(self._seq)
        self._window.append(_WEvent(self._seq, anchor, event))
        self._prune(anchor)

        findings: list[CorrelationFinding] = []
        for rule in self.rules:
            lo = anchor - rule.window_sec
            candidates = [w for w in self._window if w.time >= lo]
            if len(candidates) < max(1, rule.min_group_events) and not rule.cross_partner:
                continue
            for match in self._eval_rule(rule, candidates):
                f = self._build_finding(rule, match, anchor)
                if f is not None:
                    findings.append(f)
        return findings

    def _prune(self, anchor: float) -> None:
        lo = anchor - self._max_window
        while self._window and self._window[0].time < lo:
            self._window.popleft()
        while len(self._window) > _MAX_WINDOW_EVENTS:
            self._window.popleft()

    def _group(self, rule: CorrelationRuleV2,
               candidates: list[_WEvent]) -> dict[str, list[_WEvent]]:
        groups: dict[str, list[_WEvent]] = {}
        for w in candidates:
            for ent in w.event.entities:
                if ent.type is rule.group_by and ent.value:
                    groups.setdefault(ent.value.strip().lower(), []).append(w)
        return groups

    def _eval_rule(self, rule: CorrelationRuleV2,
                   candidates: list[_WEvent]) -> list[dict]:
        groups = self._group(rule, candidates)
        matches: list[dict] = []
        for gval, gevents in groups.items():
            gevents = sorted(gevents, key=lambda w: w.seq)
            if rule.cross_partner is not None:
                m = self._match_cross(rule, gval, gevents)
            elif rule.ordered:
                m = self._match_ordered(rule, gval, gevents)
            else:
                m = self._match_unordered(rule, gval, gevents)
            if m is not None:
                matches.append(m)
        return matches

    def _match_cross(self, rule, gval, gevents) -> dict | None:
        partners: set[str] = set()
        for w in gevents:
            for ent in w.event.entities:
                if ent.type is rule.cross_partner and ent.value:
                    partners.add(ent.value.strip().lower())
        if len(partners) < rule.cross_min:
            return None
        return {"group": gval, "events": gevents,
                "steps": [f"ioc seen on {len(partners)} distinct assets"]}

    def _match_unordered(self, rule, gval, gevents) -> dict | None:
        used: list[_WEvent] = []
        labels: list[str] = []
        for pred in rule.steps:
            hits = [w for w in gevents if pred.matches(w.event)]
            if len(hits) < pred.min_count:
                return None
            used.extend(hits[:pred.min_count])
            labels.append(f"{pred.label}×{len(hits)}")
        if len({w.seq for w in used}) < 1:
            return None
        return {"group": gval, "events": used, "steps": labels}

    def _match_ordered(self, rule, gval, gevents) -> dict | None:
        chain: list[_WEvent] = []
        labels: list[str] = []
        cursor = -1
        for pred in rule.steps:
            picked: list[_WEvent] = []
            for w in gevents:
                if w.seq <= cursor:
                    continue
                if pred.matches(w.event):
                    picked.append(w)
                    if len(picked) >= pred.min_count:
                        break
            if len(picked) < pred.min_count:
                return None
            chain.extend(picked)
            cursor = picked[-1].seq
            labels.append(f"{pred.label}×{pred.min_count}")
        # bounded window: the whole chain must span ≤ rule.window_sec
        span = chain[-1].time - chain[0].time
        if span > rule.window_sec:
            return None
        return {"group": gval, "events": chain, "steps": labels}

    def _build_finding(self, rule, match, anchor) -> CorrelationFinding | None:
        events = match["events"]
        key = f"{rule.name}|{match['group']}"
        prev = self._fired.get(key)
        if prev is not None and (anchor - prev) < rule.window_sec:
            return None                      # dedup: same (rule,entity) burst
        self._mark_fired(key, anchor)

        event_ids = tuple(dict.fromkeys(w.event.event_id for w in events))
        entities = tuple(dict.fromkeys(
            k for w in events for k in w.event.entity_keys()))
        techniques = tuple(dict.fromkeys(
            (*rule.techniques, *(t for w in events for t in w.event.mitre_techniques))))
        times = sorted(w.time for w in events)
        window_start = _iso_from_epoch(times[0])
        window_end = _iso_from_epoch(times[-1])
        max_rank = max((w.event.severity.rank for w in events), default=0)
        severity = _severity_from_rank(max_rank)
        confidence = min(0.95, 0.5 + 0.06 * (len(events) - 1) + 0.05 * max_rank)

        asset_refs = tuple(dict.fromkeys(
            f"{_ENTITY_ASSET_TYPE[et].value}:{val}"
            for w in events for et, val in
            ((e.type, e.value.strip().lower()) for e in w.event.entities)
            if et in _ENTITY_ASSET_TYPE and val))

        evidence = tuple(
            CorrelationEvidence(
                event_id=w.event.event_id, source=w.event.source.value,
                category=w.event.category.value, observed_at=w.event.observed_at,
                locator=(w.event.evidence[0].locator if w.event.evidence
                         else w.event.raw_reference),
            ) for w in events)

        explanation = CorrelationExplanation(
            rule=rule.name,
            summary=rule.description,
            matched_steps=tuple(match["steps"]),
            reason=(f"grouped {len(events)} event(s) by {rule.group_by.value}="
                    f"{match['group']} within {rule.window_sec:.0f}s; "
                    f"matched: {', '.join(match['steps'])}"),
        )
        blob = f"{rule.name}|" + "|".join(sorted(event_ids))
        fid = "cf_" + hashlib.sha256(blob.encode()).hexdigest()[:16]
        return CorrelationFinding(
            finding_id=fid, rule=rule.name, matched_event_ids=event_ids,
            window_start=window_start, window_end=window_end,
            group_entity=f"{rule.group_by.value}:{match['group']}",
            entities=entities, asset_refs=asset_refs, confidence=confidence,
            severity=severity, mitre_techniques=techniques,
            explanation=explanation, evidence=evidence, created_at=_now_iso(),
        )

    def _mark_fired(self, key: str, anchor: float) -> None:
        if key not in self._fired:
            self._fired_order.append(key)
        self._fired[key] = anchor
        while len(self._fired_order) > _DEDUP_CAP:
            old = self._fired_order.popleft()
            self._fired.pop(old, None)

    # ── async ingest (legacy feed + emission) ─────────────────────────────────
    async def ingest(self, event) -> list[CorrelationFinding]:
        """Ingest a legacy dict OR a canonical event. Legacy dicts are ALSO fed to
        the wrapped TemporalCorrelator (if attached) so existing CompoundIncident
        behavior is preserved. Structured findings are then emitted to sinks."""
        canonical: OperationalEvent | None = None
        legacy_dict: dict | None = None
        if isinstance(event, OperationalEvent):
            canonical = event
        elif isinstance(event, dict):
            legacy_dict = event
            res = normalize_event(event)
            canonical = res.event if res.ok else None

        if legacy_dict is not None and self._legacy is not None:
            try:
                await self._legacy.ingest(legacy_dict)
            except Exception as e:  # noqa: BLE001 — legacy feed never breaks V2
                logger.debug(f"CORRELATION_V2: legacy feed failed: {e}")

        if canonical is None:
            return []
        findings = self.ingest_event(canonical)
        for f in findings:
            await self._emit(f)
        return findings

    def feed(self, event: dict) -> None:
        """Fire-and-forget entry point for the live broadcast tap. Only operational
        telemetry types are normalized; HUD/model noise is ignored. Schedules the
        async ingest on the running loop; a no-op when no loop is running."""
        try:
            etype = event.get("type") if isinstance(event, dict) else None
            is_zeek_conn = isinstance(event, dict) and "type" not in event and (
                "id.orig_h" in event or "id.resp_h" in event)
            if etype not in OPERATIONAL_EVENT_TYPES and not is_zeek_conn:
                return
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        except Exception:  # noqa: BLE001
            return
        loop.create_task(self.ingest(event), name="corrv2-ingest")

    async def _emit(self, finding: CorrelationFinding) -> None:
        self._link_assets(finding)
        if self._broadcast_fn is not None:
            try:
                await self._broadcast_fn(finding.to_aura_event())
            except Exception as e:  # noqa: BLE001
                logger.debug(f"CORRELATION_V2: broadcast failed: {e}")
        for sink in self._sinks:
            try:
                await sink(finding)
            except Exception as e:  # noqa: BLE001 — a bad sink never breaks the engine
                logger.debug(f"CORRELATION_V2: sink failed: {e}")
        logger.info(f"CORRELATION_V2: finding {finding.finding_id} [{finding.rule}] "
                    f"conf={finding.confidence:.2f} on {finding.group_entity}")

    def _link_assets(self, finding: CorrelationFinding) -> None:
        """Connect involved entities to the Asset Graph as low-confidence
        'seen_in_correlation' evidence, and record IP↔IP co-occurrence. Never
        fabricates strong topology; the observation is explicitly canonical-event
        sourced and low confidence, so operator/vetted facts always outrank it."""
        if self._graph is None:
            return
        ips: list[str] = []
        try:
            for key in finding.entities:
                etype, _, value = key.partition(":")
                if not value:
                    continue
                if etype == EntityType.HOST.value:
                    self._graph.add_observation(
                        AssetType.PHYSICAL_HOST, value, "seen_in_correlation",
                        finding.rule, source=ObservationSource.CANONICAL_EVENT,
                        confidence=0.3, event_refs=finding.matched_event_ids)
                elif etype == EntityType.AGENT.value:
                    self._graph.add_observation(
                        AssetType.SECURITY_SENSOR, value, "seen_in_correlation",
                        finding.rule, source=ObservationSource.CANONICAL_EVENT,
                        confidence=0.3, event_refs=finding.matched_event_ids)
                elif etype == EntityType.IP.value:
                    ips.append(value)
            for i in range(len(ips)):
                for j in range(i + 1, len(ips)):
                    self._graph.add_relationship(
                        AssetType.UNKNOWN, ips[i], RelationshipType.COMMUNICATES_WITH,
                        AssetType.UNKNOWN, ips[j],
                        source=ObservationSource.NETWORK_OBSERVATION, confidence=0.3,
                        event_refs=finding.matched_event_ids)
        except Exception as e:  # noqa: BLE001 — asset linking never breaks emission
            logger.debug(f"CORRELATION_V2: asset link failed: {e}")


def _iso_from_epoch(epoch: float) -> str:
    if not epoch:
        return _now_iso()
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return _now_iso()


def _severity_from_rank(rank: int) -> str:
    for sev in EventSeverity:
        if sev.rank == rank:
            return sev.value
    return EventSeverity.UNKNOWN.value


# Module-level singleton. In production the live tap (aura.server.broadcast) feeds
# it via feed(); the legacy TemporalCorrelator stays driven by the existing path
# (no double ingest). Involved entities link into the shared M20 asset graph.
# main.py attaches the broadcast_fn and incident sink (M22).
from core.asset_graph import graph as _asset_graph

correlator_v2 = CorrelatorV2(asset_graph=_asset_graph)
