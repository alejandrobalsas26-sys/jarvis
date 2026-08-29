"""
core/world_status.py — V69 M63: the structured status surface.

THE INVARIANT
-------------
    If the language model is unavailable, "Is service X online?" must still be
    answerable.

So every function here is a pure read over :class:`core.world_state.WorldState`
and returns a STRUCTURED answer first. The natural-language rendering is a
deterministic second step (:func:`narrate`) built by string formatting, not by
inference — a model may LATER be asked to say it more nicely, but the answer
does not depend on one existing.

This module deliberately does not duplicate :mod:`core.ops_query`, which is the
repository's existing intent-routed operational query engine over the asset
graph and remains the richer surface. What is added here is the small set of
situational questions that must keep working when nothing else does, plus the
bounded payload the HUD consumes.
"""
from __future__ import annotations

from core.asset_graph import AssetType, asset_id
from core.world_bounds import DEFAULT_BOUNDS, WorldBounds
from core.world_state import EntityStatus, WorldState

SCHEMA_VERSION = "world-status-1"


def _entity_lookup(state: WorldState, name: str):
    """Resolve a human-typed name to an entity. Exact id first, then identity.

    Never guesses across types when the answer is ambiguous: it reports the
    ambiguity, because silently picking one of three hosts called "web" is how
    a status surface starts lying.
    """
    direct = state.get_entity(name)
    if direct is not None:
        return direct, []
    lowered = name.strip().lower()
    for etype in AssetType:
        candidate = state.get_entity(asset_id(etype, lowered))
        if candidate is not None:
            return candidate, []
    matches = [e for e in state.all_entities()
               if e.display_name.strip().lower() == lowered]
    if len(matches) == 1:
        return matches[0], []
    return None, [m.entity_id for m in matches]


def jarvis_status(state: WorldState, *, doctor_report=None) -> dict:
    """"Jarvis, status" — deterministic, and available with no model loaded."""
    summary = state.environment_summary()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "answered_by": "world_state",
        "requires_llm": False,
        "environment": summary,
        "unhealthy": [e.to_dict() for e in state.unhealthy_entities()[:16]],
        "recent_changes": [c.to_dict() for c in state.recently_changed(limit=16)],
    }
    if doctor_report is not None:
        report = (doctor_report.to_dict() if hasattr(doctor_report, "to_dict")
                  else doctor_report)
        payload["runtime_doctor"] = {
            "overall": report.get("overall"),
            "counts": report.get("counts", {}),
            "blocked": report.get("blocked", []),
        }
    return payload


def service_status(state: WorldState, name: str) -> dict:
    """"Is X online?" — the question that must never need inference."""
    entity, ambiguous = _entity_lookup(state, name)
    if entity is None:
        return {
            "schema_version": SCHEMA_VERSION, "query": name, "known": False,
            "requires_llm": False,
            "answer": (f"{name!r} is ambiguous: {', '.join(ambiguous)}"
                       if ambiguous else
                       f"{name!r} is not a known entity in the World State"),
            "candidates": ambiguous,
        }
    return {
        "schema_version": SCHEMA_VERSION, "query": name, "known": True,
        "requires_llm": False,
        "entity": entity.to_dict(),
        "online": entity.status is EntityStatus.ONLINE,
        "stale": entity.is_stale,
        "answer": (f"{entity.display_name} is {entity.status.value} "
                   f"(health {entity.health.value}, last observed "
                   f"{entity.freshness_s:.0f}s ago from {entity.source})"),
    }


def what_changed(state: WorldState, *, since: str | None = None,
                 within_s: float = 3_600.0, bounds: WorldBounds | None = None) -> dict:
    """"What changed?" — grounded state transitions, never a narrative guess."""
    bounds = bounds or DEFAULT_BOUNDS
    changes = (state.what_changed_since(since) if since
               else state.recently_changed(within_s=within_s))
    changes = changes[-bounds.max_aura_changes:]
    by_kind: dict[str, int] = {}
    for c in changes:
        by_kind[c.kind.value] = by_kind.get(c.kind.value, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION, "requires_llm": False,
        "window": since or f"last {int(within_s)}s",
        "count": len(changes),
        "by_kind": dict(sorted(by_kind.items())),
        "changes": [c.to_dict() for c in changes],
        "lines": [c.describe() for c in changes],
    }


