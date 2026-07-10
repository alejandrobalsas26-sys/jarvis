"""core/operational_store.py — V68 M38: durable operational state & recovery.

ONE coherent, local-first durable store for the operational domains that must survive a
restart — enrolled environments, asset observations/relationships/conflicts, incident
cases + timelines, digital-twin expected/desired baselines, drift/situation/verification/
decision history, and collector checkpoints. It does NOT create a per-module store and it
does NOT duplicate raw telemetry — it persists the compact ``to_dict`` projections the
V66/V67 components already produce, plus evidence *references*, never giant raw payloads.

Backend: stdlib ``sqlite3`` on the local NVMe (WAL) — always available, transactional,
durable. A configured fleet Postgres (``db_manager``) is a separate, higher tier; this
module is the field-durable primary. If the store cannot be opened writable it degrades
to an in-memory database and reports ``durable=False`` — it NEVER claims durable
persistence when it is only volatile.

Discipline:
  * schema-versioned with forward migration;
  * idempotent replay + dedup by content hash (re-putting the same payload is a no-op);
  * conflict visibility (a changed payload bumps the row version);
  * corrupted-record isolation on read (a bad row is skipped + counted, never fatal);
  * bounded journals with retention (no unbounded growth in the DB or in RAM).

All methods are synchronous and fast (local, per-row microseconds). Bulk restore runs at
startup, before the event loop is hot; callers may wrap a large restore in
``asyncio.to_thread`` if they must keep the loop free.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

SCHEMA_VERSION = 1
_JARVIS_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _JARVIS_DIR / "data" / "operational_state.db"
_DEFAULT_JOURNAL_RETENTION = 5000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class PutResult:
    entity_id: str
    outcome: str          # "written" | "unchanged"
    version: int

    @property
    def written(self) -> bool:
        return self.outcome == "written"


@dataclass
class ReconcileResult:
    domain: str
    written: int = 0
    unchanged: int = 0
    corrupted: int = 0
    corrupted_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"domain": self.domain, "written": self.written,
                "unchanged": self.unchanged, "corrupted": self.corrupted,
                "corrupted_ids": self.corrupted_ids[:20]}


class OperationalStore:
    """A durable, schema-versioned key/value + journal store for operational state."""

    def __init__(self, path: "str | Path | None" = None) -> None:
        self._durable = True
        self._degraded_reason = ""
        self._corrupt_reads = 0
        raw = Path(path) if path is not None else _DEFAULT_PATH
        try:
            if str(raw) != ":memory:":
                raw.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(raw), check_same_thread=False,
                                       isolation_level=None)   # autocommit
            self._path = str(raw)
            if str(raw) == ":memory:":
                self._durable = False
                self._degraded_reason = "in-memory store (VOLATILE)"
        except Exception as e:  # noqa: BLE001 — a store we cannot open must not crash boot
            logger.warning(f"OPERATIONAL_STORE: cannot open {raw} ({e}); "
                           f"falling back to VOLATILE in-memory store")
            self._db = sqlite3.connect(":memory:", check_same_thread=False,
                                       isolation_level=None)   # autocommit
            self._path = ":memory:"
            self._durable = False
            self._degraded_reason = f"open failed ({str(e)[:60]}) — VOLATILE"
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    # ── schema / migration ────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        cur = self._db.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL").fetchall()   # consume the result row
        except sqlite3.Error:
            pass
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS records (
                domain TEXT NOT NULL, entity_id TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, schema_version INTEGER NOT NULL,
                content_hash TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (domain, entity_id));
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
                content_hash TEXT NOT NULL, payload TEXT NOT NULL, ts TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_journal_domain ON journal(domain, id);
            """
        )
        self._db.commit()
        found = self._meta_get("schema_version")
        if found is None:
            self._meta_set("schema_version", str(SCHEMA_VERSION))
        else:
            self._migrate(int(found), SCHEMA_VERSION)

    def _migrate(self, frm: int, to: int) -> None:
        # Forward-only migration hook. v1 is the baseline; future versions add steps here.
        if frm == to:
            return
        if frm > to:
            logger.warning(f"OPERATIONAL_STORE: store schema v{frm} newer than code v{to}; "
                           f"reading forward-compatibly")
            return
        logger.info(f"OPERATIONAL_STORE: migrating schema v{frm} -> v{to}")
        # (no destructive migrations yet)
        self._meta_set("schema_version", str(to))

    # ── meta ──────────────────────────────────────────────────────────────────
    def _meta_get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self._db.commit()

    def checkpoint(self, name: str, watermark: str) -> None:
        self._meta_set(f"checkpoint:{name}", watermark)

    def get_checkpoint(self, name: str) -> str | None:
        return self._meta_get(f"checkpoint:{name}")

    # ── keyed records (idempotent upsert) ─────────────────────────────────────
    def put(self, domain: str, entity_id: str, payload: dict,
            *, now_iso: str | None = None) -> PutResult:
        """Idempotent upsert. Re-putting an unchanged payload is a no-op; a changed
        payload bumps the row version (conflict visibility)."""
        chash = _hash(payload)
        existing = self._db.execute(
            "SELECT version, content_hash FROM records WHERE domain=? AND entity_id=?",
            (domain, str(entity_id))).fetchone()
        if existing and existing["content_hash"] == chash:
            return PutResult(str(entity_id), "unchanged", existing["version"])
        version = (existing["version"] + 1) if existing else 1
        self._db.execute(
            "INSERT INTO records(domain,entity_id,version,schema_version,content_hash,"
            "payload,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(domain,entity_id) DO UPDATE SET version=excluded.version,"
            "content_hash=excluded.content_hash,payload=excluded.payload,"
            "updated_at=excluded.updated_at",
            (domain, str(entity_id), version, SCHEMA_VERSION, chash,
             json.dumps(payload, ensure_ascii=False, default=str), now_iso or _now_iso()))
        self._db.commit()
        return PutResult(str(entity_id), "written", version)

    def get(self, domain: str, entity_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT payload FROM records WHERE domain=? AND entity_id=?",
            (domain, str(entity_id))).fetchone()
        if not row:
            return None
        return self._safe_load(row["payload"], f"{domain}:{entity_id}")

    def all(self, domain: str) -> list[dict]:
        """Every record in a domain. Corrupted rows are skipped + counted, never fatal."""
        rows = self._db.execute(
            "SELECT entity_id, payload FROM records WHERE domain=? ORDER BY entity_id",
            (domain,)).fetchall()
        out: list[dict] = []
        for r in rows:
            rec = self._safe_load(r["payload"], f"{domain}:{r['entity_id']}")
            if rec is not None:
                out.append(rec)
        return out

    def delete(self, domain: str, entity_id: str) -> None:
        self._db.execute("DELETE FROM records WHERE domain=? AND entity_id=?",
                         (domain, str(entity_id)))
        self._db.commit()

    def count(self, domain: str) -> int:
        row = self._db.execute("SELECT COUNT(*) AS n FROM records WHERE domain=?",
                               (domain,)).fetchone()
        return int(row["n"]) if row else 0

    # ── append-only journal (drift/verification/decision history) ─────────────
    def append(self, domain: str, payload: dict, *, dedup_window: int = 64,
               now_iso: str | None = None) -> bool:
        """Append a history record. Deduplicates against the last ``dedup_window``
        entries in the domain (idempotent replay of the same event is a no-op)."""
        chash = _hash(payload)
        recent = self._db.execute(
            "SELECT content_hash FROM journal WHERE domain=? ORDER BY id DESC LIMIT ?",
            (domain, max(1, dedup_window))).fetchall()
        if any(r["content_hash"] == chash for r in recent):
            return False
        self._db.execute(
            "INSERT INTO journal(domain,content_hash,payload,ts) VALUES(?,?,?,?)",
            (domain, chash, json.dumps(payload, ensure_ascii=False, default=str),
             now_iso or _now_iso()))
        self._db.commit()
        return True

    def history(self, domain: str, *, limit: int = 200) -> list[dict]:
        rows = self._db.execute(
            "SELECT payload FROM journal WHERE domain=? ORDER BY id DESC LIMIT ?",
            (domain, max(1, limit))).fetchall()
        out: list[dict] = []
        for r in rows:
            rec = self._safe_load(r["payload"], f"journal:{domain}")
            if rec is not None:
                out.append(rec)
        return out

    def retention(self, domain: str, max_rows: int) -> int:
        """Bound a journal domain to its most recent ``max_rows`` entries. Returns
        the number of rows pruned."""
        row = self._db.execute("SELECT COUNT(*) AS n FROM journal WHERE domain=?",
                               (domain,)).fetchone()
        n = int(row["n"]) if row else 0
        if n <= max_rows:
            return 0
        self._db.execute(
            "DELETE FROM journal WHERE domain=? AND id NOT IN "
            "(SELECT id FROM journal WHERE domain=? ORDER BY id DESC LIMIT ?)",
            (domain, domain, max_rows))
        self._db.commit()
        return n - max_rows

    # ── reconciliation (bounded, idempotent, corrupted-record isolation) ──────
    def reconcile(self, domain: str, records: "list[tuple[str, dict]]",
                  *, now_iso: str | None = None) -> ReconcileResult:
        """Replay a list of (entity_id, payload) into a domain. Idempotent: unchanged
        payloads are skipped; a record that fails to serialize is isolated + counted,
        never aborting the batch."""
        res = ReconcileResult(domain=domain)
        for entity_id, payload in records:
            try:
                json.dumps(payload)   # STRICT validation — no coercion; quarantine non-JSON
            except Exception:  # noqa: BLE001
                res.corrupted += 1
                res.corrupted_ids.append(str(entity_id))
                logger.debug(f"OPERATIONAL_STORE: quarantined corrupt record "
                             f"{domain}:{entity_id}")
                continue
            put = self.put(domain, entity_id, payload, now_iso=now_iso)
            if put.written:
                res.written += 1
            else:
                res.unchanged += 1
        return res

    # ── health ────────────────────────────────────────────────────────────────
    def health(self) -> dict:
        domains = {}
        try:
            for r in self._db.execute(
                    "SELECT domain, COUNT(*) AS n FROM records GROUP BY domain").fetchall():
                domains[r["domain"]] = int(r["n"])
        except sqlite3.Error:
            pass
        jrow = self._db.execute("SELECT COUNT(*) AS n FROM journal").fetchone()
        return {
            "durable": self._durable,
            "backend": "sqlite",
            "path": self._path,
            "schema_version": SCHEMA_VERSION,
            "degraded_reason": self._degraded_reason,
            "domains": domains,
            "journal_rows": int(jrow["n"]) if jrow else 0,
            "corrupt_reads": self._corrupt_reads,
        }

    @property
    def durable(self) -> bool:
        return self._durable

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    # ── internals ──────────────────────────────────────────────────────────────
    def _safe_load(self, blob: str, ref: str) -> dict | None:
        try:
            return json.loads(blob)
        except Exception:  # noqa: BLE001 — a corrupt row is isolated, never fatal
            self._corrupt_reads += 1
            logger.debug(f"OPERATIONAL_STORE: corrupt record isolated at {ref}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
#  Domain adapters — persist/restore the live singletons via their own to_dict/from_dict
# ══════════════════════════════════════════════════════════════════════════════
_D_ENV = "environments"
_D_ASSET = "assets"
_D_INCIDENT = "incidents"
_D_TWIN_EXPECTED = "twin_expected"
_D_TWIN_DESIRED = "twin_desired"
_J_DRIFT = "drift_history"
_J_VERIFICATION = "verification_history"
_J_DECISION = "decision_history"
_J_SITUATION = "situation_history"


def persist_environments(store: OperationalStore, registry) -> int:
    n = 0
    for entry in registry.all():
        if store.put(_D_ENV, entry.env_id, entry.to_dict()).written:
            n += 1
    return n


def restore_environments(store: OperationalStore, registry) -> int:
    from core.environment_registry import EnvironmentEntry
    restored = 0
    for rec in store.all(_D_ENV):
        try:
            entry = EnvironmentEntry.from_dict(rec)
            registry._envs[entry.env_id] = entry
            restored += 1
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OPERATIONAL_STORE: skip bad env record: {e}")
    return restored


def persist_asset_graph(store: OperationalStore, graph) -> int:
    """Persist the whole evidence-backed graph (assets + relationships) as one
    authoritative record via its own serialization — bounded, dedup by content hash."""
    return 1 if store.put(_D_ASSET, "__graph__", graph.to_dict()).written else 0


def restore_asset_graph(store: OperationalStore, graph) -> int:
    rec = store.get(_D_ASSET, "__graph__")
    if not rec:
        return 0
    try:
        from core.asset_graph import AssetGraph
        restored = AssetGraph.from_dict(rec)
        graph.assets.update(restored.assets)
        graph.relationships.update(restored.relationships)
        return len(restored.assets)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"OPERATIONAL_STORE: asset-graph restore failed: {e}")
        return 0


