"""tests/test_semantic_migration_v69.py — V69 M53: migration + store + commands.

Real ephemeral Chroma client (explicit embeddings) + a fake embedding runtime —
deterministic, no Ollama. Exercises the operator-gated migration lifecycle, the
runtime semantic store, and the command parser.
"""
from __future__ import annotations

import pytest

from core.alias_registry import AliasRegistry
from core.embedding_runtime import BatchEmbeddingResult, EmbeddingHealth, EmbeddingResult
from core.semantic_commands import dispatch_semantic_command, parse_semantic_command
from core.semantic_memory import SemanticStore, delta_journal_for
from core.semantic_migration import (
    PHASE_ACTIVE,
    PHASE_READY_TO_ACTIVATE,
    SemanticMigrationController,
)

chromadb = pytest.importorskip("chromadb")

_FP = "395d63bbee28d585"


class FakeRuntime:
    def __init__(self, dim=4, fp=_FP, available=True, fail_after=None):
        self._dim = dim
        self._fp = fp
        self._available = available
        self._fail_after = fail_after
        self._n = 0
        self.max_chunk = 0

    def health(self):
        return EmbeddingHealth(
            self._available, "ollama", "nomic-embed-text:latest", self._dim,
            self._fp, 1, False, "ready" if self._available else "down",
            None if self._available else "provider_unreachable")

    def embed_batch(self, texts, *, should_cancel=None):
        self.max_chunk = max(self.max_chunk, len(texts))
        if should_cancel and should_cancel():
            return BatchEmbeddingResult(status="cancelled", error_class="cancelled", message="x")
        self._n += len(texts)
        if self._fail_after is not None and self._n > self._fail_after:
            return BatchEmbeddingResult(status="error", error_class="provider_error", message="boom")
        vecs = [[float(len(t) % 5) + 1.0] * self._dim for t in texts]
        return BatchEmbeddingResult(status="ok", vectors=vecs, dimension=self._dim,
                                    count=len(texts), fingerprint=self._fp)

    def embed_text(self, text):
        b = self.embed_batch([text])
        if not b.ok:
            return EmbeddingResult(status=b.status, error_class=b.error_class, message=b.message)
        return EmbeddingResult(status="ok", vector=b.vectors[0], dimension=self._dim,
                               fingerprint=self._fp, provider="ollama",
                               model="nomic-embed-text:latest")


@pytest.fixture()
def client():
    from chromadb.config import Settings
    c = chromadb.Client(Settings(allow_reset=True))
    c.reset()
    return c


def _seed_legacy(client, name="jarvis_episodic", docs=None, metas=None, vector_only=0):
    docs = docs or [f"episode {i}" for i in range(6)]
    col = client.get_or_create_collection(name, embedding_function=None)
    col.add(
        ids=[f"ep_{i}" for i in range(len(docs))],
        documents=docs,
        embeddings=[[0.1, 0.2] for _ in docs],
        metadatas=metas or [{"source": "internal", "scope": "none"} for _ in docs],
    )
    for j in range(vector_only):
        col.add(ids=[f"vo_{j}"], embeddings=[[0.3, 0.4]], metadatas=[{"source": "internal"}])
    return col


def _controller(client, tmp_path, runtime=None):
    return SemanticMigrationController(
        client=client, runtime=runtime or FakeRuntime(),
        registry=AliasRegistry(tmp_path / "alias.json"),
        vault_path=tmp_path, migrations_dir=tmp_path / "migrations",
        clock=lambda: "2026-07-13T00:00:00Z",
    )


def _full_migrate(ctrl, logical="jarvis_episodic"):
    ctrl.migrate(logical)
    ctrl.validate(logical)
    return ctrl.activate(logical)


# ── Plan (read-only) ──────────────────────────────────────────────────────────
def test_plan_is_read_only(client, tmp_path):
    _seed_legacy(client)
    ctrl = _controller(client, tmp_path)
    before = {c.name for c in client.list_collections()}
    res = ctrl.plan("jarvis_episodic")
    after = {c.name for c in client.list_collections()}
    assert res.status == "ok"
    assert res.detail["record_count"] == 6
    assert before == after     # no collections created


