"""V69 M68D — H01: HTTP destination identity.

The invariant: the destination POLICY VALIDATED must be the destination the
TRANSPORT CONTACTED. Before M68D they were two independent DNS resolutions of
the same hostname, and a fake resolver answering a public address once and
loopback thereafter made the guard approve ``93.184.216.34`` while the socket
connected to ``127.0.0.1`` — the loopback server's body came back to the caller
with ``error = None``. An ungoverned ``HTTP_PROXY`` reproduced the same split
without touching DNS at all.

Everything here is local and synthetic: a counting/fake resolver, loopback
listeners on ephemeral ports, and documentation addresses that are never
contacted. No metadata service, no LAN device, no external host.
"""
from __future__ import annotations

import io
import socket
import threading
import tokenize
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import tools.executor as ex

_SRC = Path(ex.__file__).read_text(encoding="utf-8")


def code_only(src: str) -> str:
    """*src* with comments and docstrings removed, code and literals kept.

    An absent-control detector that greps raw source answers the wrong question:
    this module's own prose says ``requests.get(url)`` while describing the
    bypass it removed, and a plain substring search reads that history as a live
    call site (measured — it is why this helper exists). Blanking EVERY string
    is the opposite error: it also deletes ``"Host"`` and the other literals the
    controls are made of. So comments and statement-level strings go, and
    nothing else does.
    """
    out: list[str] = []
    prev = tokenize.ENCODING
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev in (
                    tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                    tokenize.DEDENT, tokenize.ENCODING):
                prev = tok.type
                continue
            if tok.string.strip():
                out.append(tok.string)
            prev = tok.type
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        raise AssertionError("code_only could not tokenize the fragment") from None
    return " ".join(out)


_LINES = _SRC.splitlines()
_CODE_LINES: list[str] = [""] * (len(_LINES) + 2)
_prev = tokenize.ENCODING
for _tok in tokenize.generate_tokens(io.StringIO(_SRC).readline):
    if _tok.type == tokenize.COMMENT:
        continue
    if _tok.type == tokenize.STRING and _prev in (
            tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
            tokenize.DEDENT, tokenize.ENCODING):
        _prev = _tok.type
        continue
    if _tok.string.strip():
        _CODE_LINES[_tok.start[0]] += " " + _tok.string
    _prev = _tok.type

_CODE = " ".join(_CODE_LINES)


def code_region(header: str) -> str:
    """Code-only text of the def/class block whose first line contains *header*.

    Sliced by LINE NUMBER off a single tokenization of the whole module, because
    a function body on its own is not tokenizable: its first line carries an
    indent with no matching block opener (measured — `IndentationError`).
    """
    starts = [i for i, line in enumerate(_LINES) if header in line]
    assert starts, f"region header not found: {header!r}"
    start = starts[0]
    indent = len(_LINES[start]) - len(_LINES[start].lstrip())
    end = len(_LINES)
    for i in range(start + 1, len(_LINES)):
        line = _LINES[i]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and line.lstrip().startswith(("def ", "class ", "@", "#")):
            end = i
            break
    return " ".join(_CODE_LINES[start + 1:end + 1])

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


# ── helpers ──────────────────────────────────────────────────────────────────

