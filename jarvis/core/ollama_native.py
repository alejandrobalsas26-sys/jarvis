"""core/ollama_native.py — V69 M55: bounded native Ollama /api/chat transport.

WHY THIS EXISTS
---------------
The live FAST role runs qwen3:8b, a *reasoning* model. Through the current
OpenAI-compatible ``/v1`` shim (``core.llm.LLM.chat_stream``) there is no way to
turn reasoning off: the shim silently drops the ``<think>`` block from the content
but STILL pays the full hidden-reasoning latency. M54.1 measured this on the target
host (AMD Ryzen 5 7430U, 15 W, CPU-only Ollama):

    /v1  "hola"                    first content token ~29 s (hidden reasoning)
    native /api/chat think=true    first content token ~19 s (thinking field 339 chars)
    native /api/chat think=false   first content token ~1.3 s warm / ~15 s cold, NO thinking

So the ONLY way to make an ordinary interactive turn answer promptly is the native
endpoint's ``think`` control — proven at the wire, not a "/no_think" prompt phrase.

WHAT THIS MODULE IS
-------------------
A small, transport-neutral async client for ``POST /api/chat`` built on the async
HTTP client already installed (``httpx``); the ``ollama`` python package is NOT a
dependency and no second inference framework is added. It:

  * serializes the ``think`` field explicitly (None = omit, True, False);
  * streams NDJSON and normalizes each line into a :class:`ChatChunk` carrying only
    SAFE fields — never raw NDJSON, never an httpx object, and never the model's
    chain-of-thought (a ``thinking`` field is reduced to the boolean
    ``thinking_present`` and its text is discarded);
  * enforces connect / first-token / idle / total bounds via the same
    :class:`~core.turn_budget.TurnBudget` + :class:`~core.turn_budget.StageTimeouts`
    contract the rest of the runtime uses, so the outer turn deadline is preserved;
  * is cancellable (a :class:`CancellationToken`) and always closes the live HTTP
    response, so no late chunk and no orphan inference survive a cancelled turn.

It never shells out to the ``ollama`` CLI, never pulls a model, and never restarts
the server. Everything here is pure/bounded and unit-testable without a live server
(the NDJSON parser and the bounded/cancellable stepper take an injected async line
iterator; live use feeds them ``httpx``'s ``aiter_lines()``).
"""
from __future__ import annotations

import asyncio
import json
import time as _time
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Callable

import httpx
from loguru import logger

from core.turn_budget import StageTimeouts, TurnBudget, TurnTimeout

# ── Native endpoints (loopback Ollama). Host is resolved through the router's
# normalizer so a bare OLLAMA_HOST still yields a valid URL. ───────────────────
_NATIVE_CHAT_PATH = "/api/chat"
_VERSION_PATH = "/api/version"
_PS_PATH = "/api/ps"


def default_base_url() -> str:
    """The normalized Ollama base URL (``scheme://host:port``)."""
    try:
        from core.model_router import normalize_ollama_host
        return normalize_ollama_host()
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:11434"


class NativeTransportError(Exception):
    """A native transport failure the caller may fall back on. Carries only a
    SAFE, already-sanitized reason — never a raw connection trace to the user."""

    def __init__(self, reason: str, *, kind: str = "transport") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


# ── Cancellation ──────────────────────────────────────────────────────────────
class CancellationToken:
    """A cooperative cancel signal decoupled from any specific event source.

    Wraps an optional :class:`asyncio.Event` so the live path can bridge the
    existing ``core.cancel_bus.llm_stream_cancel`` into the transport, while tests
    drive it directly. Checking ``cancelled`` is O(1) and never blocks.
    """

    __slots__ = ("_event", "_flag")

    def __init__(self, event: "asyncio.Event | None" = None) -> None:
        self._event = event
        self._flag = False

    @classmethod
    def from_event(cls, event: "asyncio.Event | None") -> "CancellationToken":
        return cls(event)

    @property
    def cancelled(self) -> bool:
        if self._flag:
            return True
        return self._event is not None and self._event.is_set()

    def cancel(self) -> None:
        self._flag = True
        if self._event is not None and not self._event.is_set():
            self._event.set()


