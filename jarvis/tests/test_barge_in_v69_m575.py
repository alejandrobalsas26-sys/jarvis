"""tests/test_barge_in_v69_m575.py — V69 M57.5/.5.1: barge-in and turn replacement.

Proves:

  * the operator command surface is an EXACT-match allowlist — a sentence that
    merely contains a command word stays an ordinary turn, and no command takes a
    free-form argument;
  * ``/stop`` cancels active generation, cancels this turn's speech and returns the
    prompt, marking the turn INTERRUPTED_BY_OPERATOR;
  * a new turn REPLACES a still-active one, and the replaced turn's late output is
    refused at the presentation boundary — no old chunk can appear afterwards;
  * shutdown cancellation is a DIFFERENT terminal state from operator interruption;
  * partial history is truthful: an incomplete answer is never stored as a
    completed one, and a content-free cancel leaves no dangling user turn.

No live model, no live speech engine.
"""
from __future__ import annotations

import asyncio

import main as jarvis_main
from core.response_commands import (
    ResponseCommand,
    describe,
    known_aliases,
    parse_response_command,
)
from core.response_runtime import (
    INCOMPLETE_STATES,
    ResponseRuntime,
    TurnState,
    get_response_runtime,
    reset_response_runtime,
)


def setup_function(_):
    reset_response_runtime(None)


def teardown_function(_):
    reset_response_runtime(None)


# ── command parsing (allowlist) ───────────────────────────────────────────────
def test_exact_aliases_parse():
    for alias in known_aliases():
        parsed = parse_response_command(alias)
        assert parsed is not None, alias
        assert parsed.alias == alias


def test_case_and_whitespace_tolerant():
    assert parse_response_command("  /BRIEF  ").command is ResponseCommand.BRIEF


def test_a_sentence_containing_a_command_word_is_not_a_command():
    for text in ("/stop the port scan", "por favor /mute the alarm",
                 "stop", "mute", "explicame /brief", "/brief please"):
        assert parse_response_command(text) is None


def test_non_slash_text_is_never_a_command():
    assert parse_response_command("brief") is None
    assert parse_response_command("") is None
    assert parse_response_command(None) is None


def test_no_command_accepts_an_argument():
    # Any trailing token makes the line a normal turn: there is no argument
    # surface at all, so no path, PID, scope or value can arrive from free text.
    for alias in known_aliases():
        assert parse_response_command(f"{alias} extra") is None
        assert parse_response_command(f"{alias} --force") is None


def test_read_only_commands_are_marked():
    for alias in ("/response-status", "/latency", "/context-status", "/tts-status",
                  "/response-profile"):
        assert parse_response_command(alias).read_only is True
    assert parse_response_command("/brief").read_only is False


def test_continue_is_conversational_not_a_control():
    parsed = parse_response_command("/continue")
    assert parsed.conversational is True
    assert parsed.read_only is False


def test_confirmations_are_localized_and_content_free():
    assert "BREVE" in describe(ResponseCommand.BRIEF, language="es")
    assert "BRIEF" in describe(ResponseCommand.BRIEF, language="en")
    assert describe(ResponseCommand.STOP, language="es", active=True) != \
        describe(ResponseCommand.STOP, language="es", active=False)


# ── turn replacement and late-chunk suppression ───────────────────────────────
def test_new_turn_replaces_a_still_active_turn():
    rr = ResponseRuntime()
    first = rr.begin_turn(contract="BRIEF")
    second = rr.begin_turn(contract="INSTANT")
    assert first.state is TurnState.REPLACED_BY_NEW_TURN
    assert second.is_active()
    assert rr.replaced_turns == 1


def test_late_output_from_a_replaced_turn_is_refused_and_counted():
    rr = ResponseRuntime()
    first = rr.begin_turn()
    rr.begin_turn()
    assert rr.accepts(first.turn_id) is False
    assert rr.accepts(rr.current.turn_id) is True
    assert rr.late_chunks_suppressed == 1


def test_output_after_a_completed_turn_is_refused():
    rr = ResponseRuntime()
    handle = rr.begin_turn()
    rr.end_turn(TurnState.COMPLETED)
    assert rr.accepts(handle.turn_id) is False


def test_unstamped_output_is_always_accepted():
    rr = ResponseRuntime()
    rr.begin_turn()
    assert rr.accepts(None) is True


def test_end_turn_is_idempotent():
    rr = ResponseRuntime()
    rr.begin_turn()
    rr.end_turn(TurnState.COMPLETED)
    rr.end_turn(TurnState.FAILED)
    assert rr.current.state is TurnState.COMPLETED
    assert rr.turns_completed == 1


def test_terminal_state_taxonomy_is_closed():
    assert TurnState.CANCELLED_ON_SHUTDOWN in INCOMPLETE_STATES
    assert TurnState.INTERRUPTED_BY_OPERATOR in INCOMPLETE_STATES
    assert TurnState.COMPLETED not in INCOMPLETE_STATES


# ── /stop applied through the real handler ────────────────────────────────────
class _FakeLanguage:
    def active_language(self) -> str:
        return "es"


class _FakeLLM:
    language_context = _FakeLanguage()


class _FakeTTS:
    def __init__(self) -> None:
        self.interrupted = 0
        self._gov = None

    def interrupt(self) -> None:
        self.interrupted += 1


