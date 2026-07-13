"""
core/memory.py — Memoria vectorial persistente con ChromaDB.

Almacena y recupera fragmentos de conocimiento usando el runtime de embeddings
unificado (core.embedding_runtime). La DB se guarda en jarvis_v2/.chroma_db para
persistencia entre sesiones.

V69 M52: los embeddings ya no se calculan con sentence-transformers directamente
aquí — se delegan al runtime unificado (rol EMBEDDING configurado, nomic-embed
-text via Ollama, con fallback opcional explícito). La colección se estampa con la
huella del runtime activo y se verifica la compatibilidad antes de consultar o
insertar, evitando mezclar vectores de modelos distintos en una misma colección.
"""

from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_DB_PATH = Path(__file__).parent.parent.parent / ".chroma_db"
_COLLECTION_NAME = "jarvis_knowledge"


class VectorMemory:
    def __init__(self, embedder=None):
        import chromadb

        from core.embedding_runtime import get_runtime
        from core.vector_collections import (
            META_FINGERPRINT, check_compatibility, stamp_metadata,
        )

        self._embedder = embedder or get_runtime()
        self._health = self._embedder.health()
        self._chroma = chromadb.PersistentClient(path=str(_DB_PATH))

        stamp = (
            stamp_metadata(self._health, created_at=datetime.now(timezone.utc).isoformat())
            if self._health.available else {}
        )
        self._collection = self._chroma.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine", **stamp},
        )
        # Re-stamp an empty collection to the active runtime (no data to protect).
        stored_meta = dict(self._collection.metadata or {})
        if (
            self._health.available
            and self._collection.count() == 0
            and stored_meta.get(META_FINGERPRINT) != self._health.fingerprint
        ):
            try:
                self._collection.modify(metadata={"hnsw:space": "cosine", **stamp})
                stored_meta = stamp
            except Exception:  # noqa: BLE001
                pass

        self._compat = check_compatibility(stored_meta, self._health)
        if not self._compat.compatible:
            logger.warning(
                f"VectorMemory: collection incompatible [{self._compat.status}]: "
                f"{self._compat.reason}"
            )
        else:
            logger.info(
                f"VectorMemory: ChromaDB en {_DB_PATH} | provider {self._health.provider} "
                f"| model {self._health.model} | fp {self._health.fingerprint}"
            )

    def _embed(self, text: str) -> list[float]:
        res = self._embedder.embed_text(text)
        if not res.ok:
            raise RuntimeError(res.message or "Embedding failed.")
        return res.vector

    def add(self, text: str, doc_id: str, metadata: dict | None = None) -> bool:
        """Inserta o actualiza un fragmento. Bloquea inserciones incompatibles
        (nunca mezcla vectores de modelos distintos). Devuelve True si se indexó."""
        if not self._compat.compatible:
            logger.warning(
                "VectorMemory.add: skipped — collection requires reindex "
                f"({self._compat.status})."
            )
            return False
        try:
            self._collection.upsert(
                embeddings=[self._embed(text)],
                documents=[text],
                ids=[doc_id],
                metadatas=[metadata or {}],
            )
            return True
        except Exception as e:  # noqa: BLE001 — never crash the ingest loop
            logger.warning(f"VectorMemory.add error: {e}")
            return False

    def query(self, query: str, n_results: int = 3) -> dict:
        """Vectoriza la pregunta y retorna los n fragmentos más relevantes."""
        if not self._compat.compatible:
            return {
                "error": (
                    "La base de conocimiento fue indexada con otro modelo de "
                    "embeddings. Se requiere reindexar; los datos se conservan."
                )
            }
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
