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
import ipaddress
import json
import re
import secrets
import psutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
    # v35.0 emergency abort — no OTP required
    "voice_abort",
    # v36.0 Predictive Cognition
    "swap_deep",
    "swap_fast",
    "multi_agent_analyze",
    "generate_report",
    "consolidate_memory",
    # v39.0 self-healing remediator
    "execute_mitigation",
    # v43.0 BIFROST PROTOCOL
    "deploy_sigma_rule",
    "run_bas_scenario",
    "get_coverage",
})
_HIGH_RISK_HUD:   frozenset[str] = frozenset({
    "sliver_interact", "sliver_generate_implant", "emulate_chain",
    "execute_mitigation", "run_bas_scenario", "deploy_sigma_rule",
})
_MEDIUM_RISK_HUD: frozenset[str] = frozenset({"run_nmap", "emulate_technique"})

# Simple validator for scan targets / domains (no shell metacharacters)
_TARGET_RE = re.compile(r'^[a-zA-Z0-9.\-:/\[\]_]{1,100}$')


# ── WebSocket Origin allowlist (CSWSH defense) ───────────────────────────────

def _origin_is_loopback(origin: str) -> bool:
    """True only if the Origin's scheme is http(s) and its host is loopback."""
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_allowed(origin: str | None) -> bool:
    """
    Fail-closed CSWSH gate for the AURA WebSocket handshake.

      * Missing / empty / "null" Origin              -> rejected
      * Loopback origin (localhost / 127.0.0.0/8/::1) -> allowed (the local HUD)
      * Origin in settings.aura_allowed_origins       -> allowed (operator opt-in)
      * Foreign host / malformed / non-http scheme    -> rejected

    Browsers always send Origin on WebSocket handshakes, so a missing Origin is
    treated as an untrusted (non-browser / cross-site) caller and rejected.
    """
    if not origin:
        return False
    candidate = origin.strip().rstrip("/")
    if not candidate or candidate.lower() == "null":
        return False
    if _origin_is_loopback(candidate):
        return True
    try:
        from core.config import settings
        return candidate in set(settings.get_aura_allowed_origins())
    except Exception:
        return False


# ── WebSocket per-session token auth ─────────────────────────────────────────

