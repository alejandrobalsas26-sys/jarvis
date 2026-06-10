"""
tests/test_context_manager.py — V58.0 COGNITIVE CORE context compression.

Verifies token-saver compression keeps high-signal security data, secret
redaction works, and context packets are built/redacted. Pure CPU, no network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jarvis"))

import pytest
from core.context_manager import ContextManager


@pytest.fixture
def cm() -> ContextManager:
    return ContextManager()


class TestRedaction:
    def test_redacts_api_key(self, cm):
        out = cm.redact_secrets('api_key="sk-abcdef1234567890ABCDEF"')
        assert "sk-abcdef1234567890" not in out
        assert "REDACTED" in out

    def test_redacts_password_kv(self, cm):
        out = cm.redact_secrets("password: SuperSecret123")
        assert "SuperSecret123" not in out

    def test_redacts_bearer_token(self, cm):
        out = cm.redact_secrets("Authorization: Bearer abcdefgh12345678ZZ")
        assert "abcdefgh12345678" not in out

    def test_redacts_aws_key(self, cm):
        out = cm.redact_secrets("key AKIAIOSFODNN7EXAMPLE here")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_redacts_private_key_block(self, cm):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        out = cm.redact_secrets(pem)
        assert "MIIabc" not in out
        assert "REDACTED_PRIVATE_KEY" in out

    def test_preserves_plain_text(self, cm):
        text = "Host 10.0.0.5 flagged severity 9 with IOC sha256 deadbeef"
        assert cm.redact_secrets(text) == text


class TestCompression:
    def test_under_budget_unchanged(self, cm):
        msgs = [{"role": "user", "content": "short"}]
        out = cm.compress_messages(msgs, budget_chars=1000)
        assert out["dropped"] == 0
        assert out["messages"] == msgs

    def test_keeps_high_signal_drops_noise(self, cm):
        noise = [{"role": "assistant", "content": "debug heartbeat tick " + "x" * 500}
                 for _ in range(20)]
        signal = {"role": "tool", "content": "CRITICAL alert host 10.0.0.9 IOC malware c2 beacon"}
        msgs = [{"role": "system", "content": "sys"}] + noise + [signal]
        out = cm.compress_messages(msgs, budget_chars=1500)
        assert out["dropped"] > 0
        # system + high-signal observation survive
        contents = " ".join(m["content"] for m in out["messages"])
        assert "CRITICAL alert host 10.0.0.9" in contents
        assert "sys" in contents

    def test_system_always_preserved(self, cm):
        msgs = [{"role": "system", "content": "S" * 100}] + [
            {"role": "user", "content": "U" * 4000} for _ in range(5)
        ]
        out = cm.compress_messages(msgs, budget_chars=500)
        assert any(m["role"] == "system" for m in out["messages"])


class TestToolSummary:
    def test_summarizes_and_keeps_errors(self, cm):
        results = [
            {"status": "ok", "debug": "x" * 100},
            {"error": "connection timeout to host"},
            {"severity": 9.5, "host": "10.0.0.1", "indicator": "evil.com"},
        ]
        summary = cm.summarize_tool_results(results, budget_chars=2000)
        assert "ERROR" in summary
        assert "timeout" in summary
        assert "10.0.0.1" in summary

    def test_redacts_secrets_in_summary(self, cm):
        results = [{"result": 'token="ghp-aaaaaaaaaaaaaaaaaaaa"'}]
        summary = cm.summarize_tool_results(results)
        assert "ghp-aaaaaaaaaaaaaaaaaaaa" not in summary


class TestPrioritizeAndPacket:
    def test_prioritize_orders_by_severity(self, cm):
        items = [
            {"content": "routine log", "severity": 1},
            {"content": "breach detected", "severity": 9},
        ]
        ordered = cm.prioritize_context(items)
        assert ordered[0]["severity"] == 9

    def test_build_context_packet_redacts(self, cm):
        packet = cm.build_context_packet({
            "objective": "contain incident api_key=secret1234token",
            "facts": ["host 10.0.0.2 compromised"],
            "constraints": ["no destructive actions"],
            "observations": [{"content": "malware beacon", "severity": 8}],
        })
        assert packet.redacted is True
        assert "secret1234token" not in packet.objective
        assert packet.char_count > 0
        assert "host 10.0.0.2 compromised" in packet.facts
