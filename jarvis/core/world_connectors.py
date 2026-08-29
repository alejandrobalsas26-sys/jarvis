"""
core/world_connectors.py — V69 M63: one read-only connector protocol.

WHAT A CONNECTOR IS
-------------------
A bounded, READ-ONLY adapter that turns one authorized environment into
:class:`core.world_state.WorldObservation` records. It has an identity, an
availability probe, a hard timeout, a size ceiling, and exactly one honest way
to fail. It never causes an effect, never widens authority and never invents a
reading.

WHAT A CONNECTOR MAY NOT DO
---------------------------
  * discover its own targets. Every target comes from
    :class:`core.environment_registry.EnvironmentRegistry`, which the OPERATOR
    enrolls and authorizes. There is no subnet sweep, no ARP scan, no port
    scan and no "try the neighbours" path anywhere in this module — an
    unauthorized environment is skipped, not probed.
  * change ``AuthorityMode``, ``ScopePolicy`` or tool permissions. Connectors
    are readers; :mod:`tools.executor` remains the only path to an effect.
  * report success it did not have. A missing external service is
    ``UNAVAILABLE`` and a broken one is ``DEGRADED``; neither is ever an empty
    ``AVAILABLE`` result, because "I found nothing" and "I could not look" are
    different facts and the operator needs to tell them apart.
  * disable TLS verification. There is no flag for it here, deliberately.

REUSE
-----
Host/Docker/VMware collection reuses the existing
:mod:`core.asset_discovery` probes (``shell=False``, bounded, graceful) rather
than opening new subprocess paths. HTTP connectors route through
:mod:`core.url_policy`, which is the repository's mandatory SSRF guard.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from loguru import logger

from core.asset_graph import AssetType
from core.world_bounds import DEFAULT_BOUNDS, WorldBounds
from core.world_state import ObservationTrust, WorldObservation, WorldStateError

SCHEMA_VERSION = "world-connector-1"


class ConnectorState(str, Enum):
    AVAILABLE = "available"          # probed, reachable, usable
    UNAVAILABLE = "unavailable"      # not present / not reachable — often normal
    DEGRADED = "degraded"            # partially answered, or answered slowly
    MISCONFIGURED = "misconfigured"  # present but the config cannot be honoured
    DISABLED = "disabled"            # the operator turned it off


#: States that mean "this is fine, there is just nothing here". An optional
#: connector in one of these is NOT a failure of the core.
OPTIONAL_OK = (ConnectorState.UNAVAILABLE, ConnectorState.DISABLED)

#: Substrings that make a key look like it carries a secret. A value under one
#: of these never reaches an observation payload, a log line or the HUD.
_SECRET_KEY_RE = re.compile(
    r"(pass|passwd|password|secret|token|api[_-]?key|apikey|authorization|"
    r"auth|credential|private[_-]?key|session|cookie|bearer)", re.I)

#: The leading ``\b`` is applied PER ALTERNATIVE, not to the whole group: a PEM
#: header starts with ``-``, so a word boundary in front of it can never match
#: at the start of a string, and the pattern would silently never fire.
_SECRET_VALUE_RE = re.compile(
    r"(?i)(\bbearer\s+[A-Za-z0-9._\-]{8,}"
    r"|\b[A-Za-z0-9_\-]{32,}\b"
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----)")

REDACTED = "[REDACTED]"


def redact(value):
    """Strip anything that looks like a credential, at any depth. Bounded."""
    if isinstance(value, dict):
        return {k: (REDACTED if _SECRET_KEY_RE.search(str(k)) else redact(v))
                for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in list(value)[:64]]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(REDACTED, value)[:1024]
    return value


@dataclass
class ConnectorResult:
    """What one collection attempt actually produced. Never a claim it didn't."""
    connector_id: str
    state: ConnectorState
    observations: list = field(default_factory=list)
    error: str = ""
    notes: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.state is ConnectorState.AVAILABLE

    @property
    def optional_missing(self) -> bool:
        return self.state in OPTIONAL_OK

    def to_dict(self) -> dict:
        return {
            "connector_id": self.connector_id, "state": self.state.value,
            "observations": len(self.observations),
            "error": redact(self.error)[:512], "notes": [redact(n) for n in self.notes[:8]],
            "duration_s": round(self.duration_s, 3), "truncated": self.truncated,
        }


