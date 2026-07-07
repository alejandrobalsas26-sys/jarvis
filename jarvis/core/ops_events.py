"""
core/ops_events.py — V66 Milestone 19: canonical operational/security event model.

JARVIS already has a broadcast pipeline (``core.events.make_event`` →
``aura.server.broadcast`` → ``correlator.ingest`` + WebSocket fan-out) whose
producers each emit their OWN heterogeneous dict shape: ``sysmon_event`` (Sysmon
bridge), ``dpi_alert`` (Zeek DNS/HTTP), ``network_anomaly`` (network baseline),
``sensor_connected`` (sensor mesh), ``compound_incident`` (correlator), etc.

This module is the **canonical, typed, provenance-preserving envelope** those
dicts normalize *into* — WITHOUT replacing the event bus. It does not add a
second broadcaster; it adds a normalization layer that the correlation/asset/
situation subsystems (M20–M25) consume, while the legacy dict pipeline keeps
flowing unchanged. Migration is incremental: an adapter exists for each real
producer, and call paths adopt :class:`OperationalEvent` where they benefit.

Design invariants (V66 security/trust):
  * **No fact without provenance.** Every event carries :class:`EventProvenance`
    (source, source_instance, adapter, whether the telemetry was signed).
  * **Unknown stays unknown.** Absent fields are ``None`` / empty — never guessed.
  * **Deterministic identity.** ``content_hash`` is a stable SHA-256 over the
    identifying fields (excludes ingestion time), so the SAME observation
    normalizes to the SAME id and duplicates are detectable.
  * **External text is untrusted data.** Free-text telemetry fields
    (command_line, DNS query, user-agent, signatures, raw detail) are labeled
    ``untrusted_text`` and screened through the M12 prompt-injection firewall so
    instruction-like content is flagged before it can reach LLM context or
    trusted memory. The raw value is preserved for forensics; :meth:`redacted_dict`
    is the model/memory-safe projection that defangs it.
  * **No raw-payload duplication.** Events hold compact :class:`EvidenceReference`
    pointers (``raw_reference``), not copies of PCAPs / full logs (Rule of Silicon).
  * **Bounded.** The dedup index and per-event field sizes are capped.

Pure and dependency-light: normalization is a pure function of (payload, clock);
no I/O, no tool execution, no model call. The firewall import is the only cross-
module dependency and it is itself pure.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from loguru import logger

SCHEMA_VERSION = "ops-1"

# Bound on any single free-text/untrusted field we retain (Rule of Silicon).
_MAX_TEXT = 2000
# Bound on the dedup index — never grows unbounded across a long-running host.
_DEDUP_CAP = 8192


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value, limit: int = _MAX_TEXT) -> str | None:
    """Coerce to a bounded string; None/empty → None (unknown stays unknown)."""
    if value is None:
        return None
    s = str(value)
    if not s:
        return None
    return s[:limit]


def _to_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Taxonomy
# ══════════════════════════════════════════════════════════════════════════════
class EventSource(str, Enum):
    """The real producer an event originated from (one per integration anchor)."""
    SYSMON = "sysmon"
    ZEEK_CONN = "zeek_conn"
    ZEEK_DNS = "zeek_dns"
    ZEEK_HTTP = "zeek_http"
    SENSOR_MESH = "sensor_mesh"
    NETWORK_BASELINE = "network_baseline"
    CORRELATOR = "correlator"
    JARVIS_INTERNAL = "jarvis_internal"
    UNKNOWN = "unknown"


class EventCategory(str, Enum):
    """What kind of activity the event describes."""
    PROCESS = "process"
    NETWORK = "network"
    FILE = "file"
    AUTH = "auth"
    DNS = "dns"
    HTTP = "http"
    IDS_ALERT = "ids_alert"
    ANOMALY = "anomaly"
    SENSOR = "sensor"
    INCIDENT = "incident"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class EventSeverity(str, Enum):
    """Normalized severity band. UNKNOWN when the source gives no signal."""
    UNKNOWN = "unknown"
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[EventSeverity, int] = {
    EventSeverity.UNKNOWN: 0, EventSeverity.INFO: 1, EventSeverity.LOW: 2,
    EventSeverity.MEDIUM: 3, EventSeverity.HIGH: 4, EventSeverity.CRITICAL: 5,
}

_SEVERITY_WORDS: dict[str, EventSeverity] = {
    "INFO": EventSeverity.INFO, "INFORMATIONAL": EventSeverity.INFO,
    "DEBUG": EventSeverity.INFO, "LOW": EventSeverity.LOW,
    "MEDIUM": EventSeverity.MEDIUM, "NOTICE": EventSeverity.MEDIUM,
    "MODERATE": EventSeverity.MEDIUM, "WARNING": EventSeverity.HIGH,
    "WARN": EventSeverity.HIGH, "HIGH": EventSeverity.HIGH,
    "CRITICAL": EventSeverity.CRITICAL, "FATAL": EventSeverity.CRITICAL,
    "SEV1": EventSeverity.CRITICAL,
}


def normalize_severity(value) -> EventSeverity:
    """Map a numeric (0–10 correlator scale) or string severity to a band.
    Absent/unparseable → UNKNOWN (never a guessed default)."""
    if value is None or value == "":
        return EventSeverity.UNKNOWN
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return EventSeverity.INFO
        if v < 3.0:
            return EventSeverity.LOW
        if v < 5.0:
            return EventSeverity.MEDIUM
        if v < 8.0:
            return EventSeverity.HIGH
        return EventSeverity.CRITICAL
    word = str(value).strip().upper()
    if word in _SEVERITY_WORDS:
        return _SEVERITY_WORDS[word]
    num = _to_int(value)
    if num is not None:
        return normalize_severity(num)
    return EventSeverity.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
#  References & provenance
# ══════════════════════════════════════════════════════════════════════════════
class EntityType(str, Enum):
    HOST = "host"
    USER = "user"
    IP = "ip"
    PROCESS = "process"
    PID = "pid"
    DOMAIN = "domain"
    FILE = "file"
    SERVICE = "service"
    AGENT = "agent"
    HASH = "hash"


@dataclass(frozen=True)
class EntityReference:
    """A canonical pointer to an entity an event refers to. The (type, value)
    pair is what M20's Asset Graph keys observations on."""
    type: EntityType
    value: str

    def to_dict(self) -> dict:
        return {"type": self.type.value, "value": self.value}

    def key(self) -> str:
        return f"{self.type.value}:{self.value.strip().lower()}"


