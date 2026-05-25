"""
aura/server.py — AURA WebSocket telemetry server (v27.0).

v27.0 additions:
  - HMAC telemetry authentication: signed external events are verified and unwrapped
    in broadcast() before fan-out; tampered or unsigned external events are dropped.
  - Temporal correlator integration: all events are ingested into TemporalCorrelator.
  - HUD bidirectionality: browser can send commands via WebSocket; high-risk commands
    require NATO OTP challenge before dispatch.
  - attach_executor(): executor reference stored for HUD command dispatch.
  - _verified_broadcast: exported alias for broadcast() for use by telemetry sources.
"""

import asyncio
import json
import re
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

# Allowlist for HUD-originated commands
_HUD_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "sliver_list_sessions",
    "sliver_interact",
    "sliver_generate_implant",
    "aura_get_incidents",
    "canary_status",
    "rf_bridge_status",
    "run_nmap",
    "run_whois",
    # v33.0 Adversarial Intelligence
    "emulate_technique",
    "emulate_chain",
    "export_stix",
    "get_coverage",
})
_HIGH_RISK_HUD:   frozenset[str] = frozenset({
    "sliver_interact", "sliver_generate_implant", "emulate_chain",
})
_MEDIUM_RISK_HUD: frozenset[str] = frozenset({"run_nmap", "emulate_technique"})

# Simple validator for scan targets / domains (no shell metacharacters)
_TARGET_RE = re.compile(r'^[a-zA-Z0-9.\-:/\[\]_]{1,100}$')


