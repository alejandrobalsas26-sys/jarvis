"""tests/test_resilience_v67.py — V67 M35 chaos, failure & recovery validation.

Deterministic, SAFE failure injection against the REAL components. Each test asserts the
documented safe behaviour under a simulated fault — never a real outage, never a
destructive action:

  collector crash/backlog  -> DEGRADED/FAILED/BACKPRESSURE, bounded queue, no crash
  duplicate/out-of-order   -> one finding + one incident (no incident explosion)
  sensor heartbeat loss    -> uncertainty, NO fabricated compromise, never "secure"
  Ollama/model down        -> model DEGRADED but monitoring continues, no retry storm
  database unavailable     -> explicit empty/volatile state, no fake persistence claim
  Docker/VMware absent     -> graceful None / tolerant parse, no crash
  ToolExecutor denial      -> honored, no bypass
  HITL required, no approver-> blocked (fail-closed), the gated action never runs
  cancellation             -> cancelled, no world effect

Pure: no Ollama, no network, no subprocess side effects; the whole suite replays offline.
"""
from __future__ import annotations

import asyncio
import shutil

from core.asset_discovery import apply_discovery, parse_docker_ps, probe_docker_inventory
from core.collector_fabric import (
    BoundedCollectorQueue,
    Collector,
    CollectorFabric,
    CollectorSpec,
    CollectorStatus,
)
from core.environment_registry import EnvironmentRegistry, EnvironmentType
from core.ops_query import OperationalContext, answer_question
from core.runbook_engine import CancelToken, RunbookEngine
from core.runtime_health import HealthStatus, collect_runtime_health
from core.scenario_harness import SCENARIOS, ScenarioHarness

T0 = "2026-07-08T12:00:00+00:00"


def _col(**spec_kw):
    return Collector(spec=CollectorSpec("c1", "t", "C1", **spec_kw))


class _FakeWatchdog:
    def __init__(self, status=None):
        self.registered: dict = {}
        self._status = dict(status or {})

    def register(self, name, factory, policy=None):
        self.registered[name] = factory
        return ("task", name)

    def get_status(self):
        return self._status


# ── collector failure & recovery ─────────────────────────────────────────────
class TestCollectorResilience:
    def test_crash_is_failed_restart_is_degraded(self):
        col = _col(is_configured=lambda: True)
        assert col.status("done") is CollectorStatus.FAILED       # crashed
        assert col.status("restarting") is CollectorStatus.DEGRADED  # bounded restart

    def test_backlog_is_backpressure_not_crash(self):
        col = _col(is_configured=lambda: True)
        col.record_event(now_iso=T0)
        col.mark_backpressure(True, backlog=500, drops=12)
        assert col.status("running") is CollectorStatus.BACKPRESSURE
        assert col.drops == 12

    def test_bounded_queue_drops_oldest_and_accounts(self):
        q = BoundedCollectorQueue(maxsize=3)
        assert all(q.push(i) for i in range(3))
        assert q.push(99) is False        # over capacity -> drop oldest
        assert q.drops == 1 and q.depth == 3

    def test_ingestion_failure_never_crashes_the_fabric(self):
        fab = CollectorFabric()

        async def boom(ev):
            raise RuntimeError("ingest pipeline down")

        mb = fab.managed_broadcast("zeek-dpi", boom)
        asyncio.run(mb({"type": "x"}))     # must NOT raise
        assert "ingest pipeline down" in (fab.get("zeek-dpi").last_error or "")

    def test_dormant_collector_is_not_restart_looped(self):
        fab = CollectorFabric()
        wd = _FakeWatchdog()
        spec = CollectorSpec("sliver-monitor", "sliver", "Sliver", is_configured=lambda: False)
        out = fab.supervise(spec, lambda b: None, base_broadcast=lambda e: None, watchdog=wd)
        assert out is None                 # never registered -> no retry storm
        assert "sliver-monitor" not in wd.registered


# ── event storms: duplicate / out-of-order do not explode ────────────────────
class TestEventStorms:
    def test_duplicate_and_out_of_order_yield_one_incident(self):
        out = ScenarioHarness().run(SCENARIOS["duplicate_out_of_order"])
        assert len(out.findings) == 1
        assert len(out.incidents) == 1


# ── sensor loss: uncertainty, never fabricated compromise or safety ──────────
class TestSensorLoss:
    def test_no_fabricated_compromise_but_uncertainty(self):
        out = ScenarioHarness().run(SCENARIOS["sensor_loss"])
        assert len(out.incidents) == 0
        assert len(out.situation.uncertainties) > 0

    def test_query_never_claims_secure_under_blindness(self):
        out = ScenarioHarness().run(SCENARIOS["sensor_loss"])
        ctx = OperationalContext(situation=out.situation, twin_snapshot=out.drift,
                                 sensors={"edge-01-agent": "disconnected"})
        for q in ("Which sensors are blind?", "What is uncertain?",
                  "Which assets are unhealthy?"):
            ans = answer_question(q, context=ctx).answer.lower()
            assert "secure" not in ans


