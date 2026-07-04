"""
core/project_context.py — V63 Milestone 8: lightweight project awareness.

Project facts — goals, decisions, tasks, blockers, open questions, artifacts —
are stored with provenance + timestamps via the M5 memory fabric at
``scope="project"`` and recalled on demand. This is *memory retrieval*, not a
giant static prompt: JARVIS answers "what are we building?", "what did we
decide?", "what's blocked?" from real, timestamped, provenance-carrying records
rather than a hardcoded blob.

Built entirely on top of `core.memory_fabric` (M5) — no new store, no migration.
The fabric is injectable (``fabric=`` param) so this is unit-testable without a
ChromaDB backend.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from core.memory_fabric import MemoryFabric, MemoryRecord, Sensitivity, get_fabric

_PROJECT_SCOPE = "project"
# Broad recall seed used when the caller has no specific question.
_RECALL_DEFAULT = "project goal decision task blocked open question artifact status"


class ProjectFactType(str, Enum):
    GOAL = "goal"
    DECISION = "decision"
    TASK = "task"
    BLOCKED = "blocked"
    QUESTION = "question"
    ARTIFACT = "artifact"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def record_project_fact(
    kind: ProjectFactType | str,
    text: str,
    *,
    source: str = "operator",
    fabric: MemoryFabric | None = None,
) -> bool:
    """Persist one project fact (provenance + timestamp) at scope=project.

    Returns False on invalid input or a fail-open backend error.
    """
    kind_v = kind.value if isinstance(kind, ProjectFactType) else str(kind).strip().lower()
    if kind_v not in {t.value for t in ProjectFactType} or not (text or "").strip():
        return False
    fab = fabric or get_fabric()
    content = f"[project:{kind_v}] {text.strip()} (recorded {_now_iso()})"
    return await fab.store(
        content,
        memory_type=f"project_{kind_v}",
        source=source,
        scope=_PROJECT_SCOPE,
        sensitivity=Sensitivity.NORMAL,
    )


async def recall_project_context(
    query: str = "",
    *,
    limit: int = 8,
    fabric: MemoryFabric | None = None,
) -> list[MemoryRecord]:
    """Bounded, project-scoped recall. Untrusted memories are excluded (fabric
    default), so only operator/internal project facts surface."""
    fab = fabric or get_fabric()
    return await fab.retrieve(
        query or _RECALL_DEFAULT,
        scopes={_PROJECT_SCOPE},
        limit=limit,
        allow_untrusted=False,
    )


def _classify_record(rec: MemoryRecord) -> str | None:
    """Map a retrieved record back to its ProjectFactType value, if any."""
    for t in ProjectFactType:
        if rec.memory_type == f"project_{t.value}" or f"[project:{t.value}]" in rec.content:
            return t.value
    return None


async def summarize_project(
    query: str = "",
    *,
    limit: int = 12,
    fabric: MemoryFabric | None = None,
) -> dict:
    """Recall project facts and group them by type for a compact status answer."""
    records = await recall_project_context(query, limit=limit, fabric=fabric)
    buckets: dict[str, list[str]] = {t.value: [] for t in ProjectFactType}
    other: list[str] = []
    for rec in records:
        bucket = _classify_record(rec)
        (buckets[bucket] if bucket else other).append(rec.content)
    return {"total": len(records), **buckets, "other": other}
