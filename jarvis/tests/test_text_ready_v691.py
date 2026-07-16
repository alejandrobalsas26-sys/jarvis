"""
tests/test_text_ready_v691.py — V69 M54.1.8/.9: TEXT_READY is a real guarantee.

The live run logged:

    LIFECYCLE: TEXT_READY — interactive input enabled

very early in startup. The actual prompt appeared only after optional subsystem
registration, self-test, boot narration, briefing, MCP attachment, integrity
regeneration and Whisper warmup — `mark_text_ready()` fired at main.py:962 while the
real `input()` loop did not start until main.py:2188, ~1200 lines later, behind a
blocking LLM greeting.

The state was a claim about INTENT. These tests make it a claim about REACHABILITY.
"""
from __future__ import annotations

import asyncio

from core.fast_readiness import FastReadiness, FastState
from core.lifecycle import LifecycleManager, LifecycleState


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# -- The invariant -------------------------------------------------------------
def test_text_ready_is_unreachable_without_a_bound_reader():
    """THE regression. A lifecycle that cannot prove a reader must not claim one."""
    lm = LifecycleManager(clock=FakeClock())
    assert lm.mark_text_ready() is False
    assert lm.state is LifecycleState.STARTING
    assert lm.accepts_input() is False
    assert lm.input_available() is False


def test_text_ready_is_refused_while_the_reader_is_not_yet_live():
    """Binding is not readiness: the reader exists ~1200 lines before it reads."""
    lm = LifecycleManager(clock=FakeClock())
    live = {"v": False}
    lm.bind_input_reader(lambda: live["v"])
    assert lm.mark_text_ready() is False, "a constructible reader is not an available one"
    live["v"] = True
    assert lm.mark_text_ready() is True
    assert lm.state is LifecycleState.TEXT_READY


def test_at_text_ready_state_and_reader_agree():
    """The invariant: state == TEXT_READY implies input is genuinely available."""
    lm = LifecycleManager(clock=FakeClock())
    lm.bind_input_reader(lambda: True)
    lm.mark_text_ready()
    snap = lm.snapshot()
    assert snap["state"] == "TEXT_READY"
    assert snap["accepts_input"] is True
    assert snap["input_available"] is True, "a divergence here IS the old bug"


def test_optional_warmup_may_continue_after_text_ready():
    """Background warmup must not block or invalidate the prompt."""
    lm = LifecycleManager(clock=FakeClock())
    lm.bind_input_reader(lambda: True)
    lm.mark_text_ready()
    assert lm.can_start_task(), "optional warmup keeps running after TEXT_READY"
    assert lm.mark_core_ready() and lm.mark_operational()
    assert lm.accepts_input(), "input stays available through the rest of boot"


def test_a_raising_reader_probe_never_claims_readiness():
    lm = LifecycleManager(clock=FakeClock())

    def _boom():
        raise RuntimeError("probe exploded")

    lm.bind_input_reader(_boom)
    assert lm.input_available() is False
    assert lm.mark_text_ready() is False


def test_force_text_ready_is_available_for_headless_runs():
    """Voice-only/headless has no text reader; the escape hatch stays explicit."""
    lm = LifecycleManager(clock=FakeClock())
    assert lm.force_text_ready() is True
    assert lm.state is LifecycleState.TEXT_READY


def test_reader_going_dead_does_not_resurrect_a_stopping_runtime():
    lm = LifecycleManager(clock=FakeClock())
    lm.bind_input_reader(lambda: True)
    lm.mark_text_ready()
    lm.begin_stopping()
    assert lm.accepts_input() is False, "no input once shutdown begins"
    assert lm.mark_text_ready() is False


# -- FAST readiness (M54.1.8) --------------------------------------------------
def test_fast_starts_configured_not_ready():
    """A model NAME existing is not readiness — that was the false claim."""
    f = FastReadiness(model="qwen3:8b", clock=FakeClock())
    assert f.state is FastState.CONFIGURED
    assert f.accepts_input() is False, "CONFIGURED alone must not gate input open"


def test_probe_marks_unavailable_when_server_is_down():
    f = FastReadiness(model="qwen3:8b", base_url="http://127.0.0.1:1", clock=FakeClock())
    state = asyncio.run(f.probe())
    assert state is FastState.UNAVAILABLE
    assert f.snapshot()["last_error"] is not None
    assert f.snapshot()["last_probe_ms"] is not None


def test_model_matching_tolerates_the_latest_tag():
    f = FastReadiness(model="qwen2.5-coder:latest")
    assert f._model_present({"qwen2.5-coder:latest"})
    assert f._model_present({"qwen2.5-coder"}), "tagless server entry must still match"
    assert not f._model_present({"llama3:8b"})


def test_warming_still_accepts_input():
    """M54.1.9 — a warming model must not freeze the prompt; the turn's own
    deadline handles a slow answer."""
    f = FastReadiness(model="qwen3:8b", clock=FakeClock())
    f._state = FastState.WARMING
    assert f.accepts_input() is True
    f._state = FastState.DEGRADED
    assert f.accepts_input() is True, "a bounded failure beats refusing to listen"
    f._state = FastState.UNAVAILABLE
    assert f.accepts_input() is False


def test_prewarm_runs_at_most_once():
    """Repeated prewarms would each be a full cold load on a 15W CPU."""
    calls = []

    class _FakeCompletions:
        async def create(self, **kw):
            calls.append(kw)
            return object()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    async def _run():
        f = FastReadiness(model="qwen3:8b", clock=FakeClock())
        f._state = FastState.REACHABLE
        assert await f.prewarm(client=_FakeClient()) is FastState.READY
        assert await f.prewarm(client=_FakeClient()) is FastState.READY
        assert len(calls) == 1, "prewarm must be idempotent"
        assert calls[0]["max_tokens"] == 1, "boot prewarm must not be a real generation"

    asyncio.run(_run())


def test_failed_prewarm_degrades_but_still_allows_input():
    class _Boom:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    raise RuntimeError("model exploded")

    async def _run():
        f = FastReadiness(model="qwen3:8b", clock=FakeClock())
        f._state = FastState.REACHABLE
        state = await f.prewarm(client=_Boom())
        assert state is FastState.DEGRADED
        assert f.accepts_input() is True, "a broken model must not wedge the prompt"

    asyncio.run(_run())


def test_snapshot_shape_carries_no_user_content():
    f = FastReadiness(model="qwen3:8b", clock=FakeClock())
    snap = f.snapshot()
    assert set(snap) == {"state", "model", "last_probe_ms", "last_success_at",
                         "last_error", "accepts_input"}
    assert snap["model"] == "qwen3:8b"


def test_a_served_turn_is_the_strongest_readiness_evidence():
    f = FastReadiness(model="qwen3:8b", clock=FakeClock())
    f.mark_served()
    assert f.state is FastState.READY
    assert f.snapshot()["last_success_at"] is not None
