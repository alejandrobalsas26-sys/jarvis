"""core/alias_registry.py — V69 M53: durable logical→physical collection alias.

Chroma has no version-independent atomic collection rename, and even where
``collection.modify(name=…)`` exists it is neither guaranteed atomic nor a safe
rollback primitive. So a managed semantic collection is addressed by a **logical
name** (``jarvis_episodic``) that this registry maps to the **active physical
collection** (``jarvis_episodic__v1__395d63bb``). Migration builds a new physical
collection and, only after validation + operator approval, flips this alias — the
previous physical collection is retained for rollback and never deleted here.

Durability rules (single-host Windows, no DB server):
  * atomic write via temp-file + ``os.replace`` (same-dir rename is atomic on NTFS)
  * a ``.bak`` copy is written before every mutation (recover from a torn write)
  * a malformed registry is quarantined to ``.corrupt`` and, if a good ``.bak``
    exists, recovered from it; otherwise we start empty (never crash the runtime)
  * schema-versioned; a future/unknown schema is refused, not silently coerced
  * bounded — one entry per managed logical collection (a handful), no growth path
  * contains only collection identity metadata; NEVER secrets

Every field written here comes from deterministic code / operator commands — never
from model-generated content (collection names, physical aliases, migration ids,
and rollback targets are all code-chosen).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

REGISTRY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AliasEntry:
    """One logical→physical mapping with full embedding identity + rollback state."""

    logical_name: str
    active_physical_collection: str
    provider: str
    model: str
    dimension: int
    fingerprint: str
    embedding_schema_version: int
    activated_at: str
    migration_id: str = ""
    previous_physical_collection: str = ""
    rollback_available: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AliasEntry":
        # Tolerate extra/missing keys defensively — only known fields are read.
        return cls(
            logical_name=str(d["logical_name"]),
            active_physical_collection=str(d["active_physical_collection"]),
            provider=str(d.get("provider", "")),
            model=str(d.get("model", "")),
            dimension=int(d.get("dimension", 0) or 0),
            fingerprint=str(d.get("fingerprint", "")),
            embedding_schema_version=int(d.get("embedding_schema_version", 0) or 0),
            activated_at=str(d.get("activated_at", "")),
            migration_id=str(d.get("migration_id", "")),
            previous_physical_collection=str(d.get("previous_physical_collection", "")),
            rollback_available=bool(d.get("rollback_available", False)),
        )


class AliasRegistry:
    """Durable, atomically-written alias store. Load is resilient; writes are safe."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, AliasEntry] = {}
        self._schema_version = REGISTRY_SCHEMA_VERSION
        self.load()

    # ── load (resilient) ─────────────────────────────────────────────────────
    def load(self) -> None:
        self._entries = {}
        data = self._read_file(self._path)
        if data is None and self._bak_path.exists():
            logger.warning("ALIAS_REGISTRY: primary unreadable — recovering from .bak")
            data = self._read_file(self._bak_path)
            if data is not None:
                # Promote the good backup back to primary.
                self._atomic_write(self._path, data)
        if data is None:
            return
        schema = int(data.get("registry_schema_version", 0) or 0)
        if schema > REGISTRY_SCHEMA_VERSION:
            logger.error(
                f"ALIAS_REGISTRY: schema v{schema} newer than supported "
                f"v{REGISTRY_SCHEMA_VERSION} — refusing to load (fail-closed)."
            )
            return
        self._schema_version = schema or REGISTRY_SCHEMA_VERSION
        for logical, raw in (data.get("aliases") or {}).items():
            try:
                self._entries[str(logical)] = AliasEntry.from_dict({**raw, "logical_name": logical})
            except Exception as e:  # noqa: BLE001 — skip one bad entry, keep the rest
                logger.warning(f"ALIAS_REGISTRY: dropping malformed entry {logical!r}: {e}")

    def _read_file(self, path: Path) -> dict | None:
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("registry root is not an object")
            return data
        except Exception as e:  # noqa: BLE001
            logger.error(f"ALIAS_REGISTRY: {path.name} is malformed ({e}); quarantining.")
            self._quarantine(path)
            return None

    def _quarantine(self, path: Path) -> None:
        try:
            corrupt = path.with_suffix(path.suffix + ".corrupt")
            if path.exists():
                os.replace(path, corrupt)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"ALIAS_REGISTRY: quarantine failed: {e}")

    # ── query ────────────────────────────────────────────────────────────────
    def get(self, logical_name: str) -> AliasEntry | None:
        return self._entries.get(logical_name)

    def resolve(self, logical_name: str) -> str | None:
        """Logical → active physical collection name (the normal-runtime lookup)."""
        e = self._entries.get(logical_name)
        return e.active_physical_collection if e else None

    def all(self) -> dict[str, AliasEntry]:
        return dict(self._entries)

    # ── mutate (atomic + backed up) ──────────────────────────────────────────
    def set_active(self, entry: AliasEntry) -> None:
        """Insert/replace a logical alias, persisting atomically."""
        self._entries[entry.logical_name] = entry
        self._persist()

    def rollback(self, logical_name: str, *, activated_at: str) -> AliasEntry | None:
        """Restore the previous physical collection as active WITHOUT deleting the
        newer one (it becomes the new ``previous`` so a re-rollback is possible).
        Returns the restored entry, or None if there is nothing to roll back to."""
        e = self._entries.get(logical_name)
        if e is None or not e.rollback_available or not e.previous_physical_collection:
            return None
        restored = AliasEntry(
            logical_name=e.logical_name,
            active_physical_collection=e.previous_physical_collection,
            provider=e.provider, model=e.model, dimension=e.dimension,
            fingerprint=e.fingerprint, embedding_schema_version=e.embedding_schema_version,
            activated_at=activated_at, migration_id=e.migration_id,
            previous_physical_collection=e.active_physical_collection,
            rollback_available=True,
        )
        self._entries[logical_name] = restored
        self._persist()
        return restored

    # ── persistence helpers ──────────────────────────────────────────────────
    @property
    def _bak_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".bak")

    def _serialize(self) -> dict:
        return {
            "registry_schema_version": self._schema_version,
            "aliases": {name: e.to_dict() for name, e in sorted(self._entries.items())},
        }

    def _persist(self) -> None:
        payload = self._serialize()
        # Back up the current good file first (torn-write recovery source).
        try:
            if self._path.exists():
                self._bak_path.write_text(self._path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — backup is best-effort
            logger.debug(f"ALIAS_REGISTRY: backup before write failed: {e}")
        self._atomic_write(self._path, payload)

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)   # atomic same-directory rename
