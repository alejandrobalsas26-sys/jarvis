"""
tests/test_mcp_gateway.py — MCP tool-gateway hardening.

core/llm.py used to route MCP-bridge tool calls straight to
mcp_session.call_tool(), completely bypassing tools/executor.py's security
gate: no allowlist, no path-traversal guard, no HITL/NATO challenge. The
external packet_tracer_bridge.py's generar_laboratorio_red(nombre_archivo)
did an unsanitized os.path.join + open(path, "w") — an arbitrary-file-write
primitive reachable purely by the model choosing to call the tool.

These tests prove ToolExecutor.aexecute_mcp() closes that gap: unknown MCP
tool names are refused, path-traversal/absolute-path filenames are refused
BEFORE call_fn ever runs, a denied HITL challenge blocks execution, and a
granted challenge for an allowlisted tool with a safe filename actually
invokes call_fn.
"""
from __future__ import annotations

import asyncio

import pytest

from tools.executor import (
    ToolExecutor,
    MCP_TOOL_ALLOWLIST,
    _validate_mcp_filename,
)


def _executor_with_challenge(monkeypatch, granted: bool):
    te = ToolExecutor()
    calls: list[str] = []

    async def _fake_challenge(tool_name, preview):
        calls.append(tool_name)
        return (granted, "test:granted" if granted else "test:denied")

    monkeypatch.setattr(te, "_challenge", _fake_challenge)
    return te, calls


@pytest.mark.parametrize("payload", [
    "../../etc/passwd",
    "..\\..\\Windows\\System32\\evil.dll",
    "C:\\Windows\\System32\\evil.dll",
    "/etc/passwd",
    "..",
    ".",
    "",
])
def test_validate_mcp_filename_rejects_traversal(payload):
    assert _validate_mcp_filename(payload) is not None


def test_validate_mcp_filename_accepts_bare_name():
    assert _validate_mcp_filename("laboratorio_ia.pkt") is None


def test_unknown_mcp_tool_is_refused(monkeypatch):
    te, _ = _executor_with_challenge(monkeypatch, granted=True)
    calls = []

    async def _call_fn(name, args):
        calls.append(name)
        return {"result": "should not run"}

    result = asyncio.run(te.aexecute_mcp("not_a_real_mcp_tool", {}, _call_fn))
    assert "error" in result
    assert not calls, "call_fn must never run for a non-allowlisted MCP tool"


@pytest.mark.parametrize("payload", [
    "../../etc/passwd",
    "..\\..\\Windows\\System32\\evil.dll",
    "C:\\Windows\\System32\\evil.dll",
])
def test_path_traversal_never_reaches_call_fn(monkeypatch, payload):
    assert "generar_laboratorio_red" in MCP_TOOL_ALLOWLIST
    te, challenge_calls = _executor_with_challenge(monkeypatch, granted=True)
    call_fn_calls = []

    async def _call_fn(name, args):
        call_fn_calls.append((name, args))
        return {"result": "should not run"}

    result = asyncio.run(te.aexecute_mcp(
        "generar_laboratorio_red",
        {"xml_content": "<xml/>", "nombre_archivo": payload},
        _call_fn,
    ))
    assert "error" in result
    assert not call_fn_calls, "path-traversal filename must be rejected before call_fn runs"
    assert not challenge_calls, "traversal is rejected before the HITL gate is even reached"


def test_denied_hitl_challenge_blocks_mcp_execution(monkeypatch):
    te, challenge_calls = _executor_with_challenge(monkeypatch, granted=False)
    call_fn_calls = []

    async def _call_fn(name, args):
        call_fn_calls.append((name, args))
        return {"result": "should not run"}

    result = asyncio.run(te.aexecute_mcp(
        "generar_laboratorio_red",
        {"xml_content": "<xml/>", "nombre_archivo": "safe.pkt"},
        _call_fn,
    ))
    assert "error" in result
    assert not call_fn_calls, "denied HITL challenge must block the MCP call"
    assert challenge_calls == ["mcp:generar_laboratorio_red"]


def test_granted_hitl_challenge_allows_allowlisted_mcp_tool(monkeypatch):
    te, challenge_calls = _executor_with_challenge(monkeypatch, granted=True)
    call_fn_calls = []

    async def _call_fn(name, args):
        call_fn_calls.append((name, args))
        return {"result": f"generated {args['nombre_archivo']}"}

    result = asyncio.run(te.aexecute_mcp(
        "generar_laboratorio_red",
        {"xml_content": "<xml/>", "nombre_archivo": "safe.pkt"},
        _call_fn,
    ))
    assert "error" not in result
    assert call_fn_calls == [("generar_laboratorio_red", {"xml_content": "<xml/>", "nombre_archivo": "safe.pkt"})]
    assert challenge_calls == ["mcp:generar_laboratorio_red"]


def test_call_fn_exception_is_reported_not_raised(monkeypatch):
    te, _ = _executor_with_challenge(monkeypatch, granted=True)

    async def _call_fn(name, args):
        raise RuntimeError("bridge crashed")

    result = asyncio.run(te.aexecute_mcp(
        "abrir_packet_tracer", {}, _call_fn,
    ))
    assert "error" in result
    assert "bridge crashed" in result["error"]


def test_force_override_is_stripped_before_mcp_dispatch(monkeypatch):
    te, _ = _executor_with_challenge(monkeypatch, granted=True)
    seen_args = {}

    async def _call_fn(name, args):
        seen_args.update(args)
        return {"result": "ok"}

    asyncio.run(te.aexecute_mcp(
        "abrir_packet_tracer", {"FORCE_OVERRIDE": True}, _call_fn,
    ))
    assert "FORCE_OVERRIDE" not in seen_args