def _resolve_ws_token() -> str:
    """Session token for the /ws handshake.

    Uses the operator-configured token (settings.aura_ws_token) when set — a
    trusted local-config value — otherwise a per-process random token. Never
    sourced from LLM / tool input.
    """
    try:
        from core.config import settings
        configured = (settings.aura_ws_token or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return secrets.token_urlsafe(32)


_AURA_WS_TOKEN: str = _resolve_ws_token()


def get_ws_token() -> str:
    """Return the current session token (for the local HUD / trusted callers)."""
    return _AURA_WS_TOKEN


def _extract_ws_token(ws) -> "str | None":
    """Read the session token from the handshake cookie, then the query string."""
    try:
        tok = ws.cookies.get("aura_token")
        if tok:
            return tok
    except Exception:
        pass
    try:
        return ws.query_params.get("token")
    except Exception:
        return None


def _ws_token_valid(token: "str | None") -> bool:
    """Constant-time compare against the session token; empty/absent → invalid."""
    if not token or not _AURA_WS_TOKEN:
        return False
    return secrets.compare_digest(str(token), _AURA_WS_TOKEN)


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
            from core.attck_coverage import get_coverage_matrix as _attck_matrix
            try:
                from core.purple_coordinator import (
                    get_coverage_matrix as _purple_matrix,
                    get_coverage_summary as _purple_summary,
                )
                return {
                    "attck":           _attck_matrix(),
                    "purple_matrix":   _purple_matrix()[:20],
                    "purple_summary":  _purple_summary(),
                }
            except Exception:
                return _attck_matrix()

        elif cmd == "voice_abort":
            # v35.0 — operator emergency abort via HUD ABORT button
            from core.cancel_bus import cancel_all
            count = cancel_all()
            return {"cancelled": count, "status": "aborted"}

        elif cmd == "export_stix":
            from core.correlator import correlator
            incidents = correlator.get_active_incidents()
            if incidents:
                from tools.ioc_extractor import export_incident_stix
                path = await export_incident_stix(incidents[0], broadcast_fn)
                return {"exported": path}
            return {"error": "no active incidents to export"}

        # ── v36.0 Predictive Cognition ───────────────────────────────────────
        elif cmd == "swap_deep":
            from core.model_swapper import swap_to_deep
            ok = await swap_to_deep(broadcast_fn)
            return {"swapped": ok, "mode": "deep"}

        elif cmd == "swap_fast":
            from core.model_swapper import swap_to_fast
            ok = await swap_to_fast(broadcast_fn)
            return {"swapped": ok, "mode": "fast"}

        elif cmd == "multi_agent_analyze":
            from core.agent_orchestrator import orchestrator
            from core.correlator        import correlator
            task    = str(args.get("task", "Analyze current incident"))[:200]
            agents  = args.get("agents") or ["ThreatIntelligence", "IncidentResponder"]
            if not isinstance(agents, list):
                agents = ["ThreatIntelligence", "IncidentResponder"]
            agents = [str(a)[:40] for a in agents][:5]
            incidents = correlator.get_active_incidents()
            ctx       = incidents[0] if incidents else {}

            # V63 M4 — prefer the controlled specialist team runtime (bounded
            # concurrency, shared blackboard, structured conflict detection,
            # provenance). Falls back to the legacy sequential orchestrator on
            # any error so this live command never regresses.
            async def _run_controlled_team() -> None:
                try:
                    from core.specialist_runtime import team_runtime
                    await team_runtime.run_legacy_agents(task, agents, ctx)
                except Exception as exc:
                    logger.debug(f"AURA: team_runtime fallback → orchestrator: {exc}")
                    await orchestrator.run_task(task, agents, ctx)

            asyncio.create_task(_run_controlled_team())
            return {"status": "started", "agents": agents}

        elif cmd == "plan_task":
            # V63 M3 — operator-triggered bounded task-graph planning. Builds a
            # per-turn TaskDecision from the objective, and only runs a graph when
            # planning is actually warranted (fast path is never forced through it).
            objective = str(args.get("objective", args.get("task", "")))[:400].strip()
            if not objective:
                return {"error": "plan_task requires an 'objective'"}
            from core.agent_planner import agent_planner, should_plan
            from core.agent_runtime import assemble_task_decision
            td = assemble_task_decision(objective)
            explicit = bool(args.get("force"))
            if not should_plan(td, explicit=explicit):
                return {"status": "skipped",
                        "reason": "objective does not warrant multi-step planning",
                        "domain": td.domain.value}

            async def _run_plan() -> None:
                try:
                    result = await agent_planner.plan_and_run(objective, td)
                    await broadcast_fn({
                        "type": "plan_complete",
                        "objective": objective[:120],
                        "graph_status": result.status,
                        "completed": result.completed,
                        "failed": result.failed,
                        "elapsed_s": result.elapsed_s,
                    })
                except Exception as exc:
                    logger.debug(f"AURA: plan_task error: {exc}")

            asyncio.create_task(_run_plan())
            return {"status": "started", "domain": td.domain.value,
                    "planning": True}

        elif cmd == "capability_inventory":
            # V63 — honest report of which security tools are installed here.
            from core.capabilities import registry as _cap_registry
            inv = _cap_registry.inventory()
            return {"capabilities": inv,
                    "available": [c["name"] for c in inv if c["available"]],
                    "executable": [c["name"] for c in inv
                                   if c["available"] and c["executable"]]}

        elif cmd == "run_capability":
            # V63 — operator-triggered typed security capability, routed through
            # the ToolExecutor's authority/HITL/audit gates (no shell bypass).
            name = str(args.get("name", "")).strip()
            params = args.get("params") or {}
            if not name or not isinstance(params, dict):
                return {"error": "run_capability requires 'name' and dict 'params'"}
            tex = executor or _executor_ref
            if tex is None:
                return {"error": "tool executor unavailable"}
            result = await tex.run_capability(name, params, "aura:run_capability")
            return {"result": result}

        elif cmd == "generate_report":
            from core.correlator        import correlator
            from core.incident_reporter import generate_incident_report
            from core.agent_orchestrator import orchestrator
            incidents = correlator.get_active_incidents()
            if not incidents:
                return {"error": "no active incidents"}
            asyncio.create_task(generate_incident_report(
                incidents[0], [], broadcast_fn,
                orchestrator._ollama_client,
                orchestrator._deep_model,
            ))
            return {"status": "generating", "incident_id": incidents[0].get("incident_id")}

        elif cmd == "consolidate_memory":
            from core.memory_consolidator import consolidate_memory
            from core.agent_orchestrator  import orchestrator
            asyncio.create_task(consolidate_memory(
                broadcast_fn,
                orchestrator._ollama_client,
                orchestrator._deep_model,
            ))
            return {"status": "consolidating"}

        # ── v39.0 Self-healing remediator ────────────────────────────────────
        elif cmd == "execute_mitigation":
            script_path = str(args.get("script_path", ""))[:300]
            from core.auto_remediator import execute_mitigation
            asyncio.create_task(
                execute_mitigation(script_path, broadcast_fn, _executor_ref)
            )
            return {"status": "otp_challenge_issued"}

        # ── v43.0 BIFROST PROTOCOL ───────────────────────────────────────────
        elif cmd == "deploy_sigma_rule":
            draft_path = str(args.get("draft_path", ""))[:300]
            from core.detection_engineer import deploy_approved_rule
            asyncio.create_task(deploy_approved_rule(draft_path, broadcast_fn))
            return {"status": "deploying"}

        elif cmd == "run_bas_scenario":
            target   = str(args.get("target",   "192.168.1.100"))[:50]
            scenario = str(args.get("scenario", "apt_chain"))[:30]
            if not _TARGET_RE.match(target):
                return {"error": "Invalid target format"}
            from tools.breach_simulator import run_full_bas_scenario
            asyncio.create_task(run_full_bas_scenario(
                target, broadcast_fn, scenario,
            ))
            return {"status": "started", "scenario": scenario, "target": target}

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

    # ── High-risk: out-of-band operator approval (F1b) ────────────────────────
    # NEVER accept approval over the same WebSocket that requested the dangerous
    # action. Route the challenge to the executor's out-of-band HITL/NATO gate
    # (operator console / voice). If no such channel exists, refuse and require
    # out-of-band approval rather than trusting this socket.
    if cmd in _HIGH_RISK_HUD:
        challenge = getattr(executor, "_challenge", None)
        if not callable(challenge):
            await ws.send_json({
                "type":       "hud_approval_required_out_of_band",
                "request_id": req_id,
                "cmd":        sanitize_for_hud(cmd),
                "error":      "approval_required_out_of_band",
            })
            return
        await ws.send_json({
            "type":       "hud_approval_pending_out_of_band",
            "request_id": req_id,
            "cmd":        sanitize_for_hud(cmd),
            "message":    "High-risk command requires operator approval at the JARVIS console.",
        })
        granted, _audit = await challenge(f"hud:{cmd}", sanitize_for_hud(str(args)[:120]))
        if not granted:
            await ws.send_json({
                "type": "hud_command_error", "request_id": req_id,
                "error": "High-risk command denied (out-of-band approval).",
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
    # CSWSH defense: reject the handshake unless the browser Origin is trusted
    # (loopback by default, plus any operator-configured origins). Rejection
    # happens BEFORE accept() so no socket is ever established for a bad Origin.
    origin = ws.headers.get("origin")
    if not _origin_allowed(origin):
        logger.warning(f"AURA: rejected /ws handshake — disallowed Origin: {origin!r}")
        await ws.close(code=1008)  # 1008 = policy violation
        return
    # Per-session token auth: the HUD receives an HttpOnly cookie when it loads
    # the page (set by the index route); browsers replay it on the same-origin
    # handshake. Non-browser clients may pass ?token=. Missing/invalid → reject.
    if not _ws_token_valid(_extract_ws_token(ws)):
        logger.warning("AURA: rejected /ws handshake — missing/invalid session token")
        await ws.close(code=1008)
        return
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

def _hud_response() -> FileResponse:
    """Serve the HUD and hand the browser the /ws session token as an HttpOnly,
    SameSite=strict cookie — replayed automatically on the same-origin handshake
    and unreadable to page JavaScript (XSS cannot exfiltrate it)."""
    resp = FileResponse(_INDEX_PATH)
    resp.set_cookie(
        "aura_token", _AURA_WS_TOKEN,
        httponly=True, samesite="strict", path="/",
    )
    return resp


@app.get("/")
async def _index() -> FileResponse:
    return _hud_response()


@app.get("/ui")
async def _ui() -> FileResponse:
    return _hud_response()


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