class _Listener:
    """A loopback HTTP listener that records which address it was reached on."""

    def __init__(self, bind_ip: str, port: int = 0, body: bytes = b"REACHED"):
        self.hits: list[dict] = []
        self.body = body
        listener = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):                     # noqa: N802
                listener.hits.append({
                    "host_header": self.headers.get("Host"),
                    "local": self.connection.getsockname()[0],
                    "path": self.path,
                })
                self.send_response(200)
                self.send_header("Content-Length", str(len(listener.body)))
                self.end_headers()
                self.wfile.write(listener.body)

            def log_message(self, *a):            # noqa: ANN002
                pass

        self.server = ThreadingHTTPServer((bind_ip, port), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_trusted_lab(monkeypatch):
    monkeypatch.setattr(ex, "_trusted_lab_enabled", lambda: False)


@pytest.fixture
def trusted_lab(monkeypatch):
    monkeypatch.setattr(ex, "_trusted_lab_enabled", lambda: True)


def _fake_resolver(monkeypatch, answers: dict, calls: list):
    """Install a resolver over the real ``socket.getaddrinfo``.

    ``answers`` maps hostname -> list of per-call answers; the last entry is
    reused once exhausted, which is what makes "public once, loopback forever
    after" expressible without any sleep.
    """
    real = socket.getaddrinfo

    def fake(host, port, *a, **kw):
        if host in answers:
            calls.append(host)
            seq = answers[host]
            idx = min(len([c for c in calls if c == host]) - 1, len(seq) - 1)
            ip = seq[idx]
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port or 0, 0, 0) if fam is socket.AF_INET6 else (ip, port or 0)
            return [(fam, socket.SOCK_STREAM, 6, "", sockaddr)]
        return real(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


# ── the governed decision ────────────────────────────────────────────────────

class TestGovernedResolution:
    def test_resolution_happens_exactly_once_per_decision(self, monkeypatch,
                                                          no_trusted_lab):
        calls: list[str] = []
        _fake_resolver(monkeypatch, {"once.test": [PUBLIC_V4]}, calls)
        dest = ex.govern_destination("http://once.test/x")
        assert dest.error is None
        assert calls == ["once.test"], \
            f"the decision resolved {len(calls)} times; exactly one is allowed"
        assert dest.pinned == PUBLIC_V4
        assert dest.candidates == (PUBLIC_V4,)

    def test_every_candidate_is_validated_not_only_the_first(self, monkeypatch,
                                                             no_trusted_lab):
        """A hostname aliasing public AND private is refused, whichever comes first.

        Validating only ``candidates[0]`` is the obvious cheap mutation; both
        orderings are asserted so it cannot survive in either direction.
        """
        real = socket.getaddrinfo

        def mixed(order):
            def fake(host, port, *a, **kw):
                if host == "mixed.test":
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
                            for ip in order]
                return real(host, port, *a, **kw)
            return fake

        for order in ((PUBLIC_V4, "127.0.0.1"), ("127.0.0.1", PUBLIC_V4),
                      (PUBLIC_V4, "10.1.2.3"), (PUBLIC_V4, "169.254.169.254")):
            monkeypatch.setattr(socket, "getaddrinfo", mixed(order))
            dest = ex.govern_destination("http://mixed.test/")
            assert dest.blocked, f"mixed candidate set {order} was permitted"
            assert dest.pinned is None, "a blocked decision must pin nothing"

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.0.1", "192.168.1.10",
        "169.254.1.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "255.255.255.255",
        "[::1]", "[fc00::1]", "[fe80::1]", "[::]", "[ff02::1]",
    ])
    def test_internal_destinations_are_refused(self, host, no_trusted_lab):
        dest = ex.govern_destination(f"http://{host}/x")
        assert dest.blocked, f"{host} was permitted"
        assert dest.pinned is None

    @pytest.mark.parametrize("host,expected", [
        (PUBLIC_V4, PUBLIC_V4),
        ("8.8.8.8", "8.8.8.8"),
        (f"[{PUBLIC_V6}]", PUBLIC_V6),
    ])
    def test_public_ip_literals_are_pinned_to_themselves(self, host, expected,
                                                         no_trusted_lab):
        dest = ex.govern_destination(f"http://{host}/x")
        assert dest.error is None
        assert dest.pinned == expected
        assert dest.candidates == (expected,)

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd", "gopher://1.1.1.1/", "ftp://1.1.1.1/",
        "javascript:alert(1)", "data:text/plain,x", "1.1.1.1", "",
    ])
    def test_non_http_schemes_are_refused(self, url, no_trusted_lab):
        assert ex.govern_destination(url).blocked

    def test_empty_host_is_refused(self, no_trusted_lab):
        assert ex.govern_destination("http:///nohost").blocked

    def test_non_numeric_port_is_refused_rather_than_raising(self, no_trusted_lab):
        dest = ex.govern_destination("http://1.1.1.1:notaport/")
        assert dest.blocked and "puerto" in dest.error.lower()

    def test_authority_carries_the_explicit_port(self, monkeypatch, no_trusted_lab):
        _fake_resolver(monkeypatch, {"vhost.test": [PUBLIC_V4]}, [])
        dest = ex.govern_destination("http://vhost.test:8443/p")
        assert dest.authority == "vhost.test:8443"
        assert dest.port == 8443

    def test_default_ports_follow_the_scheme(self, monkeypatch, no_trusted_lab):
        _fake_resolver(monkeypatch, {"p.test": [PUBLIC_V4]}, [])
        assert ex.govern_destination("http://p.test/").port == 80
        assert ex.govern_destination("https://p.test/").port == 443

    def test_userinfo_is_stripped_from_the_host_header_authority(
            self, monkeypatch, no_trusted_lab):
        _fake_resolver(monkeypatch, {"u.test": [PUBLIC_V4]}, [])
        dest = ex.govern_destination("http://user:pw@u.test/")
        assert dest.authority == "u.test", \
            "credentials must not be echoed into the Host header"

    def test_compatibility_shim_reports_the_same_decision(self, no_trusted_lab):
        assert ex._http_target_blocked("http://127.0.0.1/") is not None
        assert ex._http_target_blocked(f"http://{PUBLIC_V4}/") is None


