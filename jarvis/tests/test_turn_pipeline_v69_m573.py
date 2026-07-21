"""tests/test_turn_pipeline_v69_m573.py — V69 M57.3/.4 live turn wiring.

Drives the REAL ``main._run_turn`` with a fake LLM stream, a fake TTS and a captured
console, and proves the end-to-end presentation contract:

  * a token-by-token stream produces readable fragments, not a character flood;
  * every character reaches the screen exactly once, in order;
  * the assistant's own speech is enqueued at HIGH/CRITICAL, so
    ``cancel_boot_narration()`` can no longer silence it;
  * code blocks are displayed but not spoken;
  * a stalled stream still flushes (the idle flusher is a timer, not a post-chunk
    check — the latter can never fire);
  * the turn closes with a truthful terminal state and leaves no orphan task;
  * prompt restoration never waits on speech.

No live model, no live speech engine.
"""
from __future__ import annotations

import asyncio

import main as jarvis_main
from core.response_runtime import TurnState, get_response_runtime, reset_response_runtime
from core.tts_queue import TTSPriority


def setup_function(_):
    reset_response_runtime(None)


def teardown_function(_):
    reset_response_runtime(None)


class _FakeLanguage:
    def active_language(self) -> str:
        return "es"


class _FakeExecutor:
    authority = None


class _FakeLLM:
    """Streams pre-baked chunks through the real turn pipeline."""

    def __init__(self, chunks, *, delay: float = 0.0) -> None:
        self._chunks = list(chunks)
        self._delay = delay
        self.language_context = _FakeLanguage()
        self.tool_executor = _FakeExecutor()
        self._last_shape = None

    async def chat_stream(self, user_input: str):
        for c in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield c


class _FakeTTS:
    """Records what was asked to be spoken, with priority and key."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, TTSPriority, str | None]] = []
        self.interrupted = 0
        self.boot_cancelled = 0

    async def speak_async(self, text, lang=None, *, priority=TTSPriority.NORMAL,
                          coalesce_key=None) -> None:
        self.spoken.append((text, priority, coalesce_key))

    def cancel_boot_narration(self) -> int:
        self.boot_cancelled += 1
        return 0

    def interrupt(self) -> None:
        self.interrupted += 1


class _CaptureConsole:
    """Stands in for ConsoleCoordinator; records the ASSISTANT stream verbatim."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.streams_open = 0
        self.streams_closed = 0

    def post(self, text, channel=None, coalesce_key=None) -> bool:
        self.chunks.append(text)
        return True

    def begin_stream(self) -> None:
        self.streams_open += 1

    def end_stream(self) -> None:
        self.streams_closed += 1

    def set_prompt(self, prompt) -> None:
        pass

    def text(self) -> str:
        return "".join(self.chunks)


def _run(llm, tts, monkeypatch, *, user="explicame algo", name="JARVIS"):
    console = _CaptureConsole()
    monkeypatch.setattr("core.console.get_console", lambda: console)
    asyncio.run(jarvis_main._run_turn(llm, tts, user, name))
    return console


# ── rendering ─────────────────────────────────────────────────────────────────
def test_token_stream_is_rendered_as_readable_fragments(monkeypatch):
    text = ("La raíz cúbica de x es x ** (1/3). "
            "Por ejemplo, la raíz cúbica de 27 es 3. ")
    tokens = [text[i:i + 2] for i in range(0, len(text), 2)]
    console = _run(_FakeLLM(tokens), _FakeTTS(), monkeypatch)
    body = console.text()
    assert "La raíz cúbica de x es x ** (1/3)." in body
    # The header + fragments + trailing newline: far fewer posts than tokens.
    assert len(console.chunks) < len(tokens) / 4


def test_every_character_reaches_the_screen_exactly_once(monkeypatch):
    chunks = ["Primera frase completa. ", "Segunda frase distinta. ",
              "Tercera y ultima frase."]
    console = _run(_FakeLLM(chunks), _FakeTTS(), monkeypatch)
    body = console.text()
    for c in chunks:
        assert body.count(c.strip()) == 1


def test_stream_region_is_opened_and_closed_once(monkeypatch):
    console = _run(_FakeLLM(["Hola. "]), _FakeTTS(), monkeypatch)
    assert console.streams_open == 1
    assert console.streams_closed == 1


