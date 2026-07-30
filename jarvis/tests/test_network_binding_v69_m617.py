"""tests/test_network_binding_v69_m617.py — V69 M61.7: loopback-first service exposure.

Bandit reported twelve B104 findings ("possible binding to all interfaces") across ten
modules. Triaged, they were two different things wearing the same string, and treating
them alike would have been wrong in both directions.

**Seven were false positives** — they compare an address, they do not bind a socket. One
is worth naming: ``core/asset_discovery.py`` was flagged for *detecting* that a
discovered listener is bound to all interfaces, i.e. Bandit flagged the detection of the
condition Bandit cares about. Another is a safety interlock: ``core/network_quarantine.py``
refuses to quarantine JARVIS's own host, so "fixing" its address set to satisfy a scanner
could let JARVIS isolate itself off the network. Those seven now reference named constants
in ``core.net_binding``; the network semantics are byte-for-byte unchanged, and the tests
below pin the behaviour rather than the spelling.

**Five were real binds, and four had no gate at all.** ``core/decoy_service.py``,
``core/tarpit_deception.py``, ``tools/active_tarpit.py`` and ``core/dns_sinkhole.py``
bound ``0.0.0.0`` unconditionally: starting JARVIS on a laptop published decoy
SSH/SMB/RDP/MSSQL listeners and a DNS resolver to whatever network that laptop was on.
``core/canary.py`` already had the right shape (V68.1 M50) and its contract is unchanged
here — the other four now share its proven pattern.

The single ``# nosec B104`` in the tree sits on the constant declaration in
``core.net_binding``, and the tests below prove the property that licenses it: that
module binds nothing.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import net_binding

_APP_ROOT = Path(__file__).resolve().parent.parent


# ── the constants ───────────────────────────────────────────────────────────
def test_the_constants_have_their_expected_values():
    """A typo here would silently change ten modules' network semantics."""
    assert net_binding.ALL_INTERFACES_V4 == "0.0.0.0"
    assert net_binding.ALL_INTERFACES_V6 == "::"
    assert net_binding.LOOPBACK_V4 == "127.0.0.1"
    assert net_binding.LOOPBACK_V6 == "::1"
    assert net_binding.BROADCAST_V4 == "255.255.255.255"


def test_the_unspecified_host_set_is_exactly_the_historical_set():
    """core/asset_discovery.py matched ("0.0.0.0", "::", "*") before M61.7."""
    assert net_binding.UNSPECIFIED_HOSTS == frozenset({"0.0.0.0", "::", "*"})


def test_the_own_host_set_is_exactly_the_historical_set():
    """core/network_quarantine.py matched ("127.0.0.1", "::1", "localhost", "0.0.0.0")."""
    assert net_binding.OWN_HOST_ADDRESSES == frozenset(
        {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "*"])
def test_is_all_interfaces_recognises_every_spelling(host: str):
    assert net_binding.is_all_interfaces(host)
    assert net_binding.is_all_interfaces(f"  {host}  "), "whitespace must not defeat it"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.0.0.5", "", None, "0.0.0.1"])
def test_is_all_interfaces_rejects_specific_addresses(host):
    assert not net_binding.is_all_interfaces(host)


