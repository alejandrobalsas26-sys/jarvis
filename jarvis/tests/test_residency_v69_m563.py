"""tests/test_residency_v69_m563.py — V69 M56.3 observed two-model residency.

M55 INFERRED that the embedding load evicts qwen3:8b, from a slow turn. M56.3
OBSERVES it through /api/ps across a bounded sequence, and these tests lock the
line between observation and inference:

  * a loaded-model COUNT is never treated as proof of OLLAMA_MAX_LOADED_MODELS;
  * a state is only reported when every step of the sequence actually succeeded;
  * eviction is derived from the observation series, naming WHICH model left;
  * the probes are tiny, bounded, and the embedding vector is discarded.
"""
from __future__ import annotations

import asyncio

from core.residency import (
    LoadedModel,
    Observation,
    ResidencyMetrics,
    ResidencyReport,
    ResidencyState,
    ResidencyVerifier,
    StepTiming,
    classify_residency,
    get_residency_metrics,
    model_matches,
    reset_residency_metrics,
)

FAST = "qwen3:8b"
EMBED = "nomic-embed-text:latest"


def teardown_function(_):
    reset_residency_metrics()


def _lm(name):
    return LoadedModel(name=name, size=5_200_000_000, expires_at="2026-07-20T12:00:00Z")


def _scripted_verifier(series, *, fast_ms=(1500.0, 1500.0), embed_ok=True,
                       fast_ok=True, embed_ms=300.0):
    """Build a verifier whose inspector replays ``series`` (a list of name-lists)."""
    calls = {"n": 0, "fast": 0}

    async def inspector():
        idx = min(calls["n"], len(series) - 1)
        calls["n"] += 1
        entry = series[idx]
        if entry is None:
            return [], "ConnectError"
        return [_lm(n) for n in entry], None

    async def fast_probe():
        i = min(calls["fast"], len(fast_ms) - 1)
        calls["fast"] += 1
        return StepTiming(step=f"fast_{i + 1}", duration_ms=fast_ms[i],
                          first_token_ms=fast_ms[i] * 0.8, ok=fast_ok,
                          error=None if fast_ok else "timeout")

    async def embed_probe():
        return StepTiming(step="embedding", duration_ms=embed_ms, ok=embed_ok,
                          error=None if embed_ok else "timeout")

    t = [0.0]

    def clock():
        t[0] += 0.1
        return t[0]

    return ResidencyVerifier(fast_model=FAST, embedding_model=EMBED,
                             inspector=inspector, fast_probe=fast_probe,
                             embed_probe=embed_probe, clock=clock)


def _run(verifier):
    return asyncio.run(verifier.run())


# ── name matching ────────────────────────────────────────────────────────────
def test_model_matching_is_tag_tolerant():
    assert model_matches("nomic-embed-text:latest", "nomic-embed-text")
    assert model_matches("qwen3:8b", "qwen3:8b")
    assert not model_matches("qwen3:14b", "qwen2.5-coder:latest")
    assert not model_matches("", FAST)


# ── the eight-step sequence ──────────────────────────────────────────────────
def test_dual_residency_observed_when_both_are_loaded_together():
    report = _run(_scripted_verifier([
        [FAST], [FAST], [FAST, EMBED], [FAST, EMBED],
    ]))
    assert report.complete is True
    assert report.state is ResidencyState.DUAL_RESIDENT_OBSERVED
    assert report.dual_resident_seen() is True
    assert report.fast_evicted_by_embedding() is False
    assert report.max_models_seen() == 2


def test_fast_eviction_after_embedding_is_detected():
    """The live M55 symptom: FAST resident, embedding runs, FAST is gone."""
    report = _run(_scripted_verifier(
        [[], [FAST], [EMBED], [FAST]], fast_ms=(1500.0, 14000.0)))
    assert report.state is ResidencyState.FAST_EVICTED
    assert report.fast_evicted_by_embedding() is True
    assert report.dual_resident_seen() is False
    # The observable price of that eviction, measured rather than asserted.
    assert report.reload_cost_ms() == 12500.0


def test_mutually_exclusive_swap_is_noted_but_never_called_a_slot_count():
    """Each request evicts the other and the two are never seen together — the classic
    one-slot signature. It must be NOTED as consistent with a single slot and still
    reported as an observation."""
    report = _run(_scripted_verifier([[], [FAST], [EMBED], [FAST]]))
    assert report.embedding_evicted_by_fast() is True
    assert report.fast_evicted_by_embedding() is True
    # FAST is the interactive path, so its eviction is the headline state.
    assert report.state is ResidencyState.FAST_EVICTED
    note = " ".join(report.notes)
    assert "mutually exclusive residency observed" in note
    assert "NOT observable" in note


def test_residency_unstable_needs_dual_residency_plus_an_eviction():
    """Unstable means 'they fit together, yet an eviction happened anyway' — flapping
    that capacity does not explain. A plain mutually-exclusive swap is not that."""
    report = _run(_scripted_verifier([[FAST, EMBED], [FAST], [EMBED], [FAST]]))
    assert report.dual_resident_seen() is True
    assert report.fast_evicted_by_embedding() is True
    assert report.state is ResidencyState.RESIDENCY_UNSTABLE


def test_single_slot_observed_is_worded_as_observation_only():
    report = _run(_scripted_verifier([[FAST], [FAST], [FAST], [FAST]]))
    # Never more than one model at any instant and no eviction between the labelled
    # steps -> SINGLE_SLOT_OBSERVED. Still not proof of MAX_LOADED_MODELS=1.
    assert report.state is ResidencyState.SINGLE_SLOT_OBSERVED
    assert report.max_models_seen() == 1