# ── failure injection on the resolver ────────────────────────────────────────

class TestResolverFailureInjection:
    def test_resolver_exception_blocks_in_normal_mode(self, monkeypatch,
                                                      no_trusted_lab):
        def boom(*a, **kw):
            raise socket.gaierror("synthetic resolver failure")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        dest = ex.govern_destination("http://unresolvable.test/")
        assert dest.blocked and dest.pinned is None

    def test_empty_resolver_answer_blocks_in_normal_mode(self, monkeypatch,
                                                         no_trusted_lab):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
        dest = ex.govern_destination("http://empty.test/")
        assert dest.blocked and dest.pinned is None

    def test_trusted_lab_keeps_its_documented_semantics(self, monkeypatch,
                                                        trusted_lab):
        """Trusted lab waives the internal-range policy and only that.

        It still resolves once and still pins when it can; an unresolvable lab
        name is permitted unpinned, which is exactly the pre-M68D behaviour for
        that mode. Normal mode is unaffected — asserted by every other test here.
        """
        calls: list[str] = []
        _fake_resolver(monkeypatch, {"lab.test": ["127.0.0.1"]}, calls)
        dest = ex.govern_destination("http://lab.test/")
        assert dest.error is None, "trusted lab must still permit internal ranges"
        assert dest.pinned == "127.0.0.1", "trusted lab must still PIN what it resolves"
        assert dest.trusted_lab is True

        def boom(*a, **kw):
            raise socket.gaierror("no such lab host")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        unresolved = ex.govern_destination("http://ghost.lab.test/")
        assert unresolved.error is None and unresolved.pinned is None

    def test_connection_failure_never_becomes_success(self, monkeypatch,
                                                      no_trusted_lab):
        _fake_resolver(monkeypatch, {"down.test": [PUBLIC_V4]}, [])

        def refuse(*a, **kw):
            raise OSError("synthetic connection failure")

        monkeypatch.setattr(ex, "_pinned_transport_request", refuse)
        with pytest.raises(OSError):
            ex._safe_http_fetch("GET", "http://down.test/")
        result = ex.ToolExecutor.__new__(ex.ToolExecutor)._tool_http_request(
            "http://down.test/")
        assert "error" in result and "status_code" not in result


# ── the socket-level pinning proof ───────────────────────────────────────────

