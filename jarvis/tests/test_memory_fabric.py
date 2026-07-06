"""
tests/test_memory_fabric.py — V63 Milestone 5: scoped memory fabric facade.

Proves the policy layer with INJECTED fake adapters (no ChromaDB): scope filter,
untrusted-source exclusion (anti-injection default), dedup, bounded retrieval,
relevance+recency ranking, provenance preserved, secret redaction on write, and
the live wiring of LLM._maybe_persist_memory through the fabric.
"""
from __future__ import annotations

import asyncio

from core.memory_fabric import (
    EpisodicAdapter,
    MemoryFabric,
    MemoryRecord,
    Provenance,
    Sensitivity,
    get_fabric,
)


class _FakeRetrieve:
    name = "fakeR"

    def __init__(self, records: list[MemoryRecord]):
        self.records = records

    async def retrieve(self, query: str, limit: int) -> list[MemoryRecord]:
        return list(self.records[:limit])


class _FakeStore:
    name = "fakeS"

    def __init__(self):
        self.calls: list[dict] = []

    async def store(self, content, *, memory_type, source, scope, sensitivity) -> bool:
        self.calls.append({
            "content": content, "memory_type": memory_type,
            "source": source, "scope": scope, "sensitivity": sensitivity,
        })
        return True


def _rec(content, *, scope="none", trusted=True, relevance=0.5, ts=None) -> MemoryRecord:
    return MemoryRecord(
        content=content,
        provenance=Provenance(source="internal", scope=scope, trusted=trusted, timestamp=ts),
        relevance=relevance,
        origin="fakeR",
    )


def _retrieve(records, **kw):
    fab = MemoryFabric(retrieval_adapters=[_FakeRetrieve(records)])
    return asyncio.run(fab.retrieve("q", **kw))


# ── retrieve policy ──────────────────────────────────────────────────────────

def test_scope_filter():
    recs = [_rec("a", scope="project"), _rec("b", scope="session"), _rec("c", scope="project")]
    out = _retrieve(recs, scopes={"project"}, limit=10)
    assert {r.content for r in out} == {"a", "c"}


def test_untrusted_excluded_by_default():
    recs = [_rec("trusted", trusted=True), _rec("web-junk", trusted=False)]
    default = _retrieve(recs, limit=10)
    assert {r.content for r in default} == {"trusted"}
    allowed = _retrieve(recs, limit=10, allow_untrusted=True)
    assert {r.content for r in allowed} == {"trusted", "web-junk"}


def test_dedup_by_normalized_content():
    recs = [_rec("Same  Thing", relevance=0.9), _rec("same thing", relevance=0.2)]
    out = _retrieve(recs, limit=10)
    assert len(out) == 1


def test_bounded_retrieval_never_dumps():
    recs = [_rec(f"item{i}", relevance=0.5 + i * 0.01) for i in range(20)]
    out = _retrieve(recs, limit=3)
    assert len(out) == 3


def test_min_relevance_filter():
    recs = [_rec("lo", relevance=0.1), _rec("mid", relevance=0.5), _rec("hi", relevance=0.9)]
    out = _retrieve(recs, limit=10, min_relevance=0.4)
    assert {r.content for r in out} == {"mid", "hi"}


def test_ranking_relevance_then_recency():
    recs = [
        _rec("old_hi", relevance=0.9, ts="2026-01-01T00:00:00"),
        _rec("low", relevance=0.5, ts="2026-09-01T00:00:00"),
        _rec("new_hi", relevance=0.9, ts="2026-06-01T00:00:00"),
    ]
    out = _retrieve(recs, limit=3)
    assert [r.content for r in out] == ["new_hi", "old_hi", "low"]


def test_provenance_preserved():
    recs = [_rec("x", scope="project", ts="2026-06-01T00:00:00")]
    out = _retrieve(recs, limit=1)
    assert out[0].provenance.scope == "project"
    assert out[0].provenance.timestamp == "2026-06-01T00:00:00"
    assert out[0].origin == "fakeR"


def test_empty_query_or_zero_limit_returns_nothing():
    fab = MemoryFabric(retrieval_adapters=[_FakeRetrieve([_rec("x")])])
    assert asyncio.run(fab.retrieve("", limit=5)) == []
    assert asyncio.run(fab.retrieve("q", limit=0)) == []


# ── store policy ─────────────────────────────────────────────────────────────

def test_store_redacts_secret_before_write():
    store = _FakeStore()
    fab = MemoryFabric(storage_adapter=store)
    ok = asyncio.run(fab.store(
        "the key is api_key = sk-ABCDEFGH12345678ZXCV done",
        memory_type="conversation_memory", scope="project",
    ))
    assert ok is True
    written = store.calls[0]["content"]
    assert "sk-ABCDEFGH12345678ZXCV" not in written, "raw secret must be redacted before write"


def test_store_passes_scope_source_sensitivity():
    store = _FakeStore()
    fab = MemoryFabric(storage_adapter=store)
    asyncio.run(fab.store(
        "benign note", memory_type="note", source="internal",
        scope="long_term", sensitivity=Sensitivity.SENSITIVE,
    ))
    call = store.calls[0]
    assert call["memory_type"] == "note"
    assert call["scope"] == "long_term"
    assert call["sensitivity"] == "sensitive"


def test_store_without_adapter_or_empty_is_false():
    assert asyncio.run(MemoryFabric().store("x")) is False
    assert asyncio.run(MemoryFabric(storage_adapter=_FakeStore()).store("   ")) is False


# ── production singleton + wiring ────────────────────────────────────────────

def test_get_fabric_singleton_and_shape():
    f = get_fabric()
    assert f is get_fabric()
    assert isinstance(f.storage_adapter, EpisodicAdapter)
    assert any(isinstance(a, EpisodicAdapter) for a in f.retrieval_adapters)


def test_maybe_persist_memory_routes_through_fabric(monkeypatch):
    import core.llm as llm_mod
    from core.llm import LLM

    captured: list[dict] = []

    class _CaptureFabric:
        async def store(self, content, **kw):
            captured.append({"content": content, **kw})
            return True

    monkeypatch.setattr("core.memory_fabric.get_fabric", lambda: _CaptureFabric())
    monkeypatch.setattr(llm_mod, "should_write_memory", lambda u, a: True)
    monkeypatch.setattr(llm_mod, "classify_memory_scope", lambda u: "project")

    inst = LLM.__new__(LLM)  # _maybe_persist_memory uses no instance state
    asyncio.run(inst._maybe_persist_memory("remember the plan", "we chose async"))

    assert captured, "conversation-memory write must route through the fabric"
    assert captured[0]["memory_type"] == "conversation_memory"
    assert captured[0]["scope"] == "project"
    assert captured[0]["source"] == "internal"


def test_maybe_persist_memory_still_skips_secrets(monkeypatch):
    import core.llm as llm_mod
    from core.llm import LLM

    called = []
    monkeypatch.setattr("core.memory_fabric.get_fabric",
                        lambda: type("F", (), {"store": lambda self, c, **k: called.append(1)})())
    monkeypatch.setattr(llm_mod, "should_write_memory", lambda u, a: True)

    inst = LLM.__new__(LLM)
    asyncio.run(inst._maybe_persist_memory(
        "remember api_key = sk-ABCDEFGH12345678ZXCV", "ok",
    ))
    assert not called, "a secret-bearing turn must never reach the store"
