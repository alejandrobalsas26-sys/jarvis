"""
tests/test_situation_engine.py — V66 M25 unified situation model.

Covers the required M25 surface: deterministic priority, confidence propagation,
uncertain-fact handling, bounded summary, recommended runbook, and no invented
facts.
"""
from __future__ import annotations

from core.asset_graph import AssetGraph, AssetType, ObservationSource
from core.digital_twin import DigitalTwin, FactKind
from core.situation_engine import SituationEngine, SituationSeverity

T0 = "2026-07-07T12:00:00+00:00"


def _finding(rule, severity, conf, group="host:win-client-01", fid="cf_1"):
    return {
        "finding_id": fid, "rule": rule, "severity": severity, "confidence": conf,
        "group_entity": group, "asset_refs": ["physical_host:win-client-01"],
        "matched_event_ids": ["oe_1", "oe_2"],
        "explanation": {"reason": "grouped 2 events"},
        "mitre_techniques": ["T1059"],
    }


def _incident(iid, severity="high", status="investigating", conf=0.7):
    return {
        "incident_id": iid, "title": f"incident {iid}", "severity": severity,
        "status": status, "confidence": conf,
        "affected_assets": ["physical_host:win-client-01"],
        "correlation_findings": ["cf_1"], "evidence": [{"x": 1}],
    }


def _eng() -> SituationEngine:
    return SituationEngine()


# ── deterministic priority ordering ───────────────────────────────────────────
def test_deterministic_priority_incident_first():
    snap = _eng().build(
        incidents=[_incident("I1", severity="high")],
        correlation_findings=[_finding("suspicious_process_then_network", "high", 0.7)],
        now_iso=T0)
    # same severity → incident outranks a correlation finding (deterministic weight)
    assert snap.priorities[0].kind == "incident"
    # rebuilding the same inputs yields the same ordering
    snap2 = _eng().build(
        incidents=[_incident("I1", severity="high")],
        correlation_findings=[_finding("suspicious_process_then_network", "high", 0.7)],
        now_iso=T0)
    assert [p.id for p in snap.priorities] == [p.id for p in snap2.priorities]


def test_critical_outranks_high():
    snap = _eng().build(correlation_findings=[
        _finding("high_severity_sequence", "high", 0.9, fid="cf_high"),
        _finding("same_ioc_multiple_assets", "critical", 0.6, fid="cf_crit"),
    ], now_iso=T0)
    assert snap.priorities[0].severity == "critical"


# ── recommended runbook ───────────────────────────────────────────────────────
def test_recommended_runbook_mapping():
    snap = _eng().build(correlation_findings=[
        _finding("auth_failures_then_success", "high", 0.8)], now_iso=T0)
    top = snap.priorities[0]
    assert top.recommended_runbook == "AUTH_FAILURE_TRIAGE"
    assert snap.recommendations[0].runbook == "AUTH_FAILURE_TRIAGE"
    assert snap.recommendations[0].mode == "dry_run"       # always dry run first
    assert snap.summary.recommended_next_step == "AUTH_FAILURE_TRIAGE"


# ── confidence propagation ────────────────────────────────────────────────────
def test_confidence_propagation():
    snap = _eng().build(correlation_findings=[
        _finding("suspicious_process_then_network", "high", 0.83)], now_iso=T0)
    assert snap.priorities[0].confidence == 0.83
    assert snap.summary.confidence == 0.83


# ── uncertain fact handling ───────────────────────────────────────────────────
def test_uncertain_low_confidence_flagged():
    snap = _eng().build(correlation_findings=[
        _finding("suspicious_process_then_network", "high", 0.3)], now_iso=T0)
    assert snap.priorities[0].uncertain is True
    assert snap.priorities[0].title in snap.uncertainties


