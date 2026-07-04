"""
tests/test_ironman_foundation_v61.py — V61 Iron Man Mode foundation tests.

Pure, dependency-free coverage for the consent-gated mode policy, the
broker-free background task queue, and the typed AURA event contract.
"""
from __future__ import annotations

import json

import pytest

from core.ironman_mode import (
    AssistantMode,
    SessionConsent,
    allowed_proactive_actions,
    default_consent,
    parse_mode,
    should_listen_continuously,
    should_run_background_tasks,
    should_use_screen_context,
)
from core.task_queue import (
    BackgroundTaskQueue,
    SAFE_TASK_TYPES,
    Task,
    TaskRejected,
    TaskState,
    is_safe_task_type,
)
from core import aura_events as ae

# Mirror of the executor's HITL-exempt set boundary — dangerous tools must NOT
# be exempt even in WAR_ROOM (see test_warroom_dangerous_tools_still_hitl).
from tools.executor import _HITL_EXEMPT_TOOLS


# ── Phase 7 — Iron Man Mode policy ───────────────────────────────────────────
class TestIronManMode:
    def test_passive_blocks_screen(self):
        # Even with an explicit screen request, no consent → no screen use.
        assert should_use_screen_context("analyze this screen", default_consent()) is False
        assert allowed_proactive_actions(AssistantMode.PASSIVE, SessionConsent(screen=True)) == []

    def test_active_allows_screen_only_with_consent(self):
        consent = SessionConsent(screen=True)
        assert should_use_screen_context("read the screen please", consent) is True
        # Without screen consent, ACTIVE still cannot use the screen.
        assert should_use_screen_context("read the screen please", default_consent()) is False
        assert "screen_suggestions" in allowed_proactive_actions(AssistantMode.ACTIVE, consent)
        assert "screen_suggestions" not in allowed_proactive_actions(
            AssistantMode.ACTIVE, default_consent()
        )

    def test_screen_requires_explicit_intent(self):
        consent = SessionConsent(screen=True)
        # Consent granted but no screen intent in the message → still False.
        assert should_use_screen_context("tell me a joke", consent) is False

    def test_focus_minimizes_interruptions(self):
        assert allowed_proactive_actions(AssistantMode.FOCUS, SessionConsent(screen=True)) == ["notify_urgent"]
        assert should_run_background_tasks(AssistantMode.FOCUS, "plugged", 5, 5) is False

    def test_presentation_no_sensitive_reading(self):
        actions = allowed_proactive_actions(AssistantMode.PRESENTATION, SessionConsent(screen=True))
        assert "screen_suggestions" not in actions
        assert actions == ["notify_urgent"]

    def test_warroom_soc_workflows(self):
        actions = allowed_proactive_actions(AssistantMode.WAR_ROOM, default_consent())
        assert "soc_workflow_suggestions" in actions
        assert "threat_hunt_suggestions" in actions

    def test_warroom_dangerous_tools_still_hitl(self):
        # WAR_ROOM never exempts dangerous tools from HITL/NATO — they are not in
        # the executor's exempt set, and proactive actions never include tool exec.
        for dangerous in ("run_shell_command", "code_execute", "http_request",
                          "write_file", "kill_process", "network_scan"):
            assert dangerous not in _HITL_EXEMPT_TOOLS
        proactive = allowed_proactive_actions(AssistantMode.WAR_ROOM, SessionConsent(shell=True))
        assert not any(t in proactive for t in
                       ("run_shell_command", "code_execute", "network_scan"))

    def test_listen_continuously(self):
        assert should_listen_continuously(AssistantMode.ACTIVE) is True
        assert should_listen_continuously(AssistantMode.WAR_ROOM) is True
        assert should_listen_continuously(AssistantMode.PASSIVE) is False

    def test_background_tasks_hardware_aware(self):
        assert should_run_background_tasks(AssistantMode.ACTIVE, "plugged", 10, 10) is True
        assert should_run_background_tasks(AssistantMode.ACTIVE, "battery", 10, 10) is False
        assert should_run_background_tasks(AssistantMode.ACTIVE, "plugged", 99, 10) is False
        assert should_run_background_tasks(AssistantMode.ACTIVE, "plugged", 10, 99) is False
        assert should_run_background_tasks(AssistantMode.PASSIVE, "plugged", 10, 10) is False

    def test_parse_mode_defaults_safe(self):
        assert parse_mode("war_room") is AssistantMode.WAR_ROOM
        assert parse_mode(AssistantMode.FOCUS) is AssistantMode.FOCUS
        assert parse_mode("nonsense") is AssistantMode.PASSIVE
        assert parse_mode(None) is AssistantMode.PASSIVE