@dataclass(frozen=True)
class EvidenceReference:
    """A compact pointer to the underlying raw evidence — NOT a copy of it.
    Preserves where the payload lives (a log line, a PCAP, an upstream event id)
    without duplicating heavy data into every downstream structure."""
    kind: str                       # e.g. "telemetry", "log_line", "pcap", "event"
    locator: str                    # e.g. "sysmon:eid=1", upstream event id, file:line
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "locator": self.locator, "detail": self.detail}


@dataclass(frozen=True)
class EventProvenance:
    """Where an event came from and how it was produced. Never empty — an event
    without provenance is not admissible."""
    source: EventSource
    source_instance: str = ""       # which sensor/agent/host produced it
    adapter: str = ""               # which normalizer built the canonical event
    signed: bool = False            # was the telemetry HMAC-signed at the bus?
    ingested_at: str = field(default_factory=_now_iso)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "source": self.source.value, "source_instance": self.source_instance,
            "adapter": self.adapter, "signed": self.signed,
            "ingested_at": self.ingested_at, "schema_version": self.schema_version,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  The canonical event
# ══════════════════════════════════════════════════════════════════════════════
# Fields that participate in the deterministic content hash (identity). Ingestion
# time is deliberately excluded so a re-emission of the same observation collides.
_IDENTITY_FIELDS = (
    "source", "category", "observed_at", "host", "user", "src_ip", "src_port",
    "dst_ip", "dst_port", "process", "pid", "parent_process", "command_line",
    "file_path", "service", "protocol", "rule_id", "signature",
)


