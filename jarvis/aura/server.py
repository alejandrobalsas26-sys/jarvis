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
import contextlib
import ipaddress
import json
import re
import secrets
import psutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

_INDEX_PATH = Path(__file__).parent / "index.html"
_STATIC_DIR = Path(__file__).parent.parent / "static"
# V69 M61 RC1 — the ``_MESHES_DIR`` constant and its import-time ``mkdir`` are
# gone: nothing in the codebase read that constant, it existed only for the side
# effect, and the mount below now handles an absent asset tree explicitly.

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
    # V67 M31 — live operator command center (READ-ONLY, bounded, redacted)
    "ops_command_center",
    # V67 M32 — grounded natural-language operational query (READ-ONLY)
    "ops_query",
    # V67 M34 — unified runtime & collector health snapshot (READ-ONLY)
    "runtime_health",
    # V68 M37 — cognitive command center panels (all READ-ONLY, bounded, redacted)
    "collector_telemetry",   # M39 rolling rate/lag/reliability intelligence
    "sensor_intel",          # M41 sensor trust / health / coverage
    "causal_timeline",       # M42 evidence-conscious causal & change timeline
    "cognitive_synthesis",   # M40 evidence-grounded narrative (degrades to deterministic)
    "decision_support",      # M43 transparent advisory over operator-supplied options
    "operational_state_health",  # M38 durable state honesty (durable vs volatile)
    # V69 M63 — situational World State (ALL READ-ONLY, bounded, redacted).
    # None of these is in _HIGH_RISK_HUD or _MEDIUM_RISK_HUD because none of
    # them can cause an effect: they are pure reads over in-memory state.
    "world_status",          # deterministic environment status, no LLM required
    "world_changed",         # grounded state transitions
    "world_impact",          # graph-derived dependency impact
    "world_unhealthy",       # entities not currently healthy
    "world_security",        # security controls and blind spots
    "world_connectors",      # connector availability (incl. OPTIONAL_MISSING)
    "world_doctor",          # runtime diagnosis; never repairs anything
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


# ── V69 M68B (B) — bounded per-connection delivery ───────────────────────────
#
# BroadcastManager.broadcast() used to be:
#
#     for ws in set(self._clients):
#         try:
#             await ws.send_text(payload)      # <- the producer awaits a SOCKET
#         except Exception:
#             dead.add(ws)
#
# One sequential await per client, on the producer's own stack. A browser tab that
# stops reading fills its TCP receive window; `send_text` then waits on the
# transport's drain future, which resolves only when THAT client reads. Nothing in
# the path bounded the wait, so a single wedged consumer stalled
# `telemetry_broadcaster` and — because `core.correlator` is attached directly to
# `manager.broadcast` — stalled compound-incident correlation too. Detection
# latency became a function of the slowest HUD tab.
#
# The shape that fixes it, and the reason each part is there:
#
#   bounded queue per connection   the producer's per-client cost is one
#                                  put_nowait: O(1), never awaits a socket
#   ONE owned writer per queue     the only frame that touches the websocket, so
#                                  concurrency is exactly one task per connection
#                                  and never a task per event
#   explicit send deadline         a writer that cannot place one frame inside the
#                                  deadline declares the client dead; without it
#                                  the stall moves into the writer instead of
#                                  being removed
#   explicit overflow policy       drop-oldest, counted. Telemetry is a stream of
#                                  snapshots: the NEWEST frame is the one a HUD
#                                  needs, so an evicted frame is the right loss
#                                  and a hidden loss is the wrong one
#   deterministic teardown         the writer is cancelled and awaited, so a
#                                  disconnected client leaves no task behind
#
# Deliberately NOT solved with `asyncio.create_task(ws.send_text(...))` per client
# per event: that trades a bounded stall for unbounded task growth, reorders
# frames on the wire, and keeps only a weak reference to each task.

#: Frames buffered per connection before the overflow policy fires.
AURA_CLIENT_QUEUE_MAX = 256
#: One frame's send deadline. A client that cannot accept a frame within this is
#: not slow, it is gone.
AURA_SEND_TIMEOUT_S = 5.0
#: How long a writer may finish in-flight work during teardown.
AURA_WRITER_DRAIN_S = 2.0

#: Queued BEHIND the frames a channel already holds at teardown. The writer
#: flushes what it has, sees this, and exits — so closing a healthy connection
#: costs one loop turn instead of always waiting out AURA_WRITER_DRAIN_S. The
#: drain is then a CEILING for a wedged client, not the normal cost.
_SHUTDOWN = object()


