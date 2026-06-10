"""
tests/test_pcap_capture.py — V57.0 NEXUS: PCAPCaptureOrchestrator unit tests.

Tests cover:
  - No-op when disabled.
  - Correct tshark / tcpdump command structure.
  - Shell injection prevention (interface name and alert IP).
  - Cooldown prevents capture storms for the same alert.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from core.pcap_capture import PCAPCaptureOrchestrator


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _enabled_orc(monkeypatch, tmp_path: Path, *, dry: bool = True) -> PCAPCaptureOrchestrator:
    monkeypatch.setenv("JARVIS_PCAP_ENABLED", "1")
    monkeypatch.setenv("JARVIS_PCAP_INTERFACE", "eth0")
    monkeypatch.setenv("JARVIS_PCAP_OUTPUT_DIR", str(tmp_path))
    if dry:
        monkeypatch.setenv("JARVIS_PCAP_DRY_RUN", "true")
    else:
        monkeypatch.delenv("JARVIS_PCAP_DRY_RUN", raising=False)
    return PCAPCaptureOrchestrator()


# ── Enabled / disabled ────────────────────────────────────────────────────────

class TestEnabled:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PCAP_ENABLED", raising=False)
        assert PCAPCaptureOrchestrator().is_enabled() is False

    def test_enabled_via_env_1(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_ENABLED", "1")
        assert PCAPCaptureOrchestrator().is_enabled() is True

    def test_enabled_via_env_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_ENABLED", "true")
        assert PCAPCaptureOrchestrator().is_enabled() is True

    def test_capture_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PCAP_ENABLED", raising=False)
        result = _run(PCAPCaptureOrchestrator().capture_for_alert({"severity": 9.9}))
        assert result["status"] == "disabled"


# ── Command building ──────────────────────────────────────────────────────────

class TestCommandBuilding:
    def test_tshark_basic_structure(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tshark")
        orc = PCAPCaptureOrchestrator()
        cmd = orc.build_command("eth0", Path("/tmp/t.pcap"), 60, {})
        assert cmd[0] == "tshark"
        assert "eth0" in cmd
        assert str(Path("/tmp/t.pcap")) in cmd
        assert any("60" in c for c in cmd)

    def test_tcpdump_basic_structure(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tcpdump")
        orc = PCAPCaptureOrchestrator()
        cmd = orc.build_command("eth0", Path("/tmp/t.pcap"), 60, {})
        assert cmd[0] == "tcpdump"
        assert "eth0" in cmd

    def test_tshark_adds_ip_filter_when_valid(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tshark")
        orc = PCAPCaptureOrchestrator()
        cmd = orc.build_command(
            "eth0", Path("/tmp/t.pcap"), 60, {"src_ip": "192.168.1.100"}
        )
        assert "192.168.1.100" in " ".join(cmd)

    def test_tcpdump_adds_ip_filter_when_valid(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tcpdump")
        orc = PCAPCaptureOrchestrator()
        cmd = orc.build_command(
            "eth0", Path("/tmp/t.pcap"), 60, {"attacker_ip": "10.0.0.55"}
        )
        assert "10.0.0.55" in cmd

    def test_command_is_always_a_list(self, monkeypatch):
        for tool in ("tshark", "tcpdump"):
            monkeypatch.setenv("JARVIS_PCAP_TOOL", tool)
            orc = PCAPCaptureOrchestrator()
            cmd = orc.build_command("eth0", Path("/tmp/t.pcap"), 60, {})
            assert isinstance(cmd, list), "command MUST be a list; never a string"

    def test_duration_appears_in_command(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tshark")
        orc = PCAPCaptureOrchestrator()
        cmd = orc.build_command("eth0", Path("/tmp/t.pcap"), 45, {})
        assert any("45" in c for c in cmd)


# ── Shell injection prevention ────────────────────────────────────────────────

class TestInjectionPrevention:
    def test_rejects_interface_with_semicolon(self):
        orc = PCAPCaptureOrchestrator()
        with pytest.raises(ValueError):
            orc.build_command("eth0; rm -rf /", Path("/tmp/t.pcap"), 60, {})

    def test_rejects_interface_with_pipe(self):
        orc = PCAPCaptureOrchestrator()
        with pytest.raises(ValueError):
            orc.build_command("eth0|evil", Path("/tmp/t.pcap"), 60, {})

    def test_rejects_interface_with_space(self):
        orc = PCAPCaptureOrchestrator()
        with pytest.raises(ValueError):
            orc.build_command("eth0 eth1", Path("/tmp/t.pcap"), 60, {})

    def test_invalid_ip_in_alert_is_silently_dropped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tshark")
        orc = PCAPCaptureOrchestrator()
        # Malicious string — should be ignored, not injected
        cmd = orc.build_command(
            "eth0", Path("/tmp/t.pcap"), 60,
            {"src_ip": "'; rm -rf /'"}
        )
        joined = " ".join(cmd)
        assert "rm" not in joined
        assert "'" not in joined

    def test_non_ipv4_value_in_alert_is_dropped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_TOOL", "tshark")
        orc  = PCAPCaptureOrchestrator()
        cmd  = orc.build_command(
            "eth0", Path("/tmp/t.pcap"), 60,
            {"src_ip": "not.an.ip.address"}
        )
        # The malformed IP must not appear in the command
        assert "not.an.ip.address" not in " ".join(cmd)

    def test_dry_run_output_cmd_is_list(self, monkeypatch, tmp_path):
        orc = _enabled_orc(monkeypatch, tmp_path, dry=True)
        result = _run(orc.capture_for_alert({"type": "c2", "src_ip": "10.0.0.1"}))
        if "cmd" in result:
            assert isinstance(result["cmd"], list), (
                "Dry-run command must be a list, not a shell string"
            )

    def test_rejects_invalid_duration(self):
        orc = PCAPCaptureOrchestrator()
        with pytest.raises(ValueError):
            orc.build_command("eth0", Path("/tmp/t.pcap"), 99999, {})

    def test_rejects_zero_duration(self):
        orc = PCAPCaptureOrchestrator()
        with pytest.raises(ValueError):
            orc.build_command("eth0", Path("/tmp/t.pcap"), 0, {})


# ── Cooldown / dedup ──────────────────────────────────────────────────────────

class TestCooldown:
    def test_second_capture_for_same_alert_is_cooldown(self, monkeypatch, tmp_path):
        orc = _enabled_orc(monkeypatch, tmp_path)
        alert = {"type": "c2_beacon", "src_ip": "10.0.0.5"}
        r1 = _run(orc.capture_for_alert(alert))
        r2 = _run(orc.capture_for_alert(alert))
        assert r1["status"] in ("dry_run", "started")
        assert r2["status"] == "cooldown"

    def test_different_alert_keys_bypass_cooldown(self, monkeypatch, tmp_path):
        orc = _enabled_orc(monkeypatch, tmp_path)
        r1 = _run(orc.capture_for_alert({"type": "alert_A", "src_ip": "10.0.0.1"}))
        r2 = _run(orc.capture_for_alert({"type": "alert_B", "src_ip": "10.0.0.2"}))
        assert r1["status"] in ("dry_run", "started")
        assert r2["status"] in ("dry_run", "started"), (
            "Different alert keys must not share the same cooldown slot"
        )

    def test_expired_cooldown_allows_new_capture(self, monkeypatch, tmp_path):
        orc   = _enabled_orc(monkeypatch, tmp_path)
        alert = {"type": "c2_beacon", "src_ip": "10.0.0.9"}
        key   = orc._dedup_key(alert)
        # Force the cooldown to appear expired
        orc._cooldown[key] = time.monotonic() - orc._cooldown_secs() - 1
        result = _run(orc.capture_for_alert(alert))
        assert result["status"] in ("dry_run", "started", "no_interface")

    def test_dedup_key_is_stable(self):
        orc = PCAPCaptureOrchestrator()
        a1  = {"type": "scan", "src_ip": "10.0.0.1", "extra": "noise"}
        a2  = {"type": "scan", "src_ip": "10.0.0.1", "different_field": 42}
        assert orc._dedup_key(a1) == orc._dedup_key(a2)

    def test_dedup_key_differs_by_ip(self):
        orc = PCAPCaptureOrchestrator()
        assert orc._dedup_key({"src_ip": "10.0.0.1"}) != orc._dedup_key({"src_ip": "10.0.0.2"})


# ── Interface detection ───────────────────────────────────────────────────────

class TestInterfaceDetection:
    def test_explicit_env_var_takes_priority(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_INTERFACE", "lo0")
        result = PCAPCaptureOrchestrator().detect_interface()
        assert result == "lo0"

    def test_invalid_env_interface_returns_none(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PCAP_INTERFACE", "bad iface!")
        result = PCAPCaptureOrchestrator().detect_interface()
        assert result is None

    def test_disabled_returns_status(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PCAP_ENABLED", raising=False)
        orc    = PCAPCaptureOrchestrator()
        result = _run(orc.capture_for_alert({"type": "c2", "src_ip": "10.0.0.1"}))
        assert result["status"] == "disabled"
