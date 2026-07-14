"""
tests/test_tts_shutdown.py — V66.1 Phase 8 TTS graceful-shutdown regression.

Proves the daemon-worker TTS teardown is:
  * daemon-threaded (a stuck engine.runAndWait() can never block interpreter
    exit or orphan a non-daemon 'tts-worker');
  * bounded (stop() returns promptly even with a wedged utterance — no 80s wait);
  * idempotent (repeated stop()/stop_sync() are safe);
  * correct on the queue sentinel (worker exits, active speech is cancelled);
  * distinct from barge-in interrupt() (interrupt keeps TTS alive; stop closes).

No audio hardware / pyttsx3 required — a fake engine is injected via the
TTS(engine=...) test seam.
"""
from __future__ import annotations

import asyncio
import threading
import time


from core.tts import TTS


class FakeVoice:
    def __init__(self, vid="v0"):
        self.id = vid
        self.name = "Fake Voice en-US"
        self.languages = [b"en-US"]


class FakeEngine:
    """Minimal pyttsx3-like engine. runAndWait() is instant by default; set
    block_event to simulate a long/wedged utterance."""

    def __init__(self, block_event: threading.Event | None = None):
        self._props = {"voices": [FakeVoice()]}
        self.block_event = block_event
        self.said: list[str] = []
        self.stopped = 0
        self.entered = threading.Event()   # set once inside runAndWait

    def getProperty(self, key):
        return self._props.get(key)

    def setProperty(self, key, value):
        self._props[key] = value

    def say(self, text):
        self.said.append(text)

    def runAndWait(self):
        self.entered.set()
        if self.block_event is not None:
            # Simulate a long/stuck SAPI call: wait until released or stop().
            self.block_event.wait(timeout=5.0)

    def stop(self):
        self.stopped += 1
        if self.block_event is not None:
            self.block_event.set()   # a real engine.stop() breaks runAndWait


def _live_tts_workers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("tts-worker")]


def test_worker_is_daemon_thread():
    tts = TTS(engine=FakeEngine())
    try:
        assert tts._worker.is_alive()
        assert tts._worker.daemon is True, "TTS worker MUST be a daemon thread"
        assert tts._worker.name == "tts-worker"
    finally:
        asyncio.run(tts.stop())


def test_graceful_stop_is_bounded_and_idempotent():
    tts = TTS(engine=FakeEngine())
    t0 = time.monotonic()
    asyncio.run(tts.stop())
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"stop() should be bounded, took {elapsed:.1f}s"
    assert tts._closed is True
    # Idempotent: second stop (async) and stop_sync() must not raise/hang.
    asyncio.run(tts.stop())
    tts.stop_sync()
    assert not tts._worker.is_alive()


def test_stop_leaves_no_nondaemon_worker_even_when_utterance_wedged():
    block = threading.Event()
    engine = FakeEngine(block_event=block)
    tts = TTS(engine=engine)
    try:
        # Enqueue an utterance and wait until the worker is inside runAndWait().
        asyncio.run(tts.speak_async("a wedged utterance"))
        assert engine.entered.wait(timeout=2.0), "worker never started speaking"

        t0 = time.monotonic()
        asyncio.run(tts.stop())          # must be bounded despite the wedge
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"stop() not bounded under wedge: {elapsed:.1f}s"

        # engine.stop() was invoked to break the active utterance.
        assert engine.stopped >= 1
        # Any surviving tts-worker thread MUST be a daemon (never blocks exit).
        for w in _live_tts_workers():
            assert w.daemon is True
    finally:
        block.set()


def test_stop_sync_idempotent_and_closes():
    tts = TTS(engine=FakeEngine())
    tts.stop_sync()
    tts.stop_sync()          # second call is a no-op, must not raise
    assert tts._closed is True
    # speak_async after close is a no-op (nothing enqueued).
    asyncio.run(tts.speak_async("ignored"))
    assert len(tts._gov) == 0


def test_interrupt_keeps_engine_alive():
    tts = TTS(engine=FakeEngine())
    try:
        tts.interrupt()                  # barge-in, NOT shutdown
        assert tts._closed is False
        assert tts._worker.is_alive()
        # TTS still accepts new utterances after a barge-in.
        asyncio.run(tts.speak_async("still alive"))
        # drain is bounded and returns once the queue empties.
        asyncio.run(asyncio.wait_for(tts.drain(), timeout=5.0))
    finally:
        asyncio.run(tts.stop())


def test_sentinel_stops_worker_cleanly():
    tts = TTS(engine=FakeEngine())
    asyncio.run(tts.speak_async("hello"))
    asyncio.run(tts.drain())             # let the utterance flush
    asyncio.run(tts.stop())
    # Worker consumed the sentinel and exited.
    for _ in range(40):
        if not tts._worker.is_alive():
            break
        time.sleep(0.05)
    assert not tts._worker.is_alive()