@runtime_checkable
class WorldConnector(Protocol):
    """The one connector interface. Read-only is a property, not a promise."""

    connector_id: str
    connector_type: str
    is_read_only: bool

    async def probe(self) -> ConnectorState: ...

    async def collect(self) -> ConnectorResult: ...


class BaseConnector:
    """Shared machinery: identity, bounds, timeout, and honest failure.

    Subclasses implement :meth:`_probe` and :meth:`_collect` and never worry
    about timeouts, exception isolation or redaction — those are enforced here
    so one careless connector cannot become the exception to the rule.
    """

    connector_type = "base"
    is_read_only = True

    def __init__(self, connector_id: str, *, bounds: WorldBounds | None = None,
                 enabled: bool = True) -> None:
        self.connector_id = connector_id
        self.bounds = bounds or DEFAULT_BOUNDS
        self.enabled = enabled

    # ── subclass surface ─────────────────────────────────────────────────────
    async def _probe(self) -> ConnectorState:
        raise NotImplementedError

    async def _collect(self) -> ConnectorResult:
        raise NotImplementedError

    # ── enforced surface ─────────────────────────────────────────────────────
    async def probe(self) -> ConnectorState:
        if not self.enabled:
            return ConnectorState.DISABLED
        try:
            return await asyncio.wait_for(self._probe(),
                                          timeout=self.bounds.connector_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(f"CONNECTOR {self.connector_id}: probe timed out")
            return ConnectorState.DEGRADED
        except Exception as exc:  # noqa: BLE001 — a probe never raises outward
            logger.warning(f"CONNECTOR {self.connector_id}: probe failed: {exc}")
            return ConnectorState.UNAVAILABLE

    async def collect(self) -> ConnectorResult:
        if not self.enabled:
            return ConnectorResult(self.connector_id, ConnectorState.DISABLED,
                                   notes=["disabled by configuration"])
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(self._collect(),
                                            timeout=self.bounds.connector_timeout_s)
        except asyncio.TimeoutError:
            return ConnectorResult(
                self.connector_id, ConnectorState.DEGRADED,
                error=f"timed out after {self.bounds.connector_timeout_s}s",
                duration_s=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            return ConnectorResult(
                self.connector_id, ConnectorState.DEGRADED,
                error=redact(f"{type(exc).__name__}: {exc}"),
                duration_s=time.monotonic() - started)
        result.duration_s = time.monotonic() - started
        if len(result.observations) > self.bounds.max_connector_items:
            del result.observations[self.bounds.max_connector_items:]
            result.truncated = True
            result.notes.append(
                f"truncated to {self.bounds.max_connector_items} items")
            if result.state is ConnectorState.AVAILABLE:
                result.state = ConnectorState.DEGRADED
        return result

    # ── helper for subclasses ────────────────────────────────────────────────
    def _observe(self, *, entity_type: AssetType, identity: str, event_type: str,
                 payload: dict, trust: ObservationTrust,
                 severity: str = "INFO", confidence: float = 0.6):
        """Build one observation with the payload redacted. Returns None (and
        logs) rather than raising, so one bad row never aborts a collection."""
        try:
            return WorldObservation.build(
                source_id=self.connector_id, source_type=self.connector_type,
                entity_type=entity_type, identity=identity, event_type=event_type,
                payload=redact(payload), trust=trust, severity=severity,
                confidence=confidence, provenance=f"connector/{self.connector_id}",
                bounds=self.bounds)
        except WorldStateError as exc:
            logger.warning(f"CONNECTOR {self.connector_id}: refusing row: {exc}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
#  Local host — always present, always read-only
# ══════════════════════════════════════════════════════════════════════════════
class LocalHostConnector(BaseConnector):
    """The machine JARVIS runs on. Uses stdlib + psutil (a base dependency)."""

    connector_type = "local_host"

    async def _probe(self) -> ConnectorState:
        return ConnectorState.AVAILABLE

    async def _collect(self) -> ConnectorResult:
        from core.asset_discovery import probe_local_host
        info = await probe_local_host()
        hostname = info.get("hostname") or "localhost"
        payload = {
            "hostname": hostname,
            "os": info.get("os_name", ""),
            "os_version": info.get("os_version", ""),
            "status": "running",
        }
        notes: list[str] = []
        try:
            import psutil
            payload["cpu_percent"] = psutil.cpu_percent(interval=None)
            payload["ram_percent"] = psutil.virtual_memory().percent
            payload["disk_percent"] = psutil.disk_usage("/").percent
            payload["health"] = (
                "critical" if payload["ram_percent"] >= 95 or payload["disk_percent"] >= 95
                else "warning" if payload["ram_percent"] >= 85 or payload["disk_percent"] >= 90
                else "healthy")
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
            notes.append(f"psutil telemetry unavailable: {type(exc).__name__}")

        obs = self._observe(entity_type=AssetType.PHYSICAL_HOST, identity=hostname,
                            event_type="host_inventory", payload=payload,
                            trust=ObservationTrust.INSTRUMENTED, confidence=0.9)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=[o for o in (obs,) if o], notes=notes)


# ══════════════════════════════════════════════════════════════════════════════
#  Docker — optional
# ══════════════════════════════════════════════════════════════════════════════
class DockerConnector(BaseConnector):
    """Container inventory via the existing ``docker ps`` probe (shell=False)."""

    connector_type = "docker"

    async def _probe(self) -> ConnectorState:
        return (ConnectorState.AVAILABLE if shutil.which("docker")
                else ConnectorState.UNAVAILABLE)

    async def _collect(self) -> ConnectorResult:
        if not shutil.which("docker"):
            return ConnectorResult(self.connector_id, ConnectorState.UNAVAILABLE,
                                   notes=["docker CLI not on PATH"])
        from core.asset_discovery import probe_docker_inventory
        inventory = await probe_docker_inventory()
        if inventory is None:
            return ConnectorResult(
                self.connector_id, ConnectorState.DEGRADED,
                error="docker is installed but the inventory probe returned nothing "
                      "(daemon down, or permission denied)")
        host = inventory.get("host_identity") or "localhost"
        observations = []
        for container in inventory.get("containers", []):
            name = (container.get("name") or container.get("Names") or "").strip()
            if not name:
                continue
            payload = {
                "status": _docker_status(container.get("status", "")),
                "image": container.get("image", ""),
                "container_id": str(container.get("id", ""))[:12],
                "host": host,
                "raw_status": container.get("status", ""),
            }
            obs = self._observe(entity_type=AssetType.CONTAINER, identity=name,
                                event_type="container_inventory", payload=payload,
                                trust=ObservationTrust.INSTRUMENTED, confidence=0.85)
            if obs:
                observations.append(obs)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=observations,
                               notes=[f"{len(observations)} containers"])


def _docker_status(raw: str) -> str:
    """Map Docker's prose status onto the state vocabulary. Unknown stays unknown."""
    low = (raw or "").strip().lower()
    if low.startswith("up"):
        return "degraded" if "unhealthy" in low else "running"
    if low.startswith(("exited", "dead", "removing")):
        return "stopped"
    if low.startswith(("created", "restarting", "paused")):
        return "degraded"
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP-API connectors — Proxmox and Wazuh
# ══════════════════════════════════════════════════════════════════════════════
class _HttpApiConnector(BaseConnector):
    """Shared read-only HTTP client. TLS verification is ON and has no off switch.

    The endpoint comes from an enrolled environment, and the credential is read
    from the environment's ``credentials_ref`` — a REFERENCE (an env-var name),
    never a stored secret. A connector with no credential reference is
    MISCONFIGURED rather than anonymous: silently probing unauthenticated is how
    a reader becomes a scanner.
    """

    def __init__(self, connector_id: str, *, endpoint: str, credentials_ref: str = "",
                 bounds: WorldBounds | None = None, enabled: bool = True,
                 verify_tls: bool = True) -> None:
        super().__init__(connector_id, bounds=bounds, enabled=enabled)
        self.endpoint = (endpoint or "").strip()
        self.credentials_ref = (credentials_ref or "").strip()
        # Present as a field so a reviewer can see it is pinned True. There is
        # no constructor path in this module that passes False.
        self.verify_tls = bool(verify_tls)

    def _credential(self) -> str:
        import os
        if not self.credentials_ref:
            return ""
        return os.environ.get(self.credentials_ref, "")

    async def _probe(self) -> ConnectorState:
        if not self.endpoint:
            return ConnectorState.MISCONFIGURED
        if not self.credentials_ref:
            return ConnectorState.MISCONFIGURED
        if not self._credential():
            return ConnectorState.MISCONFIGURED
        try:
            from core.url_policy import validate_url
            validate_url(self.endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"CONNECTOR {self.connector_id}: endpoint refused by "
                           f"url_policy: {exc}")
            return ConnectorState.MISCONFIGURED
        return ConnectorState.AVAILABLE

    async def _get_json(self, path: str) -> tuple[dict | None, str]:
        """One bounded GET. Returns (payload, error). Never raises outward."""
        import httpx
        url = f"{self.endpoint.rstrip('/')}/{path.lstrip('/')}"
        try:
            from core.url_policy import validate_url
            validate_url(url)
        except Exception as exc:  # noqa: BLE001
            return None, f"url refused by policy: {exc}"
        headers = {"Authorization": self._credential()} if self._credential() else {}
        try:
            async with httpx.AsyncClient(
                verify=self.verify_tls, timeout=self.bounds.connector_timeout_s,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers)
                body = response.content[: self.bounds.max_connector_response_bytes]
                if response.status_code >= 400:
                    return None, f"HTTP {response.status_code}"
                return json.loads(body.decode("utf-8", "replace")), ""
        except Exception as exc:  # noqa: BLE001
            return None, redact(f"{type(exc).__name__}: {exc}")


class ProxmoxConnector(_HttpApiConnector):
    """Proxmox VE cluster inventory, read-only (``/api2/json/cluster/resources``)."""

    connector_type = "proxmox"

    async def _collect(self) -> ConnectorResult:
        state = await self._probe()
        if state is not ConnectorState.AVAILABLE:
            return ConnectorResult(self.connector_id, state,
                                   notes=["proxmox endpoint not configured or "
                                          "credential reference unset"])
        payload, error = await self._get_json("/api2/json/cluster/resources")
        if payload is None:
            return ConnectorResult(self.connector_id, ConnectorState.UNAVAILABLE,
                                   error=error)
        observations = []
        for item in (payload.get("data") or [])[: self.bounds.max_connector_items]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", ""))
            name = str(item.get("name") or item.get("node") or item.get("id") or "")
            if not name:
                continue
            etype = {"qemu": AssetType.VM, "lxc": AssetType.CONTAINER,
                     "node": AssetType.SERVER, "storage": AssetType.DATASTORE}.get(
                         kind, AssetType.UNKNOWN)
            if etype is AssetType.UNKNOWN:
                continue
            obs = self._observe(
                entity_type=etype, identity=name, event_type="proxmox_resource",
                payload={"status": str(item.get("status", "")),
                         "node": str(item.get("node", "")),
                         "resource_type": kind},
                trust=ObservationTrust.REPORTED, confidence=0.8)
            if obs:
                observations.append(obs)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=observations)


