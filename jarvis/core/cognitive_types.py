"""
core/cognitive_types.py — V58.0 COGNITIVE CORE typed task model.

Dependency-light stdlib dataclasses describing the planner → executor → critic
pipeline. Kept free of pydantic on purpose: these objects are created/mutated on
the hot agentic path and must stay cheap on the CPU-bound target host.

All timestamps are timezone-aware UTC ISO-8601 strings. Every container exposes
``to_dict()`` for JSONL persistence (see task_memory) and audit broadcasts.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> str:
    """Timezone-aware UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CompletionStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    BLOCKED = "blocked"


# Ordering helper so the engine/critic can compare risk severities.
_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def risk_rank(level: RiskLevel) -> int:
    return _RISK_ORDER.get(level, 0)


@dataclass
class ToolDecision:
    """A bounded decision about whether/how to invoke a single tool."""
    tool: str | None = None
    tool_input: dict = field(default_factory=dict)
    reasoning: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    allowed: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        return d


@dataclass
class PlanStep:
    """One deterministic step in a cognitive plan."""
    index: int
    action: str
    tool: str | None = None
    tool_input: dict = field(default_factory=dict)
    rationale: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    status: CompletionStatus = CompletionStatus.PENDING

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        d["status"] = self.status.value
        return d


@dataclass
class ExecutionTrace:
    """Append-only record of a single executed step — the audit unit."""
    step_index: int
    tool: str | None
    tool_input: dict = field(default_factory=dict)
    observation: dict | str | None = None
    error: str | None = None
    duration_ms: float = 0.0
    started_at: str = field(default_factory=_utcnow)
    status: CompletionStatus = CompletionStatus.PENDING

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class ReflectionResult:
    """Critic-independent self-evaluation produced by the engine after a batch."""
    success: bool = False
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    should_retry: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContextPacket:
    """Compressed, redacted bundle handed to an LLM before a call."""
    objective: str = ""
    facts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    recent_observations: list[str] = field(default_factory=list)
    redacted: bool = False
    char_count: int = 0
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CognitiveTask:
    """High-level task definition before planning."""
    objective: str
    task_id: str = field(default_factory=lambda: _new_id("task"))
    constraints: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    required_tools: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        return d


@dataclass
class CognitivePlan:
    """Mutable plan carried through the planner → executor → critic loop."""
    objective: str
    task_id: str = field(default_factory=lambda: _new_id("task"))
    constraints: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    required_tools: list[str] = field(default_factory=list)
    plan_steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    observations: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    confidence: float = 0.0
    completion_status: CompletionStatus = CompletionStatus.PENDING
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "risk_level": self.risk_level.value,
            "required_tools": list(self.required_tools),
            "plan_steps": [s.to_dict() for s in self.plan_steps],
            "current_step": self.current_step,
            "observations": list(self.observations),
            "errors": list(self.errors),
            "confidence": self.confidence,
            "completion_status": self.completion_status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