@dataclass(frozen=True)
class OperationalEvent:
    """A single, typed, provenance-preserving operational/security observation.

    Only the fields a source actually provides are populated; everything else is
    ``None`` / empty. Free-text fields flagged in ``untrusted_text`` are external
    data and are screened before entering LLM/memory contexts (see
    :meth:`redacted_dict`)."""
    event_id: str
    provenance: EventProvenance
    source: EventSource
    category: EventCategory
    severity: EventSeverity = EventSeverity.UNKNOWN
    timestamp: str = field(default_factory=_now_iso)   # when JARVIS emitted/ingested
    observed_at: str | None = None                     # when the source observed it
    confidence: float | None = None

    host: str | None = None
    user: str | None = None
    src_ip: str | None = None
    src_port: int | None = None
    dst_ip: str | None = None
    dst_port: int | None = None
    process: str | None = None
    pid: int | None = None
    parent_process: str | None = None
    command_line: str | None = None
    file_path: str | None = None
    hashes: dict = field(default_factory=dict)
    service: str | None = None
    protocol: str | None = None
    rule_id: str | None = None
    signature: str | None = None
    mitre_techniques: tuple[str, ...] = ()

    entities: tuple[EntityReference, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    raw_reference: str = ""
    content_hash: str = ""
    # field name -> raw untrusted text (preserved for forensics; screened on export)
    untrusted_text: dict = field(default_factory=dict)
    # firewall summary if any untrusted field is instruction-like (else None)
    injection: dict | None = None
    extra: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    # ── identity ──────────────────────────────────────────────────────────────
    def compute_content_hash(self) -> str:
        payload = {}
        for name in _IDENTITY_FIELDS:
            val = getattr(self, name, None)
            if isinstance(val, EventSource) or isinstance(val, EventCategory):
                val = val.value
            if val is not None and val != "":
                payload[name] = val
        payload["techniques"] = sorted(self.mitre_techniques)
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def is_untrusted(self) -> bool:
        return bool(self.untrusted_text)

    @property
    def injection_detected(self) -> bool:
        return bool(self.injection and self.injection.get("detected"))

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """Full structured record (raw untrusted text included — for persistence /
        forensics, NOT for prompting)."""
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "observed_at": self.observed_at,
            "source": self.source.value,
            "category": self.category.value,
            "severity": self.severity.value,
            "confidence": self.confidence,
            "host": self.host, "user": self.user,
            "src_ip": self.src_ip, "src_port": self.src_port,
            "dst_ip": self.dst_ip, "dst_port": self.dst_port,
            "process": self.process, "pid": self.pid,
            "parent_process": self.parent_process,
            "command_line": self.command_line, "file_path": self.file_path,
            "hashes": dict(self.hashes), "service": self.service,
            "protocol": self.protocol, "rule_id": self.rule_id,
            "signature": self.signature,
            "mitre_techniques": list(self.mitre_techniques),
            "entities": [e.to_dict() for e in self.entities],
            "evidence": [e.to_dict() for e in self.evidence],
            "raw_reference": self.raw_reference,
            "content_hash": self.content_hash,
            "untrusted_fields": sorted(self.untrusted_text.keys()),
            "injection": self.injection,
            "provenance": self.provenance.to_dict(),
            "extra": dict(self.extra),
        }

    def redacted_dict(self, *, max_chars: int = 400) -> dict:
        """Model/HUD/memory-safe projection. Untrusted free-text fields are passed
        through the M12 firewall (``apply_firewall``) so instruction-like content
        is defanged/quarantined before it can reach an LLM prompt or long-term
        memory. Structured fields are preserved."""
        d = self.to_dict()
        for name, raw in self.untrusted_text.items():
            d[name] = _screen_text(raw, max_chars=max_chars)
        # Drop the heavy raw untrusted map; keep only which fields were untrusted.
        return d

    def entity_keys(self) -> list[str]:
        return [e.key() for e in self.entities]