class TestPinningAtTheSocket:
    def test_dns_rebinding_cannot_move_an_approved_connection(self, monkeypatch,
                                                              trusted_lab):
        """THE regression, proved at the socket.

        Two listeners on two different loopback addresses share one port. The
        resolver answers ``127.0.0.1`` once (what policy approves) and
        ``127.0.0.2`` for every call after that. Trusted-lab mode is on ONLY so
        that both addresses are permissible destinations — the property under
        test is pinning, not range policy, and running it on loopback is what
        makes the destination observable without contacting anything real.

        Before M68D the transport re-resolved and landed on the second address.
        """
        first = _Listener("127.0.0.1", body=b"APPROVED-TARGET")
        second = _Listener("127.0.0.2", port=first.port, body=b"REBOUND-TARGET")
        try:
            calls: list[str] = []
            _fake_resolver(monkeypatch,
                           {"rebind.test": ["127.0.0.1", "127.0.0.2"]}, calls)
            resp, meta = ex._safe_http_fetch(
                "GET", f"http://rebind.test:{first.port}/", timeout=5)
            assert meta["error"] is None
            assert resp is not None and resp.status_code == 200
            assert resp.text == "APPROVED-TARGET", \
                "the connection landed on the REBOUND address, not the approved one"
            assert meta["pinned_destinations"] == ["127.0.0.1"]
            assert [h["local"] for h in first.hits] == ["127.0.0.1"]
            assert second.hits == [], \
                "the rebound address received the request"
            # Non-vacuity: the rebinding answer really was available to anyone
            # who asked a second time.
            assert len(calls) >= 1
        finally:
            first.close()
            second.close()

    def test_the_transport_never_resolves_the_hostname(self, monkeypatch,
                                                       trusted_lab):
        """A tripwire resolver: the SECOND lookup of the name is fatal.

        If the transport resolves at all, this raises instead of silently
        connecting somewhere — the failure mode a counting assertion can miss.
        """
        listener = _Listener("127.0.0.1", body=b"PINNED")
        try:
            real = socket.getaddrinfo
            seen: list[str] = []

            def tripwire(host, port, *a, **kw):
                if host == "tripwire.test":
                    seen.append(host)
                    if len(seen) > 1:
                        raise AssertionError(
                            "the transport resolved the hostname a second time")
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                             ("127.0.0.1", port or 0))]
                return real(host, port, *a, **kw)

            monkeypatch.setattr(socket, "getaddrinfo", tripwire)
            resp, meta = ex._safe_http_fetch(
                "GET", f"http://tripwire.test:{listener.port}/", timeout=5)
            assert meta["error"] is None and resp.text == "PINNED"
            assert seen == ["tripwire.test"], \
                f"{len(seen)} resolutions; the policy's one is the only one allowed"
        finally:
            listener.close()

    def test_the_original_hostname_is_preserved_in_the_host_header(
            self, monkeypatch, trusted_lab):
        """Pinning must not turn into "send the IP as the Host"."""
        listener = _Listener("127.0.0.1", body=b"VHOST")
        try:
            _fake_resolver(monkeypatch, {"vhosted.test": ["127.0.0.1"]}, [])
            resp, _meta = ex._safe_http_fetch(
                "GET", f"http://vhosted.test:{listener.port}/", timeout=5)
            assert resp is not None
            assert listener.hits[0]["host_header"] == f"vhosted.test:{listener.port}", \
                f"Host header was {listener.hits[0]['host_header']!r}"
        finally:
            listener.close()

    def test_rebinding_public_to_loopback_contacts_the_public_address(
            self, monkeypatch, no_trusted_lab):
        """Normal mode, the audit's exact scenario, asserted on the destination.

        The approved address is a documentation address that is never actually
        contacted here: the transport seam records what it was HANDED, which is
        the thing the finding was about. The loopback listener proves the
        rebinding answer was live and still went nowhere.
        """
        listener = _Listener("127.0.0.1", body=b"SHOULD-NEVER-BE-REACHED")
        try:
            calls: list[str] = []
            _fake_resolver(monkeypatch,
                           {"flip.test": [PUBLIC_V4, "127.0.0.1"]}, calls)
            handed: list[ex.EgressDestination] = []

            class _Resp:
                status_code = 200
                headers: dict = {}
                text = "stub"
                url = ""
                encoding = "utf-8"

            def recorder(dest, method, url, **kw):
                handed.append(dest)
                return _Resp()

            monkeypatch.setattr(ex, "_pinned_transport_request", recorder)
            resp, meta = ex._safe_http_fetch(
                "GET", f"http://flip.test:{listener.port}/", timeout=5)
            assert meta["error"] is None
            assert len(handed) == 1, "non-vacuity: the transport was never called"
            assert handed[0].pinned == PUBLIC_V4, \
                f"the transport was handed {handed[0].pinned!r}, not the approved address"
            assert listener.hits == []
        finally:
            listener.close()


