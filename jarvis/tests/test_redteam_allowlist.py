"""
tests/test_redteam_allowlist.py — Phase 2A / F3: RedTeamShellExecutor allowlist.

RedTeamShellExecutor must route through the same strict allowlist validation as
run_shell_command. Off-allowlist binaries are refused BEFORE any trust scoring,
so no trust score (however high) and no denylist gap can authorize an unlisted
binary. Lab-only binaries are permitted only via the explicit lab allowlist when
trusted-lab mode is on.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from tools.executor import (
    COMMAND_ALLOWLIST,
    RedTeamShellExecutor,
    ToolExecutor,
    _LAB_COMMAND_ALLOWLIST,
    _validate_command,
)


# ───────────────────────── _validate_command (shared pipeline) ──────────────

@pytest.mark.parametrize("command", [
    "rm -rf /home",
    "rm  -rf  /",                    # double-space variant the 5-pattern denylist misses
    "mkfs.ext4 /dev/sda",
    "shred -n 5 /dev/sda",
    "dd if=/dev/urandom of=/dev/sda",
    "powershell -enc AAAA",
    "sqlmap -u http://x",            # lab-only, but lab not enabled here
])
def test_dangerous_and_off_allowlist_refused(command):
    ok, err, argv = _validate_command(command)
    assert ok is False
    assert argv == []


def test_allowlisted_readonly_still_validates():
    # Fixed AURA templates (run_nmap/run_whois) and other allowlisted read-only
    # commands must keep validating cleanly.
    for cmd in ("whois example.com", "nmap -sV --top-ports 100 1.2.3.4", "ping 1.2.3.4"):
        ok, err, argv = _validate_command(cmd)
        assert ok is True, err
        assert argv[0].endswith(cmd.split()[0])


def test_lab_allowlist_is_not_default_allow():
    # Without the lab allowlist, a lab binary is refused …
    ok_no, _, _ = _validate_command("masscan 1.2.3.4")
    assert ok_no is False
    # … WITH it (trusted-lab), only that explicit binary is permitted …
    ok_yes, _, _ = _validate_command("masscan 1.2.3.4", _LAB_COMMAND_ALLOWLIST)
    assert ok_yes is True
    # … but trusted-lab still does NOT allow arbitrary destructive binaries.
    ok_rm, _, _ = _validate_command("rm -rf /home", _LAB_COMMAND_ALLOWLIST)
    assert ok_rm is False


def test_lab_binaries_absent_from_base_allowlist():
    assert _LAB_COMMAND_ALLOWLIST.isdisjoint(COMMAND_ALLOWLIST)


# ───────────────────── execute_shell refuses even at max trust ──────────────

def _maxed_redteam(monkeypatch):
    """A RedTeamShellExecutor whose trust profile is fully saturated, with the
    YARA scan stubbed out so no yara-python dependency is required."""
    fake_yara = types.ModuleType("core.yara_analyzer")

    async def _scan(_argv):
        return []

    fake_yara.scan_command = _scan
    monkeypatch.setitem(sys.modules, "core.yara_analyzer", fake_yara)

    rt = RedTeamShellExecutor(ToolExecutor())
    rt._trust_profile = {"binaries": {
        b: {"count": 30, "last_used": time.time()}
        for b in ("rm", "mkfs.ext4", "shred", "dd")
    }}
    rt._profile_loaded = True
    return rt


@pytest.mark.parametrize("command", [
    "rm  -rf  /",                    # double-space variant (denylist miss) — gate blocks
    "mkfs.ext4 /dev/sda",
    "shred -n 5 /dev/sda",
    "dd if=/dev/urandom of=/dev/sda",
])
def test_execute_shell_refuses_off_allowlist_at_max_trust(monkeypatch, command):
    rt = _maxed_redteam(monkeypatch)
    result = asyncio.run(rt.execute_shell(command))
    assert result["authorized"] is False
    assert result["error"] and "BLOCK" in result["error"].upper()
