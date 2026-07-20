"""core/fast_prewarm.py — V69 M56.4: bounded native FAST full-path prewarm.

WHAT M55 ALREADY WARMED, AND WHAT IT DID NOT
--------------------------------------------
M55.1 warmed the DISPATCH path (query classification, turn policy, task-decision
assembly) so pre-inference work costs ~0 ms on a warm turn. It did not warm the
INFERENCE path: the capability probe's tiny generation proves think=false works, but
under a single model slot the boot-time embedding load evicts qwen3:8b right after,
so the operator's first real question still paid an 11-19 s cold activation.

This module adds the missing half — a complete native FAST request over the same
transport a real turn uses, so the weights are genuinely resident and the code path
is genuinely exercised before the operator types.

WHAT A PREWARM IS FORBIDDEN TO DO
---------------------------------
It is a diagnostic, not a turn. It must not become visible as one:

  * no tools, no RAG, no verifier, no conversation-history mutation;
  * no user-visible answer and no TTS;
  * no semantic-memory write and no operational-memory write;
  * no pollution of the FAST-turn latency metrics a real turn feeds (it records its
    OWN counters, deliberately kept separate);
  * once per model activation - a restart loop can never stack cold loads;
  * never concurrent with another heavy inference (it takes the residency governor's
    lowest priority when one is wired in);
  * never started after STOPPING, and cancellable at any point.

MODES (M56.4.1)
---------------
  OFF                 no model generation at boot; classification warmup may remain
  BACKGROUND          default - the prompt opens immediately, FAST reports WARMING,
                      and the prewarm continues independently
  BEFORE_TEXT_READY   the operator trades boot latency for first-turn latency; the
                      wait is HARD-bounded and a failure degrades to input-enabled
                      rather than blocking JARVIS from starting
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

# The prompt is deterministic, tiny and content-free: it exists to move weights into
# RAM and exercise the transport, never to produce an answer anyone reads.
_PREWARM_PROMPT = "ok"
_PREWARM_NUM_PREDICT = 4
# M56.4 LIVE FINDING — the prewarm MUST use the same num_ctx a real DIRECT_FAST turn
# uses. Ollama reloads the model runner when generation parameters change, so warming
# at ctx=512 and then serving at ctx=2048 makes the operator's first real turn pay a
# FULL reload anyway: measured load_duration 8723 ms on an already-resident qwen3:8b,
# against 428 ms once the contexts matched. Warming a configuration no real turn uses
# is not a warmup. This default only applies when config is unreadable.
_FALLBACK_CTX = 2048
_DEFAULT_TIMEOUT_S = 45.0
# The hard ceiling for BEFORE_TEXT_READY: past this, input opens regardless. Boot must
# never be blocked indefinitely by an optional optimisation.
_BEFORE_TEXT_READY_CAP_S = 60.0


class PrewarmMode(str, Enum):
    OFF = "OFF"
    BACKGROUND = "BACKGROUND"
    BEFORE_TEXT_READY = "BEFORE_TEXT_READY"


DEFAULT_MODE = PrewarmMode.BACKGROUND


def parse_mode(value) -> PrewarmMode:
    """Parse an operator-configured mode. An unrecognized value falls back to the
    DEFAULT rather than raising — and, critically, never silently upgrades a
    configured OFF/BACKGROUND into the boot-blocking mode."""
    if isinstance(value, PrewarmMode):
        return value
    raw = str(value or "").strip().upper().replace("-", "_")
    try:
        return PrewarmMode(raw)
    except ValueError:
        return DEFAULT_MODE


class PrewarmState(str, Enum):
    DISABLED = "DISABLED"        # mode is OFF
    IDLE = "IDLE"                # enabled, not started
    RUNNING = "RUNNING"
    READY = "READY"              # a real content token arrived
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"          # refused: already warm, stopping, or duplicate


@dataclass
class PrewarmRecord:
    """Metrics for ONE prewarm attempt. Timings and flags only — never content."""

    model: str = ""
    mode: str = DEFAULT_MODE.value
    state: PrewarmState = PrewarmState.IDLE
    prewarm_started_at: float | None = None
    queue_wait_ms: float | None = None
    load_duration_ms: float | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    success: bool = False
    model_resident_after: bool | None = None
    cancelled: bool = False
    failure_reason: str | None = None

    def snapshot(self) -> dict:
        return {
            "model": self.model, "mode": self.mode, "state": self.state.value,
            "prewarm_started_at": self.prewarm_started_at,
            "queue_wait_ms": self.queue_wait_ms,
            "load_duration_ms": self.load_duration_ms,
            "first_token_ms": self.first_token_ms, "total_ms": self.total_ms,
            "success": self.success, "model_resident_after": self.model_resident_after,
            "cancelled": self.cancelled, "failure_reason": self.failure_reason,
        }


@dataclass
class PrewarmMetrics:
    """Bounded aggregate across attempts (runtime health)."""

    mode: str = DEFAULT_MODE.value
    state: str = PrewarmState.IDLE.value
    model: str = ""
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    cancellations: int = 0
    last_load_ms: float | None = None
    last_first_token_ms: float | None = None
    last_total_ms: float | None = None
    last_failure_reason: str | None = None

    def fold(self, record: PrewarmRecord) -> None:
        self.mode = record.mode
        self.state = record.state.value
        self.model = record.model or self.model
        if record.state is PrewarmState.SKIPPED or record.state is PrewarmState.DISABLED:
            return
        self.attempts += 1
        if record.success:
            self.successes += 1
            self.last_load_ms = record.load_duration_ms
            self.last_first_token_ms = record.first_token_ms
            self.last_total_ms = record.total_ms
        elif record.cancelled:
            self.cancellations += 1
        else:
            self.failures += 1
            self.last_failure_reason = record.failure_reason

    def snapshot(self) -> dict:
        return {
            "mode": self.mode, "state": self.state, "model": self.model,
            "attempts": self.attempts, "successes": self.successes,
            "failures": self.failures, "cancellations": self.cancellations,
            "last_load_ms": self.last_load_ms,
            "last_first_token_ms": self.last_first_token_ms,
            "last_total_ms": self.last_total_ms,
            "last_failure_reason": self.last_failure_reason,
        }


def resolve_fast_context() -> int:
    """The num_ctx a real DIRECT_FAST turn will use (core.config ``fast_context``).

    Resolved from the SAME setting :mod:`core.fast_path` hands the live turn, so the
    prewarm can never warm a different runner configuration than the one that serves
    the operator.
    """
    try:
        from core.config import settings
        return int(getattr(settings, "fast_context", _FALLBACK_CTX))
    except Exception:  # noqa: BLE001
        return _FALLBACK_CTX


async def native_prewarm_runner(*, model: str, timeout_s: float, keep_alive: str,
                                cancellation=None, ctx: int | None = None) -> PrewarmRecord:
    """Run ONE complete native /api/chat prewarm over the real transport.

    Deliberately identical in shape to a DIRECT_FAST turn — same module, same
    think=false field, same streaming parser — minus everything that makes a turn a
    turn: no tools key is ever sent (``chat_stream`` cannot add one), no history is
    passed, and the streamed text is counted and dropped, never returned.
    """
    from core.ollama_native import NativeTransportError, chat_stream
    from core.turn_budget import StageTimeouts, TurnBudget

    record = PrewarmRecord(model=model, state=PrewarmState.RUNNING,
                           prewarm_started_at=time.time())
    t0 = time.monotonic()
    budget = TurnBudget(total_s=timeout_s)
    timeouts = StageTimeouts(connect_s=5.0, first_token_s=timeout_s, idle_s=10.0,
                             total_s=timeout_s)
    try:
        async for chunk in chat_stream(
            model=model,
            messages=[{"role": "user", "content": _PREWARM_PROMPT}],
            think=False, max_tokens=_PREWARM_NUM_PREDICT, temperature=0.0,
            budget=budget, timeouts=timeouts,
            ctx=ctx if ctx is not None else resolve_fast_context(),
            keep_alive=keep_alive, cancellation=cancellation,
        ):
            if chunk.content and record.first_token_ms is None:
                record.first_token_ms = round((time.monotonic() - t0) * 1000.0, 1)
                record.success = True          # a real content token = warm
            if chunk.done:
                secs = chunk.load_seconds()
                if secs is not None:
                    record.load_duration_ms = round(secs * 1000.0, 1)
                break
    except NativeTransportError as exc:
        record.state, record.failure_reason = PrewarmState.FAILED, exc.reason
    except asyncio.CancelledError:
        record.state, record.cancelled = PrewarmState.CANCELLED, True
        record.total_ms = round((time.monotonic() - t0) * 1000.0, 1)
        raise
    except Exception as exc:  # noqa: BLE001 — a prewarm must never crash the boot
        reason = type(exc).__name__
        record.state = (PrewarmState.TIMEOUT if "Timeout" in reason
                        else PrewarmState.FAILED)
        record.failure_reason = reason
    record.total_ms = round((time.monotonic() - t0) * 1000.0, 1)
    if record.state is PrewarmState.RUNNING:
        if record.success:
            record.state = PrewarmState.READY
        else:
            record.state = PrewarmState.FAILED
            record.failure_reason = record.failure_reason or "no_content_token"
    if cancellation is not None and getattr(cancellation, "cancelled", False):
        record.state, record.cancelled, record.success = PrewarmState.CANCELLED, True, False
    return record


class FastPrewarm:
    """Owns the prewarm lifecycle: mode, once-per-activation guard, cancellation.

    Every collaborator is injectable, so the OFF/BACKGROUND/BEFORE_TEXT_READY matrix,
    the timeout path and the cancellation path are all testable without a server.
    """

    def __init__(self, *, model: str, mode: PrewarmMode = DEFAULT_MODE,
                 timeout_s: float = _DEFAULT_TIMEOUT_S, keep_alive: str = "30m",
                 runner: Callable | None = None,
                 residency_check: Callable | None = None,
                 is_stopping: Callable[[], bool] | None = None,
                 ctx: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model
        self.mode = mode
        self.timeout_s = max(1.0, float(timeout_s))
        self.keep_alive = keep_alive
        # Must match the live turn's num_ctx or the prewarm warms the wrong runner.
        self.ctx = int(ctx) if ctx is not None else resolve_fast_context()
        self._runner = runner or native_prewarm_runner
        self._residency_check = residency_check
        self._is_stopping = is_stopping or (lambda: False)
        self._clock = clock
        self.metrics = PrewarmMetrics(mode=mode.value, model=model)
        self.last: PrewarmRecord | None = None
        self._task: asyncio.Task | None = None
        # Once-per-model-activation guard: the KEY is the model name, so a genuine
        # model switch may prewarm again while a restart loop on the same model
        # cannot stack cold loads.
        self._activated: set[str] = set()
        self._running = False

    # ── state ────────────────────────────────────────────────────────────────
    @property
    def state(self) -> PrewarmState:
        if self.mode is PrewarmMode.OFF:
            return PrewarmState.DISABLED
        if self._running:
            return PrewarmState.RUNNING
        return self.last.state if self.last is not None else PrewarmState.IDLE

    def is_ready(self) -> bool:
        return self.state is PrewarmState.READY

    def _skip(self, reason: str) -> PrewarmRecord:
        rec = PrewarmRecord(model=self.model, mode=self.mode.value,
                            state=(PrewarmState.DISABLED if self.mode is PrewarmMode.OFF
                                   else PrewarmState.SKIPPED),
                            failure_reason=reason)
        self.metrics.fold(rec)
        self.last = rec
        return rec

    # ── the single execution path ────────────────────────────────────────────
    async def run_once(self, *, force: bool = False, cancellation=None) -> PrewarmRecord:
        """Execute at most ONE prewarm. Bounded, guarded, and never raising except
        :class:`asyncio.CancelledError`, which callers must be free to propagate."""
        if self.mode is PrewarmMode.OFF and not force:
            return self._skip("mode_off")
        if self._is_stopping():
            return self._skip("stopping")
        if not self.model:
            return self._skip("no_model_configured")
        if self._running:
            return self._skip("already_running")
        if self.model in self._activated and not force:
            return self._skip("already_prewarmed_for_this_model_activation")

        self._activated.add(self.model)
        self._running = True
        queue_t0 = self._clock()
        try:
            record = await asyncio.wait_for(
                self._runner(model=self.model, timeout_s=self.timeout_s,
                             keep_alive=self.keep_alive, cancellation=cancellation,
                             ctx=self.ctx),
                timeout=self.timeout_s + 5.0,
            )
        except asyncio.TimeoutError:
            record = PrewarmRecord(model=self.model, state=PrewarmState.TIMEOUT,
                                   failure_reason="prewarm_timeout",
                                   total_ms=round((self._clock() - queue_t0) * 1000.0, 1))
        except asyncio.CancelledError:
            record = PrewarmRecord(model=self.model, state=PrewarmState.CANCELLED,
                                   cancelled=True, failure_reason="cancelled")
            record.mode = self.mode.value
            self.metrics.fold(record)
            self.last = record
            self._running = False
            # A cancelled attempt does not consume the activation: the next start may
            # legitimately try again.
            self._activated.discard(self.model)
            raise
        except Exception as exc:  # noqa: BLE001
            record = PrewarmRecord(model=self.model, state=PrewarmState.FAILED,
                                   failure_reason=type(exc).__name__)
        finally:
            self._running = False
        record.mode = self.mode.value
        record.queue_wait_ms = record.queue_wait_ms or 0.0
        if self._residency_check is not None:
            try:
                res = self._residency_check()
                record.model_resident_after = (await res if asyncio.iscoroutine(res)
                                               else bool(res))
            except Exception:  # noqa: BLE001
                record.model_resident_after = None
        self.metrics.fold(record)
        self.last = record
        return record

    # ── mode-driven boot entry points ────────────────────────────────────────
    def start_background(self) -> "asyncio.Task | None":
        """Fire-and-supervise. Returns the task (or None when the mode/state refuses).

        The caller's prompt opens immediately; readiness stays truthful (WARMING)
        until a content token actually arrives.
        """
        if self.mode is not PrewarmMode.BACKGROUND or self._is_stopping():
            return None
        if self._task is not None and not self._task.done():
            return self._task

        async def _supervised() -> None:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never escape into the event loop
                pass

        self._task = asyncio.ensure_future(_supervised())
        return self._task

    async def run_before_text_ready(self) -> PrewarmRecord:
        """Bounded wait used by BEFORE_TEXT_READY. Degrades, never blocks forever.

        The cap is enforced HERE and not only inside the runner, so even a runner
        that ignores its own timeout cannot hold the prompt closed.
        """
        if self.mode is not PrewarmMode.BEFORE_TEXT_READY:
            return self._skip("mode_not_before_text_ready")
        cap = min(self.timeout_s, _BEFORE_TEXT_READY_CAP_S)
        try:
            return await asyncio.wait_for(self.run_once(), timeout=cap)
        except asyncio.TimeoutError:
            rec = PrewarmRecord(model=self.model, mode=self.mode.value,
                                state=PrewarmState.TIMEOUT,
                                failure_reason="before_text_ready_cap")
            self.metrics.fold(rec)
            self.last = rec
            return rec

    async def cancel(self) -> None:
        """Cancel an in-flight background prewarm and await its teardown. Bounded."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(task,
                                                                 return_exceptions=True)),
                                   timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    def note_model_switch(self, model: str) -> None:
        """A genuine model change re-arms the once-per-activation guard."""
        if model and model != self.model:
            self.model = model
            self.metrics.model = model
            self._activated.discard(model)

    def snapshot(self) -> dict:
        out = self.metrics.snapshot()
        out["state"] = self.state.value
        out["mode"] = self.mode.value
        out["model"] = self.model
        out["last"] = self.last.snapshot() if self.last is not None else None
        return out