# ── Ollama / model down: degrade, keep monitoring, no retry storm ────────────
class TestModelDegradation:
    def test_model_down_degrades_but_monitoring_continues(self):
        snap = collect_runtime_health(
            model={"roles": {}, "probe": "not_checked"},   # Ollama/model unresolved
            fabric_metrics={"total": 4, "active": 2, "dormant": 2, "failed": 0,
                            "backpressure": 0, "events_emitted": 50, "drops": 0},
            spine={"correlation_findings": 2, "incidents_open": 1},
            resource={"cpu_percent": 30.0, "ram_percent": 40.0},
            watchdog_status={"zeek": "running"}, profiler={})
        model = next(s for s in snap.subsystems if s.name == "model_runtime")
        collectors = next(s for s in snap.subsystems if s.name == "collectors")
        assert model.status is HealthStatus.DEGRADED     # degraded intelligence
        assert collectors.status is HealthStatus.OK      # monitoring continues

    def test_health_snapshot_never_raises_even_if_sources_missing(self):
        # A completely empty world still yields a well-formed snapshot (no exception).
        snap = collect_runtime_health(fabric_metrics={}, watchdog_status={}, resource=None,
                                      profiler={}, model={}, spine={})
        assert snap.overall in {s for s in HealthStatus}


# ── database / persistence unavailable: explicit, never faked ────────────────
class TestPersistenceLoss:
    def test_missing_store_loads_empty_not_crash(self, tmp_path):
        reg = EnvironmentRegistry.load(tmp_path / "nope.json")
        assert reg.all() == []             # volatile/empty, no fake data

    def test_corrupt_store_degrades_to_empty(self, tmp_path):
        p = tmp_path / "envs.json"
        p.write_text("{ this is not valid json ", encoding="utf-8")
        reg = EnvironmentRegistry.load(p)  # must not raise
        assert reg.all() == []

    def test_roundtrip_when_available(self, tmp_path):
        reg = EnvironmentRegistry()
        reg.enroll("docker-local", EnvironmentType.DOCKER, "Local", authorized=True)
        p = tmp_path / "envs.json"
        reg.save(p)
        assert EnvironmentRegistry.load(p).is_authorized("docker-local") is True


# ── Docker / VMware unavailable: graceful degradation ────────────────────────
class TestHypervisorAbsence:
    def test_docker_probe_returns_none_when_absent(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _n: None)   # docker not installed
        assert asyncio.run(probe_docker_inventory()) is None    # graceful, no crash

    def test_parse_tolerates_empty_and_garbage(self):
        assert parse_docker_ps("") == []
        assert parse_docker_ps("garbage line\n{not json\n") == []

    def test_empty_inventory_writes_nothing_without_crashing(self):
        from core.asset_graph import AssetGraph
        reg = EnvironmentRegistry()
        entry = reg.enroll("docker-local", EnvironmentType.DOCKER, "Local",
                           endpoint="h", authorized=True)
        g = AssetGraph()
        res = apply_discovery(entry, g, {"host_identity": "h", "containers": []}, now_iso=T0)
        assert res.services == 0 and res.error is None


# ── control plane under failure: denial / HITL / cancellation, no bypass ─────
class TestControlPlaneFailure:
    def test_no_executor_cannot_effect_the_world(self):
        eng = RunbookEngine()   # no ToolExecutor wired
        res = asyncio.run(eng.execute("AUTH_FAILURE_TRIAGE", {"host": "h"}))
        assert res.status != "completed"     # fail-closed: no world effect happened

    def test_tool_denial_is_honored_no_bypass(self):
        async def _deny(tool, args, reasoning):
            return {"error": "denied by scope policy"}

        eng = RunbookEngine(exec_fn=_deny)
        res = asyncio.run(eng.execute("AUTH_FAILURE_TRIAGE", {"host": "h"}))
        assert res.status in ("failed", "partial", "blocked")
        assert all(a.status != "completed" or a.action is None for a in res.audit)

    def _gated_engine(self):
        """An engine with a single approval-gated ACTION step (the gate is hit first,
        isolating the HITL behaviour from any preceding diagnostic)."""
        from core.runbook_engine import (
            ParamType,
            RunbookDefinition,
            RunbookParameter,
            RunbookStep,
            StepKind,
        )
        calls = {"n": 0}

        async def _spy(tool, args, reasoning):
            calls["n"] += 1
            return {"ok": True}

        eng = RunbookEngine(exec_fn=_spy)
        eng.registry.register(RunbookDefinition(
            name="TEST_GATED", description="single gated action",
            parameters=(RunbookParameter("host", ParamType.TARGET),),
            steps=(RunbookStep("act", "gated action", StepKind.ACTION, "network_scan",
                               {"target": "{{host}}"}, requires_approval=True),)))
        return eng, calls

    def test_hitl_step_without_approver_is_blocked(self):
        eng, calls = self._gated_engine()
        res = asyncio.run(eng.execute("TEST_GATED", {"host": "10.0.0.5"}))  # no approver
        assert res.status == "blocked"       # fail-closed at the HITL gate
        assert calls["n"] == 0               # the gated action never ran

    def test_denied_approval_blocks_the_action(self):
        async def _deny_approval(step):
            return False

        eng, calls = self._gated_engine()
        res = asyncio.run(eng.execute("TEST_GATED", {"host": "10.0.0.5"},
                                      approval_fn=_deny_approval))
        assert res.status == "blocked"
        assert calls["n"] == 0               # denied approval -> action never ran

    def test_cancellation_yields_cancelled_no_effect(self):
        token = CancelToken()
        token.cancel()
        ran = {"n": 0}

        async def _count(tool, args, reasoning):
            ran["n"] += 1
            return {"ok": True}

        eng = RunbookEngine(exec_fn=_count)
        res = asyncio.run(eng.execute("AUTH_FAILURE_TRIAGE", {"host": "h"}, cancel=token))
        assert res.status == "cancelled"
        assert ran["n"] == 0                 # nothing executed under a cancelled token
