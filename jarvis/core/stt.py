"""
core/stt.py — Speech-to-Text
Graba audio del micrófono y lo transcribe con faster-whisper.
Cross-platform: Windows y Linux.
"""

import os
import tempfile
import numpy as np
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel
from loguru import logger


class STT:
    def __init__(self):
        model_size = os.getenv("WHISPER_MODEL", "small")
        self.language = os.getenv("WHISPER_LANGUAGE", "es")
        self.sample_rate = int(os.getenv("SAMPLE_RATE", "16000"))
        self.record_seconds = int(os.getenv("RECORD_SECONDS", "5"))

        logger.info(f"Cargando Whisper modelo '{model_size}'...")
        # device="cpu" funciona en cualquier máquina sin GPU
        # Si tienes GPU NVIDIA: device="cuda", compute_type="float16"
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        logger.info("Whisper listo.")

    def record(self, seconds: int | None = None) -> np.ndarray:
        """Graba audio del micrófono. Retorna array numpy."""
        duration = seconds or self.record_seconds
        logger.info(f"Grabando {duration}s... (habla ahora)")

        audio = sd.rec(
            int(duration * self.sample_rate),
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()  # bloquea hasta terminar la grabación
        return audio.flatten()

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe un array numpy a texto."""
        # faster-whisper necesita un archivo WAV temporal
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, audio, self.sample_rate)
            tmp_path = tmp.name

        try:
            segments, info = self.model.transcribe(
                tmp_path,
                language=self.language if self.language != "auto" else None,
                beam_size=5,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            logger.debug(f"Transcripción: '{text}' (lang={info.language})")
            return text
        finally:
            os.unlink(tmp_path)

    def listen(self, seconds: int | None = None) -> str:
        """Helper: graba + transcribe en una sola llamada."""
        audio = self.record(seconds)
        return self.transcribe(audio)
