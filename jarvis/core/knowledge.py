"""
core/knowledge.py — Knowledge Vault: RAG local con ChromaDB.

Indexa PDFs y TXTs de jarvis/brain/docs/ en un vector store persistente localizado
en jarvis/brain/vector_store/.

V69 M52: embeddings ya NO se calculan aquí con sentence-transformers directamente.
El vault consume el runtime de embeddings unificado (core.embedding_runtime), que
resuelve el rol EMBEDDING configurado (nomic-embed-text via Ollama) con fallback
opcional explícito. La colección se estampa con la huella (fingerprint) del runtime
activo y se verifica antes de consultar/insertar; en caso de incompatibilidad se
reporta REINDEX_REQUIRED sin borrar datos del usuario.
"""

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from loguru import logger

_VAULT_PATH = Path(__file__).parent.parent / "brain" / "vector_store"
_DEFAULT_DOCS = Path(__file__).parent.parent / "brain" / "docs"
_COLLECTION_NAME = "knowledge_vault"
_CHUNK_SIZE = 1000   # chars
_CHUNK_OVERLAP = 200  # chars


class KnowledgeVaultUnavailable(RuntimeError):
    """The vector backend (chromadb / sentence-transformers / torch) could not
    initialize. Carries a *classified* error and a *safe* message that never
    exposes dependency internals (torch.Tensor, infer_schema, model encode
    signatures, Chroma internals) to callers or the LLM.
    """

    def __init__(self, error_class: str, safe_message: str) -> None:
        self.error_class = error_class
        self.safe_message = safe_message
        super().__init__(safe_message)


def _classify_backend_error(exc: Exception) -> tuple[str, str]:
    """Map a raw dependency exception to (error_class, safe_message).

    V68.1 M45: the observed live failure was torch 2.x `infer_schema` rejecting
    a transformers custom-op signature (``Parameter input has unsupported type
    torch.Tensor``) at ``import sentence_transformers`` time. That raw text must
    NEVER reach the LLM — it caused unrelated-topic hallucination. We collapse
    every backend fault into a short, honest, internal-free message.
    """
    low = str(exc).lower()
    if "infer_schema" in low or ("torch" in low and "unsupported type" in low):
        return (
            "dependency_incompatibility",
            "Vector backend offline: embedding dependency version mismatch "
            "(torch/transformers). Knowledge retrieval is unavailable.",
        )
    if isinstance(exc, ModuleNotFoundError) or "no module named" in low:
        return (
            "dependency_missing",
            "Vector backend offline: a required embedding dependency is not installed.",
        )
    if isinstance(exc, ImportError):
        return (
            "dependency_import_failed",
            "Vector backend offline: an embedding dependency failed to import.",
        )
    return (
        "backend_init_failed",
        "Vector backend offline: the embedding/vector store failed to initialize.",
    )


