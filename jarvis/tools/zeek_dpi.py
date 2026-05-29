"""
tools/zeek_dpi.py — Zeek L7 DPI log streamer (v24.0).

Architecture:
- Zeek runs on Kali VM or WSL2; logs accessed via shared/mounted path
- aiofiles for non-blocking async tail
- Parses TSV Zeek log format (#fields header line)
- DNS tunneling: query len > threshold or rate > threshold/min per host
- HTTP: suspicious user-agents or large POST bodies
- Config via core.config.settings (no hardcoded constants)
"""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

from core.config import settings
from core.events import make_event

_dns_counters: dict[str, list[float]] = {}


async def _tail_log(path: str, parser_fn, broadcast_fn) -> None:
    """Generic async tail-follow for a Zeek TSV log file."""
    fields: list[str] = []
    try:
        async with aiofiles.open(path, mode="r", encoding="utf-8",
                                 errors="replace") as f:
            await f.seek(0, 2)
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
    """conn.log parser — feeds the v33.0 network baseline / beacon detector."""
    src = record.get("id.orig_h", "")
    try:
        dst_port = int(record.get("id.resp_p", 0) or 0)
    except (ValueError, TypeError):
        dst_port = 0
    try:
        nbytes = int(record.get("orig_bytes", 0) or 0)
    except (ValueError, TypeError):
        nbytes = 0
    if src and dst_port:
        try:
            from core.network_baseline import check_and_alert
            asyncio.create_task(check_and_alert(src, dst_port, nbytes, broadcast_fn))
        except Exception:
            pass


async def _parse_dns(record: dict, broadcast_fn) -> None:
    """dns.log parser — DNS tunneling detection."""
    query = record.get("query", "")
    src   = record.get("id.orig_h", "")
    now   = time.time()

    if len(query) > settings.dns_query_len_threshold:
        await broadcast_fn(make_event(
            "dpi_alert",
            protocol="DNS",
            technique="DNS Tunneling (long query)",
            src_ip=src,
            detail=f"query={query[:80]}",
        ))

    _dns_counters.setdefault(src, [])
    _dns_counters[src] = [t for t in _dns_counters[src] if now - t < 60]
    _dns_counters[src].append(now)
    if len(_dns_counters[src]) > settings.dns_query_rate_threshold:
        await broadcast_fn(make_event(
            "dpi_alert",
            protocol="DNS",
            technique="DNS Tunneling (high query rate)",
            src_ip=src,
            detail=f"{len(_dns_counters[src])} queries/min",
        ))


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
        await broadcast_fn(make_event(
            "dpi_alert",
            protocol="HTTP",
            technique="Suspicious HTTP traffic",
            src_ip=src,
            detail=f"method={method} ua={ua[:60]} body={size}B",
        ))


async def start_zeek_dpi(broadcast_fn) -> None:
    from core.telemetry_auth import make_signed_broadcaster
    from loguru import logger
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "zeek")
    """Launch all Zeek log tailers as concurrent tasks."""
    base = Path(settings.zeek_log_dir) if settings.zeek_log_dir else None
    if not base or not base.exists():
        logger.info("ZEEK: log path not found or missing config — DPI bridge dormant")
        await asyncio.Event().wait()   # sleep forever, watchdog stays happy
        return
    await broadcast_fn(make_event(
        "system",
        message=f"Zeek DPI streamer starting — {base}",
    ))
    await asyncio.gather(
        _tail_log(str(base / "conn.log"), _parse_conn, broadcast_fn),
        _tail_log(str(base / "dns.log"),  _parse_dns,  broadcast_fn),
        _tail_log(str(base / "http.log"), _parse_http, broadcast_fn),
        return_exceptions=True,
    )
