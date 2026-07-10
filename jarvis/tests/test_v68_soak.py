"""tests/test_v68_soak.py — V68 M44 long-run soak & production validation.

A DETERMINISTIC, accelerated soak: it simulates 24 hours of continuous operation by
stepping an injected clock (no real sleeping, no wall-clock) and asserts that every V68
domain stays BOUNDED (Rule of Silicon — memory must not grow with uptime), stays correct,
and degrades honestly. It is the "does it survive a shift in the field?" gate.

Domains exercised over the simulated day:
  * M39 telemetry  — 1440 minute-ticks per collector; the arrival ring stays capped while
    the lifetime counter still totals everything; derived state stays sane across drops,
    restarts and a silent period.
  * M38 store      — thousands of journal appends stay pruned to the retention cap; idempotent
    replay writes nothing; corruption stays isolated.
  * M41 sensors    — a churning mesh yields bounded, ASCII output every hour.
  * M42 timeline   — a day of findings/drift stays within the entry/link caps.
  * M43 decisions  — ranking is pure and stable across repeated evaluation.

Plus a production-validation pass: each live builder runs over cold singletons without
crashing and returns a bounded dict.

Fast (well under a second of real time); no network, no Ollama, no sleeping.
"""
from __future__ import annotations

from core.causal_timeline import Epistemic, TimelineEntry, build_timeline
from core.decision_support import DecisionOption, Level, rank_options
from core.operational_store import OperationalStore
from core.sensor_intel import assess_mesh
from core.telemetry_intel import _WINDOW, TelemetryRegistry, TelemetryState

T0 = 1_752_000_000.0
DAY_S = 86_400
MINUTE = 60


# ── M39: a full simulated day of telemetry stays bounded and sane ────────────────
class TestTelemetrySoak:
    def test_ring_bounded_over_24h(self):
        reg = TelemetryRegistry()
        # two collectors emit once a minute for 24h = 1440 events each
        for i in range(DAY_S // MINUTE):
            now = T0 + i * MINUTE
            reg.record("edr", event={"observed_at": now - 2}, now=now)
            reg.record("netflow", event={"observed_at": now - 1}, now=now)
        edr = reg.meter("edr")
        assert edr.events == 1440
        assert len(edr._arrivals) == _WINDOW          # ring capped, memory bounded
        snap = reg.snapshot("edr", now=T0 + DAY_S)
        assert snap["state"] in {s.value for s in TelemetryState}

    def test_state_transitions_over_day(self):
        reg = TelemetryRegistry()
        m = reg.meter("c")
        # produce for an hour
        for i in range(60):
            m.record(now=T0 + i * MINUTE)
        assert reg.snapshot("c", now=T0 + 60 * MINUTE)["state"] == TelemetryState.HEALTHY.value
        # then go silent for the rest of the day -> stale, then blind
        assert reg.snapshot("c", now=T0 + 60 * MINUTE + 400)["state"] == TelemetryState.STALE.value
        assert reg.snapshot("c", now=T0 + DAY_S)["state"] == TelemetryState.BLIND.value

    def test_restart_and_drop_pressure_dont_grow_memory(self):
        reg = TelemetryRegistry()
        for i in range(500):
            now = T0 + i * MINUTE
            reg.record("flappy", now=now)
            if i % 5 == 0:
                reg.record_restart("flappy", now=now)
            if i % 3 == 0:
                reg.record_drop("flappy", 1)
        m = reg.meter("flappy")
        assert len(m._arrivals) == _WINDOW
        assert len(m._restarts) <= 64                 # restart ring bounded


# ── M38: durable store stays pruned across a long run ────────────────────────────
class TestStoreSoak:
    def test_journal_retention_bounded(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        for i in range(5000):
            s.append("events", {"i": i}, dedup_window=1)
            if i % 500 == 0:
                s.retention("events", 1000)
        s.retention("events", 1000)
        assert len(s.history("events", limit=10000)) <= 1000

    def test_idempotent_replay_after_long_run(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        for _ in range(1000):
            r = s.put("d", "k", {"stable": True})
        assert r.outcome == "unchanged" and r.version == 1

    def test_corruption_isolated_under_load(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        for i in range(100):
            s.put("d", f"k{i}", {"i": i})
        s._db.execute("INSERT INTO records(domain,entity_id,version,schema_version,"
                      "content_hash,payload,updated_at) VALUES('d','bad',1,1,'h','{bad',"
                      "'2026-01-01')")
        rows = s.all("d")
        assert len(rows) == 100                        # only the good rows survive
        assert s.health()["corrupt_reads"] >= 1


# ── M41 / M42 / M43: bounded & stable across the day ─────────────────────────────
class TestOtherDomainsSoak:
    def test_sensor_mesh_bounded_hourly(self):
        agents = [{"agent_id": f"a{i}", "hostname": f"h{i}", "ip": "10.0.0.1",
                   "connected": str(T0), "events_received": i,
                   "last_event_at": str(T0 + 1), "transport": "localhost-tunnel"}
                  for i in range(500)]
        for hour in range(24):
            m = assess_mesh(agents, now=None, telemetry={}, expected=[])
            d = m.to_dict()
            assert len(d["sensors"]) <= 64 and str(d).isascii()

    def test_timeline_bounded_over_day(self):
        entries = []
        for i in range(1000):
            kind = "finding" if i % 2 == 0 else "change"
            ep = Epistemic.CORRELATED if kind == "finding" else Epistemic.INFERRED
            entries.append(TimelineEntry(entry_id=f"e{i}", at=T0 + i * MINUTE, at_iso="",
                                         kind=kind, title=f"t{i}", epistemic=ep, entity="h"))
        tl = build_timeline(entries)
        d = tl.to_dict()
        assert len(d["entries"]) <= 200 and len(d["links"]) <= 400

    def test_decision_ranking_stable(self):
        opts = [
            DecisionOption("safe", "safe", risk=Level.LOW, impact=Level.LOW,
                           reversibility=Level.HIGH, info_gain=Level.HIGH,
                           uncertainty_reduction=Level.HIGH),
            DecisionOption("risky", "risky", risk=Level.HIGH, impact=Level.HIGH,
                           reversibility=Level.LOW, info_gain=Level.LOW,
                           uncertainty_reduction=Level.MED),
        ]
        first = rank_options(opts).top.option_id
        for _ in range(50):                            # deterministic across repeats
            assert rank_options(opts).top.option_id == first == "safe"


# ── production validation: live builders survive cold start ──────────────────────
class TestProductionValidation:
    def test_all_live_builders_run_without_crash(self):
        from core.causal_timeline import build_live_causal_timeline
        from core.field_readiness import assess_field_readiness
        from core.runtime_health import build_live_runtime_health
        from core.sensor_intel import build_live_sensor_intel

        assert build_live_runtime_health()["overall"]
        assert build_live_sensor_intel()["panel"] == "sensor_intel"
        assert build_live_causal_timeline()["panel"] == "causal_timeline"
        assert assess_field_readiness(probe_ollama=False).render().isascii()

    def test_readiness_persistence_is_durable(self):
        from core.field_readiness import assess_field_readiness
        r = assess_field_readiness(probe_ollama=False)
        line = next(ln for ln in r.lines if ln.label == "PERSISTENCE")
        assert line.value.startswith("DURABLE")        # V68: never fabricated volatile
