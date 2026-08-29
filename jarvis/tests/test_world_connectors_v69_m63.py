"""V69 M63 — connector protocol: boundedness, isolation, redaction, and the
security properties a read-only connector must have.

Every test is OFFLINE. The only sockets any of these open are to a closed
loopback port, deliberately, to prove that "refused" is a RESULT and not a crash.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from core.asset_graph import AssetType
from core.world_bounds import WorldBounds
from core.world_connectors import (
    OPTIONAL_OK,
    REDACTED,
    BaseConnector,
    ConnectorRegistry,
    ConnectorResult,
    ConnectorState,
    DockerConnector,
    HttpHealthConnector,
    LocalHostConnector,
    ProxmoxConnector,
    TcpHealthConnector,
    WazuhConnector,
    ZeekConnector,
    build_default_registry,
    redact,
)
from core.world_state import ObservationTrust


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeConnector(BaseConnector):
    connector_type = "fake"

    def __init__(self, cid="fake", *, items=1, state=ConnectorState.AVAILABLE,
                 explode=False, hang=False, bounds=None):
        super().__init__(cid, bounds=bounds)
        self._items, self._state = items, state
        self._explode, self._hang = explode, hang

    async def _probe(self):
        if self._explode:
            raise RuntimeError("probe exploded")
        return self._state

    async def _collect(self):
        if self._explode:
            raise RuntimeError("collect exploded")
        if self._hang:
            await asyncio.sleep(60)
        obs = [self._observe(entity_type=AssetType.CONTAINER, identity=f"c{i}",
                             event_type="fake", payload={"status": "running"},
                             trust=ObservationTrust.INSTRUMENTED)
               for i in range(self._items)]
        return ConnectorResult(self.connector_id, self._state,
                               observations=[o for o in obs if o])


class _WritingConnector(BaseConnector):
    connector_type = "writer"
    is_read_only = False

    async def _probe(self):
        return ConnectorState.AVAILABLE


# ── protocol shape ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("cls,kwargs", [
    (LocalHostConnector, {}),
    (DockerConnector, {}),
    (ProxmoxConnector, {"endpoint": "https://pve.lan:8006", "credentials_ref": "PVE"}),
    (WazuhConnector, {"endpoint": "https://wazuh.lan:55000", "credentials_ref": "WZ"}),
    (ZeekConnector, {"log_dir": "/nonexistent"}),
    (HttpHealthConnector, {"url": "http://127.0.0.1:9/health"}),
    (TcpHealthConnector, {"host": "127.0.0.1", "port": 9}),
])
def test_every_connector_declares_read_only(cls, kwargs):
    connector = cls("c", **kwargs)
    assert connector.is_read_only is True
    assert connector.connector_type


def test_registry_refuses_a_connector_that_is_not_read_only():
    with pytest.raises(ValueError, match="read-only"):
        ConnectorRegistry().register(_WritingConnector("w"))


def test_registry_is_bounded():
    registry = ConnectorRegistry(bounds=WorldBounds(max_subscribers=2))
    registry.register(_FakeConnector("a"))
    registry.register(_FakeConnector("b"))
    with pytest.raises(ValueError, match="full"):
        registry.register(_FakeConnector("c"))


# ── isolation, timeout, bounds ───────────────────────────────────────────────
def test_one_exploding_connector_does_not_affect_the_others():
    registry = ConnectorRegistry()
    registry.register(_FakeConnector("good"))
    registry.register(_FakeConnector("bad", explode=True))
    results = {r.connector_id: r for r in asyncio.run(registry.collect_all())}
    assert results["good"].state is ConnectorState.AVAILABLE
    assert results["bad"].state is ConnectorState.DEGRADED
    assert "exploded" in results["bad"].error


def test_a_hanging_connector_times_out_into_degraded():
    connector = _FakeConnector("slow", hang=True,
                               bounds=WorldBounds(connector_timeout_s=0.5))
    result = asyncio.run(connector.collect())
    assert result.state is ConnectorState.DEGRADED
    assert "timed out" in result.error


def test_probe_failure_never_raises_outward():
    assert asyncio.run(_FakeConnector("x", explode=True).probe()) is ConnectorState.UNAVAILABLE


def test_oversized_result_is_truncated_and_marked_degraded():
    connector = _FakeConnector("many", items=50,
                               bounds=WorldBounds(max_connector_items=5))
    result = asyncio.run(connector.collect())
    assert len(result.observations) == 5
    assert result.truncated is True
    assert result.state is ConnectorState.DEGRADED, "truncation is not a clean success"


def test_collection_concurrency_is_capped():
    live, peak = 0, 0

    class _Counting(BaseConnector):
        connector_type = "counting"

        async def _probe(self):
            return ConnectorState.AVAILABLE

        async def _collect(self):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE)

    registry = ConnectorRegistry(bounds=WorldBounds(max_connector_concurrency=2))
    for i in range(8):
        registry.register(_Counting(f"c{i}"))
    asyncio.run(registry.collect_all())
    assert peak <= 2


def test_disabled_connector_reports_disabled_and_collects_nothing():
    connector = _FakeConnector("off")
    connector.enabled = False
    result = asyncio.run(connector.collect())
    assert result.state is ConnectorState.DISABLED
    assert result.observations == []


# ── optional-missing semantics ───────────────────────────────────────────────
def test_missing_optional_connector_is_optional_missing_not_failure():
    result = ConnectorResult("x", ConnectorState.UNAVAILABLE)
    assert result.optional_missing is True
    assert result.ok is False
    assert ConnectorState.UNAVAILABLE in OPTIONAL_OK
    assert ConnectorState.DISABLED in OPTIONAL_OK


def test_unconfigured_api_connector_is_misconfigured_not_silently_anonymous():
    for cls in (ProxmoxConnector, WazuhConnector):
        connector = cls("c", endpoint="", credentials_ref="")
        assert asyncio.run(connector.probe()) is ConnectorState.MISCONFIGURED
        assert asyncio.run(connector.collect()).state is ConnectorState.MISCONFIGURED


def test_api_connector_without_credential_reference_refuses_to_probe(monkeypatch):
    connector = ProxmoxConnector("pve", endpoint="https://pve.lan:8006",
                                 credentials_ref="PVE_TOKEN_ABSENT")
    monkeypatch.delenv("PVE_TOKEN_ABSENT", raising=False)
    assert asyncio.run(connector.probe()) is ConnectorState.MISCONFIGURED


def test_zeek_connector_without_log_dir_is_misconfigured():
    assert asyncio.run(ZeekConnector("z", log_dir="").probe()) is ConnectorState.MISCONFIGURED


def test_tcp_connector_reports_refused_service_as_a_result_not_a_crash():
    result = asyncio.run(TcpHealthConnector("t", host="127.0.0.1", port=1).collect())
    assert result.state is ConnectorState.AVAILABLE, "the CONNECTOR worked"
    payload = result.observations[0].payload
    assert payload["status"] == "stopped" and payload["health"] == "critical"


# ── redaction ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload", [
    {"password": "hunter2"},
    {"api_key": "abc"},
    {"Authorization": "Bearer xyz"},
    {"secret_token": "s"},
    {"private_key": "k"},
])
def test_secret_keys_are_redacted(payload):
    assert redact(payload) == {list(payload)[0]: REDACTED}


def test_secret_looking_values_are_redacted():
    assert REDACTED in redact("Bearer abcdefghijklmnopqrstuvwx")
    assert REDACTED in redact("-----BEGIN RSA PRIVATE KEY-----")


def test_redaction_reaches_nested_structures():
    out = redact({"outer": {"password": "p", "fine": "ok"}, "list": [{"token": "t"}]})
    assert out["outer"]["password"] == REDACTED
    assert out["outer"]["fine"] == "ok"
    assert out["list"][0]["token"] == REDACTED


def test_connector_observations_are_redacted_before_leaving_the_connector():
    class _Leaky(BaseConnector):
        connector_type = "leaky"

        async def _probe(self):
            return ConnectorState.AVAILABLE

        async def _collect(self):
            obs = self._observe(entity_type=AssetType.SERVICE, identity="s",
                                event_type="e",
                                payload={"status": "running", "password": "hunter2"},
                                trust=ObservationTrust.INSTRUMENTED)
            return ConnectorResult(self.connector_id, ConnectorState.AVAILABLE,
                                   observations=[obs])

    result = asyncio.run(_Leaky("leaky").collect())
    assert result.observations[0].payload["password"] == REDACTED


def test_error_text_is_redacted():
    result = ConnectorResult("c", ConnectorState.DEGRADED,
                             error="failed with token abcdefghijklmnopqrstuvwxyz012345")
    assert REDACTED in result.to_dict()["error"]


# ── security: no discovery, no shell, no authority ───────────────────────────
# These scan the AST, not the raw text. The module DOCSTRING legitimately names
# ToolExecutor and AuthorityMode in order to state that it must never touch
# them, and a raw substring scan would flag exactly the sentence that documents
# the guarantee. Executable code is what matters, so executable code is what is
# checked: identifiers, attributes, imports and keyword arguments.
import ast as _ast

_CONNECTOR_PATH = __import__("core.world_connectors", fromlist=["x"]).__file__
_CONNECTOR_SOURCE = inspect.getsource(
    __import__("core.world_connectors", fromlist=["x"]))
_TREE = _ast.parse(_CONNECTOR_SOURCE)


def _identifiers() -> set[str]:
    """Every name, attribute and imported symbol the module actually uses."""
    out: set[str] = set()
    for node in _ast.walk(_TREE):
        if isinstance(node, _ast.Name):
            out.add(node.id)
        elif isinstance(node, _ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, _ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, _ast.ImportFrom):
            out.add(node.module or "")
            out.update(a.name for a in node.names)
    return out


def _keyword_values() -> list[tuple[str, object]]:
    out = []
    for node in _ast.walk(_TREE):
        if isinstance(node, _ast.Call):
            for kw in node.keywords:
                if kw.arg and isinstance(kw.value, _ast.Constant):
                    out.append((kw.arg, kw.value.value))
    return out


def test_no_shell_true_anywhere_in_the_connector_layer():
    assert ("shell", True) not in _keyword_values()


def test_no_os_system_or_popen_in_the_connector_layer():
    used = _identifiers()
    assert "system" not in used
    assert "popen" not in used


def test_no_network_auto_discovery_primitives():
    """No sweep, no scan, no range expansion. Targets are configured, period."""
    used = _identifiers()
    for banned in ("ip_network", "iter_hosts", "nmap", "masscan", "port_scan",
                   "scan_subnet", "arp"):
        assert banned not in used, f"{banned} implies target expansion"


def test_tls_verification_is_never_disabled():
    assert ("verify", False) not in _keyword_values()
    connector = ProxmoxConnector("p", endpoint="https://x", credentials_ref="R")
    assert connector.verify_tls is True


def test_connector_layer_does_not_import_the_tool_executor():
    """A connector that could reach ToolExecutor could cause an effect."""
    used = _identifiers()
    assert "tools.executor" not in used
    assert "ToolExecutor" not in used


def test_connector_layer_cannot_touch_authority_or_scope():
    used = _identifiers()
    for banned in ("AuthorityMode", "ScopePolicy", "authorize_action",
                   "set_authority"):
        assert banned not in used


def test_http_connector_endpoint_must_pass_url_policy():
    for bad in ("file:///etc/passwd", "ftp://x/y", "not-a-url"):
        assert asyncio.run(HttpHealthConnector("h", url=bad).probe()) \
            is ConnectorState.MISCONFIGURED


# ── the default registry ─────────────────────────────────────────────────────
def test_default_registry_only_includes_authorized_environments():
    class _Registry:
        def authorized_environments(self):
            return []

    registry = build_default_registry(registry=_Registry())
    assert [c.connector_id for c in registry.all()] == ["local-host"], \
        "nothing beyond the local host may appear without operator authorization"


def test_enrolled_but_unauthorized_environment_is_not_connected():
    from core.environment_registry import EnvironmentEntry, EnvironmentType

    class _Registry:
        def authorized_environments(self):
            return []          # enrolled-but-unauthorized never reaches here

        def all(self):
            return [EnvironmentEntry(env_id="pve", env_type=EnvironmentType.PROXMOX,
                                     display_name="pve", authorized=False)]

    registry = build_default_registry(registry=_Registry())
    assert not any("proxmox" in c.connector_id for c in registry.all())


def test_local_host_connector_produces_a_usable_observation():
    result = asyncio.run(LocalHostConnector("local").collect())
    assert result.state is ConnectorState.AVAILABLE
    assert result.observations
    assert result.observations[0].entity_type is AssetType.PHYSICAL_HOST
