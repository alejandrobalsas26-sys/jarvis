"""
core/cognitive_optimizer.py — JARVIS cognitive self-optimization (v34.0).

Tracks inference latency, response quality signals, and query patterns.
Adapts thresholds and routing decisions based on observed performance.

Self-optimization loops:
  1. Latency tracker → adjust ctx dynamically if inference is slow
  2. Query classifier → 5-category pre-classification (0ms overhead)
  3. Context quality scorer → detect degrading context, trigger compaction
  4. System prompt enricher → inject latest threat intel into system prompt
  5. Conversation health monitor → detect confusion, offer fresh start
"""

import asyncio
import collections
import statistics
import time
from datetime import datetime, timezone

from loguru import logger

# ── Inference latency tracker ─────────────────────────────────────────────────

_latency_history: "collections.deque[int]" = collections.deque(maxlen=50)
_slow_count = 0
_SLOW_THRESHOLD_MS    = 8000   # 8s = slow for U-series
_CTX_REDUCTION_FACTOR = 0.75


class LatencyTracker:
    def __init__(self) -> None:
        self._start: float | None = None

    def start(self) -> None:
        self._start = time.monotonic()

    def stop(self, model: str, ctx: int) -> dict:
        if self._start is None:
            return {}
        global _slow_count
        elapsed_ms = round((time.monotonic() - self._start) * 1000)
        _latency_history.append(elapsed_ms)
        self._start = None

        result = {
            "elapsed_ms": elapsed_ms,
            "model":      model,
            "ctx":        ctx,
            "slow":       elapsed_ms > _SLOW_THRESHOLD_MS,
        }

        if elapsed_ms > _SLOW_THRESHOLD_MS:
            _slow_count += 1
            logger.debug(
                f"COGNITIVE: slow inference {elapsed_ms}ms "
                f"(#{_slow_count} slow in last {len(_latency_history)})"
            )

        return result

    @property
    def p50_ms(self) -> float:
        if not _latency_history:
            return 0.0
        return float(statistics.median(_latency_history))

    @property
    def p95_ms(self) -> float:
        if len(_latency_history) < 5:
            return 0.0
        sorted_h = sorted(_latency_history)
        idx = int(len(sorted_h) * 0.95)
        return float(sorted_h[min(idx, len(sorted_h) - 1)])

    def should_reduce_ctx(self) -> bool:
        """True if recent inference is consistently slow."""
        if len(_latency_history) < 5:
            return False
        recent = list(_latency_history)[-5:]
        return all(ms > _SLOW_THRESHOLD_MS for ms in recent)


latency_tracker = LatencyTracker()


# ── Query pre-classifier ──────────────────────────────────────────────────────

_QUERY_CATEGORIES: dict[str, dict] = {
    "security_technical": {
        "keywords": {"exploit", "payload", "shellcode", "mimikatz", "nmap",
                     "bloodhound", "kerberoast", "lateral", "privilege",
                     "escalation", "volatility", "forensic", "malware",
                     "injection", "beacon", "c2", "exfil", "sigma", "yara"},
        "force_deep": True,
    },
    "system_command": {
        "keywords": {"run", "execute", "scan", "start", "stop", "list",
                     "show", "open", "close", "status", "check"},
        "force_deep": False,
    },
    "analysis_request": {
        "keywords": {"analyze", "explain", "describe", "what", "why", "how",
                     "compare", "investigate", "correlate", "assess", "review"},
        "force_deep": True,
    },
    "conversational": {
        "keywords": {"hello", "hi", "thanks", "ok", "yes", "no", "good",
                     "great", "sure", "please", "help"},
        "force_deep": False,
    },
    "data_query": {
        "keywords": {"incident", "alert", "log", "history", "report",
                     "summary", "list", "show", "get", "fetch"},
        "force_deep": False,
    },
}


def classify_query(text: str) -> tuple[str, bool]:
    """
    Pre-classify query into category. Returns (category, force_deep).
    0ms overhead — pure set intersection, no LLM call.
    """
    words = set(text.lower().split())
    best_category = "conversational"
    best_score    = 0
    force_deep    = False

    for category, config in _QUERY_CATEGORIES.items():
        score = len(words & config["keywords"])
        if score > best_score:
            best_score    = score
            best_category = category
            force_deep    = config["force_deep"]

    return best_category, force_deep