# ══════════════════════════════════════════════════════════════════════════════
#  Untrusted-text screening (M12 firewall bridge)
# ══════════════════════════════════════════════════════════════════════════════
def _screen_text(text: str, *, max_chars: int = 400) -> str:
    """Defang one untrusted free-text field through the injection firewall
    (fail-open to a truncated copy if the firewall is unavailable)."""
    try:
        from core.injection_firewall import TrustOrigin, apply_firewall
        return apply_firewall(text or "", TrustOrigin.FILE_UNTRUSTED,
                              max_chars=max_chars).safe_content
    except Exception as e:  # noqa: BLE001 — screening must never crash normalization
        logger.debug(f"OPS_EVENTS: firewall screen unavailable: {e}")
        return (text or "")[:max_chars]


def screen_untrusted_fields(fields: dict) -> dict | None:
    """Assess a map of {field: text} as untrusted data. Returns a compact firewall
    summary if ANY field is instruction-like, else None. Pure (no mutation).

    This is the M19 realization of invariant #10 — telemetry text that may carry
    instruction-like content is flagged at normalization, so downstream consumers
    know it must be treated strictly as data."""
    clean = {k: v for k, v in (fields or {}).items() if v}
    if not clean:
        return None
    try:
        from core.injection_firewall import TrustOrigin, assess
    except Exception:  # noqa: BLE001
        return None
    worst = None
    hit_fields: list[str] = []
    for name, text in clean.items():
        a = assess(str(text), TrustOrigin.FILE_UNTRUSTED)
        if a.detected:
            hit_fields.append(name)
            if worst is None or a.confidence > worst.confidence:
                worst = a
    if worst is None:
        return None
    return {
        "detected": True,
        "fields": sorted(hit_fields),
        "attack_type": worst.attack_type.value,
        "confidence": round(worst.confidence, 2),
        "quarantine_required": worst.quarantine_required,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Adapters (one per real producer)
# ══════════════════════════════════════════════════════════════════════════════
class EventAdapter:
    """Base normalizer. A concrete adapter turns one producer's legacy dict into
    a canonical :class:`OperationalEvent`. ``can_handle`` gates dispatch."""
    name = "base"
    source = EventSource.UNKNOWN

    def can_handle(self, payload: dict) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def normalize(self, payload: dict, *, now_iso: str, signed: bool) -> OperationalEvent:  # pragma: no cover
        raise NotImplementedError

    # shared helpers -----------------------------------------------------------
    def _finalize(
        self, *, category: EventCategory, severity: EventSeverity, payload: dict,
        now_iso: str, signed: bool, observed_at: str | None,
        entities: list[EntityReference], evidence: list[EvidenceReference],
        untrusted: dict, raw_reference: str, confidence: float | None = None,
        techniques: tuple[str, ...] = (), fields: dict | None = None,
    ) -> OperationalEvent:
        prov = EventProvenance(
            source=self.source,
            source_instance=str(payload.get("source_instance")
                                 or payload.get("agent_id")
                                 or payload.get("agent_host")
                                 or payload.get("host") or ""),
            adapter=self.name, signed=signed, ingested_at=now_iso,
        )
        injection = screen_untrusted_fields(untrusted)
        base = dict(fields or {})
        ev = OperationalEvent(
            event_id="",  # set after hashing
            provenance=prov, source=self.source, category=category,
            severity=severity, timestamp=now_iso, observed_at=observed_at,
            confidence=confidence,
            entities=tuple(_dedup_entities(entities)),
            evidence=tuple(evidence),
            raw_reference=raw_reference,
            untrusted_text={k: _clip(v) for k, v in untrusted.items() if v},
            injection=injection,
            mitre_techniques=tuple(dict.fromkeys(t for t in techniques if t)),
            **base,
        )
        chash = ev.compute_content_hash()
        import dataclasses
        return dataclasses.replace(ev, content_hash=chash, event_id=f"oe_{chash[:16]}")


def _dedup_entities(entities: list[EntityReference]) -> list[EntityReference]:
    seen: set[str] = set()
    out: list[EntityReference] = []
    for e in entities:
        if not e.value:
            continue
        k = e.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _mitre_ids(*values) -> tuple[str, ...]:
    """Extract bare technique ids (T####[.###]) from free-form strings/lists."""
    import re
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        items = v if isinstance(v, (list, tuple, set)) else [v]
        for item in items:
            for m in re.findall(r"T\d{4}(?:\.\d{3})?", str(item)):
                out.append(m)
    return tuple(dict.fromkeys(out))


class SysmonAdapter(EventAdapter):
    name = "sysmon"
    source = EventSource.SYSMON
    _CATEGORY = {1: EventCategory.PROCESS, 3: EventCategory.NETWORK,
                 7: EventCategory.PROCESS, 8: EventCategory.PROCESS,
                 10: EventCategory.AUTH, 11: EventCategory.FILE,
                 25: EventCategory.PROCESS}

    def can_handle(self, payload: dict) -> bool:
        return payload.get("type") == "sysmon_event"

    def normalize(self, payload, *, now_iso, signed):
        eid = _to_int(payload.get("event_id"))
        category = self._CATEGORY.get(eid or -1, EventCategory.PROCESS)
        cmd = _clip(payload.get("commandline"))
        untrusted = {}
        if cmd:
            untrusted["command_line"] = cmd
        entities = [
            EntityReference(EntityType.HOST, str(payload.get("agent_host") or payload.get("host") or ""))
        ] if (payload.get("agent_host") or payload.get("host")) else []
        proc = _clip(payload.get("process"))
        if proc:
            entities.append(EntityReference(EntityType.PROCESS, proc))
        pid = _to_int(payload.get("pid"))
        if pid:
            entities.append(EntityReference(EntityType.PID, str(pid)))
        if payload.get("target_ip"):
            entities.append(EntityReference(EntityType.IP, str(payload["target_ip"])))
        return self._finalize(
            category=category,
            severity=normalize_severity(payload.get("severity")),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("timestamp")),
            entities=entities,
            evidence=[EvidenceReference("telemetry", f"sysmon:eid={eid}",
                                        _clip(payload.get("technique")) or "")],
            untrusted=untrusted,
            raw_reference=f"sysmon_event:eid={eid}",
            techniques=_mitre_ids(payload.get("technique")),
            fields={
                "host": _clip(payload.get("agent_host") or payload.get("host")),
                "process": proc, "pid": pid,
                "parent_process": _clip(payload.get("parent")),
                "command_line": cmd,
                "dst_ip": _clip(payload.get("target_ip")),
            },
        )


