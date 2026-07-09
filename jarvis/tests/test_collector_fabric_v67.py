"""tests/test_collector_fabric_v67.py — V67 M28 unified collector fabric.

Proves the fabric standardizes lifecycle/health over existing producers WITHOUT
replacing the watchdog or the ingestion boundary:
  * managed broadcast counts events + heartbeat and forwards UNCHANGED (never
    mutates a signed HMAC envelope) and never raises;
  * status is dormant/optional-aware (unconfigured != failed) and derives live
    state from the watchdog;
  * supervise() delegates restart to the watchdog and no-ops when dormant;
  * BoundedCollectorQueue gives real drop-oldest backpressure accounting;
  * the default catalog predicates gate DORMANT vs live purely.

No Ollama / no event loop needed beyond asyncio.run for the async paths.
"""
from __future__ import annotations

import asyncio

from core.collector_fabric import (
    BoundedCollectorQueue,
    Collector,
    CollectorFabric,
    CollectorSpec,
    CollectorStatus,
    default_collector_catalog,
)

T0 = "2026-07-08T12:00:00+00:00"


class _FakeWatchdog:
    """Captures register() calls; get_status() returns a scripted map."""
    def __init__(self, status=None):
        self.registered: dict[str, object] = {}
        self._status = dict(status or {})

    def register(self, name, factory, policy=None):
        self.registered[name] = factory
        return ("task", name)

    def get_status(self):
        return self._status


class _FakeSettings:
    def __init__(self, zeek_log_dir=""):
        self.zeek_log_dir = zeek_log_dir


# ── managed broadcast ─────────────────────────────────────────────────────────
class TestManagedBroadcast:
    def test_counts_and_forwards_unchanged(self):
        fab = CollectorFabric()
        seen = []

        async def base(ev):
            seen.append(ev)

        mb = fab.managed_broadcast("sysmon-bridge", base)
        event = {"type": "sysmon_event", "process": "x.exe"}
        asyncio.run(mb(event))

        col = fab.get("sysmon-bridge")
        assert col.events_emitted == 1
        assert col.last_event_at is not None and col.last_success is not None
        # forwarded the SAME object, unmutated
        assert seen == [event]
        assert seen[0] is event
        assert event == {"type": "sysmon_event", "process": "x.exe"}

    def test_signed_envelope_not_mutated(self):
        fab = CollectorFabric()
        got = []

        async def base(ev):
            got.append(dict(ev))

        mb = fab.managed_broadcast("zeek-dpi", base)
        envelope = {"__src": "zeek", "__sig": "deadbeef", "__payload": {"type": "dpi_alert"}}
        asyncio.run(mb(envelope))
        assert got == [{"__src": "zeek", "__sig": "deadbeef", "__payload": {"type": "dpi_alert"}}]

    def test_never_raises_records_error(self):
        fab = CollectorFabric()

        async def boom(ev):
            raise RuntimeError("ingestion down")

        mb = fab.managed_broadcast("etw-monitor", boom)
        asyncio.run(mb({"type": "x"}))  # must not raise
        col = fab.get("etw-monitor")
        assert col.events_emitted == 1
        assert "ingestion down" in (col.last_error or "")


# ── status derivation ─────────────────────────────────────────────────────────
class TestStatus:
    def _col(self, **spec_kw):
        spec = CollectorSpec("c1", "t", "C1", **spec_kw)
        return Collector(spec=spec)

    def test_unconfigured_optional_is_optional(self):
        col = self._col(is_configured=lambda: False, optional=True)
        assert col.status(None) is CollectorStatus.OPTIONAL

    def test_unconfigured_required_is_dormant(self):
        col = self._col(is_configured=lambda: False, optional=False)
        assert col.status(None) is CollectorStatus.DORMANT

    def test_running_but_silent_is_warming(self):
        col = self._col(is_configured=lambda: True)
        assert col.status("running") is CollectorStatus.WARMING

    def test_running_with_events_is_ok(self):
        col = self._col(is_configured=lambda: True)
        col.record_event(now_iso=T0)
        assert col.status("running") is CollectorStatus.OK

    def test_restarting_is_degraded(self):
        col = self._col(is_configured=lambda: True)
        assert col.status("restarting") is CollectorStatus.DEGRADED

    def test_done_is_failed(self):
        col = self._col(is_configured=lambda: True)
        assert col.status("done") is CollectorStatus.FAILED

    def test_backpressure_flag_wins(self):
        col = self._col(is_configured=lambda: True)
        col.record_event(now_iso=T0)
        col.mark_backpressure(True, backlog=99, drops=3)
        assert col.status("running") is CollectorStatus.BACKPRESSURE
        assert col.backlog == 99 and col.drops == 3

    def test_stopping_wins(self):
        col = self._col(is_configured=lambda: True)
        col.mark_stopping()
        assert col.status("running") is CollectorStatus.STOPPING

    def test_bad_predicate_is_treated_unconfigured(self):
        def boom():
            raise ValueError("nope")
        col = self._col(is_configured=boom, optional=True)
        assert col.status("running") is CollectorStatus.OPTIONAL


