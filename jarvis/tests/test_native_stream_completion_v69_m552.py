"""
tests/test_native_stream_completion_v69_m552.py — V69 M55.2 native stream completion.

The live run showed a native FAST answer that streamed a few sentences then stopped
with no completion marker, no restored prompt, and (per M55) a risk that the NEXT
turn answers the PREVIOUS question. These tests lock the terminal-state contract:

  * a native turn resolves to EXACTLY ONE state — COMPLETED / TIMED_OUT / CANCELLED /
    FAILED / DISCONNECTED;
  * a partial stream (idle stall, disconnect, done-without-content) preserves what was
    shown, appends a SHORT localized "incomplete" status, and never claims success;
  * history is finalized exactly once so a timed-out/partial turn cannot contaminate
    the next turn (no dangling user message);
  * the generator returns coherently on every path, including the outer aclose()
    (GeneratorExit) that fires when the turn-level deadline cancels mid-stream.

No live model: the native transport is stubbed to reproduce each pattern at the wire.
"""
from __future__ import annotations

import asyncio

import pytest

from core import ollama_native
from core.ollama_native import ChatChunk, NativeCapability, NativeProbeState, NativeTransportError
from core.turn_budget import TurnTimeout


class _FakeExec:
    authority = None


def _make_llm():
    from core.llm import LLM

    llm = LLM(tool_executor=_FakeExec())
    llm.history = []
    return llm


@pytest.fixture(autouse=True)
def _native_ready():
    ollama_native.set_native_capability(
        NativeCapability(state=NativeProbeState.NATIVE_READY, model="qwen3:8b"))
    yield
    ollama_native.reset_native_capability()


async def _collect(llm, msg):
    out = []
    async for piece in llm.chat_stream(msg):
        out.append(piece)
    return "".join(out)


# ── Terminal states ───────────────────────────────────────────────────────────
def test_content_plus_done_is_completed(monkeypatch):
    async def _gen(**kw):
        yield ChatChunk(content="La raíz cuadrada ", done=False)
        yield ChatChunk(content="es la operación inversa.", done=False)
        yield ChatChunk(content="", done=True, done_reason="stop",
                        eval_count=6, eval_duration=1_000_000_000)
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        text = await _collect(llm, "explicame como sacar la raiz al cuadrado")
        assert "raíz cuadrada" in text
        assert "operación inversa" in text
        # COMPLETED: full answer in history, no incomplete-status addendum.
        assert llm.history[-1]["role"] == "assistant"
        assert "incompleta" not in llm.history[-1]["content"]
        assert llm.history[-2]["role"] == "user"
        await llm.aclose()
    asyncio.run(_run())


def test_content_without_done_clean_eos_is_completed(monkeypatch):
    async def _gen(**kw):
        yield ChatChunk(content="Respuesta parcial ", done=False)
        yield ChatChunk(content="pero sin evento done.", done=False)
        # clean StopAsyncIteration, no done event
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        text = await _collect(llm, "explicame POO brevemente")
        assert "sin evento done" in text
        # Clean EOS with content is accepted as COMPLETED (valid end-of-stream policy).
        assert llm.history[-1]["role"] == "assistant"
        assert "incompleta" not in llm.history[-1]["content"]
        await llm.aclose()
    asyncio.run(_run())


def test_content_then_disconnect_is_disconnected_with_status(monkeypatch):
    async def _gen(**kw):
        yield ChatChunk(content="Para sacar la raíz cuadrada ", done=False)
        raise NativeTransportError("stream_error:ReadError", kind="transport")
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        text = await _collect(llm, "explicame como sacar la raiz al cuadrado")
        # The partial that streamed is preserved AND a short localized status appended.
        assert "raíz cuadrada" in text
        assert "incompleta" in text.lower()
        assert llm.history[-1]["role"] == "assistant"
        assert "raíz cuadrada" in llm.history[-1]["content"]
        assert "incompleta" in llm.history[-1]["content"].lower()
        await llm.aclose()
    asyncio.run(_run())


def test_done_without_content_is_coherent_status(monkeypatch):
    async def _gen(**kw):
        yield ChatChunk(content="", done=True, done_reason="stop")
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        text = await _collect(llm, "explicame POO brevemente")
        # No content: the user still gets a coherent status and history has no dangle.
        assert text.strip()
        assert llm.history[-1]["role"] == "assistant"
        assert llm.history[-2]["role"] == "user"
        await llm.aclose()
    asyncio.run(_run())


