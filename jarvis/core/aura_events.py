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
    """A dangerous tool is awaiting HITL/NATO authorization."""
    type: ClassVar[str] = "tool_auth_pending"
    tool: str = ""
    risk: str = "HIGH"
    preview: str = ""


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


# Stable registry of the event types the HUD understands.
EVENT_TYPES: tuple[str, ...] = (
    ModelDecisionEvent.type,
    VerifierStatusEvent.type,
    MemoryDecisionEvent.type,
    ToolAuthPendingEvent.type,
    BackgroundTaskEvent.type,
    ModeEvent.type,
)
