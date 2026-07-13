"""core/episodic_memory.py — Episodic operational memory via ChromaDB (v25.0).

V69 M53: the active episodic path no longer touches Chroma's implicit/default
embedder. Writes and reads go through core.semantic_memory.SemanticStore, which
resolves the logical ``jarvis_episodic`` collection to its ACTIVE physical
collection via the durable alias registry and uses EXPLICIT vectors from the
unified embedding runtime (Ollama nomic-embed-text). Until the operator migrates
the legacy (unstamped) collection, reads return empty and writes are journaled to
a bounded migration-delta journal so nothing is lost or silently mixed.
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from loguru import logger

_LOGICAL = "jarvis_episodic"


def resolve_episodic_physical(client=None, registry=None) -> str:
    """Active physical collection name for episodic memory (alias-resolved).

    Used by read-only consumers (memory_consolidator / relevance_graph) so they
    operate on the SAME physical collection the runtime writes to, before and
    after a migration. Falls back to the legacy logical name when no alias exists.
    """
    try:
        from core.alias_registry import AliasRegistry
        from core.semantic_memory import _VAULT_PATH

        reg = registry or AliasRegistry(_VAULT_PATH / "alias_registry.json")
        return reg.resolve(_LOGICAL) or _LOGICAL
    except Exception:  # noqa: BLE001
        return _LOGICAL


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


def _episode_id(content: str, timestamp: str) -> str:
    """Deterministic, stable episode id (sha256 — not Python's salted hash())."""
    h = hashlib.sha256(f"{content}|{timestamp}".encode("utf-8", "ignore")).hexdigest()
    return f"ep_{h[:24]}"


def _write_episode(data: dict) -> None:
    """Blocking — runs in executor. Explicit-embedding write via SemanticStore."""
    try:
        from core.semantic_memory import SemanticStore

        content = data["content"]
        meta = {k: v for k, v in data.items() if k != "content"}
        status = SemanticStore(_LOGICAL).write(
            _episode_id(content, data["timestamp"]), content, meta,
        )
        if status not in ("ok", "journaled"):
            logger.debug(f"EPISODIC_MEMORY: episode not stored (status={status}).")
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
    """Blocking — runs in executor. Explicit query-embedding read via SemanticStore.

    Returns [] when the active collection is incompatible (never queries an
    incompatible collection with a mismatched embedder)."""
    try:
        from core.semantic_memory import SemanticStore

        return SemanticStore(_LOGICAL).query(query, n_results=n)
    except Exception:
        return []
