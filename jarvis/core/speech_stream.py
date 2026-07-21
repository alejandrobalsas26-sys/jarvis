"""core/speech_stream.py — V69 M57.4/.4.1: progressive speech + backpressure.

WHAT CHANGES
------------
Speech was already sentence-by-sentence, but three things made it wrong:

  * every assistant sentence was enqueued at ``TTSPriority.NORMAL`` with no
    coalescing key — exactly as droppable as boot narration, so
    ``cancel_boot_narration()`` (which drops everything below HIGH) silently killed
    the operator's own answer;
  * segmentation came from ``(?<=[.!?;:])\\s+``, which spoke "10" and "30" out of
    "10:30" and read every line of a code block aloud;
  * nothing bounded the SPEECH backlog against generation. TTS at ~165 wpm is
    slower than the model on long answers, so speech drifts further behind the text
    with every sentence and ends up narrating a paragraph the operator read a
    minute ago.

WHAT THIS MODULE IS
-------------------
A PURE planner: it turns :class:`~core.stream_assembler.Fragment`s into bounded
speech instructions. It owns no thread, no queue and no engine — the existing
``core.tts.TTS`` + ``core.tts_queue.TTSQueue`` remain the only speech worker. The
caller applies the returned :class:`SpeechInstruction`s.

TEXT IS NEVER ALTERED TO SUIT SPEECH
------------------------------------
When speech falls behind, speech is shortened — never the answer. The displayed
text is always complete; truncation is reported through health counters, not
through a user-facing warning.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from core.response_contract import SpeechPolicy
from core.stream_assembler import Fragment, FragmentKind
from core.tts_queue import TTSPriority

# Utterance shaping bounds.
_MIN_SPEAK_CHARS = 2          # "Sí." is a legitimate utterance; "-" is not
_MAX_UTTERANCE_CHARS = 240    # one pyttsx3 job; longer blocks preemption granularity
_LEAD_SENTENCES = 3           # SPEAK_LEAD: how much of a long answer is spoken
_RECENT_DEDUP = 8             # bounded memory of what was already spoken

# Clause separators used to split an over-long sentence safely.
_CLAUSE_RE = re.compile(r"(?<=[,;:])\s+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_SPEAKABLE_RE = re.compile(r"[0-9A-Za-zÁÉÍÓÚÑÜáéíóúñü]")


@dataclass(frozen=True)
class SpeechInstruction:
    """One bounded utterance to hand to the existing TTS queue."""

    text: str
    priority: TTSPriority
    coalesce_key: str | None = None
    reason: str = "sentence"

    def telemetry(self) -> dict:
        return {"priority": int(self.priority), "reason": self.reason,
                "chars": len(self.text)}


@dataclass
class SpeechMetrics:
    """Bounded, content-free speech metrics for runtime health."""

    progressive_enabled: bool = True
    first_utterance_ms: float | None = None
    queued: int = 0
    suppressed_duplicates: int = 0
    stale_dropped: int = 0
    split_utterances: int = 0
    code_skipped: int = 0
    muted_skipped: int = 0
    high_watermark: int = 0
    current_depth: int = 0

    def snapshot(self) -> dict:
        return {
            "progressive_enabled": self.progressive_enabled,
            "first_utterance_ms": self.first_utterance_ms,
            "queued": self.queued,
            "suppressed_duplicates": self.suppressed_duplicates,
            "stale_dropped": self.stale_dropped,
            "split_utterances": self.split_utterances,
            "code_skipped": self.code_skipped,
            "muted_skipped": self.muted_skipped,
            "current_depth": self.current_depth,
            "high_watermark": self.high_watermark,
        }


def speakable_text(raw: str) -> str:
    """Strip everything that would be nonsense when spoken.

    Reuses :func:`core.response_surface.strip_markup` (the existing VOICE surface)
    so there is ONE markup-stripping implementation, then removes bare URLs, which
    a speech engine spells out character by character.
    """
    if not raw:
        return ""
    try:
        from core.response_surface import ResponseSurface, render
        out = render(raw, ResponseSurface.VOICE)
    except Exception:  # noqa: BLE001 — speech shaping never breaks a turn
        out = raw
    out = _URL_RE.sub(" ", out)
    out = " ".join(out.split())
    return out.strip()


def split_long_utterance(text: str, limit: int = _MAX_UTTERANCE_CHARS) -> list[str]:
    """Split an over-long sentence at clause boundaries, never mid-word.

    A single 600-character sentence is one blocking ``runAndWait()`` job, so the
    operator cannot barge in until it finishes. Splitting keeps preemption
    granularity at roughly one clause.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    current = ""
    for clause in _CLAUSE_RE.split(text):
        candidate = (current + " " + clause).strip() if current else clause.strip()
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current)
        # A single clause longer than the limit: fall back to word packing.
        if len(clause) <= limit:
            current = clause.strip()
            continue
        current = ""
        for word in clause.split():
            nxt = (current + " " + word).strip() if current else word
            if len(nxt) > limit:
                if current:
                    out.append(current)
                current = word
            else:
                current = nxt
    if current:
        out.append(current)
    return [p for p in out if p]


