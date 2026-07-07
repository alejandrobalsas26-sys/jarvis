"""
core/aura_events.py — Typed AURA/HUD event contract (V61, Phase 9).

A small, dependency-free set of dataclasses describing the events the live brain
emits for the AURA heads-up display. This is a *contract*, not a UI: every event
serializes to a flat ``dict`` (and JSON) with a stable ``type`` discriminator and
a UTC ``timestamp``, so the HUD can render:

    current model role · active model · verifier status · memory decision ·
    pending tool authorization · background task status · assistant mode

The live LLM path already broadcasts dicts with these exact ``type`` strings;
these classes document and validate that shape and give callers a typed builder.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import ClassVar


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuraEvent:
    """Base HUD event. Subclasses set ``type`` and declare their own fields."""
    type: ClassVar[str] = "aura_event"

    def to_dict(self) -> dict:
        """Flat, JSON-ready dict: ``type`` + ``timestamp`` + dataclass fields."""
        payload = {"type": self.type, "timestamp": _now_iso()}
        payload.update(asdict(self))
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


@dataclass
class ModelDecisionEvent(AuraEvent):
    """Which cognitive role / model the router picked for the current turn."""
    type: ClassVar[str] = "model_decision"
    role: str = "fast"
    model: str = ""
    provider: str = "ollama"
    complexity: float = 0.0
    requires_verification: bool = False
    reason: str = ""


@dataclass
class VerifierStatusEvent(AuraEvent):
    """Outcome of the post-stream verifier pass."""
    type: ClassVar[str] = "verifier_status"
    verified: bool = False
    confidence: float = 0.0
    needs_human_review: bool = False
    issues: list[str] = field(default_factory=list)


@dataclass
class MemoryDecisionEvent(AuraEvent):
    """Whether/where the turn touched persistent memory."""
    type: ClassVar[str] = "memory_decision"
    action: str = "skip"          # "read" | "write" | "skip"
    scope: str = "none"           # session | project | long_term | none


@dataclass
class ToolAuthPendingEvent(AuraEvent):
    """A dangerous tool is awaiting HITL/NATO authorization.

    ``risk`` (V62.0 Phase 7) is one of core.risk_classes.RiskClass's values
    (read_only/low_impact/reversible/high_impact/lab_only). ``rollback_hint``
    is populated for REVERSIBLE tools — see core.risk_classes.rollback_hint().
    """
    type: ClassVar[str] = "tool_auth_pending"
    tool: str = ""
    risk: str = "HIGH"
    preview: str = ""
    rollback_hint: str | None = None


@dataclass
class BackgroundTaskEvent(AuraEvent):
    """State change of a background task (see core.task_queue)."""
    type: ClassVar[str] = "background_task"
    task_id: str = ""
    task_type: str = ""
    state: str = "queued"


@dataclass
class ModeEvent(AuraEvent):
    """Current assistant posture (see core.ironman_mode.AssistantMode)."""
    type: ClassVar[str] = "assistant_mode"
    mode: str = "passive"


@dataclass
class AssistantResponseEvent(AuraEvent):
    """The assistant's final natural-language answer for the turn — the HUD's
    conversational-content leg (V62.0 Phase 5). Previously the HUD only ever
    received routing/verifier/memory *metadata* about a turn, never the
    answer text itself. ``verified`` mirrors whether the post-stream verifier
    (see VerifierStatusEvent) left the draft unchanged — True when
    verification passed or didn't run (trivial/low-risk turn)."""
    type: ClassVar[str] = "assistant_response"
    text: str = ""
    verified: bool = True
    model_role: str = "fast"


# ── V66 M26: operational intelligence event contract ─────────────────────────
# Bounded, redacted projections of the operational-state spine (M20–M25). These
# document the exact shapes the ops-views layer (core.ops_views) broadcasts; that
# layer is responsible for redaction and payload bounding — these classes are the
# typed contract the HUD renders.
@dataclass
class AssetGraphUpdatedEvent(AuraEvent):
    """Asset-graph rollup counts (M20)."""
    type: ClassVar[str] = "asset_graph_updated"
    known: int = 0
    healthy: int = 0
    degraded: int = 0
    unknown: int = 0
    conflicts: int = 0
    changed_assets: list = field(default_factory=list)


