"""
tests/test_stt_transcribe_bytes.py — V62.0 Phase 1/2: STT fixes backing voice parity.

main.py's continuous voice loop called stt.transcribe_bytes(pcm, sample_rate),
a method that didn't exist on HighPrioritySTTListener — every VAD-triggered
utterance silently failed transcription (AttributeError swallowed by a broad
except). Separately, the Whisper model loader thread was only ever started as
a side effect deep inside listen_vad()'s first broadcast call (a misplaced
block inside _vad_broadcast), so a caller that never calls listen_vad() —
exactly what the continuous voice loop does — never loaded a model at all.

These tests use a monkeypatched _load_model (no real faster-whisper import,
so this stays fast and CI-portable) to prove: the model loads at
construction time (not lazily), transcribe_bytes exists and returns text,
and it captures faster-whisper's language-ID output for LanguageContext.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.audio import HighPrioritySTTListener


class _FakeSegment:
    def __init__(self, text: str, avg_logprob: float):
        self.text = text
        self.avg_logprob = avg_logprob


class _FakeInfo:
    def __init__(self, language: str, language_probability: float):
        self.language = language
        self.language_probability = language_probability


class _FakeWhisperModel:
    def __init__(self, segments, info):
        self._segments = segments
        self._info = info

    def transcribe(self, path, language=None, beam_size=5):
        return iter(self._segments), self._info


@pytest.fixture
def listener(monkeypatch):
    # Skip the real faster-whisper import/load — prove the *wiring*, not the
    # model itself. Still exercises the real __init__ -> loader-thread path.
    monkeypatch.setattr(HighPrioritySTTListener, "_load_model", lambda self: self._ready.set())
    stt = HighPrioritySTTListener()
    assert stt.wait_ready(timeout=5.0), "loader thread must start at construction time"
    return stt


def test_model_loads_at_construction_not_lazily(listener):
    """Regression guard: the loader thread used to only start as a side
    effect of listen_vad()'s first broadcast — a caller (continuous voice
    mode) that never calls listen_vad() never loaded a model at all."""
    assert listener._ready.is_set()


def test_transcribe_bytes_exists_and_returns_text(listener):
    listener._model = _FakeWhisperModel(
        [_FakeSegment("hello world", -0.1)], _FakeInfo("en", 0.97)
    )
    pcm = np.zeros(1600, dtype=np.int16).tobytes()

    text = listener.transcribe_bytes(pcm, 16000)

    assert text == "hello world"


def test_transcribe_bytes_populates_language_context_inputs(listener):
    listener._model = _FakeWhisperModel(
        [_FakeSegment("hola mundo", -0.2)], _FakeInfo("es", 0.91)
    )
    pcm = np.zeros(1600, dtype=np.int16).tobytes()

    listener.transcribe_bytes(pcm, 16000)

    assert listener.last_detected_language == "es"
    assert listener.last_language_confidence == pytest.approx(0.91)


def test_transcribe_bytes_restores_sample_rate_after_call(listener):
    listener._model = _FakeWhisperModel([_FakeSegment("x", -0.1)], _FakeInfo("en", 0.9))
    original_rate = listener._sample_rate

    listener.transcribe_bytes(np.zeros(800, dtype=np.int16).tobytes(), 8000)

    assert listener._sample_rate == original_rate


def test_transcribe_bytes_returns_empty_when_model_never_becomes_ready(listener, monkeypatch):
    listener._model = None
    monkeypatch.setattr(listener, "wait_ready", lambda timeout=60.0: False)

    text = listener.transcribe_bytes(b"\x00\x00" * 100, 16000)
    assert text == ""
