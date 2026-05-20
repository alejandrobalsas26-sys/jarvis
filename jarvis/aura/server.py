"""
aura/server.py — AURA WebSocket telemetry server (v15.2).

BroadcastManager fans out JSON events from HardenedExecutor to every connected
browser client.  The module-level `broadcast()` coroutine is the single import
point for all producers:

    from aura.server import broadcast
    await broadcast({"type": "tool_invoked", "tool": "network_scan", ...})

The FastAPI `app` is launched by main.py via:

    uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765)).serve()

wrapped in asyncio.create_task() so it shares the existing event loop.
"""

import asyncio
import json
import psutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

_INDEX_PATH = Path(__file__).parent / "index.html"
_STATIC_DIR = Path(__file__).parent.parent / "static"
_MESHES_DIR = _STATIC_DIR / "meshes"
_MESHES_DIR.mkdir(parents=True, exist_ok=True)

_loading: bool = False


class BroadcastManager:
    """
    Thread-safe (asyncio-safe) fan-out broadcaster.

    Clients are stored in a plain set.  Because asyncio is single-threaded,
    set mutations between await points are safe without an explicit lock.
    We snapshot `_clients` before iteration so that connects/disconnects
    that arrive during a broadcast don't corrupt the loop.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.debug(f"AURA: +client  total={len(self._clients)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.debug(f"AURA: -client  total={len(self._clients)}")

    async def broadcast(self, event: dict) -> None:
        """Send `event` to every connected client; prune dead sockets."""
        if not self._clients:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        dead: set[WebSocket] = set()
        for ws in set(self._clients):          # snapshot before first await
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._clients -= dead                  # atomic removal after loop


# ── Module-level singleton ────────────────────────────────────────────────────
manager = BroadcastManager()


async def telemetry_broadcaster() -> None:
    psutil.cpu_percent(interval=None)          # prime the rolling counter
    while True:
        interval = 10.0 if _loading else 2.0
        await asyncio.sleep(interval)
        vm = psutil.virtual_memory()
        await manager.broadcast({
            "type": "telemetry",
            "cpu_pct": psutil.cpu_percent(interval=None),
            "ram_pct": vm.percent,
            "ram_used_gb": round(vm.used / 1_000_000_000, 2),
            "ram_total_gb": round(vm.total / 1_000_000_000, 2),
        })


@asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(telemetry_broadcaster())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="AURA", version="15.2", lifespan=_lifespan, docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def _ws_endpoint(ws: WebSocket) -> None:
    """
    Keep the connection alive while waiting for server-push events.
    AURA is push-only; any text the client sends is silently drained.
    """
    await manager.connect(ws)
    # Announce connection to the new client immediately
    try:
        await ws.send_text(json.dumps({
            "type": "system",
            "message": "AURA pipeline connected.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception:
        pass

    try:
        while True:
            await ws.receive_text()           # drain client pings / keep-alive
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Static UI routes ─────────────────────────────────────────────────────────

@app.get("/")
async def _index() -> FileResponse:
    return FileResponse(_INDEX_PATH)


@app.get("/ui")
async def _ui() -> FileResponse:
    return FileResponse(_INDEX_PATH)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def _health() -> dict:
    return {"status": "ok", "clients": len(manager._clients)}


# ── Public broadcast coroutine ─────────────────────────────────────────────────

async def broadcast(event: dict) -> None:
    """
    Top-level coroutine imported by HardenedExecutor (and any other producer).

    Always awaitable; silently does nothing when no clients are connected.
    """
    await manager.broadcast(event)
