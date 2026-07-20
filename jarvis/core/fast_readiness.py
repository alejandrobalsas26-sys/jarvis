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
    CONFIGURED = "CONFIGURED"    # a model name is configured (says nothing about the server)
    PROBING = "PROBING"          # a capability probe is in flight (M55.3)
    REACHABLE = "REACHABLE"      # server answered, model present
    # V69 M56.8 — WARMING was one word for three different situations. Splitting them
    # lets the prompt say something TRUE and specific ("loading the model" vs "running
    # a warmup generation") instead of a generic "warming".
    MODEL_LOADING = "MODEL_LOADING"  # the server is swapping the weights in
    PREWARMING = "PREWARMING"        # a full-path native prewarm generation is in flight
    WARMING = "WARMING"          # server reachable/loading OR one probe inconclusive
    READY = "READY"              # the model produced a token — it can serve now
    DEGRADED = "DEGRADED"        # native unusable but a functional fallback exists
    UNAVAILABLE = "UNAVAILABLE"  # native AND fallback proven unavailable


# States in which the operator may type and expect a bounded outcome. PROBING/WARMING
# are included: a turn under an in-flight/loading model still answers or fails within
# its own budget, and refusing input would be worse than a slow first answer.
_INPUT_OK = frozenset({FastState.PROBING, FastState.REACHABLE, FastState.WARMING,
                       FastState.MODEL_LOADING, FastState.PREWARMING,
                       FastState.READY, FastState.DEGRADED})