class WazuhConnector(_HttpApiConnector):
    """Wazuh manager agent inventory, read-only (``/agents``)."""

    connector_type = "wazuh"

    async def _collect(self) -> ConnectorResult:
        state = await self._probe()
        if state is not ConnectorState.AVAILABLE:
            return ConnectorResult(self.connector_id, state,
                                   notes=["wazuh endpoint not configured or "
                                          "credential reference unset"])
        payload, error = await self._get_json("/agents")
        if payload is None:
            return ConnectorResult(self.connector_id, ConnectorState.UNAVAILABLE,
                                   error=error)
        items = ((payload.get("data") or {}).get("affected_items")
                 if isinstance(payload.get("data"), dict) else None) or []
        observations = []
        for agent in items[: self.bounds.max_connector_items]:
            if not isinstance(agent, dict):
                continue
            name = str(agent.get("name") or agent.get("id") or "")
            if not name:
                continue
            obs = self._observe(
                entity_type=AssetType.SECURITY_CONTROL, identity=name,
                event_type="wazuh_agent",
                payload={"status": str(agent.get("status", "")),
                         "agent_id": str(agent.get("id", "")),
                         "os": str((agent.get("os") or {}).get("name", ""))},
                trust=ObservationTrust.REPORTED, confidence=0.8)
            if obs:
                observations.append(obs)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=observations)