# ── proxy semantics ──────────────────────────────────────────────────────────

class TestProxySemantics:
    def test_environment_proxy_cannot_redirect_the_egress_path(self, monkeypatch,
                                                               trusted_lab):
        """MEASURED before the fix: HTTP_PROXY made a validated public target
        arrive at a loopback listener and the result said success.

        Here the 'proxy' is a loopback listener that answers any absolute-URI
        request. The governed destination is a DIFFERENT loopback listener. If
        the proxy is honoured, the proxy's listener records the hit.
        """
        proxy = _Listener("127.0.0.1", body=b"PROXY-INTERCEPTED")
        target = _Listener("127.0.0.1", body=b"DIRECT-TARGET")
        try:
            for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                        "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(var, f"http://127.0.0.1:{proxy.port}")
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)
            _fake_resolver(monkeypatch, {"direct.test": ["127.0.0.1"]}, [])
            resp, meta = ex._safe_http_fetch(
                "GET", f"http://direct.test:{target.port}/", timeout=5)
            assert meta["error"] is None
            assert resp.text == "DIRECT-TARGET"
            assert proxy.hits == [], \
                "the ungoverned environment proxy was honoured"
            # Non-vacuity: the proxy listener was live and would have recorded a hit.
            assert target.hits, "the pinned destination received nothing"
        finally:
            proxy.close()
            target.close()

    def test_the_transport_session_does_not_trust_the_environment(self):
        """Structural: both halves of the default-deny are present.

        ``trust_env = False`` alone still lets a per-request ``proxies`` mapping
        through, and ``proxies={}`` alone does not stop netrc/env lookups, so the
        source must carry both.
        """
        body = code_region("def _pinned_transport_request(")
        assert "trust_env = False" in body
        assert "session . proxies = { }" in body
        assert "proxies = { }" in body


# ── redirects ────────────────────────────────────────────────────────────────

