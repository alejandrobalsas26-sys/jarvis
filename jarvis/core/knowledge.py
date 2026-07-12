"""
core/knowledge.py — Knowledge Vault: RAG local con ChromaDB y sentence-transformers.

Indexa PDFs y TXTs de jarvis/brain/docs/ en un vector store persistente localizado
en jarvis/brain/vector_store/. Usa all-MiniLM-L6-v2 para embeddings (CPU-only).
"""

import hashlib
from functools import lru_cache
from pathlib import Path
from loguru import logger

_VAULT_PATH = Path(__file__).parent.parent / "brain" / "vector_store"
_DEFAULT_DOCS = Path(__file__).parent.parent / "brain" / "docs"
_MODEL_NAME = "all-MiniLM-L6-v2"
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


class KnowledgeVault:
    def __init__(self) -> None:
        # V68.1 M45: construction is now cheap and NEVER imports heavy vector
        # deps. Backend init (chromadb + sentence-transformers + torch) is
        # deferred to _ensure_backend() so a dependency fault degrades honestly
        # instead of raising a raw torch stack trace out of the tool boundary.
        _VAULT_PATH.mkdir(parents=True, exist_ok=True)
        self._chroma = None
        self._collection = None
        self._model = None
        self._backend_ready = False
        self._backend_error: tuple[str, str] | None = None

    def _ensure_backend(self) -> bool:
        """Attempt one guarded initialization of the vector backend.

        Returns True when ready, False when unavailable. On failure the fault is
        classified once and cached; dependency internals never leak. Idempotent.
        """
        if self._backend_ready:
            return True
        if self._backend_error is not None:
            return False
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer

            self._chroma = chromadb.PersistentClient(path=str(_VAULT_PATH))
            self._collection = self._chroma.get_or_create_collection(
                name="knowledge_vault",
                metadata={"hnsw:space": "cosine"},
            )
            self._model = SentenceTransformer(_MODEL_NAME)
            self._backend_ready = True
            logger.info(f"KnowledgeVault: DB en {_VAULT_PATH} | modelo {_MODEL_NAME}")
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
        (never a torch trace) when the backend cannot initialize; every internal
        caller already wraps this in try/except and degrades to empty results.
        """
        if not self._ensure_backend():
            raise KnowledgeVaultUnavailable(*self._backend_error)
        return self._chroma

    def _embed(self, text: str) -> list[float]:
        # v31.0: route through LRU cache. Episodic memory and incident
        # correlator re-query identical MITRE strings constantly; caching
        # eliminates the bulk of redundant sentence-transformers passes.
        if not self._ensure_backend():
            raise KnowledgeVaultUnavailable(*self._backend_error)
        return self._embed_cached(text)

    @lru_cache(maxsize=512)
    def _embed_cached(self, text: str) -> list[float]:
        return self._model.encode([text]).tolist()[0]

    def embedding_cache_info(self) -> str:
        info = self._embed_cached.cache_info()
        return (
            f"embeddings: hits={info.hits} misses={info.misses} "
            f"size={info.currsize}/{info.maxsize}"
        )

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
            error_class, safe = self._backend_error
            return {
                "status": "unavailable",
                "error_class": error_class,
                "message": safe,
            }
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
            error_class, safe = self._backend_error
            return {"status": "unavailable", "error_class": error_class, "message": safe}

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
            return {"status": "unavailable", "error_class": e.error_class,
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
