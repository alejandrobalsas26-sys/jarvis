"""
tests/test_push_alert_mode_gating.py — V62.0 Phase 8: proactive notification
suppression by AssistantMode.

core.telegram_bridge.push_alert previously pushed every alert unconditionally
regardless of any operating posture. This directly implements the original
spec's explicit test requirement: "proactive notification suppression during
FOCUS mode" — using the real, already-tested core.ironman_mode
.allowed_proactive_actions() policy table, not a new one invented here.
"""
from __future__ import annotations

import asyncio

import core.telegram_bridge as tb
from core.assistant_state import AssistantState
from core.ironman_mode import AssistantMode


def _capture_pushes(monkeypatch):
    calls = []

    async def _fake_push(text, **kwargs):
        calls.append(text)

    monkeypatch.setattr(tb, "_push", _fake_push)
    return calls


def test_no_state_wired_fails_open(monkeypatch):
    """state=None (a caller that never went through start_telegram_bridge)
    must not suppress anything — purely additive when unconfigured."""
    monkeypatch.setattr(tb, "_state", None)
    calls = _capture_pushes(monkeypatch)

    asyncio.run(tb.push_alert("TEST", "hello", "INFO"))

    assert calls


def test_passive_mode_suppresses_even_critical_alerts(monkeypatch):
    monkeypatch.setattr(tb, "_state", AssistantState(mode=AssistantMode.PASSIVE))
    calls = _capture_pushes(monkeypatch)

    asyncio.run(tb.push_alert("BREACH", "critical event", "CRITICAL"))

    assert not calls, "PASSIVE suppresses all proactive actions, including notify_urgent"


def test_focus_mode_suppresses_routine_but_not_critical(monkeypatch):
    monkeypatch.setattr(tb, "_state", AssistantState(mode=AssistantMode.FOCUS))
    calls = _capture_pushes(monkeypatch)

    asyncio.run(tb.push_alert("HUNT COMPLETE", "routine finding", "HIGH"))
    assert not calls, "FOCUS suppresses routine ('notify') alerts"

    asyncio.run(tb.push_alert("BREACH", "critical event", "CRITICAL"))
    assert calls == ["🔴 *BREACH*\ncritical event"], "FOCUS still allows notify_urgent (CRITICAL)"


def test_presentation_mode_suppresses_routine_but_not_critical(monkeypatch):
    monkeypatch.setattr(tb, "_state", AssistantState(mode=AssistantMode.PRESENTATION))
    calls = _capture_pushes(monkeypatch)

    asyncio.run(tb.push_alert("HUNT COMPLETE", "routine finding", "MEDIUM"))
    assert not calls

    asyncio.run(tb.push_alert("BREACH", "critical", "CRITICAL"))
    assert len(calls) == 1


def test_active_mode_allows_both_tiers(monkeypatch):
    monkeypatch.setattr(tb, "_state", AssistantState(mode=AssistantMode.ACTIVE))
    calls = _capture_pushes(monkeypatch)

    asyncio.run(tb.push_alert("HUNT COMPLETE", "routine finding", "HIGH"))
    asyncio.run(tb.push_alert("BREACH", "critical", "CRITICAL"))

    assert len(calls) == 2


def test_war_room_mode_allows_both_tiers(monkeypatch):
    monkeypatch.setattr(tb, "_state", AssistantState(mode=AssistantMode.WAR_ROOM))
    calls = _capture_pushes(monkeypatch)

    asyncio.run(tb.push_alert("HUNT COMPLETE", "routine finding", "HIGH"))
    asyncio.run(tb.push_alert("BREACH", "critical", "CRITICAL"))

    assert len(calls) == 2
