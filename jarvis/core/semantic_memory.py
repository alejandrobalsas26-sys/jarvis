"""core/semantic_memory.py — V69 M53: live semantic store + migration delta journal.

The runtime read/write resolver every managed semantic consumer (episodic memory)
goes through. It removes the last split-brain: no consumer touches Chroma's
implicit/default embedder anymore. Every read embeds the query, and every write
embeds the document, through the unified M52 runtime (Ollama nomic-embed-text),
against the ACTIVE physical collection resolved from the durable alias registry.

Lifecycle for a logical collection (e.g. ``jarvis_episodic``):

  * No alias yet (legacy unstamped collection present): the active semantic path
    is REINDEX_REQUIRED. Reads return empty (we NEVER query an incompatible
    collection with a mismatched embedder). Writes are appended to a bounded,
    durable **migration-delta journal** so nothing is lost, then replayed into the
    staged collection at migration time. This is the honest, invariant-preserving
    behavior until the operator runs the migration.
  * Alias active (stamped, compatible): reads/writes use explicit vectors against
    the active physical collection. If a migration is in progress, writes are ALSO
    appended to the delta journal so the staged collection stays complete.

Nothing here decides paths, names, or targets from model input — all identity is
code/operator controlled.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from core.vector_collections import (
    COMPAT_OK,
    check_compatibility,
    stamp_metadata,
)

# Default on-disk locations (deterministic, never model-chosen).
_VAULT_PATH = Path(__file__).parent.parent / "brain" / "vector_store"
_MIGRATIONS_DIR = _VAULT_PATH / "migrations"
_ALIAS_PATH = _VAULT_PATH / "alias_registry.json"

# Bounded delta journal — protects RAM/disk on the CPU-only host. When exceeded,
# further writes are dropped with a warning rather than growing unbounded (the
# operator is told to migrate). 50k episodic deltas is already far beyond a normal
# migration window.
_DELTA_MAX_LINES = 50_000


def _content_hash(doc: str) -> str:
    return hashlib.sha256((doc or "").encode("utf-8", "ignore")).hexdigest()[:16]


@dataclass
class MigrationDeltaJournal:
    """Bounded, durable, dedup-on-replay journal of semantic writes.

    Append-only JSONL. Each line is one write ``{id, document, metadata, chash}``.
    Replay is idempotent and deduplicated by record id (last write wins), so
    re-running a resumed migration never double-inserts.
    """

    path: Path
    _count: int = 0

    def __post_init__(self) -> None:
        self._count = self._line_count()

    def _line_count(self) -> int:
        try:
            if not self.path.exists():
                return 0
            with self.path.open("r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:  # noqa: BLE001
            return 0

    def exists(self) -> bool:
        return self.path.exists()

    def append(self, record_id: str, document: str, metadata: dict) -> bool:
        if self._count >= _DELTA_MAX_LINES:
            logger.warning(
                f"SEMANTIC_DELTA: journal {self.path.name} at cap "
                f"({_DELTA_MAX_LINES}) — dropping write; run the migration."
            )
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "id": str(record_id), "document": document,
                "metadata": dict(metadata or {}), "chash": _content_hash(document),
            }, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._count += 1
            return True
        except Exception as e:  # noqa: BLE001 — never crash the write path
            logger.warning(f"SEMANTIC_DELTA: append failed: {e}")
            return False

    def read_deduped(self) -> list[dict]:
        """Return the latest write per id, in stable insertion order (dedup)."""
        latest: dict[str, dict] = {}
        try:
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except Exception:  # noqa: BLE001 — skip a torn/corrupt line
                        continue
                    rid = str(rec.get("id", ""))
                    if rid:
                        latest[rid] = rec
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SEMANTIC_DELTA: read failed: {e}")
        return list(latest.values())

    def count(self) -> int:
        return self._count

    def clear(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass
        self._count = 0


def delta_journal_for(logical_name: str, *, migrations_dir: Path = _MIGRATIONS_DIR) -> MigrationDeltaJournal:
    return MigrationDeltaJournal(migrations_dir / f"{logical_name}.delta.jsonl")


@dataclass
class SemanticResolution:
    """The resolved active target for a logical collection."""

    logical_name: str
    physical_name: str | None
    compatible: bool
    compat_status: str
    stored_meta: dict


class SemanticStore:
    """Runtime read/write resolver for one logical semantic collection.

    Injectable (client / runtime / registry / paths) so it is fully testable with
    fakes and a real ephemeral Chroma client — no Ollama required in tests.
    """

    def __init__(
        self,
        logical_name: str,
        *,
        client=None,
        runtime=None,
        registry=None,
        vault_path: Path | None = None,
        migrations_dir: Path | None = None,
    ) -> None:
        self.logical_name = logical_name
        self._client = client
        self._runtime = runtime
        self._registry = registry
        self._vault_path = vault_path or _VAULT_PATH
        self._migrations_dir = migrations_dir or _MIGRATIONS_DIR

    # ── lazy production wiring ───────────────────────────────────────────────
    def _get_client(self):
        if self._client is None:
            from core.chroma_collections import build_client
            self._client = build_client(self._vault_path)
        return self._client

    def _get_runtime(self):
        if self._runtime is None:
            from core.embedding_runtime import get_runtime
            self._runtime = get_runtime()
        return self._runtime

    def _get_registry(self):
        if self._registry is None:
            from core.alias_registry import AliasRegistry
            self._registry = AliasRegistry(self._vault_path / "alias_registry.json")
        return self._registry

    def _delta(self) -> MigrationDeltaJournal:
        return delta_journal_for(self.logical_name, migrations_dir=self._migrations_dir)

    # ── resolution ───────────────────────────────────────────────────────────
    def resolve(self) -> SemanticResolution:
        """Resolve the active physical collection + compatibility vs the runtime.

        Alias hit → that physical name. No alias → the legacy logical name itself
        (which, being unstamped, resolves as incompatible)."""
        health = self._get_runtime().health()
        registry = self._get_registry()
        physical = registry.resolve(self.logical_name) or self.logical_name
        stored_meta: dict = {}
        try:
            client = self._get_client()
            existing = {c.name for c in client.list_collections()}
            if physical in existing:
                col = client.get_collection(physical, embedding_function=None)
                stored_meta = dict(getattr(col, "metadata", None) or {})
            else:
                # No physical collection yet → nothing compatible to serve.
                return SemanticResolution(self.logical_name, None, False, "unavailable", {})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"SEMANTIC_STORE: resolve probe failed: {e}")
            return SemanticResolution(self.logical_name, physical, False, "unknown", {})
        compat = check_compatibility(stored_meta, health)
        return SemanticResolution(
            self.logical_name, physical, compat.status == COMPAT_OK,
            compat.status, stored_meta,
        )

    def _open_active_collection(self, physical: str):
        return self._get_client().get_collection(physical, embedding_function=None)

    # ── read ─────────────────────────────────────────────────────────────────
    def query(self, text: str, n_results: int = 3) -> list[dict]:
        """Explicit-embedding similarity read. Returns [] (never queries) when the
        active collection is incompatible — no implicit embedder, no mixing."""
        if not isinstance(text, str) or not text.strip():
            return []
        res = self.resolve()
        if not res.compatible or not res.physical_name:
            return []
        emb = self._get_runtime().embed_text(text)
        if not emb.ok:
            return []
        try:
            col = self._open_active_collection(res.physical_name)
            count = col.count()
            if count == 0:
                return []
            got = col.query(
                query_embeddings=[emb.vector],
                n_results=min(int(n_results), count),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:  # noqa: BLE001 — degrade to empty, never crash a turn
            logger.debug(f"SEMANTIC_STORE: query failed: {e}")
            return []
        docs = (got.get("documents") or [[]])[0]
        metas = (got.get("metadatas") or [[]])[0]
        dists = (got.get("distances") or [[]])[0]
        out: list[dict] = []
        for i, doc in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            dist = dists[i] if i < len(dists) else 1.0
            out.append({"content": doc, "distance": float(dist), **(meta or {})})
        return out

    # ── write ────────────────────────────────────────────────────────────────
    def write(self, record_id: str, document: str, metadata: dict | None = None) -> str:
        """Explicit-embedding write. Returns a status string:

          * ``ok``               — embedded + written to the active collection
          * ``journaled``        — appended to the migration-delta journal (a
            migration is running, or no compatible active collection exists yet)
          * ``skipped_secret``   — refused (contained a secret; never indexed)
          * ``dropped``          — delta journal at cap / write failed
        """
        from core.memory_router import contains_secret, redact_secrets

        if not isinstance(document, str) or not document.strip():
            return "dropped"
        # Never index secrets (defense-in-depth alongside the fabric policy).
        safe = redact_secrets(document)
        if contains_secret(safe):
            return "skipped_secret"
        meta = dict(metadata or {})

        res = self.resolve()
        delta = self._delta()
        migration_active = delta.exists()

        # No compatible active collection → journal (do not lose the write).
        if not res.compatible or not res.physical_name:
            return "journaled" if delta.append(record_id, safe, meta) else "dropped"

        # Compatible active: embed + write. If a migration is running, ALSO journal
        # so the staged collection stays complete (write-through + delta).
        emb = self._get_runtime().embed_text(safe)
        if not emb.ok:
            return "journaled" if delta.append(record_id, safe, meta) else "dropped"
        try:
            from core.chroma_collections import _clean_metadata
            col = self._open_active_collection(res.physical_name)
            cleaned = _clean_metadata(meta)
            col.upsert(
                ids=[str(record_id)], documents=[safe],
                embeddings=[emb.vector], metadatas=[cleaned or None],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SEMANTIC_STORE: write failed, journaling: {e}")
            return "journaled" if delta.append(record_id, safe, meta) else "dropped"
        if migration_active:
            delta.append(record_id, safe, meta)
        return "ok"

    # ── bootstrap an empty active collection (operator/migration only) ───────
    def ensure_active_stamped(self, *, created_at: str) -> SemanticResolution:
        """Create (if absent) an empty stamped active physical collection and set
        the alias. Used by the migration controller — NOT auto-called at boot."""
        from core.alias_registry import AliasEntry
        from core.chroma_collections import physical_name

        health = self._get_runtime().health()
        if not health.available:
            return self.resolve()
        physical = physical_name(self.logical_name, health.schema_version, health.fingerprint)
        client = self._get_client()
        client.get_or_create_collection(
            name=physical,
            metadata={"hnsw:space": "cosine", **stamp_metadata(health, created_at=created_at)},
            embedding_function=None,
        )
        registry = self._get_registry()
        prev = registry.resolve(self.logical_name)
        registry.set_active(AliasEntry(
            logical_name=self.logical_name, active_physical_collection=physical,
            provider=health.provider, model=health.model, dimension=health.dimension,
            fingerprint=health.fingerprint, embedding_schema_version=health.schema_version,
            activated_at=created_at, migration_id="",
            previous_physical_collection=prev or "",
            rollback_available=bool(prev and prev != physical),
        ))
        return self.resolve()
