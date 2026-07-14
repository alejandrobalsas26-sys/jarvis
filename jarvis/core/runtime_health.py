"""core/runtime_health.py — V67 M34: unified runtime & collector health snapshot.

One coherent, READ-ONLY health view that COMPOSES the existing diagnostics rather than
replacing them — the M28 collector fabric, the resource reading, the performance
profiler, the health watchdog, the model runtime resolution and the operational spine.
It does not add a monitoring stack; it is a bounded, structured, in-memory snapshot.

Non-blocking by construction: it only reads already-computed state (fabric counters,
profiler samples, watchdog dicts, resolved role models) and takes a single non-blocking
CPU/RAM sample. It never runs a self-test, never probes Ollama, and never touches the
event loop — safe to call while a DEEP inference is running.

Statuses reuse the fabric's vocabulary (:class:`~core.collector_fabric.CollectorStatus`):
OK / WARMING / DORMANT / OPTIONAL / DEGRADED / FAILED / STOPPING / BACKPRESSURE. DORMANT
and OPTIONAL are NOT failures — an unconfigured collector or an unmeasured metric is
reported honestly as such, never as "healthy and fine".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.collector_fabric import CollectorStatus as HealthStatus
from core.ops_views import _MAX_LIST, _redact

# Worst-of ranking. DORMANT/OPTIONAL are healthy (rank 0) — they never degrade overall.
_STATUS_RANK: dict[HealthStatus, int] = {
    HealthStatus.DORMANT: 0, HealthStatus.OPTIONAL: 0, HealthStatus.OK: 1,
    HealthStatus.WARMING: 2, HealthStatus.STOPPING: 3, HealthStatus.BACKPRESSURE: 4,
    HealthStatus.DEGRADED: 5, HealthStatus.FAILED: 6,
}
_UNHEALTHY = frozenset({HealthStatus.DEGRADED, HealthStatus.FAILED,
                        HealthStatus.BACKPRESSURE, HealthStatus.STOPPING})


def _healthy(status: HealthStatus) -> bool:
    return status not in _UNHEALTHY


@dataclass
class SubsystemHealth:
    name: str
    status: HealthStatus
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return _healthy(self.status)

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status.value, "healthy": self.healthy,
                "detail": _redact(self.detail), "metrics": self.metrics}


@dataclass
class RuntimeHealthSnapshot:
    overall: HealthStatus
    subsystems: list[SubsystemHealth]
    metrics: dict = field(default_factory=dict)

    @property
    def degraded(self) -> list[str]:
        return [s.name for s in self.subsystems if not s.healthy]

    def to_dict(self) -> dict:
        return {"panel": "runtime_health", "overall": self.overall.value,
                "healthy": _healthy(self.overall),
                "degraded": self.degraded,
                "subsystems": [s.to_dict() for s in self.subsystems],
                "metrics": self.metrics}

    def summary(self) -> str:
        """A compact ASCII one-liner (Windows/TTS-safe)."""
        parts = [f"{s.name}={s.status.value}" for s in self.subsystems]
        return f"RUNTIME {self.overall.value.upper()}: " + ", ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Subsystem builders (pure — take already-fetched raw data)
# ══════════════════════════════════════════════════════════════════════════════
def _collectors_subsystem(fm: dict, telemetry: dict | None = None) -> SubsystemHealth:
    fm = fm or {}
    total, active = fm.get("total", 0), fm.get("active", 0)
    failed, backp = fm.get("failed", 0), fm.get("backpressure", 0)
    if failed:
        status = HealthStatus.FAILED if active == 0 else HealthStatus.DEGRADED
    elif backp:
        status = HealthStatus.BACKPRESSURE
    elif active:
        status = HealthStatus.OK
    else:
        status = HealthStatus.DORMANT
    events, drops = fm.get("events_emitted", 0), fm.get("drops", 0)
    drop_ratio = round(drops / max(1, events + drops), 4)
    metrics = {"total": total, "active": active, "dormant": fm.get("dormant", 0),
               "degraded": fm.get("degraded", 0), "failed": failed, "backpressure": backp,
               "events_emitted": events, "queue_drops": drops, "drop_ratio": drop_ratio}
    tel_extra = _telemetry_rollup(telemetry)
    if tel_extra:
        metrics.update(tel_extra)
    return SubsystemHealth(
        "collectors", status,
        f"{active} active / {fm.get('dormant', 0)} dormant / {failed} failed of {total}",
        metrics)


def _telemetry_rollup(telemetry: dict | None) -> dict:
    """M39: fold per-collector telemetry into a bounded fabric-wide summary — a count
    per derived state and the peak event rate / lag — without unbounded per-collector
    fan-out into the metrics map."""
    if not telemetry:
        return {}
    states: dict[str, int] = {}
    peak_eps = 0.0
    max_lag = 0.0
    for snap in telemetry.values():
        if not isinstance(snap, dict):
            continue
        st = snap.get("state", "unknown")
        states[st] = states.get(st, 0) + 1
        peak_eps = max(peak_eps, snap.get("events_per_second") or 0.0)
        max_lag = max(max_lag, snap.get("median_lag_s") or 0.0)
    return {"telemetry_states": states, "telemetry_peak_eps": round(peak_eps, 4),
            "telemetry_max_lag_s": round(max_lag, 3)}


def _resource_subsystem(res: dict | None) -> SubsystemHealth:
    if not res:
        return SubsystemHealth("resource", HealthStatus.OPTIONAL,
                               "not measured (psutil unavailable)", {})
    cpu, ram = res.get("cpu_percent"), res.get("ram_percent")
    status = HealthStatus.OK
    if (cpu is not None and cpu >= 90) or (ram is not None and ram >= 90):
        status = HealthStatus.DEGRADED
    elif (cpu is not None and cpu >= 75) or (ram is not None and ram >= 88):
        status = HealthStatus.WARMING
    return SubsystemHealth("resource", status,
                           f"cpu {cpu}% / ram {ram}%",
                           {"cpu_percent": cpu, "ram_percent": ram})


def _tasks_subsystem(watchdog_status: dict | None) -> SubsystemHealth:
    ws = watchdog_status or {}
    if not ws:
        return SubsystemHealth("tasks", HealthStatus.DORMANT, "no supervised tasks", {})
    down = [n for n, st in ws.items()
            if str(st).lower() in ("done", "dead", "failed", "restarting")]
    status = HealthStatus.DEGRADED if down else HealthStatus.OK
    return SubsystemHealth("tasks", status,
                           f"{len(ws) - len(down)} running / {len(down)} down",
                           {"supervised": len(ws), "down": down[:_MAX_LIST]})


def _inference_subsystem(profiler: dict | None) -> SubsystemHealth:
    stats = profiler or {}
    inf = _pick_stat(stats, ("llm_inference", "inference", "chat", "generate"))
    load = _pick_stat(stats, ("model_load", "load_model", "warmup"))
    if not inf and not load:
        return SubsystemHealth("inference", HealthStatus.DORMANT,
                               "no inference samples yet", {})
    metrics = {}
    if inf:
        metrics.update({"inference_p50_s": inf.get("p50_s"),
                        "inference_p95_s": inf.get("p95_s"),
                        "inference_count": inf.get("count")})
    if load:
        metrics.update({"model_load_p50_s": load.get("p50_s"),
                        "model_load_max_s": load.get("max_s")})
    return SubsystemHealth("inference", HealthStatus.OK,
                           f"p95 {metrics.get('inference_p95_s', '?')}s over "
                           f"{metrics.get('inference_count', 0)} call(s)", metrics)


def _model_subsystem(model: dict | None) -> SubsystemHealth:
    roles = (model or {}).get("roles", {})
    status = HealthStatus.OK if roles else HealthStatus.DEGRADED
    return SubsystemHealth("model_runtime", status,
                           f"{len(roles)} role(s) resolved; probe={model.get('probe') if model else 'n/a'}",
                           {"roles": roles})


def _verifier_subsystem(verifier: dict | None = None) -> SubsystemHealth:
    """V68.1 M49 — bounded CPU-aware verification latency observability."""
    stats = verifier if verifier is not None else _live_verifier()
    count = stats.get("count", 0)
    if not count:
        return SubsystemHealth("verifier", HealthStatus.DORMANT,
                               "no verification samples yet", {})
    timeouts = stats.get("timeouts", 0)
    # Frequent timeouts => the CPU verifier is struggling; surface as DEGRADED.
    status = HealthStatus.DEGRADED if timeouts and timeouts >= max(1, count // 2) \
        else HealthStatus.OK
    return SubsystemHealth(
        "verifier", status,
        f"avg {stats.get('avg_s', 0)}s / max {stats.get('max_s', 0)}s over "
        f"{count} pass(es), {timeouts} timeout(s)",
        {"verifier_avg_s": stats.get("avg_s"), "verifier_max_s": stats.get("max_s"),
         "verifier_last_s": stats.get("last_s"), "verifier_timeouts": timeouts,
         "verifier_count": count},
    )


def _interactive_subsystem(turn: dict | None = None, life: dict | None = None,
                           console: dict | None = None,
                           tts: dict | None = None) -> SubsystemHealth:
    """V69 M54 — interactive-runtime observability: end-to-end turn latency (M54.5),
    lifecycle phase timings (M54.2), console queue health (M54.1) and TTS governor
    backpressure (M54.9). DORMANT until the first turn; DEGRADED only if the runtime
    itself has FAILED."""
    turn = turn if turn is not None else _live_turn()
    life = life if life is not None else _live_lifecycle()
    console = console if console is not None else _live_console()
    tts = tts if tts is not None else {}
    state = str(life.get("state", "UNKNOWN"))
    turns = turn.get("count", 0) or 0
    expired = turn.get("expired", 0) or 0
    # Status reflects TURN-BUDGET health only — lifecycle state (STOPPING/…) is
    # informational (a normal transient), never a health failure, so a shutting
    # -down process is not reported as "unhealthy runtime".
    if not turns:
        status = HealthStatus.DORMANT
    elif expired >= max(1, turns // 2):
        status = HealthStatus.DEGRADED   # many turns blew their end-to-end budget
    else:
        status = HealthStatus.OK
    metrics = {
        "turn_avg_total_ms": turn.get("avg_total_ms"),
        "turn_max_total_ms": turn.get("max_total_ms"),
        "turn_count": turn.get("count"),
        "turn_expired": turn.get("expired"),
        "text_ready_ms": life.get("text_ready_ms"),
        "core_ready_ms": life.get("core_ready_ms"),
        "operational_ready_ms": life.get("operational_ready_ms"),
        "console_dropped": console.get("dropped"),
        "console_coalesced": console.get("coalesced"),
        "tts_dropped": tts.get("dropped"),
        "tts_coalesced": tts.get("coalesced"),
    }
    return SubsystemHealth(
        "interactive", status,
        f"lifecycle={state}; turns={turn.get('count', 0)}; "
        f"turn_max={turn.get('max_total_ms', 0)}ms",
        metrics)


def _spine_subsystem(spine: dict | None) -> SubsystemHealth:
    s = spine or {}
    vs = s.get("verification_success_rate")
    return SubsystemHealth("spine", HealthStatus.OK,
                           f"{s.get('correlation_findings', 0)} finding(s), "
                           f"{s.get('incidents_open', 0)} open incident(s)",
                           {"correlation_findings": s.get("correlation_findings", 0),
                            "incidents_open": s.get("incidents_open", 0),
                            "verification_success_rate": vs})


def _pick_stat(stats: dict, keys: tuple[str, ...]) -> dict | None:
    for k in keys:
        if k in stats and stats[k].get("count"):
            return stats[k]
    # substring fallback (op names vary)
    for name, v in (stats or {}).items():
        if any(k in str(name).lower() for k in keys) and v.get("count"):
            return v
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Composition
# ══════════════════════════════════════════════════════════════════════════════
def collect_runtime_health(*, fabric_metrics: dict | None = None,
                           watchdog_status: dict | None = None,
                           resource: dict | None = None, profiler: dict | None = None,
                           model: dict | None = None, spine: dict | None = None,
                           telemetry: dict | None = None
                           ) -> RuntimeHealthSnapshot:
    """Compose one health snapshot from already-fetched raw data. Every arg may be
    injected (tests) or left None to read the live source (guarded)."""
    fm = fabric_metrics if fabric_metrics is not None else _live_fabric_metrics()
    ws = watchdog_status if watchdog_status is not None else _live_watchdog_status()
    res = resource if resource is not None else _live_resource()
    prof = profiler if profiler is not None else _live_profiler()
    mdl = model if model is not None else _live_model()
    sp = spine if spine is not None else _live_spine()
    tel = telemetry if telemetry is not None else _live_telemetry()

    subsystems = [
        _collectors_subsystem(fm, tel), _resource_subsystem(res), _tasks_subsystem(ws),
        _inference_subsystem(prof), _model_subsystem(mdl), _spine_subsystem(sp),
        _verifier_subsystem(), _interactive_subsystem(),
    ]
    overall = max(subsystems, key=lambda s: _STATUS_RANK.get(s.status, 0)).status
    metrics: dict = {}
    for s in subsystems:
        for k, v in s.metrics.items():
            if k not in ("roles", "down") and v is not None:
                metrics[f"{s.name}.{k}"] = v
    return RuntimeHealthSnapshot(overall=overall, subsystems=subsystems, metrics=metrics)


def build_live_runtime_health() -> dict:
    """The live read-only snapshot as a bounded dict (HUD/CLI/voice safe)."""
    return collect_runtime_health().to_dict()


# ── live source readers (all guarded; any failure degrades to None, never raises) ──
def _live_fabric_metrics() -> dict:
    try:
        from core.collector_fabric import fabric
        return fabric.metrics()
    except Exception:  # noqa: BLE001
        return {}


def _live_telemetry() -> dict:
    try:
        from core.collector_fabric import fabric
        return fabric.telemetry_snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_verifier() -> dict:
    try:
        from core.verification import verifier_latency_stats
        return verifier_latency_stats()
    except Exception:  # noqa: BLE001
        return {}


def _live_turn() -> dict:
    try:
        from core.turn_budget import turn_latency_stats
        return turn_latency_stats()
    except Exception:  # noqa: BLE001
        return {}


def _live_lifecycle() -> dict:
    try:
        from core.lifecycle import lifecycle
        return lifecycle.snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_console() -> dict:
    try:
        from core.console import get_console
        c = get_console()
        return c.metrics() if c is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def _live_watchdog_status() -> dict:
    try:
        from core import health_watchdog as hw
        out: dict = {}
        for name, info in getattr(hw, "_SUP", {}).items():
            task = info.get("task")
            out[name] = "running" if (task is not None and not task.done()) else "done"
        for name, st in hw._passive_status().items():
            out[name] = "running" if st.get("alive") else "done"
        return out
    except Exception:  # noqa: BLE001
        return {}


def _live_resource() -> dict | None:
    try:
        import psutil
        return {"cpu_percent": psutil.cpu_percent(interval=None),
                "ram_percent": psutil.virtual_memory().percent}
    except Exception:  # noqa: BLE001
        return None


def _live_profiler() -> dict:
    try:
        from core.performance_profiler import get_all_stats
        return get_all_stats()
    except Exception:  # noqa: BLE001
        return {}


def _live_model() -> dict:
    try:
        from core.ops_views import model_runtime_panel
        return model_runtime_panel()
    except Exception:  # noqa: BLE001
        return {}


def _live_spine() -> dict:
    out: dict = {}
    try:
        from core.correlation_v2 import correlator_v2
        out["correlation_findings"] = len(correlator_v2.recent(_MAX_LIST))
    except Exception:  # noqa: BLE001
        out["correlation_findings"] = 0
    try:
        from core.incident_workspace import workspace
        cases = workspace.open_cases()
        out["incidents_open"] = len(cases)
        verifs = [v for c in cases for v in getattr(c, "verification_results", [])]
        if verifs:
            ok = sum(1 for v in verifs if getattr(v, "verified", False))
            out["verification_success_rate"] = round(ok / len(verifs), 3)
    except Exception:  # noqa: BLE001
        out.setdefault("incidents_open", 0)
    return out
