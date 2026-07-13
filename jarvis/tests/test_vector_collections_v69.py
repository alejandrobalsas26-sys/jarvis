"""tests/test_vector_collections_v69.py — V69 M52: compatibility & reindex.

Hermetic coverage of the collection fingerprint guard and the atomic, resumable
reindex engine. Uses in-memory fake collections + a fake embedding runtime — no
ChromaDB, no Ollama.

Invariants:
  * a collection stamped with the active fingerprint is compatible
  * a collection stamped with a different model → REINDEX_REQUIRED (data kept)
  * an unstamped legacy collection → UNSTAMPED (fail-closed, not silently used)
  * runtime unavailable → UNKNOWN (cannot decide, fail-closed)
  * reindex is atomic (old collection untouched until validated + activated)
  * reindex validates counts/dimensions before activation
  * an interrupted reindex journals progress and RESUMES from the last offset
  * completed reindex retains the old collection for rollback
"""
from __future__ import annotations



from core.embedding_runtime import EmbeddingHealth
from core.vector_collections import (
    COMPAT_OK,
    COMPAT_REINDEX_REQUIRED,
    COMPAT_UNKNOWN,
    COMPAT_UNSTAMPED,
    META_DIMENSION,
    ReindexEngine,
    ReindexJournal,
    check_compatibility,
    stamp_metadata,
)

_ACTIVE_FP = "fp_active_aaaa"
_OTHER_FP = "fp_other_bbbb"


def _health(available=True, fp=_ACTIVE_FP, dim=8) -> EmbeddingHealth:
    return EmbeddingHealth(
        available=available, provider="ollama", model="nomic-embed-text:latest",
        dimension=dim, fingerprint=fp, schema_version=1, fallback_active=False,
        message="ready" if available else "down",
        error_class=None if available else "provider_unreachable",
    )


# ── Compatibility ─────────────────────────────────────────────────────────────
def test_matching_fingerprint_is_ok():
    stamp = stamp_metadata(_health(), created_at="2026-07-13T00:00:00Z")
    res = check_compatibility(stamp, _health())
    assert res.status == COMPAT_OK and res.compatible


def test_different_model_requires_reindex():
    stamp = stamp_metadata(_health(fp=_OTHER_FP, dim=6), created_at="t")
    res = check_compatibility(stamp, _health())
    assert res.status == COMPAT_REINDEX_REQUIRED
    assert not res.compatible
    assert res.stored_fingerprint == _OTHER_FP
    assert res.active_fingerprint == _ACTIVE_FP


def test_unstamped_legacy_collection_is_flagged():
    res = check_compatibility({}, _health())
    assert res.status == COMPAT_UNSTAMPED
    assert not res.compatible


def test_unavailable_runtime_is_unknown():
    stamp = stamp_metadata(_health(), created_at="t")
    res = check_compatibility(stamp, _health(available=False))
    assert res.status == COMPAT_UNKNOWN
    assert not res.compatible


def test_fingerprint_match_but_dimension_drift_requires_reindex():
    stamp = stamp_metadata(_health(), created_at="t")
    stamp[META_DIMENSION] = 999   # inconsistent with active dim=8
    res = check_compatibility(stamp, _health())
    assert res.status == COMPAT_REINDEX_REQUIRED


def test_stamp_has_all_six_keys():
    stamp = stamp_metadata(_health(), created_at="2026-07-13T00:00:00Z")
    for key in ("embedding_provider", "embedding_model", "embedding_dimension",
                "embedding_fingerprint", "embedding_schema_version", "created_at"):
        assert key in stamp


# ── Fakes for the reindex engine ──────────────────────────────────────────────
class FakeSource:
    def __init__(self, name, records):
        self.name = name
        self._records = records

    def count(self):
        return len(self._records)

    def get_metadata(self):
        return {}

    def get_page(self, offset, limit):
        return [dict(r) for r in self._records[offset : offset + limit]]


class FakeTarget:
    def __init__(self, name, metadata):
        self.name = name
        self.metadata = metadata
        self.ids: list[str] = []
        self.embeddings: list[list[float]] = []

    def count(self):
        return len(self.ids)

    def upsert(self, ids, documents, embeddings, metadatas):
        self.ids.extend(ids)
        self.embeddings.extend(embeddings)


class FakeFactory:
    def __init__(self):
        self.created: dict[str, FakeTarget] = {}
        self.activations: list[tuple[str, str]] = []

    def create(self, name, metadata):
        t = self.created.get(name)
        if t is None:
            t = FakeTarget(name, metadata)
            self.created[name] = t
        return t

    def activate(self, staged_name, active_name):
        self.activations.append((staged_name, active_name))

    def exists(self, name):
        return name in self.created


