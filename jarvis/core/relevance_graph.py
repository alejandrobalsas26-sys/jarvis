"""
core/relevance_graph.py — Episodic memory relevance graph (v30.0).

Builds an igraph knowledge graph over ChromaDB episodic episodes.
Edges represent shared attributes: MITRE technique, host IP, campaign,
temporal proximity, and severity correlation.
PageRank scores identify the most operationally relevant past incidents.
Replaces pure cosine similarity for LLM context injection.
Pruning scheduler removes low-relevance old episodes every 6 hours.
"""

import asyncio
import time
from datetime import datetime, timezone
from loguru import logger

_PRUNE_INTERVAL = 21600   # 6 hours
_PRUNE_AGE_DAYS = 7
_PRUNE_SCORE_THRESHOLD = 0.01  # PageRank scores below this → candidate for pruning


async def build_relevance_graph(max_episodes: int = 200) -> dict[str, float]:
    """
    Fetch up to max_episodes from jarvis_episodic ChromaDB collection,
    build an igraph directed graph, compute PageRank, return
    {episode_id: pagerank_score} dict.
    """
    try:
        import igraph as ig
        from core.knowledge import get_vault

        loop = asyncio.get_running_loop()
        vault = get_vault()

        def _fetch():
            col = vault._client.get_or_create_collection("jarvis_episodic")
            return col.get(
                limit=max_episodes,
                include=["metadatas", "documents"],
            )

        data = await loop.run_in_executor(None, _fetch)
        ids       = data.get("ids", [])
        metadatas = data.get("metadatas", []) or []

        if len(ids) < 2:
            return {}

        n = len(ids)
        edges: list[tuple[int, int]] = []
        weights: list[float] = []

        # Build edges based on shared attributes
        for i in range(n):
            mi = metadatas[i] if i < len(metadatas) else {}
            for j in range(i + 1, n):
                mj = metadatas[j] if j < len(metadatas) else {}
                w = _edge_weight(mi or {}, mj or {})
                if w > 0:
                    edges.append((i, j))
                    weights.append(w)

        g = ig.Graph(n=n, edges=edges, directed=False)
        if weights:
            g.es["weight"] = weights
            pr = g.pagerank(weights="weight")
        else:
            pr = g.pagerank()

        return {ids[i]: pr[i] for i in range(n)}

    except Exception as e:
        logger.debug(f"RELEVANCE_GRAPH: build failed: {e}")
        return {}


def _edge_weight(m1: dict, m2: dict) -> float:
    """Compute edge weight between two episode metadata dicts."""
    w = 0.0

    # Shared MITRE techniques
    t1 = set((m1.get("mitre_tags") or "").split(","))
    t2 = set((m2.get("mitre_tags") or "").split(","))
    shared_mitre = len((t1 & t2) - {""})
    w += shared_mitre * 0.4

    # Same severity
    if m1.get("severity") == m2.get("severity") and m1.get("severity"):
        w += 0.2

    # Same event type
    if m1.get("event_type") == m2.get("event_type") and m1.get("event_type"):
        w += 0.15

    # Temporal proximity — episodes within 1 hour get a bonus
    try:
        ts1 = float(m1.get("timestamp", 0) or 0)
        ts2 = float(m2.get("timestamp", 0) or 0)
        if ts1 and ts2 and abs(ts1 - ts2) < 3600:
            w += 0.25
    except (TypeError, ValueError):
        pass

    return min(w, 1.0)


async def query_graph_ranked_episodes(
    query: str,
    n_results: int = 3,
) -> list[dict]:
    """
    Hybrid retrieval: cosine similarity filtered and re-ranked by PageRank.
    Replaces pure cosine query_similar_episodes in llm.py context injection.
    """
    from core.episodic_memory import query_similar_episodes

    try:
        # Fetch more candidates than needed, then re-rank
        candidates = await query_similar_episodes(query, n_results=n_results * 3)
        if not candidates:
            return []

        # Build graph and get scores
        scores = await build_relevance_graph()

        # Re-rank by PageRank score, fall back to cosine order
        def _rank_key(ep):
            return scores.get(ep.get("id", ""), 0.0)

        ranked = sorted(candidates, key=_rank_key, reverse=True)
        return ranked[:n_results]

    except Exception as e:
        logger.debug(f"RELEVANCE_GRAPH: query failed: {e}")
        return await query_similar_episodes(query, n_results=n_results)


async def prune_low_relevance_episodes() -> int:
    """
    Delete episodes with low PageRank score AND age > _PRUNE_AGE_DAYS.
    Returns count of pruned episodes.
    """
    try:
        from core.knowledge import get_vault

        cutoff_ts = time.time() - (_PRUNE_AGE_DAYS * 86400)
        scores = await build_relevance_graph(max_episodes=500)
        if not scores:
            return 0

        vault = get_vault()
        loop  = asyncio.get_running_loop()

        def _do_prune():
            col = vault._client.get_or_create_collection("jarvis_episodic")
            data = col.get(limit=500, include=["metadatas"])
            ids  = data.get("ids", [])
            metas = data.get("metadatas", []) or []

            to_delete: list[str] = []
            for eid, meta in zip(ids, metas):
                try:
                    ts = float((meta or {}).get("timestamp", time.time()) or time.time())
                except (TypeError, ValueError):
                    ts = time.time()
                sc  = scores.get(eid, 0.0)
                old = ts < cutoff_ts
                low = sc < _PRUNE_SCORE_THRESHOLD
                if old and low:
                    to_delete.append(eid)

            if to_delete:
                col.delete(ids=to_delete)
            return len(to_delete)

        pruned = await loop.run_in_executor(None, _do_prune)
        if pruned:
            logger.info(f"RELEVANCE_GRAPH: pruned {pruned} low-relevance episodes")
        return pruned

    except Exception as e:
        logger.debug(f"RELEVANCE_GRAPH: prune failed: {e}")
        return 0


async def start_pruning_loop(broadcast_fn) -> None:
    """Background task: prune every 6 hours."""
    while True:
        await asyncio.sleep(_PRUNE_INTERVAL)
        pruned = await prune_low_relevance_episodes()
        if pruned:
            try:
                await broadcast_fn({
                    "type":      "memory_pruned",
                    "count":     pruned,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.debug(f"RELEVANCE_GRAPH: broadcast failed: {e}")
