"""
tests/test_pcap_capture.py — V57.0 NEXUS hardening tests for the PCAP
forensics orchestrator.

Cross-platform / Windows-safe: no real packet capture, no raw sockets, no admin.
Validates command building (shell=False vectors), Windows interface names,
cooldown bounding, tool-missing no-op, and dormant-by-default behaviour.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest
from core.pcap_capture import PCAPCaptureOrchestrator, _IFACE_RE

_PCAP_ENV = [
    "JARVIS_PCAP_ENABLED", "JARVIS_PCAP_DRY_RUN", "JARVIS_PCAP_INTERFACE",
    "JARVIS_PCAP_TOOL", "JARVIS_PCAP_OUTPUT_DIR", "JARVIS_PCAP_DURATION",
]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _PCAP_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture
def orc():
    return PCAPCaptureOrchestrator()


# ── Enablement ────────────────────────────────────────────────────────────────

class TestEnablement:
    def test_dormant_by_default(self, clean_env, orc):
        assert orc.is_enabled() is False
        r = asyncio.run(orc.capture_for_alert({"severity": 9.9}))
        assert r["status"] == "disabled"

    def test_enabled_flag(self, clean_env, orc):
        clean_env.setenv("JARVIS_PCAP_ENABLED", "true")
        assert orc.is_enabled() is True


# ── Interface validation (Windows-safe names) ─────────────────────────────────

class TestInterface:
    @pytest.mark.parametrize("name", ["Ethernet", "Wi-Fi", "eth0", "wlan0", "ens3"])
    def test_valid_interface_names(self, name):
        assert _IFACE_RE.match(name)

    def test_rejects_injection(self):
        assert not _IFACE_RE.match("eth0; rm -rf /")
        assert not _IFACE_RE.match("eth0 || calc.exe")

    def test_explicit_interface_env(self, clean_env, orc):
        clean_env.setenv("JARVIS_PCAP_INTERFACE", "Wi-Fi")
        assert orc.detect_interface() == "Wi-Fi"

    def test_explicit_bad_interface_rejected(self, clean_env, orc):
        clean_env.setenv("JARVIS_PCAP_INTERFACE", "Wi-Fi; shutdown")
        assert orc.detect_interface() is None


# ── build_command (shell=False vectors) ───────────────────────────────────────

class TestBuildCommand:
    def test_tshark_command_windows_iface(self, clean_env, orc):
        cmd = orc.build_command("Wi-Fi", Path("out.pcap"), 60, {})
        assert cmd[0] == "tshark"
        assert "-i" in cmd and "Wi-Fi" in cmd
        # duration must be passed as a discrete arg, never interpolated to a shell
        assert "duration:60" in cmd

    def test_tcpdump_command(self, clean_env, orc):
        clean_env.setenv("JARVIS_PCAP_TOOL", "tcpdump")
        cmd = orc.build_command("Ethernet", Path("out.pcap"), 30, {})
        assert cmd[0] == "tcpdump"
        assert "Ethernet" in cmd

    def test_ip_filter_validated(self, clean_env, orc):
        cmd = orc.build_command("Ethernet", Path("o.pcap"), 60,
                                {"src_ip": "10.0.0.5"})
        assert "host 10.0.0.5" in cmd

    def test_malicious_ip_ignored(self, clean_env, orc):
        cmd = orc.build_command("Ethernet", Path("o.pcap"), 60,
                                {"src_ip": "10.0.0.5; rm -rf /"})
        # invalid IP is silently dropped from the filter, never injected
        assert not any("rm -rf" in part for part in cmd)

    def test_bad_interface_raises(self, clean_env, orc):
        with pytest.raises(ValueError):
            orc.build_command("eth0; shutdown", Path("o.pcap"), 60, {})

    def test_bad_duration_raises(self, clean_env, orc):
        with pytest.raises(ValueError):
            orc.build_command("eth0", Path("o.pcap"), 99999, {})


# ── Cooldown bounding ─────────────────────────────────────────────────────────

class TestCooldown:
    def test_cooldown_dict_is_bounded(self, clean_env, orc):
        import time
        now = time.monotonic()
        # Flood far beyond the cap with fresh (non-expired) entries
        for i in range(orc._MAX_COOLDOWN_ENTRIES + 200):
            orc._cooldown[f"key-{i}"] = now
        orc._prune_cooldown(now)
        assert len(orc._cooldown) <= orc._MAX_COOLDOWN_ENTRIES

    def test_expired_entries_pruned(self, clean_env, orc):
        import time
        now = time.monotonic()
        orc._cooldown["old"] = now - (orc._cooldown_secs() + 10)
        orc._cooldown["fresh"] = now
        orc._prune_cooldown(now)
        assert "old" not in orc._cooldown
        assert "fresh" in orc._cooldown


# ── Tool-missing / dry-run no-op ──────────────────────────────────────────────

class TestRunSafety:
    def test_dry_run_does_not_spawn(self, clean_env, orc):
        clean_env.setenv("JARVIS_PCAP_ENABLED", "true")
        clean_env.setenv("JARVIS_PCAP_DRY_RUN", "true")
        clean_env.setenv("JARVIS_PCAP_INTERFACE", "Ethernet")

        async def _go():
            return await orc.capture_for_alert({"incident_id": "X1", "severity": 9.0})

        r = asyncio.run(_go())
        assert r["status"] == "dry_run"
        assert r["cmd"][0] in ("tshark", "tcpdump")

    def test_missing_tool_noops(self, clean_env, orc, monkeypatch):
        clean_env.setenv("JARVIS_PCAP_ENABLED", "true")
        clean_env.setenv("JARVIS_PCAP_INTERFACE", "Ethernet")
        clean_env.setenv("JARVIS_PCAP_TOOL", "tshark")
        # Force the tool to appear absent regardless of the host
        monkeypatch.setattr("core.pcap_capture.shutil.which", lambda _: None)

        async def _go():
            return await orc.capture_for_alert({"incident_id": "X2", "severity": 9.0})

        r = asyncio.run(_go())
        assert r["status"] == "tool_missing"
