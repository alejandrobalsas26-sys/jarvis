"""
tests/test_grc_auditor.py — V57.0 NEXUS hardening tests for the GRC auditor.

Cross-platform / Windows-safe: writes only under pytest's tmp_path, no admin.
Validates HTML escaping of dynamic fields and the SHA256 integrity manifest.
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest
from core.grc_auditor import GRCAuditor


@pytest.fixture
def auditor():
    return GRCAuditor()


# ── HTML escaping ─────────────────────────────────────────────────────────────

class TestHtmlEscaping:
    def test_malicious_host_and_tactic_escaped(self, auditor):
        alert = {
            "incident_id":      "AB12",
            "severity_score":   9.6,
            "status":           "ACTIVE",
            "kill_chain_phase": "<img src=x onerror=alert(1)>",
            "involved_hosts":   ["<script>alert('xss')</script>"],
            "mitre_techniques": ["T1041"],
            "first_seen":       "2026-06-10T00:00:00+00:00",
            "last_seen":        "2026-06-10T00:01:00+00:00",
        }
        summary = auditor.map_to_controls([alert])
        html_out = auditor.render_html(summary)

        # Raw injection payloads must not survive into the HTML
        assert "<script>alert('xss')</script>" not in html_out
        assert "<img src=x onerror=alert(1)>" not in html_out
        # Escaped forms must be present instead
        assert "&lt;script&gt;" in html_out
        assert "&lt;img src=x" in html_out

    def test_degraded_report_renders(self, auditor):
        summary = auditor.map_to_controls([])
        html_out = auditor.render_html(summary)
        assert "No telemetry available" in html_out
        assert "<html" in html_out.lower()


# ── SHA256 manifest ───────────────────────────────────────────────────────────

class TestManifest:
    def test_manifest_written_with_sha256(self, auditor, tmp_path, monkeypatch):
        report_path = tmp_path / "grc_report.html"
        monkeypatch.setenv("JARVIS_GRC_REPORT_PATH", str(report_path))

        html_doc = "<html><body>report</body></html>"
        md_doc   = "# report"

        result = asyncio.run(auditor.write_immutable_report(html_doc, md_doc))
        assert "error" not in result

        manifest = tmp_path / "grc_reports" / "manifest.jsonl"
        assert manifest.exists(), "manifest.jsonl must be created"

        lines = manifest.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "generated_at" in entry
        assert "html" in entry["files"]
        assert "md" in entry["files"]

        # The recorded digest must match the archived HTML file on disk
        archive_html = Path(result["archive_html"])
        expected = hashlib.sha256(archive_html.read_bytes()).hexdigest()
        assert entry["files"]["html"]["sha256"] == expected

    def test_manifest_appends(self, auditor, tmp_path, monkeypatch):
        report_path = tmp_path / "grc_report.html"
        monkeypatch.setenv("JARVIS_GRC_REPORT_PATH", str(report_path))

        asyncio.run(auditor.write_immutable_report("<html>1</html>", "# 1"))
        asyncio.run(auditor.write_immutable_report("<html>2</html>", "# 2"))

        manifest = tmp_path / "grc_reports" / "manifest.jsonl"
        lines = manifest.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for ln in lines:
            json.loads(ln)  # each line is independently valid JSON