# ── fabric views ──────────────────────────────────────────────────────────────
class TestFabricViews:
    def _fab(self):
        fab = CollectorFabric()
        fab.register(CollectorSpec("sysmon-bridge", "sysmon", "Sysmon",
                                   is_configured=lambda: True, signed_source="sysmon"))
        fab.register(CollectorSpec("zeek-dpi", "zeek", "Zeek",
                                   is_configured=lambda: False))  # dormant
        fab.attach_watchdog(_FakeWatchdog({"sysmon-bridge": "running"}))
        return fab

    def test_health_snapshot_shape(self):
        fab = self._fab()
        fab.get("sysmon-bridge").record_event(now_iso=T0)
        snap = fab.health_snapshot()
        assert snap["sysmon-bridge"]["status"] == "ok"
        assert snap["sysmon-bridge"]["signed"] is True
        assert snap["zeek-dpi"]["status"] in ("optional", "dormant")
        assert snap["zeek-dpi"]["healthy"] is True  # dormant is not a failure

    def test_metrics_buckets(self):
        fab = self._fab()
        fab.get("sysmon-bridge").record_event(now_iso=T0)
        m = fab.metrics()
        assert m["total"] == 2
        assert m["active"] >= 1
        assert m["dormant"] >= 1
        assert m["failed"] == 0
        assert m["events_emitted"] == 1

    def test_aura_panel_bounded_and_no_freetext(self):
        fab = self._fab()
        panel = fab.aura_panel()
        assert "metrics" in panel and "collectors" in panel
        assert len(panel["collectors"]) <= 24
        # rows carry only identity/status/counters — no command_line/secret keys
        for row in panel["collectors"]:
            assert set(row).issubset({
                "id", "name", "source", "status", "events", "last_event_at",
                "last_error", "signed",
            })


# ── supervise (watchdog delegation) ───────────────────────────────────────────
class TestSupervise:
    def test_configured_collector_registers_via_watchdog(self):
        fab = CollectorFabric()
        wd = _FakeWatchdog()
        fab.attach_watchdog(wd)
        spec = CollectorSpec("network-baseline", "nb", "NB", is_configured=lambda: True)

        async def start(bcast):  # producer stub
            await bcast({"type": "network_anomaly"})

        task = fab.supervise(spec, start, base_broadcast=lambda e: None, watchdog=wd)
        assert task is not None
        assert "network-baseline" in wd.registered
        assert fab.get("network-baseline").registered is True

    def test_dormant_collector_is_not_registered(self):
        fab = CollectorFabric()
        wd = _FakeWatchdog()
        spec = CollectorSpec("sliver-monitor", "sliver", "Sliver",
                             is_configured=lambda: False)
        out = fab.supervise(spec, lambda b: None, base_broadcast=lambda e: None, watchdog=wd)
        assert out is None
        assert "sliver-monitor" not in wd.registered
        # stays dormant/optional, never a restart loop
        assert fab.get("sliver-monitor").status(None) in (
            CollectorStatus.OPTIONAL, CollectorStatus.DORMANT)


# ── bounded queue ─────────────────────────────────────────────────────────────
class TestBoundedQueue:
    def test_drop_oldest_accounting(self):
        q = BoundedCollectorQueue(maxsize=3)
        assert all(q.push(i) for i in range(3))   # fills to capacity
        assert q.saturated
        assert q.push(3) is False                 # evicts oldest
        assert q.drops == 1
        assert q.depth == 3

    def test_drain_forwards(self):
        q = BoundedCollectorQueue(maxsize=8)
        out = []

        async def run():
            for i in range(5):
                q.push(i)
            drain = asyncio.create_task(q.drain(lambda x: _collect(x)))
            await asyncio.sleep(0.05)
            drain.cancel()

        async def _collect(x):
            out.append(x)

        asyncio.run(run())
        assert out == [0, 1, 2, 3, 4]


# ── default catalog predicates ────────────────────────────────────────────────
class TestDefaultCatalog:
    def test_zeek_dormant_without_logdir(self):
        specs = {s.collector_id: s for s in
                 default_collector_catalog(_FakeSettings(zeek_log_dir="/nope/zeek"), {})}
        assert specs["zeek-dpi"].configured() is False

    def test_sysmon_dormant_without_path(self):
        specs = {s.collector_id: s for s in
                 default_collector_catalog(_FakeSettings(), {})}
        assert specs["sysmon-bridge"].configured() is False

    def test_etw_gated_on_env(self):
        specs_off = {s.collector_id: s for s in
                     default_collector_catalog(_FakeSettings(), {})}
        assert specs_off["etw-monitor"].configured() is False
        specs_on = {s.collector_id: s for s in
                    default_collector_catalog(_FakeSettings(), {"JARVIS_ETW_ENABLE": "1"})}
        assert specs_on["etw-monitor"].configured() is True

    def test_always_on_detectors_are_configured(self):
        specs = {s.collector_id: s for s in
                 default_collector_catalog(_FakeSettings(), {})}
        assert specs["network-baseline"].configured() is True
        assert specs["resource-watchdog"].configured() is True
        assert specs["network-baseline"].optional is False

    def test_catalog_ids_match_watchdog_names(self):
        # ids MUST equal the TaskWatchdog register names main.py uses
        ids = {s.collector_id for s in default_collector_catalog(_FakeSettings(), {})}
        for expected in ("sysmon-bridge", "zeek-dpi", "etw-monitor", "ebpf-bridge",
                         "sliver-monitor", "network-baseline", "resource-watchdog"):
            assert expected in ids