# ── Normalized, transport-neutral streamed chunk ──────────────────────────────
@dataclass(frozen=True)
class ChatChunk:
    """One normalized streamed step. SAFE fields only — no raw NDJSON, no httpx
    object, and no chain-of-thought (``thinking_present`` is a boolean; the model's
    reasoning text is discarded at parse time and never stored or surfaced)."""

    content: str = ""
    done: bool = False
    thinking_present: bool = False
    model: str | None = None
    created_at: str | None = None
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_duration: int | None = None
    eval_duration: int | None = None

    def tokens_per_second(self) -> float | None:
        """Generation throughput from the final chunk's metadata (ns durations)."""
        if self.eval_count and self.eval_duration:
            return round(self.eval_count / (self.eval_duration / 1e9), 2)
        return None

    def load_seconds(self) -> float | None:
        if self.load_duration is None:
            return None
        return round(self.load_duration / 1e9, 3)


_META_KEYS = (
    "done_reason", "prompt_eval_count", "eval_count", "total_duration",
    "load_duration", "prompt_eval_duration", "eval_duration",
)

# V69 M57.2 — the ONLY per-turn sampling options a response contract may set.
# Deliberately excludes num_ctx (owned by the prewarm-parity invariant), num_predict
# and temperature (explicit arguments), and everything that could change what the
# server does structurally (format, tools, keep_alive, mirostat, stop).
_ALLOWED_EXTRA_OPTIONS: frozenset[str] = frozenset({"top_p", "repeat_penalty"})


def build_chat_request(
    *,
    model: str,
    messages: list[dict],
    think: bool | None,
    max_tokens: int,
    temperature: float,
    ctx: int | None = None,
    keep_alive: str | None = None,
    stream: bool = True,
    options_extra: dict | None = None,
) -> dict:
    """Build the native ``/api/chat`` request body (pure).

    ``think``: ``None`` omits the field entirely (server/model default), ``True``
    requests reasoning, ``False`` disables it. Tests assert all three shapes.
    ``max_tokens`` maps to ``options.num_predict`` — the bound that keeps a simple
    turn from running to a multi-minute cap at ~5 tok/s. NO ``tools`` key is ever
    added here: the native FAST path is deliberately tool-free (M55.3/criterion 7).

    ``options_extra`` (V69 M57.2) carries the response contract's remaining sampling
    knobs (``top_p`` / ``repeat_penalty``). It is an ALLOWLIST, not a passthrough:
    only keys in :data:`_ALLOWED_EXTRA_OPTIONS` survive, so a caller can never inject
    an arbitrary server option, a tools key, or a second num_ctx that would silently
    reload the runner the prewarm warmed.
    """
    options: dict = {"num_predict": int(max_tokens), "temperature": float(temperature)}
    if ctx is not None:
        options["num_ctx"] = int(ctx)
    for key, value in (options_extra or {}).items():
        if key in _ALLOWED_EXTRA_OPTIONS and isinstance(value, (int, float)):
            options[key] = float(value)
    body: dict = {
        "model": model,
        "messages": list(messages),
        "stream": bool(stream),
        "options": options,
    }
    if think is not None:
        body["think"] = bool(think)
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    return body


def parse_ndjson_line(line: str) -> ChatChunk | None:
    """Parse ONE NDJSON line into a :class:`ChatChunk`, or ``None`` for a blank or
    malformed line (a malformed line is skipped, never fatal). The ``thinking``
    field is reduced to ``thinking_present`` and its text discarded here — chain of
    thought never leaves this function."""
    if not line or not line.strip():
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}
    content = msg.get("content") or ""
    thinking = msg.get("thinking")
    meta = {k: obj.get(k) for k in _META_KEYS if obj.get(k) is not None}
    return ChatChunk(
        content=content if isinstance(content, str) else "",
        done=bool(obj.get("done")),
        thinking_present=bool(thinking),
        model=obj.get("model"),
        created_at=obj.get("created_at"),
        **meta,
    )


