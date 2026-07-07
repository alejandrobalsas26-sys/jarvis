"""
tests/test_asset_graph.py — V66 M20 evidence-backed asset & service graph.

Covers the required M20 surface: add asset evidence, merge repeated observations,
preserve conflict (no silent overwrite), relationship evidence, service-exposure
query, neighbors, snapshot, diff, no invented facts, and scoped/compact retrieval.
"""
from __future__ import annotations

from core.asset_graph import (
    AssetGraph,
    AssetType,
    ObservationSource,
    RelationshipType,
    asset_id,
)

T0 = "2026-07-07T10:00:00+00:00"
T1 = "2026-07-07T11:00:00+00:00"
T2 = "2026-07-07T12:00:00+00:00"


def _g() -> AssetGraph:
    return AssetGraph()


# ── add asset evidence ────────────────────────────────────────────────────────
def test_add_asset_evidence_and_provenance():
    g = _g()
    obs = g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                            source=ObservationSource.OPERATOR_DECLARATION,
                            observer="operator", now_iso=T0)
    assert obs.source is ObservationSource.OPERATOR_DECLARATION
    assert obs.confidence >= 0.9  # operator declaration default is high
    a = g.get(AssetType.VM, "kali-vm")
    assert a is not None
    assert a.current("ip") == ("192.168.56.20", obs.confidence)
    # provenance preserved
    assert a.history("ip")[0].observer == "operator"
    assert a.history("ip")[0].content_hash


def test_asset_type_is_evidence_backed():
    g = _g()
    g.add_observation(AssetType.VM, "kali-vm", "os", "Kali Linux",
                      source=ObservationSource.SENSOR_MESH, now_iso=T0)
    a = g.get(AssetType.VM, "kali-vm")
    assert a.current_type() is AssetType.VM
    # asset_type itself is recorded as an observation
    assert "asset_type" in a.attributes


# ── merge repeated observations ───────────────────────────────────────────────
def test_merge_repeated_observation():
    g = _g()
    for ts in (T0, T1, T2):
        g.add_observation(AssetType.VM, "ubuntu-server", "ip", "192.168.56.10",
                          source=ObservationSource.CANONICAL_EVENT, now_iso=ts)
    a = g.get(AssetType.VM, "ubuntu-server")
    hist = a.history("ip")
    assert len(hist) == 1              # merged, not duplicated
    assert hist[0].count == 3
    assert hist[0].last_seen == T2


# ── preserve conflict (no silent overwrite) ───────────────────────────────────
def test_conflict_preserved_not_overwritten():
    g = _g()
    # Observation 1: 0.90; Observation 2: 0.80 — the M20 directive example.
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.90,
                      now_iso=T0)
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.21",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.80,
                      now_iso=T1)
    a = g.get(AssetType.VM, "kali-vm")
    # both observations preserved
    assert len(a.history("ip")) == 2
    # current is the higher-confidence value
    assert a.current("ip")[0] == "192.168.56.20"
    # conflict surfaced
    conflict = a.conflict("ip")
    assert conflict is not None
    values = {v for v, _c, _s in conflict.values}
    assert values == {"192.168.56.20", "192.168.56.21"}
    assert conflict.current_value == "192.168.56.20"
    # graph-level conflict aggregation includes it
    assert any(c.attribute == "ip" for c in g.get_conflicts())


def test_agreeing_observations_are_not_a_conflict():
    g = _g()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.SENSOR_MESH, now_iso=T1)
    a = g.get(AssetType.VM, "kali-vm")
    assert a.conflict("ip") is None
    # corroboration from a second distinct source raises aggregate confidence
    assert a.current("ip")[1] > 0.9


def test_operator_declaration_distinguishable():
    g = _g()
    g.add_observation(AssetType.VM, "kali-vm", "role", "attacker",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    a = g.get(AssetType.VM, "kali-vm")
    assert a.history("role")[0].source.is_operator is True


# ── relationship evidence ─────────────────────────────────────────────────────
def test_relationship_evidence_and_neighbors():
    g = _g()
    g.add_relationship(AssetType.HYPERVISOR, "vmware-host", RelationshipType.HOSTS,
                       AssetType.VM, "kali-vm",
                       source=ObservationSource.LAB_MANAGER, now_iso=T0)
    hv = asset_id(AssetType.HYPERVISOR, "vmware-host")
    nbrs = g.neighbors(hv)
    assert len(nbrs) == 1
    assert nbrs[0]["rel"] == "hosts"
    assert nbrs[0]["neighbor_id"] == asset_id(AssetType.VM, "kali-vm")


def test_neighbor_traversal_bounded():
    g = _g()
    # chain host -> vm -> service; depth cap keeps traversal bounded
    g.add_relationship(AssetType.HYPERVISOR, "hv", RelationshipType.HOSTS,
                       AssetType.VM, "vm1", source=ObservationSource.LAB_MANAGER, now_iso=T0)
    g.add_relationship(AssetType.VM, "vm1", RelationshipType.EXPOSES,
                       AssetType.SERVICE, "vm1:22", source=ObservationSource.SERVICE_OBSERVATION,
                       now_iso=T0)
    hv = asset_id(AssetType.HYPERVISOR, "hv")
    assert len(g.neighbors(hv, max_depth=1)) == 1
    assert len(g.neighbors(hv, max_depth=2)) == 2


# ── service exposure query ────────────────────────────────────────────────────
def test_exposed_services_query():
    g = _g()
    g.observe_service(AssetType.VM, "ubuntu-server", port=22, protocol="tcp",
                      service_name="ssh", exposure="authorized_subnet",
                      source=ObservationSource.SERVICE_OBSERVATION, now_iso=T0)
    g.observe_service(AssetType.VM, "ubuntu-server", port=5432, protocol="tcp",
                      service_name="postgres", exposure="localhost",
                      source=ObservationSource.SERVICE_OBSERVATION, now_iso=T0)
    reachable = g.exposed_services(only_reachable=True)
    ports = {s["port"] for s in reachable}
    assert 22 in ports          # authorized_subnet is reachable
    assert 5432 not in ports    # localhost-bound is excluded
    # all services (including localhost) available when not filtering
    assert {s["port"] for s in g.exposed_services(only_reachable=False)} == {22, 5432}


def test_unknown_exposure_is_not_assumed_safe():
    g = _g()
    g.observe_service(AssetType.VM, "target", port=80, exposure="unknown",
                      source=ObservationSource.CANONICAL_EVENT, now_iso=T0)
    # unknown exposure is kept in reachable set (unknown != safe)
    assert any(s["port"] == 80 for s in g.exposed_services(only_reachable=True))


# ── observation history ───────────────────────────────────────────────────────
def test_observation_history():
    g = _g()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.9, now_iso=T0)
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.21",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.8, now_iso=T1)
    hist = g.observation_history(AssetType.VM, "kali-vm", "ip")
    assert len(hist) == 2
    assert {h["value"] for h in hist} == {"192.168.56.20", "192.168.56.21"}


