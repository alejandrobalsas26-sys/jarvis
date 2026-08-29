"""
core/world_state.py — V69 M63: the one canonical situational World State.

THE QUESTION THIS ANSWERS
-------------------------
    "What appears to be true about the AUTHORIZED environment right now?"

It is deliberately NOT long-term memory. :mod:`core.memory_fabric` owns history,
decisions and durable context; this owns *current and recent environment truth*,
which has a freshness, decays, and is allowed to be wrong in a way a recorded
decision is not.

WHY A FACADE AND NOT A NEW STORE
--------------------------------
The repository already has an evidence-backed, conflict-aware, bounded system
graph — :class:`core.asset_graph.AssetGraph` — which is live-populated by
:class:`core.correlation_v2.CorrelatorV2` on every correlation finding. Building
a second entity store beside it would create two answers to "is host X online?"
and no rule for which wins. So this module OWNS NO ENTITIES. It projects the
graph into the situational contract (status, health, freshness, staleness,
provenance, revision) and adds the three things the graph deliberately does not
have: a staleness policy, a bounded change log, and dependency-impact queries.

Everything here is DETERMINISTIC. No language model is consulted to decide
whether a service is down, and none can be: a state fabric that needs inference
to answer "is it online" is unavailable exactly when it matters most.

TRUST
-----
External text is DATA, never instruction. :func:`WorldObservation.safe_text`
routes any operator-facing string from an untrusted source through
:mod:`core.injection_firewall` before it can reach a prompt or the Memory
Fabric. A connector cannot widen authority, and nothing in this module calls a
tool, opens a socket or causes an effect.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from loguru import logger

from core.asset_graph import (
    Asset,
    AssetGraph,
    AssetType,
    ObservationSource,
    RelationshipType,
    asset_id,
)
from core.world_bounds import DEFAULT_BOUNDS, WorldBounds

SCHEMA_VERSION = "world-state-1"
OBSERVATION_SCHEMA_VERSION = "world-observation-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 instant. Returns None rather than raising — a bad
    timestamp on one observation may not take down an ingest batch."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
#  Vocabulary
# ══════════════════════════════════════════════════════════════════════════════
class EntityStatus(str, Enum):
    """What the environment appears to be doing. ``UNKNOWN`` is a real answer."""
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    STALE = "stale"            # last observed too long ago to still claim
    GONE = "gone"              # was observed, then explicitly disappeared


class EntityHealth(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


class ObservationTrust(str, Enum):
    """How much weight a claim carries. Derived from WHO said it, never from
    what it said — a connector cannot promote its own trust by asserting one."""
    OPERATOR = "operator"          # the human, or vetted config
    INSTRUMENTED = "instrumented"  # a first-party probe we ran
    REPORTED = "reported"          # a third-party system told us
    UNTRUSTED = "untrusted"        # arbitrary external text


class ChangeKind(str, Enum):
    ENTITY_APPEARED = "entity_appeared"
    ENTITY_STALE = "entity_stale"
    ENTITY_DISAPPEARED = "entity_disappeared"
    STATUS_CHANGED = "status_changed"
    HEALTH_CHANGED = "health_changed"
    SERVICE_STARTED = "service_started"
    SERVICE_STOPPED = "service_stopped"
    ADDRESS_CHANGED = "address_changed"
    DEPENDENCY_CHANGED = "dependency_changed"
    ALERT_SEVERITY_CHANGED = "alert_severity_changed"
    SENSOR_THRESHOLD_CROSSED = "sensor_threshold_crossed"


#: Trust for each graph observation source. Operator statements and vetted
#: config outrank machine probes, which outrank what a third party reported.
_TRUST_BY_SOURCE: dict[ObservationSource, ObservationTrust] = {
    ObservationSource.OPERATOR_DECLARATION: ObservationTrust.OPERATOR,
    ObservationSource.TRUSTED_CONFIG: ObservationTrust.OPERATOR,
    ObservationSource.DOCKER_INSPECT: ObservationTrust.INSTRUMENTED,
    ObservationSource.LAB_MANAGER: ObservationTrust.INSTRUMENTED,
    ObservationSource.SERVICE_OBSERVATION: ObservationTrust.INSTRUMENTED,
    ObservationSource.NETWORK_OBSERVATION: ObservationTrust.INSTRUMENTED,
    ObservationSource.INTERNAL: ObservationTrust.INSTRUMENTED,
    ObservationSource.SENSOR_MESH: ObservationTrust.REPORTED,
    ObservationSource.CANONICAL_EVENT: ObservationTrust.REPORTED,
}

#: Default confidence per trust tier, mirroring ``asset_graph._DEFAULT_CONFIDENCE``.
#: An explicit ``confidence=`` still wins; this is only the default.
_CONFIDENCE_BY_TRUST: dict[ObservationTrust, float] = {
    ObservationTrust.OPERATOR: 0.95,
    ObservationTrust.INSTRUMENTED: 0.7,
    ObservationTrust.REPORTED: 0.55,
    ObservationTrust.UNTRUSTED: 0.3,
}

#: How long an entity of each kind stays claimable without a fresh observation.
#: A container can vanish in seconds; a physical host does not stop existing
#: because nobody looked at it for an hour. Absent kinds use ``_DEFAULT_STALE``.
_DEFAULT_STALE_S = 900.0
_STALE_AFTER_S: dict[AssetType, float] = {
    AssetType.CONTAINER: 120.0,
    AssetType.PROCESS: 120.0,
    AssetType.SERVICE: 300.0,
    AssetType.VM: 600.0,
    AssetType.MODEL_RUNTIME: 300.0,
    AssetType.SECURITY_SENSOR: 600.0,
    AssetType.ALERT_SOURCE: 900.0,
    AssetType.PHYSICAL_HOST: 3_600.0,
    AssetType.SERVER: 3_600.0,
    AssetType.WORKSTATION: 3_600.0,
    AssetType.LAPTOP: 3_600.0,
    AssetType.NETWORK: 7_200.0,
    AssetType.SUBNET: 7_200.0,
    AssetType.PROJECT: 86_400.0,
    AssetType.TOOL: 86_400.0,
}

#: Attribute names the projection reads for status / health. A connector writes
#: these; nothing infers them from prose.
_STATUS_ATTR = "status"
_HEALTH_ATTR = "health"
_ADDRESS_ATTRS = ("ip", "address", "bind_addr", "endpoint")


class WorldStateError(ValueError):
    """An observation that will not be ingested as described."""


# ══════════════════════════════════════════════════════════════════════════════
#  §12 — the normalized observation contract
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class WorldObservation:
    """One normalized claim about one entity, from one source, at one time.

    Construct through :meth:`build`, which fails CLOSED on anything malformed.
    A direct constructor call is unvalidated by design so that internal,
    already-trusted callers do not pay validation twice.
    """
    observation_id: str
    source_id: str
    source_type: str
    entity_id: str
    entity_type: AssetType
    event_type: str
    observed_at: str
    ingested_at: str
    severity: str = "INFO"
    confidence: float = 0.5
    trust: ObservationTrust = ObservationTrust.REPORTED
    provenance: str = ""
    correlation_id: str = ""
    ttl_s: float = _DEFAULT_STALE_S
    payload: dict = field(default_factory=dict)
    schema_version: str = OBSERVATION_SCHEMA_VERSION

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def build(
        cls,
        *,
        source_id: str,
        source_type: str,
        entity_type: "AssetType | str",
        identity: str,
        event_type: str,
        observed_at: str | None = None,
        severity: str = "INFO",
        confidence: float | None = None,
        trust: "ObservationTrust | str" = ObservationTrust.REPORTED,
        provenance: str = "",
        correlation_id: str = "",
        ttl_s: float | None = None,
        payload: dict | None = None,
        bounds: WorldBounds = DEFAULT_BOUNDS,
    ) -> "WorldObservation":
        """Validate and normalize. Raises :class:`WorldStateError` on anything
        that cannot be ingested truthfully.

        Fails closed on: a missing source, a missing identity, an unknown entity
        type, an oversized payload, a non-serializable payload. Tolerates (by
        ignoring) unknown optional fields inside ``payload`` — an upstream that
        grows a field must not break ingestion.
        """
        if not str(source_id or "").strip():
            raise WorldStateError("observation: source_id is required; an "
                                  "unattributed claim cannot be trusted or aged")
        if not str(identity or "").strip():
            raise WorldStateError("observation: identity is required; an entity "
                                  "with no identity cannot be deduplicated")
        if not str(event_type or "").strip():
            raise WorldStateError("observation: event_type is required")

        if isinstance(entity_type, AssetType):
            etype = entity_type
        else:
            try:
                etype = AssetType(str(entity_type))
            except ValueError as exc:
                raise WorldStateError(
                    f"observation: {entity_type!r} is not a known entity type; "
                    f"ingesting it would invent a category") from exc

        payload = payload if isinstance(payload, dict) else {}
        try:
            encoded = json.dumps(payload, default=str, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise WorldStateError("observation: payload is not JSON-serializable") from exc
        if len(encoded.encode("utf-8")) > bounds.max_payload_bytes:
            raise WorldStateError(
                f"observation: payload is larger than the {bounds.max_payload_bytes}-byte "
                f"ceiling; it is refused rather than truncated, because a truncated "
                f"payload is a different claim than the one that was made")

        trust_v = trust if isinstance(trust, ObservationTrust) else ObservationTrust(str(trust))
        # Confidence DEFAULTS FROM TRUST. Passing 0.5 for everything would make
        # an operator statement and a third-party report weigh the same, and the
        # graph's conflict resolution would then be decided by string ordering.
        if confidence is None:
            confidence = _CONFIDENCE_BY_TRUST[trust_v]
        now = _now()
        obs_at = observed_at or _iso(now)
        if _parse_iso(obs_at) is None:
            raise WorldStateError(f"observation: observed_at {obs_at!r} is not ISO-8601")

        eid = asset_id(etype, identity)
        oid = hashlib.sha256(
            "|".join([eid, str(source_id), str(event_type), obs_at, encoded])
            .encode("utf-8")
        ).hexdigest()[:32]

        return cls(
            observation_id=oid,
            source_id=str(source_id)[:bounds.max_text_field_chars],
            source_type=str(source_type)[:bounds.max_text_field_chars],
            entity_id=eid,
            entity_type=etype,
            event_type=str(event_type)[:bounds.max_text_field_chars],
            observed_at=obs_at,
            ingested_at=_iso(now),
            severity=str(severity or "INFO").upper()[:32],
            confidence=max(0.0, min(1.0, float(confidence))),
            trust=trust_v,
            provenance=str(provenance)[:bounds.max_text_field_chars],
            correlation_id=str(correlation_id)[:128],
            ttl_s=float(ttl_s) if ttl_s is not None else _STALE_AFTER_S.get(
                etype, _DEFAULT_STALE_S),
            payload=payload,
        )

    # ── trust ────────────────────────────────────────────────────────────────
    def safe_text(self, value: str, *, max_chars: int = 512) -> str:
        """Render one payload string for operator/model consumption.

        Anything not operator-authored is treated as untrusted DATA and passed
        through the injection firewall, so a container label reading "ignore
        previous instructions" arrives as inert quoted evidence.
        """
        if self.trust is ObservationTrust.OPERATOR:
            return str(value)[:max_chars]
        try:
            from core.injection_firewall import TrustOrigin, apply_firewall
            return apply_firewall(str(value), TrustOrigin.FILE_UNTRUSTED,
                                  max_chars=max_chars).safe_content
        except Exception as exc:  # noqa: BLE001 — never fail an ingest on this
            logger.warning(f"WORLD_STATE: firewall unavailable ({exc}); defanging")
            return str(value)[:max_chars].replace("\n", " ")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "source_id": self.source_id, "source_type": self.source_type,
            "entity_id": self.entity_id, "entity_type": self.entity_type.value,
            "event_type": self.event_type,
            "observed_at": self.observed_at, "ingested_at": self.ingested_at,
            "severity": self.severity, "confidence": round(self.confidence, 3),
            "trust": self.trust.value, "provenance": self.provenance,
            "correlation_id": self.correlation_id, "ttl_s": self.ttl_s,
            "payload_keys": sorted(self.payload)[:32],
        }


# ══════════════════════════════════════════════════════════════════════════════
#  §11 — the entity contract (a projection, never a second store)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class WorldEntity:
    entity_id: str
    entity_type: AssetType
    display_name: str
    status: EntityStatus
    health: EntityHealth
    source: str
    trust: ObservationTrust
    provenance: str
    first_seen: str
    last_seen: str
    observed_at: str
    freshness_s: float
    stale_after_s: float
    confidence: float
    attributes: dict
    revision: int

    @property
    def is_stale(self) -> bool:
        return self.freshness_s > self.stale_after_s

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id, "entity_type": self.entity_type.value,
            "display_name": self.display_name, "status": self.status.value,
            "health": self.health.value, "source": self.source,
            "trust": self.trust.value, "provenance": self.provenance,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "observed_at": self.observed_at,
            "freshness_s": round(self.freshness_s, 1),
            "stale_after_s": self.stale_after_s,
            "confidence": round(self.confidence, 3),
            "attributes": self.attributes, "revision": self.revision,
            "is_stale": self.is_stale,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  §15 — change detection
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class StateChange:
    kind: ChangeKind
    entity_id: str
    entity_type: AssetType
    attribute: str
    before: str | None
    after: str | None
    at: str
    source: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value, "entity_id": self.entity_id,
            "entity_type": self.entity_type.value, "attribute": self.attribute,
            "before": self.before, "after": self.after, "at": self.at,
            "source": self.source, "confidence": round(self.confidence, 3),
        }

    def describe(self) -> str:
        """A deterministic sentence. No model is consulted to produce it."""
        if self.kind is ChangeKind.ENTITY_APPEARED:
            return f"{self.entity_id} appeared (source {self.source})"
        if self.kind is ChangeKind.ENTITY_STALE:
            return f"{self.entity_id} went stale; last observed {self.before}"
        if self.kind is ChangeKind.ENTITY_DISAPPEARED:
            return f"{self.entity_id} disappeared (source {self.source})"
        return (f"{self.entity_id} {self.attribute}: "
                f"{self.before or 'unknown'} -> {self.after or 'unknown'}")


# ══════════════════════════════════════════════════════════════════════════════
#  The World State
# ══════════════════════════════════════════════════════════════════════════════
class WorldState:
    """The canonical situational view. Deterministic, bounded, conflict-aware.

    Owns no entities: :attr:`graph` is the store. What this adds is the
    situational contract — freshness, staleness, status/health projection, a
    bounded change log, and dependency-impact queries.
    """

    def __init__(self, graph: AssetGraph | None = None, *,
                 bounds: WorldBounds | None = None, clock=None) -> None:
        self.graph = graph if graph is not None else AssetGraph()
        self.bounds = bounds or DEFAULT_BOUNDS
        self._clock = clock or time.time
        self._changes: list[StateChange] = []
        self._revisions: dict[str, int] = {}
        self._gone: dict[str, str] = {}          # entity_id -> when marked gone
        self._ingested: set[str] = set()          # observation ids, bounded
        self._ingest_order: list[str] = []

    # ── §12/§13 ingestion ────────────────────────────────────────────────────
    def ingest(self, obs: WorldObservation) -> bool:
        """Fold one observation into the state.

        Returns True if it changed anything, False if it was a duplicate or was
        refused. Deduplicated by ``observation_id``, which covers the entity,
        the source, the event, the instant and the payload — so a connector that
        re-reports an unchanged reading corroborates instead of churning.
        """
        if obs.observation_id in self._ingested:
            return False
        if len(self.graph.assets) >= self.bounds.max_entities and \
                obs.entity_id not in self.graph.assets:
            logger.warning(
                f"WORLD_STATE: entity ceiling {self.bounds.max_entities} reached; "
                f"refusing new entity {obs.entity_id}. This is a DEGRADED state, "
                f"not a silent drop")
            return False

        source = _source_for(obs)
        before = self.get_entity(obs.entity_id)
        identity = obs.entity_id.split(":", 1)[1] if ":" in obs.entity_id else obs.entity_id

        # The payload's scalar fields become evidence-backed attributes. Nested
        # structures are NOT flattened into the graph: a graph attribute is a
        # claim a human can read, and an exploded object is neither.
        wrote = False
        for key, value in sorted(obs.payload.items()):
            if isinstance(value, (dict, list)):
                continue
            if value is None:
                continue
            self.graph.add_observation(
                obs.entity_type, identity, str(key)[:64], str(value)[:1024],
                source=source, confidence=obs.confidence,
                observer=obs.source_id, event_refs=(obs.observation_id,),
                now_iso=obs.observed_at,
            )
            wrote = True
        if not wrote:
            # Even a payload-free observation is evidence the entity EXISTS and
            # was seen; record that rather than discarding the sighting.
            self.graph.add_observation(
                obs.entity_type, identity, "observed", obs.event_type,
                source=source, confidence=obs.confidence, observer=obs.source_id,
                event_refs=(obs.observation_id,), now_iso=obs.observed_at)

        self._gone.pop(obs.entity_id, None)
        self._revisions[obs.entity_id] = self._revisions.get(obs.entity_id, 0) + 1
        self._remember_ingest(obs.observation_id)
        self._record_transitions(before, self.get_entity(obs.entity_id), obs)
        return True

    def ingest_many(self, observations) -> dict:
        """Bounded batch ingest. Never raises on one bad member: a malformed
        observation is counted and skipped so a good batch still lands."""
        accepted = duplicates = refused = 0
        for i, obs in enumerate(observations):
            if i >= self.bounds.max_observations_per_ingest:
                logger.warning(f"WORLD_STATE: ingest batch truncated at "
                               f"{self.bounds.max_observations_per_ingest}")
                break
            if not isinstance(obs, WorldObservation):
                refused += 1
                continue
            try:
                if self.ingest(obs):
                    accepted += 1
                else:
                    duplicates += 1
            except Exception as exc:  # noqa: BLE001 — one bad row, not a batch
                logger.warning(f"WORLD_STATE: refusing observation: {exc}")
                refused += 1
        return {"accepted": accepted, "duplicates": duplicates, "refused": refused}

    def _remember_ingest(self, oid: str) -> None:
        self._ingested.add(oid)
        self._ingest_order.append(oid)
        ceiling = self.bounds.max_observations_per_ingest * 4
        if len(self._ingest_order) > ceiling:
            for old in self._ingest_order[: ceiling // 4]:
                self._ingested.discard(old)
            del self._ingest_order[: ceiling // 4]

    # ── §11 projection ───────────────────────────────────────────────────────
    def get_entity(self, entity_id: str) -> WorldEntity | None:
        asset = self.graph.get_by_id(entity_id)
        if asset is None:
            return None
        return self._project(asset)

    def _project(self, asset: Asset) -> WorldEntity:
        etype = asset.current_type()
        stale_after = _STALE_AFTER_S.get(etype, _DEFAULT_STALE_S)

        last_seen_dt: datetime | None = None
        first_seen_dt: datetime | None = None
        best_source = ObservationSource.INTERNAL
        best_conf = 0.0
        attributes: dict[str, str] = {}
        for name, observations in asset.attributes.items():
            current = asset.current(name)
            if current:
                attributes[name] = current[0]
                if current[1] > best_conf:
                    best_conf = current[1]
            for o in observations:
                ls, fs = _parse_iso(o.last_seen), _parse_iso(o.first_seen)
                if ls and (last_seen_dt is None or ls > last_seen_dt):
                    last_seen_dt, best_source = ls, o.source
                if fs and (first_seen_dt is None or fs < first_seen_dt):
                    first_seen_dt = fs

        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        last_seen_dt = last_seen_dt or _parse_iso(asset.created_at) or now
        first_seen_dt = first_seen_dt or _parse_iso(asset.created_at) or now
        freshness = max(0.0, (now - last_seen_dt).total_seconds())

        status = self._status_for(asset, entity_id=asset.id, attributes=attributes,
                                  freshness=freshness, stale_after=stale_after)
        health = _health_from(attributes.get(_HEALTH_ATTR), status)

        return WorldEntity(
            entity_id=asset.id, entity_type=etype,
            display_name=asset.identity,
            status=status, health=health,
            source=best_source.value,
            trust=_TRUST_BY_SOURCE.get(best_source, ObservationTrust.REPORTED),
            provenance=f"asset_graph/{best_source.value}",
            first_seen=_iso(first_seen_dt), last_seen=_iso(last_seen_dt),
            observed_at=_iso(last_seen_dt),
            freshness_s=freshness, stale_after_s=stale_after,
            confidence=best_conf,
            attributes=dict(sorted(attributes.items())[:32]),
            revision=self._revisions.get(asset.id, 1),
        )

    def _status_for(self, asset: Asset, *, entity_id: str, attributes: dict,
                    freshness: float, stale_after: float) -> EntityStatus:
        """Status is READ, then aged. It is never inferred from prose."""
        if entity_id in self._gone:
            return EntityStatus.GONE
        declared = (attributes.get(_STATUS_ATTR) or "").strip().lower()
        if freshness > stale_after:
            return EntityStatus.STALE
        if declared in ("running", "up", "online", "active", "reachable", "healthy"):
            return EntityStatus.ONLINE
        if declared in ("stopped", "down", "offline", "exited", "unreachable", "dead"):
            return EntityStatus.OFFLINE
        if declared in ("degraded", "unhealthy", "warning", "partial"):
            return EntityStatus.DEGRADED
        if declared:
            return EntityStatus.UNKNOWN
        return EntityStatus.ONLINE if asset.attributes else EntityStatus.UNKNOWN

    # ── §15 transitions ──────────────────────────────────────────────────────
    def _record_transitions(self, before: WorldEntity | None,
                            after: WorldEntity | None, obs: WorldObservation) -> None:
        if after is None:
            return
        at, src = obs.observed_at, obs.source_id
        if before is None:
            self._append(StateChange(
                ChangeKind.ENTITY_APPEARED, after.entity_id, after.entity_type,
                "existence", None, after.status.value, at, src, obs.confidence))
            return
        if before.status is not after.status:
            kind = ChangeKind.STATUS_CHANGED
            if after.entity_type is AssetType.SERVICE:
                if after.status is EntityStatus.ONLINE:
                    kind = ChangeKind.SERVICE_STARTED
                elif after.status is EntityStatus.OFFLINE:
                    kind = ChangeKind.SERVICE_STOPPED
            self._append(StateChange(
                kind, after.entity_id, after.entity_type, _STATUS_ATTR,
                before.status.value, after.status.value, at, src, obs.confidence))
        if before.health is not after.health:
            self._append(StateChange(
                ChangeKind.HEALTH_CHANGED, after.entity_id, after.entity_type,
                _HEALTH_ATTR, before.health.value, after.health.value, at, src,
                obs.confidence))
        for attr in _ADDRESS_ATTRS:
            old, new = before.attributes.get(attr), after.attributes.get(attr)
            if old and new and old != new:
                self._append(StateChange(
                    ChangeKind.ADDRESS_CHANGED, after.entity_id, after.entity_type,
                    attr, old, new, at, src, obs.confidence))
        old_sev = (before.attributes.get("severity") or "").upper()
        new_sev = (after.attributes.get("severity") or "").upper()
        if old_sev and new_sev and old_sev != new_sev:
            self._append(StateChange(
                ChangeKind.ALERT_SEVERITY_CHANGED, after.entity_id,
                after.entity_type, "severity", old_sev, new_sev, at, src,
                obs.confidence))

    def _append(self, change: StateChange) -> None:
        self._changes.append(change)
        if len(self._changes) > self.bounds.max_change_history:
            del self._changes[: len(self._changes) - self.bounds.max_change_history]

    def sweep_stale(self) -> list[StateChange]:
        """Age the world. Emits ENTITY_STALE once per entity that crossed its
        freshness ceiling since the last sweep. Deterministic; no I/O."""
        emitted: list[StateChange] = []
        already = {c.entity_id for c in self._changes
                   if c.kind is ChangeKind.ENTITY_STALE}
        for entity in self.all_entities():
            if entity.is_stale and entity.entity_id not in already:
                change = StateChange(
                    ChangeKind.ENTITY_STALE, entity.entity_id, entity.entity_type,
                    "freshness", entity.last_seen, None,
                    _iso(datetime.fromtimestamp(self._clock(), tz=timezone.utc)),
                    entity.source, entity.confidence)
                self._append(change)
                emitted.append(change)
        return emitted

    def mark_gone(self, entity_id: str, *, source: str = "operator") -> StateChange | None:
        """Explicitly record that an entity is no longer there. Never inferred:
        absence of evidence ages an entity to STALE, and only a positive
        observation of removal makes it GONE."""
        entity = self.get_entity(entity_id)
        if entity is None:
            return None
        now = _iso(datetime.fromtimestamp(self._clock(), tz=timezone.utc))
        self._gone[entity_id] = now
        change = StateChange(
            ChangeKind.ENTITY_DISAPPEARED, entity_id, entity.entity_type,
            "existence", entity.status.value, EntityStatus.GONE.value, now,
            source, entity.confidence)
        self._append(change)
        return change

    def what_changed_since(self, since: str | None = None, *,
                           limit: int | None = None) -> list[StateChange]:
        """The raw diff. Requires no language model, by design."""
        cutoff = _parse_iso(since)
        out = [c for c in self._changes
               if cutoff is None or (_parse_iso(c.at) or _now()) >= cutoff]
        cap = limit or self.bounds.max_change_history
        return out[-cap:]

    def recently_changed(self, *, within_s: float = 3_600.0,
                         limit: int | None = None) -> list[StateChange]:
        cutoff = datetime.fromtimestamp(self._clock(), tz=timezone.utc) - \
            timedelta(seconds=within_s)
        return self.what_changed_since(_iso(cutoff), limit=limit)

    # ── §14 queries ──────────────────────────────────────────────────────────
    def all_entities(self) -> list[WorldEntity]:
        return [self._project(a) for a in self.graph.assets.values()]

    def entities_by_type(self, entity_type: "AssetType | str") -> list[WorldEntity]:
        etype = entity_type if isinstance(entity_type, AssetType) else AssetType(str(entity_type))
        return [self._project(a) for a in self.graph.by_type(etype)]

    def dependencies_of(self, entity_id: str, *, max_depth: int = 2) -> list[dict]:
        """What this entity needs. Outward along DEPENDS_ON / RUNS_ON / USES."""
        return self.graph.neighbors(
            entity_id,
            rel_types={RelationshipType.DEPENDS_ON, RelationshipType.RUNS_ON,
                       RelationshipType.USES, RelationshipType.BACKED_BY},
            max_depth=min(max_depth, self.bounds.max_traversal_depth),
            limit=self.bounds.max_traversal_nodes)

    def dependents_of(self, entity_id: str, *, max_depth: int = 2) -> list[dict]:
        """What needs this entity. Inbound edges, which the graph's outward
        traversal cannot answer, so it is a bounded reverse walk here.

        Cycle-safe: a node is expanded once. Depth and node count are both
        capped, so a pathological graph cannot make this run long.
        """
        depth_cap = min(max_depth, self.bounds.max_traversal_depth)
        node_cap = self.bounds.max_traversal_nodes
        inbound: dict[str, list[tuple[str, str]]] = {}
        for rel in self.graph.relationships.values():
            if rel.rel_type in (RelationshipType.DEPENDS_ON, RelationshipType.RUNS_ON,
                                RelationshipType.USES, RelationshipType.BACKED_BY,
                                RelationshipType.HOSTS):
                inbound.setdefault(rel.dst_id, []).append((rel.src_id, rel.rel_type.value))

        out: list[dict] = []
        seen = {entity_id}
        frontier = [(entity_id, 0)]
        while frontier and len(out) < node_cap:
            node, depth = frontier.pop(0)
            if depth >= depth_cap:
                continue
            for src, rel_name in sorted(inbound.get(node, ())):
                if src in seen:
                    continue
                seen.add(src)
                out.append({"depth": depth + 1, "rel": rel_name, "neighbor_id": src})
                frontier.append((src, depth + 1))
                if len(out) >= node_cap:
                    break
        return out

    def related_entities(self, entity_id: str, *, max_depth: int = 1) -> list[dict]:
        return self.graph.neighbors(
            entity_id, max_depth=min(max_depth, self.bounds.max_traversal_depth),
            limit=self.bounds.max_traversal_nodes)

    def unhealthy_entities(self) -> list[WorldEntity]:
        """Everything not currently healthy. Sorted worst-first, deterministically."""
        rank = {EntityHealth.CRITICAL: 0, EntityHealth.WARNING: 1,
                EntityHealth.UNKNOWN: 2, EntityHealth.HEALTHY: 3}
        bad = [e for e in self.all_entities()
               if e.health in (EntityHealth.CRITICAL, EntityHealth.WARNING)
               or e.status in (EntityStatus.OFFLINE, EntityStatus.DEGRADED,
                               EntityStatus.STALE, EntityStatus.GONE)]
        return sorted(bad, key=lambda e: (rank[e.health], e.entity_id))

    def impact_of(self, entity_id: str, *, max_depth: int = 3) -> dict:
        """"What breaks if X disappears?" — deterministic, from graph state alone.

        Returns the dependent set, split by whether each dependent is currently
        healthy, plus the blast-radius count. No inference, no model.
        """
        entity = self.get_entity(entity_id)
        dependents = self.dependents_of(entity_id, max_depth=max_depth)
        affected: list[dict] = []
        for hop in dependents:
            dep = self.get_entity(hop["neighbor_id"])
            if dep is None:
                continue
            affected.append({
                "entity_id": dep.entity_id, "entity_type": dep.entity_type.value,
                "depth": hop["depth"], "via": hop["rel"],
                "status": dep.status.value, "health": dep.health.value,
            })
        return {
            "entity_id": entity_id,
            "exists": entity is not None,
            "entity_type": entity.entity_type.value if entity else None,
            "status": entity.status.value if entity else None,
            "blast_radius": len(affected),
            "affected": affected[: self.bounds.max_traversal_nodes],
            "truncated": len(dependents) >= self.bounds.max_traversal_nodes,
            "deterministic": True,
        }

    def environment_summary(self) -> dict:
        """The one bounded structured answer to "how is the lab?"."""
        entities = self.all_entities()
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_health: dict[str, int] = {}
        for e in entities:
            by_type[e.entity_type.value] = by_type.get(e.entity_type.value, 0) + 1
            by_status[e.status.value] = by_status.get(e.status.value, 0) + 1
            by_health[e.health.value] = by_health.get(e.health.value, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(datetime.fromtimestamp(self._clock(), tz=timezone.utc)),
            "entities": len(entities),
            "relationships": len(self.graph.relationships),
            "by_type": dict(sorted(by_type.items())),
            "by_status": dict(sorted(by_status.items())),
            "by_health": dict(sorted(by_health.items())),
            "online": by_status.get(EntityStatus.ONLINE.value, 0),
            "offline": by_status.get(EntityStatus.OFFLINE.value, 0),
            "degraded": by_status.get(EntityStatus.DEGRADED.value, 0),
            "stale": by_status.get(EntityStatus.STALE.value, 0),
            "unhealthy": len(self.unhealthy_entities()),
            "conflicts": len(self.graph.get_conflicts()),
            "changes_tracked": len(self._changes),
            "requires_llm": False,
        }


def _source_for(obs: WorldObservation) -> ObservationSource:
    """Map an observation's trust onto the graph's provenance vocabulary."""
    if obs.trust is ObservationTrust.OPERATOR:
        return ObservationSource.OPERATOR_DECLARATION
    if obs.trust is ObservationTrust.INSTRUMENTED:
        return ObservationSource.SERVICE_OBSERVATION
    return ObservationSource.CANONICAL_EVENT


def _health_from(declared: str | None, status: EntityStatus) -> EntityHealth:
    """Health is read when declared, and otherwise DERIVED from status only."""
    value = (declared or "").strip().lower()
    if value in ("healthy", "ok", "pass", "green", "nominal"):
        return EntityHealth.HEALTHY
    if value in ("warning", "warn", "degraded", "yellow"):
        return EntityHealth.WARNING
    if value in ("critical", "crit", "fail", "failed", "red"):
        return EntityHealth.CRITICAL
    if status is EntityStatus.ONLINE:
        return EntityHealth.HEALTHY
    if status in (EntityStatus.OFFLINE, EntityStatus.GONE):
        return EntityHealth.CRITICAL
    if status in (EntityStatus.DEGRADED, EntityStatus.STALE):
        return EntityHealth.WARNING
    return EntityHealth.UNKNOWN


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


#: Module singleton, following the repository's attach-at-boot convention. It is
#: bound to the SAME AssetGraph singleton the correlator already populates, so
#: the situational view and the correlation view can never disagree.
def _default_state() -> WorldState:
    try:
        from core.asset_graph import graph as _graph
        return WorldState(_graph)
    except Exception:  # noqa: BLE001 — a bare graph is still a valid world
        return WorldState()


world = _default_state()

__all__ = [
    "OBSERVATION_SCHEMA_VERSION", "SCHEMA_VERSION", "ChangeKind", "EntityHealth",
    "EntityStatus", "ObservationTrust", "StateChange", "WorldEntity",
    "WorldObservation", "WorldState", "WorldStateError", "new_correlation_id",
    "world",
]
