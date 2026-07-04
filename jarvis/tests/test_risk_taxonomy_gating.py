"""
tests/test_risk_taxonomy_gating.py — V62.0 Phase 7: proves the risk-class
taxonomy actually drives ToolExecutor.aexecute()/aexecute_mcp(), not just a
decorative parallel classification nobody consults.
"""
from __future__ import annotations

import asyncio

import tools.executor as ex_mod
from core.ironman_mode import SessionConsent
from core.risk_classes import RiskClass, classify_tool
from tools.executor import ToolExecutor


def _executor_with_challenge(monkeypatch, granted: bool):
    te = ToolExecutor(consent=SessionConsent(screen=True, clipboard=True))
    calls: list[str] = []

    async def _fake_challenge(tool_name, preview):
        calls.append(tool_name)
        return (granted, "test:granted" if granted else "test:denied")

    monkeypatch.setattr(te, "_challenge", _fake_challenge)
    return te, calls


def _capture_broadcasts(monkeypatch):
    events: list[dict] = []

    async def _fake_broadcast(event):
        events.append(event)

    monkeypatch.setattr(ex_mod, "_aura_broadcast", _fake_broadcast)
    return events


# ── aexecute(): READ_ONLY/LOW_IMPACT never challenge ──────────────────────────

def test_read_only_tool_skips_challenge_and_auth_pending_broadcast(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    events = _capture_broadcasts(monkeypatch)

    result = asyncio.run(te.aexecute("get_datetime", {}))

    assert "error" not in result
    assert not calls, "READ_ONLY must never trigger a HITL challenge"
    assert not any(e.get("type") == "tool_auth_pending" for e in events)


def test_low_impact_tool_skips_challenge(monkeypatch, tmp_path):
    monkeypatch.setattr(ex_mod, "__file__", str(tmp_path / "tools" / "executor.py"))
    te, calls = _executor_with_challenge(monkeypatch, granted=True)

    result = te.execute("save_note", {"title": "t", "content": "c"})

    assert "error" not in result
    assert classify_tool("save_note") is RiskClass.LOW_IMPACT


# ── aexecute(): REVERSIBLE/HIGH_IMPACT always challenge, with the right
#    ToolAuthPendingEvent shape ───────────────────────────────────────────────

def test_high_impact_tool_triggers_challenge_and_auth_pending_broadcast(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    events = _capture_broadcasts(monkeypatch)
    monkeypatch.setattr("psutil.process_iter", lambda *a, **k: iter([]))

    asyncio.run(te.aexecute("kill_process", {"name": "definitely_not_a_real_process"}))

    assert calls == ["kill_process"]
    pending = [e for e in events if e.get("type") == "tool_auth_pending"]
    assert len(pending) == 1
    assert pending[0]["tool"] == "kill_process"
    assert pending[0]["risk"] == "high_impact"
    assert pending[0]["rollback_hint"] is None  # only REVERSIBLE gets a hint


def test_reversible_tool_auth_pending_carries_rollback_hint(monkeypatch, tmp_path):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    events = _capture_broadcasts(monkeypatch)

    asyncio.run(te.aexecute("packet_tracer_open", {"file_path": ""}))

    assert calls == ["packet_tracer_open"]
    pending = [e for e in events if e.get("type") == "tool_auth_pending"]
    assert len(pending) == 1
    assert pending[0]["risk"] == "reversible"
    assert pending[0]["rollback_hint"]


def test_denied_challenge_blocks_high_impact_tool(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=False)

    result = asyncio.run(te.aexecute("kill_process", {"name": "x"}))

    assert "error" in result
    assert calls == ["kill_process"]


# ── LAB_ONLY: refused outright without JARVIS_TRUSTED_LAB, even before HITL ──

def test_lab_only_tool_refused_without_trusted_lab(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    monkeypatch.setattr(
        ex_mod, "classify_tool",
        lambda name: RiskClass.LAB_ONLY if name == "get_datetime" else classify_tool(name),
    )
    monkeypatch.setattr(ex_mod, "_trusted_lab_enabled", lambda: False)

    result = asyncio.run(te.aexecute("get_datetime", {}))

    assert "error" in result
    assert "LAB_ONLY" in result["error"]
    assert not calls, "a LAB_ONLY refusal must happen before any HITL challenge is issued"


def test_lab_only_tool_proceeds_to_hitl_when_trusted_lab_enabled(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    monkeypatch.setattr(
        ex_mod, "classify_tool",
        lambda name: RiskClass.LAB_ONLY if name == "get_datetime" else classify_tool(name),
    )
    monkeypatch.setattr(ex_mod, "_trusted_lab_enabled", lambda: True)

    result = asyncio.run(te.aexecute("get_datetime", {}))

    assert "error" not in result
    assert calls == ["get_datetime"], "LAB_ONLY still requires HITL even when trusted-lab is on"


# ── aexecute_mcp(): same taxonomy, same gateway ───────────────────────────────

def test_mcp_reversible_tool_gets_auth_pending_with_rollback_hint(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    events = _capture_broadcasts(monkeypatch)

    async def _call_fn(name, args):
        return {"result": "ok"}

    result = asyncio.run(te.aexecute_mcp("abrir_packet_tracer", {}, _call_fn))

    assert "error" not in result
    assert calls == ["mcp:abrir_packet_tracer"]
    pending = [e for e in events if e.get("type") == "tool_auth_pending"]
    assert len(pending) == 1
    assert pending[0]["tool"] == "mcp:abrir_packet_tracer"
    assert pending[0]["risk"] == "reversible"
    assert pending[0]["rollback_hint"]


def test_mcp_lab_only_tool_refused_without_trusted_lab(monkeypatch):
    te, calls = _executor_with_challenge(monkeypatch, granted=True)
    monkeypatch.setattr(
        ex_mod, "classify_tool",
        lambda name: RiskClass.LAB_ONLY if name == "abrir_packet_tracer" else classify_tool(name),
    )
    monkeypatch.setattr(ex_mod, "_trusted_lab_enabled", lambda: False)

    async def _call_fn(name, args):
        return {"result": "should not run"}

    result = asyncio.run(te.aexecute_mcp("abrir_packet_tracer", {}, _call_fn))

    assert "error" in result
    assert "LAB_ONLY" in result["error"]
    assert not calls


def test_mcp_unclassified_tool_would_default_high_impact():
    """Documents the fail-closed guarantee for MCP tools specifically: an
    allowlisted-but-unclassified MCP tool still requires HITL by default."""
    assert classify_tool("some_future_mcp_tool") is RiskClass.HIGH_IMPACT
