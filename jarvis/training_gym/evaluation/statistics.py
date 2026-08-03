"""training_gym/evaluation/statistics.py — V69 M62 S3C: what the numbers may be said to mean.

WHY THIS MODULE IS SMALL AND STRICT
-----------------------------------
The failure mode it exists to prevent is not a wrong calculation. It is a correct
calculation described in words it does not support: "the adapter is significantly better"
from four tasks, or "no regression" from a confidence interval that comfortably contains
one.

So the vocabulary is bounded. The verdict is one of four values, none of which is
"significant"; the interval is a percentile bootstrap over the paired per-task deltas;
the sample size is always reported; and below the policy's minimum the verdict is
``SMALL_SAMPLE`` no matter how large the observed difference is.

DETERMINISM
-----------
The bootstrap uses its own ``random.Random`` instance seeded from the policy. It never
touches the global random state — a statistic whose value depended on what else ran first
in the process would not be reproducible, and an irreproducible interval is not evidence.

NO p-VALUES
-----------
None is computed and none is reported. A percentile bootstrap interval answers "what
range of paired differences is consistent with this sample"; inventing a p-value from it
would be a claim about a null hypothesis this module never tested.
"""
from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..schemas import SchemaError, sha256_obj
from .policy import ErrorAccounting, StatisticalMethod, StatisticalPolicy

#: Bumped when a verdict's meaning changes.
STATISTICS_VERSION = "m62.evaluation_statistics.1"


class StatisticsError(SchemaError):
    """A statistic that would not support the claim made from it."""


class StatisticalVerdict(str, Enum):
    """What this sample can carry. Deliberately does not contain "significant"."""

    #: Enough pairs for the policy's claim, and the interval is informative.
    SUFFICIENT = "sufficient"
    #: A real measurement over too few pairs to support a directional claim.
    SMALL_SAMPLE = "small_sample"
    #: Nothing measurable — no pairs at all, or none that could be compared.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    #: The inputs were malformed; no number should be read from this at all.
    INVALID = "invalid"

    @property
    def supports_a_directional_claim(self) -> bool:
        return self is StatisticalVerdict.SUFFICIENT


@dataclass(frozen=True)
class PairedDelta:
    """One task's difference. The unit of evidence, because both arms saw this task."""

    task_id: str
    baseline: float
    candidate: float

    @property
    def delta(self) -> float:
        return round(self.candidate - self.baseline, 9)

    @property
    def outcome(self) -> str:
        if self.delta > 0:
            return "win"
        if self.delta < 0:
            return "loss"
        return "tie"


