"""
tests/test_project_context.py — V63 Milestone 8: project & decision awareness.

Proves project facts are recorded/recalled via the M5 fabric at scope=project
(memory-backed, provenance + timestamp), and that the project_note/project_status
tools are wired, correctly classified (LOW_IMPACT / READ_ONLY, non-HITL), and
route through the fabric.
"""
from __future__ import annotations

import asyncio

from core.memory_fabric import MemoryRecord, Provenance
from core.project_context import (
    ProjectFactType,
    recall_project_context,
    record_project_fact,
    summarize_project,
)
from core.risk_classes import RiskClass, classify_tool, requires_hitl


class _FakeFabric:
    def __init__(self, records=None):
        self.stored: list[dict] = []
        self._records = records or []
        self.last_retrieve: dict | None = None

    async def store(self, content, *, memory_type, source, scope, sensitivity):
        self.stored.append({
            "content": content, "memory_type": memory_type,
            "source": source, "scope": scope, "sensitivity": sensitivity,
        })
        return True

    async def retrieve(self, query, *, scopes=None, limit=8, min_relevance=0.0, allow_untrusted=False):
        self.last_retrieve = {
            "query": query, "scopes": scopes, "limit": limit, "allow_untrusted": allow_untrusted,
        }
        return list(self._records[:limit])


def _prec(content, mtype):
    return MemoryRecord(
        content=content,
        provenance=Provenance(source="operator", scope="project"),
        relevance=0.7,
        origin="episodic",
        memory_type=mtype,
    )


# ── record ────────────────────────────────────────────────────────────────────

def test_record_project_fact_stores_at_project_scope():
    fab = _FakeFabric()
    ok = asyncio.run(record_project_fact(ProjectFactType.DECISION, "use async everywhere", fabric=fab))
    assert ok is True
    call = fab.stored[0]
    assert call["scope"] == "project"
    assert call["memory_type"] == "project_decision"
    assert "[project:decision]" in call["content"]
    assert "use async everywhere" in call["content"]
    assert "recorded" in call["content"]  # timestamp provenance


def test_record_rejects_bad_kind_and_empty_text():
    fab = _FakeFabric()
    assert asyncio.run(record_project_fact("nonsense", "x", fabric=fab)) is False
    assert asyncio.run(record_project_fact(ProjectFactType.GOAL, "   ", fabric=fab)) is False
    assert fab.stored == []


# ── recall / summarize ─────────────────────────────────────────────────────────

def test_recall_uses_project_scope_and_bounds():
    fab = _FakeFabric(records=[_prec("[project:goal] ship V63", "project_goal")])
    out = asyncio.run(recall_project_context("goals", limit=5, fabric=fab))
    assert fab.last_retrieve["scopes"] == {"project"}
    assert fab.last_retrieve["allow_untrusted"] is False
    assert fab.last_retrieve["limit"] == 5
    assert len(out) == 1


def test_summarize_groups_by_type():
    records = [
        _prec("[project:goal] ship the runtime", "project_goal"),
        _prec("[project:decision] chose fabric facade", "project_decision"),
        _prec("[project:blocked] waiting on ollama", "project_blocked"),
        _prec("some stray memory", "conversation_memory"),
    ]
    fab = _FakeFabric(records=records)
    summary = asyncio.run(summarize_project(fabric=fab))
    assert summary["total"] == 4
    assert len(summary["goal"]) == 1
    assert len(summary["decision"]) == 1
    assert len(summary["blocked"]) == 1
    assert len(summary["other"]) == 1


# ── risk classification ─────────────────────────────────────────────────────────

def test_tools_are_classified_non_hitl():
    assert classify_tool("project_note") is RiskClass.LOW_IMPACT
    assert classify_tool("project_status") is RiskClass.READ_ONLY
    assert requires_hitl(classify_tool("project_note")) is False
    assert requires_hitl(classify_tool("project_status")) is False


# ── live tool wiring ────────────────────────────────────────────────────────────

def test_project_note_tool_routes_through_fabric(monkeypatch):
    from tools.executor import ToolExecutor
    fab = _FakeFabric()
    monkeypatch.setattr("core.project_context.get_fabric", lambda: fab)

    te = ToolExecutor()
    result = te.execute("project_note", {"kind": "task", "text": "wire the planner"})

    assert result.get("recorded") is True
    assert result.get("kind") == "task"
    assert fab.stored and fab.stored[0]["memory_type"] == "project_task"


def test_project_note_tool_rejects_bad_kind():
    from tools.executor import ToolExecutor
    te = ToolExecutor()
    result = te.execute("project_note", {"kind": "banana", "text": "x"})
    assert "error" in result


def test_project_status_tool_returns_grouped(monkeypatch):
    from tools.executor import ToolExecutor
    fab = _FakeFabric(records=[_prec("[project:goal] ship V63", "project_goal")])
    monkeypatch.setattr("core.project_context.get_fabric", lambda: fab)

    te = ToolExecutor()
    result = te.execute("project_status", {})

    assert result["total"] == 1
    assert result["goal"] == ["[project:goal] ship V63"]