class ZeekConnAdapter(EventAdapter):
    name = "zeek_conn"
    source = EventSource.ZEEK_CONN

    def can_handle(self, payload: dict) -> bool:
        # A raw Zeek conn.log record (no legacy 'type', has the TSV keys).
        return "type" not in payload and (
            "id.orig_h" in payload or "id.resp_h" in payload
        )

    def normalize(self, payload, *, now_iso, signed):
        src = _clip(payload.get("id.orig_h"))
        dst = _clip(payload.get("id.resp_h"))
        entities = []
        if src:
            entities.append(EntityReference(EntityType.IP, src))
        if dst:
            entities.append(EntityReference(EntityType.IP, dst))
        return self._finalize(
            category=EventCategory.NETWORK,
            severity=EventSeverity.INFO,
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("ts")),
            entities=entities,
            evidence=[EvidenceReference("log_line", "zeek:conn.log",
                                        _clip(payload.get("uid")) or "")],
            untrusted={},
            raw_reference=f"zeek_conn:uid={payload.get('uid','')}",
            fields={
                "src_ip": src, "dst_ip": dst,
                "src_port": _to_int(payload.get("id.orig_p")),
                "dst_port": _to_int(payload.get("id.resp_p")),
                "protocol": _clip(payload.get("proto")),
                "service": _clip(payload.get("service")),
            },
        )


