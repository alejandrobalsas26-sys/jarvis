"""tests/test_environment_discovery_v67.py — V67 M29 enrollment + asset discovery.

Proves:
  * enrollment is explicit + auditable and separate from authorization;
  * raw credentials are rejected, and no projection leaks the credentials ref;
  * discovery is FAIL-CLOSED on an un-authorized environment;
  * Docker/VMware/local/remote inventories fold into the EXISTING asset graph
    with provenance (DOCKER_INSPECT / LAB_MANAGER), preserving the V66 conflict
    model (a disagreeing observation is surfaced, not overwritten);
  * port exposure is derived from the observed bind, never assumed.

Pure — no docker/vmware/Ollama needed (the parse/transform layer is fixture-fed).
"""
from __future__ import annotations

import pytest

from core.asset_graph import AssetGraph, AssetType, ObservationSource, RelationshipType
from core.asset_discovery import (
    apply_discovery,
    discover_docker,
    discover_local_host,
    discover_vmware,
    exposure_for_bind,
    parse_docker_ports,
    parse_docker_ps,
    parse_vmrun_list,
)
from core.environment_registry import (
    EnrollmentError,
    EnvironmentHealth,
    EnvironmentRegistry,
    EnvironmentType,
)

T0 = "2026-07-08T12:00:00+00:00"
T1 = "2026-07-08T12:05:00+00:00"


# ══════════════════════════════════════════════════════════════════════════════
#  Environment registry
# ══════════════════════════════════════════════════════════════════════════════
class TestEnrollment:
    def test_enroll_then_authorize_is_two_steps(self):
        reg = EnvironmentRegistry()
        e = reg.enroll("docker-local", EnvironmentType.DOCKER, "Local Docker")
        assert e.authorized is False
        assert reg.is_authorized("docker-local") is False
        reg.authorize("docker-local", scope="LAB-A")
        assert reg.is_authorized("docker-local") is True
        assert reg.get("docker-local").authorization_scope == "LAB-A"

    def test_enrollment_is_audited(self):
        reg = EnvironmentRegistry()
        reg.enroll("vm-lab", EnvironmentType.VMWARE, "Lab VMs")
        reg.authorize("vm-lab")
        reg.revoke("vm-lab")
        actions = [a["action"] for a in reg.audit_trail()]
        assert actions == ["enroll", "authorize", "revoke"]

    def test_raw_credentials_rejected(self):
        reg = EnvironmentRegistry()
        with pytest.raises(EnrollmentError):
            reg.enroll("h", EnvironmentType.REMOTE_LINUX, "Host",
                       credentials_ref="-----BEGIN OPENSSH PRIVATE KEY-----\nabc")
        with pytest.raises(EnrollmentError):
            reg.enroll("h2", EnvironmentType.REMOTE_LINUX, "Host",
                       credentials_ref="A" * 64)  # long high-entropy blob

    def test_credentials_reference_allowed_and_never_leaked(self):
        reg = EnvironmentRegistry()
        e = reg.enroll("h", EnvironmentType.REMOTE_LINUX, "Host",
                       credentials_ref="env:LAB_SSH_KEY_PATH")
        assert e.has_credentials is True
        pub = e.to_public_dict()
        assert "credentials_ref" not in pub
        assert pub["has_credentials"] is True
        panel = reg.to_aura_panel()
        assert all("credentials_ref" not in row for row in panel["environments"])

    def test_persistence_roundtrip(self, tmp_path):
        reg = EnvironmentRegistry()
        reg.enroll("docker-local", EnvironmentType.DOCKER, "Local Docker",
                   endpoint="npipe:////./pipe/docker_engine", authorized=True)
        reg.update_health("docker-local", EnvironmentHealth.REACHABLE, now_iso=T0)
        p = tmp_path / "envs.json"
        reg.save(p)
        reloaded = EnvironmentRegistry.load(p)
        got = reloaded.get("docker-local")
        assert got is not None
        assert got.authorized is True
        assert got.health is EnvironmentHealth.REACHABLE

    def test_unknown_env_raises(self):
        reg = EnvironmentRegistry()
        with pytest.raises(EnrollmentError):
            reg.authorize("nope")


