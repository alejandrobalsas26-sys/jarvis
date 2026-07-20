"""tests/test_fast_prewarm_v69_m564.py — V69 M56.4/M56.4.1 native full-path prewarm.

M55.1 warmed the DISPATCH path; the INFERENCE path stayed cold, so the operator's
first real question still paid an 11-19 s model activation. These tests lock the new
prewarm AND — just as importantly — everything it is forbidden to do: it is a
diagnostic, never a turn.
"""
from __future__ import annotations

import asyncio

import pytest

from core.fast_prewarm import (
    DEFAULT_MODE,
    FastPrewarm,
    PrewarmMetrics,
    PrewarmMode,
    PrewarmRecord,
    PrewarmState,
    get_fast_prewarm,
    parse_mode,
    reset_fast_prewarm,
)
from core.fast_readiness import FastReadiness, FastState

MODEL = "qwen3:8b"


def teardown_function(_):
    reset_fast_prewarm()


def _ok_runner(*, first_token_ms=1200.0, load_ms=9000.0, total_ms=1400.0):
    calls = {"n": 0, "kwargs": []}

    async def runner(**kw):
        calls["n"] += 1
        calls["kwargs"].append(kw)
        return PrewarmRecord(model=kw.get("model", MODEL), state=PrewarmState.READY,
                             first_token_ms=first_token_ms, load_duration_ms=load_ms,
                             total_ms=total_ms, success=True)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _fail_runner(reason="connect_failed:ConnectError"):
    async def runner(**kw):
        return PrewarmRecord(model=kw.get("model", MODEL), state=PrewarmState.FAILED,
                             failure_reason=reason)
    return runner


def _run(coro):
    return asyncio.run(coro)


# ── modes ────────────────────────────────────────────────────────────────────
def test_default_mode_is_background():
    assert DEFAULT_MODE is PrewarmMode.BACKGROUND


def test_mode_parsing_never_upgrades_a_typo_into_the_blocking_mode():
    assert parse_mode("off") is PrewarmMode.OFF
    assert parse_mode("BEFORE-TEXT-READY") is PrewarmMode.BEFORE_TEXT_READY
    assert parse_mode(PrewarmMode.OFF) is PrewarmMode.OFF
    for junk in ("", None, "yes", "BEFORE_TEXT", "aggressive"):
        assert parse_mode(junk) is PrewarmMode.BACKGROUND


def test_config_validator_clamps_mode_and_timeout():
    from core.config import Settings

    s = Settings(fast_prewarm_mode="nonsense", fast_prewarm_timeout_s=9999)
    assert s.fast_prewarm_mode == "BACKGROUND"
    assert s.fast_prewarm_timeout_s == 120.0
    assert Settings(fast_prewarm_timeout_s=0.1).fast_prewarm_timeout_s == 5.0
    assert Settings(fast_prewarm_mode="before-text-ready").fast_prewarm_mode == "BEFORE_TEXT_READY"


def test_off_mode_runs_no_generation():
    runner = _ok_runner()
    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.OFF, runner=runner)
    rec = _run(pw.run_once())
    assert rec.state is PrewarmState.DISABLED
    assert runner.calls["n"] == 0
    assert pw.state is PrewarmState.DISABLED
    assert pw.start_background() is None


def test_background_mode_returns_a_task_and_completes():
    runner = _ok_runner()
    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BACKGROUND, runner=runner)

    async def scenario():
        task = pw.start_background()
        assert task is not None
        await task
        return pw

    _run(scenario())
    assert pw.state is PrewarmState.READY
    assert runner.calls["n"] == 1


def test_background_start_is_idempotent_while_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_runner(**kw):
        started.set()
        await release.wait()
        return PrewarmRecord(model=MODEL, state=PrewarmState.READY, success=True)

    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BACKGROUND, runner=slow_runner)

    async def scenario():
        t1 = pw.start_background()
        await started.wait()
        t2 = pw.start_background()
        assert t1 is t2, "a second start must not stack a second cold load"
        release.set()
        await t1

    _run(scenario())
    assert pw.state is PrewarmState.READY


def test_before_text_ready_is_hard_bounded_and_degrades():
    """Even a runner that ignores its own timeout cannot hold the prompt closed."""
    async def hanging_runner(**kw):
        await asyncio.sleep(3600)

    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BEFORE_TEXT_READY,
                     timeout_s=5.0, runner=hanging_runner)

    async def scenario():
        # asyncio's virtual-time-free path: a 5s cap would really sleep, so we assert
        # the cap is applied by patching the wait to a tiny value.
        pw.timeout_s = 0.05
        return await pw.run_before_text_ready()

    rec = _run(scenario())
    assert rec.state is PrewarmState.TIMEOUT
    assert rec.failure_reason in ("before_text_ready_cap", "prewarm_timeout")
    # Degrades: the caller is free to open the prompt.
    assert pw.is_ready() is False