def persist_incidents(store: OperationalStore, workspace) -> int:
    n = 0
    for case in workspace.cases.values():
        if store.put(_D_INCIDENT, case.incident_id, case.to_dict()).written:
            n += 1
    return n


def restore_incidents(store: OperationalStore, workspace) -> int:
    from core.incident_workspace import IncidentCase
    restored = 0
    for rec in store.all(_D_INCIDENT):
        try:
            case = IncidentCase.from_dict(rec)
            workspace.cases[case.incident_id] = case
            restored += 1
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OPERATIONAL_STORE: skip bad incident record: {e}")
    return restored


def persist_twin_baseline(store: OperationalStore, twin) -> int:
    """Persist the operator/config-owned expected + desired twin facts (NOT the
    volatile observed state, which is re-derived from live telemetry)."""
    n = 0
    for asset, state in getattr(twin, "_expected", {}).items():
        n += 1 if store.put(_D_TWIN_EXPECTED, asset,
                            {"asset": asset, "facts": state.to_dict()}).written else 0
    for asset, state in getattr(twin, "_desired", {}).items():
        n += 1 if store.put(_D_TWIN_DESIRED, asset,
                            {"asset": asset, "facts": state.to_dict()}).written else 0
    return n


def restore_twin_baseline(store: OperationalStore, twin) -> int:
    """Rebuild expected/desired facts via the twin's own setters (no from_dict needed)."""
    from core.digital_twin import FactKind
    restored = 0
    for domain, setter in ((_D_TWIN_EXPECTED, twin.set_expected),
                           (_D_TWIN_DESIRED, twin.set_desired)):
        for rec in store.all(domain):
            asset = rec.get("asset", "")
            for _key, fact in (rec.get("facts", {}) or {}).items():
                try:
                    kind = FactKind(fact.get("kind", "generic"))
                    setter(asset, fact["key"], fact.get("value"), kind=kind,
                           now_iso=fact.get("observed_at"))
                    restored += 1
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"OPERATIONAL_STORE: skip bad twin fact: {e}")
    return restored