def _char_chunks(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping character-based chunks."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return [c for c in chunks if c.strip()]


_vault_singleton: "KnowledgeVault | None" = None


def get_vault() -> "KnowledgeVault":
    """Singleton accessor — lazy init, blocking (call from executor or startup)."""
    global _vault_singleton
    if _vault_singleton is None:
        _vault_singleton = KnowledgeVault()
    return _vault_singleton


def vault_count_if_loaded() -> int | None:
    """V69 M55.11 — the vault document count ONLY when the backend is already
    initialized. Returns ``None`` without forcing a (heavy) Chroma load, so a
    deterministic 'is the vault empty?' bypass stays bounded and non-blocking on
    the interactive path. Guarded end-to-end."""
    v = _vault_singleton
    if v is None or not getattr(v, "_backend_ready", False):
        return None
    try:
        return int(v._collection.count())
    except Exception:  # noqa: BLE001
        return None


class KnowledgeVault:
    def __init__(self, embedder=None) -> None:
        # V68.1 M45: construction is cheap and NEVER imports heavy vector deps.
        # V69 M52: embeddings come from the injectable unified runtime (default:
        # the production singleton, Ollama-first, no torch import unless the
        # operator opted into the fallback). Backend init (chromadb + runtime
        # health + fingerprint compatibility) is deferred to _ensure_backend() so
        # a dependency/config fault degrades honestly instead of raising a raw
        # backend stack trace out of the tool boundary.
        _VAULT_PATH.mkdir(parents=True, exist_ok=True)
        self._chroma = None
        self._collection = None
        self._embedder = embedder
        self._active_fingerprint: str = ""
        self._backend_ready = False
        self._backend_error: tuple[str, str] | None = None
        self._reindex: "object | None" = None   # CompatibilityResult on mismatch

    def _get_embedder(self):
        if self._embedder is None:
            from core.embedding_runtime import get_runtime
            self._embedder = get_runtime()
        return self._embedder

    def _ensure_backend(self) -> bool:
        """Attempt one guarded initialization of the vector backend.

        Returns True when ready AND the collection is compatible with the active
        embedding runtime. Returns False when unavailable OR a reindex is required
        (distinguish via ``self._backend_error`` vs ``self._reindex``). On failure
        the fault is classified once and cached; dependency internals never leak.
        Idempotent.
        """
        if self._backend_ready:
            return True
        if self._backend_error is not None or self._reindex is not None:
            return False
        try:
            import chromadb
            from core.vector_collections import (
                META_FINGERPRINT, check_compatibility, stamp_metadata, COMPAT_OK,
            )

            embedder = self._get_embedder()
            health = embedder.health()
            if not health.available:
                self._backend_error = (
                    health.error_class or "backend_init_failed",
                    health.message or "Embedding runtime unavailable.",
                )
                logger.warning(
                    f"KnowledgeVault embedding runtime unavailable "
                    f"[{self._backend_error[0]}]: {self._backend_error[1]}"
                )
                return False

            self._chroma = chromadb.PersistentClient(path=str(_VAULT_PATH))
            stamp = stamp_metadata(
                health, created_at=datetime.now(timezone.utc).isoformat()
            )
            self._collection = self._chroma.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine", **stamp},
            )
            stored_meta = dict(self._collection.metadata or {})
            # A collection with no vectors yet is safe to (re)stamp to the active
            # runtime — there is no incompatible data to protect.
            if self._collection.count() == 0 and stored_meta.get(META_FINGERPRINT) != health.fingerprint:
                try:
                    self._collection.modify(metadata={"hnsw:space": "cosine", **stamp})
                    stored_meta = stamp
                except Exception:  # noqa: BLE001 — restamp is best-effort
                    pass

            compat = check_compatibility(stored_meta, health)
            if compat.status != COMPAT_OK:
                self._reindex = compat
                logger.warning(
                    f"KnowledgeVault collection incompatible [{compat.status}]: {compat.reason}"
                )
                return False

            self._active_fingerprint = health.fingerprint
            self._backend_ready = True
            logger.info(
                f"KnowledgeVault: DB en {_VAULT_PATH} | provider {health.provider} "
                f"| model {health.model} | fp {health.fingerprint} | dim {health.dimension}"
            )
            return True
        except Exception as e:  # noqa: BLE001 — classify, never propagate raw
            self._backend_error = _classify_backend_error(e)
            logger.warning(
                f"KnowledgeVault backend unavailable [{self._backend_error[0]}]: {e}"
            )
            return False

    def backend_status(self) -> dict:
        """Read-only, internal-free backend health for self-test / boot summary."""
        if self._backend_ready:
            return {"available": True, "error_class": None, "message": "ready"}
        if self._reindex is not None:
            return {
                "available": False,
                "error_class": "reindex_required",
                "message": getattr(self._reindex, "reason", "Collection requires reindex."),
            }
        if self._backend_error is not None:
            return {
                "available": False,
                "error_class": self._backend_error[0],
                "message": self._backend_error[1],
            }
        return {"available": None, "error_class": None, "message": "not yet initialized"}

    @property
    def _client(self):
        """Alias for the Chroma client — used by episodic_memory / relevance_graph
        for cross-collection access. Raises a *clean* KnowledgeVaultUnavailable
        (never a backend trace) when the backend cannot initialize; every internal
        caller already wraps this in try/except and degrades to empty results.

        NOTE: the "jarvis_episodic" collection reached through this client still
        uses Chroma's DEFAULT embedder (documented split-brain, deferred to a
        later milestone with its own reindex) — it is intentionally NOT routed
        through the unified runtime yet. See docs/V69_M52_EMBEDDING_RUNTIME.md.
        """
        if not self._ensure_backend():
            # Prefer the classified backend error; fall back to a reindex hint.
            if self._backend_error is not None:
                raise KnowledgeVaultUnavailable(*self._backend_error)
            raise KnowledgeVaultUnavailable(
                "reindex_required", "Knowledge Vault requires a reindex."
            )
        return self._chroma

    def _embed(self, text: str) -> list[float]:
        # v31.0 / V69 M52: route through the LRU cache keyed by the active
        # fingerprint. Episodic memory and the incident correlator re-query
        # identical MITRE strings constantly; caching eliminates redundant
        # provider passes. The fingerprint key ensures a provider change (e.g.
        # explicit fallback activation) never returns a stale-provider vector.
        if not self._ensure_backend():
            if self._backend_error is not None:
                raise KnowledgeVaultUnavailable(*self._backend_error)
            raise KnowledgeVaultUnavailable(
                "reindex_required", "Knowledge Vault requires a reindex."
            )
        return self._embed_cached(self._active_fingerprint, text)

    @lru_cache(maxsize=512)
    def _embed_cached(self, fingerprint: str, text: str) -> list[float]:
        res = self._get_embedder().embed_text(text)
        if not res.ok:
            raise KnowledgeVaultUnavailable(
                res.error_class or "embedding_error",
                res.message or "Embedding failed.",
            )
        return res.vector

    def embedding_cache_info(self) -> str:
        info = self._embed_cached.cache_info()
        return (
            f"embeddings: hits={info.hits} misses={info.misses} "
            f"size={info.currsize}/{info.maxsize}"
        )

    def _degraded_envelope(self) -> dict:
        """Structured, internal-free envelope when the backend can't serve.

        Distinguishes a hard ``unavailable`` (dependency/config/provider fault)
        from a ``reindex_required`` (collection built by a different embedding
        model — the user's data is preserved, never queried with an incompatible
        vector). Neither ever exposes backend internals to the LLM.
        """
        if self._reindex is not None:
            return {
                "status": "reindex_required",
                "error_class": "reindex_required",
                "message": (
                    "Knowledge Vault was indexed with a different embedding model. "
                    "Reindex is required before it can be queried; existing data is "
                    "preserved."
                ),
            }
        error_class, safe = self._backend_error or (
            "backend_init_failed", "Vector backend offline."
        )
        return {"status": "unavailable", "error_class": error_class, "message": safe}

    def _read_pdf(self, path: Path) -> str:
        import pdfplumber
        parts: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(text)
        return "\n\n".join(parts)

    def ingest_docs(self, folder_path: str = "") -> dict:
        """Scan folder for PDFs and TXTs, chunk into 1000-char/200-overlap pieces and index."""
        if not self._ensure_backend():
            return self._degraded_envelope()
        folder = (
            Path(folder_path).expanduser().resolve()
            if folder_path
            else _DEFAULT_DOCS.resolve()
        )

        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            return {
                "status": "ready",
                "folder": str(folder),
                "message": (
                    f"Carpeta creada en {folder}. "
                    "Agrega archivos PDF o TXT y vuelve a llamar ingest_docs."
                ),
            }

        pdf_files = list(folder.rglob("*.pdf"))
        txt_files = list(folder.rglob("*.txt"))
        all_files = pdf_files + txt_files

        if not all_files:
            return {
                "status": "empty",
                "folder": str(folder),
                "message": "No se encontraron archivos PDF ni TXT en la carpeta.",
            }

        total_chunks = 0
        indexed: list[dict] = []
        errors: list[dict] = []

        for file_path in sorted(all_files):
            try:
                if file_path.suffix.lower() == ".pdf":
                    text = self._read_pdf(file_path)
                else:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")

                if not text.strip():
                    continue

                chunks = _char_chunks(text)
                source = str(file_path.relative_to(folder))
                for i, chunk in enumerate(chunks):
                    doc_id = hashlib.md5(f"{source}:{i}".encode()).hexdigest()
                    self._collection.upsert(
                        embeddings=[self._embed(chunk)],
                        documents=[chunk],
                        ids=[doc_id],
                        metadatas=[{"source": source, "chunk_idx": i}],
                    )

                indexed.append({"file": source, "chunks": len(chunks)})
                total_chunks += len(chunks)
                logger.info(f"KnowledgeVault: indexado '{source}' → {len(chunks)} chunks")
            except Exception as e:
                errors.append({"file": file_path.name, "error": str(e)})
                logger.error(f"KnowledgeVault ingest error en {file_path}: {e}")

        result: dict = {
            "status": "ok",
            "folder": str(folder),
            "files_indexed": len(indexed),
            "total_chunks": total_chunks,
            "files": indexed,
        }
        if errors:
            result["errors"] = errors
        return result

    def query(self, query: str, n_results: int = 3) -> dict:
        """Structured, plain-Python query boundary (V68.1 M45).

        Returns a dict with a discriminating ``status`` and NO dependency
        internals. This is the stable adapter every caller (tool executor,
        self-test) should prefer over the string wrapper below.

          status == "unavailable" → backend offline (dep incompat / missing)
          status == "empty"       → vault has no indexed documents
          status == "error"       → embedding / vector-store query fault
          status == "ok"          → bounded fragments + source references
        """
        # Validate the plain public contract before touching any backend.
        if not isinstance(query, str) or not query.strip():
            return {
                "status": "error",
                "error_class": "invalid_query",
                "message": "Query must be a non-empty string.",
            }
        try:
            n_results = int(n_results)
        except (TypeError, ValueError):
            n_results = 3
        n_results = max(1, min(n_results, 10))

        if not self._ensure_backend():
            return self._degraded_envelope()

        try:
            count = self._collection.count()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"KnowledgeVault.query count error: {e}")
            return {
                "status": "error",
                "error_class": "vector_store_error",
                "message": "Knowledge Vault could not be read.",
            }

        if count == 0:
            return {
                "status": "empty",
                "message": (
                    "Knowledge Vault is empty. Index PDFs or TXTs with ingest_docs first."
                ),
            }

        try:
            embedding = self._embed(query)
            n = min(n_results, count)
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=n,
                include=["documents", "metadatas"],
            )
        except KnowledgeVaultUnavailable as e:
            # A hard dependency/config/provider condition degrades to
            # "unavailable"; a transient per-call embedding fault is "error".
            _HARD = {
                "dependency_incompatibility", "dependency_missing",
                "dependency_import_failed", "provider_unreachable",
                "not_configured", "backend_init_failed", "reindex_required",
            }
            status = "unavailable" if e.error_class in _HARD else "error"
            return {"status": status, "error_class": e.error_class,
                    "message": e.safe_message}
        except Exception as e:  # noqa: BLE001 — never leak a stack trace to the LLM
            logger.warning(f"KnowledgeVault.query embedding/search error: {e}")
            return {
                "status": "error",
                "error_class": "embedding_error",
                "message": "Knowledge Vault search failed while computing the query embedding.",
            }

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        if not docs:
            return {"status": "empty", "message": "No relevant fragments matched the query."}

        fragments: list[dict] = []
        for i, (doc, meta) in enumerate(zip(docs, metas), 1):
            meta = meta or {}
            fragments.append({
                "source": meta.get("source", "unknown"),
                "chunk": meta.get("chunk_idx", i - 1),
                "text": doc,
            })
        return {"status": "ok", "count": len(fragments), "fragments": fragments}

    @staticmethod
    def _format_fragments(result: dict) -> str:
        """Render a structured query() result to the LLM-facing text block."""
        status = result.get("status")
        if status == "ok":
            return "\n\n---\n\n".join(
                f"[Source: {f['source']} | Chunk {f['chunk']}]\n{f['text']}"
                for f in result["fragments"]
            )
        return str(result.get("message", "Knowledge Vault returned no usable result."))

    def query_knowledge(self, query: str, n_results: int = 3) -> str:
        """Back-compat string wrapper over the structured query() boundary.

        Retained for any legacy caller; the tool executor uses query() directly
        so it can build a typed failure envelope instead of a bare string.
        """
        return self._format_fragments(self.query(query, n_results))
