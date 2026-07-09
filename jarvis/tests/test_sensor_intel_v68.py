"""tests/test_sensor_intel_v68.py — V68 M41 sensor trust, health & coverage.

Proves the four dimensions stay orthogonal and honest:
  * connection vs health — a socket can be CONNECTED/IDLE yet SILENT; quiet (low-volume,
    recent) is not broken, gone-dark (past the silent horizon) is;
  * trust is conservative — an unsigned localhost agent is DECLARED (observe-only), never
    VERIFIED; signing lifts it to SIGNED; a pinned signer to VERIFIED; an origin mismatch
    drops it to UNTRUSTED; "trusted for action" counts SIGNED+ only;
  * coverage — an authorized target with a producing sensor is COVERED, with a stale one
    DEGRADED, with none UNCOVERED (an honest blind spot, never inferred as compromise);
  * bounded + ASCII output.

Deterministic: a pinned ``now``; every timestamp derived from a fixed anchor. No mesh
sockets, no network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.sensor_intel import (
    ConnectionState,
    CoverageState,
    SensorHealth,
    TrustState,
    assess_mesh,
    assess_sensor,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _iso(secs_ago: float) -> str:
    return (NOW - timedelta(seconds=secs_ago)).isoformat()


def _agent(agent_id="a1", *, connected_ago=10.0, last_event_ago=None, events=0, **extra):
    a = {"agent_id": agent_id, "hostname": f"{agent_id}-host", "ip": "10.0.0.5",
         "os": "Linux", "connected": _iso(connected_ago), "events_received": events,
         "last_event_at": _iso(last_event_ago) if last_event_ago is not None else None,
         "transport": "localhost-tunnel"}
    a.update(extra)
    return a


# ── connection vs health orthogonality ───────────────────────────────────────────
class TestConnectionAndHealth:
    def test_producing_sensor(self):
        s = assess_sensor(_agent(connected_ago=30, last_event_ago=5, events=100), now=NOW)
        assert s.connection is ConnectionState.CONNECTED
        assert s.health is SensorHealth.PRODUCING

    def test_new_quiet_sensor_is_not_broken(self):
        s = assess_sensor(_agent(connected_ago=5, events=0), now=NOW)
        assert s.connection is ConnectionState.CONNECTED
        assert s.health is SensorHealth.QUIET

    def test_connected_but_silent(self):
        s = assess_sensor(_agent(connected_ago=1000, events=0), now=NOW)
        assert s.connection is ConnectionState.IDLE       # socket open, no activity
        assert s.health is SensorHealth.SILENT

    def test_stale_after_producing(self):
        s = assess_sensor(_agent(connected_ago=1000, last_event_ago=400, events=50), now=NOW)
        assert s.connection is ConnectionState.IDLE
        assert s.health is SensorHealth.STALE

    def test_gone_dark_after_producing(self):
        s = assess_sensor(_agent(connected_ago=2000, last_event_ago=1200, events=50), now=NOW)
        assert s.health is SensorHealth.SILENT


# ── trust is conservative ─────────────────────────────────────────────────────────
class TestTrust:
    def test_unsigned_localhost_is_declared_not_verified(self):
        assert assess_sensor(_agent(), now=NOW).trust is TrustState.DECLARED

    def test_signed_is_signed(self):
        assert assess_sensor(_agent(signed=True), now=NOW).trust is TrustState.SIGNED

    def test_signed_and_pinned_is_verified(self):
        s = assess_sensor(_agent(signed=True, key_pinned=True), now=NOW)
        assert s.trust is TrustState.VERIFIED

    def test_off_transport_unsigned_is_unverified(self):
        s = assess_sensor(_agent(transport="public-internet"), now=NOW)
        assert s.trust is TrustState.UNVERIFIED

    def test_origin_mismatch_is_untrusted(self):
        s = assess_sensor(_agent(signed=True, origin_mismatch=True), now=NOW)
        assert s.trust is TrustState.UNTRUSTED


# ── mesh aggregate + coverage ─────────────────────────────────────────────────────
class TestMeshAndCoverage:
    def _mesh(self):
        agents = [
            _agent("web", connected_ago=60, last_event_ago=5, events=200),      # producing
            _agent("db", connected_ago=1000, last_event_ago=400, events=10),    # stale
            _agent("edge", connected_ago=2000, events=0, signed=True, key_pinned=True),
        ]
        expected = [
            {"target": "env-web", "match": "web-host"},     # -> producing -> COVERED
            {"target": "env-db", "match": "db-host"},        # -> stale -> DEGRADED
            {"target": "env-dmz", "match": "dmz-host"},      # -> no sensor -> UNCOVERED
        ]
        return assess_mesh(agents, now=NOW, telemetry={}, expected=expected)

    def test_aggregate_counts(self):
        m = self._mesh()
        assert len(m.sensors) == 3
        assert m.connected == 3
        assert m.producing == 1
        # only the pinned+signed edge sensor is trusted for action; the rest are DECLARED
        assert m.trusted == 1

    def test_coverage_states(self):
        cov = {c.target: c.state for c in self._mesh().coverage}
        assert cov["env-web"] is CoverageState.COVERED
        assert cov["env-db"] is CoverageState.DEGRADED
        assert cov["env-dmz"] is CoverageState.UNCOVERED

    def test_coverage_ratio(self):
        assert self._mesh().coverage_ratio == round(1 / 3, 4)

    def test_telemetry_state_threaded(self):
        m = assess_mesh([_agent("web", last_event_ago=5, events=9)], now=NOW,
                        telemetry={"sensor:web": {"state": "healthy"}}, expected=[])
        assert m.sensors[0].telemetry_state == "healthy"


# ── boundedness + ASCII ───────────────────────────────────────────────────────────
class TestBoundedAscii:
    def test_output_is_ascii_and_bounded(self):
        agents = [_agent(f"s{i}", last_event_ago=1, events=1) for i in range(200)]
        d = assess_mesh(agents, now=NOW, telemetry={}, expected=[]).to_dict()
        assert len(d["sensors"]) <= 64
        assert str(d).isascii()
        assert d["panel"] == "sensor_intel"
