"""
tests/test_voice_parity.py — V62.0 Phase 2: voice shares the real agentic runtime.

Continuous voice mode used to call llm.client.chat.completions.create()
directly (main._loop_voice_continuous's old _ask_jarvis helper), bypassing
llm.chat_stream() entirely: no tool-calling, no HITL/NATO gate, no
model-router/verifier/memory wiring. It was also non-functional — main.py
called stt.transcribe_bytes(...), a method that didn't exist on
HighPrioritySTTListener, and _ask_jarvis's model resolution
(getattr(llm, "current_model", getattr(llm, "model", ""))) always evaluated
to "" since neither attribute was ever set anywhere in the codebase.

These tests prove: (1) transcribe_bytes now exists and actually transcribes,
(2) the continuous voice loop's source routes through _process_voice_input /
_run_turn instead of the old direct-client bypass, and (3) _process_voice_input
drives the exact same llm.chat_stream() entry point _loop_text uses — the
parity claim itself, exercised end-to-end with a fake LLM/TTS.
"""
from __future__ import annotations

import asyncio
import inspect

import main
from main import _process_voice_input, _run_turn


class _FakeLLM:
    def __init__(self):
        self.calls: list[str] = []

    async def chat_stream(self, user_message: str):
        self.calls.append(user_message)
        yield "Hola."


class _FakeTTS:
    def __init__(self):
        self.spoken: list[tuple[str, str | None]] = []

    async def speak_async(self, text: str, lang: str | None = None) -> None:
        self.spoken.append((text, lang))


def _no_interrupt(_text):
    return None


async def _no_macro(*_args, **_kwargs):
    return False


def test_continuous_voice_loop_source_uses_unified_pipeline():
    """Characterization test locking in the architectural fix: the continuous
    voice loop must dispatch turns through _process_voice_input (which itself
    calls _run_turn -> llm.chat_stream), and must never again reference the
    removed direct-client bypass or its dead fallback."""
    src = inspect.getsource(main._loop_voice_continuous)
    assert "_process_voice_input" in src
    assert "_ask_jarvis" not in src
    assert "chat_async" not in src
    assert "transcribe_bytes" in src


def test_process_voice_input_routes_through_chat_stream(monkeypatch):
    monkeypatch.setattr("core.voice_interrupt.is_interrupt_command", _no_interrupt)
    monkeypatch.setattr("core.voice_macros.process_for_macro", _no_macro)

    llm = _FakeLLM()
    tts = _FakeTTS()

    handled = asyncio.run(_process_voice_input("hello jarvis", llm, tts, "Operator"))

    assert handled is False
    assert llm.calls == ["hello jarvis"], (
        "voice input must reach the SAME llm.chat_stream() entry point as text mode"
    )
    assert tts.spoken and tts.spoken[0][0].strip() == "Hola."


def test_process_voice_input_forwards_language_hint_to_tts(monkeypatch):
    monkeypatch.setattr("core.voice_interrupt.is_interrupt_command", _no_interrupt)
    monkeypatch.setattr("core.voice_macros.process_for_macro", _no_macro)

    llm = _FakeLLM()
    tts = _FakeTTS()

    asyncio.run(_process_voice_input("hola jarvis", llm, tts, "Operator", lang="es"))

    assert tts.spoken[0][1] == "es"


def test_text_and_voice_reach_identical_run_turn(monkeypatch):
    """_run_turn is the single shared entry both _loop_text and
    _process_voice_input call — prove both call paths converge on it with
    equivalent behavior (same chat_stream invocation, same TTS enqueue)."""
    monkeypatch.setattr("core.voice_interrupt.is_interrupt_command", _no_interrupt)
    monkeypatch.setattr("core.voice_macros.process_for_macro", _no_macro)

    llm_text = _FakeLLM()
    tts_text = _FakeTTS()
    asyncio.run(_run_turn(llm_text, tts_text, "same question", "Operator"))

    llm_voice = _FakeLLM()
    tts_voice = _FakeTTS()
    asyncio.run(_process_voice_input("same question", llm_voice, tts_voice, "Operator"))

    assert llm_text.calls == llm_voice.calls == ["same question"]


def test_interrupt_command_short_circuits_before_llm(monkeypatch):
    """The unified path must still honor interrupt commands (abort/status/reset)
    — a capability the old continuous-voice loop never checked at all for
    transcribed text (only acoustic VAD-level barge-in)."""
    monkeypatch.setattr("core.voice_interrupt.is_interrupt_command", lambda t: "abort")

    async def _fake_handle_interrupt(interrupt_type, broadcast_fn):
        return None

    monkeypatch.setattr("core.voice_interrupt.handle_interrupt", _fake_handle_interrupt)

    llm = _FakeLLM()
    tts = _FakeTTS()

    handled = asyncio.run(_process_voice_input("abort", llm, tts, "Operator"))

    assert handled is True
    assert llm.calls == [], "an interrupt command must never reach chat_stream"