# ── snapshot + diff ───────────────────────────────────────────────────────────
def test_snapshot_and_diff():
    g = _g()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    before = g.snapshot(now_iso=T0)
    # add a new asset + change an attribute
    g.add_observation(AssetType.SERVER, "win-2022", "os", "Windows Server 2022",
                      source=ObservationSource.LAB_MANAGER, now_iso=T1)
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.99",
                      source=ObservationSource.OPERATOR_DECLARATION, confidence=0.99, now_iso=T2)
    after = g.snapshot(now_iso=T2)
    diff = AssetGraph.diff(before, after)
    assert asset_id(AssetType.SERVER, "win-2022") in diff.added_assets
    assert any(a == asset_id(AssetType.VM, "kali-vm") and attr == "ip"
               for a, attr, _o, _n in diff.changed_attributes)
    assert not diff.empty


def test_snapshot_hash_stable():
    g = _g()
    g.add_observation(AssetType.VM, "x", "ip", "1.1.1.1",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    s1 = g.snapshot(now_iso=T0)
    s2 = g.snapshot(now_iso=T2)   # different snapshot time, same state
    assert s1.content_hash == s2.content_hash


# ── no invented facts ─────────────────────────────────────────────────────────
def test_no_invented_facts():
    g = _g()
    assert g.get(AssetType.VM, "does-not-exist") is None
    assert g.by_type(AssetType.SERVER) == []
    assert g.exposed_services() == []
    assert g.neighbors("vm:ghost") == []
    assert g.compact_context()["asset_count"] == 0


# ── compact / scoped retrieval (never a full dump) ────────────────────────────
def test_compact_context_bounded():
    g = _g()
    for i in range(50):
        g.add_observation(AssetType.CONTAINER, f"c{i}", "image", "nginx",
                          source=ObservationSource.DOCKER_INSPECT, now_iso=T0)
    ctx = g.compact_context(max_assets=5)
    assert ctx["asset_count"] == 50
    assert ctx["shown"] == 5            # bounded — no full dump
    assert len(ctx["assets"]) == 5


def test_compact_context_scoped_to_ids():
    g = _g()
    g.add_observation(AssetType.VM, "a", "ip", "1.1.1.1",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    g.add_observation(AssetType.VM, "b", "ip", "2.2.2.2",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    ctx = g.compact_context([asset_id(AssetType.VM, "a")])
    assert list(ctx["assets"].keys()) == [asset_id(AssetType.VM, "a")]


# ── JSON persistence round-trip ───────────────────────────────────────────────
def test_json_roundtrip_preserves_evidence(tmp_path):
    g = _g()
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.20",
                      source=ObservationSource.OPERATOR_DECLARATION, now_iso=T0)
    g.add_observation(AssetType.VM, "kali-vm", "ip", "192.168.56.21",
                      source=ObservationSource.NETWORK_OBSERVATION, confidence=0.8, now_iso=T1)
    g.add_relationship(AssetType.HYPERVISOR, "hv", RelationshipType.HOSTS,
                       AssetType.VM, "kali-vm", source=ObservationSource.LAB_MANAGER, now_iso=T0)
    path = tmp_path / "graph.json"
    g.save(path)
    g2 = AssetGraph.load(path)
    a = g2.get(AssetType.VM, "kali-vm")
    assert len(a.history("ip")) == 2                 # conflict evidence survives
    assert a.conflict("ip") is not None
    hv = asset_id(AssetType.HYPERVISOR, "hv")
    assert len(g2.neighbors(hv)) == 1                # relationship survives
    # snapshot equality across round-trip
    assert g.snapshot(now_iso=T2).content_hash == g2.snapshot(now_iso=T2).content_hash


def test_load_missing_file_is_empty_graph(tmp_path):
    g = AssetGraph.load(tmp_path / "nope.json")
    assert g.assets == {}