class ZeekDnsAdapter(EventAdapter):
    name = "zeek_dns"
    source = EventSource.ZEEK_DNS

    def can_handle(self, payload: dict) -> bool:
        return (payload.get("type") == "dpi_alert"
                and str(payload.get("protocol", "")).upper() == "DNS")

    def normalize(self, payload, *, now_iso, signed):
        detail = _clip(payload.get("detail"))
        untrusted = {"signature": detail} if detail else {}
        src = _clip(payload.get("src_ip"))
        entities = [EntityReference(EntityType.IP, src)] if src else []
        return self._finalize(
            category=EventCategory.DNS,
            severity=normalize_severity(payload.get("severity") or "MEDIUM"),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("timestamp")),
            entities=entities,
            evidence=[EvidenceReference("telemetry", "zeek:dns.log", detail or "")],
            untrusted=untrusted,
            raw_reference="zeek_dns:dpi_alert",
            techniques=_mitre_ids(payload.get("technique")),
            fields={
                "src_ip": src, "protocol": "DNS",
                "signature": _clip(payload.get("technique")),
            },
        )


class ZeekHttpAdapter(EventAdapter):
    name = "zeek_http"
    source = EventSource.ZEEK_HTTP

    def can_handle(self, payload: dict) -> bool:
        return (payload.get("type") == "dpi_alert"
                and str(payload.get("protocol", "")).upper() == "HTTP")

    def normalize(self, payload, *, now_iso, signed):
        detail = _clip(payload.get("detail"))
        untrusted = {"signature": detail} if detail else {}
        src = _clip(payload.get("src_ip"))
        entities = [EntityReference(EntityType.IP, src)] if src else []
        return self._finalize(
            category=EventCategory.HTTP,
            severity=normalize_severity(payload.get("severity") or "MEDIUM"),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("timestamp")),
            entities=entities,
            evidence=[EvidenceReference("telemetry", "zeek:http.log", detail or "")],
            untrusted=untrusted,
            raw_reference="zeek_http:dpi_alert",
            techniques=_mitre_ids(payload.get("technique")),
            fields={
                "src_ip": src, "protocol": "HTTP",
                "signature": _clip(payload.get("technique")),
            },
        )


class NetworkBaselineAdapter(EventAdapter):
    name = "network_baseline"
    source = EventSource.NETWORK_BASELINE

    def can_handle(self, payload: dict) -> bool:
        return payload.get("type") == "network_anomaly"

    def normalize(self, payload, *, now_iso, signed):
        detail = _clip(payload.get("description"))
        untrusted = {"signature": detail} if detail else {}
        src = _clip(payload.get("src_ip"))
        entities = [EntityReference(EntityType.IP, src)] if src else []
        return self._finalize(
            category=EventCategory.ANOMALY,
            severity=normalize_severity(payload.get("severity")),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("timestamp")),
            entities=entities,
            evidence=[EvidenceReference("telemetry",
                                        f"baseline:{payload.get('detector','')}",
                                        detail or "")],
            untrusted=untrusted,
            raw_reference=f"network_anomaly:{payload.get('detector','')}",
            techniques=_mitre_ids(payload.get("technique")),
            fields={
                "src_ip": src, "dst_port": _to_int(payload.get("dst_port")),
                "signature": _clip(payload.get("detector")),
            },
        )


class SensorMeshAdapter(EventAdapter):
    name = "sensor_mesh"
    source = EventSource.SENSOR_MESH
    _TYPES = frozenset({"sensor_connected", "sensor_disconnected",
                        "sensor_deployed", "sensor_deploy_failed"})

    def can_handle(self, payload: dict) -> bool:
        return payload.get("type") in self._TYPES

    def normalize(self, payload, *, now_iso, signed):
        agent = _clip(payload.get("agent_id"))
        host = _clip(payload.get("hostname") or payload.get("agent_host"))
        ip = _clip(payload.get("ip") or payload.get("target_ip") or payload.get("agent_ip"))
        entities: list[EntityReference] = []
        if agent:
            entities.append(EntityReference(EntityType.AGENT, agent))
        if host:
            entities.append(EntityReference(EntityType.HOST, host))
        if ip:
            entities.append(EntityReference(EntityType.IP, ip))
        return self._finalize(
            category=EventCategory.SENSOR,
            severity=normalize_severity(payload.get("severity") or "INFO"),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("timestamp")),
            entities=entities,
            evidence=[EvidenceReference("event", f"sensor_mesh:{payload.get('type')}",
                                        agent or "")],
            untrusted={},
            raw_reference=f"{payload.get('type')}:{agent or ip or ''}",
            fields={
                "host": host, "src_ip": ip,
                "service": _clip(payload.get("os")),
                "signature": _clip(payload.get("type")),
            },
        )


