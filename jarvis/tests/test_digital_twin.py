"""
tests/test_digital_twin.py — V66 M23 operational digital twin & drift.

Covers the required M23 surface: observed vs expected, unknown state, missing
service, stopped workload, exposure drift, sensor drift, version drift, confidence
propagation, and no auto-remediation.
"""
from __future__ import annotations

from core.asset_graph import AssetGraph, AssetType, ObservationSource
from core.digital_twin import (
    DigitalTwin,
    DriftSeverity,
    DriftType,
    FactKind,
)

T0 = "2026-07-07T12:00:00+00:00"


def _twin() -> DigitalTwin:
    return DigitalTwin()


# ── observed matches expected → no drift ──────────────────────────────────────
def test_match_no_drift():
    t = _twin()
    t.set_expected("ubuntu-server", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("ubuntu-server", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    snap = t.compute_drift(now_iso=T0)
    assert snap.findings == ()


# ── missing service ───────────────────────────────────────────────────────────
def test_service_missing():
    t = _twin()
    t.set_expected("ubuntu-server", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("ubuntu-server", "service:ssh", "absent", kind=FactKind.SERVICE, now_iso=T0)
    snap = t.compute_drift(now_iso=T0)
    f = snap.findings[0]
    assert f.drift_type is DriftType.SERVICE_MISSING
    assert f.severity is DriftSeverity.HIGH
    assert f.recommended_investigation == "SERVICE_DIAGNOSIS"
    assert f.verification_required is True


# ── stopped workload ──────────────────────────────────────────────────────────
def test_workload_stopped():
    t = _twin()
    t.set_expected("ubuntu-target", "container:ubuntu-target", "running",
                   kind=FactKind.WORKLOAD, now_iso=T0)
    t.observe("ubuntu-target", "container:ubuntu-target", "stopped",
              kind=FactKind.WORKLOAD, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.WORKLOAD_STOPPED
    assert f.recommended_investigation == "CONTAINER_HEALTH_CHECK"


# ── exposure drift ────────────────────────────────────────────────────────────
def test_network_exposure_drift():
    t = _twin()
    t.set_expected("svc", "exposure:8080", "localhost", kind=FactKind.EXPOSURE, now_iso=T0)
    t.observe("svc", "exposure:8080", "authorized_subnet", kind=FactKind.EXPOSURE, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.NETWORK_EXPOSURE_DRIFT
    assert f.severity is DriftSeverity.HIGH


def test_new_unexpected_exposure():
    t = _twin()
    # nothing expected on this port, but it is observed exposed
    t.observe("svc", "exposure:9999", "external", kind=FactKind.EXPOSURE, now_iso=T0)
    findings = t.compute_drift(now_iso=T0).findings
    assert any(f.drift_type is DriftType.NETWORK_EXPOSURE_DRIFT and f.expected_fact is None
               for f in findings)


# ── sensor drift ──────────────────────────────────────────────────────────────
def test_sensor_coverage_drift():
    t = _twin()
    t.set_expected("zeek", "sensor:mesh", "connected", kind=FactKind.SENSOR, now_iso=T0)
    t.observe("zeek", "sensor:mesh", "disconnected", kind=FactKind.SENSOR, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.SENSOR_COVERAGE_DRIFT
    assert f.severity is DriftSeverity.HIGH


# ── version drift ─────────────────────────────────────────────────────────────
def test_version_drift():
    t = _twin()
    t.set_expected("app", "version:app", "1.2.3", kind=FactKind.VERSION, now_iso=T0)
    t.observe("app", "version:app", "1.2.4", kind=FactKind.VERSION, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.VERSION_DRIFT
    assert f.severity is DriftSeverity.LOW


# ── unknown stays unknown ─────────────────────────────────────────────────────
def test_unknown_state_stays_unknown():
    t = _twin()
    t.set_expected("ubuntu-server", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    # no observation at all → STATE_UNKNOWN, NOT SERVICE_MISSING
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.STATE_UNKNOWN
    assert f.observed_fact is None
    # unknown does not count an asset as "having drift"
    assert "ubuntu-server" not in t.compute_drift(now_iso=T0).assets_with_drift


def test_observe_none_is_unknown_not_absent():
    t = _twin()
    t.set_expected("h", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("h", "service:ssh", None, kind=FactKind.SERVICE, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.STATE_UNKNOWN


# ── confidence propagation ────────────────────────────────────────────────────
def test_confidence_propagation():
    t = _twin()
    t.set_expected("h", "service:ssh", "active", kind=FactKind.SERVICE,
                   confidence=0.9, now_iso=T0)
    t.observe("h", "service:ssh", "absent", kind=FactKind.SERVICE,
              confidence=0.6, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.confidence == 0.6              # min of the two
    # unknown drift is lower-confidence than a verified mismatch
    t2 = _twin()
    t2.set_expected("h", "service:ssh", "active", kind=FactKind.SERVICE,
                    confidence=0.9, now_iso=T0)
    fu = t2.compute_drift(now_iso=T0).findings[0]
    assert fu.confidence <= 0.4


# ── no auto-remediation ───────────────────────────────────────────────────────
def test_no_auto_remediation():
    t = _twin()
    t.set_expected("h", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("h", "service:ssh", "absent", kind=FactKind.SERVICE, now_iso=T0)
    findings = t.compute_drift(now_iso=T0).findings
    # findings are pure data — they recommend investigation, never an action tool,
    # and always require verification (a signal, never an executed remedy).
    for f in findings:
        assert f.verification_required is True
        d = f.to_dict()
        assert "action_tool" not in d and "execute" not in d
        # the recommendation is a runbook NAME (a plan), not a world-effect
        assert f.recommended_investigation.isupper()


# ── desired-vs-observed config drift ──────────────────────────────────────────
def test_desired_config_drift():
    t = _twin()
    t.set_desired("fw", "policy:default", "deny", kind=FactKind.GENERIC, now_iso=T0)
    t.observe("fw", "policy:default", "allow", kind=FactKind.GENERIC, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.CONFIG_DRIFT


# ── snapshot summary ──────────────────────────────────────────────────────────
def test_snapshot_summary_counts():
    t = _twin()
    t.set_expected("a", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("a", "service:ssh", "absent", kind=FactKind.SERVICE, now_iso=T0)
    t.set_expected("b", "sensor:mesh", "connected", kind=FactKind.SENSOR, now_iso=T0)
    t.observe("b", "sensor:mesh", "disconnected", kind=FactKind.SENSOR, now_iso=T0)
    snap = t.compute_drift(now_iso=T0)
    assert snap.by_severity.get("high") == 2
    assert set(snap.assets_with_drift) == {"a", "b"}
    assert snap.to_dict()["drift_count"] == 2


# ── asset-graph fold (baseline/exposure signal source) ────────────────────────
def test_observe_from_asset_graph():
    g = AssetGraph()
    g.observe_service(AssetType.VM, "ubuntu-server", port=8080, exposure="external",
                      source=ObservationSource.SERVICE_OBSERVATION, now_iso=T0)
    t = _twin()
    t.set_expected("ubuntu-server", "exposure:8080", "localhost",
                   kind=FactKind.EXPOSURE, now_iso=T0)
    t.observe_from_asset_graph(g, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    assert f.drift_type is DriftType.NETWORK_EXPOSURE_DRIFT


# ── presence connector (SUGGEST, never ACT) ───────────────────────────────────
def test_drift_to_presence_event_is_suggest():
    from core.presence import PresenceLevel
    t = _twin()
    t.set_expected("h", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("h", "service:ssh", "absent", kind=FactKind.SERVICE, now_iso=T0)
    f = t.compute_drift(now_iso=T0).findings[0]
    ev = DigitalTwin.drift_to_presence_event(f)
    assert ev is not None
    assert ev.desired_level is PresenceLevel.SUGGEST
    assert ev.action_tool is None            # never proposes a concrete action