class OverflowPolicy(str, Enum):
    """What a full per-connection queue does. Explicit and observable."""

    DROP_OLDEST = "drop_oldest"


@dataclass
class ChannelMetrics:
    """Counters only — never payloads. A frame may contain operator data."""

    queued: int = 0
    sent: int = 0
    dropped_overflow: int = 0
    dropped_closed: int = 0
    send_timeouts: int = 0
    send_errors: int = 0
    high_watermark: int = 0
    last_drop_reason: str | None = None

    def snapshot(self, *, depth: int = 0, capacity: int = 0) -> dict:
        return {
            "queued": self.queued,
            "sent": self.sent,
            "dropped_overflow": self.dropped_overflow,
            "dropped_closed": self.dropped_closed,
            "send_timeouts": self.send_timeouts,
            "send_errors": self.send_errors,
            "queue_depth": depth,
            "queue_capacity": capacity,
            "high_watermark": self.high_watermark,
            "last_drop_reason": self.last_drop_reason,
            "overflow_policy": OverflowPolicy.DROP_OLDEST.value,
        }


class _ClientChannel:
    """One websocket, one bounded queue, one owned writer task."""

    def __init__(self, ws, *, maxsize: int = AURA_CLIENT_QUEUE_MAX,
                 send_timeout: float = AURA_SEND_TIMEOUT_S,
                 on_dead=None) -> None:
        self.ws = ws
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(maxsize)))
        self._send_timeout = float(send_timeout)
        self._on_dead = on_dead
        self._task: asyncio.Task | None = None
        self._closing = False
        self.metrics = ChannelMetrics()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"aura-writer-{id(self.ws):x}")

    @property
    def task(self) -> "asyncio.Task | None":
        return self._task

    @property
    def closing(self) -> bool:
        return self._closing

    def depth(self) -> int:
        return self._queue.qsize()

    def snapshot(self) -> dict:
        return self.metrics.snapshot(depth=self._queue.qsize(),
                                     capacity=self._queue.maxsize)

    # -- producer side (NEVER awaits a socket) --------------------------------
    def offer(self, payload: str) -> bool:
        """Hand one frame to this connection. Returns True when it was accepted.

        This is the producer's ENTIRE per-client cost. It is synchronous on
        purpose: there is no await here for a slow consumer to block on.
        """
        if self._closing:
            self.metrics.dropped_closed += 1
            self.metrics.last_drop_reason = "closing"
            return False
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # DROP_OLDEST: evict the stalest frame and admit the newest. Exactly
            # one frame is lost either way; this chooses which.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                self.metrics.dropped_overflow += 1
                self.metrics.last_drop_reason = "overflow"
                return False
            self.metrics.dropped_overflow += 1
            self.metrics.last_drop_reason = "overflow_evicted_oldest"
        self.metrics.queued += 1
        depth = self._queue.qsize()
        if depth > self.metrics.high_watermark:
            self.metrics.high_watermark = depth
        return True

    # -- the owned writer -----------------------------------------------------
    async def _run(self) -> None:
        try:
            while True:
                payload = await self._queue.get()
                if payload is _SHUTDOWN:
                    break
                try:
                    await asyncio.wait_for(self.ws.send_text(payload),
                                           timeout=self._send_timeout)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    self.metrics.send_timeouts += 1
                    self.metrics.last_drop_reason = "send_timeout"
                    break
                except Exception:
                    self.metrics.send_errors += 1
                    self.metrics.last_drop_reason = "send_error"
                    break
                self.metrics.sent += 1
        except asyncio.CancelledError:
            pass
        finally:
            self._closing = True
            if self._on_dead is not None:
                try:
                    self._on_dead(self.ws)
                except Exception:  # noqa: BLE001 - reaping must never raise
                    pass

    async def aclose(self, *, drain_s: float = AURA_WRITER_DRAIN_S) -> None:
        """Deterministic teardown: stop accepting, let the writer flush what it
        already holds, then cancel and AWAIT it so no task outlives the socket."""
        self._closing = True
        task = self._task
        if task is None or task.done():
            return
        try:
            self._queue.put_nowait(_SHUTDOWN)
        except asyncio.QueueFull:
            # Full queue: the sentinel matters more than the stalest frame.
            with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                self._queue.get_nowait()
                self._queue.put_nowait(_SHUTDOWN)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=drain_s)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class BroadcastManager:
    """Fan-out broadcaster with a bounded, isolated lifecycle per connection.

    ``broadcast()`` is O(clients) synchronous offers and never awaits a socket, so
    one slow or dead consumer can no longer hold a producer.
    """

    def __init__(self, *, queue_max: int = AURA_CLIENT_QUEUE_MAX,
                 send_timeout: float = AURA_SEND_TIMEOUT_S) -> None:
        # Kept as the public membership view (``ws in manager._clients``).
        self._clients: set = set()
        self._channels: "dict[int, _ClientChannel]" = {}
        self._queue_max = queue_max
        self._send_timeout = send_timeout

    # -- membership ----------------------------------------------------------
    async def connect(self, ws) -> None:
        await ws.accept()
        self._register(ws)
        logger.debug(f"AURA: +client  total={len(self._clients)}")

    def _register(self, ws) -> None:
        """Attach a bounded channel and start its owned writer."""
        self._clients.add(ws)
        channel = _ClientChannel(ws, maxsize=self._queue_max,
                                 send_timeout=self._send_timeout,
                                 on_dead=self._reap)
        self._channels[id(ws)] = channel
        channel.start()

    def _reap(self, ws) -> None:
        """Called by a writer that has stopped. Membership must reflect reality
        the moment the writer dies, not at the next broadcast."""
        self._clients.discard(ws)
        self._channels.pop(id(ws), None)

    def disconnect(self, ws) -> None:
        """Synchronous removal (kept for existing callers). The writer is
        cancelled; use ``aclose_client`` when the caller can await."""
        self._clients.discard(ws)
        channel = self._channels.pop(id(ws), None)
        if channel is not None and channel.task is not None:
            channel.task.cancel()
        logger.debug(f"AURA: -client  total={len(self._clients)}")

    async def aclose_client(self, ws, *,
                            drain_s: float = AURA_WRITER_DRAIN_S) -> None:
        """Awaitable removal: no writer task survives this call."""
        self._clients.discard(ws)
        channel = self._channels.pop(id(ws), None)
        if channel is not None:
            await channel.aclose(drain_s=drain_s)
        logger.debug(f"AURA: -client  total={len(self._clients)}")

    # -- the producer path ---------------------------------------------------
    async def broadcast(self, event: dict) -> None:
        """Offer ``event`` to every connected client. Bounded and non-blocking:
        the cost is one serialisation plus one put_nowait per client."""
        if not self._channels:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        for channel in list(self._channels.values()):
            channel.offer(payload)

    # -- observability / shutdown --------------------------------------------
    def stats(self) -> dict:
        return {
            "clients": len(self._clients),
            "queue_capacity": self._queue_max,
            "send_timeout_s": self._send_timeout,
            "overflow_policy": OverflowPolicy.DROP_OLDEST.value,
            "channels": [c.snapshot() for c in self._channels.values()],
        }

    async def aclose(self, *, drain_s: float = AURA_WRITER_DRAIN_S) -> None:
        """Application shutdown: every writer is cancelled and awaited."""
        channels = list(self._channels.values())
        self._channels.clear()
        self._clients.clear()
        for channel in channels:
            await channel.aclose(drain_s=drain_s)