class BroadcastManager:
    """
    Thread-safe (asyncio-safe) fan-out broadcaster.
    Set mutations between await points are safe without a lock (asyncio is single-threaded).
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
        for ws in set(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._clients -= dead


# ── Module-level singletons ──────────────────────────────────────────────────
manager = BroadcastManager()

# Pending OTP / confirm futures keyed by id(ws)
_pending_ws_responses: dict[int, asyncio.Future] = {}

# Executor reference injected by main.py
_executor_ref = None


def attach_executor(executor) -> None:
    """Store a reference to the ToolExecutor for HUD command dispatch."""
    global _executor_ref
    _executor_ref = executor


async def telemetry_broadcaster() -> None:
    psutil.cpu_percent(interval=None)
    while True:
        interval = 10.0 if _loading else 2.0
        await asyncio.sleep(interval)
        vm = psutil.virtual_memory()
        await manager.broadcast({
            "type":        "telemetry",
            "cpu_pct":     psutil.cpu_percent(interval=None),
            "ram_pct":     vm.percent,
            "ram_used_gb": round(vm.used  / 1_000_000_000, 2),
            "ram_total_gb":round(vm.total / 1_000_000_000, 2),
        })


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from core.correlator import correlator
    # Attach correlator to raw manager.broadcast to avoid recursive re-ingestion
    correlator.attach(manager.broadcast)
    asyncio.create_task(correlator.start(), name="correlator")

    task = asyncio.create_task(telemetry_broadcaster())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="AURA", version="27.0",
    lifespan=_lifespan, docs_url=None, redoc_url=None,
)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── HUD Command Handlers ──────────────────────────────────────────────────────

async def _dispatch_hud_command(cmd: str, args: dict, executor, broadcast_fn) -> dict:
    """Route validated HUD commands to appropriate tool functions."""
    try:
        if cmd == "aura_get_incidents":
            from core.correlator import correlator
            return {"incidents": correlator.get_active_incidents()}

        elif cmd == "sliver_list_sessions":
            try:
                from tools.sliver_bridge import _get_client, list_sessions
                client = await _get_client()
                if not client:
                    return {"error": "Sliver not connected"}
                return {"sessions": await list_sessions(client, broadcast_fn)}
            except Exception as e:
                return {"error": str(e)}

        elif cmd == "run_nmap":
            target = str(args.get("target", ""))[:50]
            if not target or not _TARGET_RE.match(target):
                return {"error": "Invalid target format"}
            if executor is None:
                return {"error": "Executor not available"}
            result = await executor.execute_shell(
                f"nmap -sV --top-ports 100 {target}",
                reasoning="HUD-initiated nmap scan",
            )
            return result

        elif cmd == "run_whois":
            domain = str(args.get("domain", "") or args.get("target", ""))[:100]
            if not domain or not _TARGET_RE.match(domain):
                return {"error": "Invalid domain format"}
            if executor is None:
                return {"error": "Executor not available"}
            result = await executor.execute_shell(
                f"whois {domain}",
                reasoning="HUD-initiated whois lookup",
            )
            return result

        # ── v33.0 Adversarial Intelligence ───────────────────────────────────
        elif cmd == "emulate_technique":
            technique = str(args.get("technique", ""))[:15]
            from core.adversary_emulator import adversary_emulator
            return await adversary_emulator.emulate_technique(technique)

        elif cmd == "emulate_chain":
            chain = str(args.get("chain", ""))[:30]
            from core.adversary_emulator import adversary_emulator
            results = await adversary_emulator.emulate_chain(chain)
            return {"results": results}

        elif cmd == "get_coverage":
            from core.attck_coverage import get_coverage_matrix
            return get_coverage_matrix()

        elif cmd == "export_stix":
            from core.correlator import correlator
            incidents = correlator.get_active_incidents()
            if incidents:
                from tools.ioc_extractor import export_incident_stix
                path = await export_incident_stix(incidents[0], broadcast_fn)
                return {"exported": path}
            return {"error": "no active incidents to export"}

        return {"error": f"Handler not implemented for '{cmd}'"}
    except Exception as e:
        return {"error": str(e)}


async def _handle_hud_command(
    raw: dict,
    ws: WebSocket,
    executor,
    broadcast_fn,
) -> None:
    """
    Process a command sent FROM the AURA HUD browser.
    Validates against allowlist, applies trust/OTP gate, dispatches, sends result back.
    """
    from core.feed_sanitizer import sanitize_for_hud, check_prompt_injection, SanitizationError

    cmd    = str(raw.get("cmd", "")).strip()
    args   = raw.get("args", {}) or {}
    req_id = str(raw.get("request_id", ""))[:32]

    try:
        check_prompt_injection(cmd, source="hud_command")
    except SanitizationError:
        await ws.send_json({
            "type": "hud_command_error", "request_id": req_id,
            "error": "Command rejected by sanitizer",
        })
        return

    if cmd not in _HUD_ALLOWED_COMMANDS:
        await ws.send_json({
            "type": "hud_command_error", "request_id": req_id,
            "error": f"Command '{sanitize_for_hud(cmd)}' not in HUD allowlist",
        })
        return

    # ── High-risk: require NATO OTP ──────────────────────────────────────────
    if cmd in _HIGH_RISK_HUD:
        otp = None
        if executor is not None and hasattr(executor, "_te") and hasattr(executor._te, "auth"):
            try:
                otp = executor._te.auth.generate_otp()
            except Exception:
                pass
        if otp:
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            _pending_ws_responses[id(ws)] = fut
            await ws.send_json({
                "type":       "hud_otp_challenge",
                "request_id": req_id,
                "otp":        otp.phonetic() if hasattr(otp, "phonetic") else str(otp),
                "ttl":        30,
            })
            try:
                response = await asyncio.wait_for(fut, timeout=30.0)
                spoken   = response.get("otp_response", "")
                ok, reason = executor._te.auth.verify_otp(spoken)
                if not ok:
                    await ws.send_json({
                        "type": "hud_command_error", "request_id": req_id,
                        "error": f"OTP failed: {reason}",
                    })
                    return
            except asyncio.TimeoutError:
                _pending_ws_responses.pop(id(ws), None)
                await ws.send_json({
                    "type": "hud_command_error", "request_id": req_id,
                    "error": "OTP challenge timed out",
                })
                return
        else:
            await ws.send_json({
                "type": "hud_command_error", "request_id": req_id,
                "error": "OTP subsystem unavailable for high-risk command",
            })
            return

    # ── Medium-risk: require explicit confirmation ────────────────────────────
    elif cmd in _MEDIUM_RISK_HUD:
        args_preview = sanitize_for_hud(str(args)[:60])
        fut_c: asyncio.Future = asyncio.get_event_loop().create_future()
        _pending_ws_responses[id(ws)] = fut_c
        await ws.send_json({
            "type":       "hud_confirm_required",
            "request_id": req_id,
            "message":    f"Confirm: {sanitize_for_hud(cmd)} {args_preview}",
        })
        try:
            confirm = await asyncio.wait_for(fut_c, timeout=15.0)
            if confirm.get("confirmed") is not True:
                await ws.send_json({
                    "type": "hud_command_error", "request_id": req_id,
                    "error": "Command declined",
                })
                return
        except asyncio.TimeoutError:
            _pending_ws_responses.pop(id(ws), None)
            return

    # ── Dispatch ─────────────────────────────────────────────────────────────
    result = await _dispatch_hud_command(cmd, args, executor, broadcast_fn)
    await ws.send_json({
        "type":       "hud_command_result",
        "request_id": req_id,
        "cmd":        cmd,
        "result":     result,
    })


# ── WebSocket endpoint (bidirectional v27.0) ─────────────────────────────────

@app.websocket("/ws")
async def _ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps({
            "type":      "system",
            "message":   "AURA pipeline connected.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception:
        pass

    try:
        while True:
            try:
                raw_text = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                try:
                    raw = json.loads(raw_text)
                    if not isinstance(raw, dict):
                        continue
                    if "cmd" in raw:
                        asyncio.create_task(
                            _handle_hud_command(raw, ws, _executor_ref, broadcast)
                        )
                    elif "otp_response" in raw or "confirmed" in raw:
                        fut = _pending_ws_responses.pop(id(ws), None)
                        if fut and not fut.done():
                            fut.set_result(raw)
                except (json.JSONDecodeError, ValueError):
                    pass
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(ws)
        _pending_ws_responses.pop(id(ws), None)


# ── Static UI routes ──────────────────────────────────────────────────────────

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


# ── Public broadcast coroutine ────────────────────────────────────────────────

async def broadcast(event: dict) -> None:
    """
    Top-level broadcast coroutine.

    External events (carrying __src) are HMAC-verified and unwrapped before fan-out.
    All events are ingested into the temporal correlator for compound incident detection.
    Always awaitable; silently does nothing when no clients are connected.
    """
    if "__src" in event:
        try:
            from core.telemetry_auth import verify_and_unwrap
            verified = verify_and_unwrap(event)
            if verified is None:
                return
            event = verified
        except Exception as exc:
            logger.debug(f"AURA: telemetry auth error: {exc}")
            return

    try:
        from core.correlator import correlator as _corr
        asyncio.create_task(_corr.ingest(event))
    except Exception:
        pass

    await manager.broadcast(event)


# Exported alias — external telemetry sources use this name explicitly
_verified_broadcast = broadcast
