# JARVIS V69 — M52: Unified Embedding Runtime

Branch: `jarvis-v69-continuity-unified-intelligence`
Status: complete (M52 only). Do not begin M53.

## 1. Root architectural problem — the embedding split-brain

The configured EMBEDDING role was never used to produce vectors:

- **Role config** (`core/model_router.py`): `EMBEDDING → nomic-embed-text:latest`
  via Ollama (768-dim). Resolved by `resolve_embedding_model()`, but **no code
  ever called an Ollama embeddings endpoint** (confirmed by
  `docs/V67_ARCHITECTURE_MAP.md` and a repo-wide search — no `/api/embed` call
  existed).
- **Actual vectors** were produced two other, duplicated ways:
  - `core/knowledge.py` (Knowledge Vault) imported `sentence-transformers`
    `all-MiniLM-L6-v2` + `torch` directly (384-dim).
  - `core/memory.py` (`VectorMemory`) imported the same stack independently.
  - The `jarvis_episodic` collection used ChromaDB's **default** embedder
    (ONNX all-MiniLM) with `add(documents=…)` / `query(query_texts=…)`.

This duplication caused the live V68.1 fault: torch 2.x `infer_schema` rejecting a
transformers custom-op signature at `import sentence_transformers`, leaking a raw
stack trace out of `query_knowledge`. V68.1 isolated the symptom; **M52 removes the
architectural cause** by giving every semantic consumer one role-safe runtime.

## 2. Existing embedding consumers found

| Consumer | Store / collection | Embedding path (before) | Dimension | Migrated in M52? | Risk |
|---|---|---|---|---|---|
| `core/knowledge.py` KnowledgeVault | Chroma `brain/vector_store` / `knowledge_vault` | `SentenceTransformer(all-MiniLM)` explicit vectors | 384 | **Yes** — routed through unified runtime + fingerprint guard | Medium (existing data → REINDEX_REQUIRED, preserved) |
| `core/memory.py` VectorMemory | Chroma `.chroma_db` / `jarvis_knowledge` | `SentenceTransformer(all-MiniLM)` explicit vectors | 384 | **Yes** — routed through unified runtime + fingerprint guard | Medium (existing data → reindex, preserved) |
| `core/episodic_memory.py` (+ `memory_consolidator`, `relevance_graph`) | Chroma `jarvis_episodic` (via `vault._client`) | Chroma **default** embedder (`query_texts`/`documents`) | 384 (ONNX) | **No — deferred** | Higher (implicit embedder; needs its own reindex) |
| `core/memory_fabric.py` | facade over the above | delegates to adapters | n/a | Inherits (no direct embedding) | Low |
| tools `estudiar_tema` / `consultar_base_conocimiento` | via `VectorMemory` | inherits `VectorMemory` | 384→runtime | Inherits | Medium |
| tools `ingest_docs` / `query_knowledge` | via KnowledgeVault | inherits KnowledgeVault | 384→runtime | Inherits | Medium |

No consumer mixes dimensions within a single collection — each collection carries
its own fingerprint stamp, and inserts/queries are blocked on mismatch.

### Why episodic is deferred (not blindly migrated)