def test_content_then_idle_timeout_is_timed_out_with_status(monkeypatch):
    async def _gen(**kw):
        yield ChatChunk(content="Para sacar la raíz cuadrada, ", done=False)
        raise TurnTimeout("stream_idle", 0.05)
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        text = await _collect(llm, "explicame como sacar la raiz al cuadrado")
        assert "raíz cuadrada" in text            # partial preserved
        assert "incompleta" in text.lower()       # honest status, never "success"
        assert llm.history[-1]["role"] == "assistant"
        await llm.aclose()
    asyncio.run(_run())


# ── Coherence: a partial turn cannot contaminate the next turn ─────────────────
def test_timed_out_turn_does_not_contaminate_next_turn(monkeypatch):
    calls = {"n": 0}

    async def _gen(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ChatChunk(content="Para sacar la raíz cuadrada, ", done=False)
            raise TurnTimeout("stream_idle", 0.05)
        else:
            yield ChatChunk(content="POO es programación orientada a objetos.", done=False)
            yield ChatChunk(content="", done=True, done_reason="stop")
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        await _collect(llm, "explicame como sacar la raiz al cuadrado")   # TIMED_OUT
        # No dangling user; the partial turn is a closed [user, assistant] pair.
        assert llm.history[-1]["role"] == "assistant"
        assert sum(1 for m in llm.history if m["role"] == "user") == 1
        text2 = await _collect(llm, "explicame POO brevemente")           # COMPLETED
        assert "orientada a objetos" in text2      # answered the NEW question
        assert "raíz" not in text2                 # not the previous one
        roles = [m["role"] for m in llm.history]
        assert roles == ["user", "assistant", "user", "assistant"]
        await llm.aclose()
    asyncio.run(_run())


# ── GeneratorExit: outer aclose() mid-stream finalizes coherently ─────────────
def test_generatorexit_midstream_finalizes_partial_and_no_dangle(monkeypatch):
    async def _gen(**kw):
        yield ChatChunk(content="Para sacar la raíz cuadrada ", done=False)
        await asyncio.sleep(10)                    # suspended when aclose fires
        yield ChatChunk(content="nunca llega", done=False)
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        agen = llm.chat_stream("explicame como sacar la raiz al cuadrado")
        first = await agen.__anext__()             # receive the first content chunk
        assert "raíz cuadrada" in first
        await agen.aclose()                        # turn-level deadline aclose()
        # History is finalized: the partial is kept, the user turn is NOT dangling.
        assert llm.history[-1]["role"] == "assistant"
        assert "raíz cuadrada" in llm.history[-1]["content"]
        assert sum(1 for m in llm.history if m["role"] == "user") == 1
        await llm.aclose()
    asyncio.run(_run())


# ── M55.2.2 — a TTS fault cannot block the turn / prompt restoration ──────────
def test_tts_failure_does_not_block_turn_finalization():
    import main

    class _LC:
        def active_language(self):
            return "es"

        def observe_text(self, _t):
            pass

    class _LLM:
        tool_executor = _FakeExec()
        language_context = _LC()

        async def chat_stream(self, _msg):
            yield "Hola. "
            yield "Esta respuesta se completa."

    class _FailTTS:
        async def speak_async(self, _text, lang=None):
            raise RuntimeError("pyttsx3 COM wedge")

    async def _run():
        # If the TTS fault propagated, _run_turn would raise or hang; wait_for proves
        # it finalizes and returns control (so the loop can restore the prompt).
        await asyncio.wait_for(
            main._run_turn(_LLM(), _FailTTS(), "hola", "Alicia"), timeout=10.0)

    asyncio.run(_run())


# ── Pre-content failure raises _NativeFastUnavailable (caller falls back) ───────
def test_pre_content_transport_error_signals_fallback(monkeypatch):
    from core.llm import _NativeFastUnavailable
    from core.fast_path import FastRouteDecision, FastReason
    from core.turn_budget import TurnBudget, StageTimeouts

    async def _gen(**kw):
        raise NativeTransportError("connect_failed:ConnectError", kind="connect")
        yield  # pragma: no cover — makes this an async generator
    monkeypatch.setattr(ollama_native, "chat_stream", _gen)

    async def _run():
        llm = _make_llm()
        route = FastRouteDecision(use_native=True, reason=FastReason.NATIVE_FAST_NO_THINK,
                                  model="qwen3:8b")
        result: dict = {}
        llm.history.append({"role": "user", "content": "hola"})
        agen = llm._native_fast_stream(
            route=route, budget=TurnBudget(total_s=10.0),
            timeouts=StageTimeouts(total_s=10.0), result=result)
        with pytest.raises(_NativeFastUnavailable):
            async for _ in agen:
                pass
        await llm.aclose()
    asyncio.run(_run())
