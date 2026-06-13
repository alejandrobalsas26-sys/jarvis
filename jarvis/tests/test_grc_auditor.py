"""
tests/test_grc_auditor.py — V57.0 NEXUS: GRCAuditor unit tests.

Tests cover:
  - Alert-to-NIST-CSF mapping correctness.
  - Panama Ley 81 data-protection detection.
  - HTML and Markdown rendering (presence of required sections).
  - Graceful handling of empty alert history.
  - Immutable report writing to disk.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


from core.grc_auditor import GRCAuditor, _fmt_duration, _build_recommendations


# ── Shared test fixtures ──────────────────────────────────────────────────────

def _make_alert(
    incident_id: str = "TEST001",
    sev: float = 8.0,
    techniques: list[str] | None = None,
    status: str = "ACTIVE",
    etype: str = "test_event",
    phase: str = "Unknown",
    hosts: list[str] | None = None,
) -> dict:
    return {
        "incident_id":      incident_id,
        "severity_score":   sev,
        "mitre_techniques": techniques or [],
        "kill_chain_phase": phase,
        "involved_hosts":   hosts or [],
        "status":           status,
        "type":             etype,
        "first_seen":       "2024-01-01T00:00:00+00:00",
        "last_seen":        "2024-01-01T00:01:00+00:00",
    }


# ── NIST CSF mapping ──────────────────────────────────────────────────────────

class TestNistMapping:
    def test_c2_technique_maps_to_respond(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=["T1071", "T1048"], etype="c2_beacon")]
        summary = auditor.map_to_controls(alerts)
        assert len(summary["nist_csf"]["RS"]["events"]) > 0, (
            "T1071/T1048 must map to NIST RS (Respond)"
        )

    def test_injection_technique_maps_to_detect(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=["T1059", "T1055"], etype="injection")]
        summary = auditor.map_to_controls(alerts)
        assert len(summary["nist_csf"]["DE"]["events"]) > 0, (
            "T1059/T1055 must map to NIST DE (Detect)"
        )

    def test_persistence_maps_to_protect(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=["T1547", "T1053"])]
        summary = auditor.map_to_controls(alerts)
        assert len(summary["nist_csf"]["PR"]["events"]) > 0

    def test_exfil_maps_to_respond(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=["T1041"], etype="exfil_detected")]
        summary = auditor.map_to_controls(alerts)
        assert len(summary["nist_csf"]["RS"]["events"]) > 0

    def test_all_nist_functions_present_in_summary(self):
        auditor  = GRCAuditor()
        summary  = auditor.map_to_controls([])
        for func in ("ID", "PR", "DE", "RS", "RC"):
            assert func in summary["nist_csf"], f"Missing NIST function {func}"


# ── Panama Ley 81 ─────────────────────────────────────────────────────────────

class TestPanamaLey81:
    def test_exfil_technique_flags_ley81(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=["T1041"], etype="test")]
        summary = auditor.map_to_controls(alerts)
        assert summary["panama_ley81_hits"] > 0

    def test_exfil_event_type_flags_ley81(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=[], etype="data_exfil_detected")]
        summary = auditor.map_to_controls(alerts)
        assert summary["panama_ley81_hits"] > 0

    def test_benign_event_no_ley81(self):
        auditor = GRCAuditor()
        alerts = [_make_alert(techniques=["T1046"], etype="port_scan")]
        summary = auditor.map_to_controls(alerts)
        assert summary["panama_ley81_hits"] == 0


# ── Management metrics ────────────────────────────────────────────────────────

class TestMetrics:
    def test_critical_alert_counted(self):
        auditor = GRCAuditor()
        alerts  = [
            _make_alert("A", sev=9.5),
            _make_alert("B", sev=7.5),
            _make_alert("C", sev=5.0),
        ]
        summary = auditor.map_to_controls(alerts)
        assert summary["critical_alerts"] == 1
        assert summary["high_alerts"] == 1
        assert summary["total_alerts"] == 3

    def test_resolved_increments_containment_success(self):
        auditor = GRCAuditor()
        alerts  = [
            _make_alert("A", status="RESOLVED"),
            _make_alert("B", status="ACTIVE"),
        ]
        summary = auditor.map_to_controls(alerts)
        assert summary["containment_success"] >= 1
        assert summary["containment_failure"] >= 1

    def test_empty_alerts_is_graceful(self):
        auditor = GRCAuditor()
        summary = auditor.map_to_controls([])
        assert summary["total_alerts"] == 0
        assert summary["data_available"] is False
        assert summary["recommendations"]  # must still have at least one rec

    def test_affected_hosts_collected(self):
        auditor = GRCAuditor()
        alerts  = [_make_alert("A", hosts=["10.0.0.1", "10.0.0.2"])]
        summary = auditor.map_to_controls(alerts)
        assert "10.0.0.1" in summary["affected_hosts"]
        assert "10.0.0.2" in summary["affected_hosts"]

    def test_mttd_computed_from_timestamps(self):
        auditor = GRCAuditor()
        alert   = {
            **_make_alert(),
            "first_seen": "2024-01-01T00:00:00+00:00",
            "last_seen":  "2024-01-01T00:01:00+00:00",
        }
        summary = auditor.map_to_controls([alert])
        assert summary["mttd_seconds"] == 60.0


# ── Report rendering ──────────────────────────────────────────────────────────

class TestRendering:
    def test_html_contains_required_sections(self):
        auditor = GRCAuditor()
        html    = auditor.render_html(auditor.map_to_controls([]))
        assert "<!DOCTYPE html>" in html
        assert "JARVIS GRC Compliance Report" in html
        assert "NIST CSF" in html
        assert "Recommendations" in html

    def test_html_reports_no_telemetry_gracefully(self):
        auditor = GRCAuditor()
        html    = auditor.render_html(auditor.map_to_controls([]))
        assert "No telemetry available" in html

    def test_markdown_contains_required_sections(self):
        auditor = GRCAuditor()
        md      = auditor.render_markdown(auditor.map_to_controls([]))
        assert "# JARVIS GRC Compliance Report" in md
        assert "NIST CSF" in md
        assert "Recommendations" in md

    def test_html_with_critical_alerts_shows_badge(self):
        auditor = GRCAuditor()
        alerts  = [_make_alert(sev=9.5)]
        html    = auditor.render_html(auditor.map_to_controls(alerts))
        assert "badge" in html

    def test_render_includes_panama_ley81(self):
        auditor  = GRCAuditor()
        alerts   = [_make_alert(techniques=["T1041"])]
        summary  = auditor.map_to_controls(alerts)
        md       = auditor.render_markdown(summary)
        assert "Panama" in md or "Ley 81" in md or "Ley81" in md

    def test_write_immutable_report_creates_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_GRC_ENABLED", "1")
        monkeypatch.setenv(
            "JARVIS_GRC_REPORT_PATH", str(tmp_path / "grc_report.html")
        )
        auditor = GRCAuditor()
        summary = auditor.map_to_controls([])
        html    = auditor.render_html(summary)
        md      = auditor.render_markdown(summary)
        result  = asyncio.run(auditor.write_immutable_report(html, md))
        assert "current" in result and "error" not in result
        assert Path(result["current"]).exists()
        assert Path(result["archive_html"]).exists()
        assert Path(result["archive_md"]).exists()


# ── Helpers ───────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_fmt_seconds(self):
        assert "s" in _fmt_duration(45)

    def test_fmt_minutes(self):
        assert "m" in _fmt_duration(120)

    def test_fmt_hours(self):
        assert "h" in _fmt_duration(7200)

    def test_recommendations_non_empty_on_critical(self):
        nist = {k: {"events": []} for k in ("ID", "PR", "DE", "RS", "RC")}
        recs = _build_recommendations(nist, critical=2, high=0, ley81=0)
        assert any("CRITICAL" in r for r in recs)

    def test_recommendations_include_ley81_when_flagged(self):
        nist = {k: {"events": []} for k in ("ID", "PR", "DE", "RS", "RC")}
        recs = _build_recommendations(nist, critical=0, high=0, ley81=3)
        assert any("Ley 81" in r or "Ley81" in r or "Panama" in r for r in recs)
