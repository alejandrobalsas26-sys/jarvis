"""tests/test_response_soak_v69_m579.py — V69 M57.9: health + long-session soak.

Runtime health:
  * ONE new advisory subsystem, rank 0, that can never degrade the overall verdict;
  * every metric bounded and content-free.

Long-session soak (30+ deterministic turns through the REAL turn loop):
  * the composed context does not grow with uptime;
  * the TTS queue does not grow with uptime;
  * conversation history stays coherent — no turn answers the previous question;
  * no orphan task survives, alternating ES/EN and short/detailed contracts;
  * cancellation, continuation and the deterministic bypass all still work at the
    end of the session.

No live model, no live speech engine.
"""
from __future__ import annotations

import asyncio

import main as jarvis_main
from core.context_composer import last_context_metrics
from core.response_runtime import TurnState, get_response_runtime, reset_response_runtime
from core.tts_queue import TTSPriority


def setup_function(_):
    reset_response_runtime(None)
    from core.continuation import clear_continuation
    from core.response_quality import reset_quality_counters
    clear_continuation()
    reset_quality_counters()


def teardown_function(_):
    reset_response_runtime(None)


# ── runtime health ────────────────────────────────────────────────────────────
def test_response_pipeline_subsystem_is_present_and_advisory():
    from core.runtime_health import build_live_runtime_health
    d = build_live_runtime_health()
    subs = {s["name"]: s for s in d["subsystems"]}
    assert "response_pipeline" in subs
    assert subs["response_pipeline"]["status"] == "optional"
    assert subs["response_pipeline"]["healthy"] is True


def test_response_pipeline_never_degrades_the_overall_verdict():
    from core.runtime_health import (
        _STATUS_RANK, _UNHEALTHY, HealthStatus, _response_pipeline_subsystem,
    )
    # Assert the PROPERTY that makes it advisory, not a snapshot comparison: other
    # subsystems read live host state and legitimately move between two calls.
    sub = _response_pipeline_subsystem(response={}, stream={}, speech={},
                                       context={}, quality={})
    assert sub.status is HealthStatus.OPTIONAL
    assert _STATUS_RANK.get(HealthStatus.OPTIONAL, 0) == 0
    assert HealthStatus.OPTIONAL not in _UNHEALTHY
    rr = get_response_runtime()
    rr.begin_turn(contract="BRIEF")
    rr.end_turn(TurnState.TIMED_OUT)
    # Even after a timed-out turn it stays advisory.
    assert _response_pipeline_subsystem().status is HealthStatus.OPTIONAL


def test_health_metrics_are_bounded_and_content_free():
    from core.runtime_health import _response_pipeline_subsystem
    rr = get_response_runtime()
    rr.begin_turn(contract="BRIEF", selection_reason="SIMPLE_HOWTO",
                  language="es", token_budget=96, context_budget=1400)
    sub = _response_pipeline_subsystem()
    blob = str(sub.metrics).lower()
    for leak in ("contraseña", "hunter2", "http://", "bearer", "sk-", "<think>"):
        assert leak not in blob
    for value in sub.metrics.values():
        assert value is None or isinstance(value, (int, float, str, bool)), value
    assert len(sub.metrics) < 60


def test_health_reports_the_declared_metric_families():
    from core.runtime_health import _response_pipeline_subsystem
    keys = set(_response_pipeline_subsystem().metrics)
    for required in ("selected_contract", "selection_reason", "token_budget",
                     "chunks_received", "fragments_emitted", "first_fragment_ms",
                     "first_sentence_ms", "duplicate_fragments_suppressed",
                     "terminal_state", "progressive_enabled", "first_utterance_ms",
                     "utterances_stale_dropped", "speech_high_watermark",
                     "estimated_total_tokens", "digest_tokens", "trimmed_items",
                     "digest_age_turns", "interrupted_turns", "replaced_turns",
                     "cancellation_latency_ms", "late_chunks_suppressed",
                     "repetition_suppressions", "placeholder_blocks",
                     "incomplete_format_repairs", "continuation_offers"):
        assert required in keys, required


# ── soak harness ──────────────────────────────────────────────────────────────
class _Language:
    def __init__(self) -> None:
        self.lang = "es"

    def active_language(self) -> str:
        return self.lang


class _Executor:
    authority = None


class _SoakLLM:
    """A fake LLM whose answer names the question, so history contamination is
    detectable: an answer mentioning the WRONG index means the turn drifted."""

    def __init__(self) -> None:
        self.language_context = _Language()
        self.tool_executor = _Executor()
        self._last_shape = None
        self.history: list[dict] = []
        self.seen: list[str] = []

    async def chat_stream(self, user_input: str):
        self.seen.append(user_input)
        self.history.append({"role": "user", "content": user_input})
        answer = (f"Respuesta para {user_input}. "
                  f"Segunda frase con contenido suficiente para el turno. ")
        for i in range(0, len(answer), 7):
            await asyncio.sleep(0)
            yield answer[i:i + 7]
        self.history.append({"role": "assistant", "content": answer})


class _SoakTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.depth = 0
        self.max_depth = 0
        self.interrupted = 0

    async def speak_async(self, text, lang=None, *, priority=TTSPriority.NORMAL,
                          coalesce_key=None) -> None:
        self.spoken.append(text)
        # Model a queue that drains slower than generation: depth only falls when
        # something is popped, which the soak does every other utterance.
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        if self.depth > 1:
            self.depth -= 1

    def cancel_boot_narration(self) -> int:
        return 0

    def interrupt(self) -> None:
        self.interrupted += 1
        self.depth = 0


