"""V69 M62 S3C — paired statistics, and the claims they will and will not support.

THE FAILURE MODE UNDER TEST
---------------------------
Not a wrong calculation. A correct calculation described in words it does not support:
"significantly better" from four tasks, or "no regression" from an interval that
comfortably contains one.

So most of these tests are about the VOCABULARY — what the verdict is allowed to be,
what the claim sentence is allowed to say, and what happens when the sample is too small
to say anything at all.
"""
from __future__ import annotations

import random

import pytest

from training_gym.evaluation.backends.fake import FakeMode
from training_gym.evaluation.comparison import ComparisonVerdict
from training_gym.evaluation.policy import (
    ErrorAccounting,
    StatisticalMethod,
    StatisticalPolicy,
)
from training_gym.evaluation.statistics import (
    PairedDelta,
    StatisticalVerdict,
    StatisticsError,
    deltas_from_scores,
    paired_statistics,
)

from _m62_evaluation_fixtures import make_pack, summarize

_POLICY = StatisticalPolicy()


def _deltas(pairs):
    return deltas_from_scores([(f"t{i}", b, c) for i, (b, c) in enumerate(pairs)])


def _uniform(n: int, baseline: float, candidate: float):
    return _deltas([(baseline, candidate)] * n)


# ══════════════════════════════════════════════════════════════════════════════
#  Determinism
# ══════════════════════════════════════════════════════════════════════════════
def test_the_same_data_and_seed_produce_the_same_interval():
    deltas = _deltas([(0.5, 0.7), (0.4, 0.4), (0.9, 0.6), (0.2, 0.5)] * 10)
    first = paired_statistics(deltas, policy=_POLICY)
    second = paired_statistics(deltas, policy=_POLICY)
    assert (first.ci_low, first.ci_high) == (second.ci_low, second.ci_high)
    assert first.report_hash() == second.report_hash()


def test_a_different_seed_produces_a_different_interval():
    deltas = _deltas([(0.5, 0.7), (0.4, 0.4), (0.9, 0.6), (0.2, 0.5)] * 10)
    first = paired_statistics(deltas, policy=_POLICY)
    second = paired_statistics(deltas, policy=StatisticalPolicy(bootstrap_seed=99))
    assert (first.ci_low, first.ci_high) != (second.ci_low, second.ci_high)


def test_the_bootstrap_never_touches_the_global_random_state():
    """A statistic whose value depended on what else ran first in the process would not
    be reproducible, and an irreproducible interval is not evidence."""
    random.seed(1234)
    before = random.getstate()
    paired_statistics(_uniform(40, 0.5, 0.7), policy=_POLICY)
    assert random.getstate() == before
    assert random.random() == pytest.approx(
        random.Random(1234).random())


def test_input_ordering_does_not_change_the_summary_statistics():
    pairs = [(0.5, 0.7), (0.4, 0.4), (0.9, 0.6), (0.2, 0.5), (0.1, 0.1)] * 8
    forward = paired_statistics(_deltas(pairs), policy=_POLICY)
    backward = paired_statistics(_deltas(list(reversed(pairs))), policy=_POLICY)
    assert forward.mean_delta == backward.mean_delta
    assert forward.median_delta == backward.median_delta
    assert (forward.wins, forward.ties, forward.losses) == \
        (backward.wins, backward.ties, backward.losses)


# ══════════════════════════════════════════════════════════════════════════════
#  Arithmetic
# ══════════════════════════════════════════════════════════════════════════════
def test_win_tie_and_loss_counts_are_correct():
    report = paired_statistics(
        _deltas([(0.2, 0.5), (0.5, 0.5), (0.8, 0.3), (0.1, 0.4)]), policy=_POLICY)
    assert (report.wins, report.ties, report.losses) == (2, 1, 1)
    assert report.n_pairs == 4


def test_the_mean_and_median_deltas_are_correct():
    report = paired_statistics(
        _deltas([(0.0, 0.1), (0.0, 0.2), (0.0, 0.3), (0.0, 0.8)]), policy=_POLICY)
    assert report.mean_delta == pytest.approx(0.35)
    assert report.median_delta == pytest.approx(0.25)


