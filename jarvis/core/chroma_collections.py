"""core/chroma_collections.py — V69 M53: production Chroma adapter for ReindexEngine.

Concrete, Chroma-backed implementations of the M52 migration protocols
(``SourceCollection`` / ``TargetCollection`` / ``CollectionFactory`` in
core.vector_collections). The M52 ReindexEngine is reused verbatim — this module
only supplies the real I/O:

  * bounded paginated reads (``collection.get(limit, offset)``) — never loads a
    whole collection into RAM
  * IDs / documents / allowed scalar metadata preserved
  * EXPLICIT embeddings on every write (no implicit Chroma default embedder)
  * deterministic physical collection names (code-chosen, never LLM-chosen)
  * collision detection; safe behavior when a collection is missing

Chroma has no version-safe atomic rename, so ``activate`` here does NOT rename —
it delegates the logical→physical flip to core.alias_registry (the durable alias
is the single source of truth for which physical collection is live). The old
physical collection is never deleted by this adapter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# Metadata values Chroma accepts as scalars; anything else is dropped on migrate.
_ALLOWED_META_TYPES = (str, int, float, bool)

# Deterministic physical-name grammar: "<logical>__v<schema>__<fingerprint>".
_PHYSICAL_RE = re.compile(r"^[a-zA-Z0-9_.-]+__v\d+__[0-9a-f]{6,32}$")
# Chroma collection-name rule (3-512 chars, start/end alphanumeric, [a-zA-Z0-9._-]).
# Enforced here so an unsafe name is refused by our code before reaching Chroma.
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,510}[a-zA-Z0-9]$")


def physical_name(logical_name: str, schema_version: int, fingerprint: str) -> str:
    """Deterministic physical collection name for a (logical, schema, fp) triple.

    Pure function of code-controlled inputs — the LLM never influences it.
    """
    if not _SAFE_NAME_RE.match(logical_name):
        raise ValueError(f"unsafe logical collection name: {logical_name!r}")
    fp = re.sub(r"[^0-9a-f]", "", (fingerprint or "").lower())[:32] or "nofp"
    name = f"{logical_name}__v{int(schema_version)}__{fp}"
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"derived physical name is unsafe: {name!r}")
    return name


def is_managed_physical_name(name: str) -> bool:
    return bool(_PHYSICAL_RE.match(name or ""))


def _clean_metadata(meta: dict | None) -> dict:
    """Keep only Chroma-acceptable scalar metadata; drop None/lists/objects.

    Chroma rejects non-scalar / None metadata values; silently dropping them
    keeps a migration from aborting on one odd legacy row while preserving every
    usable provenance field (source/scope/sensitivity/timestamp/…)."""
    out: dict = {}
    for k, v in (meta or {}).items():
        if isinstance(v, bool) or isinstance(v, (str, int, float)):
            out[str(k)] = v
    return out


@dataclass
class ChromaSourceCollection:
    """Read side of a migration — bounded pagination over a real Chroma collection."""

    name: str
    _col: object

    def count(self) -> int:
        return int(self._col.count())

    def get_metadata(self) -> dict:
        return dict(getattr(self._col, "metadata", None) or {})

    def get_page(self, offset: int, limit: int) -> list[dict]:
        """Return up to ``limit`` records at ``offset`` (documents + metadata + id).

        Embeddings are intentionally NOT fetched — the migration RE-embeds every
        document with the unified runtime, so pulling old vectors would waste RAM
        and risk mixing. A record with no recoverable document is surfaced with an
        empty ``document`` so the caller can classify it as unrecoverable rather
        than fabricate source text.
        """
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        res = self._col.get(
            limit=limit, offset=offset, include=["documents", "metadatas"],
        )
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        page: list[dict] = []
        for i, rid in enumerate(ids):
            doc = docs[i] if i < len(docs) else None
            meta = metas[i] if i < len(metas) else {}
            page.append({
                "id": str(rid),
                "document": doc if isinstance(doc, str) else "",
                "metadata": dict(meta or {}),
            })
        return page


@dataclass
class ChromaTargetCollection:
    """Write side of a migration — explicit embeddings only."""

    name: str
    _col: object

    def count(self) -> int:
        return int(self._col.count())

    def upsert(self, ids, documents, embeddings, metadatas) -> None:
        # EXPLICIT embeddings — the collection was created WITHOUT an embedding
        # function, so Chroma never embeds implicitly here.
        cleaned = [_clean_metadata(m) for m in metadatas]
        self._col.upsert(
            ids=list(ids),
            documents=list(documents),
            embeddings=[list(v) for v in embeddings],
            metadatas=[m if m else None for m in cleaned],
        )


class ChromaCollectionFactory:
    """Creates staged physical collections and reports existence.

    ``activate`` deliberately does not rename — it records the (staged, logical)
    pair for the caller, which flips the durable alias. Physical collections are
    created WITHOUT an embedding function so no implicit embedder can ever run.
    """

    def __init__(self, client) -> None:
        self._client = client
        self.activations: list[tuple[str, str]] = []

    def exists(self, name: str) -> bool:
        try:
            existing = {c.name for c in self._client.list_collections()}
            return name in existing
        except Exception as e:  # noqa: BLE001
            logger.debug(f"CHROMA_FACTORY: exists() probe failed: {e}")
            return False

    def create(self, name: str, metadata: dict) -> ChromaTargetCollection:
        if not _SAFE_NAME_RE.match(name):
            raise ValueError(f"refusing unsafe collection name: {name!r}")
        # embedding_function=None → Chroma will NOT embed implicitly; all writes
        # must carry explicit vectors (enforced by ChromaTargetCollection.upsert).
        col = self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine", **(metadata or {})},
            embedding_function=None,
        )
        return ChromaTargetCollection(name=name, _col=col)

    def activate(self, staged_name: str, logical_name: str) -> None:
        # Alias flip is performed by the migration controller against the durable
        # AliasRegistry; here we only record intent so the engine's contract holds.
        self.activations.append((staged_name, logical_name))

    # ── helpers used by the migration controller / status inventory ──────────
    def get_source(self, physical_name: str) -> ChromaSourceCollection | None:
        """Open an existing physical collection for reading, or None if missing."""
        try:
            existing = {c.name for c in self._client.list_collections()}
            if physical_name not in existing:
                return None
            col = self._client.get_collection(physical_name, embedding_function=None)
            return ChromaSourceCollection(name=physical_name, _col=col)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"CHROMA_FACTORY: get_source({physical_name}) failed: {e}")
            return None


def build_client(vault_path: str | Path):
    """Construct a PersistentClient at ``vault_path`` (deterministic path)."""
    import chromadb

    p = Path(vault_path)
    p.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(p))
