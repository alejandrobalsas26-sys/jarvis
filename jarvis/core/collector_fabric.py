"""core/collector_fabric.py — V67 M28: unified collector lifecycle & health fabric.

JARVIS already has a battle-tested lifecycle/ingestion stack:

  * ``core.task_watchdog.TaskWatchdog.register(name, factory, RestartPolicy)`` —
    supervises background coroutines with exponential-backoff restart.
  * ``tools.executor._aura_broadcast`` → ``aura.server.broadcast`` — the single
    ingestion boundary (HMAC verify → legacy correlator + ``correlation_v2.feed``
    → canonical ``OperationalEvent`` → WebSocket fan-out).
  * ``core.telemetry_auth.make_signed_broadcaster`` — per-source HMAC signing,
    already applied *inside* each producer.

What was missing (proven in V67 recon): the ~30 producers are bare
``async def start_X(broadcast_fn)`` coroutines with **no common identity, no
health/heartbeat, no per-collector metrics, no dormant-vs-failed distinction, and
no single registry**. This module is that thin standardizing layer — it does NOT
replace producers, the watchdog, the signer, or the ingestion boundary. It:

  1. Describes each collector once (:class:`CollectorSpec`) with a *pure*
     ``is_configured`` predicate so an unconfigured integration reads DORMANT,
     never FAILED (directive rule #14).
  2. Wraps ``_aura_broadcast`` in a :meth:`CollectorFabric.managed_broadcast`
     that counts events, stamps a heartbeat/last-success, and forwards
     unchanged (signed envelopes are never mutated — HMAC stays intact).
  3. Derives live lifecycle status from ``TaskWatchdog.get_status()`` so every
     supervised collector has truthful health without a second supervisor.
  4. Exposes a bounded, redacted health/metrics snapshot for M31 (AURA collectors
     panel) and M34 (readiness) — no raw secrets, all lists capped (Rule of Silicon).

It also ships :class:`BoundedCollectorQueue`, a real drop-oldest bounded queue
new collectors (M29 discovery, M30 scenario replay) use to get backpressure +
drop accounting for free. Canonical de-duplication is NOT re-implemented here —
it lives in ``core.ops_events.EventAdapterRegistry`` (content-hash); adding a
second dedup would fork that contract.

Pure/dependency-light: no I/O at import, no tool execution, no model call.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable

from loguru import logger

# Bounds (Rule of Silicon)
_MAX_ERR = 240
_MAX_PANEL = 24


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
#  Status taxonomy (shared with M34 readiness)
# ══════════════════════════════════════════════════════════════════════════════
class CollectorStatus(str, Enum):
    """Operational status of one collector. Chosen so *unknown/unconfigured* is
    never conflated with *failure* — an integration the operator has not set up is
    DORMANT/OPTIONAL, not a red alarm."""
    OK = "ok"                    # supervised, running, producing (or idle-by-design)
    WARMING = "warming"          # started, not yet confirmed producing
    DORMANT = "dormant"          # not configured on this host — expected, benign
    OPTIONAL = "optional"        # optional integration, absent — benign
    DEGRADED = "degraded"        # restarting / intermittent
    FAILED = "failed"            # crashed and abandoned / not restartable
    STOPPING = "stopping"        # graceful shutdown in progress
    BACKPRESSURE = "backpressure"  # bounded queue saturated, shedding load

    @property
    def is_healthy(self) -> bool:
        return self in (CollectorStatus.OK, CollectorStatus.WARMING,
                        CollectorStatus.DORMANT, CollectorStatus.OPTIONAL)


# Map the watchdog's coarse task state → a collector status (for configured,
# supervised collectors). "restarting" means the task died and is between backoff
# restarts; "done" with no restarts means it exited/never ran.
_WATCHDOG_STATUS_MAP = {
    "running": CollectorStatus.OK,
    "restarting": CollectorStatus.DEGRADED,
    "done": CollectorStatus.FAILED,
}


# ══════════════════════════════════════════════════════════════════════════════
#  Spec + runtime state
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CollectorSpec:
    """Static description of one collector (identity + capability + config gate).

    ``is_configured`` is a *pure* predicate evaluated to decide DORMANT vs live —
    it must not perform blocking I/O (read settings/env/known paths only).
    ``signed_source`` names the telemetry_auth key the producer signs with (for
    provenance display only — signing itself stays inside the producer).
    """
    collector_id: str                       # MUST match the TaskWatchdog register name
    source_type: str                        # e.g. "sysmon", "zeek", "network_baseline"
    display_name: str
    is_configured: Callable[[], bool] = lambda: True
    capabilities: frozenset[str] = frozenset()   # descriptive: {"stream","checkpoint","signed"}
    signed_source: str | None = None
    windows_only: bool = False
    requires_admin: bool = False
    optional: bool = True                    # unconfigured optional → OPTIONAL else DORMANT
    external_binary: str | None = None       # e.g. "zeek", "vmrun.exe" (advisory)

    def configured(self) -> bool:
        try:
            return bool(self.is_configured())
        except Exception as e:  # noqa: BLE001 — a bad predicate never breaks health
            logger.debug(f"COLLECTOR_FABRIC: is_configured({self.collector_id}) raised: {e}")
            return False


@dataclass
class Collector:
    """Live runtime state for one collector. Mutated only by the fabric / the
    collector's own managed broadcast — never by untrusted event content."""
    spec: CollectorSpec
    registered: bool = False                 # handed to a supervisor
    events_emitted: int = 0
    duplicates_seen: int = 0                  # informational; canonical dedup is downstream
    last_event_at: str | None = None
    last_success: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    started_at: str | None = None
    checkpoint: str | None = None            # opaque per-collector watermark
    backpressure: bool = False
    backlog: int = 0
    drops: int = 0
    _stopping: bool = False

    # ── mutation ──────────────────────────────────────────────────────────────
    def record_event(self, *, now_iso: str | None = None) -> None:
        ts = now_iso or _now_iso()
        self.events_emitted += 1
        self.last_event_at = ts
        self.last_success = ts

    def record_error(self, error: str, *, now_iso: str | None = None) -> None:
        self.last_error = (error or "")[:_MAX_ERR]
        self.last_error_at = now_iso or _now_iso()

    def set_checkpoint(self, watermark: str) -> None:
        self.checkpoint = (watermark or "")[:200]

    def mark_backpressure(self, on: bool, *, backlog: int = 0, drops: int = 0) -> None:
        self.backpressure = bool(on)
        self.backlog = max(0, int(backlog))
        if drops:
            self.drops += int(drops)

    def mark_stopping(self) -> None:
        self._stopping = True

    # ── derived status ─────────────────────────────────────────────────────────
    def status(self, watchdog_state: str | None) -> CollectorStatus:
        """Resolve the collector's status from config + supervisor + local flags."""
        if self._stopping:
            return CollectorStatus.STOPPING
        if not self.spec.configured():
            return CollectorStatus.OPTIONAL if self.spec.optional else CollectorStatus.DORMANT
        if self.backpressure:
            return CollectorStatus.BACKPRESSURE
        if watchdog_state is not None:
            base = _WATCHDOG_STATUS_MAP.get(watchdog_state, CollectorStatus.WARMING)
            # A running-but-silent streaming collector is healthy (no telemetry is
            # not failure); only WARM until the first event is seen.
            if base is CollectorStatus.OK and self.events_emitted == 0:
                return CollectorStatus.WARMING
            return base
        # Configured but not supervised here (wired elsewhere) → best-effort.
        return CollectorStatus.OK if self.events_emitted else CollectorStatus.WARMING

    def health(self, watchdog_state: str | None = None) -> dict:
        st = self.status(watchdog_state)
        return {
            "collector_id": self.spec.collector_id,
            "source_type": self.spec.source_type,
            "display_name": self.spec.display_name,
            "status": st.value,
            "healthy": st.is_healthy,
            "configured": self.spec.configured(),
            "registered": self.registered,
            "events_emitted": self.events_emitted,
            "last_event_at": self.last_event_at,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "checkpoint": self.checkpoint,
            "backpressure": self.backpressure,
            "backlog": self.backlog,
            "drops": self.drops,
            "signed": bool(self.spec.signed_source),
            "capabilities": sorted(self.spec.capabilities),
            "windows_only": self.spec.windows_only,
            "requires_admin": self.spec.requires_admin,
            "external_binary": self.spec.external_binary,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded queue primitive (backpressure for NEW collectors)
# ══════════════════════════════════════════════════════════════════════════════
class BoundedCollectorQueue:
    """A drop-oldest bounded queue giving a collector backpressure + drop
    accounting. New collectors (discovery/replay) push here and a drain task
    forwards to the managed broadcast — so a burst never grows memory unbounded
    (Rule of Silicon) and the pressure is visible in the fabric health."""

    def __init__(self, maxsize: int = 256) -> None:
        self._q: deque = deque(maxlen=max(1, maxsize))
        self.maxsize = max(1, maxsize)
        self.drops = 0
        self._ev = asyncio.Event()

    def push(self, item) -> bool:
        """Enqueue; returns False and increments drops if the oldest was evicted."""
        evicted = len(self._q) >= self.maxsize
        if evicted:
            self.drops += 1
        self._q.append(item)
        self._ev.set()
        return not evicted

    @property
    def depth(self) -> int:
        return len(self._q)

    @property
    def saturated(self) -> bool:
        return len(self._q) >= self.maxsize

    async def drain(self, handler: Callable[[object], Awaitable]) -> None:
        """Continuously forward queued items to *handler* (cancellable)."""
        while True:
            if not self._q:
                self._ev.clear()
                await self._ev.wait()
            while self._q:
                item = self._q.popleft()
                try:
                    await handler(item)
                except Exception as e:  # noqa: BLE001 — one bad item never stops the drain
                    logger.debug(f"COLLECTOR_FABRIC: drain handler error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  The fabric
# ══════════════════════════════════════════════════════════════════════════════
BroadcastFn = Callable[[dict], Awaitable]


class CollectorFabric:
    """Registry + lifecycle/health facade over the existing producers.

    The fabric NEVER owns the event loop's supervision — it delegates to a
    :class:`~core.task_watchdog.TaskWatchdog` (attach one via
    :meth:`attach_watchdog`) and reads its coarse status for truthful health. It
    only *adds* identity, per-collector metrics, and a dormant/optional-aware
    status view."""

    def __init__(self) -> None:
        self._collectors: dict[str, Collector] = {}
        self._watchdog = None  # duck-typed: has get_status() -> dict[str,str]

    # ── registration ──────────────────────────────────────────────────────────
    def register(self, spec: CollectorSpec) -> Collector:
        col = self._collectors.get(spec.collector_id)
        if col is None:
            col = Collector(spec=spec)
            self._collectors[spec.collector_id] = col
        return col

    def register_all(self, specs) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, collector_id: str) -> Collector | None:
        return self._collectors.get(collector_id)

    def all(self) -> list[Collector]:
        return list(self._collectors.values())

    def attach_watchdog(self, watchdog) -> None:
        self._watchdog = watchdog

    def _watchdog_status(self) -> dict[str, str]:
        wd = self._watchdog
        if wd is None:
            return {}
        try:
            return dict(wd.get_status() or {})
        except Exception:  # noqa: BLE001
            return {}

    # ── instrumentation ────────────────────────────────────────────────────────
    def managed_broadcast(self, collector_id: str, base_broadcast: BroadcastFn) -> BroadcastFn:
        """Return an instrumented broadcast for *collector_id*.

        Increments the collector's event/heartbeat counters, then forwards the
        event UNCHANGED to *base_broadcast* (usually ``_aura_broadcast``). It never
        mutates the event (signed HMAC envelopes stay intact) and — like the
        underlying facade — never raises: a broadcast failure is recorded and
        swallowed so a producer is never broken by instrumentation.
        """
        col = self.register(CollectorSpec(collector_id, collector_id, collector_id)) \
            if collector_id not in self._collectors else self._collectors[collector_id]

        async def _managed(event: dict) -> None:
            try:
                col.record_event()
            except Exception:  # noqa: BLE001
                pass
            try:
                await base_broadcast(event)
            except Exception as e:  # noqa: BLE001 — mirror _aura_broadcast fire-and-forget
                col.record_error(f"broadcast: {e}")

        return _managed

    def supervise(
        self,
        spec: CollectorSpec,
        start_fn: Callable[[BroadcastFn], Awaitable],
        base_broadcast: BroadcastFn,
        *,
        watchdog=None,
    ):
        """Register *spec*'s producer with the watchdog using a managed broadcast.

        A no-op (returns None) when the collector is not configured — it stays
        DORMANT/OPTIONAL rather than spinning a restart loop on a missing
        integration. Delegates restart/backoff entirely to the watchdog.
        """
        wd = watchdog or self._watchdog
        col = self.register(spec)
        if not spec.configured():
            logger.info(f"COLLECTOR_FABRIC: {spec.collector_id} not configured — dormant")
            return None
        if wd is None:
            raise RuntimeError("CollectorFabric.supervise requires a watchdog")
        mbcast = self.managed_broadcast(spec.collector_id, base_broadcast)
        col.started_at = _now_iso()
        col.registered = True
        try:
            from core.task_watchdog import RestartPolicy
            policy = RestartPolicy.BACKOFF
        except Exception:  # noqa: BLE001
            policy = None
        task = wd.register(spec.collector_id, lambda: start_fn(mbcast), policy) \
            if policy is not None else wd.register(spec.collector_id, lambda: start_fn(mbcast))
        return task

    async def stop_all(self) -> None:
        """Mark every collector STOPPING (graceful-shutdown signal). Actual task
        cancellation is owned by the watchdog / main shutdown; this only flips the
        status so the HUD reflects a clean stop rather than a crash."""
        for col in self._collectors.values():
            col.mark_stopping()

    # ── views ───────────────────────────────────────────────────────────────────
    def health_snapshot(self) -> dict:
        """Full per-collector health keyed by id (for M34 readiness / diagnostics)."""
        wstatus = self._watchdog_status()
        return {c.spec.collector_id: c.health(wstatus.get(c.spec.collector_id))
                for c in self._collectors.values()}

    def metrics(self) -> dict:
        """Bounded aggregate metrics across the fabric."""
        wstatus = self._watchdog_status()
        buckets: dict[str, int] = {s.value: 0 for s in CollectorStatus}
        total_events = 0
        total_drops = 0
        for c in self._collectors.values():
            st = c.status(wstatus.get(c.spec.collector_id))
            buckets[st.value] += 1
            total_events += c.events_emitted
            total_drops += c.drops
        active = sum(buckets[s.value] for s in (CollectorStatus.OK, CollectorStatus.WARMING))
        dormant = buckets[CollectorStatus.DORMANT.value] + buckets[CollectorStatus.OPTIONAL.value]
        return {
            "total": len(self._collectors),
            "active": active,
            "dormant": dormant,
            "degraded": buckets[CollectorStatus.DEGRADED.value],
            "failed": buckets[CollectorStatus.FAILED.value],
            "backpressure": buckets[CollectorStatus.BACKPRESSURE.value],
            "events_emitted": total_events,
            "drops": total_drops,
            "by_status": buckets,
        }

    def aura_panel(self) -> dict:
        """Bounded, redaction-safe collectors panel for AURA (M31).

        Contains no free-text telemetry — only identity, status and counters — so
        it is safe to broadcast without secret redaction. Lists are capped."""
        wstatus = self._watchdog_status()
        rows = []
        for c in sorted(self._collectors.values(),
                        key=lambda x: (not x.spec.configured(), x.spec.collector_id))[:_MAX_PANEL]:
            st = c.status(wstatus.get(c.spec.collector_id))
            rows.append({
                "id": c.spec.collector_id,
                "name": c.spec.display_name,
                "source": c.spec.source_type,
                "status": st.value,
                "events": c.events_emitted,
                "last_event_at": c.last_event_at,
                "last_error": (c.last_error or "")[:120] if c.last_error else None,
                "signed": bool(c.spec.signed_source),
            })
        return {"metrics": self.metrics(), "collectors": rows}


# Module-level singleton — import this, never construct a second fabric.
fabric = CollectorFabric()


def get_fabric() -> CollectorFabric:
    return fabric


# ══════════════════════════════════════════════════════════════════════════════
#  Default catalog — the real JARVIS telemetry producers (pure predicates)
# ══════════════════════════════════════════════════════════════════════════════
def _env_true(environ, key: str) -> bool:
    return (environ.get(key, "") or "").strip().lower() in ("1", "true", "yes", "on")


def default_collector_catalog(settings, environ) -> list[CollectorSpec]:
    """Describe the canonical telemetry collectors + their configuration gates.

    Pure: reads only *settings* attributes and the *environ* mapping (no I/O). The
    ``is_configured`` predicates decide DORMANT vs live so an unconfigured host
    reports honestly (Sysmon path unset → DORMANT, not FAILED).
    """
    from pathlib import Path

    def _path_set(getter) -> bool:
        try:
            v = getter()
        except Exception:
            return False
        return bool(v) and Path(str(v)).exists()

    zeek_dir = getattr(settings, "zeek_log_dir", "") or ""

    return [
        CollectorSpec(
            "sysmon-bridge", "sysmon", "Sysmon EVTX bridge",
            is_configured=lambda: bool(environ.get("SYSMON_LOG_PATH")),
            capabilities=frozenset({"stream", "signed", "checkpoint"}),
            signed_source="sysmon", windows_only=True,
            external_binary="Sysmon",
        ),
        CollectorSpec(
            "zeek-dpi", "zeek", "Zeek L7 DPI log streamer",
            is_configured=lambda: bool(zeek_dir) and Path(zeek_dir).exists(),
            capabilities=frozenset({"stream", "signed", "checkpoint"}),
            signed_source="zeek", external_binary="zeek",
        ),
        CollectorSpec(
            "network-baseline", "network_baseline", "Network statistical baseline",
            is_configured=lambda: True,   # in-process detector, always available
            capabilities=frozenset({"stream"}), optional=False,
        ),
        CollectorSpec(
            "sensor-mesh", "sensor_mesh", "Remote sensor mesh (SSH micro-agents)",
            is_configured=lambda: True,   # loopback WS server; agents connect when deployed
            capabilities=frozenset({"stream"}),
        ),
        CollectorSpec(
            "etw-monitor", "etw", "ETW kernel telemetry monitor",
            is_configured=lambda: _env_true(environ, "JARVIS_ETW_ENABLE"),
            capabilities=frozenset({"stream", "signed"}),
            signed_source="etw", windows_only=True, requires_admin=True,
        ),
        CollectorSpec(
            "ebpf-bridge", "ebpf", "eBPF/Falco kernel bridge",
            is_configured=lambda: bool(environ.get("JARVIS_EBPF_HOST")),
            capabilities=frozenset({"stream", "signed"}), signed_source="ebpf",
        ),
        CollectorSpec(
            "sliver-monitor", "sliver", "Sliver C2 session monitor",
            is_configured=lambda: _path_set(lambda: environ.get("SLIVER_CONFIG_PATH")),
            capabilities=frozenset({"poll", "signed"}), signed_source="sliver",
            external_binary="sliver-server",
        ),
        CollectorSpec(
            "threat-feed", "threat_feed", "OSINT threat-feed sync",
            is_configured=lambda: True,   # public feeds; degrades gracefully offline
            capabilities=frozenset({"poll"}),
        ),
        CollectorSpec(
            "resource-watchdog", "resource_sentinel", "Hardware resource sentinel",
            is_configured=lambda: True,   # psutil metrics always available
            capabilities=frozenset({"stream"}), optional=False,
        ),
    ]


def install_default_catalog(settings=None, environ=None) -> CollectorFabric:
    """Register the default catalog into the module singleton and return it."""
    if settings is None:
        from core.config import settings as _s
        settings = _s
    if environ is None:
        import os
        environ = os.environ
    fabric.register_all(default_collector_catalog(settings, environ))
    return fabric
