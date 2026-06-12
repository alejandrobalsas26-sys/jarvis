"""
tests/test_cisco_controller.py — V57.0 NEXUS: CiscoController unit tests.

Tests cover:
  - Dormant behaviour when env vars are absent.
  - DRY_RUN mode never invokes SSH.
  - Sev >= 9.5 produces the intended containment decision.
  - Input validation rejects malformed MAC/IP values.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import pytest
from unittest.mock import MagicMock

# ── Stub optional heavy deps so imports never fail ───────────────────────────

for _mod in ("asyncssh", "netmiko"):
    if _mod not in sys.modules:
        stub = types.ModuleType(_mod)
        sys.modules[_mod] = stub

if not hasattr(sys.modules["netmiko"], "ConnectHandler"):
    sys.modules["netmiko"].ConnectHandler = MagicMock()

from core.cisco_controller import (
    CiscoController,
    _normalize_mac,
    _parse_host,
    _validate_ip,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


# ── Dormant behaviour ─────────────────────────────────────────────────────────

class TestDormant:
    def test_disabled_without_env(self, monkeypatch):
        for k in ("JARVIS_HW_SSH_URL", "JARVIS_HW_USERNAME", "JARVIS_HW_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        assert CiscoController().is_enabled() is False

    def test_enabled_with_all_env_vars(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "192.168.1.1")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        assert CiscoController().is_enabled() is True

    def test_contain_alert_returns_dormant_when_disabled(self, monkeypatch):
        for k in ("JARVIS_HW_SSH_URL", "JARVIS_HW_USERNAME", "JARVIS_HW_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        result = _run(CiscoController().contain_alert({"severity_score": 9.9}))
        assert result["status"] == "dormant"

    def test_contain_alert_skips_below_threshold(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "192.168.1.1")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        result = _run(CiscoController().contain_alert({"severity_score": 9.4}))
        assert result["status"] == "skip"

    def test_blackhole_mac_dormant_when_disabled(self, monkeypatch):
        for k in ("JARVIS_HW_SSH_URL", "JARVIS_HW_USERNAME", "JARVIS_HW_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        result = _run(CiscoController().blackhole_mac("aa:bb:cc:dd:ee:ff"))
        assert result["status"] == "dormant"

    def test_inject_drop_acl_dormant_when_disabled(self, monkeypatch):
        for k in ("JARVIS_HW_SSH_URL", "JARVIS_HW_USERNAME", "JARVIS_HW_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        result = _run(CiscoController().inject_drop_acl("10.0.0.1", None))
        assert result["status"] == "dormant"


# ── DRY_RUN — no SSH must be invoked ─────────────────────────────────────────

class TestDryRun:
    def _enable(self, monkeypatch, device_type: str = "cisco_2960s"):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "invalid-host-xyz-nexus.test")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        monkeypatch.setenv("JARVIS_HW_DRY_RUN", "true")
        monkeypatch.setenv("JARVIS_BLACKHOLE_VLAN", "999")
        monkeypatch.setenv("JARVIS_HW_DEVICE_TYPE", device_type)
        monkeypatch.setenv("JARVIS_HW_INTERFACE", "GigabitEthernet0/0")
        monkeypatch.delenv("JARVIS_HW_PERSIST_CONFIG", raising=False)

    def test_dry_run_blackhole_mac_status(self, monkeypatch):
        self._enable(monkeypatch)
        result = _run(CiscoController().blackhole_mac("aa:bb:cc:dd:ee:ff", "test"))
        assert result["status"] == "dry_run"
        assert result["mac"] == "aabb.ccdd.eeff"
        assert result["vlan"] == "999"

    def test_dry_run_commands_are_allowlisted(self, monkeypatch):
        self._enable(monkeypatch)
        result = _run(CiscoController().blackhole_mac("aa:bb:cc:dd:ee:ff", "test"))
        cmds = result["commands"]
        assert isinstance(cmds, list)
        assert "configure terminal" in cmds
        # Containment is ephemeral by default: write memory only with
        # JARVIS_HW_PERSIST_CONFIG (so emergency rules don't survive reboot).
        assert "write memory" not in cmds
        # Verify MAC appears verbatim in one command
        assert any("aabb.ccdd.eeff" in c for c in cmds)

    def test_dry_run_persist_config_appends_write_memory(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setenv("JARVIS_HW_PERSIST_CONFIG", "true")
        result = _run(CiscoController().blackhole_mac("aa:bb:cc:dd:ee:ff", "test"))
        assert "write memory" in result["commands"]

    def test_dry_run_inject_acl_status(self, monkeypatch):
        self._enable(monkeypatch, device_type="cisco_1921")
        result = _run(CiscoController().inject_drop_acl("10.0.0.1", None, "test"))
        assert result["status"] == "dry_run"
        assert "JARVIS-DROP-" in result["acl"]

    def test_dry_run_does_not_reach_ssh_backend(self, monkeypatch):
        """DRY_RUN must return before any SSH connection attempt.
        We point at an unreachable host to confirm no network I/O occurs."""
        self._enable(monkeypatch)
        # If SSH were attempted, it would fail loudly with a connection error.
        # A successful dry_run status proves SSH was bypassed.
        result = _run(CiscoController().blackhole_mac("aa:bb:cc:dd:ee:ff", "no-ssh"))
        assert result["status"] == "dry_run", (
            "Expected dry_run; if SSH was attempted this would be 'error'"
        )

    def test_dry_run_acl_deny_lines_contain_validated_ip(self, monkeypatch):
        self._enable(monkeypatch, device_type="cisco_1921")
        result = _run(CiscoController().inject_drop_acl("172.16.0.50", None, "pen"))
        cmds = result["commands"]
        assert any("172.16.0.50" in c for c in cmds)


# ── Sev >= 9.5 produces containment decision ──────────────────────────────────

class TestSev95Containment:
    def test_sev_95_routes_to_acl_for_1921(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "invalid-host-xyz.test")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        monkeypatch.setenv("JARVIS_HW_DRY_RUN", "true")
        monkeypatch.setenv("JARVIS_HW_DEVICE_TYPE", "cisco_1921")
        monkeypatch.setenv("JARVIS_HW_INTERFACE", "GigabitEthernet0/0")
        alert = {"severity_score": 9.5, "src_ip": "10.0.0.55", "type": "c2_beacon"}
        result = _run(CiscoController().contain_alert(alert))
        assert result["status"] == "ok"
        assert len(result["actions"]) > 0
        assert result["actions"][0]["status"] == "dry_run"

    def test_sev_95_routes_to_blackhole_for_2960s(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "invalid-host-xyz.test")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        monkeypatch.setenv("JARVIS_HW_DRY_RUN", "true")
        monkeypatch.setenv("JARVIS_HW_DEVICE_TYPE", "cisco_2960s")
        monkeypatch.setenv("JARVIS_BLACKHOLE_VLAN", "999")
        alert = {
            "severity_score": 9.9,
            "mac": "de:ad:be:ef:ca:fe",
            "type": "lateral_movement",
        }
        result = _run(CiscoController().contain_alert(alert))
        assert result["status"] == "ok"
        assert result["actions"][0]["status"] == "dry_run"

    def test_sev_95_no_indicators_is_noop(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "192.168.1.1")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        monkeypatch.setenv("JARVIS_HW_DRY_RUN", "true")
        result = _run(CiscoController().contain_alert({"severity_score": 9.9}))
        assert result["status"] == "no_op"

    def test_sev_below_95_is_skipped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "192.168.1.1")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        monkeypatch.setenv("JARVIS_HW_DRY_RUN", "true")
        result = _run(CiscoController().contain_alert(
            {"severity_score": 9.4, "src_ip": "10.0.0.1"}
        ))
        assert result["status"] == "skip"


# ── Input validation ──────────────────────────────────────────────────────────

class TestInputValidation:
    def _enabled_dry(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HW_SSH_URL", "192.168.1.1")
        monkeypatch.setenv("JARVIS_HW_USERNAME", "admin")
        monkeypatch.setenv("JARVIS_HW_PASSWORD", "cisco")
        monkeypatch.setenv("JARVIS_HW_DRY_RUN", "true")

    def test_invalid_mac_is_rejected(self, monkeypatch):
        self._enabled_dry(monkeypatch)
        result = _run(CiscoController().blackhole_mac("not-a-valid-mac"))
        assert result["status"] == "error"

    def test_invalid_ip_src_is_rejected(self, monkeypatch):
        self._enabled_dry(monkeypatch)
        result = _run(CiscoController().inject_drop_acl("999.999.999.999", None))
        assert result["status"] == "error"

    def test_shell_injection_ip_is_rejected(self, monkeypatch):
        self._enabled_dry(monkeypatch)
        result = _run(CiscoController().inject_drop_acl("10.0.0.1; rm -rf /", None))
        assert result["status"] == "error"

    def test_no_ip_no_acl(self, monkeypatch):
        self._enabled_dry(monkeypatch)
        result = _run(CiscoController().inject_drop_acl(None, None))
        assert result["status"] == "no_op"

    def test_normalize_colon_mac(self):
        assert _normalize_mac("aa:bb:cc:dd:ee:ff") == "aabb.ccdd.eeff"

    def test_normalize_dash_mac(self):
        assert _normalize_mac("AA-BB-CC-DD-EE-FF") == "aabb.ccdd.eeff"

    def test_normalize_bare_hex(self):
        assert _normalize_mac("aabbccddeeff") == "aabb.ccdd.eeff"

    def test_normalize_cisco_dotted(self):
        assert _normalize_mac("aabb.ccdd.eeff") == "aabb.ccdd.eeff"

    def test_normalize_rejects_short(self):
        with pytest.raises(ValueError):
            _normalize_mac("aa:bb:cc")

    def test_validate_ip_accepts_valid(self):
        assert _validate_ip("192.168.1.1") == "192.168.1.1"

    def test_validate_ip_rejects_invalid(self):
        with pytest.raises(ValueError):
            _validate_ip("not.an.ip!")

    def test_parse_host_plain(self):
        assert _parse_host("192.168.1.1") == ("192.168.1.1", 22)

    def test_parse_host_with_port(self):
        assert _parse_host("192.168.1.1:830") == ("192.168.1.1", 830)

    def test_parse_host_strips_scheme(self):
        assert _parse_host("ssh://10.0.0.1") == ("10.0.0.1", 22)