def dependency_impact(state: WorldState, name: str) -> dict:
    """"What depends on X?" / "what breaks if X disappears?" — graph-derived."""
    entity, ambiguous = _entity_lookup(state, name)
    if entity is None:
        return {"schema_version": SCHEMA_VERSION, "query": name, "known": False,
                "requires_llm": False, "candidates": ambiguous,
                "answer": f"{name!r} is not a known entity"}
    impact = state.impact_of(entity.entity_id)
    depends_on = state.dependencies_of(entity.entity_id)
    return {
        "schema_version": SCHEMA_VERSION, "query": name, "known": True,
        "requires_llm": False,
        "entity_id": entity.entity_id,
        "depends_on": depends_on,
        "impact": impact,
        "answer": (f"{entity.display_name} has {impact['blast_radius']} dependent "
                   f"entities and itself depends on {len(depends_on)}"),
    }


def unhealthy_report(state: WorldState) -> dict:
    bad = state.unhealthy_entities()
    return {
        "schema_version": SCHEMA_VERSION, "requires_llm": False,
        "count": len(bad),
        "entities": [e.to_dict() for e in bad[:32]],
        "lines": [f"{e.display_name}: {e.status.value}/{e.health.value}"
                  for e in bad[:32]],
    }


def security_summary(state: WorldState) -> dict:
    """Security-relevant entities and their state. Counts, never raw alerts."""
    kinds = (AssetType.SECURITY_SENSOR, AssetType.SECURITY_CONTROL,
             AssetType.ALERT_SOURCE, AssetType.FIREWALL)
    entities = [e for k in kinds for e in state.entities_by_type(k)]
    unhealthy = [e for e in entities
                 if e.status in (EntityStatus.OFFLINE, EntityStatus.DEGRADED,
                                 EntityStatus.STALE, EntityStatus.GONE)]
    return {
        "schema_version": SCHEMA_VERSION, "requires_llm": False,
        "controls": len(entities),
        "unhealthy_controls": len(unhealthy),
        "entities": [e.to_dict() for e in entities[:32]],
        "blind_spots": [e.display_name for e in unhealthy[:16]],
    }


def pending_approvals(bridge_outcome=None) -> dict:
    """Proposals waiting on a human. An empty list is the normal state."""
    delivered = []
    if bridge_outcome is not None:
        raw = (bridge_outcome.to_dict() if hasattr(bridge_outcome, "to_dict")
               else bridge_outcome)
        delivered = [d for d in raw.get("delivered", []) if d.get("level") == "ask"]
    return {
        "schema_version": SCHEMA_VERSION, "requires_llm": False,
        "count": len(delivered), "pending": delivered[:16],
        "note": "an approval is a REQUEST; nothing here has been executed",
    }


def narrate(payload: dict) -> str:
    """Deterministic prose from a structured answer. No model is consulted."""
    if "answer" in payload:
        return str(payload["answer"])
    if "lines" in payload and payload["lines"]:
        return "; ".join(str(x) for x in payload["lines"][:8])
    env = payload.get("environment")
    if isinstance(env, dict):
        return (f"{env.get('entities', 0)} entities: {env.get('online', 0)} online, "
                f"{env.get('offline', 0)} offline, {env.get('degraded', 0)} degraded, "
                f"{env.get('stale', 0)} stale, {env.get('unhealthy', 0)} unhealthy")
    return "no situational data"


__all__ = [
    "SCHEMA_VERSION", "dependency_impact", "jarvis_status", "narrate",
    "pending_approvals", "security_summary", "service_status",
    "unhealthy_report", "what_changed",
]
