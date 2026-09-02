"""V69 M62 S4H — Coverage V2: execution completeness is not the quality denominator.

WHAT THESE TESTS ARE FOR
------------------------
S4F's run generated every pair on both arms and scored every pair, and its report says
``partial_live``. Not because anything was miscounted, but because
``measured_pairs = len(deltas)`` is the QUALITY denominator and
``classify_empirical_status`` compares it against the task count. One security-blocking
pair leaves the delta set, and a complete execution is labelled incomplete.

The load-bearing test here is ``test_the_s4f_shape_is_execution_complete_and_quality_partial``:
36 / 36 / 36 with 35 comparable must be COMPLETE execution and PARTIAL quality, two
words rather than one.

The historical report is not amended by any of this. These tests assert the FUTURE
vocabulary, and a separate suite asserts that S4F's own record still says what it said.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training_gym.evaluation.instruments.coverage_v2 import (  # noqa: E402
    COVERAGE_SEMANTICS_VERSION,
    CoverageAccounting,
    CoverageError,
    ExecutionCoverage,
    QualityComparability,
)


def accounting(**overrides) -> CoverageAccounting:
    base = dict(expected_pairs=36, fully_generated_pairs=36, fully_scored_pairs=36,
                classified_pairs=36, comparable_quality_pairs=36,
                noncomparable_classified_pairs=0)
    base.update(overrides)
    return CoverageAccounting(**base)


# ══════════════════════════════════════════════════════════════════════════════
#  §38 / §84 THE CORRECTION
# ══════════════════════════════════════════════════════════════════════════════
def test_the_s4f_shape_is_execution_complete_and_quality_partial():
    """36 expected, 36 generated, 36 scored, 35 comparable, 1 security blocker."""
    coverage = accounting(comparable_quality_pairs=35,
                          noncomparable_classified_pairs=1,
                          security_blocking_pairs=1)
    assert coverage.execution_coverage() is ExecutionCoverage.COMPLETE
    assert coverage.quality_comparability() is QualityComparability.PARTIAL


def test_a_security_blocking_pair_never_reduces_execution_coverage():
    """The whole defect in one property: the veto is a verdict, not a missing pair."""
    for blockers in range(0, 6):
        coverage = accounting(comparable_quality_pairs=36 - blockers,
                              noncomparable_classified_pairs=blockers,
                              security_blocking_pairs=blockers)
        assert coverage.execution_coverage() is ExecutionCoverage.COMPLETE


def test_a_genuinely_interrupted_run_is_still_incomplete():
    """The correction must not make everything complete."""
    coverage = accounting(fully_generated_pairs=30, fully_scored_pairs=30,
                          classified_pairs=30, comparable_quality_pairs=30,
                          noncomparable_classified_pairs=0,
                          failed_generation_pairs=6)
    assert coverage.execution_coverage() is ExecutionCoverage.INCOMPLETE


def test_a_run_that_generated_everything_and_scored_less_is_incomplete():
    coverage = accounting(fully_scored_pairs=34, classified_pairs=34,
                          comparable_quality_pairs=34,
                          noncomparable_classified_pairs=0)
    assert coverage.execution_coverage() is ExecutionCoverage.INCOMPLETE


def test_a_run_that_generated_nothing_is_none_rather_than_incomplete():
    coverage = accounting(fully_generated_pairs=0, fully_scored_pairs=0,
                          classified_pairs=0, comparable_quality_pairs=0,
                          noncomparable_classified_pairs=0)
    assert coverage.execution_coverage() is ExecutionCoverage.NONE


def test_execution_and_quality_are_independent_axes():
    """Four combinations exist, so one word could never have carried both."""
    observed = set()
    for comparable in (0, 18, 36):
        coverage = accounting(comparable_quality_pairs=comparable,
                              noncomparable_classified_pairs=36 - comparable)
        observed.add((coverage.execution_coverage(),
                      coverage.quality_comparability()))
    assert {c for c, _ in observed} == {ExecutionCoverage.COMPLETE}
    assert {q for _, q in observed} == {QualityComparability.NONE,
                                        QualityComparability.PARTIAL,
                                        QualityComparability.FULL}


# ══════════════════════════════════════════════════════════════════════════════
#  §36 THE FIELDS
# ══════════════════════════════════════════════════════════════════════════════
def test_every_required_accounting_field_exists_and_is_serialized():
    payload = accounting().to_dict()
    for field in ("expected_pairs", "fully_generated_pairs", "fully_scored_pairs",
                  "classified_pairs", "comparable_quality_pairs",
                  "noncomparable_classified_pairs", "security_blocking_pairs",
                  "failed_generation_pairs", "timeout_pairs", "truncated_pairs"):
        assert field in payload, field


def test_the_two_derived_verdicts_are_serialized_under_distinct_names():
    payload = accounting(comparable_quality_pairs=35,
                         noncomparable_classified_pairs=1).to_dict()
    assert payload["execution_coverage"] == "complete"
    assert payload["quality_comparability"] == "partial"
    assert "measured_pairs" not in payload
    assert "empirical_status" not in payload


# ══════════════════════════════════════════════════════════════════════════════
#  §37 PARTITION INVARIANTS — impossible states are unrepresentable
# ══════════════════════════════════════════════════════════════════════════════
IMPOSSIBLE = [
    ("generated_exceeds_expected", dict(fully_generated_pairs=37)),
    ("scored_exceeds_generated", dict(fully_generated_pairs=30,
                                      fully_scored_pairs=36)),
    ("classified_exceeds_scored", dict(fully_scored_pairs=30, classified_pairs=36)),
    ("comparable_exceeds_classified", dict(classified_pairs=30,
                                           comparable_quality_pairs=36)),
    ("blockers_exceed_classified", dict(classified_pairs=2, comparable_quality_pairs=2,
                                        security_blocking_pairs=3)),
    ("partition_gap", dict(comparable_quality_pairs=35,
                           noncomparable_classified_pairs=0)),
    ("partition_overlap", dict(comparable_quality_pairs=36,
                               noncomparable_classified_pairs=1)),
    ("negative_count", dict(timeout_pairs=-1)),
    ("boolean_as_count", dict(truncated_pairs=True)),
]


@pytest.mark.parametrize("label,overrides", IMPOSSIBLE,
                         ids=[c[0] for c in IMPOSSIBLE])
def test_an_impossible_partition_is_refused(label, overrides):
    with pytest.raises(CoverageError):
        accounting(**overrides)


def test_the_partition_must_sum_exactly_and_the_message_says_why():
    with pytest.raises(CoverageError, match="pair in no category"):
        accounting(comparable_quality_pairs=30, noncomparable_classified_pairs=2)


# ══════════════════════════════════════════════════════════════════════════════
#  §39 BOUNDED PROPERTY TESTS — deterministic, fixed enumeration, no seed needed
# ══════════════════════════════════════════════════════════════════════════════
def test_every_valid_partition_over_a_bounded_grid_round_trips():
    """A full enumeration rather than a sample: deterministic by construction."""
    checked = 0
    for expected in range(0, 5):
        for generated in range(0, expected + 1):
            for scored in range(0, generated + 1):
                for classified in range(0, scored + 1):
                    for comparable in range(0, classified + 1):
                        coverage = CoverageAccounting(
                            expected_pairs=expected,
                            fully_generated_pairs=generated,
                            fully_scored_pairs=scored,
                            classified_pairs=classified,
                            comparable_quality_pairs=comparable,
                            noncomparable_classified_pairs=classified - comparable,
                            failed_generation_pairs=expected - generated)
                        rebuilt = CoverageAccounting.from_dict(coverage.to_dict())
                        assert rebuilt.canonical_bytes() == coverage.canonical_bytes()
                        checked += 1
    assert checked >= 100


def test_every_invalid_partition_over_a_bounded_grid_is_refused():
    refused = 0
    for expected, generated, scored in itertools.product(range(0, 4), repeat=3):
        if generated <= expected and scored <= generated:
            continue
        with pytest.raises(CoverageError):
            CoverageAccounting(
                expected_pairs=expected, fully_generated_pairs=generated,
                fully_scored_pairs=scored, classified_pairs=0,
                comparable_quality_pairs=0, noncomparable_classified_pairs=0)
        refused += 1
    assert refused >= 10


# ══════════════════════════════════════════════════════════════════════════════
#  Serialization
# ══════════════════════════════════════════════════════════════════════════════
def test_serialization_round_trips_to_identical_bytes():
    coverage = accounting(comparable_quality_pairs=35,
                          noncomparable_classified_pairs=1,
                          security_blocking_pairs=1, truncated_pairs=2)
    once = coverage.canonical_bytes()
    twice = CoverageAccounting.from_dict(coverage.to_dict()).canonical_bytes()
    thrice = CoverageAccounting.from_dict(
        CoverageAccounting.from_dict(coverage.to_dict()).to_dict()).canonical_bytes()
    assert once == twice == thrice


def test_the_derived_verdicts_are_recomputed_on_read_not_trusted():
    """A hand-edited report cannot assert a status its own counts contradict."""
    payload = accounting(comparable_quality_pairs=35,
                         noncomparable_classified_pairs=1).to_dict()
    payload["execution_coverage"] = "incomplete"
    payload["quality_comparability"] = "full"
    rebuilt = CoverageAccounting.from_dict(payload)
    assert rebuilt.execution_coverage() is ExecutionCoverage.COMPLETE
    assert rebuilt.quality_comparability() is QualityComparability.PARTIAL


def test_a_reader_refuses_a_schema_version_it_does_not_know():
    payload = {**accounting().to_dict(), "schema_version": "m62.coverage_semantics.9"}
    with pytest.raises(CoverageError, match="schema_version"):
        CoverageAccounting.from_dict(payload)


def test_the_coverage_hash_moves_when_a_count_moves():
    base = accounting()
    moved = accounting(comparable_quality_pairs=35,
                       noncomparable_classified_pairs=1)
    assert base.coverage_hash() != moved.coverage_hash()


def test_the_semantics_version_is_pinned():
    assert COVERAGE_SEMANTICS_VERSION == "m62.coverage_semantics.2"
    assert accounting().schema_version == COVERAGE_SEMANTICS_VERSION
