"""
core/memory_consolidator.py — Episodic memory consolidation engine (v36.0).

Runs nightly (or on demand) to:
  1. Group related episodes by technique, host, or time proximity
  2. Use LLM to summarize each group into a "campaign meta-episode"
  3. Store meta-episodes in ChromaDB with campaign-level metadata
  4. Delete constituent episodes (keeping storage bounded)
  5. Update the relevance graph with campaign-level connections

This transforms raw episodic data into strategic campaign intelligence.
"""

import asyncio
import time
from datetime import datetime, timezone
from loguru import logger


_CONSOLIDATION_INTERVAL = 86400   # 24 hours
_MIN_GROUP_SIZE         = 3       # minimum episodes to consolidate
_GROUP_TIME_WINDOW      = 3600    # episodes within 1 hour = same group
_last_consolidation     = 0.0

_CONSOLIDATION_SYSTEM = """You are a threat intelligence analyst consolidating
security incident data into a campaign summary. Be concise and technical.
Extract the campaign narrative, TTP pattern, actor indicators, and impact."""


async def consolidate_memory(
    broadcast_fn,
    ollama_client,
    model: str,
) -> dict:
    """
    Main consolidation routine. Groups + summarizes episodic memory.
    Returns consolidation report.
    """
    global _last_consolidation
    _last_consolidation = time.time()

    logger.info("MEMORY_CONSOLIDATOR: starting consolidation cycle…")

    if ollama_client is None:
        return {"error": "no ollama client"}

    try:
        from core.knowledge import get_vault
        vault = get_vault()
        loop  = asyncio.get_running_loop()

        def _fetch_all():
            from core.episodic_memory import resolve_episodic_physical
            col = vault._client.get_or_create_collection(
                resolve_episodic_physical(), embedding_function=None)
            return col.get(
                limit=500,
                include=["documents", "metadatas"],
            )

        data  = await loop.run_in_executor(None, _fetch_all)
        ids   = data.get("ids", []) or []
        docs  = data.get("documents", []) or []
        metas = data.get("metadatas", []) or []

        if len(ids) < _MIN_GROUP_SIZE:
            logger.info("MEMORY_CONSOLIDATOR: insufficient episodes to consolidate")
            return {"consolidated": 0, "deleted": 0}

        # Group by primary MITRE technique
        technique_groups: dict[str, list[tuple]] = {}
        for eid, doc, meta in zip(ids, docs, metas):
            tags = (meta or {}).get("mitre_tags", "")
            if not tags:
                continue
            primary_tag = tags.split(",")[0].strip()
            if not primary_tag:
                continue
            technique_groups.setdefault(primary_tag, []).append((eid, doc, meta))

        consolidated = 0
        deleted_ids: list[str] = []

        for technique, group in technique_groups.items():
            if len(group) < _MIN_GROUP_SIZE:
                continue

            combined = "\n\n".join(
                f"Episode {i+1}:\n{(doc or '')[:300]}"
                for i, (_, doc, _) in enumerate(group[:10])
            )
            prompt = (
                f"TECHNIQUE: {technique}\n"
                f"EPISODES TO CONSOLIDATE ({len(group)} incidents):\n"
                f"{combined}\n\n"
                "Synthesize these incidents into a campaign summary. "
                "Extract: actor pattern, TTP signature, timeline, "
                "recurring indicators, and strategic implications."
            )

            try:
                response = await asyncio.wait_for(
                    ollama_client.chat.completions.create(
                        model    = model,
                        messages = [
                            {"role": "system", "content": _CONSOLIDATION_SYSTEM},
                            {"role": "user",   "content": prompt},
                        ],
                        stream     = False,
                        extra_body = {"options": {
                            "num_ctx":     3072,
                            "temperature": 0.1,
                        }},
                    ),
                    timeout=60.0,
                )
                summary = response.choices[0].message.content.strip()
            except Exception:
                continue

            try:
                from core.episodic_memory import store_episode
                await store_episode(
                    content    = summary,
                    event_type = "campaign_meta_episode",
                    severity   = "HIGH",
                    mitre_tags = [technique],
                )
            except Exception as e:
                logger.debug(f"MEMORY_CONSOLIDATOR: store_episode failed: {e}")
                continue

            for eid, _, _ in group:
                deleted_ids.append(eid)

            consolidated += 1
            logger.info(
                f"MEMORY_CONSOLIDATOR: consolidated "
                f"{len(group)} episodes → campaign[{technique}]"
            )

        if deleted_ids:
            def _delete():
                from core.episodic_memory import resolve_episodic_physical
                col = vault._client.get_or_create_collection(
                    resolve_episodic_physical(), embedding_function=None)
                col.delete(ids=deleted_ids)

            try:
                await loop.run_in_executor(None, _delete)
            except Exception as e:
                logger.debug(f"MEMORY_CONSOLIDATOR: delete failed: {e}")

        report = {
            "consolidated": consolidated,
            "deleted":      len(deleted_ids),
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            f"MEMORY_CONSOLIDATOR: complete — "
            f"{consolidated} campaigns created, "
            f"{len(deleted_ids)} episodes deleted"
        )

        try:
            await broadcast_fn({
                "type":         "memory_consolidated",
                "consolidated": consolidated,
                "deleted":      len(deleted_ids),
                "timestamp":    report["timestamp"],
            })
        except Exception:
            pass

        return report

    except Exception as e:
        logger.error(f"MEMORY_CONSOLIDATOR: {e}")
        return {"error": str(e)}


async def start_consolidation_scheduler(
    broadcast_fn,
    ollama_client,
    model: str,
) -> None:
    """Background task: consolidate every 24 hours during idle."""
    try:
        from core.cancel_bus import get_active_operations
    except Exception:
        get_active_operations = lambda: {}   # noqa: E731

    while True:
        await asyncio.sleep(_CONSOLIDATION_INTERVAL)
        try:
            ops = get_active_operations()
        except Exception:
            ops = {}
        if not ops:
            await consolidate_memory(broadcast_fn, ollama_client, model)
        else:
            logger.debug(
                "MEMORY_CONSOLIDATOR: skipped — operations active"
            )
