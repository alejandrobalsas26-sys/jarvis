"""
tests/test_ops_views.py — V66 M26 bounded, redacted AURA operational views.

Covers the required M26 surface: serialization, bounded payload, redaction, asset
summary, incident summary, drift summary, situation summary, and compatibility
with the current WebSocket broadcasting (flat JSON dicts with a `type`).
"""
from __future__ import annotations

import json

from core.asset_graph import AssetGraph, AssetType, ObservationSource
from core.aura_events import EVENT_TYPES
from core.digital_twin import DigitalTwin, FactKind
from core.incident_workspace import IncidentCase, IncidentSeverity, IncidentStatus
from core.ops_views import (
    asset_status_panel,
    correlation_finding_event,
    correlations_panel,
    drift_finding_event,
    drift_panel,
    incident_case_event,
    incidents_panel,
    runbooks_panel,
    sensor_health_panel,
    situation_event,
    situation_panel,
    system_status_panel,
)
from core.runbook_engine import RunbookEngine
from core.situation_engine import SituationEngine

T0 = "2026-07-07T12:00:00+00:00"


def _finding_dict(sev="high", conf=0.8):
    return {
        "finding_id": "cf_1", "rule": "suspicious_process_then_network",
        "group_entity": "host:win-client-01", "severity": sev, "confidence": conf,
        "mitre_techniques": ["T1059", "T1071"], "matched_event_ids": ["oe_1", "oe_2"],
        "explanation": {"reason": "grouped 2 events by host"},
    }


# ── serialization / websocket compatibility ───────────────────────────────────
def test_events_are_flat_json_with_type_and_timestamp():
    ev = correlation_finding_event(_finding_dict())
    assert ev["type"] == "correlation_finding"
    assert "timestamp" in ev
    # must be JSON-serializable for manager.broadcast (json.dumps)
    json.dumps(ev)


def test_all_new_event_types_registered():
    for t in ("asset_graph_updated", "asset_conflict", "service_health",
              "correlation_finding", "incident_case_updated", "drift_finding",
              "situation_snapshot", "runbook_plan", "runbook_execution",
              "verification_outcome"):
        assert t in EVENT_TYPES


# ── redaction ─────────────────────────────────────────────────────────────────
def test_redaction_strips_secrets_and_bounds_text():
    finding = _finding_dict()
    finding["explanation"] = {"reason": "token=sk-SECRETVALUE1234567890 " + "A" * 500}
    ev = correlation_finding_event(finding)
    # the raw secret is redacted and the field is length-bounded
    assert "sk-SECRETVALUE1234567890" not in ev["explanation"]
    assert len(ev["explanation"]) <= 200


def test_forbidden_keys_never_leak():
    from core.ops_views import _scrub
    scrubbed = _scrub({"command_line": "powershell -enc AAAA", "token": "abc",
                       "safe": "ok", "pcap": "....", "value": "192.168.1.1"})
    assert "command_line" not in scrubbed
    assert "token" not in scrubbed
    assert "pcap" not in scrubbed
    assert scrubbed["safe"] == "ok"
    assert scrubbed["value"] == "192.168.1.1"


# ── asset summary ─────────────────────────────────────────────────────────────
def test_asset_status_panel():
    g = AssetGraph()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    g.observe_service(AssetType.VM, "ubuntu-server", port=22, exposure="authorized_subnet",
                      source=ObservationSource.SERVICE_OBSERVATION, now_iso=T0)
    panel = asset_status_panel(g)
    assert panel["panel"] == "asset_status"
    assert panel["known"] >= 1
    assert any(s["port"] == 22 for s in panel["exposed_services"])
    json.dumps(panel)


# ── incident summary ──────────────────────────────────────────────────────────
def test_incident_case_event_and_panel():
    case = IncidentCase(incident_id="inc1", title="beacon on host",
                        status=IncidentStatus.INVESTIGATING, severity=IncidentSeverity.HIGH)
    case.add_hypothesis("lateral movement")
    case.add_question("was it C2?")
    ev = incident_case_event(case)
    assert ev["type"] == "incident_case_updated"
    assert ev["status"] == "investigating"
    assert ev["hypotheses"] == 1
    assert ev["open_questions"] == 1
    panel = incidents_panel([case])
    assert panel["open"] == 1
    json.dumps(panel)


def test_closed_cases_excluded_from_incident_panel():
    case = IncidentCase(incident_id="inc1", status=IncidentStatus.CLOSED)
    assert incidents_panel([case])["open"] == 0


# ── drift summary ─────────────────────────────────────────────────────────────
def test_drift_panel():
    t = DigitalTwin()
    t.set_expected("ubuntu-server", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    t.observe("ubuntu-server", "service:ssh", "absent", kind=FactKind.SERVICE, now_iso=T0)
    snap = t.compute_drift(now_iso=T0)
    panel = drift_panel(snap)
    assert panel["panel"] == "drift"
    assert panel["count"] == 1
    ev = panel["findings"][0]
    assert ev["type"] == "drift_finding"
    assert ev["drift_type"] == "service_missing"
    assert ev["verification_required"] is True
    json.dumps(panel)


def test_drift_finding_event_direct():
    ev = drift_finding_event({"asset": "h", "drift_type": "sensor_coverage_drift",
                              "severity": "high", "recommended_investigation": "X",
                              "confidence": 0.5, "verification_required": True})
    assert ev["severity"] == "high"


# ── situation summary ─────────────────────────────────────────────────────────
def test_situation_event_and_panel():
    snap = SituationEngine().build(correlation_findings=[_finding_dict()], now_iso=T0)
    ev = situation_event(snap)
    assert ev["type"] == "situation_snapshot"
    assert ev["recommended_next_step"]
    panel = situation_panel(snap)
    assert panel["panel"] == "current_situation"
    assert len(panel["priorities"]) >= 1
    json.dumps(panel)


# ── sensor health + runbooks + correlations panels ────────────────────────────
def test_sensor_health_panel():
    panel = sensor_health_panel({"sysmon": "active", "zeek": "disconnected"})
    assert panel["degraded"] == 1
    assert panel["total"] == 2
    json.dumps(panel)


def test_runbooks_panel():
    panel = runbooks_panel(RunbookEngine())
    assert "SERVICE_DIAGNOSIS" in panel["available"]
    json.dumps(panel)


def test_correlations_panel_bounded():
    findings = [_finding_dict() for _ in range(50)]
    panel = correlations_panel(findings)
    assert panel["count"] == 50
    assert len(panel["recent"]) <= 12          # bounded payload
    json.dumps(panel)


# ── combined SYSTEM STATUS ────────────────────────────────────────────────────
def test_system_status_panel_bounded_and_serializable():
    g = AssetGraph()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    snap = SituationEngine().build(asset_graph=g, correlation_findings=[_finding_dict()],
                                   now_iso=T0)
    panel = system_status_panel(graph=g, open_cases=[], sensors={"sysmon": "active"},
                                findings=[_finding_dict()], situation=snap)
    assert panel["panel"] == "system_status"
    assert panel["assets"]["known"] == 1
    assert panel["sensors"]["total"] == 1
    assert panel["latest_correlation"]["type"] == "correlation_finding"
    # whole panel is WebSocket-safe
    json.dumps(panel)


def test_build_live_system_status_smoke():
    # the live-singleton assembler runs without a HUD and returns a bounded dict
    from core.ops_views import build_live_system_status
    panel = build_live_system_status(sensors={"sysmon": "active"})
    assert panel["panel"] == "system_status"
    json.dumps(panel)
