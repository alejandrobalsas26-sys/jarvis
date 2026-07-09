"""tests/test_operational_store_v68.py — V68 M38 durable operational state & recovery.

Proves the store is durable, idempotent, and degrades honestly:
  * restart recovery — enrolled environments, incident cases, expected/desired twin
    baseline, and the asset graph all survive a close + reopen;
  * idempotent replay — re-persisting unchanged state writes nothing; a changed payload
    bumps the version (conflict visibility);
  * journal dedup + retention — repeated history events collapse; a bounded domain is
    pruned to its cap;
  * corrupted-record isolation — a bad stored row is skipped + counted on read (never
    fatal); a non-JSON input record is quarantined by reconcile;
  * database unavailable / volatile — an in-memory or unopenable store reports
    durable=False and NEVER claims durable persistence;
  * schema forward-compat — a store written by a newer schema reads without crashing.

Pure: stdlib sqlite3 + tmp files; no Ollama, no network.
"""
from __future__ import annotations

import sqlite3

from core.asset_graph import AssetGraph, AssetType, ObservationSource
from core.digital_twin import DigitalTwin, FactKind
from core.environment_registry import EnvironmentRegistry, EnvironmentType
from core.incident_workspace import IncidentWorkspace
from core.operational_store import (
    OperationalStore,
    persist_asset_graph,
    persist_environments,
    persist_incidents,
    persist_twin_baseline,
    record_verification,
    restore_asset_graph,
    restore_environments,
    restore_incidents,
    restore_twin_baseline,
    _D_ENV,
    _J_VERIFICATION,
)
from core.scenario_harness import SCENARIOS, ScenarioHarness

T0 = "2026-07-08T12:00:00+00:00"


def _graph_with_assets():
    g = AssetGraph()
    g.observe_service(AssetType.SERVER, "web-1", port=443, protocol="tcp",
                      service_name="https", exposure="external", bind_addr="0.0.0.0",
                      source=ObservationSource.SERVICE_OBSERVATION, observer="t", now_iso=T0)
    g.add_observation(AssetType.PHYSICAL_HOST, "ryzen", "os", "Windows 11",
                      source=ObservationSource.OPERATOR_DECLARATION, observer="t", now_iso=T0)
    return g


# ── restart recovery ──────────────────────────────────────────────────────────
class TestRestartRecovery:
    def test_environments_survive_restart(self, tmp_path):
        p = tmp_path / "op.db"
        reg = EnvironmentRegistry()
        reg.enroll("docker-local", EnvironmentType.DOCKER, "Local", authorized=True)
        reg.authorize("docker-local", scope="LAB-A")
        s = OperationalStore(p)
        persist_environments(s, reg)
        s.close()

        reg2 = EnvironmentRegistry()
        n = restore_environments(OperationalStore(p), reg2)
        assert n == 1
        assert reg2.is_authorized("docker-local") is True
        assert reg2.get("docker-local").authorization_scope == "LAB-A"

    def test_incidents_survive_restart(self, tmp_path):
        p = tmp_path / "op.db"
        out = ScenarioHarness().run(SCENARIOS["auth_sequence"])
        ws = IncidentWorkspace()
        for c in out.incidents:
            ws.cases[c.incident_id] = c
        s = OperationalStore(p)
        persist_incidents(s, ws)
        s.close()

        ws2 = IncidentWorkspace()
        restore_incidents(OperationalStore(p), ws2)
        assert sorted(ws.cases) == sorted(ws2.cases)
        assert ws2.cases[out.incidents[0].incident_id].to_dict()["timeline"]

    def test_twin_baseline_survives_restart(self, tmp_path):
        p = tmp_path / "op.db"
        tw = DigitalTwin()
        tw.set_expected("h", "sensor:mesh", "connected", kind=FactKind.SENSOR)
        tw.set_desired("h", "cfg", "v1", kind=FactKind.VERSION)
        s = OperationalStore(p)
        persist_twin_baseline(s, tw)
        s.close()

        tw2 = DigitalTwin()
        restore_twin_baseline(OperationalStore(p), tw2)
        assert "h" in tw2._expected and "h" in tw2._desired
        assert tw2._expected["h"].get("sensor:mesh").value == "connected"

    def test_asset_graph_survives_restart(self, tmp_path):
        p = tmp_path / "op.db"
        g = _graph_with_assets()
        s = OperationalStore(p)
        assert persist_asset_graph(s, g) == 1
        s.close()

        g2 = AssetGraph()
        restore_asset_graph(OperationalStore(p), g2)
        assert set(g2.assets) == set(g.assets)
        assert g2.get(AssetType.PHYSICAL_HOST, "ryzen").current("os")[0] == "Windows 11"


