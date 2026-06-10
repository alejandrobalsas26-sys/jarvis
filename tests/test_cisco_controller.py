"""
tests/test_cisco_controller.py — V57.0 NEXUS hardening tests for the Cisco
bare-metal containment controller.

Cross-platform / Windows-safe: no real SSH, no hardware, no admin privileges.
Async methods are driven via asyncio.run() so no pytest-asyncio config is needed.
Every test exercises the dry-run / dormant safety paths only.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest
from core.cisco_controller import (
    CiscoController,
    _normalize_mac,
    _validate_ip,
    _IFACE_RE,
)

# ── Env keys this module manipulates ──────────────────────────────────────────
_HW_ENV = [
    "JARVIS_HW_SSH_URL", "JARVIS_HW_USERNAME", "JARVIS_HW_PASSWORD",
    "JARVIS_HW_ENABLE", "JARVIS_HW_DRY_RUN", "JARVIS_HW_PERSIST_CONFIG",
    "JARVIS_HW_INTERFACE", "JARVIS_HW_DEVICE_TYPE", "JARVIS_HW_ENABLE_SECRET",
]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _HW_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture
def ctrl():
    return CiscoController()


def _creds(mp):
    """Set the minimum creds so is_enabled() is True."""
    mp.setenv("JARVIS_HW_SSH_URL", "10.0.0.1")
    mp.setenv("JARVIS_HW_USERNAME", "admin")
    mp.setenv("JARVIS_HW_PASSWORD", "secret")


# ── Validators ────────────────────────────────────────────────────────────────

class TestValidators:
    def test_normalize_mac_formats(self):
        assert _normalize_mac("aa:bb:cc:dd:ee:ff") == "aabb.ccdd.eeff"
        assert _normalize_mac("AABBCCDDEEFF") == "aabb.ccdd.eeff"
        assert _normalize_mac("aabb.ccdd.eeff") == "aabb.ccdd.eeff"

    def test_normalize_mac_invalid(self):
        with pytest.raises(ValueError):
            _normalize_mac("zz:zz:zz:zz:zz:zz")
        with pytest.raises(ValueError):
            _normalize_mac("aabb.ccdd")

    def test_validate_ip(self):
        assert _validate_ip("192.168.1.10") == "192.168.1.10"
        with pytest.raises(ValueError):
            _validate_ip("999.1.1.1")
        with pytest.raises(ValueError):
            _validate_ip("1.1.1.1; rm -rf /")

    def test_iface_regex_accepts_cisco_names(self):
        assert _IFACE_RE.match("GigabitEthernet0/0")
        assert _IFACE_RE.match("Fa0/1")
        assert not _IFACE_RE.match("Gi0/0; shutdown")
        assert not _IFACE_RE.match("")


# ── Dormancy / enablement gating ──────────────────────────────────────────────

class TestEnablement:
    def test_dormant_without_creds(self, clean_env, ctrl):
        assert ctrl.is_enabled() is False

    def test_enabled_with_creds(self, clean_env, ctrl):
        _creds(clean_env)
        assert ctrl.is_enabled() is True

    def test_dry_run_default_true_without_hw_enable(self, clean_env, ctrl):
        _creds(clean_env)
        # JARVIS_HW_ENABLE unset → always dry-run, even if DRY_RUN=false
        clean_env.setenv("JARVIS_HW_DRY_RUN", "false")
        assert ctrl._dry_run() is True

    def test_dry_run_true_when_enabled_but_not_disabled(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_ENABLE", "true")
        assert ctrl._dry_run() is True

    def test_dry_run_false_only_when_explicit(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_ENABLE", "true")
        clean_env.setenv("JARVIS_HW_DRY_RUN", "false")
        assert ctrl._dry_run() is False

    def test_persist_config_default_off(self, clean_env, ctrl):
        assert ctrl._persist_config() is False
        clean_env.setenv("JARVIS_HW_PERSIST_CONFIG", "true")
        assert ctrl._persist_config() is True


# ── blackhole_mac (dry-run only) ──────────────────────────────────────────────

class TestBlackholeMac:
    def test_dormant_returns_dormant(self, clean_env, ctrl):
        r = asyncio.run(ctrl.blackhole_mac("aa:bb:cc:dd:ee:ff"))
        assert r["status"] == "dormant"

    def test_dry_run_no_write_memory_by_default(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_DEVICE_TYPE", "cisco_2960")
        r = asyncio.run(ctrl.blackhole_mac("aa:bb:cc:dd:ee:ff"))
        assert r["status"] == "dry_run"
        assert "write memory" not in r["commands"]

    def test_dry_run_write_memory_when_persist(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_PERSIST_CONFIG", "true")
        r = asyncio.run(ctrl.blackhole_mac("aa:bb:cc:dd:ee:ff"))
        assert r["status"] == "dry_run"
        assert "write memory" in r["commands"]

    def test_invalid_mac_rejected(self, clean_env, ctrl):
        _creds(clean_env)
        r = asyncio.run(ctrl.blackhole_mac("not-a-mac"))
        assert r["status"] == "error"


# ── inject_drop_acl (dry-run only) ────────────────────────────────────────────

class TestInjectDropAcl:
    def test_requires_interface(self, clean_env, ctrl):
        _creds(clean_env)
        # JARVIS_HW_INTERFACE unset → error, no command built
        r = asyncio.run(ctrl.inject_drop_acl("1.2.3.4", None))
        assert r["status"] == "error"
        assert "INTERFACE" in r["reason"].upper()

    def test_invalid_interface_rejected(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_INTERFACE", "Gi0/0; shutdown")
        r = asyncio.run(ctrl.inject_drop_acl("1.2.3.4", None))
        assert r["status"] == "error"

    def test_dry_run_no_write_memory_by_default(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_INTERFACE", "GigabitEthernet0/0")
        r = asyncio.run(ctrl.inject_drop_acl("1.2.3.4", None))
        assert r["status"] == "dry_run"
        assert "write memory" not in r["commands"]
        assert "interface GigabitEthernet0/0" in r["commands"]

    def test_dry_run_write_memory_when_persist(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_INTERFACE", "GigabitEthernet0/0")
        clean_env.setenv("JARVIS_HW_PERSIST_CONFIG", "1")
        r = asyncio.run(ctrl.inject_drop_acl("1.2.3.4", "5.6.7.8"))
        assert r["status"] == "dry_run"
        assert "write memory" in r["commands"]

    def test_invalid_ip_rejected(self, clean_env, ctrl):
        _creds(clean_env)
        clean_env.setenv("JARVIS_HW_INTERFACE", "GigabitEthernet0/0")
        r = asyncio.run(ctrl.inject_drop_acl("999.999.0.1", None))
        assert r["status"] == "error"


# ── contain_alert dispatcher ──────────────────────────────────────────────────

class TestContainAlert:
    def test_dormant_when_unconfigured(self, clean_env, ctrl):
        r = asyncio.run(ctrl.contain_alert({"severity": 9.9, "mac": "aa:bb:cc:dd:ee:ff"}))
        assert r["status"] == "dormant"

    def test_low_severity_skipped(self, clean_env, ctrl):
        _creds(clean_env)
        r = asyncio.run(ctrl.contain_alert({"severity": 5.0}))
        assert r["status"] == "skip"
