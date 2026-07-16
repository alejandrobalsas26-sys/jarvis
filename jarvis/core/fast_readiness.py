"""
core/fast_readiness.py — V69 M54.1.8: is FAST actually able to answer?

M54 marked `LIFECYCLE: TEXT_READY — interactive input enabled` while nothing had
established that the FAST model could serve a turn. A model NAME existing in
configuration is not readiness: on this host the operator's first question sat for
minutes because Ollama had to swap qwen3:8b in from disk (OLLAMA_MAX_LOADED_MODELS=1
means the boot-time nomic embedding load EVICTS it).

This module answers the question honestly and cheaply:

    CONFIGURED   a FAST model name is configured (says nothing about the server)
    REACHABLE    the Ollama server answered, and the model is present in its list
    WARMING      a bounded prewarm is in flight (input is still allowed)
    READY        the model produced a token — it can serve a turn now
    DEGRADED     the server answered but the model is missing/erroring
    UNAVAILABLE  the server did not answer

Deliberately bounded: the probe is a metadata GET (no inference), and the optional
prewarm asks for ONE token. It never runs a large generation at boot, and it never
loads FAST alongside EMBEDDING deliberately — under OLLAMA_MAX_LOADED_MODELS=1 the
server serializes that for us, so we simply do not pin both.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

_PROBE_TIMEOUT_S = 3.0
_PREWARM_TIMEOUT_S = 45.0
# Bounded moving-window size for FAST first-token / total / throughput samples.
_FT_WINDOW = 20


class FastState(str, Enum):
    CONFIGURED = "CONFIGURED"
    REACHABLE = "REACHABLE"
    WARMING = "WARMING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


# States in which the operator may type and expect a bounded outcome.
_INPUT_OK = frozenset({FastState.REACHABLE, FastState.WARMING, FastState.READY,
                       FastState.DEGRADED})


@dataclass
class FastReadiness:
    """Bounded readiness state for the interactive FAST role."""

    model: str = ""
    base_url: str = "http://localhost:11434"
    clock: Callable[[], float] = time.monotonic
    _state: FastState = field(default=FastState.CONFIGURED)
    _last_probe_ms: float | None = field(default=None)
    _last_success_at: float | None = field(default=None)
    _last_error: str | None = field(default=None)
    _prewarm_started: bool = field(default=False)
    # ── V69 M55.10 — FAST transport + no-think capability + latency stats ──────
    transport: str = field(default="auto")
    think_supported: bool | None = field(default=None)
    native_state: str = field(default="UNKNOWN")
    server_version: str | None = field(default=None)
    _ft_samples: "deque[float]" = field(default_factory=lambda: deque(maxlen=_FT_WINDOW))
    _total_samples: "deque[float]" = field(default_factory=lambda: deque(maxlen=_FT_WINDOW))
    _tps_samples: "deque[float]" = field(default_factory=lambda: deque(maxlen=_FT_WINDOW))
    _successes: int = field(default=0)
    _timeouts: int = field(default=0)
    _cancellations: int = field(default=0)
    _native_fallbacks: int = field(default=0)
    _last_timeout_stage: str | None = field(default=None)
    _post_cancel_busy_ms: float | None = field(default=None)

    @property
    def state(self) -> FastState:
        return self._state

    def accepts_input(self) -> bool:
        """True when a turn will either answer or fail within its budget. DEGRADED
        counts: a bounded failure is still an answer, and refusing input would be
        worse than telling the operator the model is unwell."""
        return self._state in _INPUT_OK

    def snapshot(self) -> dict:
        """Bounded health view. No prompts, no payloads — a model name and state."""
        return {
            "state": self._state.value,
            "model": self.model,
            "last_probe_ms": self._last_probe_ms,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "accepts_input": self.accepts_input(),
            # M55.10 — transport + no-think capability (additive keys).
            "transport": self.transport,
            "think_supported": self.think_supported,
            "native_state": self.native_state,
            "server_version": self.server_version,
        }

    # ── V69 M55.10 — FAST transport capability + latency observability ─────────
    def note_capability(self, cap) -> None:
        """Fold a :class:`~core.ollama_native.NativeCapability` into readiness.
        Bounded and never raising."""
        try:
            state = getattr(cap, "state", None)
            self.native_state = getattr(state, "value", str(state)) if state else "UNKNOWN"
            self.think_supported = bool(getattr(cap, "think_false_supported", False))
            ver = getattr(cap, "server_version", None)
            if ver:
                self.server_version = ver
        except Exception:  # noqa: BLE001
            pass

    def record_fast_turn(self, *, first_token_ms=None, total_ms=None,
                         tokens_per_second=None, transport=None, think=None) -> None:
        """Record one completed FAST turn's latency sample (bounded ring)."""
        self._successes += 1
        if transport:
            self.transport = transport
        if first_token_ms:
            self._ft_samples.append(float(first_token_ms))
        if total_ms:
            self._total_samples.append(float(total_ms))
        if tokens_per_second:
            self._tps_samples.append(float(tokens_per_second))

    def note_timeout(self, stage: str | None) -> None:
        self._timeouts += 1
        self._last_timeout_stage = stage

    def note_cancellation(self) -> None:
        self._cancellations += 1

    def note_native_fallback(self) -> None:
        self._native_fallbacks += 1

    def note_post_cancel_busy(self, ms: float | None) -> None:
        """Server-side continuation observed AFTER a client cancel (M55.9). Only set
        when actually measured — left None otherwise (never invented)."""
        self._post_cancel_busy_ms = ms

    @staticmethod
    def _p50(xs: list[float]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        return round(s[len(s) // 2], 1)

    def fast_inference_snapshot(self) -> dict:
        """Bounded FAST-inference metrics for runtime health (M55.13). Counters and
        timings only — never prompt content or generated text."""
        ft = list(self._ft_samples)
        tot = list(self._total_samples)
        tps = list(self._tps_samples)
        gen_cap = ctx_cap = think_req = None
        transport = self.transport
        try:
            from core.config import settings as _s
            gen_cap = getattr(_s, "fast_max_tokens", None)
            ctx_cap = getattr(_s, "fast_context", None)
            think_req = _s.fast_think_value() if hasattr(_s, "fast_think_value") else None
            transport = self.transport or getattr(_s, "fast_transport", "auto")
        except Exception:  # noqa: BLE001
            pass
        return {
            "active_transport": transport,
            "active_model": self.model,
            "think_requested": think_req,
            "think_supported": self.think_supported,
            "native_state": self.native_state,
            "generation_cap": gen_cap,
            "context_cap": ctx_cap,
            "requests": self._successes + self._timeouts + self._cancellations,
            "successes": self._successes,
            "timeouts": self._timeouts,
            "cancellations": self._cancellations,
            "native_fallbacks": self._native_fallbacks,
            "average_first_token_ms": round(sum(ft) / len(ft), 1) if ft else None,
            "p50_first_token_ms": self._p50(ft),
            "recent_first_token_ms": ft[-1] if ft else None,
            "average_total_ms": round(sum(tot) / len(tot), 1) if tot else None,
            "recent_tokens_per_second": tps[-1] if tps else None,
            "last_timeout_stage": self._last_timeout_stage,
            "post_cancel_busy_ms": self._post_cancel_busy_ms,
        }

    async def probe(self) -> FastState:
        """Bounded metadata probe — no inference. Asks Ollama which models it has
        and whether ours is among them."""
        t0 = self.clock()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                names = {m.get("name", "") for m in resp.json().get("models", [])}
        except Exception as exc:
            self._last_probe_ms = round((self.clock() - t0) * 1000.0, 1)
            self._last_error = type(exc).__name__
            self._state = FastState.UNAVAILABLE
            return self._state
        self._last_probe_ms = round((self.clock() - t0) * 1000.0, 1)
        self._last_success_at = self.clock()
        if self._model_present(names):
            self._last_error = None
            # Never downgrade READY back to REACHABLE on a later probe.
            if self._state is not FastState.READY:
                self._state = FastState.REACHABLE
        else:
            self._last_error = "model_not_installed"
            self._state = FastState.DEGRADED
        return self._state

    def _model_present(self, names: set[str]) -> bool:
        """Ollama reports 'qwen3:8b'; a configured name may omit the ':latest' tag."""
        if not self.model:
            return False
        if self.model in names:
            return True
        base = self.model.split(":", 1)[0]
        return any(n.split(":", 1)[0] == base for n in names)

    async def prewarm(self, client=None) -> FastState:
        """Ask for ONE token so the weights are resident before the operator's first
        real question. Idempotent: repeated calls are refused, so a restart loop can
        never stack prewarms (each would be a full cold load on a 15W CPU)."""
        if self._prewarm_started or self._state is FastState.READY:
            return self._state
        self._prewarm_started = True
        if self._state is FastState.UNAVAILABLE:
            return self._state
        self._state = FastState.WARMING
        t0 = self.clock()
        try:
            if client is None:
                return self._state
            await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                    stream=False,
                ),
                timeout=_PREWARM_TIMEOUT_S,
            )
        except Exception as exc:
            self._last_error = type(exc).__name__
            # A failed prewarm does NOT block input: the turn's own deadline will
            # produce a bounded failure if the model really is broken.
            self._state = FastState.DEGRADED
            return self._state
        self._last_probe_ms = round((self.clock() - t0) * 1000.0, 1)
        self._last_success_at = self.clock()
        self._last_error = None
        self._state = FastState.READY
        return self._state

    def mark_served(self) -> None:
        """A real turn produced tokens — the strongest possible readiness evidence."""
        self._state = FastState.READY
        self._last_success_at = self.clock()
        self._last_error = None


# ── Process-global singleton ─────────────────────────────────────────────────
_fast: FastReadiness | None = None


def get_fast_readiness() -> FastReadiness:
    global _fast
    if _fast is None:
        model = ""
        try:
            from core.model_router import ModelRole, model_for_role
            model = model_for_role(ModelRole.FAST) or ""
        except Exception:
            try:
                from core.config import settings
                model = getattr(settings, "llm_model", "") or ""
            except Exception:
                model = ""
        _fast = FastReadiness(model=model)
    return _fast


def reset_fast_readiness(instance: FastReadiness | None = None) -> None:
    """Tests / a fresh process."""
    global _fast
    _fast = instance