# ── Module-level singletons ──────────────────────────────────────────────────
manager = BroadcastManager()

# V69 M68B (B) — the bounded fire-and-forget seam.
#
# `broadcast()` did `asyncio.create_task(_corr.ingest(event))` per event and the
# websocket endpoint did `asyncio.create_task(_handle_hud_command(...))` per
# command. Both were unbounded (concurrency equal to the arrival rate) and both
# were UNTRACKED — asyncio keeps only a weak reference to a running task, so a
# task nobody holds may be garbage-collected mid-await and simply stop, silently.
#
# One ceiling, one strong reference set, one counter. Over the ceiling the
# coroutine is closed (no "never awaited" warning) and the drop is counted, which
# is a policy; growing without limit is not.
AURA_MAX_INFLIGHT_TASKS = 64
_bounded_tasks: "set[asyncio.Task]" = set()
_bounded_dropped = 0


def _spawn_bounded(coro, *, name: str) -> "asyncio.Task | None":
    """Schedule background work under a finite ceiling, keeping a strong ref."""
    global _bounded_dropped
    if len(_bounded_tasks) >= AURA_MAX_INFLIGHT_TASKS:
        coro.close()
        _bounded_dropped += 1
        return None
    try:
        task = asyncio.create_task(coro, name=name)
    except RuntimeError:                     # no running loop (shutdown race)
        coro.close()
        _bounded_dropped += 1
        return None
    _bounded_tasks.add(task)
    task.add_done_callback(_bounded_tasks.discard)
    return task


