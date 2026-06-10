"""
core/rbac_manager.py — JARVIS V55.0 TITAN
Strict async RBAC enforcement for destructive security actions.
Hierarchy: Analyst < L3_Hunter < Admin
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Any

logger = logging.getLogger("jarvis.rbac_manager")


class ClearanceLevel(IntEnum):
    Analyst   = 1
    L3_Hunter = 2
    Admin     = 3


@dataclass
class ActorContext:
    identity:  str
    clearance: ClearanceLevel
    source:    str = "explicit"   # "explicit" | "contextvar" | "env"


class PermissionDenied(PermissionError):
    def __init__(self, action: str, required: ClearanceLevel, actor: ActorContext) -> None:
        self.action   = action
        self.required = required
        self.actor    = actor
        super().__init__(
            f"RBAC DENIED: action='{action}' required={required.name} "
            f"actor='{actor.identity}' has={actor.clearance.name}"
        )


class ActorNotFound(PermissionError):
    """Raised when no actor context is resolvable in production mode."""
    pass


# ── Context var ───────────────────────────────────────────────────────────────

_current_actor: ContextVar[ActorContext | None] = ContextVar("_current_actor", default=None)


def set_current_actor(actor: ActorContext) -> Any:
    return _current_actor.set(actor)


def get_current_actor() -> ActorContext | None:
    return _current_actor.get()


def clear_current_actor() -> None:
    _current_actor.set(None)


# ── RBAC Manager ──────────────────────────────────────────────────────────────

class RBACManager:
    def resolve_actor(self, action: str, kwargs: dict) -> ActorContext:
        # 1. Explicit kwarg takes priority
        actor = kwargs.get("actor")
        if isinstance(actor, ActorContext):
            return actor

        # 2. Contextvar (set by caller or task that owns this action)
        ctx = _current_actor.get()
        if ctx is not None:
            return ctx

        # 3. Env fallback — ONLY in dev/test; never silently in production
        env = os.environ.get("JARVIS_ENV", "production").lower()
        if env in ("development", "dev", "test"):
            raw = os.environ.get("RBAC_DEFAULT_ACTOR", "")
            if raw:
                try:
                    parts = raw.split(":", 1)
                    identity = parts[0]
                    level = ClearanceLevel[parts[1]] if len(parts) > 1 else ClearanceLevel.Analyst
                    return ActorContext(identity=identity, clearance=level, source="env")
                except (KeyError, ValueError):
                    pass

        raise ActorNotFound(
            f"No actor context for action '{action}' in {env} mode. "
            "Set actor= kwarg, use set_current_actor(), or set RBAC_DEFAULT_ACTOR in dev/test."
        )

    def check(self, action: str, required: ClearanceLevel, actor: ActorContext) -> None:
        if actor.clearance < required:
            logger.warning(
                "RBAC DENIED  | action=%-40s required=%-10s actor=%-30s has=%-10s ts=%.3f",
                action, required.name, actor.identity, actor.clearance.name, time.time(),
            )
            raise PermissionDenied(action, required, actor)
        logger.debug(
            "RBAC ALLOWED | action=%s actor=%s clearance=%s",
            action, actor.identity, actor.clearance.name,
        )


_rbac = RBACManager()


# ── Decorator ─────────────────────────────────────────────────────────────────

def requires_clearance(level: ClearanceLevel) -> Callable:
    """Decorator enforcing minimum clearance on async (or sync) functions.

    Actor resolution order:
      1. `actor=` kwarg passed by caller
      2. ContextVar set by the enclosing async task
      3. RBAC_DEFAULT_ACTOR env var (dev/test only)
    Raises PermissionDenied or ActorNotFound before any side effects run.
    """
    def decorator(fn: Callable) -> Callable:
        action = fn.__qualname__

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                actor = _rbac.resolve_actor(action, kwargs)
                _rbac.check(action, level, actor)
                kwargs.pop("actor", None)
                return await fn(*args, **kwargs)
            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                actor = _rbac.resolve_actor(action, kwargs)
                _rbac.check(action, level, actor)
                kwargs.pop("actor", None)
                return fn(*args, **kwargs)
            return sync_wrapper

    return decorator