def test_before_text_ready_refuses_when_mode_is_background():
    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BACKGROUND, runner=_ok_runner())
    rec = _run(pw.run_before_text_ready())
    assert rec.state is PrewarmState.SKIPPED
    assert rec.failure_reason == "mode_not_before_text_ready"


def test_before_text_ready_success_marks_ready():
    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BEFORE_TEXT_READY,
                     runner=_ok_runner())
    rec = _run(pw.run_before_text_ready())
    assert rec.state is PrewarmState.READY and rec.success is True


# ── guards ───────────────────────────────────────────────────────────────────
def test_once_per_model_activation():
    runner = _ok_runner()
    pw = FastPrewarm(model=MODEL, runner=runner)
    first = _run(pw.run_once())
    second = _run(pw.run_once())
    assert first.state is PrewarmState.READY
    assert second.state is PrewarmState.SKIPPED
    assert "already_prewarmed" in (second.failure_reason or "")
    assert runner.calls["n"] == 1, "a restart loop must never stack cold loads"


def test_model_switch_rearms_the_guard():
    runner = _ok_runner()
    pw = FastPrewarm(model=MODEL, runner=runner)
    _run(pw.run_once())
    pw.note_model_switch("qwen3:14b")
    _run(pw.run_once())
    assert runner.calls["n"] == 2
    assert pw.model == "qwen3:14b"


def test_no_prewarm_after_stopping():
    runner = _ok_runner()
    pw = FastPrewarm(model=MODEL, runner=runner, is_stopping=lambda: True)
    rec = _run(pw.run_once())
    assert rec.state is PrewarmState.SKIPPED
    assert rec.failure_reason == "stopping"
    assert runner.calls["n"] == 0
    assert pw.start_background() is None


def test_no_prewarm_without_a_configured_model():
    runner = _ok_runner()
    rec = _run(FastPrewarm(model="", runner=runner).run_once())
    assert rec.state is PrewarmState.SKIPPED
    assert rec.failure_reason == "no_model_configured"
    assert runner.calls["n"] == 0


def test_concurrent_run_once_is_refused():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_runner(**kw):
        started.set()
        await release.wait()
        return PrewarmRecord(model=MODEL, state=PrewarmState.READY, success=True)

    pw = FastPrewarm(model=MODEL, runner=slow_runner)

    async def scenario():
        t = asyncio.ensure_future(pw.run_once())
        await started.wait()
        second = await pw.run_once()
        release.set()
        await t
        return second

    second = _run(scenario())
    assert second.state is PrewarmState.SKIPPED
    assert second.failure_reason == "already_running"


# ── cancellation ─────────────────────────────────────────────────────────────
def test_cancellation_is_recorded_and_does_not_consume_the_activation():
    started = asyncio.Event()

    async def hanging(**kw):
        started.set()
        await asyncio.sleep(3600)

    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BACKGROUND, runner=hanging)

    async def scenario():
        task = pw.start_background()
        await started.wait()
        await pw.cancel()
        assert task.done()
        # A cancelled attempt must not burn the once-per-activation guard.
        ok = _ok_runner()
        pw._runner = ok
        rec = await pw.run_once()
        return rec, ok

    rec, ok = _run(scenario())
    assert rec.state is PrewarmState.READY
    assert ok.calls["n"] == 1
    assert pw.metrics.cancellations >= 1


def test_cancel_is_safe_when_nothing_is_running():
    pw = FastPrewarm(model=MODEL, runner=_ok_runner())
    _run(pw.cancel())   # must not raise


# ── failure handling ─────────────────────────────────────────────────────────
def test_failure_is_bounded_and_recorded():
    pw = FastPrewarm(model=MODEL, runner=_fail_runner())
    rec = _run(pw.run_once())
    assert rec.state is PrewarmState.FAILED
    assert rec.success is False
    assert pw.metrics.failures == 1
    assert pw.metrics.last_failure_reason == "connect_failed:ConnectError"


def test_raising_runner_never_escapes():
    async def boom(**kw):
        raise RuntimeError("server exploded")

    pw = FastPrewarm(model=MODEL, runner=boom)
    rec = _run(pw.run_once())
    assert rec.state is PrewarmState.FAILED
    assert rec.failure_reason == "RuntimeError"


def test_background_supervisor_swallows_a_raising_runner():
    async def boom(**kw):
        raise RuntimeError("boom")

    pw = FastPrewarm(model=MODEL, mode=PrewarmMode.BACKGROUND, runner=boom)

    async def scenario():
        task = pw.start_background()
        await task              # must not raise into the event loop
        return task

    task = _run(scenario())
    assert task.exception() is None


