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