def test_state_unknown_drift_is_uncertain_not_degraded():
    t = DigitalTwin()
    t.set_expected("ubuntu-server", "service:ssh", "active", kind=FactKind.SERVICE, now_iso=T0)
    drift = t.compute_drift(now_iso=T0)     # STATE_UNKNOWN (no observation)
    snap = _eng().build(drift=drift, now_iso=T0)
    # the unknown drift is surfaced but flagged uncertain, and does not degrade the asset
    assert any(p.uncertain for p in snap.priorities)
    assert snap.summary.degraded_assets == 0


# ── bounded summary ───────────────────────────────────────────────────────────
def test_bounded_priority_list():
    findings = [_finding("high_severity_sequence", "high", 0.7, fid=f"cf_{i}")
                for i in range(30)]
    snap = SituationEngine(max_priorities=5).build(correlation_findings=findings, now_iso=T0)
    assert len(snap.priorities) == 5        # bounded — never a full dump


# ── no invented facts ─────────────────────────────────────────────────────────
def test_no_invented_facts_when_empty():
    snap = _eng().build(now_iso=T0)
    assert snap.severity is SituationSeverity.CALM
    assert snap.priorities == ()
    assert snap.summary.known_assets == 0
    assert snap.summary.top_priority is None
    assert snap.recommendations == ()
    assert "nothing requires investigation" in snap.narrative().lower()


def test_asset_accounting_from_graph_only():
    g = AssetGraph()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    g.add_observation(AssetType.VM, "ubuntu-server", "ip", "192.168.56.10",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    # one asset is degraded via an incident referencing it
    inc = _incident("I1")
    inc["affected_assets"] = ["vm:kali-vm"]
    snap = _eng().build(asset_graph=g, incidents=[inc], now_iso=T0)
    assert snap.summary.known_assets == 2
    assert snap.summary.degraded_assets == 1
    assert snap.summary.healthy_assets == 1


def test_conflicting_asset_counts_as_unknown():
    g = AssetGraph()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.9, now_iso=T0)
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.21",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.8, now_iso=T0)
    snap = _eng().build(asset_graph=g, now_iso=T0)
    assert snap.summary.unknown_assets == 1


# ── sensor health raises a priority ───────────────────────────────────────────
def test_sensor_down_raises_priority():
    snap = _eng().build(sensor_health={"sysmon": "active", "zeek": "disconnected"}, now_iso=T0)
    assert any(p.kind == "sensor" and "zeek" in p.title for p in snap.priorities)


# ── closed incidents are ignored ──────────────────────────────────────────────
def test_closed_incident_not_counted():
    snap = _eng().build(incidents=[_incident("I1", status="closed")], now_iso=T0)
    assert snap.summary.open_incidents == 0
    assert snap.priorities == ()


# ── what-changed diff ─────────────────────────────────────────────────────────
def test_what_changed_diff():
    e = _eng()
    first = e.build(correlation_findings=[_finding("high_severity_sequence", "high", 0.7)],
                    now_iso=T0)
    assert first.what_changed["baseline"] is True
    second = e.build(
        correlation_findings=[
            _finding("high_severity_sequence", "high", 0.7),
            _finding("auth_failures_then_success", "high", 0.8, fid="cf_new")],
        previous=first, now_iso=T0)
    assert "finding:cf_new" in second.what_changed["new"]
    assert second.what_changed["baseline"] is False


# ── LLM grounding is facts-only ───────────────────────────────────────────────
def test_llm_grounding_is_facts_only():
    snap = _eng().build(correlation_findings=[
        _finding("suspicious_process_then_network", "high", 0.8)], now_iso=T0)
    grounding = _eng().llm_grounding(snap)
    assert "Do not invent" in grounding["instruction"]
    # grounding contains only snapshot-derived facts
    assert grounding["summary"] == snap.summary.to_dict()
    assert len(grounding["priorities"]) <= 5


# ── critical incident lifts overall severity ──────────────────────────────────
def test_critical_incident_lifts_situation_severity():
    snap = _eng().build(incidents=[_incident("I1", severity="critical")], now_iso=T0)
    assert snap.severity is SituationSeverity.CRITICAL
    assert snap.summary.critical_incidents == 1