class SpeechPlanner:
    """Plans progressive speech for ONE turn. Pure, bounded and testable.

    ``pending`` (the live TTS queue depth) is supplied by the caller each time, so
    the planner never reaches into the speech engine.
    """

    def __init__(self, *, policy: SpeechPolicy = SpeechPolicy.SPEAK_FULL,
                 muted: bool = False, enabled: bool = True, turn_id: int = 0,
                 backlog_cap: int = 4, started_at: float = 0.0) -> None:
        self.policy = policy
        self.muted = bool(muted)
        self.enabled = bool(enabled)
        self.turn_id = int(turn_id)
        self.backlog_cap = max(1, int(backlog_cap))
        self.metrics = SpeechMetrics(progressive_enabled=bool(enabled))
        self._started_at = float(started_at)
        self._spoken_sentences = 0
        self._seq = 0
        self._recent: "deque[str]" = deque(maxlen=_RECENT_DEDUP)

    # ── internals ────────────────────────────────────────────────────────────
    def _eligible(self, fragment: Fragment) -> bool:
        if fragment.kind is FragmentKind.FINAL_STATUS:
            return True
        if fragment.in_code or fragment.kind in (FragmentKind.CODE_LINE,
                                                 FragmentKind.CODE_BLOCK_BOUNDARY):
            # Code is never spoken by default: reading indentation and punctuation
            # aloud is noise, and the operator can already read it.
            self.metrics.code_skipped += 1
            return False
        if self.policy is SpeechPolicy.SPEAK_LEAD and \
                self._spoken_sentences >= _LEAD_SENTENCES:
            return False
        return fragment.is_prose()

    def _key(self) -> str:
        self._seq += 1
        return f"answer:{self.turn_id}:{self._seq}"

    # ── public API ───────────────────────────────────────────────────────────
    def plan(self, fragment: Fragment, *, pending: int = 0,
             now: float = 0.0, final: bool = False) -> list[SpeechInstruction]:
        """Plan the speech for ONE fragment. Returns [] when nothing should be said.

        ``final`` marks the last fragment of the answer: it is the "newest
        high-value conclusion" that survives backpressure.
        """
        if not self.enabled or self.policy is SpeechPolicy.SILENT:
            return []
        if self.muted:
            self.metrics.muted_skipped += 1
            return []
        if not self._eligible(fragment):
            return []
        clean = speakable_text(getattr(fragment, "text", "") or "")
        if len(clean) < _MIN_SPEAK_CHARS or not _SPEAKABLE_RE.search(clean):
            return []
        norm = clean.lower()
        if norm in self._recent:
            self.metrics.suppressed_duplicates += 1
            return []

        status = fragment.kind is FragmentKind.FINAL_STATUS
        important = status or final
        # M57.4.1 — speech is behind. Keep the conclusion and the runtime status;
        # drop the intermediate sentence. The TEXT is untouched.
        if pending >= self.backlog_cap and not important:
            self.metrics.stale_dropped += 1
            return []

        self._recent.append(norm)
        parts = split_long_utterance(clean)
        if len(parts) > 1:
            self.metrics.split_utterances += 1
        priority = TTSPriority.CRITICAL if important else TTSPriority.HIGH
        reason = ("final_status" if status else
                  "conclusion" if final else "sentence")
        out = [SpeechInstruction(text=p, priority=priority,
                                 coalesce_key=self._key(), reason=reason)
               for p in parts]
        if out:
            self._spoken_sentences += 1
            self.metrics.queued += len(out)
            if self.metrics.first_utterance_ms is None:
                self.metrics.first_utterance_ms = round(
                    max(0.0, now - self._started_at) * 1000.0, 1)
        self.note_depth(pending + len(out))
        return out

    def note_depth(self, depth: int) -> None:
        self.metrics.current_depth = max(0, int(depth))
        self.metrics.high_watermark = max(self.metrics.high_watermark,
                                          self.metrics.current_depth)

    def snapshot(self) -> dict:
        return self.metrics.snapshot()


def cancel_answer_speech(tts) -> int:
    """Cancel THIS answer's pending speech and silence the in-flight utterance.

    Used when a turn is interrupted or replaced. Bounded and never raising: a TTS
    fault must never block prompt restoration (M57.4.1).
    """
    removed = 0
    try:
        gov = getattr(tts, "_gov", None)
        cv = getattr(tts, "_cv", None)
        if gov is not None and cv is not None:
            with cv:
                removed = gov.clear()
        try:
            tts.interrupt()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return removed
    return removed


def pending_depth(tts) -> int:
    """Current TTS queue depth, or 0 when unavailable. Never raises."""
    try:
        gov = getattr(tts, "_gov", None)
        return len(gov) if gov is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def build_planner(*, shape=None, muted: bool = False, turn_id: int = 0,
                  settings=None, started_at: float = 0.0) -> SpeechPlanner:
    """Build a planner from the turn's contract and operator config."""
    if settings is None:
        try:
            from core.config import settings as _s
            settings = _s
        except Exception:  # noqa: BLE001
            settings = None
    enabled = True
    backlog = 4
    if settings is not None:
        try:
            enabled = bool(getattr(settings, "response_progressive_tts", True))
            backlog = int(getattr(settings, "response_tts_backlog", 4))
        except (TypeError, ValueError):
            enabled, backlog = True, 4
    policy = getattr(shape, "speech", SpeechPolicy.SPEAK_FULL)
    if not isinstance(policy, SpeechPolicy):
        policy = SpeechPolicy.SPEAK_FULL
    return SpeechPlanner(policy=policy, muted=muted, enabled=enabled,
                         turn_id=turn_id, backlog_cap=backlog,
                         started_at=started_at)


# Bounded module-level metrics so runtime health can read the LAST turn's speech
# behaviour without holding a reference to the turn.
_last_metrics: dict = {}


def publish_speech_metrics(metrics: dict) -> None:
    global _last_metrics
    _last_metrics = dict(metrics or {})


def last_speech_metrics() -> dict:
    return dict(_last_metrics)