@dataclass(frozen=True)
class BootstrapReport:
    """A paired comparison, and the exact words it will support."""

    verdict: StatisticalVerdict
    n_pairs: int
    n_excluded: int
    n_missing: int
    mean_delta: float
    median_delta: float
    wins: int
    ties: int
    losses: int
    ci_low: float
    ci_high: float
    confidence_level: float
    iterations: int
    seed: int
    method: str
    regression_margin: float
    error_accounting: str
    limitations: tuple[str, ...] = ()

    # -- the claims, stated in words the method supports -----------------------
    @property
    def observed_improvement(self) -> bool:
        """An OBSERVED difference. Says nothing about whether it would replicate."""
        return self.mean_delta > 0.0

    @property
    def excludes_regression_margin(self) -> bool:
        """Whether the interval's lower bound clears the policy's regression margin.

        This is the strongest statement this module makes, and it is deliberately
        phrased as a property of the interval rather than as a property of the adapter.
        """
        return (self.verdict.supports_a_directional_claim
                and self.ci_low > -abs(self.regression_margin))

    @property
    def indicates_regression(self) -> bool:
        """The interval sits entirely below the negative margin."""
        return (self.verdict.supports_a_directional_claim
                and self.ci_high < -abs(self.regression_margin))

    def claim(self) -> str:
        """The one sentence a report is allowed to print about this comparison."""
        if self.verdict is StatisticalVerdict.INVALID:
            return "the paired statistics could not be computed from these inputs"
        if self.verdict is StatisticalVerdict.INSUFFICIENT_EVIDENCE:
            return ("no task produced a comparable pair, so no difference has been "
                    "measured")
        if self.verdict is StatisticalVerdict.SMALL_SAMPLE:
            return (f"observed paired mean delta {self.mean_delta:+.4f} over "
                    f"{self.n_pairs} pair(s); the sample is too small for a directional "
                    f"claim under this policy")
        if self.indicates_regression:
            return (f"observed paired mean delta {self.mean_delta:+.4f} over "
                    f"{self.n_pairs} pairs; the {self.confidence_level:.0%} interval "
                    f"[{self.ci_low:+.4f}, {self.ci_high:+.4f}] lies below the policy "
                    f"regression margin")
        if self.excludes_regression_margin and self.observed_improvement:
            return (f"observed paired improvement {self.mean_delta:+.4f} over "
                    f"{self.n_pairs} pairs; the {self.confidence_level:.0%} interval "
                    f"[{self.ci_low:+.4f}, {self.ci_high:+.4f}] excludes the policy "
                    f"regression margin")
        if self.excludes_regression_margin:
            return (f"observed paired mean delta {self.mean_delta:+.4f} over "
                    f"{self.n_pairs} pairs; the {self.confidence_level:.0%} interval "
                    f"excludes the policy regression margin but does not show an "
                    f"improvement")
        return (f"observed paired mean delta {self.mean_delta:+.4f} over "
                f"{self.n_pairs} pairs; the {self.confidence_level:.0%} interval "
                f"[{self.ci_low:+.4f}, {self.ci_high:+.4f}] does not exclude a "
                f"regression")

    def to_dict(self) -> dict:
        return {
            "statistics_version": STATISTICS_VERSION,
            "verdict": self.verdict.value,
            "n_pairs": self.n_pairs,
            "n_excluded": self.n_excluded,
            "n_missing": self.n_missing,
            "mean_delta": self.mean_delta,
            "median_delta": self.median_delta,
            "wins": self.wins, "ties": self.ties, "losses": self.losses,
            "ci_low": self.ci_low, "ci_high": self.ci_high,
            "confidence_level": self.confidence_level,
            "iterations": self.iterations, "seed": self.seed, "method": self.method,
            "regression_margin": self.regression_margin,
            "error_accounting": self.error_accounting,
            "observed_improvement": self.observed_improvement,
            "excludes_regression_margin": self.excludes_regression_margin,
            "indicates_regression": self.indicates_regression,
            "claim": self.claim(),
            "p_value_reported": False,
            "limitations": list(self.limitations),
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 9) if values else 0.0


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 9)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 9)


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile over an already-sorted sequence."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 9)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 9)


