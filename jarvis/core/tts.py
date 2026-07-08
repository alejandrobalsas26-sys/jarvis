"""
core/tts.py — Text-to-Speech 100% local via pyttsx3.

v3: speak_async solo encola texto en asyncio.Queue. Un worker en ThreadPoolExecutor
dedicado desencola y ejecuta engine.say() + runAndWait() en segundo plano,
evitando saturar el event loop principal mientras el LLM sigue razonando.

v35.0: interrupción instantánea — drain queue + engine.stop() al recibir
señal del cancel_bus. Sub-frase splitting para preempt granular.
"""

import asyncio
import queue
import re
import threading
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
    def __init__(self, engine=None):
        # engine injection is a test seam — production passes nothing and gets a
        # real pyttsx3/SAPI engine; tests inject a fake to exercise shutdown
        # without audio hardware.
        if engine is None:
            import pyttsx3
            engine = pyttsx3.init()

        self.engine = engine
        self._voice_router = TTSVoiceRouter(self.engine.getProperty("voices"))
        self._active_lang: str | None = None
        self._configure_engine()

        # V66.1 shutdown reliability: a single DEDICATED DAEMON worker thread
        # drains a thread-safe job queue. Daemon (unlike the old non-daemon
        # ThreadPoolExecutor) means a stuck engine.runAndWait() can NEVER block
        # interpreter exit or leave an orphaned "tts-worker_0" behind — no 80s
        # atexit join, no force-exit required. The engine is only ever touched
        # from this one thread (guarded by _engine_lock), so it stays the single
        # owner of the non-thread-safe SAPI engine.
        self._jobs: "queue.Queue[tuple[str, str | None] | None]" = queue.Queue()
        self._engine_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._interrupted: bool = False
        self._busy: bool = False
        self._closed: bool = False
        self._worker = threading.Thread(
            target=self._run_worker, name="tts-worker", daemon=True,
        )
        self._worker.start()
        logger.info("TTS: pyttsx3 offline activo (daemon worker).")

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
        V66.1: engine access is serialized under _engine_lock so a rare direct
        speak() call can't race the daemon worker on the single SAPI engine.
        """
        if not text.strip():
            return
        self._busy = True
        try:
            self._apply_voice(lang)
            logger.info(f"TTS → '{text[:60]}{'...' if len(text) > 60 else ''}'")

            sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
            if not sentences:
                sentences = [text]

            for sentence in sentences:
                # v35.0 — check cancel between sentences for sub-second preempt.
                # Also honor a graceful stop() so an in-flight utterance ends fast.
                if self._closed or (
                    _cancel_bus.tts_cancel is not None
                    and _cancel_bus.tts_cancel.is_set()
                ):
                    logger.info("TTS: mid-speech interrupt — engine.stop()")
                    with self._engine_lock:
                        try:
                            self.engine.stop()
                        except Exception:
                            pass
                    self._interrupted = True
                    return
                try:
                    with self._engine_lock:
                        self.engine.say(sentence)
                        self.engine.runAndWait()
                except Exception as e:
                    logger.debug(f"TTS: engine error on sentence: {e}")
                    return
        finally:
            self._busy = False

    def _run_worker(self) -> None:
        """Dedicated daemon-thread loop: drain the job queue and synthesize.

        v35.0: check tts_cancel before each item — drop and clear the queue.
        The ONLY exits are the None sentinel (graceful stop) or the process
        ending (daemon → force-killed, never joined). No asyncio here: the queue
        is thread-safe, so speak_async never blocks the event loop.
        """
        while True:
            item = self._jobs.get()
            try:
                if item is None:                     # sentinel de parada
                    return
                if self._closed:
                    continue
                text, lang = item

                # v35.0 — pre-speech cancel check
                if (_cancel_bus.tts_cancel is not None
                        and _cancel_bus.tts_cancel.is_set()):
                    logger.debug("TTS: cancelled before speech — draining queue")
                    self._drain_jobs()
                    _cancel_bus.tts_cancel.clear()
                    continue

                self._interrupted = False
                try:
                    self._speak_sync(text, lang)
                except Exception as e:  # worker must never die on a bad utterance
                    logger.debug(f"TTS: worker error: {e}")

                # If interrupted mid-speech, drain remaining queue
                if self._interrupted:
                    self._drain_jobs()
                    if (_cancel_bus.tts_cancel is not None
                            and _cancel_bus.tts_cancel.is_set()):
                        _cancel_bus.tts_cancel.clear()
            finally:
                self._jobs.task_done()

    def _drain_jobs(self) -> int:
        """Drop all pending items in the queue without blocking. Returns count.
        Re-injects nothing — a None sentinel already consumed is not re-queued."""
        dropped = 0
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                break
            self._jobs.task_done()
            if item is None:            # preserve stop semantics if sentinel drained
                self._jobs.put_nowait(None)
                break
            dropped += 1
        if dropped:
            logger.info(f"TTS: dropped {dropped} pending utterance(s)")
        return dropped

    async def speak_async(self, text: str, lang: str | None = None) -> None:
        """
        Encola el texto (y opcionalmente un hint de idioma — V62.0 Phase 1
        TTSVoiceRouter — para elegir voz antes de sintetizar). El daemon worker
        thread reproduce en paralelo, permitiendo que el LLM continúe generando
        mientras el audio suena. Enqueue es no bloqueante (queue.Queue), así que
        nunca frena el event loop. lang=None preserva el comportamiento previo.
        """
        if not text.strip() or self._closed:
            return
        self._jobs.put_nowait((text, lang))

    def speak(self, text: str) -> None:
        """Síntesis sincrónica directa — para uso fuera del event loop."""
        if self._closed:
            return
        self._speak_sync(text)

    def interrupt(self) -> None:
        """
        v35.0 — silencio instantáneo del TTS.
        Drena la cola y señala cancelación vía cancel_bus; el worker thread
        chequea el flag en el próximo split de frase para cortar el audio activo.
        """
        self._interrupted = True
        self._drain_jobs()
        if _cancel_bus.tts_cancel is not None and not _cancel_bus.tts_cancel.is_set():
            _cancel_bus.tts_cancel.set()
        logger.info("TTS: interrupt requested — output silencing")

    async def drain(self) -> None:
        """Espera (acotado) a que la cola se vacíe y termine el audio en curso —
        útil al finalizar un turno. Bounded so a hung engine can't wedge a turn."""
        for _ in range(600):  # ~30s ceiling at 50ms granularity
            if self._jobs.empty() and not self._busy:
                return
            await asyncio.sleep(0.05)

    def _teardown(self) -> None:
        """Idempotent, non-blocking teardown primitive shared by stop()/stop_sync().
        Cancels active + queued speech and signals the daemon worker to exit."""
        with self._stop_lock:
            if self._closed:
                return
            self._closed = True
        self._interrupted = True
        self._drain_jobs()
        try:
            if (_cancel_bus.tts_cancel is not None
                    and not _cancel_bus.tts_cancel.is_set()):
                _cancel_bus.tts_cancel.set()
        except Exception:
            pass
        # Best-effort break of an in-flight runAndWait(). engine.stop() is the
        # cross-thread interrupt primitive — it is called WITHOUT _engine_lock on
        # purpose: the worker holds that lock for the duration of runAndWait(), so
        # acquiring it here would deadlock out the very signal meant to break the
        # wedge. That is exactly why the old ThreadPoolExecutor teardown could
        # hang for ~80s on a stuck utterance.
        try:
            self.engine.stop()
        except Exception:
            pass
        # Wake the worker so it observes _closed / consumes the sentinel.
        try:
            self._jobs.put_nowait(None)
        except Exception:
            pass

    async def stop(self) -> None:
        """Graceful, bounded, idempotent shutdown (registered shutdown callback).

        The worker is a daemon thread, so even if a stuck engine.runAndWait()
        ignores the stop signal the interpreter still exits cleanly — no orphaned
        non-daemon 'tts-worker', no 80s atexit join, no force-exit needed. The
        join below is a courtesy with a short ceiling, not a correctness
        dependency.
        """
        self._teardown()
        for _ in range(40):  # ~2s courtesy join
            if not self._worker.is_alive():
                break
            await asyncio.sleep(0.05)

    def stop_sync(self) -> None:
        """Synchronous teardown for non-async call sites (e.g. voice loop's
        _stop_tts, which cannot await). Idempotent and non-blocking."""
        self._teardown()
