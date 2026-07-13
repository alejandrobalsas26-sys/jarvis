# JARVIS V69 — M53: Live Semantic Collection Migration & Cross-Restart Continuity

Branch: `jarvis-v69-m53-semantic-continuity` (off master `c315ad4`).
Status: complete (M53 only). M54 not started.

## Problem M53 closes

M52 unified embeddings behind one runtime but the `jarvis_episodic` collection
still used Chroma's **implicit/default embedder** (`col.add(documents=…)` /
`col.query(query_texts=…)`) — no explicit configured-role vectors, no durable
active-collection alias, no operator-controlled migration. M53 connects the M52
foundations to the live episodic store and adds restart-survivable collection
identity.

## Semantic collection inventory (found)

| Logical | Physical (before) | Owner | Provider/model (before) | Dim | Migrated in M53 |
|---|---|---|---|---|---|
| `jarvis_episodic` | `jarvis_episodic` | episodic_memory (+consolidator/relevance_graph readers) | Chroma **implicit** ONNX all-MiniLM | 384 | **Yes** — explicit runtime vectors + alias + operator migration |
| `knowledge_vault` | `knowledge_vault` | KnowledgeVault | unified runtime (M52) | 768 | stamped already (M52); managed/reported |
| `jarvis_knowledge` | `jarvis_knowledge` | VectorMemory | unified runtime (M52) | 768 | stamped already (M52) |

Only `jarvis_episodic`'s write (`_write_episode`) and read (`_query_episodes`)
used the implicit embedder; `memory_consolidator` / `relevance_graph` only read
metadata + delete (now alias-resolved).

## Design

**Chroma adapter (`core/chroma_collections.py`)** — production Source/Target/Factory
for the M52 `ReindexEngine`. Bounded `get(limit, offset)` pagination; explicit
embeddings only (collections created with `embedding_function=None`); deterministic
`<logical>__v<schema>__<fp>` names; collision detection; safe missing-collection.
Verified against chromadb 1.5.9. No atomic rename → alias registry owns the flip.

**Alias registry (`core/alias_registry.py`)** — durable logical→physical map. Atomic
temp+`os.replace`, `.bak` before each mutation, malformed file quarantined to
`.corrupt` with `.bak` recovery, schema-versioned (future schema refused), rollback
retains the newer collection. Identity metadata only, never secrets.

**Semantic store (`core/semantic_memory.py`)** — runtime read/write resolver.
Explicit query/document embeddings against the alias-resolved active collection;
NEVER queries an incompatible collection; writes journaled to a bounded
`MigrationDeltaJournal` (dedup-on-replay) when no compatible active collection
exists or a migration is running — no write loss. Secret-redacting.

**Migration controller (`core/semantic_migration.py`)** — deterministic, operator
-gated `plan → migrate(dry-run) → resume → abort → validate → activate → rollback`
+ read-only `status` inventory + metadata-only `boot_summary`. Reuses the M52
`ReindexEngine`; `_PolicyFilteredSource` redacts secrets and excludes unrecoverable
vector-only records (reported, never fabricated). Validation replays the delta and
checks fingerprint/count/dimension. Activation flips the durable alias and retains
the old collection; rollback restores without deleting either.

**Commands (`core/semantic_commands.py`)** — deterministic `semantic-*` parser with
an allowlisted logical argument; effectful vs read-only classification.

## Migration-time write policy (chosen)

Writes go through `SemanticStore.write`:
* compatible active collection → explicit vector upsert; if a migration is running,
  ALSO append to the delta journal (write-through + delta).
* no compatible active collection (pre-migration legacy) → append to the delta
  journal only. Reads return empty (never query the incompatible legacy).

The delta is replayed into the staged collection at `validate`, before activation.
Bounded (`_DELTA_MAX_LINES`), idempotent, dedup by record id. No permanent
dual-write, no unbounded in-memory queue.

## Startup / shutdown

* **Boot** (`boot_summary`): metadata-only. Confirms the active physical collection
  exists, reads its stamp + count, compares stored vs ACTIVE fingerprint (computed
  WITHOUT embedding — no probe embed, no DEEP model), surfaces interrupted migration
  journals. Never scans records, never auto-resumes, degrades honestly
  (DEGRADED/UNAVAILABLE/NONE). Logged in `main.py` beside the V68.1 BootState.
* **Shutdown** (`semantic_shutdown_checkpoint`): bounded, fast; state/journals are
  persisted eagerly on every transition, so an interrupted operator migration is
  resumable and never orphaned. Registered with the V68.1 graceful-shutdown manager.

## Invariants held

No mixing across provider/model/dimension/fingerprint/schema; never silent migrate;
never silent delete/overwrite of a legacy collection; staged never served by normal
reads; never activate before validation; never claim continuity on failure; no
auto-resume of heavy migration at boot; secrets/blocked records never indexed; no
model-generated value chooses paths/names/aliases/targets/migration-ids/rollback.

## Live smoke result (real Ollama nomic-embed-text, temporary artifacts)

nomic responds; dim 768; fp `395d63bbee28d585`; 12 records paginated; staged
receives explicit 768-dim vectors; staged count == source; validation passes;
activation flips only the temporary alias; old temporary collection retained;
rollback restores the old alias. ~3.7 s to migrate 12 records (~306 ms/record, CPU).
Production `jarvis_episodic` untouched.

## Remaining limitations / M54

* The initial legacy→stamped migration cannot recover vector-only records (no source
  text) — they are reported and preserved, never fabricated.
* During the first (legacy-source) migration, episodic reads are empty until
  activation (never query the incompatible legacy). Documented, invariant-preserving.
* M54: wire the `semantic-*` commands into the live operator/voice command surface
  and ToolExecutor HITL gating; optional AURA panel for `status`; consider a
  background, cancellation-cooperative migration worker (currently synchronous).
