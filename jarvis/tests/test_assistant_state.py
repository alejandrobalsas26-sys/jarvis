"""
tests/test_assistant_state.py — V62.0 Phase 8: live AssistantMode holder.

core.ironman_mode.AssistantMode and its policy predicates
(allowed_proactive_actions, should_run_background_tasks) had zero production
callers — no live "current mode" existed anywhere. AssistantState is that
missing piece.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.assistant_state import AssistantState, default_state
from core.ironman_mode import AssistantMode


def test_default_state_starts_active():
    state = default_state()
    assert state.mode is AssistantMode.ACTIVE
    assert isinstance(state.updated_at, datetime)
    assert state.updated_at.tzinfo is timezone.utc


def test_set_mode_changes_and_reports_true():
    state = AssistantState()
    before = state.updated_at
    changed = state.set_mode(AssistantMode.FOCUS)
    assert changed is True
    assert state.mode is AssistantMode.FOCUS
    assert state.updated_at >= before


def test_set_mode_to_same_mode_is_a_noop():
    state = AssistantState(mode=AssistantMode.WAR_ROOM)
    changed = state.set_mode(AssistantMode.WAR_ROOM)
    assert changed is False
    assert state.mode is AssistantMode.WAR_ROOM


def test_each_instance_is_independent():
    a = default_state()
    b = default_state()
    a.set_mode(AssistantMode.PASSIVE)
    assert b.mode is AssistantMode.ACTIVE
