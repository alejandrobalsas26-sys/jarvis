"""
core/task_queue.py — Local background task scheduler abstraction (V61, Phase 8).

A lightweight, broker-free, in-memory task queue so JARVIS can line up useful
work ("create things while I do other stuff") without any hidden execution.

Hard rules:
  * ALLOWLIST, not denylist — only known-safe task types are accepted by
    default. Anything else is *dangerous* and is rejected unless the caller
    passes ``approved=True`` (an explicit operator decision).
  * This module is a *scheduler/state-machine only*. It NEVER runs shell, code,
    or any side-effecting work itself — execution is the responsibility of a
    separate, individually-guarded runner. Enqueueing is not authorization to
    bypass HITL/NATO.
  * Cancellation is always supported for queued/running tasks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Known-safe, side-effect-bounded task types (the allowlist).
SAFE_TASK_TYPES: frozenset[str] = frozenset({
    "summarize_document",
    "generate_report",
    "run_tests",
    "analyze_repo",
    "index_documents",
    "monitor_system",
})

_TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRejected(Exception):
    """Raised when a task is refused (dangerous + unapproved, or malformed)."""


@dataclass
class Task:
    """A single scheduled unit of work and its lifecycle metadata."""
    id: str
    type: str
    payload: dict = field(default_factory=dict)
    state: TaskState = TaskState.QUEUED
    approved: bool = False
    dangerous: bool = False
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: object | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "state": self.state.value,
            "approved": self.approved,
            "dangerous": self.dangerous,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


def is_safe_task_type(task_type: str) -> bool:
    return task_type in SAFE_TASK_TYPES


class BackgroundTaskQueue:
    """In-memory, broker-free task queue with an allowlist admission policy."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []

    # ── Admission ────────────────────────────────────────────────────────────
    def enqueue(
        self,
        task_type: str,
        payload: dict | None = None,
        *,
        approved: bool = False,
    ) -> Task:
        """Admit a task. Safe types are always accepted; any non-safe type is
        treated as dangerous and REJECTED unless ``approved=True``.
        """
        if not task_type or not isinstance(task_type, str):
            raise TaskRejected("task_type must be a non-empty string")

        dangerous = not is_safe_task_type(task_type)
        if dangerous and not approved:
            raise TaskRejected(
                f"dangerous task type '{task_type}' requires explicit approval"
            )

        task = Task(
            id=f"task_{uuid.uuid4().hex[:12]}",
            type=task_type,
            payload=dict(payload or {}),
            approved=bool(approved),
            dangerous=dangerous,
        )
        self._tasks[task.id] = task
        self._order.append(task.id)
        return task

    # ── Lifecycle transitions ─────────────────────────────────────────────────
    def mark_running(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.state != TaskState.QUEUED:
            return False
        task.state = TaskState.RUNNING
        task.started_at = _now()
        return True

    def mark_completed(self, task_id: str, result: object | None = None) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.state not in (TaskState.QUEUED, TaskState.RUNNING):
            return False
        task.state = TaskState.COMPLETED
        task.result = result
        task.finished_at = _now()
        return True

    def mark_failed(self, task_id: str, error: str = "") -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.state not in (TaskState.QUEUED, TaskState.RUNNING):
            return False
        task.state = TaskState.FAILED
        task.error = str(error)
        task.finished_at = _now()
        return True

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued or running task. Returns False if already terminal."""
        task = self._tasks.get(task_id)
        if task is None or task.state in _TERMINAL_STATES:
            return False
        task.state = TaskState.CANCELLED
        task.finished_at = _now()
        return True

    # ── Queries ───────────────────────────────────────────────────────────────
    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        return [self._tasks[tid] for tid in self._order if tid in self._tasks]

    def pending(self) -> list[Task]:
        return [t for t in self.list_tasks() if t.state == TaskState.QUEUED]

    def next_queued(self) -> Task | None:
        """Peek the oldest QUEUED task (FIFO) without changing its state."""
        for t in self.list_tasks():
            if t.state == TaskState.QUEUED:
                return t
        return None

    def __len__(self) -> int:
        return len(self._tasks)