def bounded_task_stats() -> dict:
    """Counters only — never event payloads."""
    return {"inflight": len(_bounded_tasks), "capacity": AURA_MAX_INFLIGHT_TASKS,
            "dropped": _bounded_dropped}


def _reset_bounded_tasks() -> None:
    """Cancel and forget every tracked task. Used at shutdown and by tests."""
    global _bounded_dropped
    for task in list(_bounded_tasks):
        task.cancel()
    _bounded_tasks.clear()
    _bounded_dropped = 0


# Pending OTP / confirm futures keyed by id(ws)
_pending_ws_responses: dict[int, asyncio.Future] = {}

# Executor reference injected by main.py
_executor_ref = None

# V63 M7 — Presence Engine + shared AssistantState references injected by main.py.
_presence_ref = None
_presence_state_ref = None


def attach_executor(executor) -> None:
    """Store a reference to the ToolExecutor for HUD command dispatch."""
    global _executor_ref
    _executor_ref = executor


def attach_presence(engine, state) -> None:
    """Store the Presence Engine + live AssistantState for the presence_status
    HUD command (V63 M7)."""
    global _presence_ref, _presence_state_ref
    _presence_ref = engine
    _presence_state_ref = state


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
    corr_task = asyncio.create_task(correlator.start(), name="correlator")

    task = asyncio.create_task(telemetry_broadcaster(), name="telemetry-broadcaster")
    try:
        yield
    finally:
        # V69 M68B (B): application shutdown is part of the contract. Every task
        # this module owns — the telemetry producer, the correlator, the bounded
        # fan-out and every per-connection writer — is cancelled and awaited here.
        for pending in (task, corr_task):
            pending.cancel()
        for pending in (task, corr_task):
            try:
                await pending
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _reset_bounded_tasks()
        await manager.aclose()


app = FastAPI(
    title="AURA", version="27.0",
    lifespan=_lifespan, docs_url=None, redoc_url=None,
)

# V69 M61 RC1 — the mount is CONDITIONAL. ``StaticFiles`` raises if its directory
# is absent, and until now the only thing that made it present was the import-time
# ``_MESHES_DIR.mkdir()`` two hundred lines above: importing the HUD server created
# ``static/meshes`` as a side effect, in whatever directory the importer stood, and
# that accident was load-bearing. With the side effect removed, an absent asset tree
# must degrade to "HUD without static assets", never to an import-time crash that
# takes the whole optional subsystem down with it.
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    logger.warning(
        "AURA: static asset directory missing (%s) — HUD served without /static",
        _STATIC_DIR.name,
    )


# ── HUD Command Handlers ──────────────────────────────────────────────────────

def _hud_level(o: dict, key: str):
    from core.decision_support import Level
    try:
        return Level(str(o.get(key, "unknown")).lower())
    except ValueError:
        return Level.UNKNOWN


def _dispatch_decision_support(args: dict) -> dict:
    """V68 M43 — build a transparent, advisory-only ranking over operator-supplied
    candidate options. Never executes anything. Malformed options are skipped, not run."""
    from core.decision_support import DecisionOption, rank_options
    raw_opts = args.get("options") if isinstance(args.get("options"), list) else []
    options: list = []
    for i, o in enumerate(raw_opts[:32]):
        if not isinstance(o, dict):
            continue
        options.append(DecisionOption(
            option_id=str(o.get("option_id", f"opt-{i}"))[:64],
            title=str(o.get("title", o.get("option_id", f"opt-{i}")))[:160],
            risk=_hud_level(o, "risk"), impact=_hud_level(o, "impact"),
            reversibility=_hud_level(o, "reversibility"),
            info_gain=_hud_level(o, "info_gain"),
            uncertainty_reduction=_hud_level(o, "uncertainty_reduction"),
            requires_authorization=bool(o.get("requires_authorization", True)),
            rationale=str(o.get("rationale", ""))[:240]))
    return rank_options(options).to_dict()


