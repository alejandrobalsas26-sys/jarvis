"""
core/network_baseline.py — Statistical network anomaly + beacon detector (v33.0).

Warmup period: 60 seconds of observation before any anomaly alerts.
Beaconing detection: coefficient of variation on inter-connection
intervals. Regular beacons have CV < 0.25 (low variance = suspicious).

Anomaly scoring uses z-score against rolling 10-minute baseline.
Scores > 3.0 sigma → anomaly alert.
"""

import asyncio
import collections
import math
import time
from datetime import datetime, timezone

from loguru import logger

_WARMUP_SECONDS     = 60
_WINDOW_SECONDS     = 600
_ZSCORE_THRESHOLD   = 3.0
_BEACON_CV_MAX      = 0.25
_BEACON_MIN_SAMPLES = 5

_host_connections: dict[str, collections.deque] = {}
_pair_stats: dict[tuple, dict] = {}

_start_time: float = time.monotonic()


def _coefficient_of_variation(values: list[float]) -> float:
    if len(values) < 2:
        return float("inf")
    mean = sum(values) / len(values)
    if mean == 0:
        return float("inf")
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance) / mean


def _zscore(value: float, history: list[float]) -> float:
    if len(history) < 3:
        return 0.0
    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return abs((value - mean) / std)


def ingest_connection(src_ip: str, dst_port: int,
                      bytes_transferred: int = 0) -> dict | None:
    """
    Ingest a new connection observation. Returns anomaly dict if
    anomaly detected, None if normal or in warmup.
    """
    now = time.monotonic()
    if (now - _start_time) < _WARMUP_SECONDS:
        return None

    if src_ip not in _host_connections:
        _host_connections[src_ip] = collections.deque()
    _host_connections[src_ip].append((now, dst_port, bytes_transferred))

    cutoff = now - _WINDOW_SECONDS
    while _host_connections[src_ip] and _host_connections[src_ip][0][0] < cutoff:
        _host_connections[src_ip].popleft()

    pair_key = (src_ip, dst_port)
    if pair_key not in _pair_stats:
        _pair_stats[pair_key] = {"timestamps": [], "bytes": []}
    _pair_stats[pair_key]["timestamps"].append(now)
    _pair_stats[pair_key]["bytes"].append(bytes_transferred)

    cutoff_idx = next(
        (i for i, t in enumerate(_pair_stats[pair_key]["timestamps"])
         if t >= cutoff),
        0,
    )
    _pair_stats[pair_key]["timestamps"] = _pair_stats[pair_key]["timestamps"][cutoff_idx:]
    _pair_stats[pair_key]["bytes"]      = _pair_stats[pair_key]["bytes"][cutoff_idx:]

    anomalies = []

    conn_count = len(_host_connections[src_ip])
    all_counts = [len(v) for v in _host_connections.values() if len(v) >= 3]
    if len(all_counts) >= 5:
        z = _zscore(conn_count, all_counts)
        if z > _ZSCORE_THRESHOLD:
            anomalies.append({
                "detector":    "connection_frequency",
                "z_score":     round(z, 2),
                "conn_count":  conn_count,
                "description": f"{src_ip} has {conn_count} connections "
                               f"({z:.1f}σ above baseline)",
            })

    timestamps = _pair_stats[pair_key]["timestamps"]
    if len(timestamps) >= _BEACON_MIN_SAMPLES:
        intervals = [timestamps[i+1] - timestamps[i]
                     for i in range(len(timestamps)-1)]
        cv = _coefficient_of_variation(intervals)
        if cv < _BEACON_CV_MAX:
            mean_interval = sum(intervals) / len(intervals)
            anomalies.append({
                "detector":        "beaconing",
                "cv":              round(cv, 3),
                "mean_interval_s": round(mean_interval, 1),
                "samples":         len(timestamps),
                "description":     f"{src_ip}:{dst_port} beaconing every "
                                   f"{mean_interval:.0f}s (CV={cv:.3f})",
                "technique":       "T1071",
            })

    if anomalies:
        return {
            "src_ip":    src_ip,
            "dst_port":  dst_port,
            "anomalies": anomalies,
            "severity":  3.0 if any(a.get("detector") == "beaconing"
                                    for a in anomalies) else 2.0,
        }

    return None


async def start_network_baseline(broadcast_fn) -> None:
    """
    Background task. The Zeek DPI handler calls check_and_alert()
    directly for each new connection. This coroutine warms up,
    then logs periodic stats.
    """
    logger.info(
        f"NETWORK_BASELINE: warmup started — "
        f"anomaly alerts active in {_WARMUP_SECONDS}s"
    )
    await asyncio.sleep(_WARMUP_SECONDS)
    logger.info("NETWORK_BASELINE: baseline established — anomaly detection active")

    while True:
        await asyncio.sleep(300)
        hosts = len(_host_connections)
        pairs = len(_pair_stats)
        logger.debug(
            f"NETWORK_BASELINE: tracking {hosts} hosts, {pairs} host:port pairs"
        )


async def check_and_alert(src_ip: str, dst_port: int,
                          bytes_transferred: int,
                          broadcast_fn) -> None:
    """
    Called inline from Zeek DPI handler for each new connection.
    Non-blocking — runs check synchronously, broadcasts if anomaly found.
    """
    try:
        from core.feed_sanitizer import sanitize_for_hud
    except Exception:
        def sanitize_for_hud(v, n=200):  # type: ignore
            return str(v)[:n]

    result = ingest_connection(src_ip, dst_port, bytes_transferred)
    if result:
        for anomaly in result.get("anomalies", []):
            logger.warning(
                f"NETWORK_BASELINE: {anomaly['detector'].upper()} — "
                f"{anomaly['description']}"
            )
            await broadcast_fn({
                "type":        "network_anomaly",
                "detector":    anomaly["detector"],
                "src_ip":      sanitize_for_hud(src_ip, 45),
                "dst_port":    dst_port,
                "description": sanitize_for_hud(anomaly["description"], 200),
                "technique":   anomaly.get("technique", ""),
                "severity":    "HIGH" if anomaly["detector"] == "beaconing"
                               else "MEDIUM",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            })