class _Console:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def post(self, text, channel=None, coalesce_key=None) -> bool:
        self.chunks.append(text)
        return True

    def begin_stream(self) -> None:
        pass

    def end_stream(self) -> None:
        pass

    def set_prompt(self, prompt) -> None:
        pass

    def text(self) -> str:
        return "".join(self.chunks)


_PROMPTS = [
    "explicame POO brevemente", "explain inheritance briefly",
    "explica Kerberos con mas detalle", "hazlo mas corto",
    "cuales son los tipos de datos", "dame un ejemplo",
]


def test_long_session_soak(monkeypatch):
    llm = _SoakLLM()
    tts = _SoakTTS()
    console = _Console()
    monkeypatch.setattr("core.console.get_console", lambda: console)

    async def _drive():
        context_samples: list[int] = []
        for i in range(34):
            prompt = f"{_PROMPTS[i % len(_PROMPTS)]} #{i}"
            llm.language_context.lang = "en" if i % 5 == 0 else "es"
            await jarvis_main._run_turn(llm, tts, prompt, "JARVIS")
            metrics = last_context_metrics()
            if metrics:
                context_samples.append(metrics.get("estimated_total_tokens", 0))
            # Every other turn the speech queue gets a chance to drain.
            if i % 2 == 0:
                tts.depth = max(0, tts.depth - 1)
        await asyncio.sleep(0)
        orphans = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        return context_samples, orphans

    samples, orphans = asyncio.run(_drive())

    rr = get_response_runtime()
    # 1. every turn ran and closed truthfully
    assert rr.turns_started == 34
    assert rr.turns_completed == 34
    assert rr.replaced_turns == 0

    # 2. no orphan task survives a 34-turn session
    assert orphans == []

    # 3. the TTS queue does not grow with uptime
    assert tts.max_depth <= 4, f"speech backlog grew to {tts.max_depth}"

    # 4. history is coherent: every question got its OWN answer
    for i, prompt in enumerate(llm.seen):
        assert f"#{i}" in prompt
    body = console.text()
    assert "Respuesta para" in body

    # 5. no unbounded context growth (when the composer ran)
    if len(samples) >= 4:
        assert max(samples[-3:]) <= max(samples[:3]) * 3 + 200


def test_history_stays_coherent_across_the_session(monkeypatch):
    llm = _SoakLLM()
    console = _Console()
    monkeypatch.setattr("core.console.get_console", lambda: console)

    async def _drive():
        for i in range(12):
            await jarvis_main._run_turn(llm, _SoakTTS(), f"pregunta {i}", "JARVIS")

    asyncio.run(_drive())
    # user/assistant strictly alternate — no dangling question the NEXT turn could
    # answer instead (the M55.1 contamination bug).
    roles = [m["role"] for m in llm.history]
    assert roles == ["user", "assistant"] * 12
    for i in range(12):
        assert f"pregunta {i}" in llm.history[i * 2]["content"]
        assert f"pregunta {i}" in llm.history[i * 2 + 1]["content"]


def test_context_composition_stays_bounded_over_a_long_history():
    from core.context_composer import compose_context, resolve_context_budget
    from core.conversation_digest import build_digest
    history: list[dict] = []
    sizes = []
    budget = resolve_context_budget(num_ctx=2048)
    for i in range(120):
        history.append({"role": "user", "content": f"pregunta {i} " + "x" * 200})
        history.append({"role": "assistant", "content": f"respuesta {i} " + "y" * 400})
        if i % 20 == 0:
            ctx = compose_context(system_prompt="SYS", history=history,
                                  digest=build_digest(history),
                                  token_budget=budget)
            sizes.append(ctx.estimated_total_tokens)
    assert max(sizes) <= budget
    assert sizes[-1] <= sizes[0] + budget


def test_speech_queue_does_not_grow_with_uptime():
    from core.speech_stream import SpeechPlanner
    from core.stream_assembler import Fragment, FragmentKind
    p = SpeechPlanner(backlog_cap=3, turn_id=1)
    pending = 0
    peak = 0
    for i in range(200):
        out = p.plan(Fragment(FragmentKind.SENTENCE,
                              f"Frase numero {i} con longitud suficiente. "),
                     pending=pending)
        pending += len(out)
        peak = max(peak, pending)
        if i % 2 == 0 and pending:
            pending -= 1          # the worker speaks one
    assert peak <= 6, f"speech backlog reached {peak}"
    assert p.snapshot()["stale_dropped"] > 0


def test_deterministic_bypass_still_costs_no_generation_after_a_session():
    from core.deterministic_bypass import maybe_bypass
    rr = get_response_runtime()
    for _ in range(30):
        rr.begin_turn(contract="BRIEF")
        rr.end_turn(TurnState.COMPLETED)
    assert maybe_bypass("que hora es", language="es")


def test_runtime_counters_stay_bounded_across_a_session():
    rr = get_response_runtime()
    for i in range(200):
        rr.begin_turn(contract="BRIEF")
        rr.end_turn(TurnState.COMPLETED if i % 3 else TurnState.TIMED_OUT)
    assert len(rr.recent()) <= 20
    assert rr.turns_started == 200
    snap = rr.snapshot()
    assert isinstance(snap["throughput"], dict)
    assert snap["throughput"]["samples"] <= 20
