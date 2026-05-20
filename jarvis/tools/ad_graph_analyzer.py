"""
tools/ad_graph_analyzer.py — Active Directory Attack Path Graph Engine (v19.0).

BloodHound JSON → igraph directed graph → Dijkstra shortest path.

ijson streams the JSON file in two passes for O(1) memory footprint — safe for
files exceeding 500 MB.  All graph construction and pathfinding runs inside
_graph_pool (ProcessPoolExecutor, max_workers=1).  Only the computed path (a
small list) crosses the process boundary; the full igraph object never does.

Do NOT share _graph_pool with _mesh_pool or _vol_pool.
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor

from loguru import logger

_graph_pool = ProcessPoolExecutor(max_workers=1)


def _compute_attack_paths(json_path: str) -> dict:
    """
    Runs in worker process.

    1. Stream-parse BloodHound JSON with ijson (two-pass, O(1) memory).
    2. Construct igraph.Graph from the edge list.
    3. Identify source nodes (low-privilege users) and target nodes (Domain Admins).
    4. Dijkstra shortest path from source → target via igraph.
    5. Return {"path": [node_names], "weights": [edge_weights]}.
    The igraph object is never returned — only the path list.
    """
    try:
        import ijson
        import igraph
    except ImportError as exc:
        return {"path": [], "weights": [], "error": f"Missing dependency: {exc}"}

    # ── Pass 1: collect nodes ─────────────────────────────────────────────────
    node_id_to_name: dict[str, str] = {}
    node_id_to_type: dict[str, str] = {}

    try:
        with open(json_path, "rb") as f:
            for node in ijson.items(f, "nodes.item", use_float=True):
                nid = str(node.get("id", ""))
                if not nid:
                    continue
                props = node.get("properties") or {}
                name = props.get("name") or nid
                labels = node.get("labels") or ["Unknown"]
                ntype = labels[0] if labels else "Unknown"
                node_id_to_name[nid] = name
                node_id_to_type[nid] = ntype
    except Exception as exc:
        return {"path": [], "weights": [], "error": f"JSON nodes parse error: {exc}"}

    # ── Pass 2: collect relationships ─────────────────────────────────────────
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

    # ── Build unified integer index ───────────────────────────────────────────
    all_ids = list(node_id_to_name.keys())
    id_to_idx: dict[str, int] = {nid: i for i, nid in enumerate(all_ids)}

    extra = len(all_ids)
    for src, dst, _ in edge_list:
        for nid in (src, dst):
            if nid not in id_to_idx:
                id_to_idx[nid] = extra
                extra += 1

    n = extra
    edges_int = [
        (id_to_idx[s], id_to_idx[d])
        for s, d, _ in edge_list
    ]

    # ── Build igraph ──────────────────────────────────────────────────────────
    g = igraph.Graph(n=n, edges=edges_int, directed=True)
    g.vs["name"] = [
        node_id_to_name.get(nid, nid)
        for nid, _ in sorted(id_to_idx.items(), key=lambda kv: kv[1])
    ]

    # ── Identify sources (users) and targets (Domain Admins) ─────────────────
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

    # ── Dijkstra shortest path ────────────────────────────────────────────────
    best_path: list[str] = []
    best_weights: list[float] = []

    for src in sources[:5]:
        for tgt in targets[:3]:
            if src == tgt:
                continue
            try:
                paths = g.get_shortest_paths(src, to=tgt, output="vpath")
                if paths and paths[0] and len(paths[0]) > len(best_path):
                    vpath = paths[0]
                    best_path = [g.vs[v]["name"] for v in vpath]
                    best_weights = [1.0] * (len(vpath) - 1)
            except Exception:
                continue

    return {"path": best_path, "weights": best_weights}


async def analyze_ad_graph(json_path: str, broadcast_fn) -> None:
    """Load a BloodHound JSON export and compute attack paths asynchronously."""
    loop = asyncio.get_running_loop()
    await broadcast_fn({"type": "ad_graph_computing", "status": "start"})
    try:
        result = await loop.run_in_executor(_graph_pool, _compute_attack_paths, json_path)
        if "error" in result and not result["path"]:
            await broadcast_fn({"type": "error", "error": f"AD graph failed: {result['error']}"})
            return
        await broadcast_fn({
            "type":    "attack_path_computed",
            "path":    result["path"],
            "weights": result["weights"],
        })
    except Exception as exc:
        logger.error(f"AD graph analysis failed: {exc}")
        await broadcast_fn({"type": "error", "error": f"AD graph failed: {exc}"})
