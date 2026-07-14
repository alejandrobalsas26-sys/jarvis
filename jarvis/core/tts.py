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
import threading
from loguru import logger

import core.cancel_bus as _cancel_bus
# V69 M54.9 — bounded, prioritized, dedup/coalescing utterance governor.
from core.tts_queue import TTSGovernor, TTSPriority

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
        # drains a bounded, prioritized governor. Daemon (unlike the old non-daemon
        # ThreadPoolExecutor) means a stuck engine.runAndWait() can NEVER block
        # interpreter exit or leave an orphaned "tts-worker_0" behind — no 80s
        # atexit join, no force-exit required. The engine is only ever touched
        # from this one thread (guarded by _engine_lock), so it stays the single
        # owner of the non-thread-safe SAPI engine.
        #
        # V69 M54.9 — the governor bounds the queue and applies priority / dedup /
        # coalescing / backpressure so boot narration and background alerts can no
        # longer flood dozens of stale utterances (the "dropped 28 pending" bug).
        # A Condition wakes the worker on enqueue / close (no busy-wait, no
        # unbounded FIFO).
        self._gov = TTSGovernor()
        self._cv = threading.Condition()
        self._engine_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._interrupted: bool = False
        self._busy: bool = False
        self._closed: bool = False
        self._worker = threading.Thread(
            target=self._run_worker, name="tts-worker", daemon=True,
        )
        self._worker.start()
        logger.info("TTS: pyttsx3 offline activo (daemon worker, bounded governor).")

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
        """Dedicated daemon-thread loop: pop the highest-priority utterance and
        synthesize. Waits on a Condition (no busy-wait). The ONLY exits are
        `_closed` with an empty governor (graceful stop) or the process ending
        (daemon → force-killed, never joined).
        """
        while True:
            with self._cv:
                while not self._closed and len(self._gov) == 0:
                    self._cv.wait(timeout=0.5)
                if self._closed and len(self._gov) == 0:
                    return
                item = self._gov.pop()
            if item is None:
                continue
            if self._closed:
                continue

            # v35.0 — pre-speech cancel check
            if (_cancel_bus.tts_cancel is not None
                    and _cancel_bus.tts_cancel.is_set()):
                logger.debug("TTS: cancelled before speech — draining queue")
                self._drain_jobs()
                _cancel_bus.tts_cancel.clear()
                continue

            self._interrupted = False
            try:
                self._speak_sync(item.text, item.lang)
            except Exception as e:  # worker must never die on a bad utterance
                logger.debug(f"TTS: worker error: {e}")

            # If interrupted mid-speech, drain remaining queue
            if self._interrupted:
                self._drain_jobs()
                if (_cancel_bus.tts_cancel is not None
                        and _cancel_bus.tts_cancel.is_set()):
                    _cancel_bus.tts_cancel.clear()

    def _drain_jobs(self) -> int:
        """Drop all pending governor items without blocking. Returns count."""
        with self._cv:
            dropped = self._gov.clear()
        if dropped:
            logger.info(f"TTS: dropped {dropped} pending utterance(s)")
        return dropped

    async def speak_async(
        self,
        text: str,
        lang: str | None = None,
        *,
        priority: TTSPriority = TTSPriority.NORMAL,
        coalesce_key: str | None = None,
    ) -> None:
        """
        Admit text into the bounded governor (with an optional language hint for
        voice routing, a priority, and a coalescing key). The daemon worker speaks
        in priority order in parallel with LLM generation. Enqueue is non-blocking
        and bounded — it never floods, never grows without limit, and never frees
        the event loop for more than a lock acquisition. lang=None and the default
        NORMAL priority preserve the previous behavior for existing callers.
        """
        if not text.strip() or self._closed:
            return
        with self._cv:
            self._gov.put(text, lang=lang, priority=priority, key=coalesce_key)
            self._cv.notify()

    def cancel_boot_narration(self) -> int:
        """Drop all queued NORMAL/LOW narration (boot status, background info),
        keeping only HIGH/CRITICAL. Called once text interaction begins so pending
        cinematic boot lines don't keep speaking over the operator (M54.9)."""
        with self._cv:
            removed = self._gov.cancel_below(TTSPriority.HIGH)
        if removed:
            logger.debug(f"TTS: cancelled {removed} obsolete boot/background utterance(s)")
        return removed

    def queue_metrics(self) -> dict:
        """Bounded governor metrics for runtime health (dropped/coalesced/…)."""
        with self._cv:
            return self._gov.metrics()

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
            if len(self._gov) == 0 and not self._busy:
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
        # Wake the worker so it observes _closed and exits (no sentinel needed —
        # the Condition + _closed flag drive the loop's only graceful exit).
        with self._cv:
            self._cv.notify_all()

    async def stop(self) -> None:
        """Graceful, bounded, idempotent shutdown (registered shutdown callback).

        M54.9 — drop NORMAL/LOW narration immediately, give any queued HIGH/CRITICAL
        speech a short bounded window to play, then tear down. The worker is a daemon
        thread, so even if a stuck engine.runAndWait() ignores the stop signal the
        interpreter still exits cleanly — no orphaned non-daemon 'tts-worker', no 80s
        atexit join, no force-exit needed.
        """
        with self._cv:
            self._gov.cancel_below(TTSPriority.HIGH)
            self._cv.notify_all()
        for _ in range(20):  # ~1s bounded drain of remaining high-priority speech
            with self._cv:
                pending = len(self._gov)
            if pending == 0 and not self._busy:
                break
            await asyncio.sleep(0.05)
        self._teardown()
        for _ in range(40):  # ~2s courtesy join
            if not self._worker.is_alive():
                break
            await asyncio.sleep(0.05)

    def stop_sync(self) -> None:
        """Synchronous teardown for non-async call sites (e.g. voice loop's
        _stop_tts, which cannot await). Idempotent and non-blocking."""
        self._teardown()