async def _aclose_lines(line_aiter) -> None:
    """Close an async line iterator if it supports it, bounded and never raising."""
    close = getattr(line_aiter, "aclose", None)
    if close is None:
        return
    try:
        res = close()
        if asyncio.iscoroutine(res):
            await asyncio.wait_for(res, timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


async def stream_chat_chunks(
    line_aiter,
    *,
    budget: TurnBudget,
    timeouts: StageTimeouts,
    cancellation: CancellationToken | None = None,
) -> AsyncIterator[ChatChunk]:
    """Yield :class:`ChatChunk`s from an async NDJSON *line iterator* under real
    first-token / idle / total bounds and cooperative cancellation.

    Bound attribution matches the rest of the runtime (M54.1): the wait BEFORE the
    first CONTENT token is ``first_token`` (covers Ollama's cold model swap — the
    wait that hung the live run); the gap between CONTENT tokens after that is
    ``stream_idle``; ``total`` is the outer ceiling. Each ``__anext__`` is awaited
    inside ``wait_for`` so a stall INSIDE the source is interrupted, not merely
    measured, and the source is always closed in ``finally`` — no late chunk and no
    orphan inference survive.
    """
    t = timeouts.clamped()
    it = line_aiter.__aiter__() if hasattr(line_aiter, "__aiter__") else line_aiter
    got_content = False
    try:
        while True:
            if cancellation is not None and cancellation.cancelled:
                return
            remaining = budget.remaining_s()
            if remaining <= 0.0:
                raise TurnTimeout("total", t.total_s)
            limit = t.idle_s if got_content else t.first_token_s
            wait = min(limit, remaining)
            try:
                line = await asyncio.wait_for(it.__anext__(), timeout=wait)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                if budget.remaining_s() <= 0.0:
                    raise TurnTimeout("total", t.total_s) from None
                raise TurnTimeout("first_token" if not got_content else "stream_idle",
                                  wait) from None
            chunk = parse_ndjson_line(line)
            if chunk is None:
                continue
            if chunk.content and not got_content:
                # Honest attribution: first CONTENT token time contains queue wait +
                # model swap + connect; Ollama does not split them, so record only
                # the observable quantity and leave model_load unknown.
                budget.record("first_token", budget.elapsed_s())
                got_content = True
            yield chunk
            if chunk.done:
                return
    finally:
        await _aclose_lines(it)


async def chat_stream(
    *,
    model: str,
    messages: list[dict],
    think: bool | None,
    max_tokens: int,
    temperature: float,
    budget: TurnBudget,
    timeouts: StageTimeouts | None = None,
    cancellation: CancellationToken | None = None,
    ctx: int | None = None,
    keep_alive: str | None = None,
    client: httpx.AsyncClient | None = None,
    base_url: str | None = None,
    options_extra: dict | None = None,
) -> AsyncIterator[ChatChunk]:
    """Live bounded stream from native ``POST /api/chat``.

    Opens (or reuses) an ``httpx`` client, streams NDJSON, and delegates parsing +
    bounds + cancellation to :func:`stream_chat_chunks`. The HTTP response is closed
    on EVERY exit path (success, timeout, cancellation, generator close) by the
    ``async with``. Raises :class:`NativeTransportError` on a connection/protocol/
    status failure so the caller can fall back to the OpenAI-compatible path with a
    clean, sanitized reason — raw traces never reach the agent runtime or the user.
    """
    t = (timeouts or StageTimeouts(total_s=budget.total_s)).clamped()
    body = build_chat_request(
        model=model, messages=messages, think=think, max_tokens=max_tokens,
        temperature=temperature, ctx=ctx, keep_alive=keep_alive, stream=True,
        options_extra=options_extra,
    )
    url = (base_url or default_base_url()).rstrip("/") + _NATIVE_CHAT_PATH
    # httpx read backstop is set ABOVE our own wait_for bounds so OUR bounds always
    # fire first and attribute the stage; connect is the one bound httpx owns.
    read_backstop = max(t.first_token_s, t.idle_s, t.total_s) + 5.0
    http_timeout = httpx.Timeout(connect=t.connect_s, read=read_backstop,
                                 write=t.connect_s, pool=t.connect_s)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=http_timeout)
    try:
        try:
            cm = client.stream("POST", url, json=body, timeout=http_timeout)
            resp = await cm.__aenter__()
        except httpx.ConnectError as exc:
            raise NativeTransportError(f"connect_failed:{type(exc).__name__}",
                                       kind="connect") from exc
        except httpx.ConnectTimeout as exc:
            raise NativeTransportError("connect_timeout", kind="connect") from exc
        except httpx.HTTPError as exc:
            raise NativeTransportError(f"http_error:{type(exc).__name__}",
                                       kind="transport") from exc
        try:
            if resp.status_code >= 400:
                raise NativeTransportError(f"status_{resp.status_code}", kind="status")
            async for chunk in stream_chat_chunks(
                resp.aiter_lines(), budget=budget, timeouts=t,
                cancellation=cancellation,
            ):
                yield chunk
        except (httpx.HTTPError,) as exc:
            raise NativeTransportError(f"stream_error:{type(exc).__name__}",
                                       kind="transport") from exc
        finally:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  M55.2 — Native transport capability probe (bounded, cached, truthful)
