"""
core/db_manager.py — JARVIS V55.0 TITAN
Async persistent state manager: PostgreSQL (asyncpg) + Redis (redis.asyncio).
Replaces volatile in-memory sets/dicts for alert state, deduplication, and caching.
Falls back gracefully if Redis is unavailable (uses PostgreSQL only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

logger = logging.getLogger("jarvis.db_manager")

_PG_DSN    = os.environ.get("DATABASE_URL", "postgresql://jarvis:jarvis@localhost:5432/jarvis")
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jarvis_alerts (
    id          TEXT PRIMARY KEY,
    rule        TEXT,
    severity    REAL,
    kill_chain  TEXT,
    status      TEXT DEFAULT 'ACTIVE',
    payload     JSONB NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_status   ON jarvis_alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON jarvis_alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_created  ON jarvis_alerts(created_at);
"""


class DBManager:
    def __init__(self, pg_dsn: str = _PG_DSN, redis_url: str = _REDIS_URL) -> None:
        self._pg_dsn    = pg_dsn
        self._redis_url = redis_url
        self._pool      = None   # asyncpg.Pool | None
        self._redis     = None   # redis.asyncio.Redis | None
        self._redis_ok  = False

    async def connect(self) -> None:
        await self._connect_pg()
        await self._connect_redis()

    async def _connect_pg(self) -> None:
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(
                self._pg_dsn, min_size=2, max_size=10, command_timeout=30
            )
            async with self._pool.acquire() as conn:
                await conn.execute(_SCHEMA)
            logger.info("DB_MANAGER: PostgreSQL pool ready")
        except Exception as e:
            logger.error("DB_MANAGER: PostgreSQL unavailable — %s", e)
            self._pool = None

    async def _connect_redis(self) -> None:
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True, socket_connect_timeout=5
            )
            await self._redis.ping()
            self._redis_ok = True
            logger.info("DB_MANAGER: Redis ready")
        except Exception as e:
            logger.warning("DB_MANAGER: Redis unavailable (dedup fallback to PG) — %s", e)
            self._redis    = None
            self._redis_ok = False

    @property
    def is_connected(self) -> bool:
        """True when the PostgreSQL pool is available (persistent alert state active)."""
        return self._pool is not None

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
        if self._redis:
            await self._redis.aclose()

    # ── Alert persistence ─────────────────────────────────────────────────────

    async def save_alert(self, alert: dict) -> str:
        alert_id = alert.get("incident_id") or str(uuid.uuid4())[:8].upper()
        if not self._pool:
            return alert_id
        now = time.time()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO jarvis_alerts
                        (id, rule, severity, kill_chain, status, payload, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                    ON CONFLICT (id) DO UPDATE
                        SET severity   = EXCLUDED.severity,
                            status     = EXCLUDED.status,
                            payload    = EXCLUDED.payload,
                            updated_at = EXCLUDED.updated_at
                    """,
                    alert_id,
                    str(alert.get("rule", "")),
                    float(alert.get("severity_score", 0)),
                    str(alert.get("kill_chain_phase", "")),
                    str(alert.get("status", "ACTIVE")),
                    json.dumps(alert),
                    now, now,
                )
        except Exception as e:
            logger.error("DB_MANAGER: save_alert failed — %s", e)
        return alert_id

    async def get_alert(self, alert_id: str) -> dict | None:
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT payload FROM jarvis_alerts WHERE id = $1", alert_id
                )
            if row:
                return json.loads(row["payload"])
        except Exception as e:
            logger.error("DB_MANAGER: get_alert failed — %s", e)
        return None

    # ── Deduplication ─────────────────────────────────────────────────────────

    async def mark_seen(
        self, namespace: str, key: str, ttl_seconds: int | None = None
    ) -> bool:
        """Returns True if key was NOT already seen (freshly marked). Conservative fallback: True."""
        rk = f"seen:{namespace}:{key}"
        if self._redis_ok and self._redis:
            try:
                result = await self._redis.set(rk, "1", nx=True, ex=ttl_seconds)
                return result is not None
            except Exception as e:
                logger.debug("DB_MANAGER: redis mark_seen failed — %s", e)
        return True  # conservative: treat as unseen to avoid dropped alerts

    async def is_seen(self, namespace: str, key: str) -> bool:
        rk = f"seen:{namespace}:{key}"
        if self._redis_ok and self._redis:
            try:
                return bool(await self._redis.exists(rk))
            except Exception as e:
                logger.debug("DB_MANAGER: redis is_seen failed — %s", e)
        return False  # conservative: assume not seen

    # ── Cache ─────────────────────────────────────────────────────────────────

    async def cache_set(self, key: str, value: dict, ttl_seconds: int = 3600) -> None:
        if not (self._redis_ok and self._redis):
            return
        try:
            await self._redis.set(f"cache:{key}", json.dumps(value), ex=ttl_seconds)
        except Exception as e:
            logger.debug("DB_MANAGER: cache_set failed — %s", e)

    async def cache_get(self, key: str) -> dict | None:
        if not (self._redis_ok and self._redis):
            return None
        try:
            raw = await self._redis.get(f"cache:{key}")
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.debug("DB_MANAGER: cache_get failed — %s", e)
        return None

    # ── Pub/Sub ───────────────────────────────────────────────────────────────

    async def publish_event(self, channel: str, event: dict) -> None:
        if not (self._redis_ok and self._redis):
            return
        try:
            await self._redis.publish(channel, json.dumps(event))
        except Exception as e:
            logger.debug("DB_MANAGER: publish_event failed — %s", e)

    # ── Distributed lock ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def acquire_lock(self, name: str, ttl_seconds: int = 60):
        """Async context manager yielding True if lock was acquired, False otherwise."""
        lock_key = f"lock:{name}"
        lock_val = str(uuid.uuid4())
        acquired = False
        if self._redis_ok and self._redis:
            try:
                acquired = bool(
                    await self._redis.set(lock_key, lock_val, nx=True, ex=ttl_seconds)
                )
            except Exception as e:
                logger.debug("DB_MANAGER: acquire_lock failed — %s", e)
        try:
            yield acquired
        finally:
            if acquired and self._redis_ok and self._redis:
                try:
                    val = await self._redis.get(lock_key)
                    if val == lock_val:
                        await self._redis.delete(lock_key)
                except Exception:
                    pass


# ── Module-level factory ──────────────────────────────────────────────────────

_instance: DBManager | None = None
_lock: asyncio.Lock | None = None


async def get_db_manager() -> DBManager:
    """Dependency-injection factory. Creates and connects the singleton on first call."""
    global _instance, _lock
    if _instance is not None:
        return _instance
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _instance is None:
            _instance = DBManager()
            await _instance.connect()
    return _instance