# ── Context quality scorer ────────────────────────────────────────────────────

_CONFUSION_SIGNALS: set[str] = {
    "what did you say", "i don't understand", "that's wrong",
    "incorrect", "that's not right", "can you repeat",
    "start over", "forget that", "wrong answer",
}


def score_context_quality(history: list[dict]) -> float:
    """
    Score conversation context quality 0.0 (degraded) to 1.0 (healthy).
    Detects: repetition, confusion signals, excessive length.
    """
    if not history:
        return 1.0

    score = 1.0
    total_tokens_est = sum(
        len(str(m.get("content", ""))) // 4 for m in history
    )

    # Length penalty — very long context degrades quality
    if total_tokens_est > 3000:
        score -= 0.3
    elif total_tokens_est > 1500:
        score -= 0.1

    # Confusion signal detection
    recent_text = " ".join(
        str(m.get("content", "")).lower()
        for m in history[-4:]
        if m.get("role") == "user"
    )
    confusion_hits = sum(
        1 for sig in _CONFUSION_SIGNALS if sig in recent_text
    )
    score -= confusion_hits * 0.2

    # Repetition detection — same user message twice in last 6 turns
    recent_user = [
        str(m.get("content", "")).strip()[:100]
        for m in history[-6:]
        if m.get("role") == "user"
    ]
    if len(recent_user) != len(set(recent_user)) and recent_user:
        score -= 0.3

    return max(0.0, min(1.0, score))


# ── System prompt enricher ────────────────────────────────────────────────────

_THREAT_ENRICHMENT     = ""
_LAST_ENRICHMENT_TS    = 0.0
_ENRICHMENT_INTERVAL   = 3600   # 1 hour


async def refresh_threat_enrichment() -> str:
    """
    Pull today's top threats from episodic memory and format them as
    a system prompt suffix. Updates every hour.
    """
    global _THREAT_ENRICHMENT, _LAST_ENRICHMENT_TS

    now = time.time()
    if now - _LAST_ENRICHMENT_TS < _ENRICHMENT_INTERVAL:
        return _THREAT_ENRICHMENT

    try:
        from core.episodic_memory import query_similar_episodes
        recent = await query_similar_episodes(
            "critical threat HIGH CRITICAL attack", n_results=3
        )
        if recent:
            summaries = [
                str(ep.get("content", ""))[:150]
                for ep in recent
            ]
            _THREAT_ENRICHMENT = (
                "\n\n[RECENT OPERATIONAL CONTEXT — last observed threats]:\n"
                + "\n".join(f"• {s}" for s in summaries)
                + "\n[END CONTEXT]"
            )
            _LAST_ENRICHMENT_TS = now
    except Exception:
        pass

    return _THREAT_ENRICHMENT


# ── Conversation health monitor ───────────────────────────────────────────────

async def monitor_conversation_health(
    history: list[dict],
    broadcast_fn,
) -> float:
    """
    Check conversation health. If score < 0.4, broadcast suggestion
    to operator to consider clearing context.
    Returns quality score.
    """
    score = score_context_quality(history)

    if score < 0.4:
        logger.warning(
            f"COGNITIVE: conversation context degrading "
            f"(quality={score:.2f}) — suggesting context reset"
        )
        try:
            await broadcast_fn({
                "type":      "cognitive_health_alert",
                "score":     round(score, 2),
                "message":   "Conversation context quality degrading. "
                             "Consider starting a fresh session.",
                "severity":  "WARNING",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    return score


# ── Performance stats broadcaster ─────────────────────────────────────────────

async def start_cognitive_monitor(broadcast_fn) -> None:
    """Background task: broadcast cognitive stats every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        if not _latency_history:
            continue
        try:
            await broadcast_fn({
                "type":               "cognitive_stats",
                "p50_ms":             round(latency_tracker.p50_ms, 1),
                "p95_ms":             round(latency_tracker.p95_ms, 1),
                "slow_count":         _slow_count,
                "samples":            len(_latency_history),
                "slow_threshold_ms":  _SLOW_THRESHOLD_MS,
                "ctx_reduce_recommended": latency_tracker.should_reduce_ctx(),
                "timestamp":          datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.debug(f"COGNITIVE: stats broadcast failed: {e}")