# ══════════════════════════════════════════════════════════════════════════════
class NativeProbeState(str, Enum):
    UNKNOWN = "UNKNOWN"                    # never probed
    PROBING = "PROBING"                    # a probe is in flight
    NATIVE_READY = "NATIVE_READY"          # streamed a structurally-valid no-think reply
    NATIVE_DEGRADED = "NATIVE_DEGRADED"    # native reachable but think=false NOT honored
    OPENAI_FALLBACK = "OPENAI_FALLBACK"    # native unusable, /v1 reachable
    UNAVAILABLE = "UNAVAILABLE"            # server did not answer at all


@dataclass
class NativeCapability:
    """Truthful, bounded record of what the local Ollama server actually supports.
    ``NATIVE_READY`` is NEVER set on an HTTP 200 alone — a small reply must stream
    and be structurally valid with the reasoning field omitted under think=false."""

    state: NativeProbeState = NativeProbeState.UNKNOWN
    model: str = ""
    native_chat_reachable: bool = False
    streaming_ok: bool = False
    think_false_accepted: bool = False
    reasoning_omitted: bool = False
    metadata_fields: tuple[str, ...] = ()
    server_version: str | None = None
    active_models: tuple[str, ...] = ()
    last_probe_ms: float | None = None
    last_probe_at: float | None = None
    last_error: str | None = None

    @property
    def think_false_supported(self) -> bool:
        """True only when think=false was accepted AND reasoning was actually
        omitted — the honest bar for claiming native no-think works."""
        return self.think_false_accepted and self.reasoning_omitted

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "model": self.model,
            "native_chat_reachable": self.native_chat_reachable,
            "streaming_ok": self.streaming_ok,
            "think_false_accepted": self.think_false_accepted,
            "reasoning_omitted": self.reasoning_omitted,
            "think_false_supported": self.think_false_supported,
            "metadata_fields": list(self.metadata_fields),
            "server_version": self.server_version,
            "active_models": list(self.active_models),
            "last_probe_ms": self.last_probe_ms,
            "last_error": self.last_error,
        }


_PROBE_TIMEOUT_S = 6.0
_PROBE_NUM_PREDICT = 8


