"""tools/mesh_generator.py — Isolated 3D mesh inference worker (DirectML / CPU).

The inference worker runs inside a dedicated ProcessPoolExecutor so that:
  - DirectML COM objects are allocated on a separate OS process heap.
  - A model crash or OOM cannot destabilize the main asyncio event loop.
  - The worker process exits cleanly, reclaiming all GPU allocations.

TripoSR must be installed separately:
  git clone https://github.com/VAST-AI-Research/TripoSR
  pip install -e ./TripoSR
"""

import asyncio
import gc
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from loguru import logger

_mesh_pool = ProcessPoolExecutor(max_workers=1)

_MESHES_DIR = Path(__file__).parent.parent / "static" / "meshes"


async def generate_mesh(image_path: str, broadcast_fn) -> str:
    """Offload TripoSR 3D reconstruction to an isolated worker process.

    Broadcasts mesh_generating / mesh_ready / error events to the AURA HUD.
    """
    await broadcast_fn({"type": "mesh_generating", "status": "start"})
    _MESHES_DIR.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(_mesh_pool, _inference_worker, image_path)
        rel = Path(path).name
        await broadcast_fn({"type": "mesh_ready", "file_url": f"/static/meshes/{rel}"})
        logger.info(f"mesh_generator: written to {path}")
        return path
    except Exception as exc:
        await broadcast_fn({"type": "error", "error": f"Mesh generation failed: {exc}"})
        raise


def _inference_worker(image_path: str) -> str:
    """Runs in isolated worker process — all GPU allocations freed on process exit."""
    out_dir = Path(__file__).parent.parent / "static" / "meshes"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / "target.glb")

    try:
        import torch_directml
        import trimesh
    except ImportError as exc:
        raise RuntimeError(
            f"torch-directml or trimesh not installed: {exc}. "
            "pip install torch-directml trimesh"
        )

    try:
        # TripoSR is not on PyPI — clone and install from:
        # https://github.com/VAST-AI-Research/TripoSR
        from tsr.system import TSR
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            f"TripoSR not available: {exc}. "
            "Clone https://github.com/VAST-AI-Research/TripoSR and pip install -e ."
        )

    device = torch_directml.device()
    model = TSR.from_pretrained(
        "stabilityai/TripoSR",
        config_name="config.yaml",
        weight_name="model.ckpt",
    )
    model = model.to(device)
    model.eval()

    try:
        image = Image.open(image_path).convert("RGBA")
        scene_codes = model([image], device=device)
        meshes = model.extract_mesh(scene_codes, resolution=256)
        verts = meshes[0].verts_list()[0].cpu().numpy()
        faces = meshes[0].faces_list()[0].cpu().numpy()
        trimesh.Trimesh(vertices=verts, faces=faces).export(out_path)
        return out_path
    finally:
        del model
        gc.collect()
