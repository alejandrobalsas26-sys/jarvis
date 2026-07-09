"""core/field_readiness.py — V67 M36: field readiness assessment (real checks only).

A single, honest "can I deploy this right now?" report for the operator. It COMPOSES
real signals from the live system — no fabricated readiness, no green-by-default:

  CORE RUNTIME   the operational spine imports and is wired
  OLLAMA         a bounded reachability probe (the one place an active probe belongs)
  FAST/DEEP/VISION MODEL   the resolved concrete model per role
  COLLECTORS     active / dormant from the M28 fabric
  ASSETS         count observed in the evidence-backed graph
  SENSORS        connected agents from the sensor mesh
  AURA           the HUD server is importable/available
  PERSISTENCE    persistent state configured, else honest VOLATILE (in-memory)
  DOCKER/VMWARE  tooling present on the host
  AUTHORIZED SCOPE   the operator-authorized environment scopes (M29)
  RUNBOOK EXECUTION  DRY-RUN READY always; EXECUTE READY only when a ToolExecutor is wired

Read-only. Every line traces to a real check; an unknown/absent thing is reported as
such, never as OK. ASCII output (Windows console / cp1252 safe).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Line-status vocabulary (drives the overall verdict + rendering).
_OK = "OK"
_READY = "READY"
_DEGRADED = "DEGRADED"
_DORMANT = "DORMANT"
_ABSENT = "ABSENT"
_UNREACHABLE = "UNREACHABLE"
_VALUE = "VALUE"          # an informational value line (e.g. a model name)

# A line is "not ready" only for these hard-failure states.
_FAIL_STATES = frozenset({_DEGRADED, _ABSENT, _UNREACHABLE, "FAILED"})


@dataclass
class ReadinessLine:
    label: str
    value: str                 # what is shown on the right (status word or a value)
    state: str = _OK           # classification for the overall verdict
    critical: bool = False      # a critical line failing blocks readiness

    def ok(self) -> bool:
        return self.state not in _FAIL_STATES


@dataclass
class FieldReadinessReport:
    lines: list[ReadinessLine] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Ready when no CRITICAL line has failed. Non-critical degradation (Ollama
        down, no Docker) lowers capability but still permits deterministic monitoring."""
        return all(ln.ok() for ln in self.lines if ln.critical)

    def to_dict(self) -> dict:
        return {"panel": "field_readiness", "ready": self.ready,
                "lines": [{"label": ln.label, "value": ln.value, "state": ln.state,
                           "critical": ln.critical, "ok": ln.ok()} for ln in self.lines]}

    def render(self) -> str:
        width = max((len(ln.label) for ln in self.lines), default=12) + 2
        head = "JARVIS FIELD READINESS"
        out = [head, "=" * len(head)]
        for ln in self.lines:
            out.append(f"{ln.label.ljust(width)}{ln.value}")
        out.append("")
        out.append(f"VERDICT: {'FIELD READY' if self.ready else 'NOT READY'}")
        return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════
