"""
core/audio.py — High-Priority STT Listener Thread

Pre-loads the Whisper model in a dedicated daemon thread with elevated
OS scheduling priority to prevent CPU contention with LLM inference.

Memory layout intent (future 64GB pool):
  Whisper model weights reside in a fixed heap region owned by this thread.
  The LLM KV-cache lives in a separate region, eliminating cache thrashing
  during concurrent tool-call execution.
"""

import io
import os
import math
import threading
import tempfile
from typing import Tuple, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf
from loguru import logger

from core.config import settings


def _try_elevate_priority() -> None:
    """Best-effort thread priority elevation. Silent on permission error."""
    try:
        if os.name == "nt":
            import ctypes
            THREAD_PRIORITY_ABOVE_NORMAL = 1
            handle = ctypes.windll.kernel32.GetCurrentThread()
            ctypes.windll.kernel32.SetThreadPriority(handle, THREAD_PRIORITY_ABOVE_NORMAL)
            logger.debug("Audio: thread priority → ABOVE_NORMAL (Windows).")
        else:
            os.setpriority(os.PRIO_PROCESS, 0, -5)
            logger.debug("Audio: thread niceness → -5 (POSIX).")
    except Exception as exc:
        logger.debug(f"Audio: no se pudo elevar prioridad del thread: {exc}")


class HighPrioritySTTListener:
    """
    Loads the Whisper model once in a background daemon thread with elevated
    scheduling priority, then exposes listen() and transcribe_with_confidence()
    for the vocal auth gate and the main push-to-talk pipeline.

    Drop-in replacement for core/stt.py STT — same listen() signature.
    """

    def __init__(self) -> None:
        self._model = None
        self._ready = threading.Event()
        self._sample_rate    = settings.sample_rate
        self._record_seconds = settings.record_seconds
        self._language       = settings.whisper_language

        loader = threading.Thread(
            target=self._load_model,
            name="whisper-hi-prio",
            daemon=True,
        )
        loader.start()

    # ── Internal loader ───────────────────────────────────────────────────────

    def _load_model(self) -> None:
        _try_elevate_priority()
        try:
            from faster_whisper import WhisperModel
            size = settings.whisper_model
            logger.info(f"Audio: cargando Whisper '{size}' en thread de alta prioridad...")
            self._model = WhisperModel(size, device="cpu", compute_type="int8")
            logger.info("Audio: Whisper listo (modelo aislado del LLM en memoria).")
        except Exception as exc:
            logger.error(f"Audio: error cargando Whisper: {exc}")
        finally:
            self._ready.set()

    # ── Public API ────────────────────────────────────────────────────────────

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until the model is loaded or timeout expires."""
        return self._ready.wait(timeout=timeout)

    @property
    def model(self):
        """Returns WhisperModel once ready, or None if loading failed."""
        self._ready.wait()
        return self._model

    def record(self, seconds: Optional[int] = None) -> np.ndarray:
        duration = seconds or self._record_seconds
        logger.info(f"Audio: grabando {duration}s...")
        audio = sd.rec(
            int(duration * self._sample_rate),
            samplerate=self._sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()
        return audio.flatten()

    def transcribe_with_confidence(self, audio: np.ndarray) -> Tuple[str, float]:
        """Returns (text, confidence) where confidence = exp(mean avg_logprob)."""
        if self._model is None:
            return "", 0.0

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, audio, self._sample_rate)
            tmp_path = tmp.name

        try:
            lang = self._language if self._language != "auto" else None
            segments, _ = self._model.transcribe(tmp_path, language=lang, beam_size=5)
            seg_list = list(segments)
            text = " ".join(s.text.strip() for s in seg_list).strip()
            if seg_list:
                avg_logprob = sum(s.avg_logprob for s in seg_list) / len(seg_list)
                confidence = math.exp(max(-10.0, avg_logprob))
            else:
                confidence = 0.0
            logger.debug(f"Audio: '{text}' confianza={confidence:.2%}")
            return text, confidence
        finally:
            os.unlink(tmp_path)

    def listen(self, seconds: Optional[int] = None) -> str:
        """Backward-compatible: record + transcribe, returns text only."""
        audio = self.record(seconds)
        text, _ = self.transcribe_with_confidence(audio)
        return text