def test_dry_run_creates_no_staged(client, tmp_path):
    _seed_legacy(client)
    ctrl = _controller(client, tmp_path)
    res = ctrl.migrate("jarvis_episodic", dry_run=True)
    assert res.status == "dry_run"
    names = {c.name for c in client.list_collections()}
    assert names == {"jarvis_episodic"}   # nothing staged


# ── Happy path ────────────────────────────────────────────────────────────────
def test_migrate_validate_activate_happy_path(client, tmp_path):
    _seed_legacy(client, docs=[f"episode {i}" for i in range(6)])
    ctrl = _controller(client, tmp_path)

    m = ctrl.migrate("jarvis_episodic")
    assert m.status == "staged"
    v = ctrl.validate("jarvis_episodic")
    assert v.status == "validated" and v.phase == PHASE_READY_TO_ACTIVATE
    a = ctrl.activate("jarvis_episodic")
    assert a.status == "activated" and a.phase == PHASE_ACTIVE

    # Alias now resolves to the staged physical; legacy retained.
    reg = AliasRegistry(tmp_path / "alias.json")
    active = reg.resolve("jarvis_episodic")
    assert active and active != "jarvis_episodic"
    assert client.get_collection(active, embedding_function=None).count() == 6
    assert "jarvis_episodic" in {c.name for c in client.list_collections()}  # legacy kept


def test_bounded_batches(client, tmp_path):
    _seed_legacy(client, docs=[f"e{i}" for i in range(80)])
    rt = FakeRuntime()
    ctrl = _controller(client, tmp_path, runtime=rt)
    ctrl.migrate("jarvis_episodic")
    assert rt.max_chunk <= 32   # _BATCH_SIZE


# ── Policy filtering ──────────────────────────────────────────────────────────
def test_secrets_are_redacted_not_migrated(client, tmp_path):
    _seed_legacy(client, docs=["clean note", "api_key: SUPERSECRETVALUE1234 leaked"])
    ctrl = _controller(client, tmp_path)
    _full_migrate(ctrl)
    active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    got = client.get_collection(active, embedding_function=None).get(include=["documents"])
    blob = " ".join(got["documents"])
    assert "SUPERSECRETVALUE1234" not in blob
    assert "[REDACTED-SECRET]" in blob


def test_vector_only_records_are_unrecoverable(client, tmp_path):
    _seed_legacy(client, docs=[f"e{i}" for i in range(4)], vector_only=2)
    ctrl = _controller(client, tmp_path)
    plan = ctrl.plan("jarvis_episodic")
    assert plan.detail["record_count"] == 4
    assert plan.detail["unrecoverable_records"] == 2
    _full_migrate(ctrl)
    active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    # Only recoverable records re-embedded; legacy (with vector-only) untouched.
    assert client.get_collection(active, embedding_function=None).count() == 4
    assert client.get_collection("jarvis_episodic", embedding_function=None).count() == 6


# ── Failures / interruption / resume ──────────────────────────────────────────
def test_dimension_drift_never_activates(client, tmp_path):
    _seed_legacy(client)

    class DriftRuntime(FakeRuntime):
        def embed_batch(self, texts, *, should_cancel=None):
            return BatchEmbeddingResult(status="ok", vectors=[[1.0] * 3 for _ in texts],
                                        dimension=3, count=len(texts), fingerprint=_FP)

    ctrl = _controller(client, tmp_path, runtime=DriftRuntime())
    m = ctrl.migrate("jarvis_episodic")
    assert m.status == "validation_failed"
    assert AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic") is None


def test_provider_failure_is_resumable(client, tmp_path):
    _seed_legacy(client, docs=[f"e{i}" for i in range(20)])
    rt = FakeRuntime(fail_after=8)
    ctrl = _controller(client, tmp_path, runtime=rt)
    m = ctrl.migrate("jarvis_episodic")
    assert m.status == "failed"
    # Resume with a healthy runtime completes.
    ctrl2 = _controller(client, tmp_path, runtime=FakeRuntime())
    r = ctrl2.resume("jarvis_episodic")
    assert r.status == "staged"
    assert ctrl2.validate("jarvis_episodic").status == "validated"