def paired_statistics(deltas: Sequence[PairedDelta], *, policy: StatisticalPolicy,
                      n_missing: int = 0, n_excluded: int = 0,
                      limitations: Sequence[str] = ()) -> BootstrapReport:
    """The paired comparison. Deterministic for a given input and seed.

    *n_missing* is the number of tasks that produced no comparable pair. It is carried
    into the report rather than discarded, because "twelve wins out of twelve" reads very
    differently once the reader knows forty tasks were attempted.
    """
    if not isinstance(policy, StatisticalPolicy):
        raise StatisticsError("paired statistics: policy must be a StatisticalPolicy")
    notes = list(limitations)
    notes.append("thresholds and the minimum sample size are initial policy values and "
                 "have not been calibrated against a measured distribution")

    values: list[float] = []
    wins = ties = losses = 0
    for pair in deltas:
        if not isinstance(pair, PairedDelta):
            return _invalid(policy, n_missing=n_missing, n_excluded=n_excluded,
                            limitations=tuple(notes + ["a delta was not a PairedDelta"]))
        delta = pair.delta
        if delta != delta or delta in (float("inf"), float("-inf")):
            return _invalid(policy, n_missing=n_missing, n_excluded=n_excluded,
                            limitations=tuple(notes + ["a delta was not finite"]))
        values.append(delta)
        wins += pair.outcome == "win"
        ties += pair.outcome == "tie"
        losses += pair.outcome == "loss"

    if not values:
        return BootstrapReport(
            verdict=StatisticalVerdict.INSUFFICIENT_EVIDENCE, n_pairs=0,
            n_excluded=n_excluded, n_missing=n_missing, mean_delta=0.0,
            median_delta=0.0, wins=0, ties=0, losses=0, ci_low=0.0, ci_high=0.0,
            confidence_level=policy.confidence_level, iterations=0,
            seed=policy.bootstrap_seed, method=policy.method.value,
            regression_margin=policy.regression_margin,
            error_accounting=policy.error_accounting.value,
            limitations=tuple(notes + ["no task produced two comparable answers"]))

    mean_delta, median_delta = _mean(values), _median(values)
    ci_low, ci_high, iterations = _bootstrap(values, policy)

    if len(values) < policy.min_pairs_for_claim:
        verdict = StatisticalVerdict.SMALL_SAMPLE
        notes.append(
            f"{len(values)} pair(s) is below the policy minimum of "
            f"{policy.min_pairs_for_claim}; no directional claim is made")
    else:
        verdict = StatisticalVerdict.SUFFICIENT
    if n_missing:
        notes.append(
            f"{n_missing} task(s) produced no comparable pair and are excluded from the "
            f"interval; they remain in every reported rate's denominator")
    return BootstrapReport(
        verdict=verdict, n_pairs=len(values), n_excluded=n_excluded,
        n_missing=n_missing, mean_delta=mean_delta, median_delta=median_delta,
        wins=wins, ties=ties, losses=losses, ci_low=ci_low, ci_high=ci_high,
        confidence_level=policy.confidence_level, iterations=iterations,
        seed=policy.bootstrap_seed, method=policy.method.value,
        regression_margin=policy.regression_margin,
        error_accounting=policy.error_accounting.value, limitations=tuple(notes))


def _bootstrap(values: Sequence[float],
               policy: StatisticalPolicy) -> tuple[float, float, int]:
    """Percentile bootstrap over the paired deltas. Own PRNG, never the global one."""
    if policy.method is not StatisticalMethod.PAIRED_BOOTSTRAP_PERCENTILE:
        raise StatisticsError(  # pragma: no cover - the enum has one member
            f"paired statistics: {policy.method} is not implemented")
    n = len(values)
    if n == 1:
        # One pair has no spread to resample. Reporting a zero-width interval around it
        # would look like certainty; the interval is the point, and SMALL_SAMPLE says so.
        return round(values[0], 9), round(values[0], 9), 0
    # A resampling PRNG, not a security primitive. It is REQUIRED to be reproducible
    # from a recorded seed — a cryptographic generator would make the interval
    # irreproducible, which is the opposite of what this statistic needs. Asserted by
    # test_the_same_data_and_seed_produce_the_same_interval.
    rng = random.Random(policy.bootstrap_seed)  # noqa: S311  # nosec B311
    iterations = policy.bootstrap_iterations
    means: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    alpha = policy.alpha
    return (_percentile(means, alpha / 2.0),
            _percentile(means, 1.0 - alpha / 2.0), iterations)


def _invalid(policy: StatisticalPolicy, *, n_missing: int, n_excluded: int,
             limitations: tuple[str, ...]) -> BootstrapReport:
    return BootstrapReport(
        verdict=StatisticalVerdict.INVALID, n_pairs=0, n_excluded=n_excluded,
        n_missing=n_missing, mean_delta=0.0, median_delta=0.0, wins=0, ties=0,
        losses=0, ci_low=0.0, ci_high=0.0, confidence_level=policy.confidence_level,
        iterations=0, seed=policy.bootstrap_seed, method=policy.method.value,
        regression_margin=policy.regression_margin,
        error_accounting=policy.error_accounting.value, limitations=limitations)


def deltas_from_scores(pairs: Sequence[tuple[str, float, float]]
                       ) -> tuple[PairedDelta, ...]:
    """Convenience for ``(task_id, baseline_reward, candidate_reward)`` triples."""
    return tuple(PairedDelta(task_id=t, baseline=float(b), candidate=float(c))
                 for t, b, c in pairs)


__all__ = [
    "STATISTICS_VERSION", "BootstrapReport", "ErrorAccounting", "PairedDelta",
    "StatisticalVerdict", "StatisticsError", "deltas_from_scores",
    "paired_statistics",
]