class CorrelatorAdapter(EventAdapter):
    name = "correlator"
    source = EventSource.CORRELATOR

    def can_handle(self, payload: dict) -> bool:
        return payload.get("type") in ("compound_incident", "compound_incident_resolved")

    def normalize(self, payload, *, now_iso, signed):
        hosts = payload.get("involved_hosts") or []
        if isinstance(hosts, (set, frozenset)):
            hosts = list(hosts)
        entities = [EntityReference(EntityType.IP, str(h)) for h in hosts if h]
        for pid in (payload.get("involved_pids") or []):
            if pid:
                entities.append(EntityReference(EntityType.PID, str(pid)))
        return self._finalize(
            category=EventCategory.INCIDENT,
            severity=normalize_severity(payload.get("severity_score")),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("last_seen") or payload.get("first_seen")),
            entities=entities,
            evidence=[EvidenceReference("event", f"incident:{payload.get('incident_id','')}",
                                        _clip(payload.get("rule")) or "")],
            untrusted={},
            raw_reference=f"compound_incident:{payload.get('incident_id','')}",
            techniques=_mitre_ids(payload.get("mitre_techniques")),
            fields={
                "rule_id": _clip(payload.get("rule")),
                "signature": _clip(payload.get("kill_chain_phase")),
            },
        )


