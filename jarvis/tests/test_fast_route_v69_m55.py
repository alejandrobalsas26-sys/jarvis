"""tests/test_fast_route_v69_m55.py — V69 M55.3/.4/.5/.7: FAST transport policy.

Proves the deterministic transport-selection policy and the live wiring:

  * ``decide_fast_route`` routes ONLY suitable DIRECT_FAST turns onto the native
    no-think path and keeps DEEP/coder/cyber/effectful/verifier turns on the
    existing OpenAI-compatible loop;
  * operator config (fast_transport / fast_think / fast_max_tokens / …) is honored
    within clamped bounds and never silently changes the model;
  * an end-to-end ``chat_stream`` turn takes the native path, streams content,
    appends the assistant turn, and never sends tools;
  * a native transport failure before first content falls back cleanly.

No live Ollama.
"""
from __future__ import annotations

import asyncio

from core.config import Settings
from core.fast_path import FastReason, decide_fast_route
from core.model_router import ModelDecision, ModelRole
from core.turn_policy import classify_request


def teardown_function(_):
    # These tests drive the REAL fast path, which mutates the process-global
    # FastReadiness / native-capability singletons. Reset them so counters do not
    # leak into other test files (e.g. runtime-health baselines read them live).
    from core.fast_readiness import reset_fast_readiness
    from core.ollama_native import reset_native_capability
    reset_fast_readiness(None)
    reset_native_capability()


def _md(role: ModelRole) -> ModelDecision:
    return ModelDecision(role=role, provider="ollama", model="m", complexity=0.1,
                         reason="t", requires_verification=False)


def _settings(**over) -> Settings:
    base = dict(fast_transport="auto", fast_think="off", fast_max_tokens=256,
                fast_context=2048, fast_keep_alive="10m", fast_model="")
    base.update(over)
    return Settings(**base)


# ── M55.3 route selection ─────────────────────────────────────────────────────
def test_greeting_takes_native_fast():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_READY",
                          settings=_settings())
    assert d.use_native is True
    assert d.reason is FastReason.NATIVE_FAST_NO_THINK
    assert d.think is False
    assert d.model == "qwen3:8b"
    assert d.max_tokens == 256


def test_educational_howto_takes_native_fast():
    tp = classify_request("como saco la raiz cubica de algo")
    assert tp.reason_code.value == "DIRECT_FAST"
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="UNKNOWN",
                          settings=_settings())
    # UNKNOWN is optimistic under auto — native is tried, fallback on error.
    assert d.use_native is True


def test_deep_role_stays_on_openai():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.DEEP),
                          routed_model="qwen3:14b", native_state="NATIVE_READY",
                          settings=_settings())
    assert d.use_native is False
    assert d.reason is FastReason.DEEP_REASONING


def test_effectful_turn_stays_on_openai():
    tp = classify_request("abre el bloc de notas")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_READY",
                          settings=_settings())
    assert d.use_native is False
    assert d.reason is FastReason.OPENAI_TOOL_CHAT


def test_cyber_sensitive_stays_on_openai():
    tp = classify_request("explica el exploit de buffer overflow y el payload")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_READY",
                          settings=_settings())
    assert d.use_native is False
    assert d.reason is FastReason.OPENAI_TOOL_CHAT


def test_private_document_stays_on_openai():
    tp = classify_request("busca en mis pdf que dice sobre TCP")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_READY",
                          settings=_settings())
    assert d.use_native is False  # PRIVATE_RAG needs the vault tool on /v1


def test_forced_openai_never_uses_native():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_READY",
                          settings=_settings(fast_transport="openai"))
    assert d.use_native is False
    assert d.reason is FastReason.OPENAI_FORCED


def test_auto_declines_native_when_unavailable():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="UNAVAILABLE",
                          settings=_settings())
    assert d.use_native is False
    assert d.reason is FastReason.NATIVE_UNAVAILABLE_FALLBACK


def test_auto_declines_native_when_degraded():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_DEGRADED",
                          settings=_settings())
    assert d.use_native is False


def test_forced_native_still_refuses_degraded_server():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_DEGRADED",
                          settings=_settings(fast_transport="native"))
    assert d.use_native is False
    assert d.reason is FastReason.NATIVE_UNAVAILABLE_FALLBACK


def test_fast_model_override_used_for_native():
    tp = classify_request("hola")
    d = decide_fast_route(turn_policy=tp, model_decision=_md(ModelRole.FAST),
                          routed_model="qwen3:8b", native_state="NATIVE_READY",
                          settings=_settings(fast_model="llama3.1:8b"))
    assert d.model == "llama3.1:8b"    # distinct non-reasoning override, no fork


# ── M55.7 config bounds ───────────────────────────────────────────────────────
def test_fast_config_clamps_and_defaults():
    s = _settings(fast_max_tokens=999999, fast_context=1, fast_transport="weird",
                  fast_think="maybe")
    assert s.fast_max_tokens == 2048       # clamped
    assert s.fast_context == 512           # clamped up
    assert s.fast_transport == "auto"      # invalid -> auto
    assert s.fast_think == "off"           # invalid -> off
    assert s.fast_think_value() is False


def test_fast_think_tristate():
    assert _settings(fast_think="off").fast_think_value() is False
    assert _settings(fast_think="on").fast_think_value() is True
    assert _settings(fast_think="omit").fast_think_value() is None