def _apply(alias: str, tts=None):
    tts = tts or _FakeTTS()
    parsed = parse_response_command(alias)
    asyncio.run(jarvis_main._apply_response_command(parsed, _FakeLLM(), tts))
    return tts


def test_stop_marks_the_active_turn_interrupted_and_cancels_speech():
    rr = get_response_runtime()
    rr.begin_turn(contract="TECHNICAL")
    tts = _apply("/stop")
    assert rr.current.state is TurnState.INTERRUPTED_BY_OPERATOR
    assert rr.interrupted_turns == 1
    assert tts.interrupted == 1
    assert rr.cancellation_latency_ms is not None


def test_stop_with_no_active_turn_is_harmless():
    rr = get_response_runtime()
    _apply("/stop")
    assert rr.interrupted_turns == 0


def test_stop_sets_the_llm_cancel_flag():
    import core.cancel_bus as bus

    async def _drive():
        bus.initialize(asyncio.get_running_loop())
        bus.reset_all()
        bus.register_operation("llm_stream")
        rr = get_response_runtime()
        rr.begin_turn()
        await jarvis_main._apply_response_command(
            parse_response_command("/stop"), _FakeLLM(), _FakeTTS())
        return bus.llm_stream_cancel.is_set()

    assert asyncio.run(_drive()) is True


def test_profile_commands_flip_the_session_profile():
    rr = get_response_runtime()
    _apply("/brief")
    assert rr.profile.value == "BRIEF"
    _apply("/detailed")
    assert rr.profile.value == "DETAILED"
    _apply("/auto")
    assert rr.profile.value == "AUTO"


def test_mute_and_unmute_flip_speech_and_cancel_pending():
    rr = get_response_runtime()
    tts = _apply("/mute")
    assert rr.muted is True
    assert tts.interrupted == 1
    _apply("/unmute")
    assert rr.muted is False


def test_read_only_panels_render_without_a_turn():
    from core.response_status import (
        render_context_status, render_latency, render_response_profile,
        render_response_status, render_tts_status,
    )
    for fn in (render_response_status, render_response_profile, render_latency,
               render_context_status, render_tts_status):
        text = fn()
        assert isinstance(text, str) and text.strip()


def test_panels_never_contain_content():
    from core.response_status import render_response_status
    rr = get_response_runtime()
    rr.begin_turn(contract="BRIEF", selection_reason="SIMPLE_HOWTO", language="es")
    text = render_response_status().lower()
    for leak in ("contraseña", "hunter2", "http://", "bearer ", "sk-"):
        assert leak not in text


# ── partial history truth (M57.5.1) ───────────────────────────────────────────
class _StubExecutor:
    authority = None

    async def aexecute(self, *a, **k):
        raise AssertionError("no tool on a DIRECT_FAST turn")


def _install_native(monkeypatch, chunks, *, done_reason="stop", raise_after=None):
    from core.ollama_native import (
        ChatChunk, NativeCapability, NativeProbeState, set_native_capability,
    )

    async def fake_native(**kw):
        for i, piece in enumerate(chunks):
            if raise_after is not None and i == raise_after:
                raise asyncio.CancelledError()
            yield ChatChunk(content=piece)
        yield ChatChunk(content="", done=True, done_reason=done_reason,
                        eval_count=8, eval_duration=1_000_000_000)

    monkeypatch.setattr("core.ollama_native.chat_stream", fake_native)
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))


def test_cancelled_turn_with_partial_content_keeps_what_was_shown(monkeypatch):
    _install_native(monkeypatch, ["La raíz cúbica ", "de x es "], raise_after=2)

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        try:
            out = []
            try:
                async for piece in llm.chat_stream("como saco la raiz cubica"):
                    out.append(piece)
            except asyncio.CancelledError:
                pass
            assert llm.history[-1]["role"] == "assistant"
            assert "raíz cúbica" in llm.history[-1]["content"]
        finally:
            await llm.aclose()

    asyncio.run(_run())


def test_cancelled_turn_with_no_content_leaves_no_dangling_user(monkeypatch):
    _install_native(monkeypatch, ["x"], raise_after=0)

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        llm.history = []
        try:
            try:
                async for _ in llm.chat_stream("como saco la raiz cubica"):
                    pass
            except asyncio.CancelledError:
                pass
            # A cancelled turn that showed nothing must not leave the question
            # unanswered in history — that is what made the NEXT turn answer the
            # PREVIOUS question (the M55.1 'hola replied about TCP' bug).
            assert not llm.history or llm.history[-1]["role"] != "user"
        finally:
            await llm.aclose()

    asyncio.run(_run())


def test_incomplete_turn_is_never_stored_as_a_completed_answer(monkeypatch):
    _install_native(monkeypatch, ["Parte visible de la respuesta"],
                    done_reason="length")

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        try:
            await llm.chat("como saco la raiz cubica")
            stored = llm.history[-1]["content"]
            assert "Parte visible" in stored
            assert "continúa" in stored.lower() or "continue" in stored.lower()
        finally:
            await llm.aclose()

    asyncio.run(_run())


def test_teardown_resets_native_capability():
    from core.ollama_native import reset_native_capability
    reset_native_capability()
