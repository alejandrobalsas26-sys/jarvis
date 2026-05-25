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


class TTS:
    def __init__(self):
        import pyttsx3

        self.engine = pyttsx3.init()
        self._configure_engine()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-worker")
        self._worker_task: asyncio.Task | None = None
        self._interrupted: bool = False
        logger.info("TTS: pyttsx3 offline activo (modo queue asíncrona).")

    def _configure_engine(self) -> None:
        self.engine.setProperty("rate", 165)
        self.engine.setProperty("volume", 1.0)
        for voice in self.engine.getProperty("voices"):
            if "es" in voice.id.lower() or "spanish" in voice.name.lower():
                self.engine.setProperty("voice", voice.id)
                logger.debug(f"Voz seleccionada: {voice.name}")
                break

    def _speak_sync(self, text: str) -> None:
        """Síntesis y reproducción bloqueante — ejecutada dentro del worker thread.

        v35.0: split en frases para permitir interrupción fina entre cada una.
        engine.stop() se llama desde el mismo thread (no thread-safe externamente).
        """
        if not text.strip():
            return
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
            text = await self._queue.get()
            if text is None:  # sentinel de parada
                self._queue.task_done()
                break

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
                await loop.run_in_executor(self._executor, self._speak_sync, text)
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

    async def speak_async(self, text: str) -> None:
        """
        Solo encola el texto. El worker thread maneja la reproducción en paralelo,
        permitiendo que el LLM continúe generando mientras el audio suena.
        """
        if not text.strip():
            return
        # Iniciar el worker la primera vez (o si terminó de forma inesperada)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
        await self._queue.put(text)

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
