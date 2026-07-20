"""
tests/test_dispatch_isolation_v69_m551.py — V69 M55.1 immediate turn dispatch.

Locks the two live-run failures this sub-milestone repairs:

  * the ~43s pre-FAST_ROUTE stall — every turn `await self._init_mcp()` at the top
    of `chat_stream`, so a DIRECT_FAST message sat behind the MCP stdio-bridge cold
    spawn. MCP now connects in its OWN supervised background task; DIRECT_FAST never
    waits, and a tool turn waits only inside its remaining budget (M55.1.1);
  * `Tú: ERROR:jarvis.db_manager:...` — stdlib `logging` bypassed the ConsoleCoordinator
    and hit logging's lastResort stderr handler. A root bridge now routes those
    records through the coordinator so they never share the input line (M55.1.2).

No live model and no live MCP bridge: the native transport is stubbed and `_init_mcp`
is replaced with a coroutine that stalls, standing in for the 60s cold spawn.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

import pytest

from core import ollama_native


# ── Helpers ───────────────────────────────────────────────────────────────────
class _FakeExec:
    """Minimal ToolExecutor stand-in — the fast path only reads `.authority`."""

    authority = None


def _make_llm():
    from core.llm import LLM

    llm = LLM(tool_executor=_FakeExec())
    llm.history = []  # ignore any persisted session so turns are deterministic
    return llm


class _MCPProbe:
    """Stands in for the ~43s cold MCP stdio-bridge spawn. Records whether it was
    started (kicked off in the background) and whether it ran to completion (which,
    if a fast turn ever awaited it, would prove the turn blocked on MCP)."""

    def __init__(self) -> None:
        self.started = False
        self.completed = False

    async def stall(self) -> None:
        self.started = True
        await asyncio.sleep(60)
        self.completed = True


async def _fake_native_chat_stream(**kwargs):
    """A tiny, instant native /api/chat stream: two content chunks then a done."""
    for piece in ("Para sacar la raíz cuadrada ", "eleva el número a 1/2."):
        yield ollama_native.ChatChunk(content=piece, done=False)
    yield ollama_native.ChatChunk(
        content="", done=True, done_reason="stop", eval_count=8,
        eval_duration=1_000_000_000,
    )


# ── M55.1.1 — DIRECT_FAST never waits for MCP ─────────────────────────────────
def test_start_mcp_background_is_nonblocking_and_schedules_task():
    async def _run():
        llm = _make_llm()
        probe = _MCPProbe()
        llm._init_mcp = probe.stall
        t0 = time.monotonic()
        llm.start_mcp_background()
        assert (time.monotonic() - t0) < 0.5      # returned immediately
        assert llm._mcp_task is not None
        await asyncio.sleep(0)                     # let the task actually start
        assert probe.started is True
        assert not llm._mcp_task.done()            # still cold-spawning
        await llm.aclose()                         # cancels the warming task
    asyncio.run(_run())


def test_ensure_mcp_is_bounded_when_bridge_is_cold():
    async def _run():
        llm = _make_llm()
        probe = _MCPProbe()
        llm._init_mcp = probe.stall
        llm.start_mcp_background()
        t0 = time.monotonic()
        connected = await llm._ensure_mcp(timeout=0.1)
        dt = time.monotonic() - t0
        assert connected is False                  # still cold
        assert dt < 2.0                            # bounded — did NOT wait 60s
        assert not llm._mcp_task.done()            # shielded: keeps warming
        await llm.aclose()
    asyncio.run(_run())


def test_direct_fast_turn_completes_without_waiting_for_cold_mcp(monkeypatch):
    """The headline acceptance test: MCP stalls 60s, the operator asks a simple math
    question, the native fast path answers, and MCP is still warming — proving the
    turn never blocked on the bridge. (Timing bound is generous so a cold first-turn
    semantic-model load in a fresh test process cannot make it flaky — the property
    under test is 'independent of the 60s MCP stall', not an absolute latency.)"""
    monkeypatch.setattr(ollama_native, "chat_stream", _fake_native_chat_stream)
    ollama_native.set_native_capability(ollama_native.NativeCapability(
        state=ollama_native.NativeProbeState.NATIVE_READY, model="qwen3:8b"))

    async def _run():
        llm = _make_llm()
        probe = _MCPProbe()
        llm._init_mcp = probe.stall
        llm.start_mcp_background()                  # MCP begins its 60s cold spawn
        await asyncio.sleep(0)                      # let the background task start (boot does)
        t0 = time.monotonic()
        out = []
        async for piece in llm.chat_stream("explicame como sacar la raiz al cuadrado"):
            out.append(piece)
        dt = time.monotonic() - t0
        text = "".join(out)
        assert "raíz cuadrada" in text             # actually answered
        assert dt < 30.0                           # nowhere near the 60s MCP stall
        assert probe.started is True               # MCP warmed in the background
        assert probe.completed is False            # never ran to completion => never blocked us
        assert llm._mcp_task is not None and not llm._mcp_task.done()
        await llm.aclose()

    try:
        asyncio.run(_run())
    finally:
        ollama_native.reset_native_capability()


def test_pre_inference_dispatch_under_ceiling_when_warm(monkeypatch):
    """Once the routing path is warm (as it is after boot in production), the second
    DIRECT_FAST turn's pre-inference dispatch (message-in -> transport selected) stays
    under the 1s hard ceiling. The first turn warms the semantic assembly; MCP stalls
    throughout to prove it is never on the measured path."""
    monkeypatch.setattr(ollama_native, "chat_stream", _fake_native_chat_stream)
    ollama_native.set_native_capability(ollama_native.NativeCapability(
        state=ollama_native.NativeProbeState.NATIVE_READY, model="qwen3:8b"))

    async def _run():
        llm = _make_llm()
        probe = _MCPProbe()
        llm._init_mcp = probe.stall
        llm.start_mcp_background()
        await asyncio.sleep(0)                      # let the background task start (boot does)
        # Turn 1 — warms classification + semantic task-decision assembly.
        async for _ in llm.chat_stream("hola"):
            pass
        warm_first = llm._last_dispatch_ms
        # Turn 2 — measured warm.
        async for _ in llm.chat_stream("explicame POO brevemente"):
            pass
        assert llm._last_dispatch_ms is not None
        assert warm_first is not None
        assert llm._last_dispatch_ms < 1000.0, (
            f"warm pre-inference dispatch {llm._last_dispatch_ms}ms exceeded 1s ceiling")
        assert probe.completed is False            # MCP never on the dispatch path
        await llm.aclose()

    try:
        asyncio.run(_run())
    finally:
        ollama_native.reset_native_capability()


def test_time_bypass_answers_without_touching_mcp():
    async def _run():
        llm = _make_llm()
        probe = _MCPProbe()
        llm._init_mcp = probe.stall
        out = []
        async for piece in llm.chat_stream("que hora es"):
            out.append(piece)
        assert "".join(out).strip()                # a deterministic answer was produced
        assert probe.started is False              # the bypass never touches MCP
        assert llm._mcp_task is None
        await llm.aclose()
    asyncio.run(_run())


# ── M55.1.2 — background logs never share the `Tú:` line ───────────────────────
class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture()
def _console_with_bridge():
    """A synchronous (thread-less) coordinator installed globally + the stdlib
    bridge, torn down cleanly (reset_console also removes the bridge)."""
    from core.console import (
        install_console, install_stdlib_logging_bridge, reset_console,
    )
    reset_console()
    cc = install_console(stream=_FakeTTY(), start=False)
    install_stdlib_logging_bridge(level=logging.INFO)
    try:
        yield cc
    finally:
        reset_console()


def test_stdlib_postgres_error_during_prompt_never_shares_input_line(_console_with_bridge):
    from core.console import ConsoleChannel  # noqa: F401 (ensures module import path)
    cc = _console_with_bridge
    cc.set_prompt("Tú: ")
    logging.getLogger("jarvis.db_manager").error(
        "PostgreSQL unavailable — No module named 'asyncpg'")
    cc.render_now()
    out = cc.stream.getvalue()
    # The exact live corruption must NOT appear.
    assert "Tú: ERROR:jarvis.db_manager" not in out
    assert "Tú: PostgreSQL" not in out
    # The error stays fully visible on its own line and the prompt is redrawn after it.
    assert "PostgreSQL unavailable" in out
    assert "jarvis.db_manager" in out
    assert out.rstrip().endswith("Tú:")


def test_stdlib_docker_warning_during_prompt_is_visible_and_offline(_console_with_bridge):
    cc = _console_with_bridge
    cc.set_prompt("Tú: ")
    logging.getLogger("jarvis.docker").warning("Docker daemon not reachable")
    cc.render_now()
    out = cc.stream.getvalue()
    assert "Docker daemon not reachable" in out
    assert "Tú: Docker" not in out
    assert out.rstrip().endswith("Tú:")


def test_stdlib_info_during_prompt_stays_off_the_input_line(_console_with_bridge):
    cc = _console_with_bridge
    cc.set_prompt("Tú: ")
    logging.getLogger("jarvis.net").info("NET_BASELINE: sample complete")
    cc.render_now()
    out = cc.stream.getvalue()
    assert "NET_BASELINE: sample complete" in out
    assert out.rstrip().endswith("Tú:")


def test_bridge_attaches_root_handler_and_is_idempotent():
    from core.console import (
        _ConsoleLoggingHandler, install_console, install_stdlib_logging_bridge,
        remove_stdlib_logging_bridge, reset_console,
    )
    reset_console()
    install_console(stream=_FakeTTY(), start=False)
    install_stdlib_logging_bridge()
    install_stdlib_logging_bridge()  # idempotent — must not stack a second handler
    try:
        root = logging.getLogger()
        bridges = [h for h in root.handlers if isinstance(h, _ConsoleLoggingHandler)]
        assert len(bridges) == 1
        remove_stdlib_logging_bridge()
        assert not any(isinstance(h, _ConsoleLoggingHandler) for h in root.handlers)
    finally:
        reset_console()