def test_activation_denied_before_validation(client, tmp_path):
    _seed_legacy(client)
    ctrl = _controller(client, tmp_path)
    ctrl.migrate("jarvis_episodic")
    a = ctrl.activate("jarvis_episodic")
    assert a.status == "not_validated"
    assert AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic") is None


def test_abort_preserves_collections(client, tmp_path):
    _seed_legacy(client)
    ctrl = _controller(client, tmp_path)
    ctrl.migrate("jarvis_episodic")
    a = ctrl.abort("jarvis_episodic")
    assert a.status == "aborted"
    assert "jarvis_episodic" in {c.name for c in client.list_collections()}


# ── Delta replay ──────────────────────────────────────────────────────────────
def test_delta_written_before_migration_is_replayed(client, tmp_path):
    _seed_legacy(client, docs=[f"e{i}" for i in range(3)])
    # A write arrives while no compatible active collection exists → journaled.
    store = SemanticStore("jarvis_episodic", client=client, runtime=FakeRuntime(),
                          registry=AliasRegistry(tmp_path / "alias.json"),
                          vault_path=tmp_path, migrations_dir=tmp_path / "migrations")
    assert store.write("new_1", "a fresh episode", {"source": "internal"}) == "journaled"
    assert delta_journal_for("jarvis_episodic", migrations_dir=tmp_path / "migrations").count() == 1

    ctrl = _controller(client, tmp_path)
    ctrl.migrate("jarvis_episodic")
    v = ctrl.validate("jarvis_episodic")
    assert v.detail["delta_replayed"] == 1
    ctrl.activate("jarvis_episodic")
    active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    # 3 legacy + 1 delta.
    assert client.get_collection(active, embedding_function=None).count() == 4


def test_validate_is_idempotent(client, tmp_path):
    _seed_legacy(client, docs=[f"e{i}" for i in range(3)])
    dj = delta_journal_for("jarvis_episodic", migrations_dir=tmp_path / "migrations")
    dj.append("new_1", "delta doc", {"source": "internal"})
    ctrl = _controller(client, tmp_path)
    ctrl.migrate("jarvis_episodic")
    ctrl.validate("jarvis_episodic")
    ctrl.validate("jarvis_episodic")   # replay twice
    active_state = ctrl.status()[0]
    # dedup by id → still 3 legacy + 1 delta after activation.
    ctrl.activate("jarvis_episodic")
    active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    assert client.get_collection(active, embedding_function=None).count() == 4
    assert active_state["migration_phase"] in ("VALIDATING", "READY_TO_ACTIVATE")


# ── Rollback ──────────────────────────────────────────────────────────────────
def test_rollback_restores_previous_without_deleting(client, tmp_path):
    # First activation establishes an active collection...
    _seed_legacy(client, name="jarvis_episodic", docs=[f"e{i}" for i in range(3)])
    ctrl = _controller(client, tmp_path)
    _full_migrate(ctrl)
    first_active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")

    # ...then a second migration from the now-active collection to a new fp.
    ctrl2 = _controller(client, tmp_path, runtime=FakeRuntime(fp="aaaabbbbccccdddd"))
    _full_migrate(ctrl2)
    second_active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    assert second_active != first_active

    # Rollback restores the first; neither collection deleted.
    rb = ctrl2.rollback("jarvis_episodic")
    assert rb.status == "rolled_back"
    assert AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic") == first_active
    names = {c.name for c in client.list_collections()}
    assert first_active in names and second_active in names


# ── Semantic store read/write ─────────────────────────────────────────────────
def test_store_read_empty_when_incompatible(client, tmp_path):
    _seed_legacy(client)   # legacy unstamped
    store = SemanticStore("jarvis_episodic", client=client, runtime=FakeRuntime(),
                          registry=AliasRegistry(tmp_path / "alias.json"),
                          vault_path=tmp_path, migrations_dir=tmp_path / "migrations")
    # Never queries the incompatible legacy collection.
    assert store.query("anything") == []


