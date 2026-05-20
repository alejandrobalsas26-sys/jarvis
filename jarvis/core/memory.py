"""
core/memory.py — Memoria vectorial persistente con ChromaDB y sentence-transformers.

Almacena y recupera fragmentos de conocimiento usando embeddings semánticos locales.
La DB se guarda en jarvis_v2/.chroma_db para persistencia entre sesiones.
"""

from pathlib import Path
from loguru import logger

_DB_PATH = Path(__file__).parent.parent.parent / ".chroma_db"
_MODEL_NAME = "all-MiniLM-L6-v2"


class VectorMemory:
    def __init__(self):
        import chromadb
        from sentence_transformers import SentenceTransformer

        self._chroma = chromadb.PersistentClient(path=str(_DB_PATH))
        self._collection = self._chroma.get_or_create_collection(
            name="jarvis_knowledge",
            metadata={"hnsw:space": "cosine"},
        )
        self._model = SentenceTransformer(_MODEL_NAME)
        logger.info(f"VectorMemory: ChromaDB en {_DB_PATH} | modelo {_MODEL_NAME}")

    def _embed(self, text: str) -> list[float]:
        return self._model.encode([text]).tolist()[0]

    def add(self, text: str, doc_id: str, metadata: dict | None = None) -> None:
        """Inserta o actualiza un fragmento en la colección."""
        self._collection.upsert(
            embeddings=[self._embed(text)],
            documents=[text],
            ids=[doc_id],
            metadatas=[metadata or {}],
        )

    def query(self, query: str, n_results: int = 3) -> dict:
        """Vectoriza la pregunta y retorna los n fragmentos más relevantes."""
        try:
            count = self._collection.count()
            if count == 0:
                return {
                    "result": (
                        "La base de conocimiento está vacía. "
                        "Usa jarvis/scripts/ingest_docs.py para poblarla."
                    )
                }

            n = min(n_results, count)
            results = self._collection.query(
                query_embeddings=[self._embed(query)],
                n_results=n,
                include=["documents", "metadatas"],
            )

            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]

            fragments = []
            for i, (doc, meta) in enumerate(zip(docs, metas), 1):
                source = meta.get("source", "desconocido")
                fragments.append(f"[{i}] Fuente: {source}\n{doc}")

            if not fragments:
                return {"result": "No se encontraron fragmentos relevantes."}

            return {
                "fragments_found": len(fragments),
                "result": "\n\n---\n\n".join(fragments),
            }
        except Exception as e:
            logger.error(f"VectorMemory.query error: {e}")
            return {"error": f"Error consultando base de conocimiento: {e}"}
