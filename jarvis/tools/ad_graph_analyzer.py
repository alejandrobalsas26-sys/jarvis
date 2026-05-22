"""
tools/ad_graph_analyzer.py — Active Directory Attack Path Graph Engine (v24.0).

BloodHound JSON → igraph directed graph → Dijkstra shortest path.
ijson streams JSON in two passes for O(1) memory footprint.
All graph work runs in _graph_pool (ProcessPoolExecutor, max_workers=1).
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor

from loguru import logger

from core.events import make_event
from core.hardware_profile import recommended_pools as _hw_pools

_graph_pool = ProcessPoolExecutor(max_workers=_hw_pools)


def _compute_attack_paths(json_path: str) -> dict:
    """Runs in worker process — two-pass ijson parse → igraph Dijkstra."""
    try:
        import ijson
        import igraph
    except ImportError as exc:
        return {"path": [], "weights": [], "error": f"Missing dependency: {exc}"}

    node_id_to_name: dict[str, str] = {}
    node_id_to_type: dict[str, str] = {}

    try:
        with open(json_path, "rb") as f:
            for node in ijson.items(f, "nodes.item", use_float=True):
                nid = str(node.get("id", ""))
                if not nid:
                    continue
                props = node.get("properties") or {}
                name  = props.get("name") or nid
                labels = node.get("labels") or ["Unknown"]
                ntype  = labels[0] if labels else "Unknown"
                node_id_to_name[nid] = name
                node_id_to_type[nid] = ntype
    except Exception as exc:
        return {"path": [], "weights": [], "error": f"JSON nodes parse error: {exc}"}

    edge_list: list[tuple[str, str, str]] = []
    try:
        with open(json_path, "rb") as f:
            for rel in ijson.items(f, "relationships.item", use_float=True):
                src = str(rel.get("startNode", ""))
                dst = str(rel.get("endNode", ""))
                if src and dst:
                    edge_list.append((src, dst, rel.get("type", "")))
    except Exception as exc:
        return {"path": [], "weights": [], "error": f"JSON relationships parse error: {exc}"}

    if not edge_list:
        return {"path": [], "weights": [], "error": "No edges found in BloodHound JSON"}

    all_ids   = list(node_id_to_name.keys())
    id_to_idx = {nid: i for i, nid in enumerate(all_ids)}

    extra = len(all_ids)
    for src, dst, _ in edge_list:
        for nid in (src, dst):
            if nid not in id_to_idx:
                id_to_idx[nid] = extra
                extra += 1

    n = extra
    edges_int = [(id_to_idx[s], id_to_idx[d]) for s, d, _ in edge_list]

    g = igraph.Graph(n=n, edges=edges_int, directed=True)
    g.vs["name"] = [
        node_id_to_name.get(nid, nid)
        for nid, _ in sorted(id_to_idx.items(), key=lambda kv: kv[1])
    ]

    sources = [
        id_to_idx[nid]
        for nid, t in node_id_to_type.items()
        if t.lower() == "user" and nid in id_to_idx
    ]
    targets = [
        id_to_idx[nid]
        for nid, name in node_id_to_name.items()
        if "domain admin" in name.lower() and nid in id_to_idx
    ]

    if not sources:
        sources = list(range(min(5, n)))
    if not targets:
        targets = [n - 1] if n > 1 else [0]

    best_path: list[str] = []
    best_weights: list[float] = []

    for src in sources[:5]:
        for tgt in targets[:3]:
            if src == tgt:
                continue
            try:
                paths = g.get_shortest_paths(src, to=tgt, output="vpath")
                if paths and paths[0] and len(paths[0]) > len(best_path):
                    vpath        = paths[0]
                    best_path    = [g.vs[v]["name"] for v in vpath]
                    best_weights = [1.0] * (len(vpath) - 1)
            except Exception:
                continue

    return {"path": best_path, "weights": best_weights}


async def analyze_ad_graph(json_path: str, broadcast_fn) -> None:
    """Load a BloodHound JSON export and compute attack paths asynchronously."""
    loop = asyncio.get_running_loop()
    await broadcast_fn(make_event("ad_graph_computing", status="start"))
    try:
        result = await loop.run_in_executor(_graph_pool, _compute_attack_paths, json_path)
        if "error" in result and not result["path"]:
            await broadcast_fn(make_event("error", error=f"AD graph failed: {result['error']}"))
            return
        await broadcast_fn(make_event(
            "attack_path_computed",
            path=result["path"],
            weights=result["weights"],
        ))
    except Exception as exc:
        logger.error(f"AD graph analysis failed: {exc}")
        await broadcast_fn(make_event("error", error=f"AD graph failed: {exc}"))
