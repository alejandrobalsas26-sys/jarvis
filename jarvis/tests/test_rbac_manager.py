"""tests/test_rbac_manager.py — JARVIS V55.0 TITAN RBAC enforcement tests."""
from __future__ import annotations

import asyncio
import pytest

from core.rbac_manager import (
    ActorContext, ClearanceLevel, PermissionDenied, ActorNotFound,
    requires_clearance, set_current_actor, clear_current_actor,
)


# ── decorated fixtures ────────────────────────────────────────────────────────

@requires_clearance(ClearanceLevel.L3_Hunter)
async def _op_hunter(*, actor=None):
    return "hunter_ok"


@requires_clearance(ClearanceLevel.Admin)
async def _op_admin(*, actor=None):
    return "admin_ok"


_side_effect_ran = False


@requires_clearance(ClearanceLevel.Admin)
async def _op_with_side_effect(*, actor=None):
    global _side_effect_ran
    _side_effect_ran = True
    return "ok"


def _analyst():
    return ActorContext("analyst@test", ClearanceLevel.Analyst)


def _hunter():
    return ActorContext("hunter@test", ClearanceLevel.L3_Hunter)


def _admin():
    return ActorContext("admin@test", ClearanceLevel.Admin)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_allows_sufficient_clearance():
    assert asyncio.run(_op_hunter(actor=_hunter())) == "hunter_ok"
    assert asyncio.run(_op_hunter(actor=_admin()))  == "hunter_ok"
    assert asyncio.run(_op_admin(actor=_admin()))   == "admin_ok"


def test_denies_insufficient_clearance():
    with pytest.raises(PermissionDenied):
        asyncio.run(_op_hunter(actor=_analyst()))

    with pytest.raises(PermissionDenied):
        asyncio.run(_op_admin(actor=_hunter()))


def test_blocks_execution_before_side_effects():
    global _side_effect_ran
    _side_effect_ran = False
    with pytest.raises(PermissionDenied):
        asyncio.run(_op_with_side_effect(actor=_analyst()))
    assert not _side_effect_ran, "side effect must not run after denial"


def test_contextvar_actor_allows():
    set_current_actor(_hunter())
    try:
        result = asyncio.run(_op_hunter())
        assert result == "hunter_ok"
    finally:
        clear_current_actor()


def test_contextvar_actor_denies_insufficient():
    set_current_actor(_analyst())
    try:
        with pytest.raises(PermissionDenied):
            asyncio.run(_op_hunter())
    finally:
        clear_current_actor()


def test_production_mode_blocks_env_fallback(monkeypatch):
    """Production mode must never silently use RBAC_DEFAULT_ACTOR."""
    monkeypatch.setenv("JARVIS_ENV", "production")
    monkeypatch.setenv("RBAC_DEFAULT_ACTOR", "ghost:Admin")
    clear_current_actor()
    with pytest.raises((PermissionDenied, ActorNotFound, PermissionError)):
        asyncio.run(_op_hunter())


def test_dev_mode_allows_env_fallback(monkeypatch):
    """Dev/test mode may use RBAC_DEFAULT_ACTOR as a fallback."""
    monkeypatch.setenv("JARVIS_ENV", "test")
    monkeypatch.setenv("RBAC_DEFAULT_ACTOR", "ci-bot:Admin")
    clear_current_actor()
    result = asyncio.run(_op_admin())
    assert result == "admin_ok"


def test_explicit_actor_overrides_contextvar():
    """Explicit actor= kwarg always wins over contextvar."""
    set_current_actor(_analyst())
    try:
        result = asyncio.run(_op_hunter(actor=_hunter()))
        assert result == "hunter_ok"
    finally:
        clear_current_actor()
