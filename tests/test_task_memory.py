"""
tests/test_task_memory.py — V58.0 COGNITIVE CORE persistent task memory.

Verifies append-only JSONL persistence, recent_tasks ordering, and the cheap
token-overlap similarity search. Uses a tmp_path file — no shared state, no
network/admin/hardware.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import json
import pytest
from core.task_memory import TaskMemory
from core.cognitive_engine import CognitiveEngine
from core.cognitive_types import CognitivePlan, ReflectionResult


@pytest.fixture
def mem(tmp_path) -> TaskMemory:
    return TaskMemory(path=tmp_path / "task_memory.jsonl")


def _plan(objective):
    return CognitivePlan(objective=objective)


class TestPersistence:
    def test_record_appends_jsonl(self, mem):
        mem.record_task(_plan("scan the network"), [], ReflectionResult(success=True))
        mem.record_task(_plan("isolate host"), [], ReflectionResult())
        lines = mem.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        # each line is valid JSON
        for ln in lines:
            json.loads(ln)

    def test_recent_tasks_newest_first(self, mem):
        for i in range(3):
            mem.record_task(_plan(f"objective {i}"), [], ReflectionResult())
        recent = mem.recent_tasks(limit=2)
        assert len(recent) == 2
        assert recent[0]["plan"]["objective"] == "objective 2"
        assert recent[1]["plan"]["objective"] == "objective 1"

    def test_empty_memory_returns_empty(self, mem):
        assert mem.recent_tasks() == []
        assert mem.find_similar_tasks("anything") == []

    def test_tolerates_corrupt_tail_line(self, mem):
        mem.record_task(_plan("good record"), [], ReflectionResult())
        with open(mem.path, "a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
        recent = mem.recent_tasks()
        assert len(recent) == 1
        assert recent[0]["plan"]["objective"] == "good record"


class TestSimilarity:
    def test_finds_similar_objectives(self, mem):
        mem.record_task(_plan("scan the corporate network for open ports"), [], None)
        mem.record_task(_plan("write incident report for the breach"), [], None)
        hits = mem.find_similar_tasks("network port scan", limit=5)
        assert hits
        assert "scan" in hits[0]["plan"]["objective"]

    def test_no_overlap_returns_empty(self, mem):
        mem.record_task(_plan("scan the network"), [], None)
        assert mem.find_similar_tasks("completely unrelated zzz") == []


class TestEngineIntegration:
    def test_engine_records_to_memory(self, mem):
        class FakeExecutor:
            async def aexecute(self, tool_name, tool_input, reasoning=""):
                return {"status": "ok"}
        import asyncio
        eng = CognitiveEngine(tool_executor=FakeExecutor(), memory=mem)
        asyncio.run(eng.run_task("search threat intelligence feeds"))
        assert len(mem.recent_tasks()) == 1
