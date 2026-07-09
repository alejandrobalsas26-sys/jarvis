"""core/sensor_intel.py — V68 M41: sensor trust, health & coverage intelligence.

The V67 sensor mesh answers one question — "which agents hold a socket open?" That
conflates four independent things an operator must keep apart:

  CONNECTION  is the transport up?           CONNECTED / IDLE / DISCONNECTED
  HEALTH      is it actually producing?      PRODUCING / QUIET / STALE / SILENT
  TRUST       may I believe / act on it?     VERIFIED > SIGNED > DECLARED > UNVERIFIED > UNTRUSTED
  COVERAGE    what surface do we observe?    COVERED / DEGRADED / UNCOVERED

They are deliberately orthogonal: a socket can be CONNECTED yet SILENT (health), and a
PRODUCING sensor can still be only DECLARED (trust) — a live, chatty, unsigned agent is
NOT a trusted one. Trust is conservative by construction: an unsigned localhost-tunnel
agent is DECLARED (safe to observe), never VERIFIED (safe to act on) — absence of a
signature is not proof of authenticity, and a sensor going dark is uncertainty about
coverage, never evidence of compromise.

This layer only READS mesh state (+ optional M39 telemetry for freshness/rate); it never
mutates trust decisions or the control plane. Deterministic: every assessment takes an
explicit ``now`` (tests pin it); bounded output; ASCII (Windows console safe).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

_MAX_ROWS = 64                 # Rule of Silicon: bounded panels
_IDLE_S = 120.0                # connected but no event this long -> IDLE (still connected)
_STALE_S = 300.0               # last event older than this -> STALE
_SILENT_S = 900.0              # no event this long -> SILENT (was producing, went dark)


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    IDLE = "idle"              # socket open, no recent activity (still connected)
    DISCONNECTED = "disconnected"   # expected but absent (coverage only)
    UNKNOWN = "unknown"


class SensorHealth(str, Enum):
    PRODUCING = "producing"    # events flowing recently
    QUIET = "quiet"            # connected, low/no events but within grace (quiet != broken)
    STALE = "stale"            # last event past the staleness horizon
    SILENT = "silent"          # connected but no events past the silent horizon
    UNKNOWN = "unknown"


class TrustState(str, Enum):
    VERIFIED = "verified"      # signed AND signer pinned/known
    SIGNED = "signed"          # cryptographically signed, signer not pinned
    DECLARED = "declared"      # localhost-tunnel, self-declared identity, unsigned
    UNVERIFIED = "unverified"  # connected but off-transport / no identity assurance
    UNTRUSTED = "untrusted"    # origin mismatch / failed verification


class CoverageState(str, Enum):
    COVERED = "covered"        # target has a producing sensor
    DEGRADED = "degraded"      # target has a sensor but stale/silent
    UNCOVERED = "uncovered"    # authorized target with no sensor


_TRUST_RANK = {TrustState.UNTRUSTED: 0, TrustState.UNVERIFIED: 1, TrustState.DECLARED: 2,
               TrustState.SIGNED: 3, TrustState.VERIFIED: 4}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _age_s(ts: datetime | None, now: datetime) -> float | None:
    return (now - ts).total_seconds() if ts else None


# ══════════════════════════════════════════════════════════════════════════════
#  Per-sensor assessment (four orthogonal dimensions)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class SensorAssessment:
    agent_id: str
    hostname: str
    ip: str
    connection: ConnectionState
    health: SensorHealth
    trust: TrustState
    events_received: int = 0
    last_event_age_s: float | None = None
    connected_age_s: float | None = None
    reasons: list[str] = field(default_factory=list)
    telemetry_state: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "hostname": self.hostname,
            "ip": self.ip,
            "connection": self.connection.value,
            "health": self.health.value,
            "trust": self.trust.value,
            "events_received": self.events_received,
            "last_event_age_s": _r(self.last_event_age_s),
            "connected_age_s": _r(self.connected_age_s),
            "telemetry_state": self.telemetry_state,
            "reasons": self.reasons[:6],
        }


def assess_sensor(agent: dict, *, now: datetime | None = None,
                  telemetry_state: str | None = None) -> SensorAssessment:
    now = now or _now()
    agent = agent or {}
    agent_id = str(agent.get("agent_id", "unknown"))
    events = int(agent.get("events_received", 0) or 0)
    last_event = _parse_iso(agent.get("last_event_at"))
    connected = _parse_iso(agent.get("connected"))
    last_age = _age_s(last_event, now)
    conn_age = _age_s(connected, now)
    reasons: list[str] = []

    # ── connection (socket present == connected; grace gap -> idle) ──────────────
    activity_age = last_age if last_age is not None else conn_age
    if activity_age is not None and activity_age > _IDLE_S:
        connection = ConnectionState.IDLE
        reasons.append(f"no activity for {int(activity_age)}s (socket still open)")
    else:
        connection = ConnectionState.CONNECTED

    # ── health (quiet != broken; distinguishes low-volume from gone-dark) ────────
    if events == 0:
        if conn_age is not None and conn_age > _SILENT_S:
            health = SensorHealth.SILENT
            reasons.append(f"connected {int(conn_age)}s, zero events")
        else:
            health = SensorHealth.QUIET
            reasons.append("connected, no events yet (within grace)")
    elif last_age is None:
        health = SensorHealth.QUIET
    elif last_age <= _STALE_S:
        health = SensorHealth.PRODUCING
    elif last_age <= _SILENT_S:
        health = SensorHealth.STALE
        reasons.append(f"last event {int(last_age)}s ago")
    else:
        health = SensorHealth.SILENT
        reasons.append(f"silent {int(last_age)}s after producing")

    # ── trust (conservative; unsigned live agent is DECLARED, never VERIFIED) ────
    trust = _assess_trust(agent, reasons)

    return SensorAssessment(
        agent_id=agent_id, hostname=str(agent.get("hostname", "")),
        ip=str(agent.get("ip", "")), connection=connection, health=health, trust=trust,
        events_received=events, last_event_age_s=last_age, connected_age_s=conn_age,
        reasons=reasons, telemetry_state=telemetry_state)


def _assess_trust(agent: dict, reasons: list[str]) -> TrustState:
    transport = str(agent.get("transport", "")).lower()
    localhost = "localhost" in transport or "tunnel" in transport or transport == ""
    if agent.get("origin_mismatch") or agent.get("verification_failed"):
        reasons.append("origin/verification failed")
        return TrustState.UNTRUSTED
    if agent.get("signed"):
        if agent.get("verified") or agent.get("key_pinned"):
            return TrustState.VERIFIED
        reasons.append("signed, signer not pinned")
        return TrustState.SIGNED
    if localhost:
        reasons.append("localhost-tunnel, self-declared, unsigned (observe-only)")
        return TrustState.DECLARED
    reasons.append("off-transport, no identity assurance")
    return TrustState.UNVERIFIED


# ══════════════════════════════════════════════════════════════════════════════
#  Mesh-wide assessment + coverage
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class CoverageTarget:
    target: str
    state: CoverageState
    sensor: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {"target": self.target, "state": self.state.value,
                "sensor": self.sensor, "note": self.note}


@dataclass
class MeshAssessment:
    sensors: list[SensorAssessment] = field(default_factory=list)
    coverage: list[CoverageTarget] = field(default_factory=list)

    @property
    def connected(self) -> int:
        return sum(1 for s in self.sensors if s.connection in
                   (ConnectionState.CONNECTED, ConnectionState.IDLE))

    @property
    def producing(self) -> int:
        return sum(1 for s in self.sensors if s.health is SensorHealth.PRODUCING)

    @property
    def trusted(self) -> int:
        # "trusted enough to act": SIGNED or better. DECLARED is observe-only.
        return sum(1 for s in self.sensors
                   if _TRUST_RANK[s.trust] >= _TRUST_RANK[TrustState.SIGNED])

    @property
    def coverage_ratio(self) -> float:
        if not self.coverage:
            return 0.0
        covered = sum(1 for c in self.coverage if c.state is CoverageState.COVERED)
        return round(covered / len(self.coverage), 4)

    def to_dict(self) -> dict:
        return {
            "panel": "sensor_intel",
            "total": len(self.sensors),
            "connected": self.connected,
            "producing": self.producing,
            "trusted_for_action": self.trusted,
            "declared_observe_only": sum(1 for s in self.sensors
                                         if s.trust is TrustState.DECLARED),
            "coverage_ratio": self.coverage_ratio,
            "uncovered": sum(1 for c in self.coverage
                             if c.state is CoverageState.UNCOVERED),
            "sensors": [s.to_dict() for s in self.sensors[:_MAX_ROWS]],
            "coverage": [c.to_dict() for c in self.coverage[:_MAX_ROWS]],
        }


def assess_mesh(agents: list[dict] | None = None, *, now: datetime | None = None,
                telemetry: dict | None = None,
                expected: list[dict] | None = None) -> MeshAssessment:
    """Assess every connected sensor and (if *expected* targets are given) the coverage
    they provide. Pure over its inputs; live wrappers below read the real singletons."""
    now = now or _now()
    agents = agents if agents is not None else _live_agents()
    telemetry = telemetry if telemetry is not None else {}
    sensors: list[SensorAssessment] = []
    for a in agents[:_MAX_ROWS]:
        aid = str(a.get("agent_id", "unknown"))
        tsnap = telemetry.get(f"sensor:{aid}") or telemetry.get(aid)
        tstate = tsnap.get("state") if isinstance(tsnap, dict) else None
        sensors.append(assess_sensor(a, now=now, telemetry_state=tstate))

    expected = expected if expected is not None else _live_expected()
    coverage = _assess_coverage(sensors, expected)
    return MeshAssessment(sensors=sensors, coverage=coverage)


def _assess_coverage(sensors: list[SensorAssessment],
                     expected: list[dict]) -> list[CoverageTarget]:
    """Map each expected target to a sensor. A target is COVERED only when its matched
    sensor is actually PRODUCING; DEGRADED when a sensor exists but is stale/silent;
    UNCOVERED when authorized but unobserved (an honest blind spot, not a failure)."""
    out: list[CoverageTarget] = []
    for exp in expected[:_MAX_ROWS]:
        target = str(exp.get("target", ""))
        match = str(exp.get("match", "") or target).lower()
        hit = _match_sensor(sensors, match)
        if hit is None:
            out.append(CoverageTarget(target, CoverageState.UNCOVERED, note="no sensor"))
        elif hit.health is SensorHealth.PRODUCING:
            out.append(CoverageTarget(target, CoverageState.COVERED, sensor=hit.agent_id))
        else:
            out.append(CoverageTarget(target, CoverageState.DEGRADED, sensor=hit.agent_id,
                                      note=hit.health.value))
    return out


def _match_sensor(sensors: list[SensorAssessment], match: str) -> SensorAssessment | None:
    if not match:
        return None
    for s in sensors:
        if match in (s.hostname.lower(), s.ip.lower(), s.agent_id.lower()):
            return s
    for s in sensors:                       # looser containment fallback
        if match and (match in s.hostname.lower() or match in s.agent_id.lower()):
            return s
    return None


def _r(v) -> float | None:
    return round(v, 3) if isinstance(v, (int, float)) else None


# ── live source readers (guarded; any failure degrades, never raises) ───────────
def _live_agents() -> list[dict]:
    try:
        from core.sensor_mesh import get_connected_agents
        return get_connected_agents()
    except Exception:  # noqa: BLE001
        return []


def _live_telemetry() -> dict:
    try:
        from core.collector_fabric import fabric
        return fabric.telemetry_snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_expected() -> list[dict]:
    """Expected coverage targets = the operator-authorized environments (M29). Each
    should be observed by a sensor or a producing collector; absence is a blind spot."""
    try:
        from core.environment_registry import env_registry
        out = []
        for e in env_registry.authorized_environments():
            match = e.endpoint or e.display_name or e.env_id
            out.append({"target": e.env_id, "match": str(match).split(":")[0]})
        return out
    except Exception:  # noqa: BLE001
        return []


def build_live_sensor_intel() -> dict:
    """Bounded, HUD/CLI/voice-safe live snapshot of sensor trust/health/coverage."""
    return assess_mesh(telemetry=_live_telemetry()).to_dict()
