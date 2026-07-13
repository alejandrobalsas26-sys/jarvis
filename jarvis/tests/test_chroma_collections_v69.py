"""tests/test_chroma_collections_v69.py — V69 M53: production Chroma adapter.

Uses a real ephemeral (in-memory) Chroma client — deterministic, no Ollama, no
persistence. Embeddings are always explicit, so no embedding model is needed.
"""
from __future__ import annotations

import pytest

from core.chroma_collections import (
    ChromaCollectionFactory,
    ChromaSourceCollection,
    is_managed_physical_name,
    physical_name,
)

chromadb = pytest.importorskip("chromadb")


@pytest.fixture()
def client():
    # A fresh, reset in-memory system per test — chromadb shares its in-memory
    # backend across Client() instances in one process, so reset() isolates tests.
    from chromadb.config import Settings

    c = chromadb.Client(Settings(allow_reset=True))
    c.reset()
    return c


def _seed(client, name, n, *, with_docs=True):
    col = client.get_or_create_collection(name, embedding_function=None)
    col.add(
        ids=[f"id{i}" for i in range(n)],
        documents=[f"doc {i}" for i in range(n)] if with_docs else None,
        embeddings=[[float(i), float(i) + 1.0] for i in range(n)],
        metadatas=[{"source": "s", "scope": "none", "n": i} for i in range(n)],
    )
    return col


# ── Deterministic naming ──────────────────────────────────────────────────────
def test_physical_name_is_deterministic():
    a = physical_name("jarvis_episodic", 1, "395d63bbee28d585")
    b = physical_name("jarvis_episodic", 1, "395d63bbee28d585")
    assert a == b == "jarvis_episodic__v1__395d63bbee28d585"
    assert is_managed_physical_name(a)


def test_physical_name_rejects_unsafe_logical():
    with pytest.raises(ValueError):
        physical_name("../evil name", 1, "abcdef")


# ── Source pagination ─────────────────────────────────────────────────────────
def test_source_count_and_paginated_reads(client):
    _seed(client, "src", 10)
    src = ChromaSourceCollection("src", client.get_collection("src", embedding_function=None))
    assert src.count() == 10
    page = src.get_page(0, 4)
    assert len(page) == 4
    assert page[0]["id"] and page[0]["document"].startswith("doc")
    # bounded page never exceeds limit
    assert len(src.get_page(8, 100)) == 2


def test_source_preserves_ids_docs_metadata(client):
    _seed(client, "src", 3)
    src = ChromaSourceCollection("src", client.get_collection("src", embedding_function=None))
    all_recs = src.get_page(0, 100)
    ids = {r["id"] for r in all_recs}
    assert ids == {"id0", "id1", "id2"}
    assert all(r["metadata"].get("source") == "s" for r in all_recs)


def test_vector_only_record_has_empty_document(client):
    # A record with an embedding but no document → document="" (never fabricated).
    col = client.get_or_create_collection("voc", embedding_function=None)
    col.add(ids=["x"], embeddings=[[0.1, 0.2]], metadatas=[{"source": "s"}])
    src = ChromaSourceCollection("voc", client.get_collection("voc", embedding_function=None))
    rec = src.get_page(0, 10)[0]
    assert rec["document"] == ""


# ── Factory ──────────────────────────────────────────────────────────────────
def test_factory_create_uses_explicit_embeddings(client):
    factory = ChromaCollectionFactory(client)
    tgt = factory.create("t__v1__abcdef", {"embedding_fingerprint": "abcdef"})
    tgt.upsert(["a"], ["doc a"], [[0.1, 0.2, 0.3]], [{"source": "s"}])
    assert tgt.count() == 1
    # Read back the explicit vector.
    got = client.get_collection("t__v1__abcdef", embedding_function=None).get(
        include=["embeddings"])
    assert len(got["embeddings"][0]) == 3


def test_factory_exists_and_collision_detection(client):
    factory = ChromaCollectionFactory(client)
    assert not factory.exists("t__v1__abcdef")
    factory.create("t__v1__abcdef", {})
    assert factory.exists("t__v1__abcdef")


def test_factory_get_source_missing_returns_none(client):
    factory = ChromaCollectionFactory(client)
    assert factory.get_source("does_not_exist") is None


def test_factory_create_rejects_unsafe_name(client):
    factory = ChromaCollectionFactory(client)
    with pytest.raises(ValueError):
        factory.create("bad/name space", {})


def test_metadata_cleaning_drops_non_scalar(client):
    factory = ChromaCollectionFactory(client)
    tgt = factory.create("clean__v1__abcdef", {})
    # list / None values must be dropped so Chroma accepts the write
    tgt.upsert(["a"], ["d"], [[0.1, 0.2]], [{"ok": "yes", "bad_list": [1, 2], "bad_none": None}])
    meta = client.get_collection("clean__v1__abcdef", embedding_function=None).get(
        include=["metadatas"])["metadatas"][0]
    assert meta.get("ok") == "yes"
    assert "bad_list" not in meta and "bad_none" not in meta


def test_activate_records_intent_no_rename(client):
    factory = ChromaCollectionFactory(client)
    factory.create("stg__v1__abcdef", {})
    factory.activate("stg__v1__abcdef", "logical")
    assert factory.activations == [("stg__v1__abcdef", "logical")]
    # No physical rename happened — the staged name still exists as-is.
    assert factory.exists("stg__v1__abcdef")


# ── ReindexEngine integration over the real adapter ───────────────────────────
def test_reindex_engine_end_to_end_with_real_chroma(client):
    from core.vector_collections import ReindexEngine

    _seed(client, "logical", 7)
    src = ChromaSourceCollection("logical", client.get_collection("logical", embedding_function=None))
    factory = ChromaCollectionFactory(client)

    class _Rt:
        def health(self):
            from core.embedding_runtime import EmbeddingHealth
            return EmbeddingHealth(True, "ollama", "nomic-embed-text:latest", 4,
                                   "abcdef123456", 1, False, "ready")

        def embed_batch(self, texts, *, should_cancel=None):
            from core.embedding_runtime import BatchEmbeddingResult
            return BatchEmbeddingResult(status="ok", vectors=[[1.0] * 4 for _ in texts],
                                        dimension=4, count=len(texts), fingerprint="abcdef123456")

    engine = ReindexEngine(_Rt(), factory, batch_size=3)
    res = engine.reindex(src, created_at="t")
    assert res.status == "ok" and res.reindexed == 7
    staged = res.staged_name
    assert client.get_collection(staged, embedding_function=None).count() == 7
