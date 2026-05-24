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
        import chromadb
        from sentence_transformers import SentenceTransformer

        _VAULT_PATH.mkdir(parents=True, exist_ok=True)
        self._chroma = chromadb.PersistentClient(path=str(_VAULT_PATH))
        self._collection = self._chroma.get_or_create_collection(
            name="knowledge_vault",
            metadata={"hnsw:space": "cosine"},
        )
        self._model = SentenceTransformer(_MODEL_NAME)
        logger.info(f"KnowledgeVault: DB en {_VAULT_PATH} | modelo {_MODEL_NAME}")

    @property
    def _client(self):
        """Alias for _chroma — used by episodic_memory for cross-collection access."""
        return self._chroma

    def _embed(self, text: str) -> list[float]:
        # v31.0: route through LRU cache. Episodic memory and incident
        # correlator re-query identical MITRE strings constantly; caching
        # eliminates the bulk of redundant sentence-transformers passes.
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

    def query_knowledge(self, query: str, n_results: int = 3) -> str:
        """Return top-n relevant chunks as formatted text for LLM context injection."""
        try:
            count = self._collection.count()
            if count == 0:
                return (
                    "Knowledge Vault vacía. "
                    "Usa ingest_docs para indexar PDFs o TXTs primero."
                )

            n = min(n_results, count)
            results = self._collection.query(
                query_embeddings=[self._embed(query)],
                n_results=n,
                include=["documents", "metadatas"],
            )

            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]

            if not docs:
                return "No se encontraron fragmentos relevantes para la consulta."

            fragments = [
                f"[Source: {meta.get('source', 'desconocido')} | Chunk {meta.get('chunk_idx', i - 1)}]\n{doc}"
                for i, (doc, meta) in enumerate(zip(docs, metas), 1)
            ]
            return "\n\n---\n\n".join(fragments)

        except Exception as e:
            logger.error(f"KnowledgeVault.query_knowledge error: {e}")
            return f"Error consultando Knowledge Vault: {e}"