# V69 M68B (B): every operator-triggered job below is spawned through
# `_spawn_bounded`, not `asyncio.create_task`. Two reasons, both measured: a HUD
# can issue commands faster than the jobs finish (unbounded concurrency), and an
# untracked task holds only a weak reference, so a long job nobody keeps may be
# collected mid-await and simply stop with no error anywhere.
async def _dispatch_hud_command(cmd: str, args: dict, executor, broadcast_fn) -> dict:
    """Route validated HUD commands to appropriate tool functions."""
    try:
        if cmd == "ops_command_center":
            # V67 M31 — read-only live command center. Pure in-memory assembly of the
            # bounded/redacted panels; no world-effect, no Ollama query, never blocks.
            from core.ops_views import build_live_command_center
            sensors = args.get("sensors") if isinstance(args.get("sensors"), dict) else None
            return build_live_command_center(sensors=sensors)

        if cmd == "ops_query":
            # V67 M32 — grounded, READ-ONLY question answering. The question is DATA:
            # it is keyword-classified and answered only from structured state; it is
            # never executed, and every field is bounded/redacted before return.
            from core.ops_query import answer_question
            question = str(args.get("question", ""))[:200]
            sensors = args.get("sensors") if isinstance(args.get("sensors"), dict) else None
            return answer_question(question, sensors=sensors).to_dict()

        if cmd == "runtime_health":
            # V67 M34 — read-only unified health. Composes existing diagnostics; a single
            # non-blocking CPU/RAM sample, no self-test, no Ollama probe. Never blocks.
            from core.runtime_health import build_live_runtime_health
            return build_live_runtime_health()

        # ── V69 M63: situational World State panels ──────────────────────
        # Every branch below is a pure read. No connector is RUN from the HUD:
        # "world_connectors" reports the last known availability rather than
        # probing on demand, so a HUD client cannot use it to reach outward.
        if cmd == "world_status":
            from core.world_state import world
            from core.world_status import jarvis_status
            return jarvis_status(world)

        if cmd == "world_changed":
            from core.world_state import world
            from core.world_status import what_changed
            within = args.get("within_s", 3600.0)
            try:
                within = max(1.0, min(float(within), 86_400.0))
            except (TypeError, ValueError):
                within = 3600.0
            return what_changed(world, within_s=within)

        if cmd == "world_impact":
            from core.world_state import world
            from core.world_status import dependency_impact
            return dependency_impact(world, str(args.get("entity", ""))[:200])

        if cmd == "world_unhealthy":
            from core.world_state import world
            from core.world_status import unhealthy_report
            return unhealthy_report(world)

        if cmd == "world_security":
            from core.world_state import world
            from core.world_status import security_summary
            return security_summary(world)

        if cmd == "world_connectors":
            from core.world_runtime import connector_snapshot
            return connector_snapshot()

        if cmd == "world_doctor":
            from core.runtime_doctor import run_diagnostics
            # include_network=False: the HUD path never triggers an outbound
            # probe, not even to loopback.
            return run_diagnostics(include_network=False).to_dict()

        if cmd == "collector_telemetry":
            # V68 M39 — bounded per-collector rate/lag/reliability + derived state. Pure
            # in-memory read of the telemetry ring; no world-effect, never blocks.
            from core.collector_fabric import fabric
            return {"panel": "collector_telemetry", "collectors": fabric.telemetry_snapshot()}

        if cmd == "sensor_intel":
            # V68 M41 — sensor trust / health / coverage. Read-only over the mesh; trust
            # is reported conservatively (unsigned == DECLARED, never VERIFIED).
            from core.sensor_intel import build_live_sensor_intel
            return build_live_sensor_intel()

        if cmd == "causal_timeline":
            # V68 M42 — evidence-conscious causal & change timeline. Every link carries its
            # epistemic label; correlation is never presented as proof.
            from core.causal_timeline import build_live_causal_timeline
            return build_live_causal_timeline()

        if cmd == "operational_state_health":
            # V68 M38 — durable-vs-volatile honesty. Never claims durable if only volatile.
            from core.operational_store import get_store
            return {"panel": "operational_state_health", **get_store().health()}

        if cmd == "cognitive_synthesis":
            # V68 M40 — evidence-grounded narrative for a question. Bounded timeout; if no
            # model is reachable it returns the deterministic grounded answer instantly.
            # The LLM never introduces a fact the structured state does not support.
            from core.cognitive_synthesis import build_live_synthesis
            question = str(args.get("question", ""))[:200]
            sensors = args.get("sensors") if isinstance(args.get("sensors"), dict) else None
            return await build_live_synthesis(question, sensors=sensors)

        if cmd == "decision_support":
            # V68 M43 — transparent advisory over operator-SUPPLIED candidate options.
            # Advisory only: it ranks and explains, it NEVER executes or auto-selects.
            return _dispatch_decision_support(args)

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

            _spawn_bounded(_run_controlled_team(), name="hud-controlled-team")
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

            _spawn_bounded(_run_plan(), name="hud-plan-task")
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

        elif cmd == "presence_status":
            # V63 M7 — report the current proactive-presence posture (which
            # ladder rungs the live mode / consent / authority / resources permit).
            if _presence_ref is None or _presence_state_ref is None:
                return {"error": "presence engine not attached"}
            from core.presence import PresenceSignal
            tex = executor or _executor_ref
            try:
                vm = psutil.virtual_memory()
                batt = psutil.sensors_battery()
                on_batt = bool(batt is not None and not batt.power_plugged)
                cpu = psutil.cpu_percent(interval=None)
            except Exception:
                vm, on_batt, cpu = None, False, 0.0
            signal = PresenceSignal(
                mode=_presence_state_ref.mode,
                consent=getattr(tex, "consent", None) or PresenceSignal().consent,
                authority=getattr(tex, "authority", None),
                cpu_pct=cpu,
                ram_pct=(vm.percent if vm else 0.0),
                on_battery=on_batt,
            )
            return {"presence": _presence_ref.snapshot(signal)}

        elif cmd == "generate_report":
            from core.correlator        import correlator
            from core.incident_reporter import generate_incident_report
            from core.agent_orchestrator import orchestrator
            incidents = correlator.get_active_incidents()
            if not incidents:
                return {"error": "no active incidents"}
            _spawn_bounded(generate_incident_report(
                incidents[0], [], broadcast_fn,
                orchestrator._ollama_client,
                orchestrator._deep_model,
            ), name="hud-incident-report")
            return {"status": "generating", "incident_id": incidents[0].get("incident_id")}

        elif cmd == "consolidate_memory":
            from core.memory_consolidator import consolidate_memory
            from core.agent_orchestrator  import orchestrator
            _spawn_bounded(consolidate_memory(
                broadcast_fn,
                orchestrator._ollama_client,
                orchestrator._deep_model,
            ), name="hud-consolidate-memory")
            return {"status": "consolidating"}

        # ── v39.0 Self-healing remediator ────────────────────────────────────
        elif cmd == "execute_mitigation":
            script_path = str(args.get("script_path", ""))[:300]
            from core.auto_remediator import execute_mitigation
            _spawn_bounded(
                execute_mitigation(script_path, broadcast_fn, _executor_ref),
                name="hud-execute-mitigation")
            return {"status": "otp_challenge_issued"}

        # ── v43.0 BIFROST PROTOCOL ───────────────────────────────────────────
        elif cmd == "deploy_sigma_rule":
            draft_path = str(args.get("draft_path", ""))[:300]
            from core.detection_engineer import deploy_approved_rule
            _spawn_bounded(deploy_approved_rule(draft_path, broadcast_fn),
                           name="hud-deploy-sigma")
            return {"status": "deploying"}

        elif cmd == "run_bas_scenario":
            target   = str(args.get("target",   "192.168.1.100"))[:50]
            scenario = str(args.get("scenario", "apt_chain"))[:30]
            if not _TARGET_RE.match(target):
                return {"error": "Invalid target format"}
            from tools.breach_simulator import run_full_bas_scenario
            _spawn_bounded(run_full_bas_scenario(
                target, broadcast_fn, scenario,
            ), name="hud-bas-scenario")
            return {"status": "started", "scenario": scenario, "target": target}

        return {"error": f"Handler not implemented for '{cmd}'"}
    except Exception as e:
        return {"error": str(e)}


