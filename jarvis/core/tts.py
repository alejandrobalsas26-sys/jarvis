"""
core/tts.py — Text-to-Speech 100% local via pyttsx3.

v3: speak_async solo encola texto en asyncio.Queue. Un worker en ThreadPoolExecutor
dedicado desencola y ejecuta engine.say() + runAndWait() en segundo plano,
evitando saturar el event loop principal mientras el LLM sigue razonando.

v35.0: interrupción instantánea — drain queue + engine.stop() al recibir
señal del cancel_bus. Sub-frase splitting para preempt granular.
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

import core.cancel_bus as _cancel_bus

_SENT_SPLIT_RE = re.compile(r'(?<=[.!?;:])\s+')


class TTSVoiceRouter:
    """V62.0 Phase 1 — maps a language code to a pyttsx3 voice id.

    Built once from the engine's installed voices (engine-independent: takes
    a plain iterable of voice-like objects, so it's testable without a real
    pyttsx3/SAPI engine). voice_for() gracefully falls back to None — the
    caller's contract is to keep whatever voice is already active — for any
    language with no matching installed voice.

    NOTE: every Windows SAPI5 voice id contains the registry path segment
    "...Speech\\Voices\\Tokens\\..." — a bare "es" substring check (the
    original heuristic here) matches "Voices" in EVERY id and always picks
    the first enumerated voice regardless of language. The hints below use
    locale-code / language-name forms that don't collide with that path.
    """

    _LANG_HINTS: dict[str, tuple[str, ...]] = {
        "es": ("es-es", "es-mx", "es-us", "es-ar", "es-co", "spanish", "español", "helena", "sabina"),
        "en": ("en-us", "en-gb", "en-au", "en-ca", "en-in", "english", "david", "zira", "hazel"),
    }

    def __init__(self, voices) -> None:
        self._voice_by_lang: dict[str, str] = {}
        voices = list(voices or [])
        for lang in self._LANG_HINTS:
            for voice in voices:
                if self._voice_matches_lang(voice, lang):
                    self._voice_by_lang[lang] = voice.id
                    break

    @classmethod
    def _voice_matches_lang(cls, voice, lang: str) -> bool:
        for code in (getattr(voice, "languages", None) or []):
            code_str = code.decode("utf-8", "ignore") if isinstance(code, bytes) else str(code)
            if code_str.lower().replace("_", "-").startswith(lang):
                return True
        hints = cls._LANG_HINTS.get(lang, ())
        ident = f"{getattr(voice, 'id', '')} {getattr(voice, 'name', '')}".lower()
        return any(h in ident for h in hints)

    def voice_for(self, lang: str | None) -> str | None:
        """Voice id for *lang*, or None if unknown/unavailable (graceful
        fallback — caller must treat None as 'no change')."""
        if not lang:
            return None
        return self._voice_by_lang.get(lang)


class TTS:
    def __init__(self):
        import pyttsx3

        self.engine = pyttsx3.init()
        self._voice_router = TTSVoiceRouter(self.engine.getProperty("voices"))
        self._active_lang: str | None = None
        self._configure_engine()
        self._queue: "asyncio.Queue[tuple[str, str | None] | None]" = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-worker")
        self._worker_task: asyncio.Task | None = None
        self._interrupted: bool = False
        logger.info("TTS: pyttsx3 offline activo (modo queue asíncrona).")

    def _configure_engine(self) -> None:
        self.engine.setProperty("rate", 165)
        self.engine.setProperty("volume", 1.0)
        try:
            from core.config import settings
            default_lang = (settings.whisper_language or "").strip().lower()
        except Exception:
            default_lang = ""
        if default_lang and default_lang != "auto":
            self._apply_voice(default_lang)

    def _apply_voice(self, lang: str | None) -> None:
        """Switch the engine's active voice for *lang*. No-op (keeps current
        voice) when lang is unset, unchanged, or has no mapped voice —
        TTSVoiceRouter's graceful-fallback contract."""
        if not lang or lang == self._active_lang:
            return
        voice_id = self._voice_router.voice_for(lang)
        if not voice_id:
            logger.debug(f"TTS: no native voice for lang='{lang}' — keeping current voice.")
            return
        try:
            self.engine.setProperty("voice", voice_id)
            self._active_lang = lang
            logger.debug(f"TTS: voice switched for lang='{lang}'")
        except Exception as e:
            logger.debug(f"TTS: voice switch failed for lang='{lang}': {e}")

    def _speak_sync(self, text: str, lang: str | None = None) -> None:
        """Síntesis y reproducción bloqueante — ejecutada dentro del worker thread.

        v35.0: split en frases para permitir interrupción fina entre cada una.
        engine.stop() se llama desde el mismo thread (no thread-safe externamente).
        V62.0: aplica el voice-routing por idioma antes de sintetizar — el
        cambio de voz de pyttsx3/SAPI debe ocurrir en el mismo thread que
        posee el engine.
        """
        if not text.strip():
            return
        self._apply_voice(lang)
        logger.info(f"TTS → '{text[:60]}{'...' if len(text) > 60 else ''}'")

        sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
        if not sentences:
            sentences = [text]

        for sentence in sentences:
            # v35.0 — check cancel between sentences for sub-second preempt
            if (_cancel_bus.tts_cancel is not None
                    and _cancel_bus.tts_cancel.is_set()):
                logger.info("TTS: mid-speech interrupt — engine.stop()")
                try:
                    self.engine.stop()
                except Exception:
                    pass
                self._interrupted = True
                return
            try:
                self.engine.say(sentence)
                self.engine.runAndWait()
            except Exception as e:
                logger.debug(f"TTS: engine error on sentence: {e}")
                return

    async def _worker(self) -> None:
        """
        Corrutina worker: desencola texto de la asyncio.Queue y lo sintetiza
        en el ThreadPoolExecutor dedicado para no bloquear el event loop.

        v35.0: revisa tts_cancel antes de cada item — descarta y limpia queue.
        """
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            if item is None:  # sentinel de parada
                self._queue.task_done()
                break
            text, lang = item

            # v35.0 — pre-speech cancel check
            if (_cancel_bus.tts_cancel is not None
                    and _cancel_bus.tts_cancel.is_set()):
                logger.debug("TTS: cancelled before speech — draining queue")
                self._drain_queue_nowait()
                _cancel_bus.tts_cancel.clear()
                self._queue.task_done()
                continue

            self._interrupted = False
            try:
                await loop.run_in_executor(self._executor, self._speak_sync, text, lang)
            finally:
                self._queue.task_done()

            # If interrupted mid-speech, drain remaining queue
            if self._interrupted:
                self._drain_queue_nowait()
                if (_cancel_bus.tts_cancel is not None
                        and _cancel_bus.tts_cancel.is_set()):
                    _cancel_bus.tts_cancel.clear()

    def _drain_queue_nowait(self) -> int:
        """Drop all pending items in the queue without blocking. Returns count."""
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except Exception:
                break
        if dropped:
            logger.info(f"TTS: dropped {dropped} pending utterance(s)")
        return dropped

    async def speak_async(self, text: str, lang: str | None = None) -> None:
        """
        Encola el texto (y opcionalmente un hint de idioma — V62.0 Phase 1
        TTSVoiceRouter — para elegir voz antes de sintetizar). El worker thread
        maneja la reproducción en paralelo, permitiendo que el LLM continúe
        generando mientras el audio suena. lang=None preserva el comportamiento
        previo (sin cambio de voz).
        """
        if not text.strip():
            return
        # Iniciar el worker la primera vez (o si terminó de forma inesperada)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
        await self._queue.put((text, lang))

    def speak(self, text: str) -> None:
        """Síntesis sincrónica directa — para uso fuera del event loop."""
        self._speak_sync(text)

    def interrupt(self) -> None:
        """
        v35.0 — silencio instantáneo del TTS.
        Llama a engine.stop() (no thread-safe → solo desde el thread que
        construyó el engine, o desde el worker thread vía cancel_bus).
        El worker thread chequeará el flag en el próximo split de frase.
        """
        self._interrupted = True
        self._drain_queue_nowait()
        if _cancel_bus.tts_cancel is not None and not _cancel_bus.tts_cancel.is_set():
            _cancel_bus.tts_cancel.set()
        logger.info("TTS: interrupt requested — output silencing")

    async def drain(self) -> None:
        """Espera a que la queue quede vacía (útil al finalizar un turno)."""
        await self._queue.join()

    async def stop(self) -> None:
        """Detiene el worker y libera el ThreadPoolExecutor."""
        if self._worker_task and not self._worker_task.done():
            await self._queue.put(None)
            await self._worker_task
        self._executor.shutdown(wait=False)