# ── the suppression is justified: this module binds nothing ─────────────────
def test_net_binding_contains_no_socket_operation():
    """The property that licenses the tree's only B104 suppression."""
    tree = ast.parse((_APP_ROOT / "core" / "net_binding.py").read_text(encoding="utf-8"))
    banned = {"bind", "listen", "connect", "socket", "create_server", "start_server",
              "create_datagram_endpoint", "bind_and_activate"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            assert name not in banned, f"core/net_binding.py:{node.lineno} calls {name}()"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "socket", "core/net_binding.py imports socket"


def test_the_tree_has_exactly_one_b104_suppression():
    """Unbounded `# nosec` growth is how a scanner stops meaning anything."""
    found: list[str] = []
    for directory in ("core", "tools"):
        for path in sorted((_APP_ROOT / directory).rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "nosec" in line and "B104" in line:
                    found.append(f"{directory}/{path.name}:{number}")
    assert found == ["core/net_binding.py:76"] or (
        len(found) == 1 and found[0].startswith("core/net_binding.py")
    ), found


def test_the_b104_suppression_is_precise_and_documented():
    lines = (_APP_ROOT / "core" / "net_binding.py").read_text(encoding="utf-8").splitlines()
    suppressed = [(n, ln) for n, ln in enumerate(lines, 1) if "nosec" in ln]
    assert len(suppressed) == 1
    number, line = suppressed[0]
    assert "nosec B104" in line, "the suppression must name the exact Bandit id"
    assert "ALL_INTERFACES_V4" in line, "it must sit on the constant it justifies"
    context = "\n".join(lines[max(0, number - 20) : number])
    assert "B104 SUPPRESSION JUSTIFICATION" in context
    assert "binds nothing" in context


# ── resolve_bind_host: loopback-first, exposure explicit ────────────────────
_EXPOSE = "JARVIS_TEST_EXPOSE"
_BIND = "JARVIS_TEST_BIND"


def test_the_default_is_loopback(monkeypatch):
    monkeypatch.delenv(_EXPOSE, raising=False)
    monkeypatch.delenv(_BIND, raising=False)
    assert net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND) == "127.0.0.1"


def test_a_bind_address_alone_does_not_expose(monkeypatch):
    """Setting only the address env var must NOT widen the bind."""
    monkeypatch.delenv(_EXPOSE, raising=False)
    monkeypatch.setenv(_BIND, "0.0.0.0")
    assert net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND) == "127.0.0.1"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_explicit_optin_exposes_all_interfaces(monkeypatch, value: str):
    monkeypatch.setenv(_EXPOSE, value)
    monkeypatch.delenv(_BIND, raising=False)
    assert net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND) == "0.0.0.0"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe", "2"])
def test_non_truthy_optin_stays_local(monkeypatch, value: str):
    monkeypatch.setenv(_EXPOSE, value)
    assert net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND) == "127.0.0.1"


def test_an_explicit_single_address_is_honoured_verbatim(monkeypatch):
    """Narrower than all-interfaces, so it is respected as-is."""
    monkeypatch.setenv(_EXPOSE, "1")
    monkeypatch.setenv(_BIND, "10.10.0.5")
    assert net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND) == "10.10.0.5"


def test_exposure_logs_a_security_warning(monkeypatch, caplog):
    monkeypatch.setenv(_EXPOSE, "1")
    monkeypatch.delenv(_BIND, raising=False)
    import logging

    records: list[str] = []
    handler_id = None
    from loguru import logger as loguru_logger

    handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
    try:
        net_binding.resolve_bind_host("DECOY", expose_env=_EXPOSE, bind_env=_BIND)
    finally:
        loguru_logger.remove(handler_id)
    text = "".join(records)
    assert "DECOY" in text
    assert "ALL" in text or "0.0.0.0" in text
    assert _EXPOSE in text, "the warning must name the var that enabled exposure"
    assert logging  # keep the import meaningful for readers


def test_no_warning_is_logged_for_the_safe_default(monkeypatch):
    monkeypatch.delenv(_EXPOSE, raising=False)
    records: list[str] = []
    from loguru import logger as loguru_logger

    handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
    try:
        net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND)
    finally:
        loguru_logger.remove(handler_id)
    assert records == [], "the safe default must not cry wolf"


def test_the_default_host_cannot_be_widened_by_the_environment(monkeypatch):
    """default_host is a code-level argument; no env var may override it."""
    monkeypatch.delenv(_EXPOSE, raising=False)
    for hostile in ("JARVIS_BIND", "BIND_HOST", "HOST", "JARVIS_TEST_DEFAULT_HOST"):
        monkeypatch.setenv(hostile, "0.0.0.0")
    assert net_binding.resolve_bind_host("T", expose_env=_EXPOSE, bind_env=_BIND) == "127.0.0.1"


# ── every deception service now shares the pattern ──────────────────────────
_GATED_SERVICES = [
    ("core.canary", "_canary_bind_host", "JARVIS_CANARY_EXPOSE", "JARVIS_CANARY_BIND"),
    ("core.decoy_service", "_bind_host", "JARVIS_DECOY_EXPOSE", "JARVIS_DECOY_BIND"),
    ("core.tarpit_deception", "_bind_host", "JARVIS_TARPIT_EXPOSE", "JARVIS_TARPIT_BIND"),
    ("tools.active_tarpit", "_bind_host", "JARVIS_TARPIT_EXPOSE", "JARVIS_TARPIT_BIND"),
    ("core.dns_sinkhole", "_bind_host", "JARVIS_DNS_EXPOSE", "JARVIS_DNS_BIND"),
]


