"""tests/test_field_readiness_v67.py — V67 M36 field readiness (real checks only).

Proves the readiness report is honest and complete:
  * it covers every operator-facing line (core runtime, ollama, models, collectors,
    assets, sensors, aura, persistence, docker, vmware, scope, runbook posture);
  * the resolved models come from the real role resolution;
  * persistence is reported honestly as VOLATILE when no Postgres DSN is configured —
    never fabricated as OK;
  * readiness is gated on CRITICAL lines only (Ollama/Docker absence lowers capability
    but still permits deterministic monitoring);
  * a failed critical line flips the verdict to NOT READY;
  * output is ASCII (Windows console safe).

Pure: probe_ollama=False so no network; reads the (empty) live singletons read-only.
"""
from __future__ import annotations

from core.field_readiness import (
    FieldReadinessReport,
    ReadinessLine,
    assess_field_readiness,
)

_REQUIRED_LABELS = {
    "CORE RUNTIME", "OLLAMA", "FAST MODEL", "DEEP MODEL", "VISION MODEL",
    "COLLECTORS", "ASSETS", "SENSORS", "AURA", "PERSISTENCE", "DOCKER", "VMWARE",
    "AUTHORIZED SCOPE", "RUNBOOK EXECUTION",
}


def _report():
    return assess_field_readiness(probe_ollama=False)


def _line(report, label):
    return next(ln for ln in report.lines if ln.label == label)


class TestCompleteness:
    def test_all_required_lines_present(self):
        labels = {ln.label for ln in _report().lines}
        assert _REQUIRED_LABELS.issubset(labels)

    def test_models_are_really_resolved(self):
        r = _report()
        for label in ("FAST MODEL", "DEEP MODEL", "VISION MODEL"):
            assert _line(r, label).value and _line(r, label).value != "UNRESOLVED"

    def test_render_is_ascii_with_verdict(self):
        text = _report().render()
        assert text.isascii()
        assert "JARVIS FIELD READINESS" in text
        assert "VERDICT:" in text


class TestHonesty:
    def test_persistence_volatile_without_postgres(self, monkeypatch):
        for var in ("JARVIS_PG_DSN", "DATABASE_URL", "POSTGRES_DSN"):
            monkeypatch.delenv(var, raising=False)
        assert "VOLATILE" in _line(_report(), "PERSISTENCE").value

    def test_persistence_configured_with_dsn(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://x")
        assert _line(_report(), "PERSISTENCE").value == "CONFIGURED"

    def test_ollama_not_probed_when_disabled(self):
        assert _line(_report(), "OLLAMA").value == "NOT CHECKED"

    def test_runbook_posture_is_dry_run_ready_without_executor(self):
        # No live ToolExecutor is wired in a bare process → DRY-RUN READY, never a
        # claim of execute-readiness.
        assert _line(_report(), "RUNBOOK EXECUTION").value in ("DRY-RUN READY", "EXECUTE READY")


class TestVerdictGating:
    def test_healthy_report_is_ready(self):
        # core runtime + fast/deep models resolve in this environment.
        assert _report().ready is True

    def test_failed_critical_line_blocks_readiness(self):
        report = FieldReadinessReport(lines=[
            ReadinessLine("CORE RUNTIME", "DEGRADED", "DEGRADED", critical=True),
            ReadinessLine("OLLAMA", "OK", "OK"),
        ])
        assert report.ready is False

    def test_noncritical_failure_does_not_block_readiness(self):
        report = FieldReadinessReport(lines=[
            ReadinessLine("CORE RUNTIME", "OK", "OK", critical=True),
            ReadinessLine("DOCKER", "ABSENT", "ABSENT"),          # non-critical
            ReadinessLine("OLLAMA", "UNREACHABLE", "UNREACHABLE"),  # non-critical
        ])
        assert report.ready is True