# ══════════════════════════════════════════════════════════════════════════════
#  Zeek — a log directory, not a network tap
# ══════════════════════════════════════════════════════════════════════════════
class ZeekConnector(BaseConnector):
    """Reports whether the configured Zeek log directory is producing logs.

    It deliberately does NOT parse connection bodies: :mod:`tools.zeek_dpi`
    already streams those into the canonical event pipeline. This connector's
    only job is the situational fact "is the sensor alive and how fresh is it".
    """

    connector_type = "zeek"

    def __init__(self, connector_id: str, *, log_dir: str = "",
                 bounds: WorldBounds | None = None, enabled: bool = True) -> None:
        super().__init__(connector_id, bounds=bounds, enabled=enabled)
        self.log_dir = (log_dir or "").strip()

    async def _probe(self) -> ConnectorState:
        from pathlib import Path
        if not self.log_dir:
            return ConnectorState.MISCONFIGURED
        return (ConnectorState.AVAILABLE if Path(self.log_dir).is_dir()
                else ConnectorState.UNAVAILABLE)

    async def _collect(self) -> ConnectorResult:
        from pathlib import Path
        state = await self._probe()
        if state is not ConnectorState.AVAILABLE:
            return ConnectorResult(self.connector_id, state,
                                   notes=[f"zeek log dir {self.log_dir or '(unset)'} "
                                          f"is not a directory"])
        root = Path(self.log_dir)

        def _scan() -> tuple[int, float]:
            newest, count = 0.0, 0
            for path in sorted(root.glob("*.log"))[: self.bounds.max_connector_items]:
                try:
                    newest = max(newest, path.stat().st_mtime)
                    count += 1
                except OSError:
                    continue
            return count, newest

        count, newest = await asyncio.to_thread(_scan)
        age = max(0.0, time.time() - newest) if newest else -1.0
        health = ("healthy" if 0 <= age < 300 else
                  "warning" if 0 <= age < 3_600 else "critical")
        obs = self._observe(
            entity_type=AssetType.SECURITY_SENSOR, identity=f"zeek:{root.name}",
            event_type="zeek_sensor_health",
            payload={"status": "running" if count else "stopped", "health": health,
                     "log_files": count, "newest_log_age_s": round(age, 1)},
            trust=ObservationTrust.INSTRUMENTED, confidence=0.85)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=[o for o in (obs,) if o])


