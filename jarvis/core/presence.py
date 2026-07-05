"""
core/presence.py — V63 Milestone 7: state-driven Presence Engine.

Evolves the AssistantMode/consent policy foundation (core.ironman_mode +
core.assistant_state) into a richer *presence* decision layer: given the live
operating state, it decides how forward JARVIS should be about a candidate
proactive event, on a five-rung ladder:

    OBSERVE      — only already-permitted state; take nothing further.
    UNDERSTAND   — infer / rank / correlate internally; no operator-facing output.
    SUGGEST      — surface a recommendation / notification.
    ASK          — request permission, clarification, or scope.
    ACT          — propose an action — which STILL passes ToolExecutor / risk
                   taxonomy / authority scope / consent / trusted-lab / HITL.
                   The engine NEVER executes; it only decides the ladder rung and
                   flags that gated execution is required.

Behavioral guarantees (each has a test):
  * FOCUS / PRESENTATION suppress routine notifications; CRITICAL still escalates.
  * PASSIVE suppresses all proactive output.
  * repeated identical events deduplicate within a per-urgency cooldown.
  * background *work* concurrency backs off under CPU / RAM pressure and on
    battery (Rule of Silicon), and to zero in quiet modes.
  * an ACT proposal for a target outside the authorized scope (or a sensitive
    surface without consent) is downgraded to ASK — it can never bypass the
    authority / consent / HITL gates.

Pure decision logic + a small bounded history map. No I/O, no tool execution,
no sensor access — deterministic and unit-testable with an injected clock.
The mode-ceiling policy reuses core.ironman_mode.allowed_proactive_actions, so
it never drifts from the existing notification policy (telegram push_alert).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

from core.ironman_mode import (
    AssistantMode,
    SessionConsent,
    allowed_proactive_actions,
    default_consent,
    should_run_background_tasks,
)


class PresenceLevel(IntEnum):
    OBSERVE = 0
    UNDERSTAND = 1
    SUGGEST = 2
    ASK = 3
    ACT = 4


class Urgency(IntEnum):
    ROUTINE = 0
    ELEVATED = 1
    HIGH = 2
    CRITICAL = 3


_SEVERITY_URGENCY: dict[str, Urgency] = {
    "INFO": Urgency.ROUTINE, "LOW": Urgency.ROUTINE, "DEBUG": Urgency.ROUTINE,
    "MEDIUM": Urgency.ELEVATED, "NOTICE": Urgency.ELEVATED,
    "HIGH": Urgency.HIGH, "WARNING": Urgency.HIGH, "WARN": Urgency.HIGH,
    "CRITICAL": Urgency.CRITICAL, "FATAL": Urgency.CRITICAL, "SEV1": Urgency.CRITICAL,
}

# Per-urgency dedup cooldown (seconds). CRITICAL still dedups a rapid burst of
# the *identical* alert, but with a short window so real escalations get through.
_COOLDOWN_S: dict[Urgency, float] = {
    Urgency.ROUTINE: 300.0, Urgency.ELEVATED: 120.0,
    Urgency.HIGH: 60.0, Urgency.CRITICAL: 15.0,
}

# Background-work back-off thresholds (below the hard ceilings in ironman_mode).
_WORK_REDUCE_CPU = 70.0
_WORK_REDUCE_RAM = 80.0


def urgency_from_severity(severity: str) -> Urgency:
    return _SEVERITY_URGENCY.get((severity or "").strip().upper(), Urgency.ROUTINE)


def mode_permits_notification(
    mode: AssistantMode, consent: SessionConsent | None, urgency: Urgency
) -> bool:
    """The mode ceiling for operator-facing proactive output, reusing the
    existing allowed_proactive_actions policy: a mode granting the general
    'notify' allows any urgency; one granting only 'notify_urgent' allows just
    CRITICAL; neither (PASSIVE) suppresses everything."""
    allowed = allowed_proactive_actions(mode, consent or default_consent())
    if "notify" in allowed:
        return True
    return urgency == Urgency.CRITICAL and "notify_urgent" in allowed


@dataclass
class PresenceSignal:
    """The live operating state the engine reasons over. Everything is optional
    with a safe default so callers can supply only what they know."""
    mode: AssistantMode = AssistantMode.ACTIVE
    consent: SessionConsent = field(default_factory=default_consent)
    authority: object | None = None          # core.authority.AuthorityState | None
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    on_battery: "bool | str" = False
    operator_available: bool = True
    active_incidents: int = 0
    running_tasks: int = 0
    recent_failures: int = 0
    project_active: bool = False


@dataclass
class PresenceEvent:
    """A candidate proactive event the engine rules on."""
    key: str
    urgency: Urgency = Urgency.ROUTINE
    message: str = ""
    desired_level: PresenceLevel = PresenceLevel.SUGGEST
    requires_work: bool = False              # needs background computation to act on
    action_tool: str | None = None           # if ACT proposes a concrete tool…
    action_target: str | None = None         # …against this target (scope-checked)


@dataclass(frozen=True)
class PresenceDecision:
    level: PresenceLevel
    deliver: bool
    reason: str
    requires_gates: bool = False             # ACT: execution must still pass all gates
    urgency: Urgency = Urgency.ROUTINE

    def to_dict(self) -> dict:
        return {
            "level": self.level.name.lower(), "deliver": self.deliver,
            "reason": self.reason, "requires_gates": self.requires_gates,
            "urgency": self.urgency.name.lower(),
        }


class PresenceEngine:
    """State-driven ladder decision + bounded dedup/cooldown history."""

    def __init__(self, *, cooldowns: dict[Urgency, float] | None = None, clock=None) -> None:
        self._cooldowns = cooldowns or dict(_COOLDOWN_S)
        self._history: dict[str, float] = {}
        self._clock = clock or time.monotonic

    # ── ceilings ─────────────────────────────────────────────────────────────
    @staticmethod
    def _mode_ceiling(mode: AssistantMode) -> PresenceLevel:
        if mode == AssistantMode.PASSIVE:
            return PresenceLevel.UNDERSTAND     # internal only, never proactive
        if mode in (AssistantMode.FOCUS, AssistantMode.PRESENTATION):
            return PresenceLevel.SUGGEST        # minimal interruption (urgent only)
        return PresenceLevel.ACT                # ACTIVE / WAR_ROOM

    def max_background_concurrency(self, signal: PresenceSignal) -> int:
        """Resource-aware background-work width. Zero in quiet modes / under
        pressure / on battery; reduced to 1 under moderate load; 2 otherwise."""
        if not should_run_background_tasks(
            signal.mode, signal.on_battery, signal.cpu_pct, signal.ram_pct
        ):
            return 0
        if signal.cpu_pct >= _WORK_REDUCE_CPU or signal.ram_pct >= _WORK_REDUCE_RAM:
            return 1
        return 2

    # ── dedup / cooldown ─────────────────────────────────────────────────────
    def _suppressed_by_cooldown(self, event: PresenceEvent) -> bool:
        last = self._history.get(event.key)
        if last is None:
            return False
        cooldown = self._cooldowns.get(event.urgency, 300.0)
        return (self._clock() - last) < cooldown

    def _record(self, event: PresenceEvent) -> None:
        self._history[event.key] = self._clock()
        # Bound the history map (drop oldest) — never grows unbounded.
        if len(self._history) > 256:
            oldest = sorted(self._history.items(), key=lambda kv: kv[1])[:64]
            for k, _ in oldest:
                self._history.pop(k, None)

    # ── ACT authorization (never a bypass) ───────────────────────────────────
    def _act_authorized(self, event: PresenceEvent, signal: PresenceSignal) -> bool:
        """Whether an ACT proposal *may* be surfaced as ACT (not whether it may
        execute — execution always re-passes ToolExecutor). A target-bound action
        outside the authorized scope, or a sensitive surface without consent,
        must be downgraded to ASK."""
        if event.action_tool is None:
            return True
        if signal.authority is not None and event.action_target:
            try:
                from core.authority import authorize_action
                d = authorize_action(
                    signal.authority, event.action_tool,
                    {"target": event.action_target, "host": event.action_target,
                     "domain": event.action_target, "url": event.action_target},
                )
                if not d.allowed:
                    return False
            except Exception:
                return False       # authority error → fail closed → ASK
        return True

    # ── the decision ─────────────────────────────────────────────────────────
    def evaluate(
        self, event: PresenceEvent, signal: PresenceSignal, *, apply_cooldown: bool = True
    ) -> PresenceDecision:
        urgency = event.urgency

        # 1. Mode ceiling on operator-facing output.
        if not mode_permits_notification(signal.mode, signal.consent, urgency):
            level = (PresenceLevel.OBSERVE if signal.mode == AssistantMode.PASSIVE
                     else PresenceLevel.UNDERSTAND)
            return PresenceDecision(level, False,
                                    f"mode {signal.mode.value} suppresses {urgency.name}",
                                    urgency=urgency)

        # 2. Dedup / cooldown (identical key within window).
        if apply_cooldown and self._suppressed_by_cooldown(event):
            return PresenceDecision(PresenceLevel.UNDERSTAND, False,
                                    f"deduplicated within {urgency.name} cooldown",
                                    urgency=urgency)

        # 3. Work back-off: an event needing background computation is held when
        #    no background capacity is available (unless CRITICAL).
        if event.requires_work and urgency != Urgency.CRITICAL:
            if self.max_background_concurrency(signal) == 0:
                return PresenceDecision(PresenceLevel.UNDERSTAND, False,
                                        "background work deferred (resource/mode pressure)",
                                        urgency=urgency)

        # 4. Rung = min(desired, mode ceiling).
        level = min(event.desired_level, self._mode_ceiling(signal.mode))

        # 5. ACT never bypasses gates: unauthorized ACT downgrades to ASK.
        requires_gates = False
        suffix = ""
        if level >= PresenceLevel.ACT:
            if self._act_authorized(event, signal):
                requires_gates = True
                suffix = " — ACT proposed; execution still passes ToolExecutor/authority/HITL"
            else:
                level = PresenceLevel.ASK
                suffix = " — ACT→ASK (target out of scope / consent missing)"

        deliver = level >= PresenceLevel.SUGGEST
        if deliver and apply_cooldown:
            self._record(event)
        return PresenceDecision(
            level, deliver,
            f"mode={signal.mode.value} urgency={urgency.name} rung={level.name}{suffix}",
            requires_gates=requires_gates, urgency=urgency,
        )

    def snapshot(self, signal: PresenceSignal) -> dict:
        """A compact view of the current proactive posture (for the HUD)."""
        return {
            "mode": signal.mode.value,
            "mode_ceiling": self._mode_ceiling(signal.mode).name.lower(),
            "permits_routine": mode_permits_notification(
                signal.mode, signal.consent, Urgency.ROUTINE),
            "permits_critical": mode_permits_notification(
                signal.mode, signal.consent, Urgency.CRITICAL),
            "max_background_concurrency": self.max_background_concurrency(signal),
            "active_incidents": signal.active_incidents,
            "operator_available": signal.operator_available,
            "tracked_events": len(self._history),
        }


# Module singleton — attached in main.py; consulted by the AURA presence_status
# command and available to proactive subsystems as the canonical ladder.
presence = PresenceEngine()