class FakeRuntime:
    """Runtime whose embed_batch returns fixed-dim vectors; health is injectable."""

    def __init__(self, dim=8, available=True, fp=_ACTIVE_FP, fail_after=None):
        self._dim = dim
        self._available = available
        self._fp = fp
        self._fail_after = fail_after   # fail embed once >= this many rows embedded
        self._embedded = 0

    def health(self):
        return _health(available=self._available, fp=self._fp, dim=self._dim)

    def embed_batch(self, texts, *, should_cancel=None):
        from core.embedding_runtime import BatchEmbeddingResult

        if should_cancel and should_cancel():
            return BatchEmbeddingResult(status="cancelled", error_class="cancelled",
                                        message="cancelled")
        self._embedded += len(texts)
        if self._fail_after is not None and self._embedded > self._fail_after:
            return BatchEmbeddingResult(status="error", error_class="provider_error",
                                        message="provider failed")
        vecs = [[1.0] * self._dim for _ in texts]
        return BatchEmbeddingResult(
            status="ok", vectors=vecs, dimension=self._dim, count=len(texts),
            provider="ollama", model="nomic-embed-text:latest", fingerprint=self._fp,
        )


def _records(n):
    return [{"id": f"id{i}", "document": f"doc {i}", "metadata": {"source": "s"}} for i in range(n)]


# ── Reindex: happy path ───────────────────────────────────────────────────────
def test_reindex_atomic_activation(tmp_path):
    src = FakeSource("knowledge_vault", _records(10))
    factory = FakeFactory()
    engine = ReindexEngine(FakeRuntime(dim=8), factory, batch_size=4)
    res = engine.reindex(src, created_at="t", journal_path=tmp_path / "j.json")

    assert res.status == "ok"
    assert res.reindexed == 10 and res.total == 10
    assert res.activated and res.rollback_available
    # exactly one staged collection, activated onto the source name
    assert len(factory.activations) == 1
    staged, active = factory.activations[0]
    assert active == "knowledge_vault"
    assert factory.created[staged].count() == 10
    # journal cleared after success
    assert not (tmp_path / "j.json").exists()


def test_reindex_never_activates_on_dimension_drift(tmp_path):
    class DriftRuntime(FakeRuntime):
        def embed_batch(self, texts, *, should_cancel=None):
            from core.embedding_runtime import BatchEmbeddingResult
            # return wrong dimension vs health.dimension=8
            return BatchEmbeddingResult(status="ok", vectors=[[1.0] * 3 for _ in texts],
                                        dimension=3, count=len(texts), fingerprint=_ACTIVE_FP)

    engine = ReindexEngine(DriftRuntime(dim=8), FakeFactory(), batch_size=4)
    res = engine.reindex(FakeSource("c", _records(6)), created_at="t",
                         journal_path=tmp_path / "j.json")
    assert res.status == "validation_failed"
    assert not res.activated
    assert res.rollback_available


def test_reindex_fails_when_runtime_unavailable():
    engine = ReindexEngine(FakeRuntime(available=False), FakeFactory())
    res = engine.reindex(FakeSource("c", _records(3)), created_at="t")
    assert res.status == "failed"
    assert not res.activated


def test_reindex_aborts_on_embedding_failure(tmp_path):
    engine = ReindexEngine(FakeRuntime(dim=8, fail_after=4), FakeFactory(), batch_size=4)
    res = engine.reindex(FakeSource("c", _records(12)), created_at="t",
                         journal_path=tmp_path / "j.json")
    assert res.status == "failed"
    assert not res.activated
    assert res.rollback_available
    # journal preserved for resume
    assert (tmp_path / "j.json").exists()


# ── Reindex: interruption + resume ────────────────────────────────────────────
def test_reindex_cancellation_journals_progress(tmp_path):
    journal = tmp_path / "j.json"
    engine = ReindexEngine(FakeRuntime(dim=8), FakeFactory(), batch_size=2)
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 2   # allow ~2 chunks, then cancel

    res = engine.reindex(FakeSource("c", _records(10)), created_at="t",
                         journal_path=journal, should_cancel=cancel)
    assert res.status == "failed"
    assert not res.activated
    assert journal.exists()
    j = ReindexJournal.load(journal)
    assert j is not None and 0 < j.offset < 10 and not j.activated


def test_reindex_resumes_from_journal(tmp_path):
    journal = tmp_path / "j.json"
    src = FakeSource("c", _records(10))
    factory = FakeFactory()

    # First pass: cancel midway.
    engine1 = ReindexEngine(FakeRuntime(dim=8), factory, batch_size=2)
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 2

    r1 = engine1.reindex(src, created_at="t", journal_path=journal, should_cancel=cancel)
    assert r1.status == "failed"
    partial = ReindexJournal.load(journal).offset
    assert 0 < partial < 10

    # Second pass: resume to completion, same staged collection.
    engine2 = ReindexEngine(FakeRuntime(dim=8), factory, batch_size=2)
    r2 = engine2.reindex(src, created_at="t", journal_path=journal)
    assert r2.status == "ok"
    assert r2.reindexed == 10
    staged, _ = factory.activations[0]
    # Resume did NOT re-embed the already-committed prefix into a fresh target:
    # the staged collection holds exactly the full source count, no duplicates.
    assert factory.created[staged].count() == 10


def test_journal_roundtrip(tmp_path):
    j = ReindexJournal(source_name="c", staged_name="c__v1_x", fingerprint="x",
                       dimension=8, total=10, offset=4, path=tmp_path / "j.json")
    j.save()
    loaded = ReindexJournal.load(tmp_path / "j.json")
    assert loaded.offset == 4 and loaded.total == 10 and loaded.fingerprint == "x"
    j.clear()
    assert ReindexJournal.load(tmp_path / "j.json") is None
