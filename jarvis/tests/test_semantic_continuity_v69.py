"""tests/test_semantic_continuity_v69.py — V69 M53.8/M53.9: restart continuity.

Metadata-only boot restoration + bounded shutdown checkpoint, with a real
ephemeral Chroma client and a fake embedding runtime (no Ollama).
"""
from __future__ import annotations

import pytest

from core.alias_registry import AliasRegistry
from core.embedding_runtime import BatchEmbeddingResult, EmbeddingHealth, EmbeddingResult
from core.semantic_migration import SemanticMigrationController
from core.vector_collections import ReindexJournal

chromadb = pytest.importorskip("chromadb")
_FP = "395d63bbee28d585"


class FakeRuntime:
    def __init__(self, dim=4, fp=_FP, available=True):
        self._dim, self._fp, self._available = dim, fp, available

    def health(self):
        return EmbeddingHealth(self._available, "ollama", "nomic-embed-text:latest",
                               self._dim, self._fp, 1, False,
                               "ready" if self._available else "down",
                               None if self._available else "provider_unreachable")

    def active_fingerprint(self):
        return self._fp if self._available else ""

    def embed_batch(self, texts, *, should_cancel=None):
        return BatchEmbeddingResult(status="ok", vectors=[[1.0] * self._dim for _ in texts],
                                    dimension=self._dim, count=len(texts), fingerprint=self._fp)

    def embed_text(self, text):
        return EmbeddingResult(status="ok", vector=[1.0] * self._dim, dimension=self._dim,
                               fingerprint=self._fp, provider="ollama",
                               model="nomic-embed-text:latest")


@pytest.fixture()
def client():
    from chromadb.config import Settings
    c = chromadb.Client(Settings(allow_reset=True))
    c.reset()
    return c


def _ctrl(client, tmp_path, runtime=None):
    return SemanticMigrationController(
        client=client, runtime=runtime or FakeRuntime(),
        registry=AliasRegistry(tmp_path / "alias.json"),
        vault_path=tmp_path, migrations_dir=tmp_path / "migrations",
        clock=lambda: "T")


def _seed(client, name="jarvis_episodic", n=4):
    col = client.get_or_create_collection(name, embedding_function=None)
    col.add(ids=[f"ep_{i}" for i in range(n)], documents=[f"e{i}" for i in range(n)],
            embeddings=[[0.1, 0.2] for _ in range(n)],
            metadatas=[{"source": "internal"} for _ in range(n)])
    return col


# ── Boot restoration ──────────────────────────────────────────────────────────
def test_boot_reports_reindex_required_for_legacy(client, tmp_path):
    _seed(client)
    summary = _ctrl(client, tmp_path).boot_summary(["jarvis_episodic"])
    row = summary["collections"][0]
    assert row["status"] == "REINDEX_REQUIRED"
    assert summary["overall"] == "DEGRADED"


def test_boot_reports_active_after_migration(client, tmp_path):
    _seed(client)
    ctrl = _ctrl(client, tmp_path)
    ctrl.migrate("jarvis_episodic"); ctrl.validate("jarvis_episodic"); ctrl.activate("jarvis_episodic")
    # Fresh controller (simulates restart) reads durable alias + metadata only.
    summary = _ctrl(client, tmp_path).boot_summary(["jarvis_episodic"])
    row = summary["collections"][0]
    assert row["status"] == "ACTIVE"
    assert row["records"] == 4
    assert row["active_physical"] and row["active_physical"] != "jarvis_episodic"


def test_boot_alias_survives_restart(client, tmp_path):
    _seed(client)
    ctrl = _ctrl(client, tmp_path)
    ctrl.migrate("jarvis_episodic"); ctrl.validate("jarvis_episodic"); ctrl.activate("jarvis_episodic")
    first = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    # New registry object from the same file → same active physical.
    assert AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic") == first


def test_boot_missing_collection_is_honest(client, tmp_path):
    # No collection seeded, no alias → NONE, not a fabricated empty ACTIVE.
    summary = _ctrl(client, tmp_path).boot_summary(["jarvis_episodic"])
    assert summary["collections"][0]["status"] == "NONE"


def test_boot_runtime_unavailable_degrades(client, tmp_path):
    _seed(client)
    summary = _ctrl(client, tmp_path, runtime=FakeRuntime(available=False)).boot_summary(
        ["jarvis_episodic"])
    assert summary["collections"][0]["status"] == "UNAVAILABLE"
    assert summary["overall"] == "DEGRADED"


def test_boot_detects_interrupted_migration(client, tmp_path):
    _seed(client)
    # Simulate an interrupted migration journal.
    (tmp_path / "migrations").mkdir(parents=True, exist_ok=True)
    ReindexJournal(source_name="jarvis_episodic", staged_name="jarvis_episodic__v1_x",
                   fingerprint=_FP, dimension=4, total=10, offset=4,
                   path=tmp_path / "migrations" / "jarvis_episodic.reindex.json").save()
    summary = _ctrl(client, tmp_path).boot_summary(["jarvis_episodic"])
    row = summary["collections"][0]
    assert row["status"] == "MIGRATING"
    assert "resume" in row["detail"]


def test_boot_does_not_create_collections(client, tmp_path):
    _seed(client)
    before = {c.name for c in client.list_collections()}
    _ctrl(client, tmp_path).boot_summary(["jarvis_episodic", "knowledge_vault"])
    after = {c.name for c in client.list_collections()}
    assert before == after   # metadata-only; never creates


def test_render_boot_summary_is_ascii_and_internal_free(client, tmp_path):
    _seed(client)
    summary = _ctrl(client, tmp_path).boot_summary(["jarvis_episodic"])
    lines = SemanticMigrationController.render_boot_summary(summary)
    blob = "\n".join(lines)
    assert blob.isascii()
    assert "SEMANTIC MEMORY" in blob
    for tok in ("torch", "traceback", "chromadb", "httpx"):
        assert tok not in blob.lower()


# ── Shutdown checkpoint ───────────────────────────────────────────────────────
def test_shutdown_checkpoint_is_bounded_and_safe():
    import asyncio

    from core.semantic_migration import semantic_shutdown_checkpoint
    # Must complete quickly and never raise, even with no state on disk.
    asyncio.run(asyncio.wait_for(semantic_shutdown_checkpoint(), timeout=5.0))
