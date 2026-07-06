"""
tests/test_presence.py — V63 Milestone 7 Presence Engine coverage.

Proves:
  * FOCUS / PRESENTATION suppress routine notifications; CRITICAL escalates;
  * PASSIVE suppresses all proactive output;
  * repeated identical events deduplicate within cooldown; cooldown expiry works;
  * high CPU / RAM reduces background concurrency; battery reduces work to zero;
  * a work-needing event is deferred under resource pressure (but CRITICAL isn't);
  * an ACT proposal outside authorized scope is downgraded to ASK (no bypass);
  * an authorized ACT still requires gated execution.
"""
from __future__ import annotations

from core.authority import AuthorityMode, AuthorityState, ScopePolicy
from core.ironman_mode import AssistantMode, SessionConsent
from core.presence import (
    PresenceEngine,
    PresenceEvent,
    PresenceLevel,
    PresenceSignal,
    Urgency,
    mode_permits_notification,
    urgency_from_severity,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _future():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


# ── mode ceilings ─────────────────────────────────────────────────────────────
def test_focus_suppresses_routine_allows_critical():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.FOCUS)
    routine = eng.evaluate(PresenceEvent("hunt", Urgency.HIGH), sig)
    assert routine.deliver is False
    crit = eng.evaluate(PresenceEvent("breach", Urgency.CRITICAL), sig)
    assert crit.deliver is True


def test_passive_suppresses_everything():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.PASSIVE)
    d = eng.evaluate(PresenceEvent("breach", Urgency.CRITICAL), sig)
    assert d.deliver is False
    assert d.level == PresenceLevel.OBSERVE


def test_active_delivers_routine():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.ACTIVE)
    d = eng.evaluate(PresenceEvent("finding", Urgency.ROUTINE), sig)
    assert d.deliver is True


def test_mode_permits_helper_matches_policy():
    c = SessionConsent()
    assert mode_permits_notification(AssistantMode.ACTIVE, c, Urgency.ROUTINE) is True
    assert mode_permits_notification(AssistantMode.FOCUS, c, Urgency.ROUTINE) is False
    assert mode_permits_notification(AssistantMode.FOCUS, c, Urgency.CRITICAL) is True
    assert mode_permits_notification(AssistantMode.PASSIVE, c, Urgency.CRITICAL) is False


def test_urgency_from_severity():
    assert urgency_from_severity("CRITICAL") == Urgency.CRITICAL
    assert urgency_from_severity("HIGH") == Urgency.HIGH
    assert urgency_from_severity("info") == Urgency.ROUTINE
    assert urgency_from_severity("weird") == Urgency.ROUTINE


# ── dedup / cooldown ──────────────────────────────────────────────────────────
def test_repeated_alert_deduplicates_then_recovers_after_cooldown():
    clock = _Clock()
    eng = PresenceEngine(clock=clock)
    sig = PresenceSignal(mode=AssistantMode.ACTIVE)
    first = eng.evaluate(PresenceEvent("dup", Urgency.HIGH), sig)
    assert first.deliver is True
    second = eng.evaluate(PresenceEvent("dup", Urgency.HIGH), sig)
    assert second.deliver is False       # deduped within cooldown
    clock.advance(61.0)                  # HIGH cooldown is 60s
    third = eng.evaluate(PresenceEvent("dup", Urgency.HIGH), sig)
    assert third.deliver is True


def test_distinct_keys_do_not_dedup():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.ACTIVE)
    assert eng.evaluate(PresenceEvent("a", Urgency.HIGH), sig).deliver is True
    assert eng.evaluate(PresenceEvent("b", Urgency.HIGH), sig).deliver is True


# ── resource-aware background work ────────────────────────────────────────────
def test_high_cpu_reduces_background_concurrency():
    eng = PresenceEngine()
    calm = PresenceSignal(mode=AssistantMode.ACTIVE, cpu_pct=10.0, ram_pct=20.0)
    assert eng.max_background_concurrency(calm) == 2
    loaded = PresenceSignal(mode=AssistantMode.ACTIVE, cpu_pct=75.0, ram_pct=20.0)
    assert eng.max_background_concurrency(loaded) == 1
    pressure = PresenceSignal(mode=AssistantMode.ACTIVE, cpu_pct=99.0, ram_pct=20.0)
    assert eng.max_background_concurrency(pressure) == 0


def test_battery_reduces_work_to_zero():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.ACTIVE, cpu_pct=10.0, ram_pct=10.0, on_battery=True)
    assert eng.max_background_concurrency(sig) == 0


def test_quiet_modes_have_no_background_work():
    eng = PresenceEngine()
    for m in (AssistantMode.FOCUS, AssistantMode.PRESENTATION, AssistantMode.PASSIVE):
        sig = PresenceSignal(mode=m, cpu_pct=5.0, ram_pct=5.0)
        assert eng.max_background_concurrency(sig) == 0


def test_work_event_deferred_under_pressure_but_critical_escalates():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.ACTIVE, cpu_pct=99.0, ram_pct=20.0)
    work = eng.evaluate(PresenceEvent("scan", Urgency.HIGH, requires_work=True), sig)
    assert work.deliver is False         # background work deferred
    crit = eng.evaluate(PresenceEvent("scan2", Urgency.CRITICAL, requires_work=True), sig)
    assert crit.deliver is True          # critical escalates past pressure


# ── ACT never bypasses gates ──────────────────────────────────────────────────
def test_act_out_of_scope_downgrades_to_ask():
    eng = PresenceEngine()
    auth = AuthorityState(mode=AuthorityMode.CTF)
    auth.add_scope(ScopePolicy(scope_id="c", cidrs=("10.10.10.0/24",), expires_at=_future()))
    sig = PresenceSignal(mode=AssistantMode.WAR_ROOM, authority=auth)
    ev = PresenceEvent("scan-host", Urgency.HIGH, desired_level=PresenceLevel.ACT,
                       action_tool="network_scan", action_target="8.8.8.8")
    d = eng.evaluate(ev, sig)
    assert d.level == PresenceLevel.ASK   # ACT→ASK, not executed
    assert d.requires_gates is False


def test_act_in_scope_still_requires_gates():
    eng = PresenceEngine()
    auth = AuthorityState(mode=AuthorityMode.CTF)
    auth.add_scope(ScopePolicy(scope_id="c", cidrs=("10.10.10.0/24",), expires_at=_future()))
    sig = PresenceSignal(mode=AssistantMode.WAR_ROOM, authority=auth)
    ev = PresenceEvent("scan-host", Urgency.HIGH, desired_level=PresenceLevel.ACT,
                       action_tool="network_scan", action_target="10.10.10.5")
    d = eng.evaluate(ev, sig)
    assert d.level == PresenceLevel.ACT
    assert d.requires_gates is True       # execution still passes ToolExecutor/HITL


def test_act_downgraded_when_mode_is_lower_ceiling():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.FOCUS)   # ceiling = SUGGEST
    ev = PresenceEvent("x", Urgency.CRITICAL, desired_level=PresenceLevel.ACT)
    d = eng.evaluate(ev, sig)
    assert d.level == PresenceLevel.SUGGEST


# ── snapshot ──────────────────────────────────────────────────────────────────
def test_snapshot_reports_posture():
    eng = PresenceEngine()
    sig = PresenceSignal(mode=AssistantMode.ACTIVE, cpu_pct=10.0, ram_pct=10.0)
    snap = eng.snapshot(sig)
    assert snap["mode"] == "active"
    assert snap["permits_routine"] is True
    assert snap["max_background_concurrency"] == 2