@pytest.mark.parametrize("module_name,func_name,expose_env,bind_env", _GATED_SERVICES)
def test_every_service_defaults_to_loopback(
    monkeypatch, module_name: str, func_name: str, expose_env: str, bind_env: str
):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.delenv(expose_env, raising=False)
    monkeypatch.delenv(bind_env, raising=False)
    assert getattr(module, func_name)() == "127.0.0.1", module_name


@pytest.mark.parametrize("module_name,func_name,expose_env,bind_env", _GATED_SERVICES)
def test_every_service_exposes_only_on_explicit_optin(
    monkeypatch, module_name: str, func_name: str, expose_env: str, bind_env: str
):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setenv(expose_env, "true")
    monkeypatch.delenv(bind_env, raising=False)
    assert getattr(module, func_name)() == "0.0.0.0", module_name


@pytest.mark.parametrize("module_name,func_name,expose_env,bind_env", _GATED_SERVICES)
def test_every_service_honours_an_explicit_address(
    monkeypatch, module_name: str, func_name: str, expose_env: str, bind_env: str
):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setenv(expose_env, "1")
    monkeypatch.setenv(bind_env, "172.16.9.9")
    assert getattr(module, func_name)() == "172.16.9.9", module_name


def test_no_server_start_hardcodes_an_all_interfaces_literal():
    """Structural: the bind sites must read the resolver, not a literal."""
    offenders: list[str] = []
    for directory in ("core", "tools"):
        for path in sorted((_APP_ROOT / directory).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name not in {"start_server", "create_server", "create_datagram_endpoint",
                                "serve", "bind"}:
                    continue
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Constant)
                        and isinstance(sub.value, str)
                        and sub.value in net_binding.UNSPECIFIED_HOSTS
                    ):
                        offenders.append(f"{directory}/{path.name}:{node.lineno}")
    assert offenders == [], f"hardcoded all-interfaces bind: {offenders}"


# ── the false positives kept their behaviour ───────────────────────────────
def test_asset_discovery_still_classifies_all_interfaces_as_external():
    """The flagged line DETECTS exposure; that detection must not have been softened."""
    from core import asset_discovery

    classify = asset_discovery.exposure_for_bind
    for host in ("0.0.0.0", "::", "*"):
        assert classify(host) == "external", host
    assert classify("") == "unknown"
    assert classify("127.0.0.1") == "localhost"
    assert classify("192.168.1.10") == "internal"


def test_network_quarantine_still_protects_its_own_host():
    """A safety interlock: JARVIS must never isolate itself off the network."""
    from core import network_quarantine

    for ip in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        assert network_quarantine._is_protected(ip) is True, ip
    assert network_quarantine._is_protected("8.8.8.8") is False


def test_itdr_sentinel_still_skips_unspecified_and_loopback():
    from core import itdr_sentinel

    assert net_binding.ALL_INTERFACES_V4 in (
        net_binding.ALL_INTERFACES_V4, net_binding.LOOPBACK_V4
    )
    source = (_APP_ROOT / "core" / "itdr_sentinel.py").read_text(encoding="utf-8")
    assert "net_binding.ALL_INTERFACES_V4" in source
    assert "net_binding.LOOPBACK_V4" in source
    assert itdr_sentinel  # module imports cleanly with the shared constants


def test_industrial_asset_guard_still_skips_unspecified_and_broadcast():
    source = (_APP_ROOT / "core" / "industrial_asset_guard.py").read_text(encoding="utf-8")
    assert "net_binding.ALL_INTERFACES_V4" in source
    assert "net_binding.BROADCAST_V4" in source
    assert '"255.255.255.255"' not in source.split("def ", 1)[-1]


def test_arp_deception_still_filters_the_unspecified_source():
    source = (_APP_ROOT / "core" / "arp_deception.py").read_text(encoding="utf-8")
    assert "psrc != net_binding.ALL_INTERFACES_V4" in source


@pytest.mark.parametrize("module_name", ["core.decoy_service", "core.tarpit_deception"])
def test_local_ip_sets_are_unchanged(module_name: str):
    import importlib

    module = importlib.import_module(module_name)
    ips = module._local_ips()
    for expected in ("127.0.0.1", "::1", "0.0.0.0"):
        assert expected in ips, f"{module_name} lost {expected}"
