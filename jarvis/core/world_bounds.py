"""
core/world_bounds.py — V69 M63: the finite bounds the World State runs inside.

Every collection the situational fabric can grow from OUTSIDE input has a named,
finite ceiling, and every one of them lives here rather than as a literal buried
in the module that happens to enforce it. That is the repository's "Rule of
Silicon" convention (see ``core/asset_graph.py``'s ``_MAX_*`` block, which this
does not replace — the graph keeps owning its own traversal limits) applied to
the one subsystem whose input volume is set by the environment rather than by
the operator.

WHY A SEPARATE MODULE
---------------------
A connector reads a homelab. A homelab can misbehave: a runaway container loop
can publish ten thousand ports, a compromised sensor can emit alerts as fast as
the socket accepts them, and a misconfigured Zeek can hand back a log that never
ends. None of those may be able to exhaust this process. Naming the ceilings in
one file makes "is there an unbounded path?" a question that is answered by
reading forty lines instead of auditing every connector.

NOTHING HERE IS A SECURITY BOUNDARY BY ITSELF. These are resource ceilings. The
authorization boundary is :mod:`core.environment_registry` (which targets may be
read at all) and :mod:`tools.executor` (what may cause an effect). A bound that
is hit is a DEGRADED reading, never a silent truncation that reads as success.

OVERRIDING
----------
:func:`load_bounds` reads the operator-tunable YAML layer
(:mod:`core.config_manager`) under the ``world_state`` key, clamps every value
into a reviewed range and returns a frozen record. An override that is missing,
malformed, negative or absurd is REFUSED back to the default rather than
applied: a bound is a safety property, so an unparseable one fails closed to the
reviewed value instead of to "unlimited".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from loguru import logger

SCHEMA_VERSION = "world-bounds-1"


@dataclass(frozen=True)
class WorldBounds:
    """The complete set of finite ceilings. No field may be zero or negative."""

    # ── state size ───────────────────────────────────────────────────────────
    max_entities: int = 4_096
    max_edges: int = 8_192
    max_observations_per_ingest: int = 2_048
    max_change_history: int = 1_024

    # ── event fabric ─────────────────────────────────────────────────────────
    max_queue_depth: int = 1_024
    max_subscribers: int = 32
    max_retries: int = 3

    # ── graph traversal ──────────────────────────────────────────────────────
    max_traversal_depth: int = 4
    max_traversal_nodes: int = 256

    # ── connectors ───────────────────────────────────────────────────────────
    max_connector_concurrency: int = 4
    connector_timeout_s: float = 10.0
    max_connector_response_bytes: int = 1_048_576      # 1 MiB
    max_connector_items: int = 512

    # ── observation shape ────────────────────────────────────────────────────
    max_payload_bytes: int = 65_536                    # 64 KiB per observation
    max_text_field_chars: int = 4_096

    # ── outward payloads ─────────────────────────────────────────────────────
    max_aura_entities: int = 128
    max_aura_changes: int = 64

    # ── memory boundary (§ world-state vs Memory Fabric) ─────────────────────
    max_memory_writes_per_cycle: int = 8

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


#: The reviewed clamp range for every field. An override outside its range is
#: pulled back to the nearest end rather than honoured: the point of a bound is
#: that a config file cannot remove it.
_RANGES: dict[str, tuple[float, float]] = {
    "max_entities": (16, 65_536),
    "max_edges": (16, 131_072),
    "max_observations_per_ingest": (16, 16_384),
    "max_change_history": (16, 16_384),
    "max_queue_depth": (16, 16_384),
    "max_subscribers": (1, 256),
    "max_retries": (0, 10),
    "max_traversal_depth": (1, 8),
    "max_traversal_nodes": (8, 4_096),
    "max_connector_concurrency": (1, 32),
    "connector_timeout_s": (0.5, 120.0),
    "max_connector_response_bytes": (1_024, 16_777_216),
    "max_connector_items": (8, 8_192),
    "max_payload_bytes": (256, 1_048_576),
    "max_text_field_chars": (64, 65_536),
    "max_aura_entities": (8, 2_048),
    "max_aura_changes": (4, 1_024),
    "max_memory_writes_per_cycle": (0, 128),
}

DEFAULT_BOUNDS = WorldBounds()


def _clamp(name: str, value, fallback):
    """Coerce one override. Anything unusable returns the reviewed default."""
    lo, hi = _RANGES[name]
    is_float = isinstance(fallback, float)
    try:
        # bool is an int subclass and is never a meaningful ceiling; refuse it by
        # type rather than letting ``True`` silently become 1.
        if isinstance(value, bool):
            raise TypeError("bool is not a bound")
        num = float(value) if is_float else int(value)
    except (TypeError, ValueError):
        logger.warning(f"WORLD_BOUNDS: {name}={value!r} is not a number; "
                       f"keeping reviewed default {fallback}")
        return fallback
    if num < lo or num > hi:
        clamped = type(fallback)(min(max(num, lo), hi))
        logger.warning(f"WORLD_BOUNDS: {name}={num} is outside the reviewed range "
                       f"[{lo}, {hi}]; clamped to {clamped}")
        return clamped
    return type(fallback)(num)


def load_bounds(overrides: dict | None = None) -> WorldBounds:
    """Build the bounds, applying a clamped operator override layer.

    ``overrides`` is normally omitted, in which case the YAML config layer is
    consulted. An unknown key is ignored with a warning rather than raising: a
    typo in an operator's config file must not stop the fabric from starting
    with safe ceilings.
    """
    if overrides is None:
        try:
            from core.config_manager import get
            overrides = get("world_state.bounds", {}) or {}
        except Exception as exc:  # noqa: BLE001 — config layer is optional
            logger.debug(f"WORLD_BOUNDS: no config override layer ({exc})")
            overrides = {}
    if not isinstance(overrides, dict):
        logger.warning("WORLD_BOUNDS: override block is not a mapping; ignoring")
        return DEFAULT_BOUNDS

    defaults = asdict(DEFAULT_BOUNDS)
    resolved: dict[str, object] = {}
    for key, value in overrides.items():
        if key not in defaults:
            logger.warning(f"WORLD_BOUNDS: unknown bound {key!r} ignored")
            continue
        resolved[key] = _clamp(key, value, defaults[key])
    return WorldBounds(**{**defaults, **resolved})


__all__ = ["DEFAULT_BOUNDS", "SCHEMA_VERSION", "WorldBounds", "load_bounds"]
