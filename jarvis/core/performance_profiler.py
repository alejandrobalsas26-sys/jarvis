"""
core/performance_profiler.py — Per-subsystem performance tracking (v46.0).

Sampling-based — never blocks operations.
Tracks: LLM inference latency, vision processing time, OCR duration,
tool execution time, hunt duration, broadcast latency.

Slow operation detector: any operation > 5 seconds is logged
prominently and broadcast to AURA.
"""

import asyncio, time
from collections import defaultdict
from loguru import logger

# Per-operation histograms
_latencies: dict[str, list[float]] = defaultdict(list)
_MAX_SAMPLES = 100   # rolling window per operation
_SLOW_THRESHOLD_S = 5.0


def record_latency(operation: str, duration_s: float) -> None:
    """Record an operation's duration."""
    samples = _latencies[operation]
    samples.append(duration_s)
    if len(samples) > _MAX_SAMPLES:
        samples.pop(0)

    if duration_s > _SLOW_THRESHOLD_S:
        logger.warning(
            f"PROFILER: SLOW — {operation} took {duration_s:.1f}s"
        )


def get_stats(operation: str) -> dict:
    """Get statistics for an operation."""
    samples = _latencies.get(operation, [])
    if not samples:
        return {"count": 0}

    sorted_samples = sorted(samples)
    n = len(sorted_samples)
    return {
        "count":  n,
        "min_s":  round(sorted_samples[0], 3),
        "max_s":  round(sorted_samples[-1], 3),
        "mean_s": round(sum(samples) / n, 3),
        "p50_s":  round(sorted_samples[n // 2], 3),
        "p95_s":  round(sorted_samples[min(int(n * 0.95), n-1)], 3),
        "p99_s":  round(sorted_samples[min(int(n * 0.99), n-1)], 3),
    }


def get_all_stats() -> dict:
    return {op: get_stats(op) for op in _latencies}


class profile_context:
    """Async context manager for timing operations.

    Usage:
        async with profile_context("llm_inference"):
            result = await llm.generate(...)
    """
    def __init__(self, operation: str):
        self.operation = operation
        self.start     = 0.0

    async def __aenter__(self):
        self.start = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        duration = time.monotonic() - self.start
        record_latency(self.operation, duration)


async def broadcast_stats(broadcast_fn) -> None:
    """Send full profile to AURA for visualization."""
    stats = get_all_stats()
    if not stats:
        return

    # Top 5 slowest operations
    by_mean = sorted(
        ((op, s.get("mean_s", 0)) for op, s in stats.items()),
        key=lambda x: -x[1],
    )[:5]

    await broadcast_fn({
        "type":          "performance_profile",
        "operations":    len(stats),
        "top_slowest":   by_mean,
        "total_samples": sum(s.get("count", 0) for s in stats.values()),
        "timestamp":     __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    })