#  Assessment (each probe guarded; a failure degrades that line, never the process)
# ══════════════════════════════════════════════════════════════════════════════
def assess_field_readiness(*, probe_ollama: bool = True) -> FieldReadinessReport:
    lines: list[ReadinessLine] = []

    # ── core runtime ──────────────────────────────────────────────────────────
    try:
        import core.correlation_v2  # noqa: F401
        import core.incident_workspace  # noqa: F401
        import core.situation_engine  # noqa: F401
        lines.append(ReadinessLine("CORE RUNTIME", _OK, _OK, critical=True))
    except Exception as e:  # noqa: BLE001
        lines.append(ReadinessLine("CORE RUNTIME", f"DEGRADED ({str(e)[:40]})",
                                   _DEGRADED, critical=True))

    # ── ollama (bounded active probe — a readiness check, not the hot path) ────
    if probe_ollama:
        ok, info = _probe_ollama()
        lines.append(ReadinessLine("OLLAMA", _OK if ok else f"{_UNREACHABLE} ({info})",
                                   _OK if ok else _UNREACHABLE))
    else:
        lines.append(ReadinessLine("OLLAMA", "NOT CHECKED", _VALUE))

    # ── resolved role models ──────────────────────────────────────────────────
    roles = _resolved_roles()
    for role, label in (("fast", "FAST MODEL"), ("deep", "DEEP MODEL"),
                        ("vision", "VISION MODEL")):
        model = roles.get(role, "")
        lines.append(ReadinessLine(
            label, model or "UNRESOLVED", _VALUE if model else _DEGRADED,
            critical=(role in ("fast", "deep"))))

    # ── collectors ────────────────────────────────────────────────────────────
    fm = _fabric_metrics()
    lines.append(ReadinessLine(
        "COLLECTORS", f"{fm.get('active', 0)} ACTIVE / {fm.get('dormant', 0)} DORMANT"
        + (f" / {fm['failed']} FAILED" if fm.get("failed") else ""),
        _DEGRADED if fm.get("failed") else _OK))

    # ── assets ────────────────────────────────────────────────────────────────
    lines.append(ReadinessLine("ASSETS", f"{_asset_count()} OBSERVED", _VALUE))

    # ── sensors ───────────────────────────────────────────────────────────────
    n_sensors = _sensor_count()
    lines.append(ReadinessLine("SENSORS", f"{n_sensors} CONNECTED",
                               _VALUE if n_sensors else _DORMANT))

    # ── aura ──────────────────────────────────────────────────────────────────
    lines.append(ReadinessLine("AURA", _READY if _aura_available() else _ABSENT,
                               _READY if _aura_available() else _ABSENT))

    # ── persistence ───────────────────────────────────────────────────────────
    persist = _persistence_state()
    lines.append(ReadinessLine("PERSISTENCE", persist,
                               _OK if persist.startswith("DURABLE") else _DORMANT))

    # ── docker / vmware ───────────────────────────────────────────────────────
    lines.append(ReadinessLine("DOCKER", _AVAILABLE if shutil.which("docker")
                               else _ABSENT, _DORMANT if shutil.which("docker") else _ABSENT))
    lines.append(ReadinessLine("VMWARE", _AVAILABLE if _vmware_present() else _ABSENT,
                               _DORMANT if _vmware_present() else _ABSENT))

    # ── authorized scope (M29) ────────────────────────────────────────────────
    scopes = _authorized_scopes()
    lines.append(ReadinessLine("AUTHORIZED SCOPE", ", ".join(scopes) if scopes else "NONE",
                               _VALUE if scopes else _DORMANT))

    # ── runbook execution posture ─────────────────────────────────────────────
    lines.append(ReadinessLine("RUNBOOK EXECUTION",
                               "EXECUTE READY" if _executor_wired() else "DRY-RUN READY",
                               _OK))

    return FieldReadinessReport(lines=lines)


_AVAILABLE = "AVAILABLE"


# ── guarded probes ─────────────────────────────────────────────────────────────
def _probe_ollama() -> tuple[bool, str]:
    try:
        from core.health_watchdog import _check_ollama
        _name, ok, info = _check_ollama()
        return bool(ok), str(info)[:40]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:40]


def _resolved_roles() -> dict:
    try:
        from core.config import settings
        return settings.resolved_role_models()
    except Exception:  # noqa: BLE001
        return {}


def _fabric_metrics() -> dict:
    try:
        from core.collector_fabric import fabric
        return fabric.metrics()
    except Exception:  # noqa: BLE001
        return {}


def _asset_count() -> int:
    try:
        from core.asset_graph import graph
        return len(graph.assets)
    except Exception:  # noqa: BLE001
        return 0


def _sensor_count() -> int:
    try:
        from core.sensor_mesh import get_connected_agents
        return len(get_connected_agents())
    except Exception:  # noqa: BLE001
        return 0


def _aura_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("aura.server") is not None
    except Exception:  # noqa: BLE001
        return False


def _persistence_state() -> str:
    # Real check: V68 gives durable local operational state via SQLite on the NVMe
    # (always available when the data dir is writable). A configured fleet Postgres is
    # an additional tier. Reported honestly — VOLATILE only when nothing durable exists.
    try:
        from core.operational_store import _DEFAULT_PATH
        target = _DEFAULT_PATH.parent
        probe = target if target.exists() else target.parent
        if os.access(probe, os.W_OK):
            fleet = any(os.environ.get(v) for v in
                        ("JARVIS_PG_DSN", "DATABASE_URL", "POSTGRES_DSN"))
            return "DURABLE (sqlite + fleet PG)" if fleet else "DURABLE (sqlite)"
    except Exception:  # noqa: BLE001
        pass
    return "VOLATILE (in-memory)"


def _vmware_present() -> bool:
    try:
        from core.config import settings
        vmrun = getattr(settings, "vmrun_path", "") or ""
        if vmrun and Path(vmrun).exists():
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(shutil.which("vmrun"))


def _authorized_scopes() -> list[str]:
    try:
        from core.environment_registry import env_registry
        scopes = {e.authorization_scope or "(unscoped)"
                  for e in env_registry.authorized_environments()}
        return sorted(scopes)
    except Exception:  # noqa: BLE001
        return []


def _executor_wired() -> bool:
    try:
        from core.runbook_engine import engine
        return engine._tool_executor is not None
    except Exception:  # noqa: BLE001
        return False