# ── Phase 8 — Background task queue ───────────────────────────────────────────
class TestTaskQueue:
    def test_enqueue_safe_task(self):
        q = BackgroundTaskQueue()
        t = q.enqueue("summarize_document", {"path": "report.pdf"})
        assert isinstance(t, Task)
        assert t.state is TaskState.QUEUED
        assert t.dangerous is False
        assert q.get(t.id) is t
        assert t in q.pending()

    def test_all_safe_types_admitted(self):
        q = BackgroundTaskQueue()
        for tt in SAFE_TASK_TYPES:
            assert is_safe_task_type(tt)
            assert q.enqueue(tt).state is TaskState.QUEUED

    def test_reject_dangerous_without_approval(self):
        q = BackgroundTaskQueue()
        with pytest.raises(TaskRejected):
            q.enqueue("run_shell")
        with pytest.raises(TaskRejected):
            q.enqueue("exfiltrate_data")

    def test_dangerous_with_approval_is_flagged(self):
        q = BackgroundTaskQueue()
        t = q.enqueue("delete_everything", approved=True)
        assert t.dangerous is True
        assert t.approved is True

    def test_empty_type_rejected(self):
        q = BackgroundTaskQueue()
        with pytest.raises(TaskRejected):
            q.enqueue("")

    def test_cancel_task(self):
        q = BackgroundTaskQueue()
        t = q.enqueue("run_tests")
        assert q.cancel(t.id) is True
        assert q.get(t.id).state is TaskState.CANCELLED
        # Cannot cancel an already-terminal task.
        assert q.cancel(t.id) is False

    def test_state_transitions(self):
        q = BackgroundTaskQueue()
        t = q.enqueue("analyze_repo")
        assert q.mark_running(t.id) is True
        assert q.get(t.id).state is TaskState.RUNNING
        assert q.mark_running(t.id) is False  # already running
        assert q.mark_completed(t.id, {"ok": True}) is True
        assert q.get(t.id).state is TaskState.COMPLETED
        assert q.get(t.id).result == {"ok": True}
        # Cannot fail a completed task.
        assert q.mark_failed(t.id, "x") is False

    def test_fifo_next_queued(self):
        q = BackgroundTaskQueue()
        a = q.enqueue("run_tests")
        q.enqueue("analyze_repo")
        assert q.next_queued().id == a.id
        q.mark_running(a.id)
        assert q.next_queued().type == "analyze_repo"

    def test_task_to_dict_serializes(self):
        q = BackgroundTaskQueue()
        t = q.enqueue("generate_report")
        d = t.to_dict()
        assert d["type"] == "generate_report"
        assert d["state"] == "queued"
        json.dumps(d)  # must be JSON-serializable


# ── Phase 9 — AURA event contract ─────────────────────────────────────────────
class TestAuraEvents:
    def test_model_decision_event(self):
        e = ae.ModelDecisionEvent(role="coder", model="qwen2.5-coder:14b",
                                  requires_verification=True, complexity=0.7)
        d = e.to_dict()
        assert d["type"] == "model_decision"
        assert "timestamp" in d
        assert d["role"] == "coder"
        assert d["requires_verification"] is True

    def test_verifier_status_event(self):
        e = ae.VerifierStatusEvent(verified=False, confidence=0.3, issues=["x"])
        d = json.loads(e.to_json())
        assert d["type"] == "verifier_status"
        assert d["issues"] == ["x"]

    def test_memory_decision_event(self):
        e = ae.MemoryDecisionEvent(action="write", scope="project")
        assert e.to_dict()["scope"] == "project"

    def test_tool_auth_pending_event(self):
        e = ae.ToolAuthPendingEvent(tool="run_shell_command", risk="HIGH")
        assert e.to_dict()["tool"] == "run_shell_command"

    def test_background_task_event(self):
        e = ae.BackgroundTaskEvent(task_id="task_1", task_type="run_tests", state="running")
        assert e.to_dict()["state"] == "running"

    def test_mode_event(self):
        e = ae.ModeEvent(mode="war_room")
        assert e.to_dict()["mode"] == "war_room"

    def test_assistant_response_event(self):
        e = ae.AssistantResponseEvent(text="Hello.", verified=False, model_role="deep")
        d = e.to_dict()
        assert d["type"] == "assistant_response"
        assert d["text"] == "Hello."
        assert d["verified"] is False
        assert d["model_role"] == "deep"

    def test_all_events_json_serializable(self):
        events = [
            ae.ModelDecisionEvent(), ae.VerifierStatusEvent(), ae.MemoryDecisionEvent(),
            ae.ToolAuthPendingEvent(), ae.BackgroundTaskEvent(), ae.ModeEvent(),
            ae.AssistantResponseEvent(),
        ]
        for e in events:
            payload = json.loads(e.to_json())
            assert payload["type"] in ae.EVENT_TYPES
            assert "timestamp" in payload

    def test_event_types_registry_complete(self):
        assert set(ae.EVENT_TYPES) == {
            "model_decision", "verifier_status", "memory_decision",
            "tool_auth_pending", "background_task", "assistant_mode",
            "assistant_response",
        }