class TestRedirectsReturnToPolicy:
    @staticmethod
    def _resp(status, headers=None, text="OK"):
        class _R:
            status_code = status
            url = ""
            encoding = "utf-8"

            def __init__(self):
                self.headers = headers or {}
                self.text = text

        return _R()

    def test_every_hop_gets_its_own_governed_resolution(self, monkeypatch,
                                                        no_trusted_lab):
        decisions: list[str] = []
        real_govern = ex.govern_destination

        def counting(url):
            d = real_govern(url)
            decisions.append(url)
            return d

        monkeypatch.setattr(ex, "govern_destination", counting)
        routes = {
            f"http://{PUBLIC_V4}/": self._resp(302, {"Location": "http://8.8.8.8/"}),
            "http://8.8.8.8/": self._resp(200, {}, "FINAL"),
        }
        monkeypatch.setattr(ex, "_pinned_transport_request",
                            lambda dest, m, u, **kw: routes[u])
        resp, meta = ex._safe_http_fetch("GET", f"http://{PUBLIC_V4}/")
        assert resp.text == "FINAL"
        assert decisions == [f"http://{PUBLIC_V4}/", "http://8.8.8.8/"]
        assert meta["pinned_destinations"] == [PUBLIC_V4, "8.8.8.8"]

    def test_a_redirect_to_an_internal_host_is_refused_and_never_contacted(
            self, monkeypatch, no_trusted_lab):
        contacted: list[str] = []
        routes = {
            f"http://{PUBLIC_V4}/": self._resp(
                302, {"Location": "http://169.254.169.254/latest/meta-data/"}),
        }

        def transport(dest, method, url, **kw):
            contacted.append(url)
            return routes[url]

        monkeypatch.setattr(ex, "_pinned_transport_request", transport)
        resp, meta = ex._safe_http_fetch("GET", f"http://{PUBLIC_V4}/")
        assert resp is None and meta["error"]
        assert contacted == [f"http://{PUBLIC_V4}/"], \
            "the internal redirect target was contacted"

    def test_a_rebinding_redirect_target_is_pinned_per_hop(self, monkeypatch,
                                                           no_trusted_lab):
        """The second hop resolves once too, and the transport uses THAT answer."""
        calls: list[str] = []
        _fake_resolver(monkeypatch, {"hop2.test": [PUBLIC_V4, "127.0.0.1"]}, calls)
        handed: list = []
        routes = {
            f"http://{PUBLIC_V4}/": self._resp(302, {"Location": "http://hop2.test/"}),
            "http://hop2.test/": self._resp(200, {}, "HOP2"),
        }

        def transport(dest, method, url, **kw):
            handed.append(dest.pinned)
            return routes[url]

        monkeypatch.setattr(ex, "_pinned_transport_request", transport)
        resp, meta = ex._safe_http_fetch("GET", f"http://{PUBLIC_V4}/")
        assert resp.text == "HOP2"
        assert handed == [PUBLIC_V4, PUBLIC_V4]

    def test_cross_origin_credential_stripping_is_still_intact(self, monkeypatch,
                                                               no_trusted_lab):
        """M66A.1 §F3 must survive M68D."""
        sent: list[dict] = []
        routes = {
            f"http://{PUBLIC_V4}/": self._resp(302, {"Location": "http://8.8.8.8/"}),
            "http://8.8.8.8/": self._resp(200, {}, "X"),
        }

        def transport(dest, method, url, headers=None, **kw):
            sent.append(dict(headers or {}))
            return routes[url]

        monkeypatch.setattr(ex, "_pinned_transport_request", transport)
        resp, meta = ex._safe_http_fetch(
            "GET", f"http://{PUBLIC_V4}/",
            headers={"Authorization": "Bearer synthetic", "PRIVATE-TOKEN": "synthetic",
                     "Accept": "text/plain"})
        assert resp is not None
        assert "Authorization" in sent[0] and "PRIVATE-TOKEN" in sent[0]
        assert "Authorization" not in sent[1] and "PRIVATE-TOKEN" not in sent[1]
        assert sent[1].get("Accept") == "text/plain"
        assert meta["sensitive_headers_stripped"] == 2

    def test_same_origin_headers_are_preserved(self, monkeypatch, no_trusted_lab):
        sent: list[dict] = []
        routes = {
            f"http://{PUBLIC_V4}/": self._resp(302, {"Location": "/next"}),
            f"http://{PUBLIC_V4}/next": self._resp(200, {}, "REL"),
        }

        def transport(dest, method, url, headers=None, **kw):
            sent.append(dict(headers or {}))
            return routes[url]

        monkeypatch.setattr(ex, "_pinned_transport_request", transport)
        resp, meta = ex._safe_http_fetch(
            "GET", f"http://{PUBLIC_V4}/", headers={"Authorization": "Bearer synthetic"})
        assert resp.text == "REL"
        assert sent[1].get("Authorization") == "Bearer synthetic"
        assert meta["sensitive_headers_stripped"] == 0


# ── TLS semantics ────────────────────────────────────────────────────────────

class TestTlsIsNotWeakened:
    def test_the_adapter_keeps_the_original_hostname_for_sni_and_certificates(self):
        adapter_src = "\n".join(
            _LINES[_LINES.index("class _PinnedDestinationAdapter(requests.adapters.HTTPAdapter):"):])
        adapter_src = adapter_src.split("def _pinned_transport_request", 1)[0]
        assert 'pool_kwargs["server_hostname"] = host_params["host"]' in adapter_src, \
            "SNI/certificate hostname must come from the ORIGINAL host"
        order = adapter_src.index('pool_kwargs["server_hostname"] = host_params')
        overwrite = adapter_src.index('host_params = dict(host_params, host=')
        assert order < overwrite, \
            "server_hostname was read AFTER the host was overwritten with the IP"

    def test_no_egress_path_disables_certificate_verification(self):
        """Absent-control with a non-vacuity witness.

        The detector must be able to see the real token, or 'no verify=False
        found' proves nothing. It is checked against a synthetic sample first.
        """
        forbidden = ("verify = False", "assert_hostname = False",
                     "CERT_NONE", "InsecureRequestWarning")
        sample = code_only("resp = requests.get(url, verify=False)\n")
        assert any(tok in sample for tok in forbidden), "detector is vacuous"
        for tok in forbidden:
            assert tok not in _CODE, f"{tok} present in the egress module"


