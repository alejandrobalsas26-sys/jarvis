"""
tests/test_code_execute_gate.py — Phase 1B / F4: code_execute approval hardening.

Proves that code_execute (arbitrary Python execution) can never be HITL-exempt
and can never be auto-approved: the async execution gate always issues an
explicit HITL/NATO challenge for it — even if it were mistakenly added to the
exempt list — and a denied challenge blocks execution.
"""
from __future__ import annotations

import asyncio

from tools.executor import (
    ToolExecutor,
    _ALWAYS_HITL_TOOLS,
    _HITL_EXEMPT_TOOLS,
)


def test_code_execute_absent_from_hitl_exempt():
    assert "code_execute" not in _HITL_EXEMPT_TOOLS


def test_code_execute_in_always_hitl():
    assert "code_execute" in _ALWAYS_HITL_TOOLS


def test_always_hitl_disjoint_from_exempt():
    assert _ALWAYS_HITL_TOOLS.isdisjoint(_HITL_EXEMPT_TOOLS)


def _run_aexecute_with_denied_challenge(monkeypatch, tool_input, force_exempt=False):
    te = ToolExecutor()
    calls: list[str] = []

    async def _fake_challenge(tool_name, preview):
        calls.append(tool_name)
        return (False, "test:denied")

    monkeypatch.setattr(te, "_challenge", _fake_challenge)

    if force_exempt:
        # Even if a future edit wrongly exempts code_execute, the gate must still
        # challenge it because it is in _ALWAYS_HITL_TOOLS.
        import tools.executor as ex
        monkeypatch.setattr(ex, "_HITL_EXEMPT_TOOLS", _HITL_EXEMPT_TOOLS | {"code_execute"})

    result = asyncio.run(te.aexecute("code_execute", tool_input))
    return result, calls


def test_code_execute_requires_challenge_and_denial_blocks(monkeypatch):
    result, calls = _run_aexecute_with_denied_challenge(
        monkeypatch, {"code": "print('should not run')"},
    )
    assert calls == ["code_execute"]             # challenge WAS issued
    assert "error" in result                     # and denial blocked execution
    assert "cancel" in result["error"].lower()


def test_code_execute_challenged_even_if_wrongly_exempted(monkeypatch):
    result, calls = _run_aexecute_with_denied_challenge(
        monkeypatch, {"code": "print('nope')"}, force_exempt=True,
    )
    assert calls == ["code_execute"]             # _ALWAYS_HITL overrides exemption
    assert "error" in result