# ══════════════════════════════════════════════════════════════════════════════
#  Generic service health — the two liveness probes
# ══════════════════════════════════════════════════════════════════════════════
class HttpHealthConnector(BaseConnector):
    """Liveness for ONE configured URL. Never a range, never a discovered host."""

    connector_type = "http_health"

    def __init__(self, connector_id: str, *, url: str, service_name: str = "",
                 bounds: WorldBounds | None = None, enabled: bool = True) -> None:
        super().__init__(connector_id, bounds=bounds, enabled=enabled)
        self.url = (url or "").strip()
        self.service_name = service_name or connector_id

    async def _probe(self) -> ConnectorState:
        if not self.url:
            return ConnectorState.MISCONFIGURED
        try:
            from core.url_policy import validate_url
            validate_url(self.url)
        except Exception:  # noqa: BLE001
            return ConnectorState.MISCONFIGURED
        return ConnectorState.AVAILABLE

    async def _collect(self) -> ConnectorResult:
        state = await self._probe()
        if state is not ConnectorState.AVAILABLE:
            return ConnectorResult(self.connector_id, state,
                                   notes=["url unset or refused by url_policy"])
        import httpx
        started = time.monotonic()
        status, health, note = "stopped", "critical", ""
        try:
            async with httpx.AsyncClient(
                verify=True, timeout=self.bounds.connector_timeout_s,
                follow_redirects=False,
            ) as client:
                response = await client.get(self.url)
                code = response.status_code
                status = "running" if code < 500 else "degraded"
                health = ("healthy" if code < 400 else
                          "warning" if code < 500 else "critical")
                note = f"HTTP {code}"
        except Exception as exc:  # noqa: BLE001 — unreachable is a RESULT
            note = redact(f"{type(exc).__name__}")
        obs = self._observe(
            entity_type=AssetType.SERVICE, identity=self.service_name,
            event_type="http_health_probe",
            payload={"status": status, "health": health, "detail": note,
                     "latency_ms": round((time.monotonic() - started) * 1000, 1)},
            trust=ObservationTrust.INSTRUMENTED, confidence=0.85)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=[o for o in (obs,) if o], notes=[note])


