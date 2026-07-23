"""tests/test_response_wiring_v69_m572.py — V69 M57.1/.2 live wiring.

Proves the contract + budget actually reach the native transport on a real
``LLM.chat_stream`` turn:

  * a greeting is generated with the INSTANT token budget, not the flat 256 cap;
  * an explicit detail request raises the budget within the ceiling;
  * the live ``num_ctx`` equals the PREWARM ``num_ctx`` — the M56.4 invariant that
    ``_adaptive_ctx`` was silently breaking (a warmed-at-2048 runner served at 1024
    is a full reload, measured at 8 723 ms);
  * the contract's style directive reaches the system prompt and carries no
    reasoning/tool vocabulary;
  * hitting the token cap is reported truthfully instead of read as a completed
    answer;
  * only allowlisted sampling options are ever sent to the server.

No live Ollama.
"""
from __future__ import annotations

import asyncio

from core.ollama_native import (
    ChatChunk,
    NativeCapability,
    NativeProbeState,
    build_chat_request,
    set_native_capability,
)


def teardown_function(_):
    from core.fast_readiness import reset_fast_readiness
    from core.ollama_native import reset_native_capability
    from core.response_runtime import reset_response_runtime
    reset_fast_readiness(None)
    reset_native_capability()
    reset_response_runtime(None)


class _StubExecutor:
    authority = None

    async def aexecute(self, *a, **k):
        raise AssertionError("no tool should run on a DIRECT_FAST native turn")


def _install_native(monkeypatch, calls: dict, *, chunks=None, done_reason="stop",
                    eval_count=8):
    async def fake_native(**kw):
        calls["kw"] = kw
        for piece in (chunks if chunks is not None else ["Hola. ", "¿Qué necesitas?"]):
            yield ChatChunk(content=piece)
        yield ChatChunk(content="", done=True, done_reason=done_reason,
                        eval_count=eval_count, eval_duration=1_000_000_000)

    monkeypatch.setattr("core.ollama_native.chat_stream", fake_native)
    set_native_capability(NativeCapability(state=NativeProbeState.NATIVE_READY,
                                           model="qwen3:8b"))


def _turn(message: str, calls: dict) -> str:
    async def _run() -> str:
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        try:
            return await llm.chat(message)
        finally:
            await llm.aclose()

    return asyncio.run(_run())