# ══════════════════════════════════════════════════════════════════════════════
#  Parsers + exposure
# ══════════════════════════════════════════════════════════════════════════════
class TestParsers:
    def test_docker_ports(self):
        ports = parse_docker_ports("0.0.0.0:8080->80/tcp, 127.0.0.1:5432->5432/tcp, 9000/tcp")
        # only published (->) mappings expose a service
        assert {"public": 8080, "ip": "0.0.0.0", "type": "tcp"} in ports
        assert {"public": 5432, "ip": "127.0.0.1", "type": "tcp"} in ports
        assert len(ports) == 2

    def test_docker_ps_json_lines(self):
        out = (
            '{"ID":"abc123","Names":"web","Image":"nginx:latest","State":"running","Ports":"0.0.0.0:8080->80/tcp"}\n'
            'garbage line\n'
            '{"ID":"def456","Names":"db","Image":"postgres:16","State":"running","Ports":"127.0.0.1:5432->5432/tcp"}\n'
        )
        cs = parse_docker_ps(out)
        assert [c["name"] for c in cs] == ["web", "db"]
        assert cs[0]["image"] == "nginx:latest"
        assert cs[0]["ports"][0]["public"] == 8080

    def test_vmrun_list(self):
        out = "Total running VMs: 2\nC:\\VMs\\kali\\kali.vmx\nC:\\VMs\\win\\win.vmx\n"
        assert parse_vmrun_list(out) == ["C:\\VMs\\kali\\kali.vmx", "C:\\VMs\\win\\win.vmx"]

    @pytest.mark.parametrize("ip,expected", [
        ("0.0.0.0", "external"), ("::", "external"),
        ("127.0.0.1", "localhost"), ("192.168.1.10", "internal"),
        ("10.0.0.5", "internal"), ("8.8.8.8", "external"),
        ("", "unknown"), ("not-an-ip", "unknown"),
    ])
    def test_exposure(self, ip, expected):
        assert exposure_for_bind(ip) == expected


# ══════════════════════════════════════════════════════════════════════════════
#  Discovery transforms → asset graph (provenance + services)
# ══════════════════════════════════════════════════════════════════════════════
class TestDockerDiscovery:
    def test_containers_and_services_written(self):
        g = AssetGraph()
        containers = parse_docker_ps(
            '{"ID":"abc","Names":"web","Image":"nginx:latest","State":"running","Ports":"0.0.0.0:8080->80/tcp"}\n'
            '{"ID":"def","Names":"db","Image":"postgres:16","State":"running","Ports":"127.0.0.1:5432->5432/tcp"}\n'
        )
        res = discover_docker(g, containers, host_identity="ryzen-host", env_id="docker-local", now_iso=T0)
        assert res.services == 2
        web = g.get(AssetType.CONTAINER, "web")
        assert web is not None
        assert web.current("image")[0] == "nginx:latest"
        # container RUNS_ON engine, engine RUNS_ON host
        assert any(r.rel_type is RelationshipType.RUNS_ON for r in g.relationships.values())
        # exposure was derived from the bind: 8080 external, 5432 localhost
        svcs = {s["port"]: s["exposure"] for s in g.exposed_services(only_reachable=False)}
        assert svcs.get(8080) == "external"
        assert svcs.get(5432) == "localhost"

    def test_provenance_is_docker_inspect(self):
        g = AssetGraph()
        discover_docker(g, [{"id": "x", "name": "web", "image": "nginx", "state": "running",
                             "ports": []}], host_identity="h", env_id="docker-local", now_iso=T0)
        web = g.get(AssetType.CONTAINER, "web")
        sources = {o.source for obss in web.attributes.values() for o in obss}
        assert ObservationSource.DOCKER_INSPECT in sources


class TestVmwareDiscovery:
    def test_vms_and_networks(self):
        g = AssetGraph()
        vms = [{"name": "kali", "vmx_path": "C:\\VMs\\kali.vmx", "state": "running",
                "guest_os": "kali-linux", "networks": [{"adapter": "eth0", "connected_to": "lab-net"}]}]
        res = discover_vmware(g, vms, env_id="vmware", now_iso=T0)
        assert res.relationships >= 2  # vm RUNS_ON hypervisor + vm CONNECTED_TO net
        kali = g.get(AssetType.VM, "kali")
        assert kali.current("state")[0] == "running"
        assert g.get(AssetType.NETWORK, "lab-net") is not None


