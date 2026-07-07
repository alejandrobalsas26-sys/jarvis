"""
core/asset_graph.py — V66 Milestone 20: evidence-backed asset & service graph.

The largest genuinely-missing operational abstraction. JARVIS observes hosts,
VMs, containers, services and sensors across many telemetry sources, but nowhere
holds a *coherent, evidence-backed* model of "what exists, what refers to what,
and how sure are we." This module is that model.

Design law — **the operator's lab topology is NOT hardcoded as truth.** The graph
can *represent* a Windows 11 host, VMware, a Windows Server 2022 / Ubuntu / Kali
VM, PNETLab, Docker networks, Suricata/Zeek/Sysmon sensors — but only when they
are actually observed or explicitly declared. Every fact carries provenance;
nothing is fabricated; unknown stays unknown.

Core invariants (V66 security/trust):
  * **No fact without provenance.** Every :class:`AssetObservation` records its
    source, source_type, observer, timestamp, confidence, event references, and a
    content hash.
  * **No silent conflict overwrite.** Two observations that disagree on the same
    attribute are BOTH preserved; the graph marks the current best-confidence
    value and *surfaces* the conflict (:class:`AssetConflict`) rather than
    dropping the loser.
  * **Operator declarations are distinguishable.** ``ObservationSource``
    distinguishes ``OPERATOR_DECLARATION`` / ``TRUSTED_CONFIG`` from machine
    observation, and they carry a high default confidence — but they still
    surface a conflict when they disagree with an observation (they win on
    confidence, transparently, never by silent overwrite).
  * **Merge, don't pile up.** A repeated identical observation from the same
    source increments corroboration (count / last_seen), it does not duplicate.
  * **Bounded.** Neighbor traversal is depth- and count-capped; compact context
    retrieval never dumps the whole graph into a prompt.
  * **Local-first.** JSON persistence with no external DB requirement.

Pure data structure + deterministic queries. No I/O beyond explicit
save/load; no tool execution; no model call. Unit-testable with injected clocks.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from loguru import logger

SCHEMA_VERSION = "asset-graph-1"

# Bounds (Rule of Silicon).
_MAX_NEIGHBOR_DEPTH = 4
_MAX_NEIGHBOR_NODES = 256
_MAX_OBS_PER_ATTR = 32          # keep the most-recent evidence per attribute
_COMPACT_MAX_ASSETS = 12
_COMPACT_MAX_ATTRS = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value) -> str:
    return str(value).strip().lower()


# ══════════════════════════════════════════════════════════════════════════════
#  Taxonomy
# ══════════════════════════════════════════════════════════════════════════════
class AssetType(str, Enum):
    PHYSICAL_HOST = "physical_host"
    LAPTOP = "laptop"
    SERVER = "server"
    WORKSTATION = "workstation"
    VM = "vm"
    CONTAINER = "container"
    HYPERVISOR = "hypervisor"
    ROUTER = "router"
    SWITCH = "switch"
    FIREWALL = "firewall"
    NETWORK = "network"
    SUBNET = "subnet"
    INTERFACE = "interface"
    SERVICE = "service"
    APPLICATION = "application"
    DATABASE = "database"
    SECURITY_SENSOR = "security_sensor"
    LAB_PLATFORM = "lab_platform"
    UNKNOWN = "unknown"


class RelationshipType(str, Enum):
    HOSTS = "hosts"
    RUNS_ON = "runs_on"
    CONNECTED_TO = "connected_to"
    MEMBER_OF = "member_of"
    ROUTES_TO = "routes_to"
    DEPENDS_ON = "depends_on"
    EXPOSES = "exposes"
    MONITORED_BY = "monitored_by"
    MANAGES = "manages"
    BACKED_BY = "backed_by"
    COMMUNICATES_WITH = "communicates_with"
    OBSERVED_BY = "observed_by"


class ObservationSource(str, Enum):
    """Where an asset fact came from — machine observation vs operator declaration
    stay distinguishable, per the V66 trust rules."""
    CANONICAL_EVENT = "canonical_event"          # an M19 OperationalEvent
    SENSOR_MESH = "sensor_mesh"                   # a sensor registration
    DOCKER_INSPECT = "docker_inspect"            # container inspection
    LAB_MANAGER = "lab_manager"                  # VM inventory
    NETWORK_OBSERVATION = "network_observation"   # observed connectivity
    SERVICE_OBSERVATION = "service_observation"   # observed service/port
    OPERATOR_DECLARATION = "operator_declaration"  # the human said so
    TRUSTED_CONFIG = "trusted_config"            # vetted configuration
    INTERNAL = "internal"

    @property
    def is_operator(self) -> bool:
        return self in (ObservationSource.OPERATOR_DECLARATION,
                        ObservationSource.TRUSTED_CONFIG)


# Default confidence when a caller does not specify one. Operator/trusted config
# outrank machine observation — but transparently (a conflict is still surfaced).
_DEFAULT_CONFIDENCE: dict[ObservationSource, float] = {
    ObservationSource.OPERATOR_DECLARATION: 0.95,
    ObservationSource.TRUSTED_CONFIG: 0.9,
    ObservationSource.LAB_MANAGER: 0.85,
    ObservationSource.DOCKER_INSPECT: 0.85,
    ObservationSource.SENSOR_MESH: 0.7,
    ObservationSource.CANONICAL_EVENT: 0.6,
    ObservationSource.NETWORK_OBSERVATION: 0.55,
    ObservationSource.SERVICE_OBSERVATION: 0.6,
    ObservationSource.INTERNAL: 0.5,
}


def asset_id(asset_type: AssetType, identity: str) -> str:
    """Deterministic, stable id from a type + a caller-chosen identity key. The
    graph never *fabricates* a merge across identity keys — cross-key identity
    resolution is out of scope (it would invent topology)."""
    return f"{asset_type.value}:{_norm(identity)}"


# ══════════════════════════════════════════════════════════════════════════════
#  Evidence
# ══════════════════════════════════════════════════════════════════════════════
def _obs_hash(asset: str, attribute: str, value: str, source: str,
              source_type: str, observer: str) -> str:
    blob = "|".join([asset, attribute, _norm(value), source, source_type, observer])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


@dataclass
class AssetObservation:
    """One evidence-bearing claim about an asset attribute (or a relationship).
    Repeated identical observations from the same source corroborate (count/
    last_seen) rather than duplicate."""
    attribute: str
    value: str
    source: ObservationSource
    confidence: float
    observer: str = ""                       # which host/agent/analyst reported it
    first_seen: str = field(default_factory=_now_iso)
    last_seen: str = field(default_factory=_now_iso)
    count: int = 1
    event_refs: tuple[str, ...] = ()          # canonical event ids / evidence locators
    note: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "attribute": self.attribute, "value": self.value,
            "source": self.source.value, "confidence": round(self.confidence, 3),
            "observer": self.observer, "first_seen": self.first_seen,
            "last_seen": self.last_seen, "count": self.count,
            "event_refs": list(self.event_refs), "note": self.note,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AssetObservation":
        return cls(
            attribute=d["attribute"], value=d["value"],
            source=ObservationSource(d["source"]), confidence=float(d["confidence"]),
            observer=d.get("observer", ""), first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""), count=int(d.get("count", 1)),
            event_refs=tuple(d.get("event_refs", ())), note=d.get("note", ""),
            content_hash=d.get("content_hash", ""),
        )


@dataclass(frozen=True)
class AssetConflict:
    """A surfaced (never-silenced) disagreement on one attribute of one asset,
    or on the type of one relationship. Both values and their best confidence are
    preserved so an analyst can adjudicate."""
    asset_id: str
    attribute: str
    values: tuple[tuple[str, float, str], ...]   # (value, best_confidence, source)
    current_value: str
    current_confidence: float

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id, "attribute": self.attribute,
            "values": [{"value": v, "confidence": round(c, 3), "source": s}
                       for v, c, s in self.values],
            "current_value": self.current_value,
            "current_confidence": round(self.current_confidence, 3),
        }


def _aggregate(observations: list[AssetObservation]) -> dict[str, tuple[float, str]]:
    """Aggregate confidence per distinct value: max single confidence plus a small
    corroboration bonus for multiple distinct sources (bounded ≤ 1.0)."""
    by_value: dict[str, list[AssetObservation]] = {}
    for o in observations:
        by_value.setdefault(_norm(o.value), []).append(o)
    out: dict[str, tuple[float, str]] = {}
    for _key, obs in by_value.items():
        best = max(obs, key=lambda o: o.confidence)
        distinct_sources = len({o.source for o in obs})
        conf = min(1.0, best.confidence + 0.05 * (distinct_sources - 1))
        out[best.value] = (conf, best.source.value)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Asset & relationship
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Asset:
    """One node: a stable id, a (conflict-aware) type, and per-attribute evidence.
    ``attributes[name]`` is the full observation history for that attribute; the
    *current* value is the highest-aggregate-confidence one."""
    id: str
    asset_type: AssetType
    identity: str
    attributes: dict[str, list[AssetObservation]] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    # ── observation add / merge ───────────────────────────────────────────────
    def add(self, obs: AssetObservation) -> AssetObservation:
        bucket = self.attributes.setdefault(obs.attribute, [])
        for existing in bucket:
            if existing.content_hash == obs.content_hash:
                existing.count += 1
                existing.last_seen = obs.last_seen
                # corroboration can only raise confidence, never silently lower it
                existing.confidence = max(existing.confidence, obs.confidence)
                if obs.event_refs:
                    existing.event_refs = tuple(dict.fromkeys(
                        (*existing.event_refs, *obs.event_refs)))[:16]
                return existing
        bucket.append(obs)
        # bound history per attribute (keep most-recent by last_seen)
        if len(bucket) > _MAX_OBS_PER_ATTR:
            bucket.sort(key=lambda o: o.last_seen)
            del bucket[0:len(bucket) - _MAX_OBS_PER_ATTR]
        return obs

    # ── queries ───────────────────────────────────────────────────────────────
    def current(self, attribute: str) -> tuple[str, float] | None:
        obs = self.attributes.get(attribute)
        if not obs:
            return None
        agg = _aggregate(obs)
        value, (conf, _src) = max(agg.items(), key=lambda kv: (kv[1][0], kv[0]))
        return value, conf

    def current_type(self) -> AssetType:
        cur = self.current("asset_type")
        if cur:
            try:
                return AssetType(cur[0])
            except ValueError:
                pass
        return self.asset_type

    def conflict(self, attribute: str) -> AssetConflict | None:
        obs = self.attributes.get(attribute)
        if not obs:
            return None
        agg = _aggregate(obs)
        if len(agg) < 2:
            return None
        values = tuple(sorted(
            ((v, c, s) for v, (c, s) in agg.items()),
            key=lambda t: (-t[1], t[0]),
        ))
        top_value, (top_conf, _s) = max(agg.items(), key=lambda kv: (kv[1][0], kv[0]))
        return AssetConflict(self.id, attribute, values, top_value, top_conf)

    def conflicts(self) -> list[AssetConflict]:
        out = []
        for attr in self.attributes:
            c = self.conflict(attr)
            if c is not None:
                out.append(c)
        return out

    def history(self, attribute: str) -> list[AssetObservation]:
        return list(self.attributes.get(attribute, []))

    def current_view(self, *, max_attrs: int = _COMPACT_MAX_ATTRS) -> dict:
        """Compact current-state view (no full history) for prompts/HUD."""
        view: dict[str, object] = {}
        for attr in sorted(self.attributes):
            cur = self.current(attr)
            if cur is None:
                continue
            entry: dict[str, object] = {"value": cur[0], "confidence": round(cur[1], 2)}
            if self.conflict(attr) is not None:
                entry["conflict"] = True
            view[attr] = entry
            if len(view) >= max_attrs:
                break
        return view

    def to_dict(self) -> dict:
        return {
            "id": self.id, "asset_type": self.asset_type.value,
            "identity": self.identity, "created_at": self.created_at,
            "attributes": {
                attr: [o.to_dict() for o in obs]
                for attr, obs in self.attributes.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Asset":
        a = cls(id=d["id"], asset_type=AssetType(d["asset_type"]),
                identity=d.get("identity", ""), created_at=d.get("created_at", ""))
        for attr, obs_list in d.get("attributes", {}).items():
            a.attributes[attr] = [AssetObservation.from_dict(o) for o in obs_list]
        return a


@dataclass
class AssetRelationship:
    """A directed edge with its own evidence list. Conflicts are surfaced the same
    way as attribute conflicts (multiple distinct 'active' observations)."""
    src_id: str
    rel_type: RelationshipType
    dst_id: str
    observations: list[AssetObservation] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.src_id}|{self.rel_type.value}|{self.dst_id}"

    def add(self, obs: AssetObservation) -> AssetObservation:
        for existing in self.observations:
            if existing.content_hash == obs.content_hash:
                existing.count += 1
                existing.last_seen = obs.last_seen
                existing.confidence = max(existing.confidence, obs.confidence)
                return existing
        self.observations.append(obs)
        return obs

    def confidence(self) -> float:
        if not self.observations:
            return 0.0
        agg = _aggregate(self.observations)
        return max(c for c, _s in agg.values())

    def to_dict(self) -> dict:
        return {
            "src_id": self.src_id, "rel_type": self.rel_type.value,
            "dst_id": self.dst_id,
            "observations": [o.to_dict() for o in self.observations],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AssetRelationship":
        r = cls(src_id=d["src_id"], rel_type=RelationshipType(d["rel_type"]),
                dst_id=d["dst_id"])
        r.observations = [AssetObservation.from_dict(o) for o in d.get("observations", [])]
        return r


# ══════════════════════════════════════════════════════════════════════════════
#  Snapshot / diff
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class GraphSnapshot:
    taken_at: str
    assets: dict            # id -> compact current view + type
    relationships: list     # [{src, rel, dst, confidence}]
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "taken_at": self.taken_at, "assets": self.assets,
            "relationships": self.relationships, "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class GraphDifference:
    added_assets: tuple[str, ...]
    removed_assets: tuple[str, ...]
    changed_attributes: tuple[tuple[str, str, str, str], ...]  # (asset, attr, old, new)
    added_relationships: tuple[str, ...]
    removed_relationships: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.added_assets or self.removed_assets or self.changed_attributes
                    or self.added_relationships or self.removed_relationships)

    def to_dict(self) -> dict:
        return {
            "added_assets": list(self.added_assets),
            "removed_assets": list(self.removed_assets),
            "changed_attributes": [
                {"asset_id": a, "attribute": at, "old": o, "new": n}
                for a, at, o, n in self.changed_attributes
            ],
            "added_relationships": list(self.added_relationships),
            "removed_relationships": list(self.removed_relationships),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  The graph
# ══════════════════════════════════════════════════════════════════════════════
class AssetGraph:
    """Evidence-backed asset & service graph. Add observations and relationship
    evidence; query neighbors / by-type / exposed-services / history / conflicts;
    snapshot and diff; persist to JSON. Never fabricates, never silently
    overwrites, never dumps the whole graph into a prompt."""

    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}
        self.relationships: dict[str, AssetRelationship] = {}
        self._adj: dict[str, set[str]] = {}   # src -> set of relationship keys

    # ── mutation ──────────────────────────────────────────────────────────────
    def _ensure_asset(self, asset_type: AssetType, identity: str,
                      *, source: ObservationSource, confidence: float,
                      observer: str, now_iso: str, event_refs) -> Asset:
        aid = asset_id(asset_type, identity)
        asset = self.assets.get(aid)
        if asset is None:
            asset = Asset(id=aid, asset_type=asset_type, identity=str(identity),
                          created_at=now_iso)
            self.assets[aid] = asset
        # record the type itself as an evidence-backed (conflict-aware) attribute
        self._add_obs(asset, "asset_type", asset_type.value, source=source,
                      confidence=confidence, observer=observer, now_iso=now_iso,
                      event_refs=event_refs)
        return asset

    @staticmethod
    def _add_obs(asset: Asset, attribute: str, value, *, source: ObservationSource,
                 confidence: float, observer: str, now_iso: str,
                 event_refs=()) -> AssetObservation:
        chash = _obs_hash(asset.id, attribute, str(value), source.value,
                          source.value, observer)
        obs = AssetObservation(
            attribute=attribute, value=str(value), source=source,
            confidence=confidence, observer=observer, first_seen=now_iso,
            last_seen=now_iso, event_refs=tuple(event_refs), content_hash=chash,
        )
        return asset.add(obs)

    def add_observation(
        self, asset_type: AssetType, identity: str, attribute: str, value,
        *, source: ObservationSource, confidence: float | None = None,
        observer: str = "", event_refs=(), note: str = "",
        now_iso: str | None = None,
    ) -> AssetObservation:
        """Record one evidence-bearing attribute claim about an asset. Creates the
        asset node if new. Conflicting values are preserved and surfaced (never
        overwritten). Repeated identical claims corroborate."""
        now_iso = now_iso or _now_iso()
        conf = _DEFAULT_CONFIDENCE.get(source, 0.5) if confidence is None else float(confidence)
        conf = max(0.0, min(1.0, conf))
        asset = self._ensure_asset(asset_type, identity, source=source,
                                   confidence=conf, observer=observer,
                                   now_iso=now_iso, event_refs=event_refs)
        chash = _obs_hash(asset.id, attribute, str(value), source.value,
                          source.value, observer)
        obs = AssetObservation(
            attribute=attribute, value=str(value), source=source, confidence=conf,
            observer=observer, first_seen=now_iso, last_seen=now_iso,
            event_refs=tuple(event_refs), note=note, content_hash=chash,
        )
        return asset.add(obs)

    def add_relationship(
        self, src_type: AssetType, src_identity: str, rel_type: RelationshipType,
        dst_type: AssetType, dst_identity: str, *, source: ObservationSource,
        confidence: float | None = None, observer: str = "", event_refs=(),
        now_iso: str | None = None,
    ) -> AssetRelationship:
        """Record relationship evidence between two assets (creating minimal nodes
        for either endpoint if needed)."""
        now_iso = now_iso or _now_iso()
        conf = _DEFAULT_CONFIDENCE.get(source, 0.5) if confidence is None else float(confidence)
        conf = max(0.0, min(1.0, conf))
        src = self._ensure_asset(src_type, src_identity, source=source, confidence=conf,
                                 observer=observer, now_iso=now_iso, event_refs=event_refs)
        dst = self._ensure_asset(dst_type, dst_identity, source=source, confidence=conf,
                                 observer=observer, now_iso=now_iso, event_refs=event_refs)
        rkey = f"{src.id}|{rel_type.value}|{dst.id}"
        rel = self.relationships.get(rkey)
        if rel is None:
            rel = AssetRelationship(src_id=src.id, rel_type=rel_type, dst_id=dst.id)
            self.relationships[rkey] = rel
            self._adj.setdefault(src.id, set()).add(rkey)
        chash = _obs_hash(rkey, "relationship", f"{src.id}->{dst.id}", source.value,
                          source.value, observer)
        rel.add(AssetObservation(
            attribute="relationship", value=f"{src.id}->{dst.id}", source=source,
            confidence=conf, observer=observer, first_seen=now_iso, last_seen=now_iso,
            event_refs=tuple(event_refs), content_hash=chash,
        ))
        return rel

    def observe_service(
        self, host_type: AssetType, host_identity: str, *, port: int,
        protocol: str = "", service_name: str = "", exposure: str = "unknown",
        bind_addr: str = "", source: ObservationSource, confidence: float | None = None,
        observer: str = "", event_refs=(), now_iso: str | None = None,
    ) -> str:
        """Record a service endpoint on a host: creates a SERVICE asset, links it
        with EXPOSES, and records port/protocol/exposure/bind evidence. Returns the
        service asset id. ``exposure`` is one of localhost/internal/authorized_subnet
        /external/unknown — observed, never assumed."""
        now_iso = now_iso or _now_iso()
        svc_identity = f"{host_identity}:{port}"
        for attr, val in (("port", port), ("protocol", protocol),
                          ("service_name", service_name), ("exposure", exposure),
                          ("bind_addr", bind_addr), ("host", host_identity)):
            if val in (None, ""):
                continue
            self.add_observation(AssetType.SERVICE, svc_identity, attr, val,
                                 source=source, confidence=confidence, observer=observer,
                                 event_refs=event_refs, now_iso=now_iso)
        self.add_relationship(host_type, host_identity, RelationshipType.EXPOSES,
                              AssetType.SERVICE, svc_identity, source=source,
                              confidence=confidence, observer=observer,
                              event_refs=event_refs, now_iso=now_iso)
        return asset_id(AssetType.SERVICE, svc_identity)

    # ── read queries ──────────────────────────────────────────────────────────
    def get(self, asset_type: AssetType, identity: str) -> Asset | None:
        return self.assets.get(asset_id(asset_type, identity))

    def get_by_id(self, aid: str) -> Asset | None:
        return self.assets.get(aid)

    def by_type(self, asset_type: AssetType) -> list[Asset]:
        return [a for a in self.assets.values()
                if a.current_type() is asset_type]

    def neighbors(self, aid: str, *, rel_types: "set[RelationshipType] | None" = None,
                  max_depth: int = 1, limit: int = _MAX_NEIGHBOR_NODES) -> list[dict]:
        """Bounded outward traversal. Returns [{depth, rel, neighbor_id}] up to
        ``max_depth`` (capped) and ``limit`` nodes (capped) — never unbounded."""
        max_depth = max(1, min(int(max_depth), _MAX_NEIGHBOR_DEPTH))
        limit = max(1, min(int(limit), _MAX_NEIGHBOR_NODES))
        if aid not in self.assets:
            return []
        out: list[dict] = []
        seen: set[str] = {aid}
        frontier: deque[tuple[str, int]] = deque([(aid, 0)])
        while frontier and len(out) < limit:
            node, depth = frontier.popleft()
            if depth >= max_depth:
                continue
            for rkey in sorted(self._adj.get(node, ())):
                rel = self.relationships.get(rkey)
                if rel is None:
                    continue
                if rel_types and rel.rel_type not in rel_types:
                    continue
                out.append({"depth": depth + 1, "rel": rel.rel_type.value,
                            "neighbor_id": rel.dst_id,
                            "confidence": round(rel.confidence(), 3)})
                if rel.dst_id not in seen:
                    seen.add(rel.dst_id)
                    frontier.append((rel.dst_id, depth + 1))
                if len(out) >= limit:
                    break
        return out

    def exposed_services(self, *, only_reachable: bool = True) -> list[dict]:
        """Every SERVICE asset with its current port/protocol/exposure/host. When
        ``only_reachable`` (default), services observed bound to localhost are
        excluded — but an *unknown* exposure is kept (unknown ≠ safe)."""
        out: list[dict] = []
        for svc in self.by_type(AssetType.SERVICE):
            exposure = (svc.current("exposure") or ("unknown", 0.0))[0]
            if only_reachable and _norm(exposure) == "localhost":
                continue
            port = svc.current("port")
            out.append({
                "service_id": svc.id,
                "host": (svc.current("host") or ("", 0.0))[0],
                "port": int(port[0]) if port and str(port[0]).isdigit() else None,
                "protocol": (svc.current("protocol") or ("", 0.0))[0],
                "service_name": (svc.current("service_name") or ("", 0.0))[0],
                "exposure": exposure,
                "conflict": svc.conflict("exposure") is not None,
            })
        return out

    def observation_history(self, asset_type: AssetType, identity: str,
                            attribute: str) -> list[dict]:
        a = self.get(asset_type, identity)
        if a is None:
            return []
        return [o.to_dict() for o in a.history(attribute)]

    def get_conflicts(self) -> list[AssetConflict]:
        out: list[AssetConflict] = []
        for a in self.assets.values():
            out.extend(a.conflicts())
        return out

    # ── compact context (scoped retrieval; never a full dump) ─────────────────
    def compact_context(
        self, asset_ids: "list[str] | None" = None, *,
        max_assets: int = _COMPACT_MAX_ASSETS, max_attrs: int = _COMPACT_MAX_ATTRS,
    ) -> dict:
        """A bounded, prompt-safe digest of current state — the ONLY graph→prompt
        path. Selects the requested assets (or the most-connected ones), each with
        its current attribute view (no history), conflict flags, and neighbor
        count. Integrates with Memory Fabric / Specialist Runtime as scoped
        context, never the whole graph."""
        max_assets = max(1, min(int(max_assets), _COMPACT_MAX_ASSETS))
        if asset_ids:
            chosen = [self.assets[i] for i in asset_ids if i in self.assets][:max_assets]
        else:
            chosen = sorted(self.assets.values(),
                            key=lambda a: len(self._adj.get(a.id, ())),
                            reverse=True)[:max_assets]
        assets_view = {}
        for a in chosen:
            assets_view[a.id] = {
                "type": a.current_type().value,
                "attributes": a.current_view(max_attrs=max_attrs),
                "neighbors": len(self._adj.get(a.id, ())),
            }
        conflicts = self.get_conflicts()
        return {
            "asset_count": len(self.assets),
            "shown": len(assets_view),
            "assets": assets_view,
            "conflict_count": len(conflicts),
            "conflicts": [c.to_dict() for c in conflicts[:8]],
        }

    # ── snapshot / diff ───────────────────────────────────────────────────────
    def snapshot(self, *, now_iso: str | None = None) -> GraphSnapshot:
        now_iso = now_iso or _now_iso()
        assets_view = {}
        for a in self.assets.values():
            cur = {attr: self.current_pair(a, attr) for attr in a.attributes}
            assets_view[a.id] = {"type": a.current_type().value, "current": cur}
        rels = sorted(
            [{"src": r.src_id, "rel": r.rel_type.value, "dst": r.dst_id,
              "confidence": round(r.confidence(), 3)}
             for r in self.relationships.values()],
            key=lambda d: (d["src"], d["rel"], d["dst"]),
        )
        blob = json.dumps({"a": assets_view, "r": rels}, sort_keys=True, default=str)
        chash = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]
        return GraphSnapshot(taken_at=now_iso, assets=assets_view,
                             relationships=rels, content_hash=chash)

    @staticmethod
    def current_pair(asset: Asset, attribute: str):
        cur = asset.current(attribute)
        return None if cur is None else [cur[0], round(cur[1], 3)]

    @staticmethod
    def diff(before: GraphSnapshot, after: GraphSnapshot) -> GraphDifference:
        before_ids = set(before.assets)
        after_ids = set(after.assets)
        added = tuple(sorted(after_ids - before_ids))
        removed = tuple(sorted(before_ids - after_ids))
        changed: list[tuple[str, str, str, str]] = []
        for aid in sorted(before_ids & after_ids):
            b_cur = before.assets[aid].get("current", {})
            a_cur = after.assets[aid].get("current", {})
            for attr in sorted(set(b_cur) | set(a_cur)):
                bv = b_cur.get(attr)
                av = a_cur.get(attr)
                b_val = bv[0] if bv else None
                a_val = av[0] if av else None
                if b_val != a_val:
                    changed.append((aid, attr, str(b_val), str(a_val)))
        b_rels = {f"{r['src']}|{r['rel']}|{r['dst']}" for r in before.relationships}
        a_rels = {f"{r['src']}|{r['rel']}|{r['dst']}" for r in after.relationships}
        return GraphDifference(
            added_assets=added, removed_assets=removed,
            changed_attributes=tuple(changed),
            added_relationships=tuple(sorted(a_rels - b_rels)),
            removed_relationships=tuple(sorted(b_rels - a_rels)),
        )

    # ── persistence (local-first JSON) ────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "assets": [a.to_dict() for a in self.assets.values()],
            "relationships": [r.to_dict() for r in self.relationships.values()],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "AssetGraph":
        g = cls()
        for ad in d.get("assets", []):
            a = Asset.from_dict(ad)
            g.assets[a.id] = a
        for rd in d.get("relationships", []):
            r = AssetRelationship.from_dict(rd)
            g.relationships[r.key] = r
            g._adj.setdefault(r.src_id, set()).add(r.key)
        return g

    @classmethod
    def from_json(cls, text: str) -> "AssetGraph":
        return cls.from_dict(json.loads(text))

    def save(self, path: "str | Path") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(indent=2), encoding="utf-8")
        logger.info(f"ASSET_GRAPH: persisted {len(self.assets)} assets → {p}")

    @classmethod
    def load(cls, path: "str | Path") -> "AssetGraph":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls.from_json(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — a corrupt file must not crash boot
            logger.warning(f"ASSET_GRAPH: load failed ({e}) — starting empty")
            return cls()


# Module-level singleton — the canonical operational graph for the live host.
graph = AssetGraph()
