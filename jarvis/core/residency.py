"""core/residency.py — V69 M56.3: observed two-model residency verification.

THE QUESTION THIS ANSWERS
-------------------------
The desired steady state on this host is a PAIR of resident models:

    FAST       qwen3:8b               the interactive conversation model
    EMBEDDING  nomic-embed-text       semantic memory

M55 proved they do not coexist reliably: after the boot-time embedding load,
qwen3:8b was gone and the next interactive turn paid an 11-19 s cold activation.
But "they do not coexist" was an INFERENCE from a slow turn, not an observation.

This module observes it directly, and reports ONLY what it observed. It never
infers the server's configured slot count from a loaded-model count: a server with
room for four models that happens to hold one looks identical, at the API, to a
server pinned to a single slot. Slot count is not observable; EVICTION is.

THE SEQUENCE
------------
    1 inspect loaded models
    2 tiny native FAST request
    3 inspect
    4 one embedding request
    5 inspect
    6 tiny native FAST request
    7 inspect
    8 compare cold/warm timing

From that sequence, eviction is DERIVED from the observation series, not guessed:
if FAST was resident at step 3 and absent at step 5, the embedding request evicted
it — that is a behavioral observation (SERVER_BEHAVIOR_OBSERVED), and it is the
strongest statement anyone can make about this server without reading its source.

BOUNDS
------
Every request is tiny (num_predict<=4, one short embedding input), the whole run has
a hard deadline, and it is OPERATOR-TRIGGERED — never a boot step and never a loop.
It writes nothing to semantic memory: the embedding probe's vector is discarded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

_PS_PATH = "/api/ps"
_DEFAULT_STEP_TIMEOUT_S = 30.0
_DEFAULT_TOTAL_S = 180.0
# The probe generation is as small as it can be while still forcing a real load.
_PROBE_NUM_PREDICT = 4
_PROBE_PROMPT = "ok"
# A short deterministic string; its vector is computed and immediately discarded.
_EMBED_PROBE_TEXT = "residency probe"


class ResidencyState(str, Enum):
    UNKNOWN = "UNKNOWN"
    SINGLE_SLOT_OBSERVED = "SINGLE_SLOT_OBSERVED"      # only ever one model at a time
    DUAL_RESIDENT_OBSERVED = "DUAL_RESIDENT_OBSERVED"  # both seen loaded together
    FAST_EVICTED = "FAST_EVICTED"                      # FAST left after another load
    EMBEDDING_EVICTED = "EMBEDDING_EVICTED"
    RESIDENCY_UNSTABLE = "RESIDENCY_UNSTABLE"          # both evicted / flapping
    VERIFICATION_INCOMPLETE = "VERIFICATION_INCOMPLETE"  # a step failed or timed out


@dataclass(frozen=True)
class LoadedModel:
    """One model the server reports as resident. Only server-provided fields."""

    name: str
    size: int | None = None
    size_vram: int | None = None
    expires_at: str | None = None

    def snapshot(self) -> dict:
        return {"name": self.name, "size": self.size, "size_vram": self.size_vram,
                "expires_at": self.expires_at}


def model_matches(candidate: str, target: str) -> bool:
    """Tag-tolerant comparison: 'nomic-embed-text' matches 'nomic-embed-text:latest'."""
    if not candidate or not target:
        return False
    if candidate == target:
        return True
    return candidate.split(":", 1)[0] == target.split(":", 1)[0]


@dataclass(frozen=True)
class Observation:
    """One /api/ps inspection at a labelled point in the sequence."""

    step: str
    at: float
    models: tuple[LoadedModel, ...] = ()
    error: str | None = None

    def names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.models)

    def holds(self, target: str) -> bool:
        return any(model_matches(m.name, target) for m in self.models)

    def snapshot(self) -> dict:
        return {"step": self.step, "at": self.at, "error": self.error,
                "models": [m.snapshot() for m in self.models]}


@dataclass(frozen=True)
class StepTiming:
    """One timed action in the sequence."""

    step: str
    duration_ms: float | None = None
    load_ms: float | None = None
    first_token_ms: float | None = None
    ok: bool = False
    error: str | None = None

    def snapshot(self) -> dict:
        return {"step": self.step, "duration_ms": self.duration_ms,
                "load_ms": self.load_ms, "first_token_ms": self.first_token_ms,
                "ok": self.ok, "error": self.error}


@dataclass
class ResidencyReport:
    """The result of ONE verification run. Observation only — no configuration claim."""

    state: ResidencyState = ResidencyState.UNKNOWN
    fast_model: str = ""
    embedding_model: str = ""
    observations: list[Observation] = field(default_factory=list)
    timings: list[StepTiming] = field(default_factory=list)
    verified_at: float = 0.0
    complete: bool = False
    notes: list[str] = field(default_factory=list)

    # ── derived evidence ─────────────────────────────────────────────────────
    def timing(self, step: str) -> StepTiming | None:
        for t in self.timings:
            if t.step == step:
                return t
        return None

    def observation(self, step: str) -> Observation | None:
        for o in self.observations:
            if o.step == step:
                return o
        return None

    def dual_resident_seen(self) -> bool:
        """True when ONE observation held both models at the same moment."""
        return any(o.holds(self.fast_model) and o.holds(self.embedding_model)
                   for o in self.observations if o.error is None)

    def fast_evicted_by_embedding(self) -> bool:
        """FAST was resident after its request and gone after the embedding one."""
        before = self.observation("after_fast_1")
        after = self.observation("after_embedding")
        if before is None or after is None or before.error or after.error:
            return False
        return before.holds(self.fast_model) and not after.holds(self.fast_model)

    def embedding_evicted_by_fast(self) -> bool:
        before = self.observation("after_embedding")
        after = self.observation("after_fast_2")
        if before is None or after is None or before.error or after.error:
            return False
        return before.holds(self.embedding_model) and not after.holds(self.embedding_model)

    def reload_cost_ms(self) -> float | None:
        """How much the SECOND FAST request cost relative to the first.

        Positive means the post-embedding request was slower — the observable price
        of an eviction. Reported only when both requests produced a timing.
        """
        a, b = self.timing("fast_1"), self.timing("fast_2")
        if a is None or b is None or a.duration_ms is None or b.duration_ms is None:
            return None
        return round(b.duration_ms - a.duration_ms, 1)

    def max_models_seen(self) -> int:
        return max((len(o.models) for o in self.observations if o.error is None),
                   default=0)

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "fast_model": self.fast_model,
            "embedding_model": self.embedding_model,
            "verified_at": self.verified_at,
            "complete": self.complete,
            "dual_resident_seen": self.dual_resident_seen(),
            "fast_evicted_by_embedding": self.fast_evicted_by_embedding(),
            "embedding_evicted_by_fast": self.embedding_evicted_by_fast(),
            "max_models_seen": self.max_models_seen(),
            "reload_cost_ms": self.reload_cost_ms(),
            "observations": [o.snapshot() for o in self.observations],
            "timings": [t.snapshot() for t in self.timings],
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        return (
            "RESIDENCY: state={} fast={} embedding={} dual_seen={} "
            "fast_evicted={} max_models_seen={} reload_cost_ms={}".format(
                self.state.value, self.fast_model or "?", self.embedding_model or "?",
                self.dual_resident_seen(), self.fast_evicted_by_embedding(),
                self.max_models_seen(), self.reload_cost_ms(),
            )
        )


def classify_residency(report: ResidencyReport) -> ResidencyState:
    """Derive the residency state from the OBSERVATION SERIES. Pure and total.

    The ordering encodes what is most informative to the operator:

      * an incomplete run can conclude nothing (VERIFICATION_INCOMPLETE);
      * RESIDENCY_UNSTABLE is reserved for the genuinely confusing case — the two
        models WERE seen resident together at some instant, and an eviction happened
        anyway. That is flapping, and it is not explained by capacity;
      * both seen together with no eviction is the good outcome;
      * FAST eviction outranks EMBEDDING eviction because FAST is the interactive
        path: "your conversation model was unloaded" is the operator's headline even
        when the embedding model was swapped out later too. A mutually-exclusive
        swap (each request evicting the other, never two at once) is the classic
        one-slot signature and is NOTED as such — but it is still reported as an
        observation, never as proof of OLLAMA_MAX_LOADED_MODELS=1;
      * never more than one model at any instant, with no eviction detected, is
        SINGLE_SLOT_OBSERVED — again an OBSERVATION. A server with four free slots
        that happens to hold one model is indistinguishable from a pinned one here.
    """
    if not report.complete:
        return ResidencyState.VERIFICATION_INCOMPLETE
    usable = [o for o in report.observations if o.error is None]
    if len(usable) < 2:
        return ResidencyState.VERIFICATION_INCOMPLETE
    fast_out = report.fast_evicted_by_embedding()
    embed_out = report.embedding_evicted_by_fast()
    dual = report.dual_resident_seen()
    if dual and (fast_out or embed_out):
        return ResidencyState.RESIDENCY_UNSTABLE
    if dual:
        return ResidencyState.DUAL_RESIDENT_OBSERVED
    if fast_out:
        return ResidencyState.FAST_EVICTED
    if embed_out:
        return ResidencyState.EMBEDDING_EVICTED
    if report.max_models_seen() <= 1:
        return ResidencyState.SINGLE_SLOT_OBSERVED
    return ResidencyState.UNKNOWN


def mutually_exclusive_observed(report: ResidencyReport) -> bool:
    """True when each model's request evicted the other and the two were NEVER seen
    resident together. This is what a single model slot looks like from outside —
    reported as a behavioral observation, not as a configuration reading."""
    return (report.fast_evicted_by_embedding() and report.embedding_evicted_by_fast()
            and not report.dual_resident_seen() and report.max_models_seen() <= 1)


# ── Live inspection (bounded, read-only) ─────────────────────────────────────
async def inspect_loaded_models(*, base_url: str | None = None, client=None,
                                timeout_s: float = 5.0) -> tuple[list[LoadedModel], str | None]:
    """GET /api/ps. Returns (models, error). Never raises, never loads anything."""
    import httpx

    if base_url is None:
        try:
            from core.ollama_native import default_base_url
            base_url = default_base_url()
        except Exception:  # noqa: BLE001
            base_url = "http://127.0.0.1:11434"
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    try:
        r = await client.get(base_url.rstrip("/") + _PS_PATH, timeout=timeout_s)
        if r.status_code != 200:
            return [], f"status_{r.status_code}"
        rows = (r.json() or {}).get("models") or []
        out = [
            LoadedModel(name=row.get("name") or row.get("model") or "",
                        size=row.get("size"), size_vram=row.get("size_vram"),
                        expires_at=row.get("expires_at"))
            for row in rows if isinstance(row, dict) and (row.get("name") or row.get("model"))
        ]
        return out, None
    except Exception as exc:  # noqa: BLE001
        return [], type(exc).__name__
    finally:
        if owns:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


class ResidencyVerifier:
    """Runs the bounded 8-step sequence. Every collaborator is injectable.

    ``fast_probe`` and ``embed_probe`` are awaited callables returning a
    :class:`StepTiming`; ``inspector`` returns ``(models, error)``. Tests drive the
    whole state machine with no server; the live wiring passes the real ones.
    """

    def __init__(self, *, fast_model: str, embedding_model: str,
                 inspector: Callable | None = None,
                 fast_probe: Callable | None = None,
                 embed_probe: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 total_budget_s: float = _DEFAULT_TOTAL_S) -> None:
        self.fast_model = fast_model
        self.embedding_model = embedding_model
        self._inspect = inspector
        self._fast = fast_probe
        self._embed = embed_probe
        self._clock = clock
        self._total_s = total_budget_s

    async def _observe(self, report: ResidencyReport, step: str) -> Observation:
        models, err = ([], "no_inspector")
        if self._inspect is not None:
            try:
                models, err = await self._inspect()
            except Exception as exc:  # noqa: BLE001
                models, err = [], type(exc).__name__
        obs = Observation(step=step, at=self._clock(), models=tuple(models), error=err)
        report.observations.append(obs)
        return obs

    async def _timed(self, report: ResidencyReport, step: str, fn) -> StepTiming:
        if fn is None:
            timing = StepTiming(step=step, ok=False, error="no_probe")
        else:
            try:
                timing = await fn()
            except Exception as exc:  # noqa: BLE001
                timing = StepTiming(step=step, ok=False, error=type(exc).__name__)
        report.timings.append(timing)
        return timing

    async def run(self) -> ResidencyReport:
        """Execute the sequence under a hard total deadline. Never raises."""
        report = ResidencyReport(fast_model=self.fast_model,
                                 embedding_model=self.embedding_model,
                                 verified_at=self._clock())
        deadline = self._clock() + self._total_s

        def expired() -> bool:
            return self._clock() >= deadline

        await self._observe(report, "initial")
        if expired():
            report.notes.append("total budget exhausted before the first request")
            report.state = classify_residency(report)
            return report
        await self._timed(report, "fast_1", self._fast)
        await self._observe(report, "after_fast_1")
        if expired():
            report.notes.append("total budget exhausted after the first FAST request")
            report.state = classify_residency(report)
            return report
        await self._timed(report, "embedding", self._embed)
        await self._observe(report, "after_embedding")
        if expired():
            report.notes.append("total budget exhausted after the embedding request")
            report.state = classify_residency(report)
            return report
        await self._timed(report, "fast_2", self._fast)
        await self._observe(report, "after_fast_2")

        # "Complete" means every step ran AND produced a usable result. A failed
        # probe leaves the run incomplete rather than yielding a confident verdict.
        report.complete = (
            len(report.observations) == 4
            and all(o.error is None for o in report.observations)
            and len(report.timings) == 3
            and all(t.ok for t in report.timings)
        )
        if not report.complete:
            failed = [t.step for t in report.timings if not t.ok]
            failed += [o.step for o in report.observations if o.error]
            if failed:
                report.notes.append("incomplete steps: " + ",".join(failed))
        report.state = classify_residency(report)
        if mutually_exclusive_observed(report):
            report.notes.append(
                "mutually exclusive residency observed: each request evicted the other "
                "and the two were never resident together. This is consistent with a "
                "single model slot, but slot count is NOT observable through the API")
        return report


# ── Live probe factories (bounded; used by the operator-triggered run) ───────
def live_inspector(*, base_url: str | None = None, client=None):
    async def _inspect():
        return await inspect_loaded_models(base_url=base_url, client=client)
    return _inspect


def live_fast_probe(*, model: str, step: str = "fast", keep_alive: str = "30m",
                    timeout_s: float = _DEFAULT_STEP_TIMEOUT_S):
    """A tiny native think=false generation — the smallest request that still forces
    the weights to become resident."""
    async def _probe() -> StepTiming:
        from core.ollama_native import NativeTransportError, chat_stream
        from core.turn_budget import StageTimeouts, TurnBudget

        budget = TurnBudget(total_s=timeout_s)
        timeouts = StageTimeouts(connect_s=5.0, first_token_s=timeout_s,
                                 idle_s=10.0, total_s=timeout_s)
        t0 = time.monotonic()
        first_ms = load_ms = None
        ok = False
        err = None
        try:
            async for chunk in chat_stream(
                model=model, messages=[{"role": "user", "content": _PROBE_PROMPT}],
                think=False, max_tokens=_PROBE_NUM_PREDICT, temperature=0.0,
                budget=budget, timeouts=timeouts, ctx=512, keep_alive=keep_alive,
            ):
                if chunk.content and first_ms is None:
                    first_ms = round((time.monotonic() - t0) * 1000.0, 1)
                if chunk.done:
                    ok = True
                    secs = chunk.load_seconds()
                    load_ms = round(secs * 1000.0, 1) if secs is not None else None
        except NativeTransportError as exc:
            err = exc.reason
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        return StepTiming(step=step, duration_ms=round((time.monotonic() - t0) * 1000.0, 1),
                          load_ms=load_ms, first_token_ms=first_ms, ok=ok, error=err)
    return _probe


def live_embedding_probe(*, step: str = "embedding",
                         timeout_s: float = _DEFAULT_STEP_TIMEOUT_S):
    """One embedding of a short deterministic string. The vector is DISCARDED — this
    probe never writes to a semantic collection and leaves no user record."""
    async def _probe() -> StepTiming:
        import asyncio

        t0 = time.monotonic()
        ok = False
        err = None
        try:
            from core.embedding_runtime import get_runtime

            def _embed():
                return get_runtime().embed_text(_EMBED_PROBE_TEXT)

            result = await asyncio.wait_for(asyncio.to_thread(_embed), timeout=timeout_s)
            ok = bool(getattr(result, "ok", False))
            if not ok:
                err = getattr(result, "error_class", None) or "embedding_failed"
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        return StepTiming(step=step, duration_ms=round((time.monotonic() - t0) * 1000.0, 1),
                          ok=ok, error=err)
    return _probe


async def verify_residency(*, fast_model: str | None = None,
                           embedding_model: str | None = None,
                           total_budget_s: float = _DEFAULT_TOTAL_S) -> ResidencyReport:
    """Operator-triggered live verification with the real probes. Bounded; never
    raises; never runs at boot and never on a schedule tighter than the operator's."""
    if fast_model is None:
        try:
            from core.model_router import ModelRole, model_for_role
            fast_model = model_for_role(ModelRole.FAST) or ""
        except Exception:  # noqa: BLE001
            fast_model = ""
    if embedding_model is None:
        try:
            from core.model_router import resolve_embedding_model
            embedding_model = resolve_embedding_model()
        except Exception:  # noqa: BLE001
            embedding_model = ""
    verifier = ResidencyVerifier(
        fast_model=fast_model, embedding_model=embedding_model,
        inspector=live_inspector(),
        fast_probe=live_fast_probe(model=fast_model),
        embed_probe=live_embedding_probe(),
        total_budget_s=total_budget_s,
    )
    return await verifier.run()


