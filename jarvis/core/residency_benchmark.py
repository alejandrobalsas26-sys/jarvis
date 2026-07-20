"""core/residency_benchmark.py — V69 M56.7: cold/warm/post-switch benchmark harness.

WHY A HARNESS AND NOT AD-HOC TIMING
------------------------------------
Every performance claim in M55 came from reading a log by hand. That is fine once and
useless as a regression guard: nobody can tell whether "27.5 s" is better or worse than
last week without the same prompt, the same bounds and the same scenario definition.

This module defines the scenarios ONCE, as data, and runs them two ways:

  * a DETERMINISTIC harness against injected fakes — the scenario logic, the metric
    derivation and the comparisons are all unit-testable with no model at all;
  * a BOUNDED live runner the operator triggers, which uses the same definitions and
    the same tiny controlled prompts.

SCENARIOS
---------
    A PROCESS_COLD     no JARVIS cache; inspect models; one native FAST turn
    B FAST_WARM        the same short native turn repeated
    C POST_EMBEDDING   FAST, one embedding, FAST again
    D POST_DEEP        FAST, one bounded DEEP request, FAST again
    E POST_CANCEL      cancel a bounded generation, then the next FAST turn
    F PREWARM          full-path prewarm, then the first operator-style FAST turn

BOUNDS AND HYGIENE
------------------
Trials are few and short by design (this is a 15 W CPU: a dozen permutations would
cost an hour and tell us nothing new). Prompts are three fixed controlled strings and
NEVER production user content. Time/status questions stay deterministic bypasses and
are asserted to issue no model request at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

# The three controlled prompts. Fixed, ASCII-safe where possible, and never sourced
# from a transcript, a memory or a model.
PROMPT_GREETING = "hola"
PROMPT_SIMPLE_EDU = "como saco la raiz cuadrada de algo"
PROMPT_CONCEPT = "explicame POO brevemente"
# Deterministic-bypass probes: these must be answered with NO model request.
PROMPT_TIME = "que hora es"
PROMPT_STATUS = "estado del sistema"

CONTROLLED_PROMPTS = (PROMPT_GREETING, PROMPT_SIMPLE_EDU, PROMPT_CONCEPT)
DETERMINISTIC_PROMPTS = (PROMPT_TIME, PROMPT_STATUS)

# Bounded by design — see the module docstring.
_DEFAULT_TRIALS = 2
_MAX_TRIALS = 5


class Scenario(str, Enum):
    PROCESS_COLD = "A_PROCESS_COLD"
    FAST_WARM = "B_FAST_WARM"
    POST_EMBEDDING = "C_POST_EMBEDDING"
    POST_DEEP = "D_POST_DEEP"
    POST_CANCEL = "E_POST_CANCEL"
    PREWARM = "F_PREWARM"


@dataclass(frozen=True)
class TrialMetrics:
    """One measured request. Every field is observed or None — never interpolated."""

    label: str
    prompt: str = ""
    queue_wait_ms: float | None = None
    dispatch_ms: float | None = None
    connect_ms: float | None = None
    load_duration_ms: float | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    eval_count: int | None = None
    tokens_per_second: float | None = None
    models_before: tuple[str, ...] = ()
    models_after: tuple[str, ...] = ()
    model_requested: bool = True
    cancelled: bool = False
    ok: bool = False
    error: str | None = None

    def snapshot(self) -> dict:
        return {
            "label": self.label,
            # The prompt is one of the fixed controlled strings; recording it is safe
            # and makes a result reproducible.
            "prompt": self.prompt,
            "queue_wait_ms": self.queue_wait_ms, "dispatch_ms": self.dispatch_ms,
            "connect_ms": self.connect_ms, "load_duration_ms": self.load_duration_ms,
            "first_token_ms": self.first_token_ms, "total_ms": self.total_ms,
            "eval_count": self.eval_count, "tokens_per_second": self.tokens_per_second,
            "models_before": list(self.models_before),
            "models_after": list(self.models_after),
            "model_requested": self.model_requested, "cancelled": self.cancelled,
            "ok": self.ok, "error": self.error,
        }


@dataclass
class ScenarioResult:
    """The outcome of one scenario: its trials plus derived, honest comparisons."""

    scenario: Scenario
    trials: list[TrialMetrics] = field(default_factory=list)
    power_profile: str = "UNKNOWN"
    started_at: float = 0.0
    notes: list[str] = field(default_factory=list)

    def trial(self, label: str) -> TrialMetrics | None:
        for t in self.trials:
            if t.label == label:
                return t
        return None

    def ok(self) -> bool:
        return bool(self.trials) and all(t.ok for t in self.trials)

    def first_token_series(self) -> list[float]:
        return [t.first_token_ms for t in self.trials
                if t.first_token_ms is not None]

    def improvement_ms(self, baseline: str, candidate: str) -> float | None:
        """How much faster ``candidate``'s first token was than ``baseline``'s.

        Positive = the candidate was faster. Returns None when either measurement is
        missing — a missing number is never treated as zero.
        """
        a, b = self.trial(baseline), self.trial(candidate)
        if a is None or b is None:
            return None
        if a.first_token_ms is None or b.first_token_ms is None:
            return None
        return round(a.first_token_ms - b.first_token_ms, 1)

    def model_restored(self, model: str) -> bool | None:
        """Did the LAST trial observe ``model`` resident afterwards?"""
        from core.residency import model_matches

        if not self.trials:
            return None
        after = self.trials[-1].models_after
        if not after:
            return None
        return any(model_matches(n, model) for n in after)

    def snapshot(self) -> dict:
        return {
            "scenario": self.scenario.value,
            "ok": self.ok(),
            "power_profile": self.power_profile,
            "started_at": self.started_at,
            "trials": [t.snapshot() for t in self.trials],
            "first_token_series": self.first_token_series(),
            "notes": list(self.notes),
        }


@dataclass
class BenchmarkReport:
    """All scenarios from one bounded run."""

    results: list[ScenarioResult] = field(default_factory=list)
    power_profile: str = "UNKNOWN"
    residency_state: str = "UNKNOWN"
    started_at: float = 0.0
    finished_at: float | None = None

    def result(self, scenario: Scenario) -> ScenarioResult | None:
        for r in self.results:
            if r.scenario is scenario:
                return r
        return None

    def cold_vs_warm_ms(self) -> float | None:
        """The headline number: how much a warm first token beats a cold one."""
        cold = self.result(Scenario.PROCESS_COLD)
        warm = self.result(Scenario.FAST_WARM)
        if cold is None or warm is None:
            return None
        c = cold.first_token_series()
        w = warm.first_token_series()
        if not c or not w:
            return None
        return round(c[0] - min(w), 1)

    def eviction_cost_ms(self) -> float | None:
        """The cost of the post-embedding reload, if scenario C measured one."""
        c = self.result(Scenario.POST_EMBEDDING)
        return c.improvement_ms("fast_after_embedding", "fast_before_embedding") if c else None

    def snapshot(self) -> dict:
        return {
            "power_profile": self.power_profile,
            "residency_state": self.residency_state,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "cold_vs_warm_ms": self.cold_vs_warm_ms(),
            "eviction_cost_ms": self.eviction_cost_ms(),
            "results": [r.snapshot() for r in self.results],
        }

    def render(self) -> str:
        """A compact ASCII table for the operator."""
        lines = ["RESIDENCY BENCHMARK",
                 f"  power_profile={self.power_profile} residency={self.residency_state}"]
        for r in self.results:
            lines.append(f"  {r.scenario.value}: ok={r.ok()}")
            for t in r.trials:
                lines.append(
                    "    {:<26} first_token_ms={} total_ms={} load_ms={} tps={}".format(
                        t.label, t.first_token_ms, t.total_ms, t.load_duration_ms,
                        t.tokens_per_second))
        cw = self.cold_vs_warm_ms()
        if cw is not None:
            lines.append(f"  cold -> warm first-token improvement: {cw} ms")
        ev = self.eviction_cost_ms()
        if ev is not None:
            lines.append(f"  post-embedding reload cost: {ev} ms")
        return "\n".join(lines)


class BenchmarkHarness:
    """Runs the scenarios. Every collaborator is injected, so the whole harness is
    deterministic under test and bounded in live use.

    ``fast_turn(prompt, label)`` -> TrialMetrics
    ``embedding()``              -> TrialMetrics
    ``deep_turn()``              -> TrialMetrics
    ``cancel_turn()``            -> TrialMetrics
    ``prewarm()``                -> TrialMetrics
    ``inspector()``              -> tuple[str, ...] of resident model names
    ``deterministic(prompt)``    -> TrialMetrics (must report model_requested=False)
    """

    def __init__(self, *, fast_turn: Callable, embedding: Callable | None = None,
                 deep_turn: Callable | None = None, cancel_turn: Callable | None = None,
                 prewarm: Callable | None = None, inspector: Callable | None = None,
                 deterministic: Callable | None = None,
                 power_profile: str = "UNKNOWN",
                 trials: int = _DEFAULT_TRIALS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fast = fast_turn
        self._embedding = embedding
        self._deep = deep_turn
        self._cancel = cancel_turn
        self._prewarm = prewarm
        self._inspect = inspector
        self._deterministic = deterministic
        self.power_profile = power_profile
        self.trials = max(1, min(int(trials), _MAX_TRIALS))
        self._clock = clock

    async def _models(self) -> tuple[str, ...]:
        if self._inspect is None:
            return ()
        try:
            names = await self._inspect()
            return tuple(names or ())
        except Exception:  # noqa: BLE001
            return ()

    def _new(self, scenario: Scenario) -> ScenarioResult:
        return ScenarioResult(scenario=scenario, power_profile=self.power_profile,
                              started_at=self._clock())

    async def _step(self, fn, *args, label: str) -> TrialMetrics:
        if fn is None:
            return TrialMetrics(label=label, ok=False, error="no_probe")
        try:
            return await fn(*args)
        except Exception as exc:  # noqa: BLE001 — a benchmark never crashes the runtime
            return TrialMetrics(label=label, ok=False, error=type(exc).__name__)

    # ── scenarios ────────────────────────────────────────────────────────────
    async def scenario_a_process_cold(self) -> ScenarioResult:
        result = self._new(Scenario.PROCESS_COLD)
        before = await self._models()
        trial = await self._step(self._fast, PROMPT_GREETING, "cold_fast",
                                 label="cold_fast")
        result.trials.append(trial)
        if before:
            result.notes.append("models resident before the cold turn: " + ",".join(before))
        else:
            result.notes.append("no model was resident before the cold turn")
        return result

    async def scenario_b_fast_warm(self) -> ScenarioResult:
        result = self._new(Scenario.FAST_WARM)
        for i in range(self.trials):
            result.trials.append(await self._step(self._fast, PROMPT_GREETING,
                                                  f"warm_fast_{i + 1}",
                                                  label=f"warm_fast_{i + 1}"))
        return result

    async def scenario_c_post_embedding(self) -> ScenarioResult:
        result = self._new(Scenario.POST_EMBEDDING)
        result.trials.append(await self._step(self._fast, PROMPT_GREETING,
                                              "fast_before_embedding",
                                              label="fast_before_embedding"))
        result.trials.append(await self._step(self._embedding, label="embedding"))
        result.trials.append(await self._step(self._fast, PROMPT_GREETING,
                                              "fast_after_embedding",
                                              label="fast_after_embedding"))
        return result

    async def scenario_d_post_deep(self) -> ScenarioResult:
        result = self._new(Scenario.POST_DEEP)
        result.trials.append(await self._step(self._fast, PROMPT_GREETING,
                                              "fast_before_deep",
                                              label="fast_before_deep"))
        result.trials.append(await self._step(self._deep, label="deep"))
        result.trials.append(await self._step(self._fast, PROMPT_GREETING,
                                              "fast_after_deep",
                                              label="fast_after_deep"))
        return result

    async def scenario_e_post_cancel(self) -> ScenarioResult:
        result = self._new(Scenario.POST_CANCEL)
        result.trials.append(await self._step(self._cancel, label="cancelled"))
        result.trials.append(await self._step(self._fast, PROMPT_SIMPLE_EDU,
                                              "fast_after_cancel",
                                              label="fast_after_cancel"))
        return result

    async def scenario_f_prewarm(self) -> ScenarioResult:
        result = self._new(Scenario.PREWARM)
        result.trials.append(await self._step(self._prewarm, label="prewarm"))
        result.trials.append(await self._step(self._fast, PROMPT_CONCEPT,
                                              "first_operator_turn",
                                              label="first_operator_turn"))
        return result

    async def deterministic_bypass_check(self) -> list[TrialMetrics]:
        """Time/status questions must be answered with NO model request at all."""
        out: list[TrialMetrics] = []
        for prompt in DETERMINISTIC_PROMPTS:
            out.append(await self._step(self._deterministic, prompt,
                                        label=f"deterministic:{prompt}"))
        return out

    async def run(self, scenarios=None) -> BenchmarkReport:
        """Run the requested scenarios (default: all). Bounded and never raising."""
        wanted = list(scenarios) if scenarios is not None else list(Scenario)
        report = BenchmarkReport(power_profile=self.power_profile,
                                 started_at=self._clock())
        runners = {
            Scenario.PROCESS_COLD: self.scenario_a_process_cold,
            Scenario.FAST_WARM: self.scenario_b_fast_warm,
            Scenario.POST_EMBEDDING: self.scenario_c_post_embedding,
            Scenario.POST_DEEP: self.scenario_d_post_deep,
            Scenario.POST_CANCEL: self.scenario_e_post_cancel,
            Scenario.PREWARM: self.scenario_f_prewarm,
        }
        for scenario in wanted:
            runner = runners.get(scenario)
            if runner is None:
                continue
            try:
                report.results.append(await runner())
            except Exception as exc:  # noqa: BLE001
                failed = self._new(scenario)
                failed.notes.append(f"scenario failed: {type(exc).__name__}")
                report.results.append(failed)
        report.finished_at = self._clock()
        return report


# ── Live probe factories (bounded; operator-triggered) ───────────────────────
def live_fast_turn(*, model: str, timeout_s: float = 60.0, max_tokens: int = 64,
                   keep_alive: str = "30m", inspector: Callable | None = None):
    """One controlled native think=false turn measured end to end."""
    async def _turn(prompt: str, label: str) -> TrialMetrics:
        from core.ollama_native import NativeTransportError, chat_stream
        from core.residency import inspect_loaded_models
        from core.turn_budget import StageTimeouts, TurnBudget

        async def _names():
            if inspector is not None:
                return tuple(await inspector())
            models, _err = await inspect_loaded_models()
            return tuple(m.name for m in models)

        before = await _names()
        budget = TurnBudget(total_s=timeout_s)
        timeouts = StageTimeouts(connect_s=5.0, first_token_s=timeout_s, idle_s=15.0,
                                 total_s=timeout_s)
        t0 = time.monotonic()
        first_ms = load_ms = tps = None
        eval_count = None
        ok = False
        err = None
        try:
            async for chunk in chat_stream(
                model=model, messages=[{"role": "user", "content": prompt}],
                think=False, max_tokens=max_tokens, temperature=0.0, budget=budget,
                timeouts=timeouts, ctx=2048, keep_alive=keep_alive,
            ):
                if chunk.content and first_ms is None:
                    first_ms = round((time.monotonic() - t0) * 1000.0, 1)
                if chunk.done:
                    ok = True
                    secs = chunk.load_seconds()
                    load_ms = round(secs * 1000.0, 1) if secs is not None else None
                    tps = chunk.tokens_per_second()
                    eval_count = chunk.eval_count
        except NativeTransportError as exc:
            err = exc.reason
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        after = await _names()
        return TrialMetrics(
            label=label, prompt=prompt, first_token_ms=first_ms,
            total_ms=round((time.monotonic() - t0) * 1000.0, 1), load_duration_ms=load_ms,
            tokens_per_second=tps, eval_count=eval_count, models_before=before,
            models_after=after, ok=ok, error=err)
    return _turn


def live_embedding_step(*, timeout_s: float = 30.0):
    """One embedding of a fixed controlled string; the vector is DISCARDED."""
    async def _step() -> TrialMetrics:
        import asyncio

        from core.residency import inspect_loaded_models

        t0 = time.monotonic()
        ok = False
        err = None
        try:
            from core.embedding_runtime import get_runtime

            res = await asyncio.wait_for(
                asyncio.to_thread(lambda: get_runtime().embed_text("benchmark probe")),
                timeout=timeout_s)
            ok = bool(getattr(res, "ok", False))
            if not ok:
                err = getattr(res, "error_class", None) or "embedding_failed"
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        models, _e = await inspect_loaded_models()
        return TrialMetrics(label="embedding",
                            total_ms=round((time.monotonic() - t0) * 1000.0, 1),
                            models_after=tuple(m.name for m in models), ok=ok, error=err)
    return _step


def live_cancel_step(*, model: str, cancel_after_s: float = 2.0):
    """Start a bounded generation and CANCEL it, so recovery can be measured."""
    async def _step() -> TrialMetrics:
        import asyncio

        from core.ollama_native import CancellationToken, chat_stream
        from core.turn_budget import StageTimeouts, TurnBudget

        token = CancellationToken()
        budget = TurnBudget(total_s=30.0)
        timeouts = StageTimeouts(connect_s=5.0, first_token_s=30.0, idle_s=10.0,
                                 total_s=30.0)
        t0 = time.monotonic()
        err = None
        try:
            async def _consume():
                async for _chunk in chat_stream(
                    model=model, messages=[{"role": "user", "content": PROMPT_CONCEPT}],
                    think=False, max_tokens=256, temperature=0.0, budget=budget,
                    timeouts=timeouts, ctx=2048, cancellation=token,
                ):
                    pass

            task = asyncio.ensure_future(_consume())
            await asyncio.sleep(cancel_after_s)
            token.cancel()
            await asyncio.wait_for(task, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        return TrialMetrics(label="cancelled", prompt=PROMPT_CONCEPT,
                            total_ms=round((time.monotonic() - t0) * 1000.0, 1),
                            cancelled=True, ok=True, error=err)
    return _step