async def _ws_send(ws, payload: dict) -> bool:
    """Send ONE frame to ONE websocket under the same deadline the channel writers
    use. Every direct send in this module goes through here.

    V69 M68B (B): a send with no deadline holds its task for as long as the client
    refuses to read. That is not the producer stall Finding B was about — these
    sends run in a connection's own handler or in a bounded background task — but
    64 wedged HUD replies exhaust AURA_MAX_INFLIGHT_TASKS and starve the correlator
    fan-out, which is the same leak one layer down. Returns whether it landed;
    a False means the client is gone, never a reason to retry.
    """
    try:
        await asyncio.wait_for(ws.send_json(payload), timeout=AURA_SEND_TIMEOUT_S)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:          # noqa: BLE001 - a dead client is data, not a crash
        return False


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
        await _ws_send(ws, {
            "type": "hud_command_error", "request_id": req_id,
            "error": "Command rejected by sanitizer",
        })
        return

    if cmd not in _HUD_ALLOWED_COMMANDS:
        await _ws_send(ws, {
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
            await _ws_send(ws, {
                "type":       "hud_approval_required_out_of_band",
                "request_id": req_id,
                "cmd":        sanitize_for_hud(cmd),
                "error":      "approval_required_out_of_band",
            })
            return
        await _ws_send(ws, {
            "type":       "hud_approval_pending_out_of_band",
            "request_id": req_id,
            "cmd":        sanitize_for_hud(cmd),
            "message":    "High-risk command requires operator approval at the JARVIS console.",
        })
        granted, _audit = await challenge(f"hud:{cmd}", sanitize_for_hud(str(args)[:120]))
        if not granted:
            await _ws_send(ws, {
                "type": "hud_command_error", "request_id": req_id,
                "error": "High-risk command denied (out-of-band approval).",
            })
            return

    # ── Medium-risk: require explicit confirmation ────────────────────────────
    elif cmd in _MEDIUM_RISK_HUD:
        args_preview = sanitize_for_hud(str(args)[:60])
        fut_c: asyncio.Future = asyncio.get_event_loop().create_future()
        _pending_ws_responses[id(ws)] = fut_c
        await _ws_send(ws, {
            "type":       "hud_confirm_required",
            "request_id": req_id,
            "message":    f"Confirm: {sanitize_for_hud(cmd)} {args_preview}",
        })
        try:
            confirm = await asyncio.wait_for(fut_c, timeout=15.0)
            if confirm.get("confirmed") is not True:
                await _ws_send(ws, {
                    "type": "hud_command_error", "request_id": req_id,
                    "error": "Command declined",
                })
                return
        except asyncio.TimeoutError:
            _pending_ws_responses.pop(id(ws), None)
            return

    # ── Dispatch ─────────────────────────────────────────────────────────────
    result = await _dispatch_hud_command(cmd, args, executor, broadcast_fn)
    await _ws_send(ws, {
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
    # Bounded like every other send (V69 M68B/B): a client that completes the
    # handshake and then stops reading used to hold this frame — and this
    # connection's whole handler — indefinitely.
    await _ws_send(ws, {
        "type":      "system",
        "message":   "AURA pipeline connected.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    try:
        while True:
            try:
                raw_text = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                try:
                    raw = json.loads(raw_text)
                    if not isinstance(raw, dict):
                        continue
                    if "cmd" in raw:
                        _spawn_bounded(
                            _handle_hud_command(raw, ws, _executor_ref, broadcast),
                            name="hud-command")
                    elif "otp_response" in raw or "confirmed" in raw:
                        fut = _pending_ws_responses.pop(id(ws), None)
                        if fut and not fut.done():
                            fut.set_result(raw)
                except (json.JSONDecodeError, ValueError):
                    pass
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        # V69 M68B (B): cleanup in a `finally`, not only in the disconnect
        # handler. A transport error, a cancellation or a shutdown used to leave
        # the client in the set with a live writer behind it.
        await manager.aclose_client(ws)
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
    return {"status": "ok", "clients": len(manager._clients),
            "delivery": manager.stats(), "tasks": bounded_task_stats()}


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
        _spawn_bounded(_corr.ingest(event), name="correlator-ingest")
    except Exception:
        pass

    # V66 M21 — canonical evidence-linked correlation layer. Fed the SAME event
    # stream but only normalizes operational telemetry types (HUD/model noise is
    # ignored) and drives NO legacy ingest (the line above owns that), so there is
    # no double-ingest. Fire-and-forget; never blocks or breaks the broadcast.
    try:
        from core.correlation_v2 import correlator_v2
        correlator_v2.feed(event)
    except Exception:
        pass

    await manager.broadcast(event)


# Exported alias — external telemetry sources use this name explicitly
_verified_broadcast = broadcast