# The warming family: states in which the operator's turn will work but may be slow.
# TEXT_READY is INDEPENDENT of every one of them (M56.8) — input works throughout.
WARMING_STATES = frozenset({FastState.PROBING, FastState.WARMING,
                            FastState.MODEL_LOADING, FastState.PREWARMING})


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
    # M55.3 — did the metadata probe ever actually reach the server? Distinguishes a
    # busy/loading server (WARMING) from a truly-down one (eligible for UNAVAILABLE).
    _reached_server: bool = field(default=False)
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
        """Bounded metadata probe — no inference. Asks Ollama which models it has and
        whether ours is among them.

        M55.3 truthfulness: the probe transitions UNKNOWN/CONFIGURED -> PROBING while
        in flight, and a SINGLE failure resolves to WARMING, never UNAVAILABLE — on
        this host the boot-time nomic embedding load can make Ollama momentarily
        unresponsive under OLLAMA_MAX_LOADED_MODELS=1, and declaring the model dead
        because one 3s metadata GET timed out is exactly the premature verdict the live
        run showed. UNAVAILABLE is decided only by reconcile(), after the native
        transport probe ALSO fails and the server was never reached."""
        t0 = self.clock()
        # M56.4 — never downgrade a more specific in-flight state (a running prewarm /
        # an observed model load) back to the generic PROBING.
        if self._state not in (FastState.READY, FastState.PREWARMING,
                               FastState.MODEL_LOADING):
            self._state = FastState.PROBING
        try:
            import httpx
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                names = {m.get("name", "") for m in resp.json().get("models", [])}
        except Exception as exc:
            self._last_probe_ms = round((self.clock() - t0) * 1000.0, 1)
            self._last_error = type(exc).__name__
            # Inconclusive, not fatal: stay WARMING (input still allowed) and let
            # reconcile() reach the truthful verdict once the native probe concludes.
            # M56.4 — a more specific in-flight state (PREWARMING / MODEL_LOADING) is
            # never replaced by this generic one: the prewarm is still running and the
            # operator prompt would otherwise lose the accurate explanation.
            if self._state not in (FastState.READY, FastState.PREWARMING,
                                   FastState.MODEL_LOADING):
                self._state = FastState.WARMING
            return self._state
        self._last_probe_ms = round((self.clock() - t0) * 1000.0, 1)
        self._last_success_at = self.clock()
        self._reached_server = True
        if self._model_present(names):
            self._last_error = None
            # Never downgrade READY back to REACHABLE on a later probe — nor a
            # PREWARMING/MODEL_LOADING state, which is strictly more informative than
            # "the server lists this model" (M56.4).
            if self._state not in (FastState.READY, FastState.PREWARMING,
                                   FastState.MODEL_LOADING):
                self._state = FastState.REACHABLE
        else:
            self._last_error = "model_not_installed"
            self._state = FastState.DEGRADED
        return self._state

    def reconcile(self, cap) -> FastState:
        """V69 M55.3 — the truthful FAST verdict AFTER both the metadata probe and the
        native-transport capability probe have run. This is where UNAVAILABLE may be
        declared, and only when the server is proven unreachable by BOTH paths:

          native NATIVE_READY / streamed a token   -> READY
          native OPENAI_FALLBACK / NATIVE_DEGRADED  -> DEGRADED (fallback exists)
          native UNKNOWN / PROBING (inconclusive)   -> WARMING (never UNAVAILABLE)
          native UNAVAILABLE + server never reached -> UNAVAILABLE
          native UNAVAILABLE + server WAS reached   -> DEGRADED (reachable, transport bad)

        Bounded and never raising."""
        try:
            self.note_capability(cap)
            native = getattr(getattr(cap, "state", None), "value", "") or ""
            if getattr(cap, "streaming_ok", False) or native == "NATIVE_READY":
                self.mark_served()                     # a real token — strongest evidence
                return self._state
            if native in ("OPENAI_FALLBACK", "NATIVE_DEGRADED"):
                if self._state is not FastState.READY:
                    self._state = FastState.DEGRADED
                return self._state
            if native in ("", "UNKNOWN", "PROBING"):
                # Native probe inconclusive — do not regress a good metadata result.
                if self._state in (FastState.PROBING, FastState.CONFIGURED):
                    self._state = FastState.WARMING
                return self._state
            # native == UNAVAILABLE (proven). Only now can UNAVAILABLE be truthful, and
            # only if the metadata probe also never reached the server.
            self._state = (FastState.DEGRADED if self._reached_server
                           else FastState.UNAVAILABLE)
        except Exception:  # noqa: BLE001 — readiness must never crash boot
            pass
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

    # ── V69 M56.4 — full-path prewarm integration ────────────────────────────
    def note_model_loading(self) -> None:
        """The server is swapping the weights in. Input stays enabled; the prompt can
        now say something specific instead of a generic 'warming'."""
        if self._state is not FastState.READY:
            self._state = FastState.MODEL_LOADING

    def note_prewarm_started(self) -> None:
        """A full-path native prewarm generation is in flight. NEVER READY yet — the
        state only becomes READY when a real content token arrives (M56.8: readiness
        is never claimed because a prewarm was merely started)."""
        if self._state is not FastState.READY:
            self._state = FastState.PREWARMING

    def note_prewarm_result(self, record) -> FastState:
        """Fold a :class:`~core.fast_prewarm.PrewarmRecord` into readiness.

        Only a prewarm that actually produced a content token yields READY. A
        timeout/failure does NOT mark the model unavailable — the turn's own deadline
        remains the honest bound — so it degrades to WARMING and input stays open.
        """
        try:
            success = bool(getattr(record, "success", False))
            state = getattr(getattr(record, "state", None), "value", "") or ""
            if success and state == "READY":
                self.mark_served()
                return self._state
            if state in ("SKIPPED", "DISABLED"):
                # The prewarm did not run; readiness is whatever the probes concluded.
                if self._state in (FastState.PREWARMING, FastState.MODEL_LOADING):
                    self._state = FastState.WARMING
                return self._state
            self._last_error = getattr(record, "failure_reason", None) or state or None
            if self._state is not FastState.READY:
                self._state = FastState.WARMING
        except Exception:  # noqa: BLE001 — readiness must never crash boot
            pass
        return self._state

    def warming_hint(self) -> str | None:
        """A short, TRUE sentence for the prompt when FAST is not yet READY, or None.

        Spanish to match the operator-facing surface, ASCII-safe, and specific about
        WHICH wait is happening — an indefinite silence is exactly what M54.1 fixed.
        """
        hints = {
            FastState.MODEL_LOADING: "cargando el modelo FAST; tu primer mensaje puede tardar mas",
            FastState.PREWARMING: "calentando el modelo FAST; tu primer mensaje puede tardar mas",
            FastState.PROBING: "comprobando el modelo FAST; puedes escribir ya",
            FastState.WARMING: "modelo FAST calentando; tu primer mensaje puede tardar mas",
            FastState.DEGRADED: "modelo FAST degradado; respondere con la ruta alternativa",
            FastState.UNAVAILABLE: "modelo FAST no disponible; solo respuestas deterministas",
        }
        return hints.get(self._state)


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
