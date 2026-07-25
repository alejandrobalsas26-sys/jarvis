"""core/recovery_supervisor.py — V69 M60.4: bounded restart policy for services.

WHAT WAS MISSING
----------------
``core.task_watchdog`` restarts a failed task with ``min(30 * 2**n, 300)`` backoff and
a monotonic ``restart_count`` that never decays. That gives quiet logs, but it has no
restart WINDOW, no circuit breaker, no criticality and no notion of an operation that
must never be restarted. A service that fails every 31 s is restarted forever, and a
task that was in the middle of an EFFECTFUL operation is restarted exactly like a
stateless probe.

``core.optional_service`` has the opposite gap: a truthful lifecycle and a bounded,
cancellation-safe stop — but no restart at all.

This module is the POLICY layer between them. It is not a third supervisor: it owns no
event loop, creates no task of its own, and drives the two existing seams. Every
decision it makes is a pure function of recorded counters, so the whole policy is
unit-testable without a running loop.

THE SIX REFUSALS (each one is a live-runtime failure mode, not a hypothetical)
-----------------------------------------------------------------------------
  1. never after STOPPING            — a restart during shutdown resurrects a writer
                                       after storage was already flushed;
  2. never a duplicate instance      — two AURA servers race for the same port;
  3. never within the cooldown       — that is what a restart storm IS;
  4. never past the circuit breaker  — repeated failure in a window is a broken
                                       service, not a transient one;
  5. never an effectful operation    — a half-finished runbook step, a HITL prompt,
                                       a semantic migration or a backup restore must
                                       be resumed by an operator, never by a policy;
  6. never READY without a probe     — a restarted service claims DEGRADED until its
                                       own health probe passes.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger


class ServiceClass(str, Enum):
    """What a failure of this service MEANS."""

    CRITICAL = "CRITICAL"        # degrades the runtime truthfully; may block CORE
    OPTIONAL = "OPTIONAL"        # must NEVER block text interaction
    BACKGROUND = "BACKGROUND"    # best-effort producers (collectors, feeds)
    ONE_SHOT = "ONE_SHOT"        # runs once; completion is success, never restarted


class CircuitState(str, Enum):
    CLOSED = "CLOSED"            # restarts permitted
    OPEN = "OPEN"                # too many failures in the window — refusing
    HALF_OPEN = "HALF_OPEN"      # cooldown elapsed; ONE probe restart permitted


class RestartDecision(str, Enum):
    RESTART = "RESTART"
    REFUSED_STOPPING = "REFUSED_STOPPING"
    REFUSED_DUPLICATE = "REFUSED_DUPLICATE"
    REFUSED_BACKOFF = "REFUSED_BACKOFF"
    REFUSED_CIRCUIT_OPEN = "REFUSED_CIRCUIT_OPEN"
    REFUSED_EFFECTFUL = "REFUSED_EFFECTFUL"
    REFUSED_POLICY = "REFUSED_POLICY"
    REFUSED_BUDGET = "REFUSED_BUDGET"


# Operations that are NEVER eligible for automatic restart (M60.4.1).
INELIGIBLE_OPERATIONS: frozenset[str] = frozenset({
    "active_user_turn", "tool_execution", "hitl_prompt", "effectful_runbook_step",
    "semantic_migration", "backup_restore", "deployment_apply",
})

MAX_RESTART_HISTORY = 20


@dataclass
class RecoveryPolicy:
    """Bounded restart policy. Every field is a hard limit, not a suggestion."""

    max_restarts: int = 3                 # within `window_s`
    window_s: float = 300.0               # the restart window
    base_backoff_s: float = 2.0
    max_backoff_s: float = 60.0
    cooldown_s: float = 120.0             # OPEN -> HALF_OPEN
    startup_timeout_s: float = 20.0
    shutdown_timeout_s: float = 5.0
    notify_operator_after: int = 2        # failures before one operator-visible line
    auto_restart: bool = True

    def backoff_for(self, attempt: int) -> float:
        """Exponential backoff, capped. Attempt 0 waits ``base_backoff_s``."""
        return min(self.max_backoff_s,
                   self.base_backoff_s * (2 ** max(0, int(attempt))))

    def snapshot(self) -> dict:
        return {"max_restarts": self.max_restarts, "window_s": self.window_s,
                "base_backoff_s": self.base_backoff_s,
                "max_backoff_s": self.max_backoff_s, "cooldown_s": self.cooldown_s,
                "startup_timeout_s": self.startup_timeout_s,
                "shutdown_timeout_s": self.shutdown_timeout_s,
                "auto_restart": self.auto_restart}


# Sensible per-class defaults. OPTIONAL is the permissive one BECAUSE its failures
# cannot block text; CRITICAL restarts less and reports more loudly.
_CLASS_DEFAULTS: dict[ServiceClass, RecoveryPolicy] = {
    ServiceClass.CRITICAL: RecoveryPolicy(max_restarts=2, window_s=300.0,
                                          cooldown_s=180.0, notify_operator_after=1),
    ServiceClass.OPTIONAL: RecoveryPolicy(max_restarts=3, window_s=300.0),
    ServiceClass.BACKGROUND: RecoveryPolicy(max_restarts=5, window_s=600.0,
                                            base_backoff_s=5.0, max_backoff_s=120.0),
    ServiceClass.ONE_SHOT: RecoveryPolicy(max_restarts=0, auto_restart=False),
}


def policy_for(service_class: ServiceClass) -> RecoveryPolicy:
    return RecoveryPolicy(**_CLASS_DEFAULTS[service_class].__dict__)


@dataclass
class SupervisedService:
    """One supervised service's bounded recovery state. Content-free."""

    name: str
    service_class: ServiceClass = ServiceClass.OPTIONAL
    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    circuit: CircuitState = CircuitState.CLOSED
    restart_attempts: int = 0             # cumulative, for telemetry
    consecutive_failures: int = 0
    last_restart_at: float | None = None
    last_failure_at: float | None = None
    opened_at: float | None = None
    last_reason: str = ""
    health_probe_passed: bool = False
    running: bool = False
    generation: int = 0                   # bumped per restart: a FRESH queue/context
    _window: "deque[float]" = field(default_factory=lambda: deque(maxlen=64))
    _history: "deque[dict]" = field(default_factory=lambda:
                                    deque(maxlen=MAX_RESTART_HISTORY))

    def prune_window(self, now: float) -> None:
        while self._window and (now - self._window[0]) > self.policy.window_s:
            self._window.popleft()

    def restarts_in_window(self, now: float) -> int:
        self.prune_window(now)
        return len(self._window)

    def snapshot(self) -> dict:
        return {
            "name": self.name, "class": self.service_class.value,
            "circuit": self.circuit.value, "running": self.running,
            "restart_attempts": self.restart_attempts,
            "consecutive_failures": self.consecutive_failures,
            "health_probe_passed": self.health_probe_passed,
            "generation": self.generation,
            "last_reason": self.last_reason[:60],
        }

    def history(self) -> list[dict]:
        return list(self._history)


