"""core/asset_discovery.py — V67 M29: authorized environment discovery.

Turns an *already-fetched* inventory of an enrolled, authorized environment into
evidence-backed writes on the existing V66 asset & service graph
(:data:`core.asset_graph.graph`). It does NOT build a parallel store, and it does
NOT scan the network — discovery order is: local API / inventory → config →
hypervisor/container inventory → passive observation. Only enrolled + authorized
environments (:mod:`core.environment_registry`) are ever touched (fail-closed).

Every write carries provenance (``ObservationSource.DOCKER_INSPECT`` /
``LAB_MANAGER`` / ``OPERATOR_DECLARATION``) and a confidence, so the graph's
conflict model applies automatically — a discovered fact never *silently*
overwrites an existing one; a disagreement surfaces as an ``AssetConflict``.
Unknown stays unknown: absent inventory fields are not guessed.

The parse/transform layer is pure and unit-testable with fixtures; the async
``probe_*`` helpers (subprocess, shell=False, graceful degradation) are the thin,
best-effort bridge to real tooling and return ``None`` when the tool is absent.
"""
from __future__ import annotations

import ipaddress
import json
import shutil
from dataclasses import dataclass, field

from loguru import logger

from core.asset_graph import AssetType, ObservationSource, RelationshipType


# ══════════════════════════════════════════════════════════════════════════════
#  Result
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class DiscoveryResult:
    env_id: str
    authorized: bool = True
    observations: int = 0
    relationships: int = 0
    services: int = 0
    assets_touched: set[str] = field(default_factory=set)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def touch(self, aid: str) -> None:
        self.assets_touched.add(aid)

    def to_dict(self) -> dict:
        return {
            "env_id": self.env_id,
            "authorized": self.authorized,
            "observations": self.observations,
            "relationships": self.relationships,
            "services": self.services,
            "assets": len(self.assets_touched),
            "error": self.error,
            "notes": self.notes[:12],
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Exposure classification (observed, never assumed)
# ══════════════════════════════════════════════════════════════════════════════
def exposure_for_bind(ip: str | None) -> str:
    """Classify a service bind address into the graph's exposure vocabulary.

    localhost / internal / external / unknown — from the OBSERVED bind, not a
    guess. An empty/unknown bind stays ``unknown``.
    """
    v = (ip or "").strip()
    if not v:
        return "unknown"
    if v in ("0.0.0.0", "::", "*"):
        return "external"       # bound to all interfaces → externally reachable
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:
        return "unknown"
    if addr.is_loopback:
        return "localhost"
    if addr.is_private:
        return "internal"
    return "external"


# ══════════════════════════════════════════════════════════════════════════════
#  Pure parsers (fixture-testable)
# ══════════════════════════════════════════════════════════════════════════════
def parse_docker_ps(output: str) -> list[dict]:
    """Parse ``docker ps --format '{{json .}}'`` (one JSON object per line) into a
    normalized container list. Tolerant: skips unparseable lines."""
    containers: list[dict] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        containers.append({
            "id": str(row.get("ID", ""))[:16],
            "name": str(row.get("Names", "")).split(",")[0].strip(),
            "image": str(row.get("Image", "")),
            "state": str(row.get("State", "") or row.get("Status", "")),
            "ports": parse_docker_ports(str(row.get("Ports", ""))),
        })
    return containers


def parse_docker_ports(ports_field: str) -> list[dict]:
    """Parse Docker's ``Ports`` string, e.g.
    ``0.0.0.0:8080->80/tcp, 127.0.0.1:5432->5432/tcp`` → published ports."""
    out: list[dict] = []
    for part in (ports_field or "").split(","):
        part = part.strip()
        if "->" not in part:
            continue                       # only published mappings expose a service
        host, _, container = part.partition("->")
        ip, _, hport = host.rpartition(":")
        proto = container.rpartition("/")[2] if "/" in container else "tcp"
        try:
            public = int(hport)
        except ValueError:
            continue
        out.append({"public": public, "ip": ip, "type": proto or "tcp"})
    return out


def parse_vmrun_list(output: str) -> list[str]:
    """Parse ``vmrun list`` output → list of running .vmx paths."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    # First line is "Total running VMs: N"; the rest are paths.
    return [ln for ln in lines if ln.lower().endswith(".vmx")]


# ══════════════════════════════════════════════════════════════════════════════
#  Transforms (write to the shared asset graph with provenance)
# ══════════════════════════════════════════════════════════════════════════════
def discover_local_host(
    graph, *, hostname: str, os_name: str = "", os_version: str = "",
    env_id: str = "local", source: ObservationSource = ObservationSource.OPERATOR_DECLARATION,
    now_iso: str | None = None,
) -> DiscoveryResult:
    """Record the local host as an evidence-backed PHYSICAL_HOST asset."""
    res = DiscoveryResult(env_id=env_id)

    def obs(attr, val):
        _obs(graph, res, AssetType.PHYSICAL_HOST, hostname, attr, val,
             source=source, env_id=env_id, now_iso=now_iso)

    obs("hostname", hostname)
    if os_name:
        obs("os", os_name)
    if os_version:
        obs("os_version", os_version)
    res.touch(f"physical_host:{hostname.lower()}")
    return res


def discover_docker(
    graph, containers: list[dict], *, host_identity: str,
    engine_identity: str = "docker-engine", env_id: str = "docker",
    now_iso: str | None = None,
) -> DiscoveryResult:
    """Fold a Docker container inventory into the graph.

    container --RUNS_ON--> docker engine --RUNS_ON--> local host; each published
    port becomes an observed SERVICE the container EXPOSES, with exposure derived
    from the OBSERVED bind address.
    """
    res = DiscoveryResult(env_id=env_id)
    src = ObservationSource.DOCKER_INSPECT

    # engine RUNS_ON host
    _rel(graph, res, AssetType.APPLICATION, engine_identity, RelationshipType.RUNS_ON,
         AssetType.PHYSICAL_HOST, host_identity, source=src, env_id=env_id, now_iso=now_iso)
    _obs(graph, res, AssetType.APPLICATION, engine_identity, "role", "container_engine",
         source=src, env_id=env_id, now_iso=now_iso)

    for c in containers or []:
        name = (c.get("name") or c.get("id") or "").strip()
        if not name:
            continue
        _obs(graph, res, AssetType.CONTAINER, name, "container_id", c.get("id", ""),
             source=src, env_id=env_id, now_iso=now_iso)
        if c.get("image"):
            _obs(graph, res, AssetType.CONTAINER, name, "image", c["image"],
                 source=src, env_id=env_id, now_iso=now_iso)
        if c.get("state"):
            _obs(graph, res, AssetType.CONTAINER, name, "state", c["state"],
                 source=src, env_id=env_id, now_iso=now_iso)
        _rel(graph, res, AssetType.CONTAINER, name, RelationshipType.RUNS_ON,
             AssetType.APPLICATION, engine_identity, source=src, env_id=env_id, now_iso=now_iso)
        res.touch(f"container:{name.lower()}")
        for p in c.get("ports", []) or []:
            port = p.get("public")
            if not port:
                continue
            graph.observe_service(
                AssetType.CONTAINER, name, port=int(port),
                protocol=str(p.get("type", "tcp")), exposure=exposure_for_bind(p.get("ip")),
                bind_addr=str(p.get("ip", "")), source=src,
                observer=env_id, event_refs=(f"discovery:{env_id}",), now_iso=now_iso,
            )
            res.services += 1
    return res


def discover_vmware(
    graph, vms: list[dict], *, hypervisor_identity: str = "vmware-workstation",
    env_id: str = "vmware", now_iso: str | None = None,
) -> DiscoveryResult:
    """Fold a VMware VM inventory into the graph.

    vm --RUNS_ON--> hypervisor; observed state/guest_os/vmx_path; each network
    adapter --CONNECTED_TO--> its virtual network.
    """
    res = DiscoveryResult(env_id=env_id)
    src = ObservationSource.LAB_MANAGER
    _obs(graph, res, AssetType.HYPERVISOR, hypervisor_identity, "role", "hypervisor",
         source=src, env_id=env_id, now_iso=now_iso)

    for vm in vms or []:
        name = (vm.get("name") or vm.get("vmx_path") or "").strip()
        if not name:
            continue
        _rel(graph, res, AssetType.VM, name, RelationshipType.RUNS_ON,
             AssetType.HYPERVISOR, hypervisor_identity, source=src, env_id=env_id, now_iso=now_iso)
        for attr in ("state", "guest_os", "vmx_path"):
            if vm.get(attr):
                _obs(graph, res, AssetType.VM, name, attr, vm[attr],
                     source=src, env_id=env_id, now_iso=now_iso)
        res.touch(f"vm:{name.lower()}")
        for net in vm.get("networks", []) or []:
            connected = (net.get("connected_to") or net.get("type") or "").strip()
            if not connected:
                continue
            _rel(graph, res, AssetType.VM, name, RelationshipType.CONNECTED_TO,
                 AssetType.NETWORK, connected, source=src, env_id=env_id, now_iso=now_iso)
    return res


def discover_remote_linux(
    graph, host_identity: str, *, services: list[dict] | None = None,
    guest_os: str = "", sensor_coverage: str = "", env_id: str = "remote",
    now_iso: str | None = None,
) -> DiscoveryResult:
    """Fold an authorized remote-Linux observation into the graph (via existing
    sensor/SSH capability output — not a scanner)."""
    res = DiscoveryResult(env_id=env_id)
    src = ObservationSource.SENSOR_MESH
    if guest_os:
        _obs(graph, res, AssetType.SERVER, host_identity, "os", guest_os,
             source=src, env_id=env_id, now_iso=now_iso)
    if sensor_coverage:
        _obs(graph, res, AssetType.SERVER, host_identity, "sensor_coverage", sensor_coverage,
             source=src, env_id=env_id, now_iso=now_iso)
    res.touch(f"server:{host_identity.lower()}")
    for svc in services or []:
        port = svc.get("port")
        if not port:
            continue
        graph.observe_service(
            AssetType.SERVER, host_identity, port=int(port),
            protocol=str(svc.get("protocol", "tcp")), service_name=str(svc.get("name", "")),
            exposure=str(svc.get("exposure", "internal")), source=ObservationSource.SERVICE_OBSERVATION,
            observer=env_id, event_refs=(f"discovery:{env_id}",), now_iso=now_iso,
        )
        res.services += 1
    return res


# ── shared write helpers (count + provenance) ─────────────────────────────────
def _obs(graph, res, atype, identity, attr, val, *, source, env_id, now_iso):
    graph.add_observation(atype, identity, attr, val, source=source, observer=env_id,
                          event_refs=(f"discovery:{env_id}",), now_iso=now_iso)
    res.observations += 1
    res.touch(f"{atype.value}:{str(identity).lower()}")


def _rel(graph, res, stype, sident, rel, dtype, dident, *, source, env_id, now_iso):
    graph.add_relationship(stype, sident, rel, dtype, dident, source=source,
                           observer=env_id, event_refs=(f"discovery:{env_id}",), now_iso=now_iso)
    res.relationships += 1


# ══════════════════════════════════════════════════════════════════════════════
#  Gated orchestrator
# ══════════════════════════════════════════════════════════════════════════════
def apply_discovery(entry, graph, inventory: dict, *, now_iso: str | None = None) -> DiscoveryResult:
    """Apply a fetched *inventory* for an enrolled *entry* to *graph* — FAIL-CLOSED.

    Refuses an un-authorized environment (returns an error result, writes nothing).
    Dispatches by the entry's ``env_type``. ``inventory`` is the already-fetched,
    normalized payload (so this stays pure/testable); the ``probe_*`` helpers
    produce it in production.
    """
    from core.environment_registry import EnvironmentType

    if not getattr(entry, "authorized", False):
        return DiscoveryResult(env_id=getattr(entry, "env_id", "?"), authorized=False,
                               error="environment not authorized for discovery")
    et = entry.env_type
    host_identity = entry.endpoint or entry.display_name or entry.env_id
    if et is EnvironmentType.DOCKER:
        return discover_docker(graph, inventory.get("containers", []),
                               host_identity=inventory.get("host_identity", "local-host"),
                               env_id=entry.env_id, now_iso=now_iso)
    if et is EnvironmentType.VMWARE:
        return discover_vmware(graph, inventory.get("vms", []),
                               hypervisor_identity=inventory.get("hypervisor", "vmware-workstation"),
                               env_id=entry.env_id, now_iso=now_iso)
    if et is EnvironmentType.LOCAL_WINDOWS:
        return discover_local_host(graph, hostname=inventory.get("hostname", host_identity),
                                   os_name=inventory.get("os_name", ""),
                                   os_version=inventory.get("os_version", ""),
                                   env_id=entry.env_id,
                                   source=ObservationSource.TRUSTED_CONFIG, now_iso=now_iso)
    if et in (EnvironmentType.REMOTE_LINUX, EnvironmentType.LAB_NODE):
        return discover_remote_linux(graph, inventory.get("host_identity", host_identity),
                                     services=inventory.get("services", []),
                                     guest_os=inventory.get("guest_os", ""),
                                     sensor_coverage=inventory.get("sensor_coverage", ""),
                                     env_id=entry.env_id, now_iso=now_iso)
    return DiscoveryResult(env_id=entry.env_id, error=f"no discovery for {et.value}")


# ══════════════════════════════════════════════════════════════════════════════
#  Best-effort async probes (subprocess, shell=False, graceful degradation)
# ══════════════════════════════════════════════════════════════════════════════
async def probe_local_host() -> dict:
    """Local host inventory from stdlib (no subprocess)."""
    import platform
    import socket
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "localhost"
    return {
        "hostname": hostname,
        "os_name": platform.system(),
        "os_version": platform.version(),
    }


async def probe_docker_inventory() -> dict | None:
    """Fetch Docker inventory via ``docker ps`` (shell=False). None if docker absent."""
    if not shutil.which("docker"):
        return None
    import asyncio
    import subprocess

    def _run() -> str | None:
        try:
            proc = subprocess.run(
                ["docker", "ps", "--no-trunc", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=15,
            )
            return proc.stdout if proc.returncode == 0 else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"ASSET_DISCOVERY: docker probe failed: {e}")
            return None

    out = await asyncio.to_thread(_run)
    if out is None:
        return None
    return {"containers": parse_docker_ps(out), "host_identity": (await probe_local_host())["hostname"]}


async def probe_vmware_inventory(vmrun_path: str) -> dict | None:
    """Fetch running VMs via ``vmrun list`` (shell=False). None if vmrun absent."""
    from pathlib import Path
    if not vmrun_path or not Path(vmrun_path).exists():
        return None
    import asyncio
    import subprocess

    def _run() -> str | None:
        try:
            proc = subprocess.run([vmrun_path, "list"], capture_output=True,
                                  text=True, timeout=15)
            return proc.stdout if proc.returncode == 0 else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"ASSET_DISCOVERY: vmrun probe failed: {e}")
            return None

    out = await asyncio.to_thread(_run)
    if out is None:
        return None
    vms = [{"name": Path(p).stem, "vmx_path": p, "state": "running"}
           for p in parse_vmrun_list(out)]
    return {"vms": vms, "hypervisor": "vmware-workstation"}
