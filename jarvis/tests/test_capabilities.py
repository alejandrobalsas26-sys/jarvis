"""
tests/test_capabilities.py — V63 typed security capability layer coverage.

Proves:
  * availability/version probes are honest (present tools detected, absent not);
  * safe argv construction validates input and rejects shell metacharacters;
  * inventory-only capabilities never execute (no fake wrappers);
  * uninstalled tools are refused (fail-closed);
  * a scope-bound capability is refused outside authorized scope;
  * structured parsing + artifact capture work;
  * execution goes through a validated shell=False argv (no shell string);
  * a real dns_lookup runs when nslookup is available (integration, guarded).
"""
from __future__ import annotations

import asyncio

import pytest

from core.authority import AuthorityMode, AuthorityState, ScopePolicy
from core.capabilities import (
    AvailabilityProbe,
    CapabilityInputError,
    CapabilityRegistry,
    StructuredResult,
    ToolCapability,
    build_default_registry,
    execute_capability,
    registry,
)
from core.risk_classes import RiskClass


def _future() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


# ── registry / probes ─────────────────────────────────────────────────────────
def test_default_registry_has_real_and_inventory_caps():
    dns = registry.get("dns_lookup")
    assert dns is not None and dns.executable is True
    nmap = registry.get("nmap")
    assert nmap is not None and nmap.executable is False  # inventory-only, no adapter


def test_inventory_reports_availability_honestly():
    inv = {c["name"]: c for c in registry.inventory()}
    # nslookup ships with Windows/most Unix — expect the dns_lookup adapter present
    assert "dns_lookup" in inv
    # every entry reports a boolean availability, never fakes it
    assert all(isinstance(c["available"], bool) for c in inv.values())


def test_availability_probe_absent_tool():
    assert AvailabilityProbe.available("definitely-not-a-real-binary-xyz") is False


# ── safe argv construction ────────────────────────────────────────────────────
def test_dns_argv_is_validated():
    argv = registry.build_argv("dns_lookup", {"name": "example.com", "type": "A"})
    assert argv[0] == "nslookup"
    assert "example.com" in argv


def test_dns_argv_rejects_shell_metacharacters():
    with pytest.raises(CapabilityInputError):
        registry.build_argv("dns_lookup", {"name": "example.com; rm -rf /"})
    with pytest.raises(CapabilityInputError):
        registry.build_argv("dns_lookup", {"name": "$(whoami).evil.com"})


def test_dns_argv_rejects_bad_record_type():
    with pytest.raises(CapabilityInputError):
        registry.build_argv("dns_lookup", {"name": "example.com", "type": "EVIL"})


def test_inventory_cap_has_no_argv_builder():
    with pytest.raises(CapabilityInputError):
        registry.build_argv("nmap", {"target": "10.0.0.1"})


# ── execution gating (no bypass) ──────────────────────────────────────────────
def test_execute_refuses_inventory_only():
    res = asyncio.run(execute_capability(registry, "nmap", {"target": "10.0.0.1"}))
    assert res.ok is False
    assert "inventory" in res.error.lower() or "adapter" in res.error.lower()


def test_execute_refuses_uninstalled_tool():
    reg = CapabilityRegistry()
    reg.register(ToolCapability(
        name="ghost", binary="nonexistent-binary-xyz",
        category=registry.get("dns_lookup").category, risk_class=RiskClass.READ_ONLY,
        build_argv=lambda p: ["nonexistent-binary-xyz"], parse=lambda o: [],
    ))
    res = asyncio.run(execute_capability(reg, "ghost", {}))
    assert res.ok is False
    assert "not installed" in res.error.lower()


def test_execute_uses_argv_not_shell():
    """The runner receives a list[str] argv — never a shell string."""
    captured = {}

    async def _fake_runner(argv, timeout):
        captured["argv"] = argv
        return 0, "Name:\tapp.lab.local\nAddress: 10.10.10.5\n", ""

    reg = build_default_registry()
    # Force availability so the adapter runs with our fake runner.
    AvailabilityProbe._cache["nslookup"] = "/usr/bin/nslookup"
    try:
        res = asyncio.run(execute_capability(
            reg, "dns_lookup", {"name": "app.lab.local"}, runner=_fake_runner))
    finally:
        AvailabilityProbe.clear()
    assert isinstance(captured["argv"], list)
    assert captured["argv"][0] == "nslookup"
    assert res.ok is True
    assert any(r["value"] == "10.10.10.5" for r in res.records)
    assert res.artifact is not None and res.artifact.sha256


# ── scope enforcement on a scope-bound capability ─────────────────────────────
def test_scope_bound_capability_refused_out_of_scope():
    async def _fake_runner(argv, timeout):
        return 0, "should not run", ""

    reg = build_default_registry()
    AvailabilityProbe._cache["nslookup"] = "/usr/bin/nslookup"
    auth = AuthorityState(mode=AuthorityMode.CTF)
    auth.add_scope(ScopePolicy(scope_id="c", domains=("target.htb",), expires_at=_future()))
    try:
        # out-of-scope target → refused before the runner is invoked
        res = asyncio.run(execute_capability(
            reg, "dns_lookup", {"name": "google.com"},
            runner=_fake_runner, authority=auth))
    finally:
        AvailabilityProbe.clear()
    assert res.ok is False
    assert "scope" in res.error.lower()


def test_scope_bound_capability_allowed_in_scope():
    async def _fake_runner(argv, timeout):
        return 0, "Name:\ttarget.htb\nAddress: 10.10.10.9\n", ""

    reg = build_default_registry()
    AvailabilityProbe._cache["nslookup"] = "/usr/bin/nslookup"
    auth = AuthorityState(mode=AuthorityMode.CTF)
    auth.add_scope(ScopePolicy(scope_id="c", domains=("target.htb",), expires_at=_future()))
    try:
        res = asyncio.run(execute_capability(
            reg, "dns_lookup", {"name": "target.htb"},
            runner=_fake_runner, authority=auth))
    finally:
        AvailabilityProbe.clear()
    assert res.ok is True


# ── executor integration ──────────────────────────────────────────────────────
def test_executor_run_capability_refuses_unknown():
    from tools.executor import ToolExecutor
    ex = ToolExecutor()
    out = asyncio.run(ex.run_capability("does-not-exist", {}))
    assert "error" in out


def test_executor_run_capability_refuses_inventory_only():
    from tools.executor import ToolExecutor
    ex = ToolExecutor()
    out = asyncio.run(ex.run_capability("nmap", {"target": "10.0.0.1"}))
    assert "error" in out


# ── real integration (guarded) ────────────────────────────────────────────────
@pytest.mark.skipif(not AvailabilityProbe.available("nslookup"),
                    reason="nslookup not installed")
def test_real_dns_lookup_runs():
    res = asyncio.run(execute_capability(registry, "dns_lookup",
                                         {"name": "localhost", "type": "A"}))
    assert isinstance(res, StructuredResult)
    # rc may be nonzero offline; the point is it executed via argv and produced a result
    assert res.argv[0] == "nslookup"
    assert res.artifact is not None