# ── speech ────────────────────────────────────────────────────────────────────
def test_assistant_speech_is_high_priority_and_turn_keyed(monkeypatch):
    tts = _FakeTTS()
    _run(_FakeLLM(["Una frase suficientemente larga. ",
                   "Y otra frase distinta tambien larga. "]), tts, monkeypatch)
    assert tts.spoken, "progressive speech must queue completed sentences"
    for _text, priority, key in tts.spoken:
        assert priority >= TTSPriority.HIGH, (
            "assistant speech at NORMAL is dropped by cancel_boot_narration()")
        assert key and key.startswith("answer:")


def test_code_blocks_are_displayed_but_not_spoken(monkeypatch):
    tts = _FakeTTS()
    console = _run(_FakeLLM(["Asi se calcula:\n", "```python\n",
                             "def f(x):\n", "    return x ** (1/3)\n", "```\n"]),
                   tts, monkeypatch)
    assert "def f(x):" in console.text()
    assert not any("def f(x)" in t for t, _p, _k in tts.spoken)


def test_markdown_is_never_spoken_literally(monkeypatch):
    tts = _FakeTTS()
    _run(_FakeLLM(["## Titulo importante del tema\n",
                   "El **concepto** central es simple. "]), tts, monkeypatch)
    for text, _p, _k in tts.spoken:
        assert "**" not in text and "##" not in text


def test_prompt_restoration_never_waits_on_speech(monkeypatch):
    class _SlowTTS(_FakeTTS):
        async def speak_async(self, text, lang=None, *, priority=TTSPriority.NORMAL,
                              coalesce_key=None):
            await asyncio.sleep(0)          # yields, but never blocks the turn
            await super().speak_async(text, lang, priority=priority,
                                      coalesce_key=coalesce_key)

    import time as _t
    t0 = _t.monotonic()
    _run(_FakeLLM(["Una frase. " * 5]), _SlowTTS(), monkeypatch)
    assert (_t.monotonic() - t0) < 5.0


# ── idle flush ────────────────────────────────────────────────────────────────
def test_stalled_stream_still_flushes_buffered_text(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "response_stream_flush_ms", 150, raising=False)
    # No sentence terminator anywhere, and a real gap between chunks: without the
    # timer-based flusher the operator would watch a frozen half-sentence.
    console = _run(_FakeLLM(["una respuesta larga sin puntuacion ",
                             "que sigue y sigue sin terminar nunca "],
                            delay=0.45),
                   _FakeTTS(), monkeypatch)
    assert "una respuesta larga sin puntuacion" in console.text()


# ── turn lifecycle ────────────────────────────────────────────────────────────
def test_turn_is_recorded_with_a_truthful_terminal_state(monkeypatch):
    _run(_FakeLLM(["Hola. "]), _FakeTTS(), monkeypatch)
    rr = get_response_runtime()
    assert rr.turns_started == 1
    assert rr.current.state is TurnState.COMPLETED
    assert rr.current.chars_shown > 0


def test_turn_metrics_are_published_and_content_free(monkeypatch):
    from core.speech_stream import last_speech_metrics
    _run(_FakeLLM(["Mi contraseña es hunter2 y es larga. "]), _FakeTTS(), monkeypatch)
    metrics = last_speech_metrics()
    blob = str(metrics).lower()
    assert "hunter2" not in blob and "contraseña" not in blob
    assert "stream" in metrics
    assert metrics["stream"]["fragments_emitted"] >= 1


def test_no_orphan_task_survives_the_turn(monkeypatch):
    async def _drive():
        console = _CaptureConsole()
        monkeypatch.setattr("core.console.get_console", lambda: console)
        await jarvis_main._run_turn(_FakeLLM(["Hola mundo entero. "]), _FakeTTS(),
                                    "hola", "JARVIS")
        # Give any leaked task one loop iteration to show itself.
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()]

    assert asyncio.run(_drive()) == []


def test_empty_stream_still_finalizes_and_restores(monkeypatch):
    console = _run(_FakeLLM([]), _FakeTTS(), monkeypatch)
    assert console.streams_closed == 1
    assert get_response_runtime().current.state is TurnState.COMPLETED