# ── M55.3 live wiring (chat_stream takes the native path) ─────────────────────
class _StubExecutor:
    authority = None

    async def aexecute(self, *a, **k):  # never called on the native fast path
        raise AssertionError("no tool should run on a DIRECT_FAST native turn")


def test_chat_stream_takes_native_path(monkeypatch):
    from core.ollama_native import (
        ChatChunk,
        NativeCapability,
        NativeProbeState,
        set_native_capability,
    )

    calls = {}

    async def fake_native(**kw):
        calls["kw"] = kw
        yield ChatChunk(content="¡Hola! ")
        yield ChatChunk(content="¿En qué puedo ayudarte?")
        yield ChatChunk(content="", done=True, done_reason="stop", eval_count=8,
                        eval_duration=1_000_000_000)

    monkeypatch.setattr("core.ollama_native.chat_stream", fake_native)
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        out = await llm.chat("hola")
        assert "Hola" in out
        # assistant turn recorded; native path never offered tools.
        assert llm.history[-1]["role"] == "assistant"
        assert "tools" not in calls["kw"]
        assert calls["kw"]["think"] is False
        await llm.aclose()

    asyncio.run(_run())


def test_native_failure_falls_back(monkeypatch):
    """A native transport error BEFORE first content falls through to the existing
    path — proven by the OpenAI client being invoked as the fallback."""
    from core.ollama_native import (
        NativeCapability,
        NativeProbeState,
        NativeTransportError,
        set_native_capability,
    )

    async def failing_native(**kw):
        raise NativeTransportError("connect_failed", kind="connect")
        yield  # pragma: no cover — make it an async generator

    fell_back = {"v": False}

    async def fake_v1_stream(*a, **k):
        fell_back["v"] = True

        class _Stream:
            async def __aiter__(self):
                return
                yield  # pragma: no cover
        return _Stream()

    monkeypatch.setattr("core.ollama_native.chat_stream", failing_native)
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        # Patch the OpenAI-compatible create() so the fallback does not hit network.
        monkeypatch.setattr(
            llm.client,
            "with_options",
            lambda **kw: _FakeOpenAI(fell_back),
        )
        await llm.chat("hola")
        assert fell_back["v"] is True, "native failure must fall back to /v1"
        await llm.aclose()

    asyncio.run(_run())


def test_both_transports_unavailable_returns_localized_error(monkeypatch):
    """M55.12 — native declined (UNAVAILABLE) AND /v1 cannot connect: both
    transports have failed, so the turn yields a concise localized error and
    returns prompt control — never a raw connection trace."""
    import httpx
    from openai import APIConnectionError

    from core.llm import _FAST_UNREACHABLE_ES
    from core.ollama_native import (
        NativeCapability,
        NativeProbeState,
        set_native_capability,
    )

    # Native is unavailable -> decide_fast_route never even tries it.
    set_native_capability(NativeCapability(state=NativeProbeState.UNAVAILABLE,
                                           model="qwen3:8b"))

    class _ConnDown:
        def chat(self):  # pragma: no cover
            return self

        @property
        def completions(self):
            return self

        async def create(self, **kw):
            raise APIConnectionError(
                request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"))

    _cd = _ConnDown()
    _cd.chat = _cd  # type: ignore[assignment]

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        monkeypatch.setattr(llm.client, "with_options", lambda **kw: _cd)
        out = await llm.chat("hola")
        assert out.strip() == _FAST_UNREACHABLE_ES.strip()
        await llm.aclose()

    asyncio.run(_run())


def test_fast_timeout_pairs_user_with_assistant_no_dangling(monkeypatch):
    """M55.15 — a timed-out fast turn must pair the user message with an assistant
    turn, so the NEXT turn's model can't answer the previous (unanswered) question
    (the live 'hola replied about TCP' bug)."""
    from core.ollama_native import (
        ChatChunk,
        NativeCapability,
        NativeProbeState,
        set_native_capability,
    )
    from core.turn_budget import TurnTimeout

    async def timing_out_native(**kw):
        yield ChatChunk(content="Empez")     # partial content, then a stall
        raise TurnTimeout("total", 1.0)

    monkeypatch.setattr("core.ollama_native.chat_stream", timing_out_native)
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        llm.history = []
        await llm.chat("hola")
        assert llm.history[-1]["role"] == "assistant"   # paired, not dangling
        assert llm.history[-2]["role"] == "user"
        users = [m for m in llm.history if m["role"] == "user"]
        assert len(users) == 1 and users[0]["content"] == "hola"
        await llm.aclose()

    asyncio.run(_run())


def test_fast_cancel_without_content_drops_dangling_user(monkeypatch):
    """A cancelled fast turn that produced no content must not leave a dangling
    user message behind."""
    from core.ollama_native import (
        NativeCapability,
        NativeProbeState,
        set_native_capability,
    )

    async def cancelling_native(**kw):
        raise asyncio.CancelledError()
        yield  # pragma: no cover — make it an async generator

    monkeypatch.setattr("core.ollama_native.chat_stream", cancelling_native)
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        llm.history = []
        await llm.chat("hola")
        assert not any(m["role"] == "user" for m in llm.history), \
            "the unanswered user turn must be dropped, not left dangling"
        await llm.aclose()

    asyncio.run(_run())


class _FakeOpenAI:
    def __init__(self, flag):
        self._flag = flag

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kw):
        self._flag["v"] = True

        class _S:
            def __aiter__(self_inner):
                return self_inner

            async def __anext__(self_inner):
                raise StopAsyncIteration
        return _S()