class RecoverySupervisor:
    """The bounded restart decision engine. Pure, injectable clock, no event loop."""

    def __init__(self, *, clock=time.monotonic,
                 is_stopping=None, total_restart_budget: int = 40) -> None:
        self.clock = clock
        self._services: dict[str, SupervisedService] = {}
        self._is_stopping = is_stopping
        self.total_restart_budget = max(0, int(total_restart_budget))
        self.total_restarts = 0
        self.refusals: dict[str, int] = {}
        self.notifications: list[str] = []

    # ── registration ─────────────────────────────────────────────────────────
    def register(self, name: str, *,
                 service_class: ServiceClass = ServiceClass.OPTIONAL,
                 policy: RecoveryPolicy | None = None) -> SupervisedService:
        """Register (or return) a supervised service. Idempotent by name — a second
        registration NEVER creates a duplicate instance."""
        existing = self._services.get(name)
        if existing is not None:
            return existing
        svc = SupervisedService(name=name, service_class=service_class,
                                policy=policy or policy_for(service_class))
        self._services[name] = svc
        return svc

    def get(self, name: str) -> SupervisedService | None:
        return self._services.get(name)

    def mark_running(self, name: str, *, health_probe_passed: bool = False) -> None:
        """Declare a service started. It is NOT READY until its probe passes — a
        service that claims READY on start alone is the M55.1 'premature FAST
        UNAVAILABLE' bug in a different costume."""
        svc = self.register(name)
        svc.running = True
        svc.health_probe_passed = bool(health_probe_passed)

    def note_health_probe(self, name: str, passed: bool) -> None:
        svc = self.register(name)
        svc.health_probe_passed = bool(passed)
        if passed:
            svc.consecutive_failures = 0
            if svc.circuit is CircuitState.HALF_OPEN:
                svc.circuit = CircuitState.CLOSED
                svc.opened_at = None

    def note_failure(self, name: str, reason: str = "") -> SupervisedService:
        svc = self.register(name)
        svc.running = False
        svc.health_probe_passed = False
        svc.consecutive_failures += 1
        svc.last_failure_at = self.clock()
        svc.last_reason = str(reason or "")[:120]
        if svc.consecutive_failures == svc.policy.notify_operator_after:
            self._notify(f"{name}: {svc.consecutive_failures} consecutive failure(s)")
        return svc

    def note_stopped(self, name: str) -> None:
        """A DELIBERATE stop. Clears failure state so a clean stop never counts
        toward the circuit breaker."""
        svc = self.register(name)
        svc.running = False
        svc.consecutive_failures = 0
        svc.health_probe_passed = False

    # ── the decision ─────────────────────────────────────────────────────────
    def evaluate(self, name: str, *, operation: str = "",
                 instance_running: bool = False) -> tuple[RestartDecision, float]:
        """Decide whether *name* may restart NOW. Returns (decision, delay_seconds).

        Order is deliberate: the absolute refusals (stopping, duplicate, effectful)
        are evaluated BEFORE any counter, so a policy bug can never turn one of them
        into a permission.
        """
        svc = self.register(name)
        now = self.clock()

        if self._stopping():
            return self._refuse(svc, RestartDecision.REFUSED_STOPPING)
        if instance_running or svc.running:
            return self._refuse(svc, RestartDecision.REFUSED_DUPLICATE)
        if operation and operation in INELIGIBLE_OPERATIONS:
            return self._refuse(svc, RestartDecision.REFUSED_EFFECTFUL)
        if not svc.policy.auto_restart or svc.service_class is ServiceClass.ONE_SHOT:
            return self._refuse(svc, RestartDecision.REFUSED_POLICY)
        if self.total_restarts >= self.total_restart_budget:
            return self._refuse(svc, RestartDecision.REFUSED_BUDGET)

        # Circuit breaker: OPEN refuses until the cooldown elapses, then allows
        # exactly ONE probe restart (HALF_OPEN).
        if svc.circuit is CircuitState.OPEN:
            if svc.opened_at is not None and \
                    (now - svc.opened_at) >= svc.policy.cooldown_s:
                svc.circuit = CircuitState.HALF_OPEN
            else:
                return self._refuse(svc, RestartDecision.REFUSED_CIRCUIT_OPEN)

        if svc.restarts_in_window(now) >= svc.policy.max_restarts:
            svc.circuit = CircuitState.OPEN
            svc.opened_at = now
            self._notify(f"{name}: circuit OPEN after "
                         f"{svc.policy.max_restarts} restarts in "
                         f"{int(svc.policy.window_s)}s")
            return self._refuse(svc, RestartDecision.REFUSED_CIRCUIT_OPEN)

        delay = svc.policy.backoff_for(svc.consecutive_failures - 1)
        if svc.last_restart_at is not None and (now - svc.last_restart_at) < delay:
            return self._refuse(svc, RestartDecision.REFUSED_BACKOFF,
                                delay - (now - svc.last_restart_at))
        return RestartDecision.RESTART, delay

    def commit_restart(self, name: str) -> SupervisedService:
        """Record that a restart was actually performed.

        Bumping ``generation`` is the contract that a restarted service receives a
        FRESH task/queue context: a caller that reuses a queue from an older
        generation is reusing a closed event-loop object (the M54.1 QueueFull storm).
        """
        svc = self.register(name)
        now = self.clock()
        svc.restart_attempts += 1
        svc.last_restart_at = now
        svc.generation += 1
        svc.running = True
        svc.health_probe_passed = False       # must re-prove readiness
        svc._window.append(now)
        svc._history.append({"generation": svc.generation,
                             "reason": svc.last_reason[:60],
                             "restarts_in_window": svc.restarts_in_window(now)})
        self.total_restarts += 1
        return svc

    def _refuse(self, svc: SupervisedService, decision: RestartDecision,
                delay: float = 0.0) -> tuple[RestartDecision, float]:
        self.refusals[decision.value] = self.refusals.get(decision.value, 0) + 1
        return decision, max(0.0, float(delay))

    def _stopping(self) -> bool:
        fn = self._is_stopping
        if fn is None:
            try:
                from core.lifecycle import is_stopping
                return bool(is_stopping())
            except Exception:  # noqa: BLE001
                return False
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False

    def _notify(self, message: str) -> None:
        if len(self.notifications) < 20:
            self.notifications.append(str(message)[:120])
            logger.warning(f"RECOVERY: {message}")

    # ── health (bounded, content-free) ───────────────────────────────────────
    def snapshot(self) -> dict:
        services = list(self._services.values())
        ready = [s for s in services if s.running and s.health_probe_passed]
        degraded = [s for s in services if s.running and not s.health_probe_passed]
        return {
            "services_registered": len(services),
            "services_ready": len(ready),
            "services_degraded": len(degraded),
            "restart_attempts": self.total_restarts,
            "circuits_open": sum(1 for s in services
                                 if s.circuit is CircuitState.OPEN),
            "circuits_half_open": sum(1 for s in services
                                      if s.circuit is CircuitState.HALF_OPEN),
            "last_restart_reason": next(
                (s.last_reason[:60] for s in services if s.last_reason), ""),
            "refusals": dict(self.refusals),
            "restart_budget_remaining": max(
                0, self.total_restart_budget - self.total_restarts),
            "services": [s.snapshot() for s in services[:16]],
        }

    def render_panel(self, *, language: str = "es") -> str:
        english = str(language or "es").lower().startswith("en")
        s = self.snapshot()
        rows = [
            ("registered", s["services_registered"]),
            ("ready", s["services_ready"]),
            ("degraded", s["services_degraded"]),
            ("restart_attempts", s["restart_attempts"]),
            ("circuits_open", s["circuits_open"]),
            ("restart_budget_remaining", s["restart_budget_remaining"]),
        ]
        title = "RECOVERY SUPERVISOR" if english else "SUPERVISOR DE RECUPERACION"
        lines = [title] + [f"  {k}={v}" for k, v in rows]
        for svc in s["services"][:8]:
            lines.append(f"  {svc['name']}={svc['circuit']}/"
                         f"{'ready' if svc['health_probe_passed'] else 'unproven'}"
                         f" gen={svc['generation']}")
        note = ("effectful operations are never restarted automatically" if english
                else "las operaciones con efecto nunca se reinician automaticamente")
        lines.append(f"  ({note})")
        return "\n".join(lines)


# ── Process-global singleton ─────────────────────────────────────────────────
_supervisor: RecoverySupervisor | None = None


def get_recovery_supervisor() -> RecoverySupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = RecoverySupervisor()
    return _supervisor


def reset_recovery_supervisor(instance: "RecoverySupervisor | None" = None) -> None:
    global _supervisor
    _supervisor = instance