@dataclass
class AssetConflictEvent(AuraEvent):
    """A surfaced (never-silenced) asset attribute conflict (M20)."""
    type: ClassVar[str] = "asset_conflict"
    asset_id: str = ""
    attribute: str = ""
    current_value: str = ""
    values: list = field(default_factory=list)


@dataclass
class ServiceHealthEvent(AuraEvent):
    """A service endpoint's observed exposure/health (M20)."""
    type: ClassVar[str] = "service_health"
    host: str = ""
    port: int = 0
    service: str = ""
    exposure: str = "unknown"
    reachable: bool = False


@dataclass
class CorrelationFindingEvent(AuraEvent):
    """Bounded projection of an M21 CorrelationFinding."""
    type: ClassVar[str] = "correlation_finding"
    finding_id: str = ""
    rule: str = ""
    group_entity: str = ""
    severity: str = "unknown"
    confidence: float = 0.0
    techniques: list = field(default_factory=list)
    explanation: str = ""
    matched: int = 0


@dataclass
class IncidentCaseUpdatedEvent(AuraEvent):
    """Bounded projection of an M22 IncidentCase."""
    type: ClassVar[str] = "incident_case_updated"
    incident_id: str = ""
    title: str = ""
    status: str = "new"
    severity: str = "medium"
    open_questions: int = 0
    hypotheses: int = 0
    findings: int = 0
    proposals: int = 0


@dataclass
class DriftFindingEvent(AuraEvent):
    """Bounded projection of an M23 DriftFinding."""
    type: ClassVar[str] = "drift_finding"
    asset: str = ""
    drift_type: str = ""
    severity: str = "low"
    recommended_investigation: str = ""
    confidence: float = 0.0
    verification_required: bool = True


@dataclass
class SituationSnapshotEvent(AuraEvent):
    """Bounded projection of an M25 SituationSnapshot."""
    type: ClassVar[str] = "situation_snapshot"
    severity: str = "calm"
    summary: dict = field(default_factory=dict)
    top_priority: dict | None = None
    recommended_next_step: str = "monitor"


@dataclass
class RunbookPlanEvent(AuraEvent):
    """A guarded runbook dry-run plan (M24) — what WOULD run, never an effect."""
    type: ClassVar[str] = "runbook_plan"
    runbook: str = ""
    steps: int = 0
    requires_hitl_steps: list = field(default_factory=list)
    scope_targets: list = field(default_factory=list)


@dataclass
class RunbookExecutionEvent(AuraEvent):
    """A guarded runbook execution outcome (M24)."""
    type: ClassVar[str] = "runbook_execution"
    runbook: str = ""
    status: str = "completed"
    steps_completed: int = 0
    steps_failed: int = 0
    steps_blocked: int = 0


@dataclass
class VerificationOutcomeEvent(AuraEvent):
    """A post-action verification outcome (M22/M24)."""
    type: ClassVar[str] = "verification_outcome"
    subject: str = ""
    verified: bool = False
    confidence: float = 0.0
    note: str = ""


# Stable registry of the event types the HUD understands.
EVENT_TYPES: tuple[str, ...] = (
    ModelDecisionEvent.type,
    VerifierStatusEvent.type,
    MemoryDecisionEvent.type,
    ToolAuthPendingEvent.type,
    BackgroundTaskEvent.type,
    ModeEvent.type,
    AssistantResponseEvent.type,
    # V66 M26 operational views
    AssetGraphUpdatedEvent.type,
    AssetConflictEvent.type,
    ServiceHealthEvent.type,
    CorrelationFindingEvent.type,
    IncidentCaseUpdatedEvent.type,
    DriftFindingEvent.type,
    SituationSnapshotEvent.type,
    RunbookPlanEvent.type,
    RunbookExecutionEvent.type,
    VerificationOutcomeEvent.type,
)