def test_loaded_model_count_is_never_slot_count_proof():
    """One model resident on a server with four slots is indistinguishable, at the
    API, from a server pinned to one. The report must not claim a configured value."""
    report = _run(_scripted_verifier([[FAST], [FAST], [FAST], [FAST]]))
    snap = report.snapshot()
    flat = repr(snap).lower()
    assert "max_loaded_models" not in flat
    assert "num_parallel" not in flat
    assert "slot_count" not in flat
    assert snap["state"] == "SINGLE_SLOT_OBSERVED"


# ── incompleteness must not become confidence ────────────────────────────────
def test_failed_fast_probe_leaves_verification_incomplete():
    report = _run(_scripted_verifier([[FAST], [FAST], [FAST, EMBED], [FAST, EMBED]],
                                     fast_ok=False))
    assert report.complete is False
    assert report.state is ResidencyState.VERIFICATION_INCOMPLETE
    assert any("incomplete steps" in n for n in report.notes)


def test_failed_embedding_probe_leaves_verification_incomplete():
    report = _run(_scripted_verifier([[FAST], [FAST], [FAST], [FAST]], embed_ok=False))
    assert report.state is ResidencyState.VERIFICATION_INCOMPLETE


def test_inspector_error_leaves_verification_incomplete():
    report = _run(_scripted_verifier([[FAST], None, [FAST], [FAST]]))
    assert report.complete is False
    assert report.state is ResidencyState.VERIFICATION_INCOMPLETE


def test_missing_probes_do_not_raise():
    v = ResidencyVerifier(fast_model=FAST, embedding_model=EMBED)
    report = _run(v)
    assert report.state is ResidencyState.VERIFICATION_INCOMPLETE
    assert report.complete is False


def test_raising_probe_is_captured_not_propagated():
    async def boom():
        raise RuntimeError("server exploded")

    async def inspector():
        return [_lm(FAST)], None

    v = ResidencyVerifier(fast_model=FAST, embedding_model=EMBED,
                          inspector=inspector, fast_probe=boom, embed_probe=boom)
    report = _run(v)
    assert report.state is ResidencyState.VERIFICATION_INCOMPLETE
    assert any(t.error == "RuntimeError" for t in report.timings)


def test_total_budget_stops_the_sequence_early():
    calls = {"n": 0}

    async def inspector():
        calls["n"] += 1
        return [_lm(FAST)], None

    async def slow():
        return StepTiming(step="fast", duration_ms=1.0, ok=True)

    t = [0.0]

    def clock():
        t[0] += 100.0     # each clock read burns 100 s of a 180 s budget
        return t[0]

    v = ResidencyVerifier(fast_model=FAST, embedding_model=EMBED, inspector=inspector,
                          fast_probe=slow, embed_probe=slow, clock=clock,
                          total_budget_s=180.0)
    report = asyncio.run(v.run())
    assert report.complete is False
    assert calls["n"] < 4, "the sequence must stop when the budget is exhausted"
    assert any("budget exhausted" in n for n in report.notes)


# ── pure classifier ──────────────────────────────────────────────────────────
def test_classifier_is_pure_and_total_on_an_empty_report():
    assert classify_residency(ResidencyReport()) is ResidencyState.VERIFICATION_INCOMPLETE


def test_classifier_needs_two_usable_observations():
    r = ResidencyReport(fast_model=FAST, embedding_model=EMBED, complete=True)
    r.observations.append(Observation(step="initial", at=0.0, models=(_lm(FAST),)))
    assert classify_residency(r) is ResidencyState.VERIFICATION_INCOMPLETE


# ── metrics ──────────────────────────────────────────────────────────────────
def test_metrics_fold_a_report_without_content():
    m = ResidencyMetrics(preferred_models=(FAST, EMBED))
    report = _run(_scripted_verifier([[], [FAST], [EMBED], [FAST]],
                                     fast_ms=(1500.0, 14000.0)))
    m.note_report(report)
    snap = m.snapshot()
    assert snap["residency_state"] == report.state.value
    assert snap["fast_evictions"] == 1
    assert snap["preferred_models"] == [FAST, EMBED]
    assert snap["observed_models"] == [FAST]
    # Bounded counters only — no prompt, no vector, no generated text.
    assert set(snap) == {
        "observed_models", "preferred_models", "residency_state", "fast_evictions",
        "embedding_evictions", "restoration_attempts", "restoration_successes",
        "last_switch_reason", "last_observation_at", "last_verification_at",
    }


def test_metrics_track_restoration_outcomes():
    m = ResidencyMetrics()
    m.note_restoration(success=False)
    m.note_restoration(success=True)
    assert m.snapshot()["restoration_attempts"] == 2
    assert m.snapshot()["restoration_successes"] == 1


def test_metrics_singleton_is_resettable():
    a = get_residency_metrics()
    a.note_eviction("fast", reason="unit-test")
    assert get_residency_metrics() is a
    reset_residency_metrics()
    assert get_residency_metrics() is not a


def test_summary_is_ascii_single_line():
    report = _run(_scripted_verifier([[FAST], [FAST], [FAST, EMBED], [FAST, EMBED]]))
    s = report.summary()
    assert s.isascii() and "\n" not in s
    assert "RESIDENCY:" in s
