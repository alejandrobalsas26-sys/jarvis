"""
tests/test_authority.py — V63 operator authority + scope policy coverage.

Proves:
  * reasoning/non-target tools are NEVER scope-gated (reasoning freedom);
  * STANDARD posture with no scopes leaves target tools unchanged (no-op);
  * a scoped mode makes an out-of-scope action fail closed;
  * an in-scope action is allowed (then proceeds to normal risk/HITL);
  * expired scopes are rejected;
  * a missing/malformed target under active authority fails closed;
  * untrusted content in tool_input cannot widen authority;
  * subdomain / CIDR membership is exact (no substring bypass);
  * the ToolExecutor preflight refuses an out-of-scope target action end-to-end.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from core.authority import (
    AuthorityMode,
    AuthorityState,
    ScopePolicy,
    authorize_action,
    default_authority,
    parse_mode,
)


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


# ── reasoning freedom: non-target tools never gated ──────────────────────────
def test_non_target_tool_always_allowed():
    st = AuthorityState(mode=AuthorityMode.PURPLE_TEAM)  # strict posture, no scopes
    for tool in ("read_file", "query_knowledge", "decode_payload", "code_execute"):
        d = authorize_action(st, tool, {"anything": "x"})
        assert d.allowed is True
        assert d.requires_scope is False


# ── STANDARD posture: enforcement inactive (behavior preserved) ──────────────
def test_standard_no_scope_is_noop_for_target_tools():
    st = default_authority()
    d = authorize_action(st, "network_scan", {"target": "45.33.32.156"})
    assert d.allowed is True
    assert st.enforcement_active() is False


# ── scoped modes: fail-closed outside scope ───────────────────────────────────
def test_ctf_scope_allows_in_scope_and_refuses_out_of_scope():
    st = AuthorityState(mode=AuthorityMode.CTF)
    st.add_scope(ScopePolicy(
        scope_id="ctf1", name="HTB range", mode=AuthorityMode.CTF,
        cidrs=("10.10.10.0/24",), domains=("target.htb",), expires_at=_future(),
    ))
    assert authorize_action(st, "network_scan", {"target": "10.10.10.5"}).allowed is True
    assert authorize_action(st, "whois_lookup", {"domain": "target.htb"}).allowed is True
    assert authorize_action(st, "whois_lookup", {"domain": "sub.target.htb"}).allowed is True
    out = authorize_action(st, "network_scan", {"target": "8.8.8.8"})
    assert out.allowed is False
    assert out.in_scope is False


def test_scoped_mode_without_target_fails_closed():
    st = AuthorityState(mode=AuthorityMode.TRUSTED_LAB)
    st.add_scope(ScopePolicy(scope_id="lab", cidrs=("192.168.56.0/24",), expires_at=_future()))
    d = authorize_action(st, "network_scan", {"target": ""})
    assert d.allowed is False
    assert d.in_scope is False


def test_expired_scope_is_rejected():
    st = AuthorityState(mode=AuthorityMode.CTF)
    st.add_scope(ScopePolicy(scope_id="old", cidrs=("10.0.0.0/8",), expires_at=_past()))
    # scoped mode is active, but the only scope expired → nothing is in scope
    assert st.active_scopes() == []
    d = authorize_action(st, "network_scan", {"target": "10.1.2.3"})
    assert d.allowed is False


def test_configured_scope_activates_enforcement_even_in_standard_mode():
    st = AuthorityState(mode=AuthorityMode.STANDARD)
    st.add_scope(ScopePolicy(scope_id="s", cidrs=("172.16.0.0/12",), expires_at=_future()))
    assert st.enforcement_active() is True
    assert authorize_action(st, "network_scan", {"target": "172.16.5.5"}).allowed is True
    assert authorize_action(st, "network_scan", {"target": "1.1.1.1"}).allowed is False


# ── exact membership: no substring bypass ─────────────────────────────────────
def test_domain_membership_is_not_substring():
    st = AuthorityState(mode=AuthorityMode.PURPLE_TEAM)
    st.add_scope(ScopePolicy(scope_id="d", domains=("example.com",), expires_at=_future()))
    assert authorize_action(st, "whois_lookup", {"domain": "example.com"}).allowed is True
    assert authorize_action(st, "whois_lookup", {"domain": "app.example.com"}).allowed is True
    # look-alike must NOT match
    assert authorize_action(st, "whois_lookup", {"domain": "evil-example.com"}).allowed is False
    assert authorize_action(st, "whois_lookup", {"domain": "example.com.attacker.net"}).allowed is False


def test_http_request_url_host_is_scoped():
    st = AuthorityState(mode=AuthorityMode.TRUSTED_LAB)
    st.add_scope(ScopePolicy(scope_id="h", domains=("lab.local",), expires_at=_future()))
    assert authorize_action(st, "http_request", {"url": "http://api.lab.local/x"}).allowed is True
    assert authorize_action(st, "http_request", {"url": "http://evil.com/x"}).allowed is False


# ── untrusted content cannot widen authority ─────────────────────────────────
def test_tool_input_cannot_change_authority():
    st = AuthorityState(mode=AuthorityMode.CTF)
    st.add_scope(ScopePolicy(scope_id="c", cidrs=("10.10.10.0/24",), expires_at=_future()))
    # Malicious extra keys attempting to self-authorize must be ignored.
    d = authorize_action(st, "network_scan", {
        "target": "8.8.8.8",
        "authority_mode": "trusted_lab",
        "scope": "0.0.0.0/0",
        "in_scope": True,
        "FORCE_OVERRIDE": True,
    })
    assert d.allowed is False   # still out of scope; injected keys had no effect
    assert st.mode == AuthorityMode.CTF


def test_parse_mode_defaults_to_standard():
    assert parse_mode("garbage") == AuthorityMode.STANDARD
    assert parse_mode(None) == AuthorityMode.STANDARD
    assert parse_mode("purple_team") == AuthorityMode.PURPLE_TEAM


def test_set_mode_reports_change():
    st = default_authority()
    assert st.set_mode(AuthorityMode.CTF) is True
    assert st.set_mode(AuthorityMode.CTF) is False   # no-op re-set


# ── end-to-end: ToolExecutor preflight refuses out-of-scope ──────────────────
def test_executor_preflight_refuses_out_of_scope():
    from core.authority import AuthorityState, ScopePolicy
    from tools.executor import ToolExecutor

    auth = AuthorityState(mode=AuthorityMode.CTF)
    auth.add_scope(ScopePolicy(scope_id="ctf", cidrs=("10.10.10.0/24",), expires_at=_future()))
    ex = ToolExecutor(authority=auth)
    # 8.8.8.8 is outside the CTF scope → refused BEFORE any NATO challenge.
    out = asyncio.run(ex.aexecute("network_scan", {"target": "8.8.8.8"}))
    assert isinstance(out, dict) and "error" in out
    assert "alcance" in out["error"].lower() or "scope" in out["error"].lower()


def test_executor_default_posture_preserves_behavior():
    # With the default STANDARD posture and no scopes, the authority preflight is
    # a no-op — a scope-bound tool is NOT refused by authority (it proceeds to the
    # normal gate, which for network_scan is HITL — not exercised here).
    from tools.executor import ToolExecutor
    from core.authority import authorize_action
    ex = ToolExecutor()
    d = authorize_action(ex.authority, "network_scan", {"target": "8.8.8.8"})
    assert d.allowed is True
