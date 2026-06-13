"""
tests/test_memory_and_verification.py — Phase 5 & 6 unit tests.

memory_router: secret refusal, scope classification, read/write decisions.
verification: should_verify triggers + fail-closed verify_answer (with a fake
async LLM client; no Ollama required).
"""
from __future__ import annotations

import asyncio

import pytest

from core import memory_router as mr
from core import verification as ver


# ───────────────────────────── memory_router ───────────────────────────────

class TestSecretRefusal:
    @pytest.mark.parametrize("text", [
        "api_key = sk-ABCDEFGH12345678ZXCV",
        "password: hunter2zzz",
        "Authorization: Bearer abcdefghij1234567890",
        "AKIAIOSFODNN7EXAMPLE is the key",
        "-----BEGIN RSA PRIVATE KEY-----",
        "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ])
    def test_detects_secrets(self, text):
        assert mr.contains_secret(text) is True

    def test_clean_text_not_flagged(self):
        assert mr.contains_secret("the weather in Panama is hot") is False

    def test_redaction(self):
        red = mr.redact_secrets("my api_key = sk-ABCDEFGH12345678ZXCV done")
        assert "sk-ABCDEFGH" not in red
        assert "REDACTED" in red

    def test_write_refuses_secret(self):
        assert mr.should_write_memory("remember this", "password: hunter2zzz") is False

    def test_scope_none_for_secret(self):
        assert mr.classify_memory_scope("my password: hunter2zzz") == "none"


class TestScopeClassification:
    def test_longterm_preference(self):
        assert mr.classify_memory_scope("from now on, default to dark mode") == "long_term"

    def test_project_scope(self):
        assert mr.classify_memory_scope("the architecture of this repo uses async") == "project"

    def test_session_scope(self):
        assert mr.classify_memory_scope("remember this for later") == "session"

    def test_none_scope(self):
        assert mr.classify_memory_scope("what's 2+2?") == "none"


class TestMemoryReadWrite:
    def test_should_use_memory_on_recall(self):
        assert mr.should_use_memory("what did we discuss earlier?") is True

    def test_should_not_use_memory_on_chatter(self):
        assert mr.should_use_memory("tell me a joke") is False

    def test_untrusted_source(self):
        assert mr.is_untrusted_source("web") is True
        assert mr.is_untrusted_source("user") is False


# ───────────────────────────── verification ────────────────────────────────

class TestShouldVerify:
    def test_tool_used_forces_verify(self):
        assert ver.should_verify("hi", tool_used=True) is True

    def test_security_sensitive_forces_verify(self):
        assert ver.should_verify("hi", security_sensitive=True) is True

    def test_security_keywords_trigger(self):
        assert ver.should_verify("explain this exploit and CVE") is True

    def test_plain_chat_no_verify(self):
        assert ver.should_verify("what time is it?") is False


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content=None, raise_exc=None):
        self._content = content
        self._raise = raise_exc

    async def create(self, **kwargs):
        if self._raise is not None:
            raise self._raise
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, content=None, raise_exc=None):
        self.chat = _FakeChat(_FakeCompletions(content, raise_exc))


class TestVerifyAnswer:
    def test_parses_valid_verdict(self):
        client = _FakeClient(content=(
            '{"verified": true, "confidence": 0.9, "issues": [], '
            '"needs_human_review": false, "reasoning": "looks good"}'
        ))
        res = asyncio.run(ver.verify_answer(client, "do x", "x is done"))
        assert res.verified is True
        assert res.confidence == pytest.approx(0.9)
        assert res.needs_human_review is False

    def test_fail_closed_on_exception(self):
        client = _FakeClient(raise_exc=RuntimeError("ollama down"))
        res = asyncio.run(ver.verify_answer(client, "do x", "x is done"))
        assert res.verified is False
        assert res.needs_human_review is True
        assert res.confidence == 0.0

    def test_fail_closed_on_garbage(self):
        client = _FakeClient(content="not json at all <<<")
        res = asyncio.run(ver.verify_answer(client, "do x", "x is done"))
        assert res.verified is False
        assert res.needs_human_review is True

    def test_handles_fenced_json(self):
        client = _FakeClient(content=(
            '```json\n{"verified": false, "confidence": 0.3, '
            '"issues": ["unsupported claim"], "reasoning": "x"}\n```'
        ))
        res = asyncio.run(ver.verify_answer(client, "do x", "x is done"))
        assert res.verified is False
        assert "unsupported claim" in res.issues