async def probe_native(
    *,
    model: str,
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = _time.monotonic,
) -> NativeCapability:
    """Bounded, single-shot capability probe. Does a tiny (``num_predict=8``) native
    think=false stream and proves it is structurally valid with reasoning omitted —
    NOT merely that the server returned 200. Never runs a long generation, never
    pulls a model. Guarded end-to-end: any failure degrades to a truthful state."""
    base = base_url or default_base_url()
    cap = NativeCapability(model=model)
    t0 = clock()
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(_PROBE_TIMEOUT_S))
    try:
        # 1) server version (best-effort; absence is not fatal)
        try:
            r = await client.get(base.rstrip("/") + _VERSION_PATH,
                                 timeout=_PROBE_TIMEOUT_S)
            if r.status_code == 200:
                cap.server_version = (r.json() or {}).get("version")
        except Exception:  # noqa: BLE001
            pass
        # 2) active models (best-effort)
        try:
            r = await client.get(base.rstrip("/") + _PS_PATH, timeout=_PROBE_TIMEOUT_S)
            if r.status_code == 200:
                cap.active_models = tuple(
                    m.get("name", "") for m in (r.json() or {}).get("models", [])
                    if m.get("name")
                )
        except Exception:  # noqa: BLE001
            pass
        # 3) the real proof — a tiny no-think stream must be structurally valid.
        probe_budget = TurnBudget(total_s=_PROBE_TIMEOUT_S * 4)
        probe_timeouts = StageTimeouts(
            connect_s=_PROBE_TIMEOUT_S, first_token_s=_PROBE_TIMEOUT_S * 3,
            idle_s=_PROBE_TIMEOUT_S, total_s=_PROBE_TIMEOUT_S * 4,
        )
        saw_content = False
        saw_thinking = False
        saw_done = False
        fields: set[str] = set()
        try:
            async for ch in chat_stream(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                think=False, max_tokens=_PROBE_NUM_PREDICT, temperature=0.0,
                budget=probe_budget, timeouts=probe_timeouts, ctx=512,
                keep_alive="5m", client=client, base_url=base,
            ):
                cap.native_chat_reachable = True
                if ch.content:
                    saw_content = True
                    cap.streaming_ok = True
                if ch.thinking_present:
                    saw_thinking = True
                if ch.done:
                    saw_done = True
                    for k in _META_KEYS:
                        if getattr(ch, k) is not None:
                            fields.add(k)
        except NativeTransportError as exc:
            cap.last_error = exc.reason
        cap.metadata_fields = tuple(sorted(fields))
        if cap.native_chat_reachable and saw_content and saw_done:
            cap.think_false_accepted = True
            cap.reasoning_omitted = not saw_thinking
            cap.state = (NativeProbeState.NATIVE_READY if cap.reasoning_omitted
                         else NativeProbeState.NATIVE_DEGRADED)
        elif cap.native_chat_reachable and saw_content:
            # streamed content but no done metadata — usable but not fully proven
            cap.think_false_accepted = True
            cap.reasoning_omitted = not saw_thinking
            cap.state = NativeProbeState.NATIVE_DEGRADED
        else:
            cap.state = await _fallback_state(client, base)
    except Exception as exc:  # noqa: BLE001 — the probe must never crash the boot
        cap.last_error = type(exc).__name__
        cap.state = NativeProbeState.UNAVAILABLE
    finally:
        if owns:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    cap.last_probe_ms = round((clock() - t0) * 1000.0, 1)
    cap.last_probe_at = clock()
    logger.info(
        "NATIVE_PROBE: state={} version={} think_false_supported={} "
        "reachable={} probe_ms={}".format(
            cap.state.value, cap.server_version, cap.think_false_supported,
            cap.native_chat_reachable, cap.last_probe_ms,
        )
    )
    return cap


async def _fallback_state(client: httpx.AsyncClient, base: str) -> NativeProbeState:
    """Native chat was unusable — is the OpenAI-compatible ``/v1`` path at least
    reachable? Distinguishes OPENAI_FALLBACK from a fully-down server."""
    try:
        r = await client.get(base.rstrip("/") + "/v1/models", timeout=_PROBE_TIMEOUT_S)
        if r.status_code < 500:
            return NativeProbeState.OPENAI_FALLBACK
    except Exception:  # noqa: BLE001
        pass
    return NativeProbeState.UNAVAILABLE


# ── Process-global cached capability ──────────────────────────────────────────
_native_capability: NativeCapability | None = None


def get_native_capability() -> NativeCapability:
    """The cached capability (``UNKNOWN`` until the first probe). Non-blocking."""
    global _native_capability
    if _native_capability is None:
        _native_capability = NativeCapability()
    return _native_capability


def set_native_capability(cap: NativeCapability) -> None:
    global _native_capability
    _native_capability = cap


def reset_native_capability() -> None:
    """Tests / a fresh process."""
    global _native_capability
    _native_capability = None


async def refresh_native_capability(
    *, model: str, base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> NativeCapability:
    """Run a bounded probe and cache the result. Callers may await this off the
    interactive-startup critical path (it is bounded to a few seconds)."""
    cap = await probe_native(model=model, base_url=base_url, client=client)
    set_native_capability(cap)
    return cap