def record_drift(store: OperationalStore, snapshot) -> int:
    """Append the current drift findings to bounded drift history (idempotent)."""
    d = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
    written = 0
    for f in d.get("findings", []):
        if store.append(_J_DRIFT, f):
            written += 1
    store.retention(_J_DRIFT, _DEFAULT_JOURNAL_RETENTION)
    return written


def record_verification(store: OperationalStore, subject: str, verified: bool,
                        *, confidence: float = 1.0, note: str = "",
                        now_iso: str | None = None) -> bool:
    ok = store.append(_J_VERIFICATION, {"subject": subject, "verified": bool(verified),
                                        "confidence": float(confidence), "note": note[:200]},
                      now_iso=now_iso)
    store.retention(_J_VERIFICATION, _DEFAULT_JOURNAL_RETENTION)
    return ok


def record_decision(store: OperationalStore, decision: dict, *,
                    now_iso: str | None = None) -> bool:
    ok = store.append(_J_DECISION, decision, now_iso=now_iso)
    store.retention(_J_DECISION, _DEFAULT_JOURNAL_RETENTION)
    return ok


def record_situation(store: OperationalStore, snapshot, *, now_iso: str | None = None) -> bool:
    d = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
    compact = {"taken_at": d.get("taken_at"), "severity": d.get("severity"),
               "summary": d.get("summary"), "recommended": d.get("summary", {})
               .get("recommended_next_step")}
    ok = store.append(_J_SITUATION, compact, now_iso=now_iso)
    store.retention(_J_SITUATION, _DEFAULT_JOURNAL_RETENTION)
    return ok


