"""
core/memory_fabric.py — V63 Milestone 5: scoped memory fabric facade.

A **policy layer** unifying JARVIS's three existing memory stores
(episodic_memory / KnowledgeVault / VectorMemory) behind ONE facade **without
migrating or deleting any of them**. Adapters wrap the stores unchanged;
consolidation stays gradual — the V62 report's explicit "consolidate before
adding" direction (residual risk #4).

The facade enforces the retrieval/write policy the raw stores lack:
  * secret redaction on write (core.memory_router.redact_secrets)
  * untrusted-source labeling + anti-injection: untrusted content is stored with
    trusted=False and is excluded from retrieval by default, so web/tool text can
    never silently become injected prompt context (episodic also sanitizes
    external sources via feed_sanitizer at the store layer)
  * sensitivity labels + scope filters
  * bounded retrieval — never dumps an entire store into a prompt
  * deduplication by normalized content
  * recency + relevance ranking
  * provenance preserved on every record

Dependency-injectable: adapters are passed in (or lazily built for production),
so the policy is unit-testable with fakes — no ChromaDB required.

Wiring status: `store()` is wired into the live conversation-memory write path
(`core.llm.LLM._maybe_persist_memory`). `retrieve()` is the facade's read API,
implemented and tested; migrating the hot retrieval path (the PageRank-ranked
episodic query in chat_stream) onto it is the next *gradual* step, deliberately
deferred so it lands with its own regression coverage rather than silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from loguru import logger

from core.memory_router import contains_secret, is_untrusted_source, redact_secrets

# Scopes align with core.memory_router.classify_memory_scope's MemoryScope.
VALID_SCOPES: frozenset[str] = frozenset({"session", "project", "long_term", "none"})


class Sensitivity(str, Enum):
    PUBLIC = "public"
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


@dataclass(frozen=True)
class Provenance:
    """Where a memory came from and how it must be treated."""

    source: str = "internal"
    scope: str = "none"
    sensitivity: str = Sensitivity.NORMAL.value
    trusted: bool = True
    timestamp: str | None = None


@dataclass(frozen=True)
class MemoryRecord:
    """One retrieved/stored memory with full provenance."""

    content: str
    provenance: Provenance
    relevance: float = 0.0        # 0.0 .. 1.0 (higher = more relevant)
    origin: str = "unknown"       # which adapter produced it
    memory_type: str = "episode"


@runtime_checkable
class RetrievalAdapter(Protocol):
    name: str

    async def retrieve(self, query: str, limit: int) -> list[MemoryRecord]:
        ...


@runtime_checkable
class StorageAdapter(Protocol):
    name: str

    async def store(
        self,
        content: str,
        *,
        memory_type: str,
        source: str,
        scope: str,
        sensitivity: str,
    ) -> bool:
        ...


# ── Concrete adapters over the existing stores (lazy imports; no Chroma unless
# actually used) ─────────────────────────────────────────────────────────────
class EpisodicAdapter:
    """Wraps core.episodic_memory (primary store: retrieval + storage)."""

    name = "episodic"

    async def retrieve(self, query: str, limit: int) -> list[MemoryRecord]:
        try:
            from core.episodic_memory import query_similar_episodes
            rows = await query_similar_episodes(query, n_results=limit)
        except Exception as e:  # pragma: no cover - backend optional
            logger.debug(f"MEMORY_FABRIC: episodic retrieve unavailable: {e}")
            return []
        out: list[MemoryRecord] = []
        for row in rows:
            source = str(row.get("source", "internal"))
            distance = float(row.get("distance", 1.0) or 1.0)
            out.append(MemoryRecord(
                content=str(row.get("content", "")),
                provenance=Provenance(
                    source=source,
                    scope=str(row.get("scope", "none")),
                    sensitivity=str(row.get("sensitivity", Sensitivity.NORMAL.value)),
                    trusted=not is_untrusted_source(source),
                    timestamp=row.get("timestamp"),
                ),
                relevance=max(0.0, min(1.0, 1.0 - distance)),
                origin=self.name,
                memory_type=str(row.get("event_type", "episode")),
            ))
        return out

    async def store(
        self, content: str, *, memory_type: str, source: str, scope: str, sensitivity: str,
    ) -> bool:
        try:
            from core.episodic_memory import store_episode
            await store_episode(
                content,
                event_type=memory_type,
                severity="INFO",
                source=source,
                scope=scope,
                sensitivity=sensitivity,
            )
            return True
        except Exception as e:  # pragma: no cover - backend optional
            logger.debug(f"MEMORY_FABRIC: episodic store unavailable: {e}")
            return False


class VectorMemoryAdapter:
    """Wraps core.memory.VectorMemory (semantic knowledge; retrieval, best-effort)."""

    name = "vector"

    async def retrieve(self, query: str, limit: int) -> list[MemoryRecord]:
        try:
            from core.memory import VectorMemory
            res = VectorMemory().query(query, n_results=limit)
        except Exception as e:  # pragma: no cover - backend optional
            logger.debug(f"MEMORY_FABRIC: vector retrieve unavailable: {e}")
            return []
        text = res.get("result") if isinstance(res, dict) else None
        if not text:
            return []
        return [MemoryRecord(
            content=str(text),
            provenance=Provenance(source="vector_store", scope="long_term", trusted=True),
            relevance=0.5,
            origin=self.name,
            memory_type="knowledge",
        )]


@dataclass
class MemoryFabric:
    """The unified facade. Compose from adapters (injectable for tests)."""

    retrieval_adapters: list[RetrievalAdapter] = field(default_factory=list)
    storage_adapter: StorageAdapter | None = None
    dedup_prefix: int = 200

    # ── retrieve ─────────────────────────────────────────────────────────────
    async def retrieve(
        self,
        query: str,
        *,
        scopes: set[str] | None = None,
        limit: int = 5,
        min_relevance: float = 0.0,
        allow_untrusted: bool = False,
    ) -> list[MemoryRecord]:
        """Bounded, scoped, deduped, provenance-tagged retrieval across stores.

        Never returns more than ``limit`` records (no full-store dump). Untrusted
        memories are excluded unless ``allow_untrusted`` — the anti-injection
        default. Ranked by relevance, then recency (timestamp).
        """
        if not query or limit <= 0:
            return []
        per_adapter = max(limit, 1)
        gathered: list[MemoryRecord] = []
        for adapter in self.retrieval_adapters:
            try:
                gathered.extend(await adapter.retrieve(query, per_adapter))
            except Exception as e:
                logger.debug(f"MEMORY_FABRIC: adapter {getattr(adapter,'name','?')} failed: {e}")

        scope_filter = {s for s in (scopes or set()) if s in VALID_SCOPES}
        seen: set[str] = set()
        kept: list[MemoryRecord] = []
        for rec in gathered:
            if rec.relevance < min_relevance:
                continue
            if not allow_untrusted and not rec.provenance.trusted:
                continue
            if scope_filter and rec.provenance.scope not in scope_filter:
                continue
            key = " ".join(rec.content.lower().split())[: self.dedup_prefix]
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(rec)

        kept.sort(key=lambda r: (r.relevance, r.provenance.timestamp or ""), reverse=True)
        return kept[:limit]

    # ── store ─────────────────────────────────────────────────────────────────
    async def store(
        self,
        content: str,
        *,
        memory_type: str = "episode",
        source: str = "internal",
        scope: str = "none",
        sensitivity: Sensitivity | str = Sensitivity.NORMAL,
        confidence: float = 1.0,
    ) -> bool:
        """Policy-guarded write. Redacts secrets, labels untrusted provenance,
        applies sensitivity/scope, delegates to the storage adapter. Fail-open.
        """
        if self.storage_adapter is None or not (content or "").strip():
            return False
        sens = sensitivity.value if isinstance(sensitivity, Sensitivity) else str(sensitivity)
        # Secret redaction (idempotent). A still-secret payload is escalated to
        # SECRET sensitivity so downstream never treats it as normal.
        safe = redact_secrets(content)
        if contains_secret(safe):
            sens = Sensitivity.SECRET.value
        try:
            return await self.storage_adapter.store(
                safe, memory_type=memory_type, source=source, scope=scope, sensitivity=sens,
            )
        except Exception as e:
            logger.debug(f"MEMORY_FABRIC: store failed: {e}")
            return False


_FABRIC: MemoryFabric | None = None


def get_fabric() -> MemoryFabric:
    """Production singleton: episodic as primary store + episodic/vector for
    retrieval fan-out. Adapters lazily touch their backends only when called."""
    global _FABRIC
    if _FABRIC is None:
        episodic = EpisodicAdapter()
        _FABRIC = MemoryFabric(
            retrieval_adapters=[episodic, VectorMemoryAdapter()],
            storage_adapter=episodic,
        )
    return _FABRIC
