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
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

_PROBE_TIMEOUT_S = 3.0
_PREWARM_TIMEOUT_S = 45.0


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
