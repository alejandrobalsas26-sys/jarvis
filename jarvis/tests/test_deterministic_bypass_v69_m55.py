"""tests/test_deterministic_bypass_v69_m55.py — V69 M55.11: no-model bypasses.

Proves time/date/lifecycle/FAST-model/vault questions are answered from trusted
runtime data WITHOUT a model call, in the active language, and that anything else
falls through (returns None). Includes a chat_stream integration proving no
transport is invoked for a bypassed turn.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import core.host_time as host_time
from core.deterministic_bypass import (
    BypassKind,
    answer_bypass,
    classify_bypass,
    maybe_bypass,
)


def _freeze(dt: datetime):
    host_time.set_clock(lambda: dt)


def teardown_function(_):
    host_time.reset_clock()


def test_classify_time_and_date():
    assert classify_bypass("¿qué hora es?") is BypassKind.TIME
    assert classify_bypass("what time is it") is BypassKind.TIME
    assert classify_bypass("qué fecha es hoy") is BypassKind.DATE
    assert classify_bypass("what day is it") is BypassKind.DATE
    assert classify_bypass("explícame POO") is BypassKind.NONE


def test_time_answer_localized():
    _freeze(datetime(2026, 7, 16, 9, 30, 0, tzinfo=timezone.utc))
    es = maybe_bypass("¿qué hora es?", language="es")
    en = maybe_bypass("what time is it", language="en")
    assert es and es.startswith("Son las")
    assert en and en.startswith("It's")


def test_date_answer_localized():
    _freeze(datetime(2026, 7, 16, 9, 30, 0, tzinfo=timezone.utc))
    es = maybe_bypass("qué fecha es hoy", language="es")
    en = maybe_bypass("what date is it", language="en")
    assert es and es.startswith("Hoy es")
    assert en and "2026" in en


def test_fast_model_bypass_reports_config():
    ans = maybe_bypass("qué modelo fast activo hay", language="es")
    assert ans and "Modelo FAST activo" in ans
    ans_en = maybe_bypass("which fast model is active", language="en")
    assert ans_en and "Active FAST model" in ans_en


def test_lifecycle_bypass():
    ans = maybe_bypass("what is the current lifecycle state", language="en")
    assert ans and "lifecycle state" in ans.lower()


def test_vault_bypass_returns_none_when_backend_not_loaded():
    # No Chroma load forced on the interactive path -> falls through cleanly.
    assert maybe_bypass("is the knowledge vault empty", language="en") is None


def test_unknown_kind_answer_is_none():
    assert answer_bypass(BypassKind.NONE, language="es") is None


def test_non_bypass_returns_none():
    assert maybe_bypass("como saco la raiz cubica de algo", language="es") is None
    assert maybe_bypass("hola", language="es") is None


# ── chat_stream integration: a bypassed turn calls NO transport ───────────────
class _StubExecutor:
    authority = None


def test_chat_stream_bypass_calls_no_model(monkeypatch):
    def _boom_native(**kw):
        raise AssertionError("no native call on a deterministic bypass")

    monkeypatch.setattr("core.ollama_native.chat_stream", _boom_native)
    _freeze(datetime(2026, 7, 16, 9, 30, 0, tzinfo=timezone.utc))

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        # Guard the OpenAI path too: it must not be reached either.
        monkeypatch.setattr(
            llm.client, "with_options",
            lambda **kw: (_ for _ in ()).throw(
                AssertionError("no /v1 call on a deterministic bypass")),
        )
        out = await llm.chat("¿qué hora es?")
        assert "Son las" in out
        assert llm.history[-1]["role"] == "assistant"
        await llm.aclose()

    asyncio.run(_run())
