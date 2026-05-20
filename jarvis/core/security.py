"""
core/security.py — Vocal Authorization Protocol (VAP) v11.1

Two-factor HITL gate with voice-first confirmation:
  1. Ambient calibration (SpeechRecognition + PyAudio)
  2. Voice capture with 5-second listen timeout → keyboard fallback
  3. Transcription + confidence scoring via faster-whisper (threshold 0.85)
  4. 10-second auth window enforced end-to-end
  5. Keyboard fallback on any failure path
"""

import io
import os
import sys
import math
import time
import tempfile
from typing import Optional

import soundfile as sf
from loguru import logger


_CONFIRM_KEYWORDS = frozenset({
    "hazlo", "sí", "si", "yes", "confirmar", "autorizar", "ejecutar", "adelante", "execute",
})
_DENY_KEYWORDS = frozenset({
    "no", "cancelar", "cancel", "denegar", "abort", "detener",
})

_CONFIDENCE_THRESHOLD = 0.85
_LISTEN_TIMEOUT_S     = 5.0    # seconds before falling back to keyboard
_AUTH_WINDOW_S        = 10.0   # total window from challenge display to decision


class VocalAuthenticator:
    """
    HITL authorization gate with vocal confirmation and confidence scoring.

    Accepts a HighPrioritySTTListener (from core/audio.py) for the Whisper model.
    Falls back to keyboard if SpeechRecognition/PyAudio are unavailable or
    the model fails to load.
    """

    def __init__(self, stt_listener=None) -> None:
        self._listener = stt_listener   # HighPrioritySTTListener | None
        self._sr_ok = False
        try:
            import speech_recognition   # noqa: F401
            import pyaudio              # noqa: F401
            self._sr_ok = True
        except ImportError as exc:
            logger.warning(f"VAP: SpeechRecognition/PyAudio no disponibles ({exc}). Modo teclado activo.")

    # ── Public API ────────────────────────────────────────────────────────────

    def request(self, tool_name: str, preview: str) -> bool:
        """
        Display the authorization challenge and wait for confirmation.
        Returns True if the operator grants authorization.
        """
        display = tool_name.upper()
        bar = "=" * 62
        print(f"\n{bar}")
        print(f"  [!] CONFIRMACIÓN DE EJECUCIÓN REQUERIDA")
        print(f"      Tool      : {display}")
        print(f"      Parámetros: {preview}")
        print(f"  A la espera de autorización táctica para: [{display}]")
        print(f"  Di 'Hazlo' o presiona 'y' dentro de {_AUTH_WINDOW_S:.0f}s.")
        print(f"{bar}")
        sys.stdout.flush()

        model = self._listener.model if self._listener is not None else None

        if self._sr_ok and model is not None:
            return self._vocal_confirmation_loop(model, tool_name)
        return self._keyboard_fallback()

    # ── Phase 1 & 2: Vocal loop ───────────────────────────────────────────────

    def _vocal_confirmation_loop(self, model, tool_name: str) -> bool:
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        start = time.monotonic()

        print("  [ LISTENING... 🎤 ]", end="\r", flush=True)
        try:
            with sr.Microphone(sample_rate=16000) as source:
                # Dynamic ambient noise calibration
                recognizer.adjust_for_ambient_noise(source, duration=1)

                elapsed = time.monotonic() - start
                listen_budget = max(1.0, _LISTEN_TIMEOUT_S - elapsed)

                try:
                    audio = recognizer.listen(
                        source,
                        timeout=listen_budget,
                        phrase_time_limit=4,
                    )
                except sr.WaitTimeoutError:
                    self._clear_hud()
                    logger.info("VAP: Sin respuesta vocal — fallback a teclado.")
                    return self._keyboard_fallback()

        except Exception as exc:
            self._clear_hud()
            logger.warning(f"VAP: Captura de audio fallida ({exc}) — fallback a teclado.")
            return self._keyboard_fallback()

        self._clear_hud()

        # Phase 2: reject confirmations outside the 10-second auth window
        if time.monotonic() - start > _AUTH_WINDOW_S:
            print("  [X] Ventana de autorización táctica expirada.")
            return False

        wav_bytes = audio.get_wav_data()
        return self._transcribe_and_decide(model, wav_bytes)

    def _transcribe_and_decide(self, model, wav_bytes: bytes) -> bool:
        try:
            audio_array, sr_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            if audio_array.ndim > 1:
                audio_array = audio_array.mean(axis=1)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                sf.write(tmp.name, audio_array, sr_rate)
                tmp_path = tmp.name

            try:
                segments, _ = model.transcribe(tmp_path, language="es", beam_size=5)
                seg_list = list(segments)
                text = " ".join(s.text.strip() for s in seg_list).strip().lower()

                if seg_list:
                    avg_logprob = sum(s.avg_logprob for s in seg_list) / len(seg_list)
                    confidence = math.exp(max(-10.0, avg_logprob))
                else:
                    confidence = 0.0
            finally:
                os.unlink(tmp_path)

        except Exception as exc:
            logger.warning(f"VAP: Transcripción fallida ({exc}) — fallback a teclado.")
            return self._keyboard_fallback()

        logger.info(f"VAP: texto='{text}' confianza={confidence:.2%}")

        if confidence < _CONFIDENCE_THRESHOLD:
            print(
                f"\n  Baja confianza vocal ({confidence:.0%}). "
                "Por favor, repite claramente."
            )
            return self._keyboard_fallback()

        words = set(text.replace(".", "").replace(",", "").replace("¡", "").replace("!", "").split())
        if words & _CONFIRM_KEYWORDS:
            print(f"  [OK] Autorización vocal recibida ({confidence:.0%}).")
            logger.info("VAP: AUTORIZADO por voz.")
            return True
        if words & _DENY_KEYWORDS:
            print("  [X] Comando denegado por voz.")
            logger.warning("VAP: DENEGADO por voz.")
            return False

        print(f"  Comando no reconocido: '{text}'. Confirmación de ejecución requerida.")
        return self._keyboard_fallback()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clear_hud() -> None:
        print("  " + " " * 22, end="\r", flush=True)

    @staticmethod
    def _keyboard_fallback() -> bool:
        auth = input("  ¿Autorizar ejecución? (y/N): ").strip().lower()
        return auth == "y"