# ── metrics: separate from real-turn metrics, and content-free ───────────────
def test_metrics_are_bounded_and_carry_no_content():
    pw = FastPrewarm(model=MODEL, runner=_ok_runner())
    _run(pw.run_once())
    snap = pw.snapshot()
    assert snap["successes"] == 1 and snap["attempts"] == 1
    assert snap["last_first_token_ms"] == 1200.0
    assert snap["last_load_ms"] == 9000.0
    flat = repr(snap).lower()
    for forbidden in ("prompt", "content", "message", "text", "hola"):
        assert forbidden not in flat


def test_skipped_attempts_do_not_inflate_the_attempt_count():
    m = PrewarmMetrics()
    m.fold(PrewarmRecord(state=PrewarmState.SKIPPED))
    m.fold(PrewarmRecord(state=PrewarmState.DISABLED))
    assert m.attempts == 0
    m.fold(PrewarmRecord(state=PrewarmState.READY, success=True))
    assert m.attempts == 1 and m.successes == 1


def test_prewarm_does_not_pollute_real_fast_turn_metrics():
    """The FAST-turn latency window is fed by REAL turns only; a prewarm records its
    own counters so a fast synthetic warm cannot flatter the operator's p50."""
    fast = FastReadiness(model=MODEL)
    pw = FastPrewarm(model=MODEL, runner=_ok_runner())
    _run(pw.run_once())
    fast.note_prewarm_result(pw.last)
    stats = fast.fast_inference_snapshot()
    assert stats["requests"] == 0
    assert stats["successes"] == 0
    assert stats["average_first_token_ms"] is None


# ── readiness integration (M56.8 truthfulness) ───────────────────────────────
def test_prewarm_started_is_not_readiness():
    fast = FastReadiness(model=MODEL)
    fast.note_prewarm_started()
    assert fast.state is FastState.PREWARMING
    assert fast.accepts_input() is True, "TEXT_READY is independent of model warmth"
    assert fast.state is not FastState.READY


def test_only_a_content_token_yields_ready():
    fast = FastReadiness(model=MODEL)
    fast.note_prewarm_started()
    fast.note_prewarm_result(PrewarmRecord(state=PrewarmState.READY, success=True))
    assert fast.state is FastState.READY


def test_failed_prewarm_degrades_to_warming_not_unavailable():
    fast = FastReadiness(model=MODEL)
    fast.note_prewarm_started()
    fast.note_prewarm_result(PrewarmRecord(state=PrewarmState.TIMEOUT,
                                           failure_reason="prewarm_timeout"))
    assert fast.state is FastState.WARMING
    assert fast.accepts_input() is True


def test_model_loading_state_accepts_input_and_has_a_specific_hint():
    fast = FastReadiness(model=MODEL)
    fast.note_model_loading()
    assert fast.state is FastState.MODEL_LOADING
    assert fast.accepts_input() is True
    hint = fast.warming_hint()
    assert hint and hint.isascii()
    assert "cargando" in hint


def test_prewarming_hint_differs_from_loading_hint():
    a, b = FastReadiness(model=MODEL), FastReadiness(model=MODEL)
    a.note_model_loading()
    b.note_prewarm_started()
    assert a.warming_hint() != b.warming_hint()


def test_ready_has_no_warming_hint():
    fast = FastReadiness(model=MODEL)
    fast.mark_served()
    assert fast.warming_hint() is None


def test_probe_does_not_downgrade_an_in_flight_prewarm():
    fast = FastReadiness(model=MODEL, base_url="http://127.0.0.1:1")  # unreachable
    fast.note_prewarm_started()
    _run(fast.probe())
    assert fast.state is FastState.PREWARMING


def test_skipped_prewarm_leaves_readiness_to_the_probes():
    fast = FastReadiness(model=MODEL)
    fast.note_prewarm_started()
    fast.note_prewarm_result(PrewarmRecord(state=PrewarmState.SKIPPED,
                                           failure_reason="mode_off"))
    assert fast.state is FastState.WARMING


# ── singleton ────────────────────────────────────────────────────────────────
def test_singleton_builds_from_config_and_is_resettable():
    pw = get_fast_prewarm()
    assert get_fast_prewarm() is pw
    assert pw.mode in set(PrewarmMode)
    reset_fast_prewarm()
    assert get_fast_prewarm() is not pw


@pytest.mark.parametrize("mode", list(PrewarmMode))
def test_snapshot_shape_is_stable_across_modes(mode):
    pw = FastPrewarm(model=MODEL, mode=mode, runner=_ok_runner())
    snap = pw.snapshot()
    for key in ("mode", "state", "model", "attempts", "successes", "failures",
                "cancellations", "last_load_ms", "last_first_token_ms",
                "last_total_ms", "last_failure_reason", "last"):
        assert key in snap