`jarvis_episodic` relies on Chroma's **implicit default embedder**; it never
computed explicit vectors. Routing it through the runtime means computing explicit
vectors for every add/query and reindexing all stored episodes — a real migration
with its own regression surface. Per the directive ("do not force a migration of
every consumer blindly"), it is documented here and left for a dedicated milestone
with its own reindex + coverage. It remains isolated (separate collection, separate
fingerprint) so nothing mixes.

## 3. Unified embedding design (`core/embedding_runtime.py`)

Flow: `semantic consumer → EmbeddingRuntime → configured EMBEDDING role → Ollama
nomic-embed-text → normalized vector`.

Plain-Python boundary (no `torch.Tensor` / numpy / `SentenceTransformer` / Chroma /
HTTP internals ever cross it):

- `embed_text(text) -> EmbeddingResult`
- `embed_batch(texts) -> BatchEmbeddingResult`
- `health() -> EmbeddingHealth`

`EmbeddingResult`: `status, vector: list[float], model, provider, dimension,
fingerprint, latency_ms, normalized, error_class, message`.

Boundary purity is enforced at **one** choke point: the runtime coerces every
provider row to a native `list[float]` (numpy/torch scalars via `__float__`;
strings/bools/None rejected as `malformed_vector`). Vectors are L2-normalized.

Hardware discipline (Ryzen 5 7430U, CPU-only, `OLLAMA_NUM_PARALLEL=1`): bounded
batch size (default 16, hard cap 128), explicit per-call timeout, cooperative
cancellation checked between batch chunks, no unbounded cache (the LRU lives in the
consumer, keyed by the active fingerprint so a provider change can't return a stale
vector).

## 4. Provider policy (no silent switching)

- **Primary**: `OllamaEmbeddingProvider` — configured EMBEDDING role via `/api/embed`.
  Embedding-only models can never leak into chat (chat resolution goes through
  `resolve_inference_model`, which rejects non-chat models — V67 invariant).
- **Fallback**: `SentenceTransformerProvider` (all-MiniLM), used **only** when the
  operator sets `JARVIS_EMBEDDING_FALLBACK=true` (`settings.embedding_fallback_enabled`)
  and the dependency imports cleanly. The fallback carries a **different
  fingerprint/dimension**, so its vectors are never mistaken for primary vectors,
  and the active provider is always reported to the caller. Off by default → no
  torch import unless explicitly requested.

When the primary is down and no fallback is configured, the runtime returns
`unavailable` — honest degradation, exactly like V68.1.

## 5. Fingerprint & compatibility design (`core/vector_collections.py`)

Every managed collection records six keys: `embedding_provider`, `embedding_model`,
`embedding_dimension`, `embedding_fingerprint`, `embedding_schema_version`,
`created_at`. Fingerprint = `sha256(provider | model | norm | schema)[:16]`
(input-independent, stable, computable before the first embed).

Before querying/inserting, `check_compatibility(stored_meta, health)` returns:

- `ok` — fingerprints match (dimension cross-checked).
- `reindex_required` — different model/provider, or fingerprint match with a
  dimension inconsistency.
- `unstamped` — legacy collection with no stamp (fail-closed).
- `unknown` — runtime unavailable, cannot decide (fail-closed).

On any non-`ok` verdict the vault/`VectorMemory` **does not query or append
incompatible vectors, does not delete data**, and reports `REINDEX_REQUIRED`
through the existing structured, internal-free tool envelope. An empty collection
is safely re-stamped to the active runtime (no data to protect).

## 6. Migration / reindex strategy

`ReindexEngine` performs an **atomic, resumable** migration over Chroma-agnostic
`SourceCollection` / `CollectionFactory` protocols:

```
old collection
  → create new versioned collection  (name = "<src>__v<schema>_<fingerprint>")
  → re-embed in bounded batches       (progress written to a ReindexJournal)
  → validate staged count == source count  (never activate a short write)
  → validate every vector dimension == active dimension (abort on drift)
  → activate new collection            (old retained for rollback)
  → clear journal
```

Interruption/resume: `ReindexJournal` persists `{offset, staged_name, fingerprint,
total, activated}` to JSON. On resume, the engine continues from the last committed
offset iff the journal targets the same staged collection and fingerprint —
otherwise it starts fresh. Cancellation mid-run journals progress and reports
`rollback_available`. The active collection is never mutated until the staged one is
built, validated, and activated, so an interruption never leaves served data
corrupt.

## 7. Files changed

- **new** `core/embedding_runtime.py` — the unified runtime + providers.
- **new** `core/vector_collections.py` — fingerprint/compatibility + reindex engine.
- `core/knowledge.py` — KnowledgeVault consumes the runtime; fingerprint guard;
  `reindex_required` state; V68.1 structured errors and degradation preserved.
- `core/memory.py` — VectorMemory consumes the runtime; fingerprint guard.
- `core/config.py` — `embedding_fallback_enabled`, `embedding_timeout_s`,
  `embedding_batch_size` (operator-only).
- `core/llm.py` — `ingest_docs` tool description made provider-neutral.
- `requirements.txt` — sentence-transformers documented as optional fallback.
- **tests** — see §8.

## 8. Tests added

- `tests/test_embedding_runtime_v69.py` — single/batch embedding, configured model
  resolution, embedding-only never chat-safe, stable dimension/fingerprint,
  malformed response, timeout, cancellation, empty input, bounded batch size, no
  tensor object crossing the boundary, no internals leaked, primary unavailable,
  explicit fallback, fallback disabled.
- `tests/test_vector_collections_v69.py` — compatibility (ok/reindex/unstamped/
  unknown, dimension drift), atomic activation, count/dimension validation,
  interrupted migration, migration resume, rollback availability, journal roundtrip.
- `tests/test_knowledge_reliability_v681.py` — updated to the new runtime seam
  (fake injected runtime); all V68.1 honest-degradation guarantees retained.

## 9. Remaining consumers not migrated

- `jarvis_episodic` (episodic_memory / memory_consolidator / relevance_graph) —
  uses Chroma's implicit default embedder; deferred to a dedicated milestone with
  its own reindex. Isolated by collection + fingerprint; nothing mixes.

## 10. Continuation point for M53

- Wire a Chroma-backed `CollectionFactory` and an operator-triggered `reindex()`
  entry on KnowledgeVault/VectorMemory that drives `ReindexEngine` end-to-end on
  the live stores (journal under `brain/vector_store`).
- Then migrate the `jarvis_episodic` collection onto the unified runtime with
  explicit vectors + reindex, and route `memory_fabric` retrieval through it.
