"""
core/audio.py — High-Priority STT Listener Thread

Pre-loads the Whisper model in a dedicated daemon thread with elevated
OS scheduling priority to prevent CPU contention with LLM inference.

Memory layout intent (future 64GB pool):
  Whisper model weights reside in a fixed heap region owned by this thread.
  The LLM KV-cache lives in a separate region, eliminating cache thrashing
  during concurrent tool-call execution.
"""

import asyncio
import os
import math
import queue
import threading
import tempfile
from typing import Tuple, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf
from loguru import logger

from core.config import settings
from core.audio_vad import VADAccumulator, FRAME_BYTES, SAMPLE_RATE as VAD_SAMPLE_RATE


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

        # v31.0: WebRTC VAD pre-filter. Gates Whisper inference so it only
        # runs on confirmed speech segments — eliminates the dominant
        # idle-silence CPU consumer on the Ryzen 5 7430U.
        self._vad = VADAccumulator()

        # v32.0: AURA HUD VAD-event bridge (set externally from main.py)
        self._loop_ref: Optional[asyncio.AbstractEventLoop] = None
        self._broadcast_ref = None

    def _vad_broadcast(self, state: str) -> None:
        """v32.0 — Thread-safe VAD state broadcast to AURA HUD."""
        if self._loop_ref and self._broadcast_ref:
            try:
                self._loop_ref.call_soon_threadsafe(
                    self._loop_ref.create_task,
                    self._broadcast_ref({
                        "type":  "vad_event",
                        "state": state,
                    })
                )
            except Exception:
                pass

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
            # v31.0: int8 + tuned thread budget. cpu_threads=4 leaves 2 of
            # the Ryzen 5 7430U's 6 physical cores free for the asyncio event
            # loop and Ollama inference. num_workers=1 because VAD already
            # pre-filters concurrent requests upstream.
            self._model = WhisperModel(
                size,
                device       = "cpu",
                compute_type = "int8",
                cpu_threads  = 4,
                num_workers  = 1,
            )
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

    # ── v31.0 VAD-gated capture ───────────────────────────────────────────────
    def listen_vad(self, max_seconds: int = 30) -> str:
        """
        VAD-gated capture: stream raw 16 kHz PCM-16 mono frames into the
        VADAccumulator until it returns a complete utterance, then transcribe.

        Uses a single sounddevice RawInputStream (one audio device, one
        capture thread — no second PyAudio stream). Whisper runs only on
        confirmed speech, eliminating idle-silence inference cost.

        max_seconds caps the wait so this never blocks the UI loop forever.
        """
        if self._model is None and not self.wait_ready(timeout=30.0):
            return ""

        self._vad.reset()
        self._vad_broadcast("listening")  # v32.0
        utterance_holder: dict[str, bytes] = {}
        frame_q: queue.Queue[bytes] = queue.Queue()
        stop_event = threading.Event()

        # blocksize=480 samples = 30ms @ 16kHz mono PCM-16 → FRAME_BYTES bytes
        block_samples = FRAME_BYTES // 2

        def _on_audio(indata, frames, time_info, status):
            if status:
                logger.debug(f"VAD stream status: {status}")
            # indata is a CFFI buffer of bytes — copy out before yielding
            frame_q.put(bytes(indata))

        try:
            stream = sd.RawInputStream(
                samplerate = VAD_SAMPLE_RATE,
                blocksize  = block_samples,
                dtype      = "int16",
                channels   = 1,
                callback   = _on_audio,
            )
        except Exception as exc:
            logger.warning(f"VAD: could not open audio stream — {exc}")
            return ""

        deadline = threading.Event()
        timer = threading.Timer(max_seconds, deadline.set)
        timer.daemon = True
        timer.start()

        with stream:
            while not deadline.is_set():
                try:
                    frame = frame_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                utterance = self._vad.feed(frame)
                if utterance is not None:
                    self._vad_broadcast("speech_detected")  # v32.0
                    utterance_holder["pcm"] = utterance
                    break

        timer.cancel()
        stop_event.set()

        pcm = utterance_holder.get("pcm")
        if not pcm:
            self._vad_broadcast("idle")  # v32.0
            return ""

        # Convert int16 PCM bytes → float32 numpy at 16kHz for Whisper
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # transcribe_with_confidence writes via self._sample_rate; temporarily
        # honour the VAD-native rate to avoid pitch/time distortion.
        prev_rate = self._sample_rate
        try:
            self._sample_rate = VAD_SAMPLE_RATE
            self._vad_broadcast("transcribing")  # v32.0
            text, _ = self.transcribe_with_confidence(audio)
        finally:
            self._sample_rate = prev_rate
            self._vad_broadcast("listening")  # v32.0
        return text