class TcpHealthConnector(BaseConnector):
    """Liveness for ONE configured host:port. No range, no sweep, no discovery."""

    connector_type = "tcp_health"

    def __init__(self, connector_id: str, *, host: str, port: int,
                 service_name: str = "", bounds: WorldBounds | None = None,
                 enabled: bool = True) -> None:
        super().__init__(connector_id, bounds=bounds, enabled=enabled)
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.service_name = service_name or f"{self.host}:{self.port}"

    async def _probe(self) -> ConnectorState:
        if not self.host or not (0 < self.port < 65_536):
            return ConnectorState.MISCONFIGURED
        return ConnectorState.AVAILABLE

    async def _collect(self) -> ConnectorResult:
        state = await self._probe()
        if state is not ConnectorState.AVAILABLE:
            return ConnectorResult(self.connector_id, state,
                                   notes=["host/port unset or out of range"])
        started = time.monotonic()
        status, health, note = "stopped", "critical", ""
        writer = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.bounds.connector_timeout_s)
            status, health, note = "running", "healthy", "connect ok"
        except Exception as exc:  # noqa: BLE001 — refused is a RESULT
            note = type(exc).__name__
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
        obs = self._observe(
            entity_type=AssetType.SERVICE, identity=self.service_name,
            event_type="tcp_health_probe",
            payload={"status": status, "health": health, "detail": note,
                     "host": self.host, "port": self.port,
                     "latency_ms": round((time.monotonic() - started) * 1000, 1)},
            trust=ObservationTrust.INSTRUMENTED, confidence=0.85)
        return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                               observations=[o for o in (obs,) if o], notes=[note])


# ══════════════════════════════════════════════════════════════════════════════
#  The registry
# ══════════════════════════════════════════════════════════════════════════════
class ConnectorRegistry:
    """Holds the connectors and runs them with bounded concurrency.

    One connector's failure never affects another's: every collection is
    isolated, timed out and turned into a result rather than an exception.
    """

    def __init__(self, *, bounds: WorldBounds | None = None) -> None:
        self.bounds = bounds or DEFAULT_BOUNDS
        self._connectors: dict[str, BaseConnector] = {}

    def register(self, connector: BaseConnector) -> None:
        if len(self._connectors) >= self.bounds.max_subscribers:
            raise ValueError(
                f"connector registry is full at {self.bounds.max_subscribers}; "
                f"a registry that grows without limit is an unbounded work queue")
        if not getattr(connector, "is_read_only", False):
            raise ValueError(
                f"connector {connector.connector_id!r} does not declare itself "
                f"read-only; a writing connector belongs behind ToolExecutor")
        self._connectors[connector.connector_id] = connector

    def get(self, connector_id: str) -> BaseConnector | None:
        return self._connectors.get(connector_id)

    def all(self) -> list[BaseConnector]:
        return [self._connectors[k] for k in sorted(self._connectors)]

    async def probe_all(self) -> dict[str, ConnectorState]:
        out: dict[str, ConnectorState] = {}
        for connector in self.all():
            out[connector.connector_id] = await connector.probe()
        return out

    async def collect_all(self) -> list[ConnectorResult]:
        """Run every connector, at most ``max_connector_concurrency`` at a time."""
        semaphore = asyncio.Semaphore(self.bounds.max_connector_concurrency)

        async def _one(connector: BaseConnector) -> ConnectorResult:
            async with semaphore:
                return await connector.collect()

        gathered = await asyncio.gather(
            *(_one(c) for c in self.all()), return_exceptions=True)
        results: list[ConnectorResult] = []
        for connector, outcome in zip(self.all(), gathered):
            if isinstance(outcome, ConnectorResult):
                results.append(outcome)
            else:
                results.append(ConnectorResult(
                    connector.connector_id, ConnectorState.DEGRADED,
                    error=redact(f"{type(outcome).__name__}: {outcome}")))
        return results

    def summary(self, results: list[ConnectorResult]) -> dict:
        by_state: dict[str, int] = {}
        for r in results:
            by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "connectors": len(results),
            "by_state": dict(sorted(by_state.items())),
            "observations": sum(len(r.observations) for r in results),
            "optional_missing": [r.connector_id for r in results if r.optional_missing],
            "results": [r.to_dict() for r in results],
        }


