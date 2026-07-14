"""
core/tts_queue.py — V69 M54.9: bounded, prioritized TTS utterance governor.

Shutdown reported "TTS: dropped 28 pending utterance(s)" because every boot phase
and background subsystem pushed onto an unbounded FIFO faster than pyttsx3 could
speak (symptom #8). This module is the policy layer that keeps that queue small and
meaningful: it is a pure, deterministic, audio-free governor that TTS enqueues into.

Guarantees:
  * hard maximum size — the queue can never grow without bound;
  * four priorities (CRITICAL / HIGH / NORMAL / LOW) with strict pop ordering;
  * duplicate suppression — identical text within a short window is dropped;
  * coalescing by event key — repeated same-key events collapse to the latest;
  * stale expiration — an utterance older than its TTL is skipped on pop;
  * drop policy — LOW is dropped before NORMAL/HIGH/CRITICAL under pressure;
  * cancellation of obsolete narration (drop everything below a priority);
  * bounded shutdown drain — keep only CRITICAL/HIGH, drop the rest;
  * metrics — enqueued / spoken / dropped / coalesced / deduped counts.

Injectable `clock` for deterministic tests. Not thread-safe by itself; TTS owns the
lock around it (the governor is a data-structure, the engine owns concurrency).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum


class TTSPriority(IntEnum):
    """Higher value = spoken first, dropped last."""

    LOW = 0        # background info, repeated monitor status, optional integrations
    NORMAL = 1     # concise boot status, direct assistant response
    HIGH = 2       # operator-requested answer, important readiness warning
    CRITICAL = 3   # HITL challenge, critical incident, shutdown error


@dataclass
class TTSUtterance:
    text: str
    lang: str | None
    priority: TTSPriority
    key: str | None
    enqueued_at: float
    expires_at: float | None

    def is_stale(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


# Default governance parameters tuned for the CPU host: a short queue (speech is
# slow, so a long backlog is always stale by the time it plays) and a modest TTL.
_DEFAULT_MAX = 12
_DEFAULT_TTL_S = 20.0
_DEFAULT_DEDUP_WINDOW_S = 6.0


@dataclass
class TTSGovernor:
    max_size: int = _DEFAULT_MAX
    ttl_s: float = _DEFAULT_TTL_S
    dedup_window_s: float = _DEFAULT_DEDUP_WINDOW_S
    clock: "callable" = time.monotonic

    _items: list = field(default_factory=list)
    # text -> last-enqueued time, for duplicate suppression across a short window.
    _recent_text: dict = field(default_factory=dict)
    # counters
    enqueued: int = 0
    spoken: int = 0
    dropped: int = 0
    coalesced: int = 0
    deduped: int = 0

    # ── Enqueue ──────────────────────────────────────────────────────────────
    def put(self, text: str, *, lang: str | None = None,
            priority: TTSPriority = TTSPriority.NORMAL,
            key: str | None = None) -> str:
        """Admit an utterance under policy. Returns the action taken:
        'enqueued' | 'coalesced' | 'deduped' | 'dropped'. Never raises, never
        blocks, never exceeds max_size."""
        text = (text or "").strip()
        if not text:
            return "dropped"
        now = self.clock()
        self._expire(now)

        # 1. Coalesce: an unspoken item with the same key collapses to this one
        #    (latest text wins; priority lifts to the max of the two).
        if key is not None:
            for it in self._items:
                if it.key == key:
                    it.text = text
                    it.lang = lang
                    it.priority = TTSPriority(max(it.priority, priority))
                    it.enqueued_at = now
                    it.expires_at = now + self.ttl_s
                    self.coalesced += 1
                    return "coalesced"

        # 2. Duplicate suppression: identical text spoken/queued within the window.
        last = self._recent_text.get(text)
        if last is not None and (now - last) < self.dedup_window_s:
            self.deduped += 1
            return "deduped"
        if any(it.text == text for it in self._items):
            self.deduped += 1
            return "deduped"

        item = TTSUtterance(
            text=text, lang=lang, priority=priority, key=key,
            enqueued_at=now, expires_at=now + self.ttl_s,
        )

        # 3. Backpressure: at capacity, evict the lowest-priority OLDEST item that
        #    is no higher-priority than the incoming one. If nothing qualifies, the
        #    incoming (lowest) item is dropped instead of overflowing.
        if len(self._items) >= self.max_size:
            if not self._evict_for(item):
                self.dropped += 1
                return "dropped"

        self._items.append(item)
        self._recent_text[text] = now
        self.enqueued += 1
        return "enqueued"

    def _evict_for(self, incoming: TTSUtterance) -> bool:
        """Drop one existing item to make room for *incoming*. Prefer the
        lowest-priority, then oldest. Only evict an item whose priority is <= the
        incoming priority (so a CRITICAL is never dropped to admit a LOW)."""
        candidate = None
        for it in self._items:
            if it.priority > incoming.priority:
                continue
            if candidate is None or (it.priority, it.enqueued_at) < (
                candidate.priority, candidate.enqueued_at
            ):
                candidate = it
        if candidate is None:
            return False
        self._items.remove(candidate)
        self.dropped += 1
        return True

    # ── Dequeue ──────────────────────────────────────────────────────────────
    def pop(self) -> TTSUtterance | None:
        """Return the highest-priority, non-stale utterance (ties: oldest first),
        removing it. Stale items encountered are discarded (counted as dropped).
        Returns None when empty."""
        now = self.clock()
        self._expire(now)
        if not self._items:
            return None
        best = None
        for it in self._items:
            if best is None or (it.priority, -it.enqueued_at) > (
                best.priority, -best.enqueued_at
            ):
                best = it
        self._items.remove(best)
        self.spoken += 1
        return best

    def _expire(self, now: float) -> None:
        if not self._items:
            return
        fresh = []
        for it in self._items:
            if it.is_stale(now):
                self.dropped += 1
            else:
                fresh.append(it)
        self._items = fresh

    # ── Cancellation / shutdown ──────────────────────────────────────────────
    def cancel_below(self, priority: TTSPriority) -> int:
        """Drop every queued item strictly below *priority*. Used to cancel
        obsolete boot narration once text interaction begins, and to bound the
        shutdown drain (keep only CRITICAL/HIGH). Returns the count dropped."""
        before = len(self._items)
        self._items = [it for it in self._items if it.priority >= priority]
        removed = before - len(self._items)
        self.dropped += removed
        return removed

    def clear(self) -> int:
        removed = len(self._items)
        self._items.clear()
        self.dropped += removed
        return removed

    def __len__(self) -> int:
        return len(self._items)

    def metrics(self) -> dict:
        return {
            "queued": len(self._items),
            "max_size": self.max_size,
            "enqueued": self.enqueued,
            "spoken": self.spoken,
            "dropped": self.dropped,
            "coalesced": self.coalesced,
            "deduped": self.deduped,
        }
