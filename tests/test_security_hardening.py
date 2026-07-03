"""
tests/test_security_hardening.py — V60.0 executor hardening regression tests.

Covers two concrete LLM-facing attack surfaces closed in V60.0:

  1. FORCE_OVERRIDE — a model-generated tool argument must NOT be able to
     disable the destructive-pattern guardrails. The only legitimate override
     is operator-set trusted-lab mode (JARVIS_TRUSTED_LAB), read from the
     environment, never from tool input.

  2. http_request SSRF — outbound HTTP must reject loopback / RFC1918 private /
     link-local (incl. 169.254.169.254 cloud metadata) / multicast / reserved
     targets, including hostnames that resolve to them, unless trusted-lab mode
     is enabled.

Pure unit/integration tests — no Ollama, network egress, or audio required.
The SSRF cases use IP literals so no DNS lookup is performed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest

import tools.executor as _executor_mod
from tools.executor import (
    ToolExecutor,
    _http_target_blocked,
    _strip_override,
    _trusted_lab_enabled,
)


@pytest.fixture
def executor() -> ToolExecutor:
    return ToolExecutor()


@pytest.fixture(autouse=True)
def _force_hardened_default(monkeypatch):
    """Ensure trusted-lab mode is OFF unless a test opts in."""
    monkeypatch.delenv("JARVIS_TRUSTED_LAB", raising=False)
    yield


# ─────────────────────────── FORCE_OVERRIDE ────────────────────────────────

class TestForceOverride:
    def test_guardrail_blocks_root_delete(self, executor):
        result = executor.execute("write_file", {"path": "n.txt", "content": "rm -rf / "})
        assert "error" in result and "GUARDRAIL" in result["error"]

    def test_force_override_does_not_bypass_guardrail(self, executor):
        """The headline fix: FORCE_OVERRIDE=true in tool input must NOT bypass."""
        result = executor.execute(
            "write_file",
            {"path": "n.txt", "content": "rm -rf / ", "FORCE_OVERRIDE": True},
        )
        assert "error" in result and "GUARDRAIL" in result["error"]

    def test_validate_guardrails_ignores_override_key(self, executor):
        """Even if the key reaches the guardrail directly, it is ignored."""
        block = executor._validate_guardrails(
            "x", {"command": "rm -rf / ", "FORCE_OVERRIDE": True}
        )
        assert block is not None

    def test_system_write_override_does_not_bypass(self, executor):
        block = executor._validate_guardrails(
            "x", {"command": "reg add HKLM\\Foo", "FORCE_OVERRIDE": "yes"}
        )
        assert block is not None

    def test_strip_override_removes_key(self):
        cleaned = _strip_override("write_file", {"path": "p", "FORCE_OVERRIDE": True})
        assert "FORCE_OVERRIDE" not in cleaned
        assert cleaned["path"] == "p"

    def test_strip_override_noop_when_absent(self):
        original = {"path": "p"}
        assert _strip_override("write_file", original) is original

    def test_trusted_lab_allows_override(self, executor, monkeypatch):
        monkeypatch.setenv("JARVIS_TRUSTED_LAB", "true")
        assert _trusted_lab_enabled() is True
        block = executor._validate_guardrails("x", {"command": "rm -rf / "})
        assert block is None

    def test_trusted_lab_off_by_default(self):
        assert _trusted_lab_enabled() is False


# ───────────────────────────── http_request SSRF ───────────────────────────

class TestSSRF:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",   # AWS/GCP metadata
        "http://192.168.1.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",                           # multicast
    ])
    def test_internal_targets_blocked(self, url):
        assert _http_target_blocked(url) is not None

    def test_public_ip_allowed(self):
        assert _http_target_blocked("http://1.1.1.1/") is None
        assert _http_target_blocked("https://8.8.8.8/") is None

    def test_non_http_scheme_rejected(self):
        assert _http_target_blocked("file:///etc/passwd") is not None
        assert _http_target_blocked("gopher://1.1.1.1/") is not None

    def test_empty_host_rejected(self):
        assert _http_target_blocked("http:///nohost") is not None

    def test_trusted_lab_allows_internal(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TRUSTED_LAB", "true")
        assert _http_target_blocked("http://127.0.0.1/") is None
        assert _http_target_blocked("http://169.254.169.254/") is None

    def test_http_request_handler_blocks_metadata(self, executor):
        """End-to-end through the sync gate — no network egress occurs."""
        result = executor.execute("http_request", {"url": "http://169.254.169.254/"})
        assert "error" in result and "SSRF" in result["error"]


# ───────────────────────────── read_file sandbox ───────────────────────────

class TestReadFileSandbox:
    def test_read_outside_sandbox_blocked(self, executor):
        # /etc/passwd (Linux) or an absolute system path is outside the sandbox.
        result = executor.execute("read_file", {"path": "/etc/shadow"})
        assert "error" in result


# ───────────────────── http_request redirect SSRF (F2) ─────────────────────

class _FakeResp:
    def __init__(self, status_code, headers=None, text="OK", url="", encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url
        self.encoding = encoding


class _FakeRequests:
    """Route-table stand-in for the `requests` module used by the executor.

    Records every fetched URL and refuses any URL not explicitly routed, so a
    test fails loudly if the handler ever fetches an unchecked redirect target.
    All targets are IP literals — _http_target_blocked never performs DNS.
    """

    def __init__(self, routes):
        self.routes = routes
        self.fetched: list[str] = []

    def request(self, method, url, headers=None, data=None, timeout=None,
                allow_redirects=True, **kw):
        assert allow_redirects is False, "handler must follow redirects manually"
        self.fetched.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected (unchecked) fetch to {url!r}")
        return self.routes[url]


class TestSSRFRedirect:
    def _run(self, executor, routes, monkeypatch, url="http://1.1.1.1/"):
        fake = _FakeRequests(routes)
        monkeypatch.setattr(_executor_mod, "requests", fake)
        result = executor.execute("http_request", {"url": url})
        return result, fake

    def test_redirect_to_metadata_blocked(self, executor, monkeypatch):
        target = "http://169.254.169.254/latest/meta-data/"
        routes = {"http://1.1.1.1/": _FakeResp(302, {"Location": target})}
        result, fake = self._run(executor, routes, monkeypatch)
        assert "error" in result
        assert "SSRF" in result["error"] or "bloqueado" in result["error"].lower()
        assert target not in fake.fetched          # never fetched the internal host

    def test_redirect_to_loopback_blocked(self, executor, monkeypatch):
        routes = {"http://1.1.1.1/": _FakeResp(302, {"Location": "http://127.0.0.1/"})}
        result, fake = self._run(executor, routes, monkeypatch)
        assert "error" in result
        assert "http://127.0.0.1/" not in fake.fetched

    def test_redirect_to_private_blocked(self, executor, monkeypatch):
        for loc in ("http://10.0.0.1/", "http://192.168.1.10/"):
            routes = {"http://1.1.1.1/": _FakeResp(302, {"Location": loc})}
            result, fake = self._run(executor, routes, monkeypatch)
            assert "error" in result
            assert loc not in fake.fetched

    def test_public_to_public_redirect_allowed(self, executor, monkeypatch):
        routes = {
            "http://1.1.1.1/": _FakeResp(302, {"Location": "http://8.8.8.8/"}),
            "http://8.8.8.8/": _FakeResp(200, text="FINAL", url="http://8.8.8.8/"),
        }
        result, fake = self._run(executor, routes, monkeypatch)
        assert result.get("status_code") == 200
        assert result.get("body") == "FINAL"
        assert "http://8.8.8.8/" in fake.fetched

    def test_relative_public_redirect_allowed(self, executor, monkeypatch):
        routes = {
            "http://1.1.1.1/": _FakeResp(302, {"Location": "/next"}),
            "http://1.1.1.1/next": _FakeResp(200, text="REL", url="http://1.1.1.1/next"),
        }
        result, fake = self._run(executor, routes, monkeypatch)
        assert result.get("status_code") == 200
        assert result.get("body") == "REL"

    def test_too_many_redirects_blocked(self, executor, monkeypatch):
        routes = {
            "http://1.1.1.1/": _FakeResp(302, {"Location": "http://8.8.8.8/"}),
            "http://8.8.8.8/": _FakeResp(302, {"Location": "http://1.1.1.1/"}),
        }
        result, fake = self._run(executor, routes, monkeypatch)
        assert "error" in result and "redirecc" in result["error"].lower()
        assert len(fake.fetched) <= 6              # hop count is bounded

    def test_malformed_redirect_location_fails_closed(self, executor, monkeypatch):
        routes = {"http://1.1.1.1/": _FakeResp(302, {"Location": "javascript:alert(1)"})}
        result, fake = self._run(executor, routes, monkeypatch)
        assert "error" in result

    def test_missing_location_header_fails_closed(self, executor, monkeypatch):
        routes = {"http://1.1.1.1/": _FakeResp(302, {})}  # 30x with no Location
        result, fake = self._run(executor, routes, monkeypatch)
        assert "error" in result

    def test_trusted_lab_follows_internal_redirect(self, executor, monkeypatch):
        monkeypatch.setenv("JARVIS_TRUSTED_LAB", "true")
        routes = {
            "http://1.1.1.1/": _FakeResp(302, {"Location": "http://127.0.0.1/"}),
            "http://127.0.0.1/": _FakeResp(200, text="LAB", url="http://127.0.0.1/"),
        }
        result, fake = self._run(executor, routes, monkeypatch)
        assert result.get("status_code") == 200    # trusted-lab bypass preserved
        assert result.get("body") == "LAB"
