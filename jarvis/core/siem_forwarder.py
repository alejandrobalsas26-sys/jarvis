"""
core/siem_forwarder.py — JARVIS V55.0 TITAN
Async batched SIEM forwarder with Elastic Common Schema (ECS) mapping.
No-op mode when SIEM_ENDPOINT is unset. Safe under high alert volume.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.siem_forwarder")

_ENDPOINT    = os.environ.get("SIEM_ENDPOINT", "")
_API_KEY     = os.environ.get("SIEM_API_KEY", "")
_VERIFY_TLS  = os.environ.get("SIEM_TLS_VERIFY", "true").lower() not in ("false", "0", "no")
_BATCH_SIZE  = int(os.environ.get("SIEM_BATCH_SIZE", "50"))
_FLUSH_SEC   = float(os.environ.get("SIEM_FLUSH_INTERVAL", "10"))
_TIMEOUT_SEC = float(os.environ.get("SIEM_TIMEOUT", "15"))
_MAX_RETRY   = int(os.environ.get("SIEM_MAX_RETRIES", "3"))

_HOSTNAME = socket.gethostname()


class SIEMForwarder:
    def __init__(
        self,
        endpoint: str       = _ENDPOINT,
        api_key: str        = _API_KEY,
        batch_size: int     = _BATCH_SIZE,
        flush_interval: float = _FLUSH_SEC,
    ) -> None:
        self._endpoint       = endpoint
        self._api_key        = api_key
        self._batch_size     = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=10_000)
        self._flush_task: asyncio.Task | None = None
        self._running = False

    @property
    def _noop(self) -> bool:
        return not self._endpoint

    async def start(self) -> None:
        if self._noop:
            logger.info("SIEM_FORWARDER: no-op mode (SIEM_ENDPOINT not configured)")
            return
        self._running = True
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="siem-flush"
        )
        logger.info(
            "SIEM_FORWARDER: started → %s (batch=%d flush=%.1fs)",
            self._endpoint, self._batch_size, self._flush_interval,
        )

    async def stop(self) -> None:
        self._running = False
        if not self._noop:
            try:
                await self.flush()
            except Exception as e:
                logger.debug("SIEM_FORWARDER: final flush error — %s", e)
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

    def enqueue(self, event: dict) -> None:
        if self._noop:
            return
        try:
            self._queue.put_nowait(self.map_to_ecs(event))
        except asyncio.QueueFull:
            logger.warning(
                "SIEM_FORWARDER: queue full — dropping event type=%s",
                event.get("type", "?"),
            )

    async def flush(self) -> None:
        if self._noop or self._queue.empty():
            return
        batch: list[dict] = []
        while not self._queue.empty() and len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._send(batch)

    def map_to_ecs(self, event: dict) -> dict:
        ts = event.get("ts", time.time())
        try:
            timestamp = datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        except Exception:
            timestamp = datetime.now(timezone.utc).isoformat()

        severity_raw = event.get("severity", event.get("severity_score", 0))
        try:
            severity_int = int(min(float(severity_raw), 10))
        except (TypeError, ValueError):
            severity_int = 0

        ecs: dict[str, Any] = {
            "@timestamp": timestamp,
            "message": event.get("message", event.get("type", "jarvis_event")),
            "labels": {
                k: str(v) for k, v in event.items()
                if k not in ("ts", "__mono_ts")
                and isinstance(v, (str, int, float, bool))
            },
            "event": {
                "kind":     "alert",
                "category": ["intrusion_detection"],
                "type":     ["info"],
                "action":   str(event.get("type", "")),
                "severity": severity_int,
                "dataset":  "jarvis.alerts",
                "module":   str(event.get("source", "jarvis")),
            },
            "host": {
                "name": _HOSTNAME,
            },
            "rule": {
                "name": str(event.get("rule", event.get("type", ""))),
                "id":   str(event.get("incident_id", "")),
            },
        }

        src_ip = event.get("src_ip") or event.get("attacker_ip")
        if src_ip:
            ecs["source"] = {"ip": str(src_ip)}

        dst_ip = event.get("dst_ip") or event.get("remote_ip")
        if dst_ip:
            ecs["destination"] = {"ip": str(dst_ip)}

        user = event.get("user") or event.get("username")
        if user:
            ecs["user"] = {"name": str(user)}

        proc = event.get("process") or event.get("process_name")
        if proc:
            ecs["process"] = {"name": str(proc), "pid": event.get("pid")}

        fpath = event.get("file_path") or event.get("path")
        if fpath:
            ecs["file"] = {"path": str(fpath)}

        attck = (event.get("attck") or event.get("technique")
                 or event.get("mitre_techniques") or [])
        if isinstance(attck, str):
            attck = [attck]
        if attck:
            ecs["threat"] = {
                "tactic":    {"name": str(event.get("kill_chain_phase", ""))},
                "technique": {"name": str(attck[0])},
            }

        return ecs

    def load_tactic_audit(self, path: str | Path) -> list[dict]:
        events: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("SIEM_FORWARDER: load_tactic_audit error — %s", e)
        return events

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception as e:
                logger.error("SIEM_FORWARDER: flush loop error — %s", e)

    async def _send(self, batch: list[dict]) -> None:
        try:
            import aiohttp
        except ImportError:
            logger.error("SIEM_FORWARDER: aiohttp not installed — cannot send")
            return

        headers = {"Content-Type": "application/x-ndjson"}
        if self._api_key:
            headers["Authorization"] = f"ApiKey {self._api_key}"

        payload = "\n".join(json.dumps(e) for e in batch) + "\n"
        backoff = 1.0

        for attempt in range(1, _MAX_RETRY + 1):
            try:
                connector = aiohttp.TCPConnector(ssl=_VERIFY_TLS)
                timeout   = aiohttp.ClientTimeout(total=_TIMEOUT_SEC)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
                    async with sess.post(self._endpoint, data=payload, headers=headers) as resp:
                        if resp.status < 300:
                            logger.debug(
                                "SIEM_FORWARDER: flushed %d events → HTTP %d",
                                len(batch), resp.status,
                            )
                            return
                        body = await resp.text()
                        logger.warning("SIEM_FORWARDER: HTTP %d — %s", resp.status, body[:200])
            except Exception as e:
                logger.warning("SIEM_FORWARDER: attempt %d/%d failed — %s", attempt, _MAX_RETRY, e)

            if attempt < _MAX_RETRY:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

        logger.error(
            "SIEM_FORWARDER: gave up after %d attempts, %d events lost",
            _MAX_RETRY, len(batch),
        )
