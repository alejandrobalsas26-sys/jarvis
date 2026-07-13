"""tests/test_runtime_health_v67.py — V67 M34 unified runtime & collector health.

Proves the health model COMPOSES existing sources into one bounded, read-only snapshot
using the fabric's 8-status vocabulary, and that:
  * per-subsystem status derives correctly (collectors failed -> degraded, backpressure
    -> backpressure, resource pressure -> degraded/warming, a down task -> degraded);
  * DORMANT/OPTIONAL are healthy — an unconfigured collector or an unmeasured metric is
    reported honestly, never as a failure and never fabricated;
  * overall is the worst UNHEALTHY subsystem (dormant/optional never degrade it);
  * the live snapshot is well-formed and non-blocking (callable with no event loop).

Pure: injected raw data; the live path is read-only and needs no Ollama/HUD.
"""
from __future__ import annotations

from core.runtime_health import (
    HealthStatus,
    _resource_subsystem,
    build_live_runtime_health,
    collect_runtime_health,
)

_HEALTHY_FABRIC = {"total": 5, "active": 2, "dormant": 3, "degraded": 0, "failed": 0,
                   "backpressure": 0, "events_emitted": 100, "drops": 4}
_MODEL = {"roles": {"fast": "qwen3:8b", "deep": "qwen3:14b"}, "probe": "not_checked"}


def _snap(**over):
    base = dict(fabric_metrics=_HEALTHY_FABRIC, watchdog_status={"zeek": "running"},
                resource={"cpu_percent": 40.0, "ram_percent": 55.0},
                profiler={"llm_inference": {"count": 3, "p50_s": 1.0, "p95_s": 2.0}},
                model=_MODEL, spine={"correlation_findings": 1, "incidents_open": 0})
    base.update(over)
    return collect_runtime_health(**base)


def _sub(snap, name):
    return next(s for s in snap.subsystems if s.name == name)


# ── status derivation ─────────────────────────────────────────────────────────
class TestStatusDerivation:
    def test_healthy_baseline_is_ok(self):
        snap = _snap()
        assert snap.overall is HealthStatus.OK
        assert snap.degraded == []

    def test_failed_collector_degrades(self):
        fm = dict(_HEALTHY_FABRIC, failed=1)
        snap = _snap(fabric_metrics=fm)
        assert _sub(snap, "collectors").status is HealthStatus.DEGRADED
        assert snap.overall is HealthStatus.DEGRADED

    def test_backpressure_surfaces(self):
        fm = dict(_HEALTHY_FABRIC, backpressure=1)
        assert _sub(_snap(fabric_metrics=fm), "collectors").status is HealthStatus.BACKPRESSURE

    def test_resource_pressure_degrades(self):
        snap = _snap(resource={"cpu_percent": 95.0, "ram_percent": 70.0})
        assert _sub(snap, "resource").status is HealthStatus.DEGRADED
        assert snap.overall is HealthStatus.DEGRADED

    def test_resource_warming_band(self):
        snap = _snap(resource={"cpu_percent": 80.0, "ram_percent": 70.0})
        assert _sub(snap, "resource").status is HealthStatus.WARMING

    def test_down_task_degrades(self):
        snap = _snap(watchdog_status={"zeek": "running", "etw": "done"})
        assert _sub(snap, "tasks").status is HealthStatus.DEGRADED
        assert "tasks" in snap.degraded


# ── honesty: dormant/optional/unmeasured are not failures, not fabricated ─────
class TestHonesty:
    def test_unmeasured_resource_is_optional_not_ok(self):
        # When psutil is unavailable the reading is absent — reported honestly as
        # OPTIONAL (healthy, unmeasured), never OK/fabricated. (None to
        # collect_runtime_health means "read live", so test the pure builder.)
        for empty in (None, {}):
            sub = _resource_subsystem(empty)
            assert sub.status is HealthStatus.OPTIONAL
            assert sub.healthy is True
            assert "not measured" in sub.detail

    def test_dormant_collectors_do_not_degrade_overall(self):
        fm = {"total": 5, "active": 0, "dormant": 5, "degraded": 0, "failed": 0,
              "backpressure": 0, "events_emitted": 0, "drops": 0}
        snap = _snap(fabric_metrics=fm)
        assert _sub(snap, "collectors").status is HealthStatus.DORMANT
        assert snap.overall in (HealthStatus.OK, HealthStatus.DORMANT)
        assert "collectors" not in snap.degraded

    def test_no_inference_samples_is_dormant_not_ok(self):
        snap = _snap(profiler={})
        assert _sub(snap, "inference").status is HealthStatus.DORMANT

    def test_drop_ratio_is_computed_not_faked(self):
        snap = _snap()  # 4 drops / (100+4)
        assert snap.metrics["collectors.drop_ratio"] == round(4 / 104, 4)


# ── shape + live path ─────────────────────────────────────────────────────────
class TestShapeAndLive:
    def test_snapshot_has_all_subsystems(self):
        names = {s.name for s in _snap().subsystems}
        assert names == {"collectors", "resource", "tasks", "inference",
                         "model_runtime", "spine", "verifier"}

    def test_metrics_are_flat_and_bounded(self):
        d = _snap().to_dict()
        assert d["panel"] == "runtime_health"
        assert all("." in k for k in d["metrics"])   # namespaced subsystem.metric
        assert "roles" not in " ".join(d["metrics"])  # nested maps excluded from flat metrics

    def test_summary_is_ascii(self):
        s = _snap().summary()
        assert s.isascii()
        assert s.startswith("RUNTIME")

    def test_live_snapshot_is_wellformed_and_nonblocking(self):
        # Callable with no running event loop → no awaiting, no active probe.
        live = build_live_runtime_health()
        assert live["panel"] == "runtime_health"
        assert {s["name"] for s in live["subsystems"]} >= {"collectors", "model_runtime", "spine"}
        assert live["overall"] in {s.value for s in HealthStatus}