class TestConflictPreservation:
    def test_disagreeing_observation_surfaces_conflict_not_overwrite(self):
        g = AssetGraph()
        # first discovery: container image nginx:1.24
        discover_docker(g, [{"id": "x", "name": "web", "image": "nginx:1.24",
                             "state": "running", "ports": []}],
                        host_identity="h", env_id="docker-local", now_iso=T0)
        # later discovery disagrees: nginx:1.25 — must be preserved, not silently replaced
        discover_docker(g, [{"id": "x", "name": "web", "image": "nginx:1.25",
                             "state": "running", "ports": []}],
                        host_identity="h", env_id="docker-local", now_iso=T1)
        web = g.get(AssetType.CONTAINER, "web")
        image_values = {o.value for o in web.attributes.get("image", [])}
        assert image_values == {"nginx:1.24", "nginx:1.25"}  # both retained


class TestLocalHostDiscovery:
    def test_local_host_written_with_provenance(self):
        g = AssetGraph()
        res = discover_local_host(g, hostname="ryzen-host", os_name="Windows",
                                  os_version="11", env_id="local", now_iso=T0)
        assert res.observations >= 1
        host = g.get(AssetType.PHYSICAL_HOST, "ryzen-host")
        assert host is not None
        assert host.current("os")[0] == "Windows"
        sources = {o.source for obss in host.attributes.values() for o in obss}
        assert ObservationSource.OPERATOR_DECLARATION in sources

    def test_unknown_os_stays_unknown(self):
        # No os_name/os_version observed → the graph carries no os attribute (not a guess).
        g = AssetGraph()
        discover_local_host(g, hostname="bare-host", env_id="local", now_iso=T0)
        host = g.get(AssetType.PHYSICAL_HOST, "bare-host")
        assert host is not None
        assert host.current("os") is None or host.attributes.get("os", []) == []


class TestRemoteLinuxDiscovery:
    def test_remote_linux_requires_authorization(self):
        # Fail-closed for the REMOTE_LINUX path specifically (no uncontrolled scanning).
        g = AssetGraph()
        reg = EnvironmentRegistry()
        entry = reg.enroll("edge-01", EnvironmentType.REMOTE_LINUX, "Edge host",
                           endpoint="edge-01.lab", credentials_ref="env:LAB_SSH_KEY_PATH")
        res = apply_discovery(entry, g, {"host_identity": "edge-01",
                              "services": [{"port": 22, "name": "ssh"}]}, now_iso=T0)
        assert res.authorized is False
        assert len(g.assets) == 0

    def test_authorized_remote_linux_writes_services_and_coverage(self):
        g = AssetGraph()
        reg = EnvironmentRegistry()
        entry = reg.enroll("edge-01", EnvironmentType.REMOTE_LINUX, "Edge host",
                           endpoint="edge-01", authorized=True,
                           credentials_ref="env:LAB_SSH_KEY_PATH")
        res = apply_discovery(entry, g, {
            "host_identity": "edge-01", "guest_os": "ubuntu-22.04",
            "sensor_coverage": "partial",
            "services": [{"port": 22, "name": "ssh", "exposure": "internal"}],
        }, now_iso=T0)
        assert res.authorized is True
        assert res.services == 1
        server = g.get(AssetType.SERVER, "edge-01")
        assert server is not None
        assert server.current("sensor_coverage")[0] == "partial"


# ══════════════════════════════════════════════════════════════════════════════
#  Gated orchestrator (fail-closed)
# ══════════════════════════════════════════════════════════════════════════════
class TestGating:
    def test_unauthorized_environment_writes_nothing(self):
        g = AssetGraph()
        reg = EnvironmentRegistry()
        entry = reg.enroll("docker-local", EnvironmentType.DOCKER, "Local Docker")
        # NOT authorized
        res = apply_discovery(entry, g, {"containers": [{"id": "x", "name": "web",
                              "image": "nginx", "state": "running", "ports": []}]}, now_iso=T0)
        assert res.authorized is False
        assert res.error and "not authorized" in res.error
        assert len(g.assets) == 0  # nothing written

    def test_authorized_docker_discovery_writes(self):
        g = AssetGraph()
        reg = EnvironmentRegistry()
        entry = reg.enroll("docker-local", EnvironmentType.DOCKER, "Local Docker",
                           endpoint="ryzen-host", authorized=True)
        res = apply_discovery(entry, g, {
            "host_identity": "ryzen-host",
            "containers": [{"id": "x", "name": "web", "image": "nginx", "state": "running",
                            "ports": [{"public": 8080, "ip": "0.0.0.0", "type": "tcp"}]}],
        }, now_iso=T0)
        assert res.authorized is True
        assert res.services == 1
        assert g.get(AssetType.CONTAINER, "web") is not None
