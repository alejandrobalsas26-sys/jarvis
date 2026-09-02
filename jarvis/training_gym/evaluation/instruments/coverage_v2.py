"""training_gym/evaluation/instruments/coverage_v2.py — V69 M62 S4H.

THE AMBIGUITY THIS EXISTS TO END
--------------------------------
S4F's measurement was, factually:

    expected pairs                36
    fully generated pairs         36   (72 generations, both arms, none missing)
    fully scored pairs            36
    quality-comparable pairs      35   (one pair is a security regression, and a
                                        security regression has no meaningful reward
                                        delta, so it leaves the delta set)

The report said ``measured_pairs = 35`` and ``empirical_status = partial_live``.

Both of those come from one line. ``comparison.build_comparison`` sets
``measured_pairs = len(deltas)`` — the QUALITY denominator — and
``reports.classify_empirical_status`` then asks ``measured_pairs < task_count``. So a run
in which every task was generated and every task was scored is labelled as one where "a
real model ran, but not over the whole pack", because a pair left the *statistics* for a
reason that has nothing to do with execution.

Nothing was miscounted. The two questions were answered by one number:

    "did the experiment finish?"          — an EXECUTION question
    "how many pairs can the mean use?"    — a STATISTICAL question

WHAT V2 DOES
------------
It gives each question its own field and refuses to let them share one. Execution
completeness is derived from generation and scoring coverage ONLY; quality comparability
is a separate, separately-named judgement. A partition invariant then makes the
impossible states unrepresentable rather than merely unlikely.

THE HISTORICAL REPORT IS NOT AMENDED. S4F's ``partial_live`` stands in its receipt and
in its milestone document, and this module cannot reach either. What changes is that the
NEXT run cannot produce the same ambiguity.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ...schemas import SchemaError, canonical_json, sha256_obj

#: Bumped when a field is added or an invariant changes.
COVERAGE_SEMANTICS_VERSION = "m62.coverage_semantics.2"


class CoverageError(SchemaError):
    """A coverage accounting that describes a run that cannot exist."""


class ExecutionCoverage(str, Enum):
    """Did the experiment RUN to completion? Generation and scoring only."""

    #: Every expected pair was generated on both arms and scored.
    COMPLETE = "complete"
    #: A real run that did not reach every pair.
    INCOMPLETE = "incomplete"
    #: Nothing was generated at all.
    NONE = "none"

    @property
    def is_complete(self) -> bool:
        return self is ExecutionCoverage.COMPLETE


class QualityComparability(str, Enum):
    """How much of a COMPLETE run the quality statistic may speak for.

    Deliberately a separate enum from :class:`ExecutionCoverage`, and deliberately not
    orderable against it. A run can be execution-COMPLETE and quality-PARTIAL at the
    same time; that is the exact state S4F was in, and it needs two words.
    """

    #: Every classified pair carries a comparable reward delta.
    FULL = "full"
    #: Some classified pairs are non-comparable (a security verdict, an error class).
    PARTIAL = "partial"
    #: No pair is comparable; no quality claim is available at any confidence.
    NONE = "none"


@dataclass(frozen=True)
class CoverageAccounting:
    """Every pair, counted once, in exactly one place.

    The invariants in :meth:`__post_init__` are not defensive programming. Each one
    corresponds to a report that could otherwise be written and believed: a scored pair
    that was never generated, a comparable pair that was never classified, a partition
    whose parts do not sum to the whole.
    """

    expected_pairs: int
    fully_generated_pairs: int
    fully_scored_pairs: int
    classified_pairs: int
    comparable_quality_pairs: int
    noncomparable_classified_pairs: int
    security_blocking_pairs: int = 0
    failed_generation_pairs: int = 0
    timeout_pairs: int = 0
    truncated_pairs: int = 0
    schema_version: str = COVERAGE_SEMANTICS_VERSION

    def __post_init__(self) -> None:
        fields = ("expected_pairs", "fully_generated_pairs", "fully_scored_pairs",
                  "classified_pairs", "comparable_quality_pairs",
                  "noncomparable_classified_pairs", "security_blocking_pairs",
                  "failed_generation_pairs", "timeout_pairs", "truncated_pairs")
        for name in fields:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CoverageError(f"coverage: {name} must be a non-negative integer")
        for lower, upper in (("fully_generated_pairs", "expected_pairs"),
                             ("fully_scored_pairs", "fully_generated_pairs"),
                             ("classified_pairs", "fully_scored_pairs"),
                             ("comparable_quality_pairs", "classified_pairs"),
                             ("security_blocking_pairs", "classified_pairs"),
                             ("noncomparable_classified_pairs", "classified_pairs")):
            if getattr(self, lower) > getattr(self, upper):
                raise CoverageError(
                    f"coverage: {lower}={getattr(self, lower)} exceeds "
                    f"{upper}={getattr(self, upper)}; a pair cannot reach a later stage "
                    f"than it reached an earlier one")
        if (self.comparable_quality_pairs + self.noncomparable_classified_pairs
                != self.classified_pairs):
            raise CoverageError(
                f"coverage: {self.comparable_quality_pairs} comparable + "
                f"{self.noncomparable_classified_pairs} non-comparable != "
                f"{self.classified_pairs} classified; the partition leaves pairs in no "
                f"category, and a pair in no category is a pair nobody will look for")
        if self.failed_generation_pairs > (self.expected_pairs
                                           - self.fully_generated_pairs):
            raise CoverageError(
                "coverage: more pairs failed generation than went ungenerated")

    # -- the two questions, asked separately ------------------------------------
    def execution_coverage(self) -> ExecutionCoverage:
        """Did it finish? Reads generation and scoring. Never the quality denominator.

        This is the whole correction. ``comparable_quality_pairs`` is not consulted, so
        a security-blocking pair — which is a SCORED pair with a decided verdict — can
        no longer make a complete run look interrupted.
        """
        if self.expected_pairs == 0 or self.fully_generated_pairs == 0:
            return ExecutionCoverage.NONE
        if (self.fully_generated_pairs == self.expected_pairs
                and self.fully_scored_pairs == self.expected_pairs
                and self.classified_pairs == self.expected_pairs):
            return ExecutionCoverage.COMPLETE
        return ExecutionCoverage.INCOMPLETE

    def quality_comparability(self) -> QualityComparability:
        """How much of the run the quality statistic speaks for."""
        if self.classified_pairs == 0 or self.comparable_quality_pairs == 0:
            return QualityComparability.NONE
        if self.comparable_quality_pairs == self.classified_pairs:
            return QualityComparability.FULL
        return QualityComparability.PARTIAL

    def to_dict(self) -> dict:
        return {"classified_pairs": self.classified_pairs,
                "comparable_quality_pairs": self.comparable_quality_pairs,
                "execution_coverage": self.execution_coverage().value,
                "expected_pairs": self.expected_pairs,
                "failed_generation_pairs": self.failed_generation_pairs,
                "fully_generated_pairs": self.fully_generated_pairs,
                "fully_scored_pairs": self.fully_scored_pairs,
                "noncomparable_classified_pairs": self.noncomparable_classified_pairs,
                "quality_comparability": self.quality_comparability().value,
                "schema_version": self.schema_version,
                "security_blocking_pairs": self.security_blocking_pairs,
                "timeout_pairs": self.timeout_pairs,
                "truncated_pairs": self.truncated_pairs}

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")

    def coverage_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload) -> "CoverageAccounting":
        """Rebuild from :meth:`to_dict`. The two DERIVED fields are recomputed, never read.

        Reading them back would let a hand-edited report assert an execution status its
        own counts contradict, which is the class of defect this module exists to close.
        """
        if not isinstance(payload, dict):
            raise CoverageError("coverage: a serialized accounting must be a mapping")
        version = payload.get("schema_version")
        if version != COVERAGE_SEMANTICS_VERSION:
            raise CoverageError(
                f"coverage: schema_version {version!r} is not "
                f"{COVERAGE_SEMANTICS_VERSION}")
        try:
            return cls(
                expected_pairs=payload["expected_pairs"],
                fully_generated_pairs=payload["fully_generated_pairs"],
                fully_scored_pairs=payload["fully_scored_pairs"],
                classified_pairs=payload["classified_pairs"],
                comparable_quality_pairs=payload["comparable_quality_pairs"],
                noncomparable_classified_pairs=payload[
                    "noncomparable_classified_pairs"],
                security_blocking_pairs=payload.get("security_blocking_pairs", 0),
                failed_generation_pairs=payload.get("failed_generation_pairs", 0),
                timeout_pairs=payload.get("timeout_pairs", 0),
                truncated_pairs=payload.get("truncated_pairs", 0))
        except KeyError as exc:
            raise CoverageError(
                f"coverage: serialized accounting omits {exc.args[0]!r}") from None

    def __repr__(self) -> str:
        return (f"CoverageAccounting(expected={self.expected_pairs}, "
                f"generated={self.fully_generated_pairs}, "
                f"scored={self.fully_scored_pairs}, "
                f"comparable={self.comparable_quality_pairs}, "
                f"execution={self.execution_coverage().value}, "
                f"quality={self.quality_comparability().value})")


__all__ = ["COVERAGE_SEMANTICS_VERSION", "CoverageAccounting", "CoverageError",
           "ExecutionCoverage", "QualityComparability"]
