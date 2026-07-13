"""core/semantic_migration.py — V69 M53: operator-controlled semantic migration.

Deterministic controller that drives the M52 ReindexEngine + the M53 Chroma
adapter + alias registry + delta journal through the full, operator-gated
lifecycle:

    plan → migrate (build staged, resumable) → validate (replay delta + checks)
         → activate (flip alias, retain old) → rollback (restore old, delete none)

Every decision is code-driven; no model output chooses collection names, physical
aliases, migration ids, active targets, or rollback targets. Effectful steps are
meant to be invoked only from the operator command surface (core.semantic_commands)
and, when wired through ToolExecutor, HITL-gated. Read-only status/plan are safe.

Migration state is persisted (atomic temp+replace) so status survives restart and
an interrupted staged build resumes from the M52 ReindexJournal offset. The legacy
collection is NEVER deleted here.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from core.semantic_memory import (
    _ALIAS_PATH,
    _MIGRATIONS_DIR,
    _VAULT_PATH,
    SemanticStore,
    delta_journal_for,
)
from core.vector_collections import (
    COMPAT_OK,
    ReindexEngine,
    ReindexJournal,
    check_compatibility,
)

# Migration lifecycle phases (distinct from M52 COMPAT_* verdicts, which describe
# a collection's compatibility, not a migration's progress).
PHASE_PLANNED = "PLANNED"
PHASE_MIGRATING = "MIGRATING"
PHASE_STAGED_BUILT = "STAGED_BUILT"
PHASE_VALIDATING = "VALIDATING"
PHASE_READY_TO_ACTIVATE = "READY_TO_ACTIVATE"
PHASE_ACTIVE = "ACTIVE"
PHASE_FAILED = "FAILED"
PHASE_ABORTED = "ABORTED"

# Managed logical semantic collections (code-controlled; never model input).
MANAGED_LOGICAL = ("jarvis_episodic", "knowledge_vault")

_PAGE_SIZE = 128       # bounded source pagination
_BATCH_SIZE = 32       # bounded embedding batch (CPU-only host)
_VALIDATION_SAMPLE = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _PolicyFilteredSource:
    """Wraps a real source with migration-time memory policy.

    * Secrets are redacted in-place (count-preserving) so no credential is ever
      re-embedded or copied into the new collection.
    * Vector-only / empty-document records are UNRECOVERABLE (we will not fabricate
      source text to re-embed) — they are excluded from the migration and counted,
      so an activation never hides unexplained data loss. The legacy collection is
      preserved, so those records are never lost, only not re-embedded.

    Only recoverable record IDs are held (strings, no documents/vectors), so the
    whole collection is never loaded into RAM. Documents are fetched per page.
    """

    def __init__(self, inner, *, page_size: int = _PAGE_SIZE) -> None:
        self._inner = inner
        self.name = inner.name
        self._page_size = page_size
        self._recoverable_ids: list[str] = []
        self._unrecoverable = 0
        self._scan()

    def _scan(self) -> None:
        total = self._inner.count()
        offset = 0
        while offset < total:
            page = self._inner.get_page(offset, self._page_size)
            if not page:
                break
            for r in page:
                if (r.get("document") or "").strip():
                    self._recoverable_ids.append(str(r["id"]))
                else:
                    self._unrecoverable += 1
            offset += len(page)

    def count(self) -> int:
        return len(self._recoverable_ids)

    def unrecoverable_count(self) -> int:
        return self._unrecoverable

    def get_metadata(self) -> dict:
        return self._inner.get_metadata()

    def get_page(self, offset: int, limit: int) -> list[dict]:
        from core.memory_router import redact_secrets

        ids = self._recoverable_ids[offset : offset + max(1, int(limit))]
        if not ids:
            return []
        by_id = {r["id"]: r for r in self._fetch_ids(ids)}
        out: list[dict] = []
        for rid in ids:                       # preserve stable recoverable order
            r = by_id.get(rid)
            if r is None:
                continue
            out.append({
                "id": rid,
                "document": redact_secrets(r.get("document", "")),
                "metadata": dict(r.get("metadata", {}) or {}),
            })
        return out

    def _fetch_ids(self, ids: list[str]) -> list[dict]:
        # ChromaSourceCollection has no by-id fetch; use its inner collection.
        col = getattr(self._inner, "_col", None)
        if col is not None:
            res = col.get(ids=ids, include=["documents", "metadatas"])
            got = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            return [
                {"id": str(got[i]),
                 "document": docs[i] if i < len(docs) else "",
                 "metadata": metas[i] if i < len(metas) else {}}
                for i in range(len(got))
            ]
        # Fallback for fakes: linear scan of all pages.
        out: list[dict] = []
        want = set(ids)
        offset, total = 0, self._inner.count()
        while offset < total and want:
            page = self._inner.get_page(offset, self._page_size)
            if not page:
                break
            for r in page:
                if str(r["id"]) in want:
                    out.append(r)
                    want.discard(str(r["id"]))
            offset += len(page)
        return out


@dataclass
class MigrationState:
    migration_id: str
    logical_name: str
    source_physical: str
    staged_physical: str
    phase: str
    source_count: int
    fingerprint: str
    dimension: int
    created_at: str
    validated: bool = False
    notes: str = ""
    path: Path | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("path", None)
        return d

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"SEMANTIC_MIGRATION: state save failed: {e}")

    @classmethod
    def load(cls, path: Path) -> "MigrationState | None":
        try:
            if not path.exists():
                return None
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls(path=path, **{k: d[k] for k in (
                "migration_id", "logical_name", "source_physical", "staged_physical",
                "phase", "source_count", "fingerprint", "dimension", "created_at",
                "validated", "notes",
            ) if k in d})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"SEMANTIC_MIGRATION: state load failed: {e}")
            return None


@dataclass
class MigrationResult:
    status: str
    phase: str = ""
    migration_id: str = ""
    message: str = ""
    detail: dict = field(default_factory=dict)


class SemanticMigrationController:
    """Deterministic, operator-gated migration orchestration for one host."""

    def __init__(
        self,
        *,
        client=None,
        runtime=None,
        registry=None,
        vault_path: Path | None = None,
        migrations_dir: Path | None = None,
        clock=_now,
    ) -> None:
        self._client = client
        self._runtime = runtime
        self._registry = registry
        self._vault_path = vault_path or _VAULT_PATH
        self._migrations_dir = migrations_dir or _MIGRATIONS_DIR
        self._clock = clock

    # ── lazy wiring ──────────────────────────────────────────────────────────
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
            self._registry = AliasRegistry(self._vault_path / _ALIAS_PATH.name)
        return self._registry

    def _factory(self):
        from core.chroma_collections import ChromaCollectionFactory
        return ChromaCollectionFactory(self._get_client())

    def _store(self, logical: str) -> SemanticStore:
        return SemanticStore(
            logical, client=self._get_client(), runtime=self._get_runtime(),
            registry=self._get_registry(), vault_path=self._vault_path,
            migrations_dir=self._migrations_dir,
        )

    def _state_path(self, logical: str) -> Path:
        return self._migrations_dir / f"{logical}.state.json"

    def _journal_path(self, logical: str) -> Path:
        return self._migrations_dir / f"{logical}.reindex.json"

    def _source_physical(self, logical: str) -> str:
        return self._get_registry().resolve(logical) or logical

    # ── plan (read-only) ─────────────────────────────────────────────────────
    def plan(self, logical: str) -> MigrationResult:
        health = self._get_runtime().health()
        if not health.available:
            return MigrationResult("unavailable", message="Embedding runtime unavailable.")
        from core.chroma_collections import physical_name

        source = self._source_physical(logical)
        factory = self._factory()
        raw = factory.get_source(source)
        if raw is None:
            return MigrationResult(
                "no_source", message=f"No source collection '{source}' to migrate.")
        staged = physical_name(logical, health.schema_version, health.fingerprint)
        if staged == source:
            return MigrationResult(
                "already_current", phase=PHASE_ACTIVE,
                message=f"'{logical}' already active on {staged}.")
        src = _PolicyFilteredSource(raw)
        count = src.count()
        unrecoverable = src.unrecoverable_count()
        batches = (count + _BATCH_SIZE - 1) // _BATCH_SIZE
        compat = check_compatibility(raw.get_metadata(), health)
        risk = "low" if count < 500 else ("medium" if count < 5000 else "high")
        delta = delta_journal_for(logical, migrations_dir=self._migrations_dir)
        return MigrationResult(
            "ok", phase=PHASE_PLANNED,
            message=f"Plan: migrate {count} recoverable records from {source} → {staged}.",
            detail={
                "logical": logical, "source_physical": source, "staged_physical": staged,
                "record_count": count, "unrecoverable_records": unrecoverable,
                "batch_size": _BATCH_SIZE, "batches": batches,
                "page_size": _PAGE_SIZE, "fingerprint": health.fingerprint,
                "dimension": health.dimension, "source_compat": compat.status,
                "pending_delta_writes": delta.count(), "risk": risk,
                "rollback": f"alias reverts {logical} → {source} (no deletion)",
            },
        )

    # ── migrate (build staged; resumable) ────────────────────────────────────
    def migrate(self, logical: str, *, dry_run: bool = False, should_cancel=None) -> MigrationResult:
        plan = self.plan(logical)
        if plan.status != "ok":
            return plan
        if dry_run:
            return MigrationResult(
                "dry_run", phase=PHASE_PLANNED, migration_id="",
                message="Dry run — no mutations.", detail=plan.detail)

        health = self._get_runtime().health()
        source = plan.detail["source_physical"]
        staged = plan.detail["staged_physical"]
        migration_id = f"mig_{staged}"
        factory = self._factory()
        raw = factory.get_source(source)
        if raw is None:
            return MigrationResult("no_source", message=f"Source '{source}' vanished.")
        src = _PolicyFilteredSource(raw)

        state = MigrationState(
            migration_id=migration_id, logical_name=logical, source_physical=source,
            staged_physical=staged, phase=PHASE_MIGRATING, source_count=src.count(),
            fingerprint=health.fingerprint, dimension=health.dimension,
            created_at=self._clock(), path=self._state_path(logical),
        )
        state.save()

        engine = ReindexEngine(self._get_runtime(), factory, batch_size=_BATCH_SIZE)
        res = engine.reindex(
            src, created_at=state.created_at,
            journal_path=self._journal_path(logical), should_cancel=should_cancel,
        )
        # ReindexEngine names the staged collection itself; align our record.
        state.staged_physical = res.staged_name or staged
        if res.status != "ok":
            state.phase = PHASE_FAILED if res.status == "failed" else PHASE_ABORTED
            state.notes = res.message
            state.save()
            return MigrationResult(
                res.status, phase=state.phase, migration_id=migration_id,
                message=res.message,
                detail={"reindexed": res.reindexed, "total": res.total,
                        "staged": state.staged_physical, "resumable": True})
        state.phase = PHASE_STAGED_BUILT
        state.save()
        return MigrationResult(
            "staged", phase=PHASE_STAGED_BUILT, migration_id=migration_id,
            message=f"Staged {res.reindexed}/{res.total} records; validate before activation.",
            detail={"staged": state.staged_physical, "reindexed": res.reindexed})

    def resume(self, logical: str, *, should_cancel=None) -> MigrationResult:
        state = MigrationState.load(self._state_path(logical))
        if state is None:
            return MigrationResult("no_migration", message=f"No migration for '{logical}'.")
        if state.phase in (PHASE_ACTIVE,):
            return MigrationResult("already_active", phase=state.phase, migration_id=state.migration_id)
        # Re-run migrate: ReindexEngine resumes from the journal offset.
        return self.migrate(logical, should_cancel=should_cancel)

    def abort(self, logical: str) -> MigrationResult:
        state = MigrationState.load(self._state_path(logical))
        if state is None:
            return MigrationResult("no_migration", message=f"No migration for '{logical}'.")
        state.phase = PHASE_ABORTED
        state.notes = "operator abort"
        state.save()
        # Preserve staged + legacy + delta; only mark aborted.
        return MigrationResult(
            "aborted", phase=PHASE_ABORTED, migration_id=state.migration_id,
            message="Migration aborted; staged and legacy collections preserved.")

    # ── validate (delta replay + bounded checks) ─────────────────────────────
    def validate(self, logical: str) -> MigrationResult:
        state = MigrationState.load(self._state_path(logical))
        if state is None:
            return MigrationResult("no_migration", message=f"No migration for '{logical}'.")
        if state.phase not in (PHASE_STAGED_BUILT, PHASE_VALIDATING, PHASE_READY_TO_ACTIVATE):
            return MigrationResult(
                "not_ready", phase=state.phase, migration_id=state.migration_id,
                message=f"Cannot validate from phase {state.phase}.")
        state.phase = PHASE_VALIDATING
        state.save()

        health = self._get_runtime().health()
        client = self._get_client()
        try:
            staged = client.get_collection(state.staged_physical, embedding_function=None)
        except Exception:  # noqa: BLE001
            state.phase = PHASE_FAILED
            state.notes = "staged collection missing"
            state.save()
            return MigrationResult("failed", phase=PHASE_FAILED, message="Staged collection missing.")

        # 1) Replay the migration-delta into staged (idempotent, dedup by id).
        replayed = self._replay_delta(logical, staged, health)

        # 2) Fingerprint stamp matches the active runtime.
        stored_meta = dict(getattr(staged, "metadata", None) or {})
        compat = check_compatibility(stored_meta, health)
        if compat.status != COMPAT_OK:
            state.phase = PHASE_FAILED
            state.notes = f"staged fingerprint mismatch ({compat.status})"
            state.save()
            return MigrationResult("failed", phase=PHASE_FAILED, message=state.notes)

        # 3) Count: staged >= source count (delta may add new ids).
        staged_count = staged.count()
        if staged_count < state.source_count:
            state.phase = PHASE_FAILED
            state.notes = f"staged count {staged_count} < source {state.source_count}"
            state.save()
            return MigrationResult("failed", phase=PHASE_FAILED, message=state.notes)

        # 4) Bounded dimension sample on real stored vectors.
        try:
            sample = staged.get(limit=_VALIDATION_SAMPLE, include=["embeddings"])
            embs = sample.get("embeddings")
            # Chroma returns embeddings as a numpy array — avoid `x or []` (truthy
            # test on an ndarray is ambiguous); check length explicitly.
            if embs is not None:
                for vec in embs:
                    if len(vec) != health.dimension:
                        state.phase = PHASE_FAILED
                        state.notes = "staged vector dimension drift"
                        state.save()
                        return MigrationResult("failed", phase=PHASE_FAILED, message=state.notes)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"SEMANTIC_MIGRATION: sample check skipped: {e}")

        state.phase = PHASE_READY_TO_ACTIVATE
        state.validated = True
        state.notes = f"validated: staged={staged_count}, delta_replayed={replayed}"
        state.save()
        return MigrationResult(
            "validated", phase=PHASE_READY_TO_ACTIVATE, migration_id=state.migration_id,
            message=f"Validated. staged={staged_count}, delta_replayed={replayed}.",
            detail={"staged_count": staged_count, "delta_replayed": replayed})

    def _replay_delta(self, logical: str, staged, health) -> int:
        delta = delta_journal_for(logical, migrations_dir=self._migrations_dir)
        records = delta.read_deduped()
        if not records:
            return 0
        from core.chroma_collections import _clean_metadata

        runtime = self._get_runtime()
        replayed = 0
        for i in range(0, len(records), _BATCH_SIZE):
            chunk = records[i : i + _BATCH_SIZE]
            docs = [str(r.get("document", "")) for r in chunk]
            batch = runtime.embed_batch(docs)
            if not batch.ok:
                logger.warning(f"SEMANTIC_MIGRATION: delta replay embed failed ({batch.error_class}).")
                break
            staged.upsert(
                ids=[str(r.get("id")) for r in chunk],
                documents=docs,
                embeddings=[list(v) for v in batch.vectors],
                metadatas=[_clean_metadata(r.get("metadata")) or None for r in chunk],
            )
            replayed += len(chunk)
        return replayed

    # ── activate (operator-gated; only after validation) ─────────────────────
    def activate(self, logical: str) -> MigrationResult:
        state = MigrationState.load(self._state_path(logical))
        if state is None:
            return MigrationResult("no_migration", message=f"No migration for '{logical}'.")
        if state.phase != PHASE_READY_TO_ACTIVATE or not state.validated:
            return MigrationResult(
                "not_validated", phase=state.phase, migration_id=state.migration_id,
                message="Activation denied — validation not complete.")
        from core.alias_registry import AliasEntry

        registry = self._get_registry()
        prev = registry.resolve(logical) or state.source_physical
        registry.set_active(AliasEntry(
            logical_name=logical, active_physical_collection=state.staged_physical,
            provider=self._get_runtime().health().provider,
            model=self._get_runtime().health().model,
            dimension=state.dimension, fingerprint=state.fingerprint,
            embedding_schema_version=self._get_runtime().health().schema_version,
            activated_at=self._clock(), migration_id=state.migration_id,
            previous_physical_collection=prev,
            rollback_available=bool(prev and prev != state.staged_physical),
        ))
        # Migration complete: clear delta + reindex journals (state kept ACTIVE).
        delta_journal_for(logical, migrations_dir=self._migrations_dir).clear()
        ReindexJournal(source_name="", staged_name="", fingerprint="", dimension=0,
                       total=0, path=self._journal_path(logical)).clear()
        state.phase = PHASE_ACTIVE
        state.notes = f"activated → {state.staged_physical}; previous {prev} retained"
        state.save()
        logger.info(f"SEMANTIC_MIGRATION: {logical} active → {state.staged_physical} "
                    f"(previous {prev} retained for rollback)")
        return MigrationResult(
            "activated", phase=PHASE_ACTIVE, migration_id=state.migration_id,
            message=f"{logical} now active on {state.staged_physical}; {prev} retained.",
            detail={"active": state.staged_physical, "previous": prev})

    def rollback(self, logical: str) -> MigrationResult:
        registry = self._get_registry()
        restored = registry.rollback(logical, activated_at=self._clock())
        if restored is None:
            return MigrationResult("no_rollback", message=f"No rollback target for '{logical}'.")
        return MigrationResult(
            "rolled_back", phase=PHASE_ACTIVE, message=(
                f"{logical} reverted → {restored.active_physical_collection}; "
                f"{restored.previous_physical_collection} retained (nothing deleted)."),
            detail={"active": restored.active_physical_collection,
                    "retained": restored.previous_physical_collection})

    # ── status inventory (read-only) ─────────────────────────────────────────
    def status(self, logical_names=MANAGED_LOGICAL) -> list[dict]:
        health = self._get_runtime().health()
        client = self._get_client()
        try:
            existing = {c.name for c in client.list_collections()}
        except Exception:  # noqa: BLE001
            existing = set()
        registry = self._get_registry()
        out: list[dict] = []
        for logical in logical_names:
            out.append(self._status_one(logical, health, client, existing, registry))
        return out

    def _status_one(self, logical, health, client, existing, registry) -> dict:
        entry = registry.get(logical)
        active_physical = (entry.active_physical_collection if entry else None) or (
            logical if logical in existing else None)
        state = MigrationState.load(self._state_path(logical))
        delta = delta_journal_for(logical, migrations_dir=self._migrations_dir)

        record = {
            "logical_name": logical,
            "active_physical": active_physical,
            "previous_physical": entry.previous_physical_collection if entry else "",
            "provider": health.provider, "model": health.model,
            "dimension": None, "fingerprint": None, "embedding_schema_version": None,
            "record_count": None, "compat_state": "unknown", "migration_phase": None,
            "rollback_available": bool(entry and entry.rollback_available),
            "pending_delta_writes": delta.count(),
            "warnings": [],
        }
        if not health.available:
            record["compat_state"] = "unavailable"
            record["warnings"].append("embedding runtime unavailable")
            return record
        if active_physical is None or active_physical not in existing:
            record["compat_state"] = "unavailable"
            record["warnings"].append("no active physical collection")
        else:
            try:
                col = client.get_collection(active_physical, embedding_function=None)
                meta = dict(getattr(col, "metadata", None) or {})
                compat = check_compatibility(meta, health)
                record["compat_state"] = compat.status
                record["record_count"] = col.count()
                record["dimension"] = meta.get("embedding_dimension")
                record["fingerprint"] = meta.get("embedding_fingerprint")
                record["embedding_schema_version"] = meta.get("embedding_schema_version")
                if compat.status != COMPAT_OK:
                    record["warnings"].append(compat.reason)
            except Exception as e:  # noqa: BLE001
                record["compat_state"] = "unknown"
                record["warnings"].append(f"status probe failed: {str(e)[:80]}")
        if state is not None:
            record["migration_phase"] = state.phase
            record["staged_physical"] = state.staged_physical
        return record


def get_controller() -> SemanticMigrationController:
    """Production controller (lazy singleton wiring)."""
    return SemanticMigrationController()