class InternalAdapter(EventAdapter):
    """Fallback for JARVIS-internal operational events (any dict with a ``type``
    that no producer-specific adapter claimed). Preserves whatever structured
    fields exist and treats a ``message``/``detail`` string as untrusted text."""
    name = "internal"
    source = EventSource.JARVIS_INTERNAL

    def can_handle(self, payload: dict) -> bool:
        return True  # registry uses this last

    def normalize(self, payload, *, now_iso, signed):
        msg = _clip(payload.get("message") or payload.get("detail") or payload.get("description"))
        untrusted = {"signature": msg} if msg else {}
        entities: list[EntityReference] = []
        for key, etype in (("host", EntityType.HOST), ("src_ip", EntityType.IP),
                           ("ip", EntityType.IP), ("user", EntityType.USER),
                           ("process", EntityType.PROCESS)):
            val = _clip(payload.get(key))
            if val:
                entities.append(EntityReference(etype, val))
        return self._finalize(
            category=EventCategory.SYSTEM,
            severity=normalize_severity(payload.get("severity")),
            payload=payload, now_iso=now_iso, signed=signed,
            observed_at=_clip(payload.get("timestamp")),
            entities=entities,
            evidence=[EvidenceReference("event", f"internal:{payload.get('type','')}",
                                        msg or "")],
            untrusted=untrusted,
            raw_reference=f"{payload.get('type','event')}",
            techniques=_mitre_ids(payload.get("technique"), payload.get("attck")),
            fields={
                "host": _clip(payload.get("host")),
                "src_ip": _clip(payload.get("src_ip") or payload.get("ip")),
                "user": _clip(payload.get("user")),
                "process": _clip(payload.get("process")),
                "pid": _to_int(payload.get("pid")),
                "rule_id": _clip(payload.get("rule")),
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Ingest result + registry
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EventIngestResult:
    """The outcome of normalizing one payload."""
    ok: bool
    event: OperationalEvent | None = None
    duplicate: bool = False
    adapter: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "duplicate": self.duplicate, "adapter": self.adapter,
            "error": self.error,
            "event": self.event.to_dict() if self.event else None,
        }


class EventAdapterRegistry:
    """Dispatches a legacy payload to the first adapter that claims it, normalizes
    it into an :class:`OperationalEvent`, and tracks a bounded dedup index of
    content hashes so repeated observations are reported (not silently dropped)."""

    def __init__(self, adapters: list[EventAdapter] | None = None,
                 *, dedup_cap: int = _DEDUP_CAP) -> None:
        # Order matters: specific producers first, InternalAdapter last (catch-all).
        self.adapters: list[EventAdapter] = adapters or _default_adapters()
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._dedup_cap = max(64, dedup_cap)

    def register(self, adapter: EventAdapter, *, first: bool = False) -> None:
        if first:
            self.adapters.insert(0, adapter)
        else:
            # keep the catch-all InternalAdapter last
            idx = len(self.adapters)
            for i, a in enumerate(self.adapters):
                if isinstance(a, InternalAdapter):
                    idx = i
                    break
            self.adapters.insert(idx, adapter)

    def _pick(self, payload: dict) -> EventAdapter | None:
        for adapter in self.adapters:
            try:
                if adapter.can_handle(payload):
                    return adapter
            except Exception:  # noqa: BLE001 — a bad can_handle never breaks ingest
                continue
        return None

    def _mark_seen(self, content_hash: str) -> bool:
        """Return True if this hash was newly recorded (i.e. NOT a duplicate)."""
        if content_hash in self._seen:
            return False
        self._seen.add(content_hash)
        self._order.append(content_hash)
        while len(self._order) > self._dedup_cap:
            old = self._order.popleft()
            self._seen.discard(old)
        return True

    def normalize(
        self, payload: dict, *, now_iso: str | None = None, signed: bool = False,
    ) -> EventIngestResult:
        """Normalize one legacy dict into a canonical event.

        ``signed`` records whether the telemetry arrived HMAC-authenticated at the
        bus (provenance). Determinism: pass ``now_iso`` to pin the ingestion time;
        the content hash never depends on it, so IDs are stable regardless."""
        if not isinstance(payload, dict) or not payload:
            return EventIngestResult(ok=False, error="empty or non-dict payload")
        now_iso = now_iso or _now_iso()
        # An event already carrying provenance/signing survives round-trips.
        signed = bool(signed or payload.get("__signed") or payload.get("__src"))
        adapter = self._pick(payload)
        if adapter is None:
            return EventIngestResult(ok=False, error="no adapter claimed payload")
        try:
            event = adapter.normalize(payload, now_iso=now_iso, signed=signed)
        except Exception as e:  # noqa: BLE001 — a bad payload is a failed ingest, not a crash
            logger.warning(f"OPS_EVENTS: {adapter.name} normalize failed: {e}")
            return EventIngestResult(ok=False, adapter=adapter.name, error=str(e)[:200])
        fresh = self._mark_seen(event.content_hash)
        return EventIngestResult(ok=True, event=event, duplicate=not fresh,
                                 adapter=adapter.name)

    def normalize_many(self, payloads, *, now_iso: str | None = None,
                       signed: bool = False) -> list[EventIngestResult]:
        return [self.normalize(p, now_iso=now_iso, signed=signed) for p in payloads]


def _default_adapters() -> list[EventAdapter]:
    return [
        SysmonAdapter(), ZeekDnsAdapter(), ZeekHttpAdapter(), ZeekConnAdapter(),
        NetworkBaselineAdapter(), SensorMeshAdapter(), CorrelatorAdapter(),
        InternalAdapter(),   # catch-all — must stay last
    ]


# Module-level singleton — the canonical normalization entry point.
registry = EventAdapterRegistry()


def normalize_event(payload: dict, *, now_iso: str | None = None,
                    signed: bool = False) -> EventIngestResult:
    """Canonical normalization via the module singleton (convenience)."""
    return registry.normalize(payload, now_iso=now_iso, signed=signed)