# ── Process-global singleton ─────────────────────────────────────────────────
_prewarm: FastPrewarm | None = None


def get_fast_prewarm() -> FastPrewarm:
    """The process prewarm, built from operator config on first use."""
    global _prewarm
    if _prewarm is None:
        model = ""
        mode = DEFAULT_MODE
        timeout_s = _DEFAULT_TIMEOUT_S
        keep_alive = "30m"
        try:
            from core.config import settings
            mode = parse_mode(getattr(settings, "fast_prewarm_mode", DEFAULT_MODE.value))
            timeout_s = float(getattr(settings, "fast_prewarm_timeout_s", _DEFAULT_TIMEOUT_S))
            keep_alive = getattr(settings, "fast_keep_alive", "30m")
            model = (getattr(settings, "fast_model", "") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        if not model:
            try:
                from core.model_router import ModelRole, model_for_role
                model = model_for_role(ModelRole.FAST) or ""
            except Exception:  # noqa: BLE001
                model = ""
        stopping = None
        try:
            from core.lifecycle import get_lifecycle
            _lc = get_lifecycle()
            stopping = _lc.is_stopping
        except Exception:  # noqa: BLE001
            pass
        _prewarm = FastPrewarm(model=model, mode=mode, timeout_s=timeout_s,
                               keep_alive=keep_alive, is_stopping=stopping)
    return _prewarm


def reset_fast_prewarm(instance: FastPrewarm | None = None) -> None:
    """Tests / a fresh process."""
    global _prewarm
    _prewarm = instance