# ── Bounded residency metrics for runtime health ─────────────────────────────
@dataclass
class ResidencyMetrics:
    """Bounded counters the runtime updates as it observes residency changes.

    No prompts, no content — names, counters and timestamps only.
    """

    observed_models: tuple[str, ...] = ()
    preferred_models: tuple[str, ...] = ()
    residency_state: str = ResidencyState.UNKNOWN.value
    fast_evictions: int = 0
    embedding_evictions: int = 0
    restoration_attempts: int = 0
    restoration_successes: int = 0
    last_switch_reason: str | None = None
    last_observation_at: float | None = None
    last_verification_at: float | None = None

    def note_observation(self, names, *, at: float | None = None) -> None:
        self.observed_models = tuple(names)
        self.last_observation_at = at if at is not None else time.time()

    def note_eviction(self, role: str, *, reason: str = "") -> None:
        if role == "fast":
            self.fast_evictions += 1
        elif role == "embedding":
            self.embedding_evictions += 1
        self.last_switch_reason = reason or role

    def note_restoration(self, *, success: bool) -> None:
        self.restoration_attempts += 1
        if success:
            self.restoration_successes += 1

    def note_report(self, report: ResidencyReport) -> None:
        self.residency_state = report.state.value
        self.last_verification_at = report.verified_at
        if report.fast_evicted_by_embedding():
            self.note_eviction("fast", reason="embedding_request")
        if report.embedding_evicted_by_fast():
            self.note_eviction("embedding", reason="fast_request")
        last = report.observations[-1] if report.observations else None
        if last is not None and last.error is None:
            self.note_observation(last.names(), at=last.at)

    def snapshot(self) -> dict:
        return {
            "observed_models": list(self.observed_models),
            "preferred_models": list(self.preferred_models),
            "residency_state": self.residency_state,
            "fast_evictions": self.fast_evictions,
            "embedding_evictions": self.embedding_evictions,
            "restoration_attempts": self.restoration_attempts,
            "restoration_successes": self.restoration_successes,
            "last_switch_reason": self.last_switch_reason,
            "last_observation_at": self.last_observation_at,
            "last_verification_at": self.last_verification_at,
        }


_metrics: ResidencyMetrics | None = None


def get_residency_metrics() -> ResidencyMetrics:
    global _metrics
    if _metrics is None:
        preferred: tuple[str, ...] = ()
        try:
            from core.model_router import ModelRole, model_for_role, resolve_embedding_model
            preferred = (model_for_role(ModelRole.FAST) or "", resolve_embedding_model())
        except Exception:  # noqa: BLE001
            pass
        _metrics = ResidencyMetrics(preferred_models=tuple(p for p in preferred if p))
    return _metrics


def reset_residency_metrics(instance: ResidencyMetrics | None = None) -> None:
    """Tests / a fresh process."""
    global _metrics
    _metrics = instance
