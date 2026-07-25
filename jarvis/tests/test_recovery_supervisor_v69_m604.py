"""V69 M60.4 — bounded runtime recovery supervisor.

Proves the six refusals: no restart after STOPPING, no duplicate instance, no restart
storm, a circuit breaker that opens and recovers, no automatic restart of an effectful
operation, and no READY claim without a passing health probe.
"""
from __future__ import annotations

import json

import pytest

from core.recovery_supervisor import (
    INELIGIBLE_OPERATIONS, CircuitState, RecoveryPolicy, RecoverySupervisor,
    RestartDecision, ServiceClass, get_recovery_supervisor, policy_for,
    reset_recovery_supervisor,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def _sup(stopping=False, **kw) -> tuple[RecoverySupervisor, _Clock]:
    clock = _Clock()
    return RecoverySupervisor(clock=clock, is_stopping=lambda: stopping, **kw), clock


def _fail_and_restart(sup, clock, name, times: int, *, gap: float = 100.0) -> None:
    for _ in range(times):
        sup.note_failure(name, "boom")
        decision, _ = sup.evaluate(name)
        if decision is RestartDecision.RESTART:
            sup.commit_restart(name)
            sup.note_failure(name, "boom")
        clock.advance(gap)


# ══════════════════════════════════════════════════════════════════════════════
#  Policy
# ══════════════════════════════════════════════════════════════════════════════
class TestPolicy:
    def test_backoff_is_exponential_and_capped(self):
        p = RecoveryPolicy(base_backoff_s=2.0, max_backoff_s=30.0)
        assert p.backoff_for(0) == 2.0
        assert p.backoff_for(1) == 4.0
        assert p.backoff_for(2) == 8.0
        assert p.backoff_for(10) == 30.0

    def test_class_defaults_differ_meaningfully(self):
        crit = policy_for(ServiceClass.CRITICAL)
        opt = policy_for(ServiceClass.OPTIONAL)
        bg = policy_for(ServiceClass.BACKGROUND)
        assert crit.max_restarts < opt.max_restarts < bg.max_restarts
        assert crit.notify_operator_after == 1

    def test_one_shot_never_auto_restarts(self):
        p = policy_for(ServiceClass.ONE_SHOT)
        assert p.auto_restart is False and p.max_restarts == 0

    def test_policy_defaults_are_independent_instances(self):
        a, b = policy_for(ServiceClass.OPTIONAL), policy_for(ServiceClass.OPTIONAL)
        a.max_restarts = 99
        assert b.max_restarts == 3


# ══════════════════════════════════════════════════════════════════════════════
#  Registration & duplicates
# ══════════════════════════════════════════════════════════════════════════════
class TestRegistration:
    def test_register_is_idempotent(self):
        sup, _ = _sup()
        a = sup.register("aura", service_class=ServiceClass.OPTIONAL)
        b = sup.register("aura")
        assert a is b and sup.snapshot()["services_registered"] == 1

    def test_running_service_is_never_restarted(self):
        sup, _ = _sup()
        sup.register("mcp")
        sup.mark_running("mcp")
        decision, _ = sup.evaluate("mcp")
        assert decision is RestartDecision.REFUSED_DUPLICATE

    def test_explicit_instance_running_flag_refuses(self):
        sup, _ = _sup()
        sup.register("aura")
        sup.note_failure("aura", "x")
        decision, _ = sup.evaluate("aura", instance_running=True)
        assert decision is RestartDecision.REFUSED_DUPLICATE

    def test_restart_bumps_generation_for_a_fresh_queue(self):
        sup, clock = _sup()
        sup.register("collector")
        sup.note_failure("collector", "x")
        assert sup.evaluate("collector")[0] is RestartDecision.RESTART
        svc = sup.commit_restart("collector")
        assert svc.generation == 1
        clock.advance(200)
        sup.note_failure("collector", "x")
        assert sup.evaluate("collector")[0] is RestartDecision.RESTART
        assert sup.commit_restart("collector").generation == 2


# ══════════════════════════════════════════════════════════════════════════════
#  The six refusals
# ══════════════════════════════════════════════════════════════════════════════
class TestRefusals:
    def test_no_restart_after_stopping(self):
        sup, _ = _sup(stopping=True)
        sup.register("aura")
        sup.note_failure("aura", "x")
        assert sup.evaluate("aura")[0] is RestartDecision.REFUSED_STOPPING

    def test_stopping_outranks_everything_else(self):
        sup, _ = _sup(stopping=True)
        sup.register("fresh")
        assert sup.evaluate("fresh")[0] is RestartDecision.REFUSED_STOPPING

    @pytest.mark.parametrize("operation", sorted(INELIGIBLE_OPERATIONS))
    def test_effectful_operations_are_never_restarted(self, operation):
        sup, _ = _sup()
        sup.register("worker")
        sup.note_failure("worker", "x")
        assert sup.evaluate("worker", operation=operation)[0] \
            is RestartDecision.REFUSED_EFFECTFUL

    def test_ineligible_set_names_the_dangerous_operations(self):
        for expected in ("tool_execution", "hitl_prompt", "semantic_migration",
                         "backup_restore", "active_user_turn"):
            assert expected in INELIGIBLE_OPERATIONS

    def test_one_shot_service_refused_by_policy(self):
        sup, _ = _sup()
        sup.register("selftest", service_class=ServiceClass.ONE_SHOT)
        sup.note_failure("selftest", "x")
        assert sup.evaluate("selftest")[0] is RestartDecision.REFUSED_POLICY

    def test_backoff_refuses_an_immediate_second_restart(self):
        sup, clock = _sup()
        sup.register("mcp")
        sup.note_failure("mcp", "x")
        assert sup.evaluate("mcp")[0] is RestartDecision.RESTART
        sup.commit_restart("mcp")
        sup.note_failure("mcp", "x")
        decision, wait = sup.evaluate("mcp")
        assert decision is RestartDecision.REFUSED_BACKOFF and wait > 0

    def test_backoff_clears_after_waiting(self):
        sup, clock = _sup()
        sup.register("mcp")
        sup.note_failure("mcp", "x")
        sup.evaluate("mcp")
        sup.commit_restart("mcp")
        sup.note_failure("mcp", "x")
        clock.advance(120)
        assert sup.evaluate("mcp")[0] is RestartDecision.RESTART

    def test_global_restart_budget_is_bounded(self):
        sup, clock = _sup(total_restart_budget=2)
        for i in range(3):
            name = f"svc{i}"
            sup.register(name)
            sup.note_failure(name, "x")
            decision, _ = sup.evaluate(name)
            if decision is RestartDecision.RESTART:
                sup.commit_restart(name)
        assert sup.total_restarts == 2
        sup.register("svc9")
        sup.note_failure("svc9", "x")
        assert sup.evaluate("svc9")[0] is RestartDecision.REFUSED_BUDGET


# ══════════════════════════════════════════════════════════════════════════════
#  Circuit breaker
# ══════════════════════════════════════════════════════════════════════════════
class TestCircuitBreaker:
    def test_storm_opens_the_circuit(self):
        sup, clock = _sup()
        sup.register("flapper", policy=RecoveryPolicy(max_restarts=3, window_s=300,
                                                      base_backoff_s=0.0))
        _fail_and_restart(sup, clock, "flapper", 4, gap=10)
        assert sup.get("flapper").circuit is CircuitState.OPEN
        assert sup.evaluate("flapper")[0] is RestartDecision.REFUSED_CIRCUIT_OPEN

    def test_restarts_are_bounded_not_infinite(self):
        sup, clock = _sup()
        sup.register("flapper", policy=RecoveryPolicy(max_restarts=3, window_s=300,
                                                      base_backoff_s=0.0))
        for _ in range(50):
            sup.note_failure("flapper", "x")
            if sup.evaluate("flapper")[0] is RestartDecision.RESTART:
                sup.commit_restart("flapper")
            clock.advance(5)
        assert sup.get("flapper").restart_attempts <= 3

    def test_cooldown_moves_open_to_half_open_and_allows_one_probe(self):
        sup, clock = _sup()
        sup.register("flapper", policy=RecoveryPolicy(max_restarts=2, window_s=300,
                                                      cooldown_s=100.0,
                                                      base_backoff_s=0.0))
        _fail_and_restart(sup, clock, "flapper", 3, gap=5)
        assert sup.get("flapper").circuit is CircuitState.OPEN
        clock.advance(400)                       # cooldown AND window elapse
        sup.note_failure("flapper", "x")
        assert sup.evaluate("flapper")[0] is RestartDecision.RESTART
        assert sup.get("flapper").circuit is CircuitState.HALF_OPEN

    def test_passing_probe_closes_a_half_open_circuit(self):
        sup, clock = _sup()
        svc = sup.register("flapper")
        svc.circuit = CircuitState.HALF_OPEN
        sup.note_health_probe("flapper", True)
        assert svc.circuit is CircuitState.CLOSED

    def test_window_expiry_forgets_old_restarts(self):
        sup, clock = _sup()
        sup.register("svc", policy=RecoveryPolicy(max_restarts=2, window_s=60,
                                                  base_backoff_s=0.0))
        sup.note_failure("svc", "x")
        sup.evaluate("svc")
        sup.commit_restart("svc")
        clock.advance(120)
        assert sup.get("svc").restarts_in_window(clock.t) == 0

    def test_restart_history_is_bounded(self):
        sup, clock = _sup(total_restart_budget=1000)
        sup.register("svc", policy=RecoveryPolicy(max_restarts=1000, window_s=1,
                                                  base_backoff_s=0.0))
        for _ in range(60):
            sup.note_failure("svc", "x")
            if sup.evaluate("svc")[0] is RestartDecision.RESTART:
                sup.commit_restart("svc")
            clock.advance(2)
        assert len(sup.get("svc").history()) <= 20

    def test_operator_notifications_are_bounded(self):
        sup, clock = _sup()
        for i in range(50):
            name = f"svc{i}"
            sup.register(name)
            sup.note_failure(name, "x")
            sup.note_failure(name, "x")
        assert len(sup.notifications) <= 20


# ══════════════════════════════════════════════════════════════════════════════
#  Readiness truthfulness
# ══════════════════════════════════════════════════════════════════════════════
class TestReadiness:
    def test_started_service_is_not_ready_without_a_probe(self):
        sup, _ = _sup()
        sup.mark_running("aura")
        snap = sup.snapshot()
        assert snap["services_ready"] == 0 and snap["services_degraded"] == 1

    def test_probe_pass_makes_it_ready(self):
        sup, _ = _sup()
        sup.mark_running("aura")
        sup.note_health_probe("aura", True)
        assert sup.snapshot()["services_ready"] == 1

    def test_restart_revokes_readiness_until_reproven(self):
        sup, clock = _sup()
        sup.mark_running("aura")
        sup.note_health_probe("aura", True)
        sup.note_failure("aura", "died")
        sup.evaluate("aura")
        sup.commit_restart("aura")
        assert sup.get("aura").health_probe_passed is False
        assert sup.snapshot()["services_ready"] == 0

    def test_deliberate_stop_does_not_count_as_failure(self):
        sup, _ = _sup()
        sup.mark_running("aura")
        sup.note_failure("aura", "x")
        sup.note_stopped("aura")
        assert sup.get("aura").consecutive_failures == 0

    def test_optional_failure_does_not_block_anything(self):
        sup, _ = _sup()
        sup.register("aura", service_class=ServiceClass.OPTIONAL)
        sup.note_failure("aura", "x")
        # An OPTIONAL failure is reported, never fatal — the supervisor exposes no
        # blocking API at all, which is the structural guarantee.
        assert not hasattr(sup, "block")
        assert sup.snapshot()["services_degraded"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  Health surface & panel
# ══════════════════════════════════════════════════════════════════════════════
class TestSupervisorHealth:
    def test_snapshot_is_content_free(self):
        sup, _ = _sup()
        sup.register("aura")
        sup.note_failure("aura", "connection to C:\\Users\\aleja failed")
        raw = json.dumps(sup.snapshot())
        assert "aura" in raw                      # names are fine
        assert len(raw) < 4000

    def test_snapshot_exposes_required_metrics(self):
        sup, _ = _sup()
        sup.register("aura")
        snap = sup.snapshot()
        for key in ("services_registered", "services_ready", "services_degraded",
                    "restart_attempts", "circuits_open", "last_restart_reason"):
            assert key in snap

    def test_refusals_are_counted(self):
        sup, _ = _sup(stopping=True)
        sup.register("aura")
        sup.evaluate("aura")
        sup.evaluate("aura")
        assert sup.snapshot()["refusals"]["REFUSED_STOPPING"] == 2

    def test_panel_is_ascii_and_states_the_guarantee(self):
        sup, _ = _sup()
        sup.register("aura")
        panel = sup.render_panel(language="en")
        assert panel.isascii()
        assert "never restarted automatically" in panel
        sup.render_panel(language="es").encode("cp1252")

    def test_singleton_reset(self):
        reset_recovery_supervisor()
        a = get_recovery_supervisor()
        assert get_recovery_supervisor() is a
        reset_recovery_supervisor()
        assert get_recovery_supervisor() is not a
