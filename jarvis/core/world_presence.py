"""
core/world_presence.py — V69 M63: World State -> Presence, and the memory boundary.

TWO JOBS, BOTH ABOUT RESTRAINT
------------------------------
1. Turn situational changes into PROPOSALS on the existing Presence ladder
   (:mod:`core.presence`), capped so that no environment event can ever reach
   ``ACT``.
2. Decide the very small subset of situational facts that deserve to become
   durable memory, so telemetry cannot flood the Memory Fabric.

WHY ACT IS STRUCTURALLY UNREACHABLE HERE
----------------------------------------
:data:`MAX_WORLD_LEVEL` is ``ASK``, and :func:`change_to_presence_event` clamps
every event it builds to it. It also never sets ``action_tool`` or
``action_target``, which are the only fields that could make the Presence
Engine consider an ACT proposal at all. So there are two independent reasons an
environment observation cannot propose an action, and a test asserts both.

That is deliberate. A critical alert may make JARVIS notify, suggest an
investigation, or ask for permission. It may never decide on its own to restart
a container, block an address or re-image a host: those are effects, effects go
through :mod:`tools.executor`, and the executor answers to authority, scope,
risk class and HITL — not to a sensor reading.

THE MEMORY BOUNDARY
-------------------
The Memory Fabric holds history, decisions and durable context. A container
that flapped twice at 3am is not any of those. :func:`memory_worthy` keeps only
transitions that are DURABLE (an entity appearing or disappearing, a health
crossing into or out of CRITICAL, an operator decision) and drops the rest,
under a per-cycle write ceiling. Everything discarded is still in the World
State's own bounded change log, which is where recent environment truth belongs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from core.presence import (
    PresenceDecision,
    PresenceEngine,
    PresenceEvent,
    PresenceLevel,
    PresenceSignal,
    Urgency,
)
from core.world_bounds import DEFAULT_BOUNDS, WorldBounds
from core.world_state import ChangeKind, EntityHealth, StateChange, WorldState

SCHEMA_VERSION = "world-presence-1"

#: The hard ceiling for anything originating in the environment. Never raise
#: this. An environment event that "obviously" warrants action still only
#: warrants ASKING, because the thing that decides is the operator.
MAX_WORLD_LEVEL = PresenceLevel.ASK

#: How urgent each kind of change is. Absent kinds are ROUTINE.
_URGENCY: dict[ChangeKind, Urgency] = {
    ChangeKind.ENTITY_APPEARED: Urgency.ROUTINE,
    ChangeKind.ENTITY_STALE: Urgency.ROUTINE,
    ChangeKind.ENTITY_DISAPPEARED: Urgency.ELEVATED,
    ChangeKind.STATUS_CHANGED: Urgency.ELEVATED,
    ChangeKind.HEALTH_CHANGED: Urgency.HIGH,
    ChangeKind.SERVICE_STARTED: Urgency.ROUTINE,
    ChangeKind.SERVICE_STOPPED: Urgency.HIGH,
    ChangeKind.ADDRESS_CHANGED: Urgency.ELEVATED,
    ChangeKind.DEPENDENCY_CHANGED: Urgency.ELEVATED,
    ChangeKind.ALERT_SEVERITY_CHANGED: Urgency.HIGH,
    ChangeKind.SENSOR_THRESHOLD_CROSSED: Urgency.HIGH,
}

#: Changes that are DURABLE environment facts rather than passing telemetry.
_MEMORY_WORTHY_KINDS = frozenset({
    ChangeKind.ENTITY_APPEARED,
    ChangeKind.ENTITY_DISAPPEARED,
    ChangeKind.DEPENDENCY_CHANGED,
})


def urgency_for(change: StateChange) -> Urgency:
    """Urgency from the KIND and the severity it moved to — never from prose."""
    base = _URGENCY.get(change.kind, Urgency.ROUTINE)
    after = (change.after or "").strip().lower()
    if after in ("critical", "red", "fail", "failed"):
        return Urgency.CRITICAL
    if change.kind is ChangeKind.ALERT_SEVERITY_CHANGED and after in ("high", "sev1"):
        return Urgency.CRITICAL
    return base


def change_to_presence_event(change: StateChange) -> PresenceEvent:
    """Build a proposal for the Presence ladder, capped at ASK.

    ``action_tool`` and ``action_target`` are deliberately left unset. They are
    the only inputs that make :meth:`PresenceEngine.evaluate` consider ACT, so
    omitting them removes the possibility rather than merely declining it.
    """
    urgency = urgency_for(change)
    desired = (PresenceLevel.ASK if urgency >= Urgency.HIGH
               else PresenceLevel.SUGGEST)
    return PresenceEvent(
        key=f"world:{change.kind.value}:{change.entity_id}",
        urgency=urgency,
        message=change.describe(),
        # min() is redundant given the branch above and is kept anyway: if the
        # branch is ever edited, the ceiling still holds.
        desired_level=min(desired, MAX_WORLD_LEVEL),
        requires_work=change.kind in (ChangeKind.HEALTH_CHANGED,
                                      ChangeKind.ALERT_SEVERITY_CHANGED),
        action_tool=None,
        action_target=None,
    )


@dataclass
class BridgeOutcome:
    """What one bridge cycle proposed — and what it did NOT do."""
    considered: int = 0
    delivered: list[dict] = field(default_factory=list)
    suppressed: int = 0
    memory_writes: list[str] = field(default_factory=list)
    world_effects: int = 0          # structurally always 0; asserted by test
    act_proposals: int = 0          # structurally always 0; asserted by test

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "considered": self.considered,
            "delivered": self.delivered[:32],
            "delivered_count": len(self.delivered),
            "suppressed": self.suppressed,
            "memory_writes": self.memory_writes[:16],
            "world_effects": self.world_effects,
            "act_proposals": self.act_proposals,
            "can_act": False,
        }


class WorldPresenceBridge:
    """Feeds World State changes to the Presence Engine. Causes no effects.

    This class has no reference to :mod:`tools.executor`, no tool handle and no
    network client. The strongest thing it can do is return a dictionary saying
    a human might want to look at something.
    """

    def __init__(self, state: WorldState, *, engine: PresenceEngine | None = None,
                 bounds: WorldBounds | None = None) -> None:
        self.state = state
        self.bounds = bounds or DEFAULT_BOUNDS
        if engine is not None:
            self.engine = engine
        else:
            from core.presence import presence
            self.engine = presence

    def run_cycle(self, signal: PresenceSignal, *, since: str | None = None,
                  ) -> BridgeOutcome:
        """Evaluate recent changes. Returns proposals; performs no action."""
        outcome = BridgeOutcome()
        changes = self.state.what_changed_since(since)
        for change in changes[-self.bounds.max_change_history:]:
            outcome.considered += 1
            event = change_to_presence_event(change)
            if event.desired_level > MAX_WORLD_LEVEL:
                # Unreachable by construction; kept as a live assertion rather
                # than a comment, because a future edit could make it reachable.
                raise RuntimeError(
                    f"world presence event {event.key!r} asked for "
                    f"{event.desired_level.name}, above the {MAX_WORLD_LEVEL.name} "
                    f"ceiling the environment is allowed to reach")
            decision: PresenceDecision = self.engine.evaluate(event, signal)
            if decision.level >= PresenceLevel.ACT:
                raise RuntimeError(
                    f"presence returned ACT for environment event {event.key!r}; "
                    f"environment observations may never reach ACT")
            if decision.deliver:
                outcome.delivered.append({
                    "key": event.key, "level": decision.level.name.lower(),
                    "urgency": decision.urgency.name.lower(),
                    "message": event.message[:280],
                    "requires_gates": decision.requires_gates,
                    "kind": change.kind.value, "entity_id": change.entity_id,
                })
            else:
                outcome.suppressed += 1
        outcome.memory_writes = [c.entity_id for c in select_for_memory(
            changes, bounds=self.bounds)]
        return outcome


# ══════════════════════════════════════════════════════════════════════════════
#  §21 — the memory boundary
# ══════════════════════════════════════════════════════════════════════════════
def memory_worthy(change: StateChange) -> bool:
    """Whether one transition is durable enough for the Memory Fabric.

    Deliberately strict. A status flap, an address change and a freshness
    expiry are all REAL and all belong in the World State's change log; none of
    them is a thing the assistant should still be recalling next month.
    """
    if change.kind in _MEMORY_WORTHY_KINDS:
        return True
    if change.kind is ChangeKind.HEALTH_CHANGED:
        # Only a crossing INTO or OUT OF critical is durable.
        return EntityHealth.CRITICAL.value in (
            (change.before or "").lower(), (change.after or "").lower())
    return False


def select_for_memory(changes, *, bounds: WorldBounds | None = None) -> list[StateChange]:
    """The bounded subset that may be persisted, newest first.

    The ceiling is per-cycle and finite: a storm that produces ten thousand
    durable-looking transitions still writes at most
    ``max_memory_writes_per_cycle`` of them, and the overflow is logged rather
    than silently dropped.
    """
    bounds = bounds or DEFAULT_BOUNDS
    worthy = [c for c in changes if memory_worthy(c)]
    if len(worthy) > bounds.max_memory_writes_per_cycle:
        logger.warning(
            f"WORLD_PRESENCE: {len(worthy)} durable transitions exceed the "
            f"{bounds.max_memory_writes_per_cycle}-per-cycle memory ceiling; "
            f"keeping the newest and leaving the rest in the change log")
    return list(reversed(worthy))[: bounds.max_memory_writes_per_cycle]


def memory_summary(change: StateChange) -> str:
    """The one durable sentence. A summary, never the raw telemetry."""
    return (f"[world] {change.describe()} "
            f"(source {change.source}, confidence {change.confidence:.2f})")


__all__ = [
    "MAX_WORLD_LEVEL", "SCHEMA_VERSION", "BridgeOutcome", "WorldPresenceBridge",
    "change_to_presence_event", "memory_summary", "memory_worthy",
    "select_for_memory", "urgency_for",
]
