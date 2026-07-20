"""
tests/test_readiness_timing_v69_m553.py — V69 M55.3 truthful readiness + timing.

Two live-run lies this sub-milestone corrects:

  * "FAST_READINESS: UNAVAILABLE model=qwen3:8b" at 16:52:20 — emitted 19s BEFORE the
    native probe proved NATIVE_READY. A single timed-out metadata GET is not proof the
    model is dead; UNAVAILABLE now requires BOTH the metadata probe and the native
    transport probe to fail (and the server never reached);
  * "OPERATIONAL — text_ready=Nonems" while the prompt was, in fact, accepting input —
    boot reached CORE_READY before the reader started, so the monotonic mark_text_ready()
    no-oped and the phase was never stamped. text_ready_ms is now backfilled at the real
    reader-live moment and is never None once input is possible.
"""
from __future__ import annotations

import asyncio

from core.fast_readiness import FastReadiness, FastState
from core.lifecycle import LifecycleManager, LifecycleState
from core.ollama_native import NativeCapability, NativeProbeState


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _cap(state: NativeProbeState, *, streaming: bool = False) -> NativeCapability:
    return NativeCapability(
        state=state, model="qwen3:8b", streaming_ok=streaming,
        think_false_accepted=streaming, reasoning_omitted=streaming,
        server_version="0.32.0")


# ── FAST readiness: PROBING/WARMING before any verdict; UNAVAILABLE only if proven ─
def test_probe_sets_probing_then_warming_never_premature_unavailable():
    f = FastReadiness(model="qwen3:8b", base_url="http://127.0.0.1:1", clock=FakeClock())
    assert f.state is FastState.CONFIGURED
    state = asyncio.run(f.probe())            # server down
    assert state is FastState.WARMING         # inconclusive, NOT UNAVAILABLE
    assert f.accepts_input() is True


def test_reconcile_native_ready_is_ready_even_if_metadata_probe_failed():
    f = FastReadiness(model="qwen3:8b", base_url="http://127.0.0.1:1", clock=FakeClock())
    asyncio.run(f.probe())                    # WARMING (metadata timed out)
    assert f.reconcile(_cap(NativeProbeState.NATIVE_READY, streaming=True)) is FastState.READY


def test_reconcile_fallback_is_degraded_not_unavailable():
    f = FastReadiness(model="qwen3:8b", base_url="http://127.0.0.1:1", clock=FakeClock())
    asyncio.run(f.probe())
    assert f.reconcile(_cap(NativeProbeState.OPENAI_FALLBACK)) is FastState.DEGRADED


def test_reconcile_inconclusive_native_stays_warming():
    f = FastReadiness(model="qwen3:8b", base_url="http://127.0.0.1:1", clock=FakeClock())
    asyncio.run(f.probe())
    # Native probe not concluded — must not force UNAVAILABLE.
    assert f.reconcile(_cap(NativeProbeState.PROBING)) is FastState.WARMING


def test_reconcile_unavailable_only_when_server_never_reached():
    f = FastReadiness(model="qwen3:8b", base_url="http://127.0.0.1:1", clock=FakeClock())
    asyncio.run(f.probe())                    # WARMING, server never reached
    assert f._reached_server is False
    assert f.reconcile(_cap(NativeProbeState.UNAVAILABLE)) is FastState.UNAVAILABLE


def test_reconcile_reachable_server_with_bad_native_is_degraded():
    """If the metadata probe DID reach the server, a failed native transport is a
    transport problem (DEGRADED), not a dead server (UNAVAILABLE)."""
    f = FastReadiness(model="qwen3:8b", clock=FakeClock())
    f._state = FastState.REACHABLE            # simulate a successful metadata probe
    f._reached_server = True
    assert f.reconcile(_cap(NativeProbeState.UNAVAILABLE)) is FastState.DEGRADED


# ── TEXT_READY timing: backfilled truthfully, never None once the reader is live ──
def test_text_ready_is_none_until_reader_then_backfilled_after_core():
    """The exact live order: CORE_READY and OPERATIONAL reached BEFORE the reader
    starts. text_ready is None at OPERATIONAL (truthful — no reader yet), then stamped
    the instant the reader goes live."""
    clk = FakeClock()
    lm = LifecycleManager(clock=clk)
    lm.bind_input_reader(lambda: True)
    clk.advance(0.5)
    lm.stamp("CONSOLE_READY")
    clk.advance(12.0)
    lm.mark_core_ready()
    clk.advance(0.5)
    lm.mark_operational()
    # No reader yet — text_ready is legitimately None at the OPERATIONAL boot log.
    assert lm.snapshot()["text_ready_ms"] is None
    # The reader goes live (mark_text_ready no-ops past TEXT_READY; the stamp backfills).
    assert lm.mark_text_ready() is False       # monotonic: FSM is already OPERATIONAL
    clk.advance(0.1)
    ms = lm.note_reader_ready()
    snap = lm.snapshot()
    assert snap["text_ready_ms"] is not None    # NEVER None once the prompt accepts input
    assert snap["reader_ready_ms"] == ms
    assert snap["console_ready_ms"] == 500.0
    assert lm.accepts_input()                   # input is accepted across TEXT_READY..OPERATIONAL
    # Monotonic where appropriate (process <= console <= core <= operational).
    assert (snap["process_started_ms"] <= snap["console_ready_ms"]
            <= snap["core_ready_ms"] <= snap["operational_ready_ms"])
    assert snap["reader_ready_ms"] >= snap["console_ready_ms"]


def test_text_ready_stamped_early_when_reader_precedes_core():
    clk = FakeClock()
    lm = LifecycleManager(clock=clk)
    lm.bind_input_reader(lambda: True)
    clk.advance(0.2)
    assert lm.mark_text_ready() is True         # reached early -> advances + stamps
    assert lm.state is LifecycleState.TEXT_READY
    assert lm.note_reader_ready() == 200.0       # backfill is a no-op (already stamped)
    clk.advance(0.3)
    lm.mark_core_ready()
    snap = lm.snapshot()
    assert snap["text_ready_ms"] == 200.0
    assert snap["core_ready_ms"] == 500.0
    assert snap["text_ready_ms"] <= snap["core_ready_ms"]   # monotonic here


def test_reader_ready_first_write_wins():
    clk = FakeClock()
    lm = LifecycleManager(clock=clk)
    clk.advance(1.0)
    first = lm.note_reader_ready()
    clk.advance(5.0)
    second = lm.note_reader_ready()             # a later call must not move the stamp
    assert first == second == 1000.0
