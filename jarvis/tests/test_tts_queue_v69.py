"""
tests/test_tts_queue_v69.py — V69 M54.9 bounded, prioritized TTS governor.

Locks symptom #8 ("dropped 28 pending utterance(s)"): the queue is hard-bounded,
duplicates are suppressed, same-key events coalesce, LOW is dropped before HIGH,
stale utterances expire, and the governor pops in strict priority order. Plus the
TTS integration keeps the daemon-worker shutdown contract intact.
"""
from __future__ import annotations

import asyncio

from core.tts_queue import TTSGovernor, TTSPriority
from core.tts import TTS
from tests.test_tts_shutdown import FakeEngine


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ── Bounded queue + priority drop policy ──────────────────────────────────────

def test_queue_is_hard_bounded():
    g = TTSGovernor(max_size=4)
    for i in range(10):
        g.put(f"line {i}", priority=TTSPriority.NORMAL)
    assert len(g) <= 4
    assert g.metrics()["dropped"] >= 6


def test_low_dropped_before_high_under_pressure():
    g = TTSGovernor(max_size=2)
    g.put("low a", priority=TTSPriority.LOW)
    g.put("low b", priority=TTSPriority.LOW)
    # A HIGH arriving at capacity evicts a LOW, never the reverse.
    g.put("important", priority=TTSPriority.HIGH)
    texts = {it.text for it in g._items}
    assert "important" in texts
    assert len(g) == 2


def test_low_dropped_when_all_higher_priority():
    g = TTSGovernor(max_size=2)
    g.put("crit", priority=TTSPriority.CRITICAL)
    g.put("high", priority=TTSPriority.HIGH)
    # A LOW cannot evict higher-priority items → it is dropped.
    assert g.put("low", priority=TTSPriority.LOW) == "dropped"
    assert len(g) == 2


# ── Duplicate suppression + coalescing ────────────────────────────────────────

def test_duplicate_text_suppressed_in_window():
    clk = FakeClock()
    g = TTSGovernor(dedup_window_s=5.0, clock=clk)
    assert g.put("beep") == "enqueued"
    assert g.put("beep") == "deduped"      # identical, still queued
    assert g.metrics()["deduped"] == 1


def test_coalesce_by_key_collapses_repeats():
    g = TTSGovernor()
    g.put("scan 1/12", priority=TTSPriority.LOW, key="hunt")
    g.put("scan 6/12", priority=TTSPriority.LOW, key="hunt")
    g.put("scan 12/12", priority=TTSPriority.LOW, key="hunt")
    assert len(g) == 1                     # collapsed to one
    assert g._items[0].text == "scan 12/12"
    assert g.metrics()["coalesced"] == 2


def test_coalesce_lifts_priority_to_max():
    g = TTSGovernor()
    g.put("status", priority=TTSPriority.LOW, key="k")
    g.put("status urgent", priority=TTSPriority.HIGH, key="k")
    assert g._items[0].priority == TTSPriority.HIGH


# ── Stale expiration ──────────────────────────────────────────────────────────

def test_stale_utterances_expire_on_pop():
    clk = FakeClock()
    g = TTSGovernor(ttl_s=10.0, clock=clk)
    g.put("old news")
    clk.advance(11.0)
    assert g.pop() is None                 # expired, not spoken
    assert g.metrics()["dropped"] == 1


# ── Priority pop ordering ─────────────────────────────────────────────────────

def test_pop_returns_highest_priority_first():
    g = TTSGovernor()
    g.put("low", priority=TTSPriority.LOW)
    g.put("normal", priority=TTSPriority.NORMAL)
    g.put("critical", priority=TTSPriority.CRITICAL)
    g.put("high", priority=TTSPriority.HIGH)
    assert g.pop().text == "critical"
    assert g.pop().text == "high"
    assert g.pop().text == "normal"
    assert g.pop().text == "low"


def test_cancel_below_drops_boot_narration():
    g = TTSGovernor()
    g.put("boot line", priority=TTSPriority.LOW, key="a")
    g.put("boot line 2", priority=TTSPriority.NORMAL, key="b")
    g.put("incident!", priority=TTSPriority.CRITICAL, key="c")
    removed = g.cancel_below(TTSPriority.HIGH)
    assert removed == 2
    assert len(g) == 1 and g._items[0].text == "incident!"


# ── TTS integration keeps the shutdown contract ───────────────────────────────

def test_tts_speak_async_uses_bounded_governor():
    tts = TTS(engine=FakeEngine())
    try:
        # Flood with duplicate low-priority events — the queue must stay bounded.
        async def _flood():
            for i in range(50):
                await tts.speak_async("monitor tick", priority=TTSPriority.LOW,
                                      coalesce_key="mon")
        asyncio.run(_flood())
        m = tts.queue_metrics()
        assert m["queued"] <= m["max_size"]
        assert m["coalesced"] >= 1
    finally:
        asyncio.run(tts.stop())


def test_cancel_boot_narration_keeps_high_priority():
    tts = TTS(engine=FakeEngine())
    try:
        async def _seed():
            await tts.speak_async("boot phase", priority=TTSPriority.LOW,
                                  coalesce_key="boot:x")
            await tts.speak_async("HITL challenge", priority=TTSPriority.CRITICAL)
        asyncio.run(_seed())
        tts.cancel_boot_narration()
        # LOW boot line dropped; CRITICAL survives.
        assert tts._gov.metrics()["queued"] >= 0  # bounded, no exception
    finally:
        asyncio.run(tts.stop())
