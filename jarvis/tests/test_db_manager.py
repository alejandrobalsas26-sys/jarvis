"""tests/test_db_manager.py — JARVIS V55.0 TITAN DB manager smoke tests."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock



def test_import_and_public_api():
    import core.db_manager as dbm
    assert hasattr(dbm, "DBManager")
    assert hasattr(dbm, "get_db_manager")


def test_instantiation_without_services():
    from core.db_manager import DBManager
    mgr = DBManager(
        pg_dsn="postgresql://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/15",
    )
    assert mgr._pool     is None
    assert mgr._redis    is None
    assert mgr._redis_ok is False


def test_save_alert_no_pool_returns_incident_id():
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        aid = await mgr.save_alert({"incident_id": "TEST01", "severity_score": 7.5})
        assert aid == "TEST01"

    asyncio.run(drive())


def test_save_alert_no_pool_generates_id_when_missing():
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        aid = await mgr.save_alert({"severity_score": 5.0})
        assert aid and len(aid) > 0

    asyncio.run(drive())


def test_mark_seen_no_redis_conservative():
    """Without Redis, mark_seen returns True (conservative: treat as unseen)."""
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        result = await mgr.mark_seen("ns", "key1")
        assert result is True

    asyncio.run(drive())


def test_is_seen_no_redis_returns_false():
    """Without Redis, is_seen returns False (conservative: assume not seen)."""
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        result = await mgr.is_seen("ns", "key1")
        assert result is False

    asyncio.run(drive())


def test_cache_ops_no_redis_are_noop():
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        await mgr.cache_set("k1", {"x": 1})
        result = await mgr.cache_get("k1")
        assert result is None

    asyncio.run(drive())


def test_acquire_lock_no_redis_yields_false():
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        async with mgr.acquire_lock("test-lock") as acquired:
            assert acquired is False

    asyncio.run(drive())


def test_get_alert_no_pool_returns_none():
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        result = await mgr.get_alert("NONEXISTENT")
        assert result is None

    asyncio.run(drive())


def test_connect_lifecycle_with_mocks():
    """Full lifecycle smoke test with inline module stubs for asyncpg and redis."""
    import sys
    import types
    from core.db_manager import DBManager

    # Build asyncpg stub
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    pool_instance = MagicMock()
    pool_instance.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    pool_instance.acquire.return_value.__aexit__  = AsyncMock(return_value=False)
    pool_instance.close = AsyncMock()

    async def _async_create_pool(*a, **kw):
        return pool_instance

    asyncpg_stub = types.ModuleType("asyncpg")
    asyncpg_stub.create_pool = _async_create_pool

    # Build redis stub
    mock_redis = AsyncMock()
    mock_redis.ping   = AsyncMock()
    mock_redis.aclose = AsyncMock()

    redis_asyncio_stub = types.ModuleType("redis.asyncio")
    redis_asyncio_stub.from_url = MagicMock(return_value=mock_redis)

    redis_stub = types.ModuleType("redis")
    redis_stub.asyncio = redis_asyncio_stub

    original_asyncpg = sys.modules.get("asyncpg")
    original_redis   = sys.modules.get("redis")
    original_ra      = sys.modules.get("redis.asyncio")

    sys.modules["asyncpg"]       = asyncpg_stub
    sys.modules["redis"]         = redis_stub
    sys.modules["redis.asyncio"] = redis_asyncio_stub

    async def drive():
        mgr = DBManager(
            pg_dsn="postgresql://test:test@localhost:5432/test",
            redis_url="redis://localhost:6379/0",
        )
        await mgr.connect()
        assert mgr._pool  is not None
        assert mgr._redis is not None
        assert mgr._redis_ok is True
        await mgr.close()
        pool_instance.close.assert_awaited_once()
        mock_redis.aclose.assert_awaited_once()

    try:
        asyncio.run(drive())
    finally:
        if original_asyncpg is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = original_asyncpg
        if original_redis is None:
            sys.modules.pop("redis", None)
        else:
            sys.modules["redis"] = original_redis
        if original_ra is None:
            sys.modules.pop("redis.asyncio", None)
        else:
            sys.modules["redis.asyncio"] = original_ra


def test_deduplication_with_mocked_redis():
    """mark_seen + is_seen round-trip using a mocked Redis client."""
    from core.db_manager import DBManager

    async def drive():
        mgr = DBManager()
        mgr._redis_ok = True
        mgr._redis    = AsyncMock()

        # First call: SETNX returns True (key did not exist)
        mgr._redis.set  = AsyncMock(return_value=True)
        mgr._redis.exists = AsyncMock(return_value=1)

        fresh = await mgr.mark_seen("ns", "alert-001", ttl_seconds=90)
        assert fresh is True

        seen = await mgr.is_seen("ns", "alert-001")
        assert seen is True

        # Second call: SETNX returns None (key already exists)
        mgr._redis.set = AsyncMock(return_value=None)
        duplicate = await mgr.mark_seen("ns", "alert-001", ttl_seconds=90)
        assert duplicate is False

    asyncio.run(drive())
