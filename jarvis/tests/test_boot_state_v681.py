"""
tests/test_boot_state_v681.py — V68.1 M48 truthful startup narration.

Locks the contradictions the live run exhibited: boot narration must reflect the
real subsystem state — never fabricate Moondream/ETW/Sysmon/Telegram/"all
nominal". One snapshot; every consumer renders from it.
"""
from __future__ import annotations

from core.boot_state import (
    BootState,
    BootSubsystem,
    assemble_boot_state,
    OK, FAILED, DEGRADED, OPTIONAL, DORMANT,
)


def _report(results, failed=0, optional_missing=0):
    return {"results": results, "failed": failed, "optional_missing": optional_missing}


def _sub(id_, status, name=None, detail=""):
    return {"id": id_, "name": name or id_, "status": status, "detail": detail}


# ── VISION narration uses configured model, never Moondream ───────────────────

def test_vision_line_uses_configured_model_not_moondream():
    st = assemble_boot_state(
        _report([_sub("vision", OK)]),
        vision_model="gemma3:4b",
    )
    lines = dict(st.narration_lines())
    assert "gemma3:4b" in lines["vision"]
    assert "moondream" not in lines["vision"].lower()


def test_vision_degraded_when_not_loaded():
    st = assemble_boot_state(_report([_sub("vision", FAILED)]), vision_model="gemma3:4b")
    lines = dict(st.narration_lines())
    assert "degraded" in lines["vision"].lower()
    # Must not falsely claim the model is online/loaded.
    assert "online" not in lines["vision"].lower()
    assert "not loaded" in lines["vision"].lower()


# ── Detection reflects real ETW / Sysmon / canary states ──────────────────────

def test_detection_reports_etw_disabled_and_sysmon_dormant():
    st = assemble_boot_state(
        _report([_sub("etw", FAILED), _sub("canary", OK)]),
        etw_enabled=False,
        sysmon_active=False,
    )
    line = dict(st.narration_lines())["detection"]
    assert "ETW disabled" in line
    assert "Sysmon dormant" in line
    assert "canaries armed" in line
    # Never the old blanket "ETW, Sysmon, canaries armed" claim.
    assert "Sysmon active" not in line


def test_detection_reports_active_when_truly_active():
    st = assemble_boot_state(
        _report([_sub("etw", OK), _sub("canary", OK)]),
        etw_enabled=True,
        sysmon_active=True,
    )
    line = dict(st.narration_lines())["detection"]
    assert "ETW active" in line and "Sysmon active" in line


# ── Telegram disabled is never "established" ──────────────────────────────────

def test_telegram_disabled_not_established():
    st = assemble_boot_state(_report([_sub("telegram", OPTIONAL)]), telegram_configured=False)
    line = dict(st.narration_lines())["communication"]
    assert "disabled" in line.lower()
    assert "established" not in line.lower()


def test_telegram_established_only_when_configured():
    st = assemble_boot_state(
        _report([_sub("telegram", OK)]),
        telegram_configured=True,
    )
    line = dict(st.narration_lines())["communication"]
    assert "established" in line.lower()


# ── "All systems nominal" only when true ──────────────────────────────────────

def test_not_nominal_when_a_subsystem_failed():
    st = assemble_boot_state(_report([_sub("ollama", FAILED)], failed=1))
    assert st.all_systems_nominal() is False
    ready = dict(st.narration_lines())["ready"]
    assert "nominal" not in ready.lower()
    assert "reduced capability" in ready.lower()


def test_nominal_when_everything_ok():
    st = assemble_boot_state(
        _report([_sub("ollama", OK), _sub("chromadb", OK), _sub("correlator", OK)]),
        vision_model="gemma3:4b",
    )
    assert st.all_systems_nominal() is True
    ready = dict(st.narration_lines())["ready"]
    assert "nominal" in ready.lower()


def test_optional_dormant_alone_still_reports_and_is_honest():
    st = assemble_boot_state(
        _report([_sub("ollama", OK), _sub("docker", OPTIONAL)], optional_missing=1),
    )
    # Optional dormant does not FAIL nominal, but the count is surfaced honestly.
    ready = dict(st.narration_lines())["ready"]
    assert "optional" in ready.lower()


# ── PostgreSQL degradation is reported separately from local durability ───────

def test_postgres_unavailable_reports_degraded_persistence():
    st = assemble_boot_state(_report([]), postgres_available=False)
    line = dict(st.narration_lines())["persistence"]
    assert "degraded" in line.lower()
    assert "local" in line.lower()  # local durable store still reported


# ── Guardian / self-test agreement: same normalized host + tolerant timeout ───

def test_self_test_shares_tolerant_ollama_probe():
    import core.self_test as stmod
    # The probe timeout must be >= Guardian's 5s so they cannot disagree due to
    # a stricter self-test deadline under CPU model-load latency.
    assert stmod._OLLAMA_PROBE_TIMEOUT_S >= 5.0
    assert stmod._OLLAMA_PROBE_RETRIES >= 1


# ── Robustness: malformed / empty report degrades honestly ────────────────────

def test_empty_report_is_safe():
    st = assemble_boot_state(None)
    assert isinstance(st, BootState)
    # No crash; nominal is vacuously true (nothing failed) but persistence honest.
    lines = dict(st.narration_lines())
    assert "vision" in lines and "ready" in lines