def build_default_registry(*, registry=None, bounds: WorldBounds | None = None
                           ) -> ConnectorRegistry:
    """Build the registry from ENROLLED, AUTHORIZED environments only.

    The local host is always present — it is where this process runs, so
    reading it needs no enrollment. Everything else must be enrolled AND
    authorized in :class:`core.environment_registry.EnvironmentRegistry`; an
    environment that is merely enrolled is skipped, because enrollment records
    that a thing exists and authorization records that we may look at it.
    """
    out = ConnectorRegistry(bounds=bounds)
    out.register(LocalHostConnector("local-host", bounds=bounds))

    if registry is None:
        try:
            from core.environment_registry import get_env_registry
            registry = get_env_registry()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"WORLD_CONNECTORS: no environment registry ({exc})")
            return out

    from core.environment_registry import EnvironmentType
    for entry in registry.authorized_environments():
        try:
            if entry.env_type is EnvironmentType.DOCKER:
                out.register(DockerConnector(f"docker:{entry.env_id}", bounds=bounds))
            elif entry.env_type is EnvironmentType.PROXMOX:
                out.register(ProxmoxConnector(
                    f"proxmox:{entry.env_id}", endpoint=entry.endpoint,
                    credentials_ref=entry.credentials_ref, bounds=bounds))
            elif entry.env_type is EnvironmentType.WAZUH:
                out.register(WazuhConnector(
                    f"wazuh:{entry.env_id}", endpoint=entry.endpoint,
                    credentials_ref=entry.credentials_ref, bounds=bounds))
            elif entry.env_type is EnvironmentType.ZEEK:
                out.register(ZeekConnector(f"zeek:{entry.env_id}",
                                           log_dir=entry.endpoint, bounds=bounds))
            elif entry.env_type is EnvironmentType.HTTP_SERVICE:
                out.register(HttpHealthConnector(
                    f"http:{entry.env_id}", url=entry.endpoint,
                    service_name=entry.display_name, bounds=bounds))
            elif entry.env_type is EnvironmentType.TCP_SERVICE:
                host, _, port = entry.endpoint.rpartition(":")
                out.register(TcpHealthConnector(
                    f"tcp:{entry.env_id}", host=host, port=int(port or 0),
                    service_name=entry.display_name, bounds=bounds))
        except Exception as exc:  # noqa: BLE001 — one bad entry, not the registry
            logger.warning(f"WORLD_CONNECTORS: skipping {entry.env_id}: {exc}")
    return out


__all__ = [
    "OPTIONAL_OK", "REDACTED", "SCHEMA_VERSION", "BaseConnector",
    "ConnectorRegistry", "ConnectorResult", "ConnectorState", "DockerConnector",
    "HttpHealthConnector", "LocalHostConnector", "ProxmoxConnector",
    "TcpHealthConnector", "WazuhConnector", "WorldConnector", "ZeekConnector",
    "build_default_registry", "redact",
]