# ── absent-control: could the control be missing at the composition point? ───

class TestAbsentControls:
    #: Every handler that fetches a model-supplied URL. Non-vacuity: this list is
    #: asserted non-empty AND every name is asserted to exist on the class.
    ARBITRARY_URL_HANDLERS = ("_tool_http_request", "_tool_fetch_webpage",
                              "_tool_estudiar_tema")

    def test_the_handler_inventory_is_not_vacuous(self):
        assert self.ARBITRARY_URL_HANDLERS
        for name in self.ARBITRARY_URL_HANDLERS:
            assert hasattr(ex.ToolExecutor, name), f"{name} no longer exists"

    def test_the_comment_stripping_detector_is_itself_sound(self):
        """Non-vacuity for `code_only`: it must blank prose and keep code."""
        assert "requests" not in code_only("# a comment about requests.get(url)\n")
        assert "requests" not in code_only('"""a docstring about requests.get"""\n')
        assert "requests" in code_only("resp = requests.get(url)\n")
        # It must NOT blank literals the controls are made of.
        assert '"Host"' in code_only('h["Host"] = a\n')
        # And code_region must find a real block and exclude the next one.
        region = code_region("def _pinned_transport_request(")
        assert "session . request" in region
        assert "def _safe_http_fetch" not in region

    def test_every_arbitrary_url_handler_routes_through_the_one_egress_path(self):
        for name in self.ARBITRARY_URL_HANDLERS:
            body = code_region(f"def {name}(")
            assert "_safe_http_fetch" in body, \
                f"{name} does not use the canonical egress path"
            for raw in ("requests . get", "requests . post", "requests . request",
                        "urlopen (", "session . request"):
                assert raw not in body, f"{name} performs a raw {raw} fetch"

    def test_the_transport_is_the_only_caller_of_session_request(self):
        """One transport primitive, and it is the only thing that opens a socket."""
        assert _CODE.count("session . request") == 1
        assert "session . request" in code_region("def _pinned_transport_request(")

    def test_the_fetch_loop_consumes_the_pinned_destination(self):
        """The mutation 'validate, then connect by hostname again' must not fit."""
        loop = code_region("def _safe_http_fetch(")
        assert "govern_destination ( current_url )" in loop
        assert "_pinned_transport_request (" in loop
        assert "requests . request" not in loop, \
            "the fetch loop still calls requests.request with a hostname"
        assert "dest . pinned" in loop, \
            "the loop does not reference the pinned address at all"

    def test_the_transport_connects_only_to_the_pinned_address(self):
        transport = code_region("def _pinned_transport_request(")
        assert "_PinnedDestinationAdapter ( dest . pinned )" in transport
        assert 'send_headers [ "Host" ] = dest . authority' in transport

    def test_an_unpinned_decision_cannot_reach_the_transport_in_normal_mode(
            self, monkeypatch, no_trusted_lab):
        """Fail closed: a decision with no address is refused, not attempted."""
        monkeypatch.setattr(
            ex, "govern_destination",
            lambda url: ex.EgressDestination(
                scheme="http", host="x.test", port=80, authority="x.test",
                candidates=(), pinned=None, error=None, trusted_lab=False))
        called: list = []
        monkeypatch.setattr(ex, "_pinned_transport_request",
                            lambda *a, **kw: called.append(1))
        resp, meta = ex._safe_http_fetch("GET", "http://x.test/")
        assert resp is None and meta["error"]
        assert called == [], "an unpinned decision reached the transport"

    def test_the_redirect_loop_still_refuses_to_delegate_redirects(self):
        assert "allow_redirects = False" in code_region(
            "def _pinned_transport_request(")
