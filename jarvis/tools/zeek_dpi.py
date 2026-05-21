"""
tools/zeek_dpi.py — Zeek L7 DPI log streamer (v22.0).

Architecture:
- Zeek runs on Kali VM or WSL2; logs accessed via shared/mounted path
- aiofiles for non-blocking async tail; classic tail-follow pattern
- Parses TSV Zeek log format (#fields header line)
- DNS tunneling: query len > 52 or rate > 100/min per host
- HTTP: suspicious user-agents or large POST bodies
- I/O-only; no ProcessPoolExecutor needed
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

ZEEK_LOG_DIR = os.getenv("ZEEK_LOG_DIR", "/mnt/zeek/logs/current")
DNS_QUERY_LEN_THRESHOLD  = 52
DNS_QUERY_RATE_THRESHOLD = 100   # per minute per host

_dns_counters: dict[str, list[float]] = {}  # host -> [timestamps]


async def _tail_log(path: str, parser_fn, broadcast_fn) -> None:
    """Generic async tail-follow for a Zeek TSV log file."""
    fields: list[str] = []
    try:
        async with aiofiles.open(path, mode="r", encoding="utf-8",
                                 errors="replace") as f:
            await f.seek(0, 2)   # seek to end — only new lines from now on
            while True:
                line = await f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue
                line = line.rstrip("\n")
                if line.startswith("#fields"):
                    fields = line.split("\t")[1:]
                    continue
                if line.startswith("#") or not fields:
                    continue
                values = line.split("\t")
                record = dict(zip(fields, values))
                await parser_fn(record, broadcast_fn)
    except FileNotFoundError:
        pass   # Zeek not running — degrade gracefully


async def _parse_conn(record: dict, broadcast_fn) -> None:
    """conn.log parser — placeholder for C2 beacon detection."""
    pass   # future: inter-arrival time analysis for beaconing


async def _parse_dns(record: dict, broadcast_fn) -> None:
    """dns.log parser — DNS tunneling detection."""
    query = record.get("query", "")
    src   = record.get("id.orig_h", "")
    now   = time.time()

    if len(query) > DNS_QUERY_LEN_THRESHOLD:
        await broadcast_fn({
            "type":      "dpi_alert",
            "protocol":  "DNS",
            "technique": "DNS Tunneling (long query)",
            "src_ip":    src,
            "detail":    f"query={query[:80]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    _dns_counters.setdefault(src, [])
    _dns_counters[src] = [t for t in _dns_counters[src] if now - t < 60]
    _dns_counters[src].append(now)
    if len(_dns_counters[src]) > DNS_QUERY_RATE_THRESHOLD:
        await broadcast_fn({
            "type":      "dpi_alert",
            "protocol":  "DNS",
            "technique": "DNS Tunneling (high query rate)",
            "src_ip":    src,
            "detail":    f"{len(_dns_counters[src])} queries/min",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


async def _parse_http(record: dict, broadcast_fn) -> None:
    """http.log parser — suspicious user-agents, large POST bodies."""
    ua     = record.get("user_agent", "")
    method = record.get("method", "")
    src    = record.get("id.orig_h", "")
    try:
        size = int(record.get("request_body_len", 0) or 0)
    except (ValueError, TypeError):
        size = 0

    suspicious_ua = any(k in ua.lower() for k in
                        ["python-requests", "curl", "wget", "go-http",
                         "powershell", "masscan"])
    if suspicious_ua or (method == "POST" and size > 50_000):
        await broadcast_fn({
            "type":      "dpi_alert",
            "protocol":  "HTTP",
            "technique": "Suspicious HTTP traffic",
            "src_ip":    src,
            "detail":    f"method={method} ua={ua[:60]} body={size}B",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


async def start_zeek_dpi(broadcast_fn) -> None:
    """Launch all Zeek log tailers as concurrent tasks."""
    base = Path(ZEEK_LOG_DIR)
    await broadcast_fn({"type": "system",
                        "message": f"Zeek DPI streamer starting — {base}"})
    await asyncio.gather(
        _tail_log(str(base / "conn.log"), _parse_conn, broadcast_fn),
        _tail_log(str(base / "dns.log"),  _parse_dns,  broadcast_fn),
        _tail_log(str(base / "http.log"), _parse_http, broadcast_fn),
        return_exceptions=True,
    )
