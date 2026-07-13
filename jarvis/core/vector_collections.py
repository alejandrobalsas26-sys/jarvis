"""core/vector_collections.py — V69 M52: vector-collection compatibility & reindex.

A vector collection is only queryable/appendable with vectors produced by the
SAME embedding runtime that built it. Mixing a 768-dim nomic-embed-text vector
into a 384-dim all-MiniLM collection silently corrupts retrieval. This module is
the guard: every collection records the active embedding identity, and every
read/write first verifies the collection's stamp matches the runtime.

On mismatch we NEVER query incompatible vectors, NEVER append incompatible
vectors, and NEVER delete the user's data — we report ``REINDEX_REQUIRED`` and
offer an atomic, resumable migration:

    old collection
      -> create a new versioned collection stamped with the active fingerprint
      -> re-embed in bounded batches (progress journaled → interruptible/resumable)
      -> validate counts + dimensions
      -> activate the new collection
      -> retain the old collection for rollback

The engine operates over small Protocols (:class:`SourceCollection`,
:class:`CollectionFactory`) so it is fully testable with in-memory fakes — no
ChromaDB required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from loguru import logger

from core.embedding_runtime import EmbeddingHealth, EmbeddingRuntime

# Metadata keys stamped on every managed collection.
META_PROVIDER = "embedding_provider"
META_MODEL = "embedding_model"
META_DIMENSION = "embedding_dimension"
META_FINGERPRINT = "embedding_fingerprint"
META_SCHEMA_VERSION = "embedding_schema_version"
META_CREATED_AT = "created_at"

# Compatibility verdicts.
COMPAT_OK = "ok"
COMPAT_REINDEX_REQUIRED = "reindex_required"
COMPAT_UNSTAMPED = "unstamped"          # legacy collection with no embedding stamp
COMPAT_UNKNOWN = "unknown"              # runtime health unavailable → cannot decide


@dataclass(frozen=True)
class CompatibilityResult:
    status: str
    reason: str
    stored_fingerprint: str | None = None
    active_fingerprint: str | None = None
    stored_dimension: int | None = None
    active_dimension: int | None = None

    @property
    def compatible(self) -> bool:
        return self.status == COMPAT_OK


def stamp_metadata(health: EmbeddingHealth, *, created_at: str) -> dict:
    """Build the six-key embedding stamp for a new collection.

    ``created_at`` is passed in (never generated here) so callers control the
    clock — the workflow stays deterministic and time-source agnostic.
    """
    return {
        META_PROVIDER: health.provider,
        META_MODEL: health.model,
        META_DIMENSION: int(health.dimension),
        META_FINGERPRINT: health.fingerprint,
        META_SCHEMA_VERSION: int(health.schema_version),
        META_CREATED_AT: created_at,
    }


def check_compatibility(stored_meta: dict | None, health: EmbeddingHealth) -> CompatibilityResult:
    """Decide whether a collection stamped with ``stored_meta`` can serve the
    active runtime described by ``health``. Fail-closed on ambiguity."""
    if not health.available:
        return CompatibilityResult(
            COMPAT_UNKNOWN, "Active embedding runtime is unavailable — cannot verify compatibility.",
            active_fingerprint=health.fingerprint or None,
        )
    stored_meta = stored_meta or {}
    stored_fp = stored_meta.get(META_FINGERPRINT)
    stored_dim = stored_meta.get(META_DIMENSION)
    if not stored_fp:
        return CompatibilityResult(
            COMPAT_UNSTAMPED,
            "Collection has no embedding stamp (created before the unified runtime).",
            active_fingerprint=health.fingerprint, active_dimension=health.dimension,
        )
    if stored_fp == health.fingerprint:
        # Fingerprint match is authoritative; dimension is a defensive cross-check.
        if stored_dim not in (None, 0) and int(stored_dim) != int(health.dimension):
            return CompatibilityResult(
                COMPAT_REINDEX_REQUIRED,
                "Fingerprint matches but stored dimension differs — collection is inconsistent.",
                stored_fingerprint=stored_fp, active_fingerprint=health.fingerprint,
                stored_dimension=stored_dim, active_dimension=health.dimension,
            )
        return CompatibilityResult(
            COMPAT_OK, "Collection matches the active embedding runtime.",
            stored_fingerprint=stored_fp, active_fingerprint=health.fingerprint,
            stored_dimension=stored_dim, active_dimension=health.dimension,
        )
    return CompatibilityResult(
        COMPAT_REINDEX_REQUIRED,
        "Collection was built by a different embedding model/provider — reindex required.",
        stored_fingerprint=stored_fp, active_fingerprint=health.fingerprint,
        stored_dimension=stored_dim, active_dimension=health.dimension,
    )


# ── Migration protocols (Chroma-agnostic; testable with fakes) ────────────────
@runtime_checkable
class SourceCollection(Protocol):
    name: str

    def count(self) -> int: ...
    def get_metadata(self) -> dict: ...
    def get_page(self, offset: int, limit: int) -> list[dict]:
        """Return up to ``limit`` records at ``offset`` as
        ``{"id": str, "document": str, "metadata": dict}`` in a STABLE order."""
        ...


@runtime_checkable
class TargetCollection(Protocol):
    name: str

    def count(self) -> int: ...
    def upsert(self, ids: list[str], documents: list[str],
               embeddings: list[list[float]], metadatas: list[dict]) -> None: ...


@runtime_checkable
class CollectionFactory(Protocol):
    def create(self, name: str, metadata: dict) -> TargetCollection: ...
    def activate(self, staged_name: str, active_name: str) -> None:
        """Atomically make ``staged_name`` the collection served under
        ``active_name``, retaining the old one for rollback."""
        ...
    def exists(self, name: str) -> bool: ...


@dataclass
class ReindexJournal:
    """Durable, resumable progress for one reindex job.

    Persisted as JSON next to the vector store so an interrupted migration
    (power loss, crash, cancellation) resumes from the last committed offset
    instead of re-embedding from zero or corrupting the target.
    """

    source_name: str
    staged_name: str
    fingerprint: str
    dimension: int
    total: int
    offset: int = 0                 # records already committed to the target
    activated: bool = False
    path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name, "staged_name": self.staged_name,
            "fingerprint": self.fingerprint, "dimension": self.dimension,
            "total": self.total, "offset": self.offset, "activated": self.activated,
        }

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — journal is best-effort durability
            logger.debug(f"VECTOR_COLLECTIONS: journal save failed: {e}")

    @classmethod
    def load(cls, path: Path) -> "ReindexJournal | None":
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(path=path, **{k: data[k] for k in (
                "source_name", "staged_name", "fingerprint", "dimension",
                "total", "offset", "activated",
            )})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"VECTOR_COLLECTIONS: journal load failed: {e}")
            return None

    def clear(self) -> None:
        try:
            if self.path and self.path.exists():
                self.path.unlink()
        except Exception:
            pass


@dataclass
class ReindexResult:
    status: str                     # ok | failed | already_current | validation_failed
    reindexed: int = 0
    total: int = 0
    staged_name: str = ""
    activated: bool = False
    rollback_available: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


class ReindexEngine:
    """Atomic, resumable re-embedding of a source collection.

    The active collection is left untouched until the staged one is fully built
    and validated, so an interruption never leaves the served data corrupt and a
    completed migration retains the old collection for rollback.
    """

    def __init__(
        self, runtime: EmbeddingRuntime, factory: CollectionFactory,
        *, batch_size: int = 32,
    ) -> None:
        self._runtime = runtime
        self._factory = factory
        self._batch_size = max(1, int(batch_size))

    def reindex(
        self,
        source: SourceCollection,
        *,
        created_at: str,
        journal_path: Path | None = None,
        should_cancel=None,
    ) -> ReindexResult:
        health = self._runtime.health()
        if not health.available:
            return ReindexResult("failed", message="Embedding runtime unavailable — cannot reindex.")

        total = source.count()
        staged_name = f"{source.name}__v{health.schema_version}_{health.fingerprint}"

        # Resume an interrupted job iff it targets the SAME staged collection.
        journal = ReindexJournal.load(journal_path) if journal_path else None
        resuming = bool(
            journal and journal.staged_name == staged_name
            and journal.fingerprint == health.fingerprint and not journal.activated
        )
        if not resuming:
            journal = ReindexJournal(
                source_name=source.name, staged_name=staged_name,
                fingerprint=health.fingerprint, dimension=health.dimension,
                total=total, offset=0, path=journal_path,
            )
            journal.save()

        target = self._factory.create(staged_name, stamp_metadata(health, created_at=created_at))
        errors: list[str] = []
        offset = journal.offset

        while offset < total:
            if should_cancel and should_cancel():
                journal.save()
                return ReindexResult(
                    "failed", reindexed=offset, total=total, staged_name=staged_name,
                    message="Reindex cancelled — resumable from journal.",
                    rollback_available=True,
                )
            page = source.get_page(offset, self._batch_size)
            if not page:
                break
            docs = [str(r.get("document", "")) for r in page]
            ids = [str(r.get("id", f"{staged_name}:{offset + i}")) for i, r in enumerate(page)]
            metas = [dict(r.get("metadata", {}) or {}) for r in page]

            batch = self._runtime.embed_batch(docs, should_cancel=should_cancel)
            if not batch.ok:
                journal.save()
                return ReindexResult(
                    "failed", reindexed=offset, total=total, staged_name=staged_name,
                    message=f"Embedding failed mid-reindex ({batch.error_class}).",
                    rollback_available=True, errors=[batch.message or "embedding error"],
                )
            if any(len(v) != health.dimension for v in batch.vectors):
                journal.save()
                return ReindexResult(
                    "validation_failed", reindexed=offset, total=total,
                    staged_name=staged_name, rollback_available=True,
                    message="Dimension drift during reindex — aborted before activation.",
                )
            try:
                target.upsert(ids, docs, batch.vectors, metas)
            except Exception as e:  # noqa: BLE001
                journal.save()
                return ReindexResult(
                    "failed", reindexed=offset, total=total, staged_name=staged_name,
                    rollback_available=True, message="Target write failed during reindex.",
                    errors=[str(e)[:200]],
                )
            offset += len(page)
            journal.offset = offset
            journal.save()

        # Validate counts before activation — never activate a short write.
        staged_count = target.count()
        if staged_count != total:
            return ReindexResult(
                "validation_failed", reindexed=offset, total=total, staged_name=staged_name,
                rollback_available=True,
                message=f"Staged count {staged_count} != source count {total} — not activated.",
                errors=errors,
            )

        try:
            self._factory.activate(staged_name, source.name)
        except Exception as e:  # noqa: BLE001
            return ReindexResult(
                "failed", reindexed=offset, total=total, staged_name=staged_name,
                rollback_available=True, message="Activation failed — old collection retained.",
                errors=[str(e)[:200]],
            )
        journal.activated = True
        journal.save()
        journal.clear()
        logger.info(
            f"VECTOR_COLLECTIONS: reindexed '{source.name}' → {staged_name} "
            f"({offset}/{total}), activated; old collection retained for rollback."
        )
        return ReindexResult(
            "ok", reindexed=offset, total=total, staged_name=staged_name,
            activated=True, rollback_available=True, message="Reindex complete and activated.",
        )