# ── idempotency & conflict visibility ────────────────────────────────────────
class TestIdempotency:
    def test_unchanged_replay_writes_nothing(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        r1 = s.put("d", "k", {"a": 1})
        r2 = s.put("d", "k", {"a": 1})
        assert r1.outcome == "written" and r2.outcome == "unchanged"
        assert r2.version == 1

    def test_changed_payload_bumps_version(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        s.put("d", "k", {"a": 1})
        r = s.put("d", "k", {"a": 2})
        assert r.outcome == "written" and r.version == 2


# ── journal dedup + retention ─────────────────────────────────────────────────
class TestJournal:
    def test_dedup_and_history(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        assert record_verification(s, "host-x", True) is True
        assert record_verification(s, "host-x", True) is False   # duplicate
        assert len(s.history(_J_VERIFICATION)) == 1

    def test_retention_prunes_to_cap(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        for i in range(50):
            s.append("hist", {"i": i}, dedup_window=1)
        pruned = s.retention("hist", 10)
        assert pruned == 40
        assert len(s.history("hist", limit=100)) == 10


# ── corrupted-record isolation ────────────────────────────────────────────────
class TestCorruption:
    def test_bad_stored_row_is_isolated_on_read(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        s.put("d", "good", {"ok": 1})
        # simulate a partial/corrupt write directly in the DB
        s._db.execute("INSERT INTO records(domain,entity_id,version,schema_version,"
                      "content_hash,payload,updated_at) VALUES('d','bad',1,1,'h','{not json',"
                      "'2026-01-01')")
        rows = s.all("d")
        assert {r.get("ok") for r in rows} == {1}   # only the good row survives
        assert s.health()["corrupt_reads"] >= 1

    def test_non_json_input_is_quarantined_by_reconcile(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        res = s.reconcile("d", [("good", {"a": 1}), ("bad", {"x": {1, 2}})])  # set not JSON
        assert res.written == 1 and res.corrupted == 1
        assert res.corrupted_ids == ["bad"]


# ── degradation honesty ───────────────────────────────────────────────────────
class TestDegradation:
    def test_in_memory_is_not_durable(self):
        s = OperationalStore(":memory:")
        assert s.durable is False
        assert "VOLATILE" in s.health()["degraded_reason"]

    def test_file_store_is_durable(self, tmp_path):
        s = OperationalStore(tmp_path / "op.db")
        assert s.durable is True
        assert s.health()["backend"] == "sqlite"

    def test_unopenable_path_falls_back_volatile(self, monkeypatch, tmp_path):
        real = sqlite3.connect
        calls = {"n": 0}

        def _flaky(dbpath, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1 and dbpath != ":memory:":
                raise sqlite3.OperationalError("unable to open database file")
            return real(":memory:", *a, **k)

        monkeypatch.setattr(sqlite3, "connect", _flaky)
        s = OperationalStore(tmp_path / "op.db")
        assert s.durable is False                    # honest: fell back to volatile
        assert "VOLATILE" in s.health()["degraded_reason"]


# ── schema forward-compat ─────────────────────────────────────────────────────
class TestSchema:
    def test_newer_schema_reads_without_crashing(self, tmp_path):
        p = tmp_path / "op.db"
        s = OperationalStore(p)
        s.put(_D_ENV, "e", {"env_id": "e"})
        s._meta_set("schema_version", "999")         # pretend a newer JARVIS wrote it
        s.close()
        s2 = OperationalStore(p)                      # must not crash
        assert s2.get(_D_ENV, "e") == {"env_id": "e"}
