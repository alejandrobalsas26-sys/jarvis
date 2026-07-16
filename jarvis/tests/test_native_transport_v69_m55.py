"""tests/test_native_transport_v69_m55.py — V69 M55.1/.2: native /api/chat transport.

Proves the bounded native Ollama transport WITHOUT a live server:

  * request serialization — think None/true/false, bounded num_predict, num_ctx,
    and NO tool schema on the fast path;
  * NDJSON parsing — content, done metadata, malformed-line skip, and reasoning
    text discarded (only a boolean ``thinking_present`` survives);
  * bounded/cancellable streaming — first-token / idle / total timeouts, no late
    chunks after cancellation, source always closed;
  * live streaming and honest failure via an injected ``httpx.MockTransport``;
  * the capability probe — NATIVE_READY only on a structurally valid no-think
    reply, NATIVE_DEGRADED when reasoning leaks, caching, and fallback states.

No live Ollama, no model pull.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core.ollama_native import (
    CancellationToken,
    NativeCapability,
    NativeProbeState,
    NativeTransportError,
    build_chat_request,
    chat_stream,
    get_native_capability,
    parse_ndjson_line,
    probe_native,
    refresh_native_capability,
    reset_native_capability,
    set_native_capability,
    stream_chat_chunks,
)
from core.turn_budget import StageTimeouts, TurnBudget, TurnTimeout


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeLineSource:
    """A fake async NDJSON line iterator. Records closure and late emission."""

    def __init__(self, lines, *, stall_after=None, stall_forever=False):
        self.lines = list(lines)
        self.stall_after = stall_after
        self.stall_forever = stall_forever
        self.closed = False
        self.emitted = 0
        self.emitted_after_close = 0
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.stall_forever or (self.stall_after is not None
                                  and self._i >= self.stall_after):
            await asyncio.sleep(3600)
        if self._i >= len(self.lines):
            raise StopAsyncIteration
        line = self.lines[self._i]
        self._i += 1
        if self.closed:
            self.emitted_after_close += 1
        self.emitted += 1
        return line

    async def aclose(self):
        self.closed = True


def _line(content="", done=False, thinking=None, **meta):
    obj = {"model": "qwen3:8b", "created_at": "2026-07-16T00:00:00Z",
           "message": {"role": "assistant", "content": content}}
    if thinking is not None:
        obj["message"]["thinking"] = thinking
    obj["done"] = done
    obj.update(meta)
    return json.dumps(obj)


# ── M55.1 request serialization ───────────────────────────────────────────────
def test_build_request_think_false_and_bounds():
    body = build_chat_request(
        model="qwen3:8b", messages=[{"role": "user", "content": "hola"}],
        think=False, max_tokens=160, temperature=0.3, ctx=2048, keep_alive="10m",
    )
    assert body["think"] is False
    assert body["options"]["num_predict"] == 160
    assert body["options"]["num_ctx"] == 2048
    assert body["options"]["temperature"] == 0.3
    assert body["keep_alive"] == "10m"
    assert body["stream"] is True
    # criterion 7 — a DIRECT_FAST request must carry NO tool schema.
    assert "tools" not in body


def test_build_request_think_variants():
    omitted = build_chat_request(model="m", messages=[], think=None,
                                 max_tokens=8, temperature=0.0)
    assert "think" not in omitted            # None => field omitted entirely
    on = build_chat_request(model="m", messages=[], think=True,
                            max_tokens=8, temperature=0.0)
    assert on["think"] is True
    off = build_chat_request(model="m", messages=[], think=False,
                             max_tokens=8, temperature=0.0)
    assert off["think"] is False


# ── M55.1 NDJSON parsing ──────────────────────────────────────────────────────
def test_parse_content_line():
    ch = parse_ndjson_line(_line(content="Hola"))
    assert ch is not None
    assert ch.content == "Hola"
    assert ch.done is False
    assert ch.thinking_present is False


def test_parse_done_line_carries_metadata():
    ch = parse_ndjson_line(_line(
        content="", done=True, done_reason="stop", eval_count=13,
        eval_duration=2_000_000_000, prompt_eval_count=9,
        load_duration=500_000_000, total_duration=3_000_000_000,
    ))
    assert ch.done is True
    assert ch.done_reason == "stop"
    assert ch.eval_count == 13
    assert ch.tokens_per_second() == 6.5
    assert ch.load_seconds() == 0.5


def test_parse_thinking_is_reduced_to_boolean_no_text_leak():
    ch = parse_ndjson_line(_line(content="", thinking="secret chain of thought"))
    assert ch is not None
    assert ch.thinking_present is True
    # ChatChunk exposes NO field holding the reasoning text.
    assert "secret" not in json.dumps(ch.__dict__)
    assert not hasattr(ch, "thinking")


def test_parse_malformed_and_blank_lines_are_skipped():
    assert parse_ndjson_line("") is None
    assert parse_ndjson_line("   ") is None
    assert parse_ndjson_line("{not json") is None
    assert parse_ndjson_line("[1,2,3]") is None       # not an object


# ── M55.1 bounded / cancellable streaming ─────────────────────────────────────
def _stream(src, *, budget, timeouts, cancellation=None):
    return stream_chat_chunks(src, budget=budget, timeouts=timeouts,
                              cancellation=cancellation)


def test_stream_yields_content_and_stops_on_done():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        src = FakeLineSource([_line(content="Hola "), _line(content="mundo"),
                              _line(content="", done=True, done_reason="stop")])
        t = StageTimeouts(first_token_s=1.0, idle_s=1.0, total_s=60.0)
        out = [c.content async for c in _stream(src, budget=budget, timeouts=t)]
        assert "".join(out) == "Hola mundo"
        await asyncio.sleep(0)
        assert src.closed is True
    asyncio.run(_run())


def test_stream_first_token_timeout():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        src = FakeLineSource(["never"], stall_forever=True)
        t = StageTimeouts(first_token_s=0.05, idle_s=0.05, total_s=60.0)
        got = []
        with pytest.raises(TurnTimeout) as exc:
            async for c in _stream(src, budget=budget, timeouts=t):
                got.append(c)
        assert exc.value.stage == "first_token"
        assert got == []
        await asyncio.sleep(0)
        assert src.closed is True
    asyncio.run(_run())


def test_stream_idle_timeout_after_first_content():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        src = FakeLineSource([_line(content="Hola"), "stall"], stall_after=1)
        t = StageTimeouts(first_token_s=1.0, idle_s=0.05, total_s=60.0)
        got = []
        with pytest.raises(TurnTimeout) as exc:
            async for c in _stream(src, budget=budget, timeouts=t):
                got.append(c.content)
        assert exc.value.stage == "stream_idle"
        assert got == ["Hola"]
    asyncio.run(_run())


def test_stream_total_deadline():
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=10.0, clock=clk)
        clk.advance(10.0)  # already expired
        src = FakeLineSource([_line(content="x")])
        t = StageTimeouts(first_token_s=999.0, idle_s=999.0, total_s=10.0)
        with pytest.raises(TurnTimeout) as exc:
            async for _c in _stream(src, budget=budget, timeouts=t):
                raise AssertionError("no chunk after total expiry")
        assert exc.value.stage == "total"
    asyncio.run(_run())


def test_stream_cancellation_stops_with_no_late_chunks():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        src = FakeLineSource([_line(content="a"), _line(content="b"),
                              _line(content="c")])
        t = StageTimeouts(first_token_s=1.0, idle_s=1.0, total_s=60.0)
        token = CancellationToken()
        got = []
        async for c in _stream(src, budget=budget, timeouts=t, cancellation=token):
            got.append(c.content)
            token.cancel()   # cancel after the first chunk
        assert got == ["a"]
        await asyncio.sleep(0.02)
        assert src.emitted_after_close == 0
        assert src.closed is True
    asyncio.run(_run())


def test_stream_server_disconnect_is_clean():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        # No done line — the source simply ends (server closed the connection).
        src = FakeLineSource([_line(content="partial")])
        t = StageTimeouts(first_token_s=1.0, idle_s=1.0, total_s=60.0)
        out = [c.content async for c in _stream(src, budget=budget, timeouts=t)]
        assert out == ["partial"]
        assert src.closed is True
    asyncio.run(_run())


def test_stream_skips_malformed_lines_midstream():
    async def _run():
        budget = TurnBudget(total_s=60.0, clock=FakeClock())
        src = FakeLineSource([_line(content="ok"), "{bad json",
                              _line(content="!", done=True)])
        t = StageTimeouts(first_token_s=1.0, idle_s=1.0, total_s=60.0)
        out = [c.content async for c in _stream(src, budget=budget, timeouts=t)]
        assert "".join(out) == "ok!"
    asyncio.run(_run())


# ── M55.1 live streaming + honest failure via injected transport ──────────────
def _ndjson_response(lines: list[str]) -> httpx.Response:
    body = ("\n".join(lines) + "\n").encode("utf-8")
    return httpx.Response(200, content=body,
                          headers={"content-type": "application/x-ndjson"})


def test_chat_stream_live_over_mock_transport():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return _ndjson_response([
            _line(content="Hola "), _line(content="Alejandro"),
            _line(content="", done=True, done_reason="stop", eval_count=5,
                  eval_duration=1_000_000_000),
        ])

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        budget = TurnBudget(total_s=60.0)
        t = StageTimeouts(first_token_s=5.0, idle_s=5.0, total_s=60.0)
        out = []
        async for c in chat_stream(model="qwen3:8b",
                                   messages=[{"role": "user", "content": "hola"}],
                                   think=False, max_tokens=64, temperature=0.2,
                                   budget=budget, timeouts=t, ctx=1024,
                                   keep_alive="10m", client=client):
            out.append(c)
        await client.aclose()
        assert "".join(c.content for c in out) == "Hola Alejandro"
        assert out[-1].done is True and out[-1].eval_count == 5
        # request went to the NATIVE endpoint, think=false, and NO tools.
        assert captured["url"].endswith("/api/chat")
        assert captured["body"]["think"] is False
        assert "tools" not in captured["body"]
        assert captured["body"]["options"]["num_predict"] == 64
    asyncio.run(_run())


def test_chat_stream_status_error_raises_native_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        budget = TurnBudget(total_s=60.0)
        with pytest.raises(NativeTransportError) as exc:
            async for _c in chat_stream(model="missing", messages=[],
                                        think=False, max_tokens=8, temperature=0.0,
                                        budget=budget, client=client):
                pass
        assert exc.value.kind == "status"
        assert "404" in exc.value.reason
        await client.aclose()
    asyncio.run(_run())


def test_chat_stream_connect_error_raises_native_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        budget = TurnBudget(total_s=60.0)
        with pytest.raises(NativeTransportError) as exc:
            async for _c in chat_stream(model="qwen3:8b", messages=[],
                                        think=False, max_tokens=8, temperature=0.0,
                                        budget=budget, client=client):
                pass
        assert exc.value.kind == "connect"
        await client.aclose()
    asyncio.run(_run())


# ── M55.2 capability probe ────────────────────────────────────────────────────
def _probe_client(chat_lines, *, version="0.32.0", ps_models=("qwen3:8b",),
                  chat_status=200, v1_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/version":
            return httpx.Response(200, json={"version": version})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": m} for m in ps_models]})
        if p == "/api/chat":
            if chat_status != 200:
                return httpx.Response(chat_status, json={"error": "no"})
            return _ndjson_response(chat_lines)
        if p == "/v1/models":
            return httpx.Response(v1_status, json={"data": []})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_probe_native_ready_on_valid_no_think_stream():
    async def _run():
        client = _probe_client([
            _line(content="Hello"),
            _line(content="", done=True, done_reason="stop", eval_count=3,
                  eval_duration=1_000_000_000),
        ])
        cap = await probe_native(model="qwen3:8b", client=client)
        await client.aclose()
        assert cap.state is NativeProbeState.NATIVE_READY
        assert cap.think_false_supported is True
        assert cap.reasoning_omitted is True
        assert cap.server_version == "0.32.0"
        assert "qwen3:8b" in cap.active_models
        assert "eval_count" in cap.metadata_fields
    asyncio.run(_run())


def test_probe_native_degraded_when_reasoning_leaks():
    async def _run():
        client = _probe_client([
            _line(content="", thinking="thinking hard"),
            _line(content="Hello"),
            _line(content="", done=True, done_reason="stop"),
        ])
        cap = await probe_native(model="qwen3:8b", client=client)
        await client.aclose()
        assert cap.state is NativeProbeState.NATIVE_DEGRADED
        assert cap.reasoning_omitted is False
        assert cap.think_false_supported is False
    asyncio.run(_run())


def test_probe_openai_fallback_when_native_chat_down_but_v1_up():
    async def _run():
        client = _probe_client([], chat_status=500, v1_status=200)
        cap = await probe_native(model="qwen3:8b", client=client)
        await client.aclose()
        assert cap.state is NativeProbeState.OPENAI_FALLBACK
    asyncio.run(_run())


def test_probe_unavailable_when_everything_down():
    async def _run():
        client = _probe_client([], chat_status=500, v1_status=500)
        cap = await probe_native(model="qwen3:8b", client=client)
        await client.aclose()
        assert cap.state is NativeProbeState.UNAVAILABLE
    asyncio.run(_run())


def test_capability_cache_default_and_set_and_reset():
    reset_native_capability()
    assert get_native_capability().state is NativeProbeState.UNKNOWN
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))
    assert get_native_capability().state is NativeProbeState.NATIVE_READY
    reset_native_capability()
    assert get_native_capability().state is NativeProbeState.UNKNOWN


def test_refresh_native_capability_caches_result():
    async def _run():
        reset_native_capability()
        client = _probe_client([
            _line(content="Hi"),
            _line(content="", done=True, done_reason="stop"),
        ])
        cap = await refresh_native_capability(model="qwen3:8b", client=client)
        await client.aclose()
        assert cap.state is NativeProbeState.NATIVE_READY
        assert get_native_capability().state is NativeProbeState.NATIVE_READY
    asyncio.run(_run())


def test_no_automatic_model_pull_in_module():
    """Criterion 16/17 — the transport must never pull or swap a model."""
    import inspect

    import core.ollama_native as mod
    src = inspect.getsource(mod)
    assert "/api/pull" not in src
    assert "ollama pull" not in src
    assert "subprocess" not in src
