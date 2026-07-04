"""core/episodic_memory.py — Episodic operational memory via ChromaDB (v25.0)."""

import asyncio
from datetime import datetime, timezone
from loguru import logger


async def store_episode(
    content: str,
    event_type: str,
    severity: str = "INFO",
    mitre_tags: list[str] | None = None,
    source: str = "internal",
    scope: str = "none",
    sensitivity: str = "normal",
) -> None:
    """
    Embed and store an operational episode in ChromaDB jarvis_episodic collection.
    Called after: triage manifest write, agentic_summary broadcast,
    canary_intrusion with banner, binary_inversion_complete.
    For external content (source != internal): sanitize via feed_sanitizer first.

    ``scope`` (V62.0 Phase 3, e.g. session/project/long_term/none — see
    core.memory_router.classify_memory_scope) and ``sensitivity`` are written
    as real, filterable Chroma metadata — additive alongside any scope prefix
    a caller already bakes into ``content`` itself. ``source`` is likewise
    now persisted as metadata instead of being discarded after the
    sanitization gate above, so stored episodes carry real provenance.
    """
    if source != "internal":
        from core.feed_sanitizer import sanitize_for_llm, SanitizationError
        try:
            content = sanitize_for_llm(content, source=source)
        except SanitizationError as e:
            logger.warning(f"EPISODIC_MEMORY: Episode rejected from {source}: {e}")
            return

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_episode, {
        "content":     content,
        "event_type":  event_type,
        "severity":    severity,
        "mitre_tags":  ",".join(mitre_tags or []),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "source":      source,
        "scope":       scope,
        "sensitivity": sensitivity,
    })


def _write_episode(data: dict) -> None:
    """Blocking — runs in executor."""
    try:
        from core.knowledge import get_vault
        vault = get_vault()
        col   = vault._client.get_or_create_collection("jarvis_episodic")
        col.add(
            documents=[data["content"]],
            metadatas=[{k: v for k, v in data.items() if k != "content"}],
            ids=[f"ep_{hash(data['content'] + data['timestamp'])}"],
        )
    except Exception as e:
        logger.warning(f"EPISODIC_MEMORY: write failed: {e}")


async def query_similar_episodes(query: str, n_results: int = 3) -> list[dict]:
    """
    Semantic search over past operational episodes.
    Returns list of {content, event_type, timestamp, distance}.
    Called by core/llm.py before LLM inference to inject relevant
    past incident context into the prompt.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _query_episodes, query, n_results)


def _query_episodes(query: str, n: int) -> list[dict]:
    """Blocking — runs in executor."""
    try:
        from core.knowledge import get_vault
        vault = get_vault()
        col   = vault._client.get_or_create_collection("jarvis_episodic")
        count = col.count()
        if count == 0:
            return []
        n = min(n, count)
        res   = col.query(query_texts=[query], n_results=n)
        return [
            {"content": doc, "distance": dist, **meta}
            for doc, dist, meta in zip(
                res["documents"][0],
                res["distances"][0],
                res["metadatas"][0],
            )
        ]
    except Exception:
        return []