# ── budget reaches the wire ───────────────────────────────────────────────────
def test_greeting_uses_the_instant_budget_not_the_flat_cap(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("hola", calls)
    assert calls["kw"]["max_tokens"] <= 64, "a greeting must not ask for 256 tokens"
    assert calls["kw"]["think"] is False


def test_detail_request_raises_the_budget_within_the_ceiling(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("explica Kerberos con mas detalle", calls)
    assert calls["kw"]["max_tokens"] >= 160
    assert calls["kw"]["max_tokens"] <= 512


def test_brevity_request_lowers_the_budget(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("explicame POO brevemente", calls)
    brief = calls["kw"]["max_tokens"]
    _install_native(monkeypatch, calls)
    _turn("explica Kerberos con mas detalle", calls)
    assert brief < calls["kw"]["max_tokens"]


# ── the M56.4 prewarm-parity invariant ────────────────────────────────────────
def test_live_num_ctx_matches_the_prewarm_context(monkeypatch):
    from core.fast_prewarm import resolve_fast_context
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("hola", calls)
    assert calls["kw"]["ctx"] == resolve_fast_context(), (
        "a short turn must not shrink num_ctx below the warmed runner's context — "
        "that is the 8.7s reload M56.4 measured and fixed"
    )


def test_num_ctx_is_stable_across_contracts(monkeypatch):
    # FAST-eligible prompts only: a CODER/DEEP turn deliberately leaves the native
    # path (core.fast_path), so it is not part of the prewarm-parity invariant.
    seen = set()
    for msg in ("hola", "explica Kerberos con mas detalle",
                "explicame POO brevemente"):
        calls: dict = {}
        _install_native(monkeypatch, calls)
        _turn(msg, calls)
        seen.add(calls["kw"]["ctx"])
    assert len(seen) == 1, "num_ctx must not vary per turn on the warmed FAST path"


# ── contract delta (V69 M58.3: replaces the M57 prose style tail) ─────────────
def test_contract_delta_reaches_the_system_prompt(monkeypatch):
    # M58: the FAST system prompt carries a COMPACT, machine-readable contract delta
    # instead of the M57 natural-language style paragraph. The stable prefix comes
    # first (byte-reusable) and the delta names the selected contract.
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("como saco la raiz cuadrada de algo", calls)
    system = calls["kw"]["messages"][0]
    assert system["role"] == "system"
    content = system["content"]
    assert "[RESPONSE_CONTRACT]" in content
    assert "contract=BRIEF" in content
    # The stable prefix precedes the delta so the reusable region is a byte-prefix.
    from core.prompt_manifest import stable_core_prefix
    assert content.startswith(stable_core_prefix().split("\n\n", 1)[0])
    assert content.index("local AI assistant") < content.index("[RESPONSE_CONTRACT]")


def test_contract_delta_cannot_grant_tools_or_authority(monkeypatch):
    # A delta is presentation only: no permission/tool/authority/scope vocabulary.
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("como saco la raiz cuadrada de algo", calls)
    content = calls["kw"]["messages"][0]["content"]
    lo = content.lower()
    delta = content[content.index("[RESPONSE_CONTRACT]"):]
    dl = delta.lower()
    for banned in ("authority", "scope", "tool", "permission", "risk", "nato"):
        assert banned not in dl, f"delta must not carry {banned!r}"
    assert "host clock" in lo  # the dynamic clock moved to the tail, after the delta
    assert lo.index("[response_contract]") < lo.index("host clock")


def test_system_prompt_never_leaks_reasoning_or_tool_vocabulary(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("explica Kerberos con mas detalle", calls)
    content = calls["kw"]["messages"][0]["content"]
    assert "<think>" not in content and "chain of thought" not in content.lower()
    assert "```json" not in content


# ── sampling options ──────────────────────────────────────────────────────────
def test_only_allowlisted_sampling_options_reach_the_wire(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls)
    _turn("hola", calls)
    extra = calls["kw"].get("options_extra") or {}
    assert set(extra) <= {"top_p", "repeat_penalty"}


def test_build_chat_request_rejects_unknown_options():
    body = build_chat_request(
        model="m", messages=[{"role": "user", "content": "x"}], think=False,
        max_tokens=32, temperature=0.2, ctx=2048,
        options_extra={"top_p": 0.9, "num_ctx": 512, "tools": ["evil"],
                       "mirostat": 2, "stop": ["x"]},
    )
    opts = body["options"]
    assert opts["num_ctx"] == 2048, "options_extra must never override num_ctx"
    assert opts["top_p"] == 0.9
    assert "tools" not in opts and "mirostat" not in opts and "stop" not in opts
    assert "tools" not in body


def test_build_chat_request_ignores_non_numeric_option_values():
    body = build_chat_request(
        model="m", messages=[], think=False, max_tokens=32, temperature=0.2,
        options_extra={"top_p": "0.9", "repeat_penalty": None},
    )
    assert "top_p" not in body["options"]
    assert "repeat_penalty" not in body["options"]


# ── truthful truncation ───────────────────────────────────────────────────────
def test_token_cap_truncation_is_reported_not_hidden(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls, chunks=["La raíz cúbica de x es x elevado a"],
                    done_reason="length", eval_count=40)

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        try:
            out = await llm.chat("como saco la raiz cubica de algo")
            assert "continúa" in out.lower() or "continue" in out.lower()
            assert llm.history[-1]["role"] == "assistant"
            assert "raíz cúbica" in llm.history[-1]["content"]
        finally:
            await llm.aclose()

    asyncio.run(_run())


def test_clean_stop_is_not_marked_as_truncated(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls, chunks=["Hola, ¿qué necesitas?"],
                    done_reason="stop", eval_count=6)

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        try:
            out = await llm.chat("hola")
            assert "acortada" not in out and "shortened" not in out
        finally:
            await llm.aclose()

    asyncio.run(_run())


# ── throughput feedback ───────────────────────────────────────────────────────
def test_completed_turn_feeds_the_throughput_estimate(monkeypatch):
    from core.response_runtime import get_response_runtime, reset_response_runtime
    reset_response_runtime(None)
    calls: dict = {}
    _install_native(monkeypatch, calls, eval_count=12)
    _turn("hola", calls)
    rr = get_response_runtime()
    assert rr.throughput.samples >= 1


def test_contract_disabled_falls_back_to_legacy_behaviour(monkeypatch):
    calls: dict = {}
    _install_native(monkeypatch, calls)
    monkeypatch.setattr("core.config.settings.response_contracts_enabled", False,
                        raising=False)
    _turn("hola", calls)
    # Legacy path: the flat operator cap, and _adaptive_ctx's shrunken context.
    from core.config import settings
    assert calls["kw"]["max_tokens"] == settings.fast_max_tokens
