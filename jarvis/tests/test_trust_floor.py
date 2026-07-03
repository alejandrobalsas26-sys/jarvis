"""
tests/test_trust_floor.py — Phase 1B / F5: high-risk challenge floor in trust_engine.

Verifies that learned trust can de-escalate ONLY genuinely read-only actions to
ChallengeLevel.NONE. Execution- / state-changing actions keep at least a CONFIRM
gate no matter how high the trust score climbs, so repeated successful use never
silently removes approval for a dangerous binary.
"""
from __future__ import annotations

import string
import time

from core.trust_engine import (
    ChallengeLevel,
    calculate_trust_score,
    get_challenge_level,
    is_high_risk_action,
)


def _fully_trusted_profile(binary: str) -> dict:
    """A profile that yields the maximum trust score (freq saturated, fresh)."""
    return {"binaries": {binary: {"count": 30, "last_used": time.time()}}}


# ───────────────────────────── is_high_risk_action ──────────────────────────

def test_read_only_binaries_are_low_risk():
    for b in ("whois", "dig", "nslookup", "cat", "ls", "whoami", "hostname"):
        assert is_high_risk_action(b, f"{b} something") is False


def test_execution_binaries_are_high_risk():
    for b in ("rm", "del", "nmap", "curl", "wget", "powershell", "python", "bash", "mkfs"):
        assert is_high_risk_action(b, f"{b} something") is True


def test_high_risk_normalizes_path_and_exe():
    assert is_high_risk_action(r"C:\Windows\System32\WHOIS.EXE", "whois x") is False
    assert is_high_risk_action("/usr/bin/RM", "rm -rf x") is True


# ───────────────────────── high-risk challenge floor ────────────────────────

def test_high_risk_never_none_even_at_max_trust():
    profile = _fully_trusted_profile("rm")
    score = calculate_trust_score("rm", "rm -rf /home/x", profile, ["rm"])
    assert score >= 0.80                         # trust is genuinely maxed out …
    level, lvl_score = get_challenge_level(
        "rm", "rm -rf /home/x", "known", [], profile, ["rm"],
    )
    assert level is not ChallengeLevel.NONE      # … yet it never auto-approves
    assert level is ChallengeLevel.CONFIRM
    assert lvl_score >= 0.80                      # scoring itself is preserved


def test_low_risk_read_only_may_reach_none_at_max_trust():
    profile = _fully_trusted_profile("whois")
    level, score = get_challenge_level(
        "whois", "whois example.com", "known", [], profile, ["whois"],
    )
    assert score >= 0.80
    assert level is ChallengeLevel.NONE          # read-only still benefits from trust


# ─────────────────────────── anomaly / entropy clamp ────────────────────────

def _high_entropy_command() -> str:
    return "exec " + string.ascii_letters + string.digits + "+/=@#%&*"


def test_calculate_trust_score_entropy_clamp():
    profile = _fully_trusted_profile("whois")
    assert calculate_trust_score("whois", _high_entropy_command(), profile, ["whois"]) == 0.0


def test_entropy_clamp_still_forces_full_nato():
    profile = _fully_trusted_profile("whois")
    level, score = get_challenge_level(
        "whois", _high_entropy_command(), "known", [], profile, ["whois"],
    )
    assert level is ChallengeLevel.FULL_NATO
    assert score == 0.0


def test_hard_rules_unaffected():
    profile = _fully_trusted_profile("ls")
    lvl1, s1 = get_challenge_level("ls", "ls", "unlisted_escalation", [], profile, ["ls"])
    lvl2, s2 = get_challenge_level("ls", "ls", "known", ["hit"], profile, ["ls"])
    assert lvl1 is ChallengeLevel.FULL_NATO and s1 == 0.0
    assert lvl2 is ChallengeLevel.FULL_NATO and s2 == 0.0