def test_store_read_write_after_activation(client, tmp_path):
    _seed_legacy(client, docs=["alpha episode", "beta episode"])
    ctrl = _controller(client, tmp_path)
    _full_migrate(ctrl)
    store = SemanticStore("jarvis_episodic", client=client, runtime=FakeRuntime(),
                          registry=AliasRegistry(tmp_path / "alias.json"),
                          vault_path=tmp_path, migrations_dir=tmp_path / "migrations")
    assert store.write("w1", "gamma episode", {"source": "internal"}) == "ok"
    res = store.query("episode", n_results=3)
    assert isinstance(res, list) and len(res) >= 1


def test_store_redacts_secret_never_indexes_it(client, tmp_path):
    _seed_legacy(client, docs=["x"])
    ctrl = _controller(client, tmp_path)
    _full_migrate(ctrl)
    store = SemanticStore("jarvis_episodic", client=client, runtime=FakeRuntime(),
                          registry=AliasRegistry(tmp_path / "alias.json"),
                          vault_path=tmp_path, migrations_dir=tmp_path / "migrations")
    # A note containing a secret is stored with the secret REDACTED — never indexed.
    status = store.write("s1", "deploy note password: hunter2secret1234 end", {})
    assert status == "ok"
    active = AliasRegistry(tmp_path / "alias.json").resolve("jarvis_episodic")
    got = client.get_collection(active, embedding_function=None).get(ids=["s1"], include=["documents"])
    assert "hunter2secret1234" not in got["documents"][0]
    assert "[REDACTED-SECRET]" in got["documents"][0]


def test_staged_never_queried_by_store(client, tmp_path):
    # Build a staged collection but do NOT activate; store must not read it.
    _seed_legacy(client, docs=["one", "two"])
    ctrl = _controller(client, tmp_path)
    ctrl.migrate("jarvis_episodic")   # staged built, not activated
    store = SemanticStore("jarvis_episodic", client=client, runtime=FakeRuntime(),
                          registry=AliasRegistry(tmp_path / "alias.json"),
                          vault_path=tmp_path, migrations_dir=tmp_path / "migrations")
    # No alias yet → resolves legacy (incompatible) → empty, never the staged.
    assert store.query("one") == []


# ── Status inventory ──────────────────────────────────────────────────────────
def test_status_reports_states(client, tmp_path):
    _seed_legacy(client)
    ctrl = _controller(client, tmp_path)
    rows = {r["logical_name"]: r for r in ctrl.status()}
    assert "jarvis_episodic" in rows
    # legacy unstamped → not OK
    assert rows["jarvis_episodic"]["compat_state"] in ("unstamped", "reindex_required")
    _full_migrate(ctrl)
    rows2 = {r["logical_name"]: r for r in ctrl.status()}
    assert rows2["jarvis_episodic"]["compat_state"] == "ok"
    assert rows2["jarvis_episodic"]["migration_phase"] == PHASE_ACTIVE


# ── Command parser ────────────────────────────────────────────────────────────
def test_parse_status_is_read_only():
    cmd = parse_semantic_command("semantic-status")
    assert cmd and cmd.action == "status" and not cmd.effectful


def test_parse_migrate_dry_run_not_effectful():
    cmd = parse_semantic_command("semantic-migrate jarvis_episodic --dry-run")
    assert cmd.action == "migrate" and cmd.logical == "jarvis_episodic"
    assert cmd.dry_run and not cmd.effectful


def test_parse_activate_is_effectful():
    cmd = parse_semantic_command("semantic-activate jarvis_episodic")
    assert cmd.action == "activate" and cmd.effectful


def test_parse_rejects_unknown_logical():
    assert parse_semantic_command("semantic-plan some_evil_collection") is None


def test_non_semantic_text_is_ignored():
    assert parse_semantic_command("what time is it?") is None


def test_dispatch_status(client, tmp_path):
    _seed_legacy(client)
    ctrl = _controller(client, tmp_path)
    out = dispatch_semantic_command(parse_semantic_command("semantic-status"), ctrl)
    assert out["action"] == "status" and isinstance(out["collections"], list)