def test_identical_arms_produce_ties_and_a_zero_delta():
    report = paired_statistics(_uniform(40, 0.6, 0.6), policy=_POLICY)
    assert report.mean_delta == 0.0
    assert (report.wins, report.losses) == (0, 0)
    assert report.ties == 40


def test_a_uniform_improvement_yields_an_interval_that_excludes_the_margin():
    report = paired_statistics(_uniform(40, 0.4, 0.6), policy=_POLICY)
    assert report.verdict is StatisticalVerdict.SUFFICIENT
    assert report.observed_improvement is True
    assert report.excludes_regression_margin is True
    assert report.indicates_regression is False


def test_a_uniform_regression_is_reported_as_one():
    report = paired_statistics(_uniform(40, 0.7, 0.3), policy=_POLICY)
    assert report.indicates_regression is True
    assert report.excludes_regression_margin is False
    assert "below the policy regression margin" in report.claim()


# ══════════════════════════════════════════════════════════════════════════════
#  What may be claimed
# ══════════════════════════════════════════════════════════════════════════════
def test_zero_pairs_is_insufficient_evidence_and_not_a_zero_delta():
    report = paired_statistics((), policy=_POLICY, n_missing=12)
    assert report.verdict is StatisticalVerdict.INSUFFICIENT_EVIDENCE
    assert report.n_pairs == 0
    assert report.n_missing == 12
    assert "no difference has been measured" in report.claim()
    assert report.excludes_regression_margin is False


def test_a_tiny_sample_is_small_sample_however_large_the_observed_difference():
    """A 100% improvement over three tasks is still three tasks."""
    report = paired_statistics(_uniform(3, 0.0, 1.0), policy=_POLICY)
    assert report.verdict is StatisticalVerdict.SMALL_SAMPLE
    assert report.mean_delta == 1.0
    assert report.excludes_regression_margin is False
    assert "too small for a directional claim" in report.claim()


def test_a_single_pair_produces_no_spread_and_says_so():
    report = paired_statistics(_uniform(1, 0.2, 0.9), policy=_POLICY)
    assert report.verdict is StatisticalVerdict.SMALL_SAMPLE
    assert report.iterations == 0
    assert report.ci_low == report.ci_high


def test_the_word_significant_never_appears_in_a_claim():
    for deltas in (_uniform(40, 0.2, 0.9), _uniform(3, 0.2, 0.9), (),
                   _uniform(40, 0.9, 0.2)):
        report = paired_statistics(deltas, policy=_POLICY)
        assert "significant" not in report.claim().lower()
        assert "significant" not in " ".join(report.limitations).lower()


def test_no_p_value_is_computed_or_reported():
    payload = paired_statistics(_uniform(40, 0.2, 0.9), policy=_POLICY).to_dict()
    assert payload["p_value_reported"] is False
    assert not any("p_value" in k and k != "p_value_reported" for k in payload)


def test_the_verdict_vocabulary_contains_no_significance_claim():
    assert {v.value for v in StatisticalVerdict} == {
        "sufficient", "small_sample", "insufficient_evidence", "invalid"}


def test_every_report_says_its_thresholds_are_uncalibrated():
    report = paired_statistics(_uniform(40, 0.4, 0.6), policy=_POLICY)
    assert any("not been calibrated" in limitation
               for limitation in report.limitations)


# ══════════════════════════════════════════════════════════════════════════════
#  Missing pairs and bad input
# ══════════════════════════════════════════════════════════════════════════════
def test_missing_pairs_are_carried_into_the_report_rather_than_discarded():
    """'Twelve wins out of twelve' reads very differently once the reader knows forty
    tasks were attempted."""
    report = paired_statistics(_uniform(12, 0.4, 0.6), policy=_POLICY, n_missing=28)
    assert report.n_missing == 28
    assert any("produced no comparable pair" in limitation
               for limitation in report.limitations)


def test_a_non_finite_delta_invalidates_the_whole_report():
    report = paired_statistics(
        (PairedDelta(task_id="t", baseline=0.0, candidate=float("nan")),),
        policy=_POLICY)
    assert report.verdict is StatisticalVerdict.INVALID
    assert "could not be computed" in report.claim()