def checkpoint_all(store: "OperationalStore | None" = None) -> dict:
    """Persist all durable operational domains from the live singletons. Safe to call
    at a checkpoint / before shutdown; each domain is guarded independently."""
    store = store or get_store()
    out: dict = {"durable": store.durable}
    for name, fn in (("environments", _cp_env), ("assets", _cp_assets),
                     ("incidents", _cp_incidents), ("twin", _cp_twin)):
        try:
            out[name] = fn(store)
        except Exception as e:  # noqa: BLE001
            out[name] = f"error: {str(e)[:60]}"
    return out


def restore_all(store: "OperationalStore | None" = None) -> dict:
    """Restore all durable operational domains into the live singletons at startup.
    Critical state (environments, incidents, twin baseline) first; guarded per domain."""
    store = store or get_store()
    out: dict = {"durable": store.durable}
    for name, fn in (("environments", _rs_env), ("incidents", _rs_incidents),
                     ("twin", _rs_twin), ("assets", _rs_assets)):
        try:
            out[name] = fn(store)
        except Exception as e:  # noqa: BLE001
            out[name] = f"error: {str(e)[:60]}"
    return out


def _cp_env(s):
    from core.environment_registry import env_registry
    return persist_environments(s, env_registry)


def _cp_assets(s):
    from core.asset_graph import graph
    return persist_asset_graph(s, graph)


def _cp_incidents(s):
    from core.incident_workspace import workspace
    return persist_incidents(s, workspace)


def _cp_twin(s):
    from core.digital_twin import twin
    return persist_twin_baseline(s, twin)


def _rs_env(s):
    from core.environment_registry import env_registry
    return restore_environments(s, env_registry)


def _rs_assets(s):
    from core.asset_graph import graph
    return restore_asset_graph(s, graph)


def _rs_incidents(s):
    from core.incident_workspace import workspace
    return restore_incidents(s, workspace)


def _rs_twin(s):
    from core.digital_twin import twin
    return restore_twin_baseline(s, twin)


# Module-level singleton (lazy; opens the default local store on first use).
_store: OperationalStore | None = None


def get_store() -> OperationalStore:
    global _store
    if _store is None:
        _store = OperationalStore()
    return _store