def test_a_malformed_delta_invalidates_the_report_rather_than_being_skipped():
    report = paired_statistics(({"task_id": "t"},), policy=_POLICY)  # type: ignore[arg-type]
    assert report.verdict is StatisticalVerdict.INVALID


def test_a_policy_of_the_wrong_type_is_refused():
    with pytest.raises(StatisticsError, match="StatisticalPolicy"):
        paired_statistics(_uniform(4, 0.1, 0.2), policy={"iterations": 100})  # type: ignore[arg-type]


def test_the_bootstrap_iteration_count_is_honoured_and_bounded():
    policy = StatisticalPolicy(bootstrap_iterations=250)
    assert paired_statistics(_uniform(40, 0.4, 0.6), policy=policy).iterations == 250
    from training_gym.evaluation.policy import MAX_BOOTSTRAP_ITERATIONS
    assert MAX_BOOTSTRAP_ITERATIONS <= 20_000


def test_the_only_implemented_method_is_the_one_the_policy_can_name():
    assert [m.value for m in StatisticalMethod] == ["paired_bootstrap_percentile"]
    assert _POLICY.method is StatisticalMethod.PAIRED_BOOTSTRAP_PERCENTILE


def test_the_error_accounting_policy_is_recorded_in_the_report():
    report = paired_statistics(_uniform(40, 0.4, 0.6), policy=_POLICY)
    assert report.error_accounting == ErrorAccounting.COUNT_AS_FAILURE.value
    assert report.to_dict()["error_accounting"] == "count_as_failure"


def test_the_report_serialization_is_deterministic():
    deltas = _uniform(40, 0.4, 0.6)
    assert paired_statistics(deltas, policy=_POLICY).to_dict() == \
        paired_statistics(deltas, policy=_POLICY).to_dict()


# ══════════════════════════════════════════════════════════════════════════════
#  Against a real comparison
# ══════════════════════════════════════════════════════════════════════════════
def test_a_candidate_improvement_is_represented_in_the_bootstrap():
    summary = summarize(FakeMode.CANDIDATE_IMPROVED)
    assert summary.bootstrap.observed_improvement is True
    assert summary.overall_delta is not None and summary.overall_delta > 0
    assert summary.wins > summary.losses


def test_a_candidate_regression_is_represented_in_the_bootstrap():
    summary = summarize(FakeMode.CANDIDATE_REGRESSED)
    assert summary.bootstrap.observed_improvement is False
    assert summary.losses > summary.wins


def test_a_security_regression_never_becomes_a_favourable_delta():
    """It is not filed as a loss, so it cannot be averaged against wins elsewhere; it
    is counted separately and blocks on its own."""
    summary = summarize(FakeMode.CANDIDATE_SECURITY_REGRESSION)
    assert summary.security_regressions > 0
    assert all(c.verdict is not ComparisonVerdict.REGRESSED
               for c in summary.comparisons if c.new_security_findings)


def test_a_timeout_regression_leaves_nothing_comparable_and_says_so():
    summary = summarize(FakeMode.CANDIDATE_TIMEOUT)
    assert summary.measured_pairs == 0
    assert summary.missing_pairs == summary.task_count
    assert summary.bootstrap.verdict is StatisticalVerdict.INSUFFICIENT_EVIDENCE


def test_a_latency_regression_does_not_change_the_quality_delta():
    """Latency is an operational fact, not a quality one; blending them would let a
    fast wrong answer beat a slow right one."""
    fast = summarize(FakeMode.IDENTICAL)
    slow = summarize(FakeMode.CANDIDATE_LATENCY_REGRESSION)
    assert fast.overall_delta == slow.overall_delta == 0.0
    base = slow.baseline_metrics.operational["median_latency_ms"]["value"]
    cand = slow.candidate_metrics.operational["median_latency_ms"]["value"]
    assert cand > base


def test_a_small_pack_cannot_support_a_directional_claim():
    summary = summarize(FakeMode.CANDIDATE_IMPROVED,
                        pack=make_pack(completions=2, refusals=1, safe=1))
    assert summary.bootstrap.verdict is StatisticalVerdict.SMALL_SAMPLE
    assert summary.bootstrap.excludes_regression_margin is False
