"""training_gym/evaluation/comparison.py — V69 M62 S3C: the per-task and aggregate delta.

WHAT A COMPARISON IS
--------------------
A per-task record of what each arm did, and an aggregate that never hides its own
composition. The aggregate reports by family and by split alongside the headline, because
an average conceals exactly the failure this stage exists to catch: a candidate that
improves structured reports by twelve points and collapses on refusals reports a healthy
mean.

THE ONE ASYMMETRY
-----------------
Security is not a dimension of quality here — it is a veto. If the candidate produces a
blocking finding on a task where the baseline did not, that task's verdict is
``SECURITY_REGRESSION`` regardless of its reward delta, and the aggregate carries the
count separately so no later averaging can absorb it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from ..schemas import ResultStatus, SchemaError, sha256_obj
from .metrics import ArmMetrics, build_arm_metrics
from .policy import EvaluationPolicySet
from .runner import PairedGeneration, PairedRun, PairedStatus
from .scoring import ArmScore, RefusalClass, score_arm
from .statistics import BootstrapReport, PairedDelta, paired_statistics
from .task_pack import EvaluationTaskPack, HiddenTargetStore

#: Bumped when a verdict's meaning changes.
COMPARISON_VERSION = "m62.evaluation_comparison.1"


class ComparisonError(SchemaError):
    """A comparison that could not be produced as described."""


class ComparisonVerdict(str, Enum):
    """What happened on one task. Security regressions are their own verdict."""

    #: The candidate produced a blocking finding the baseline did not. Never a "loss".
    SECURITY_REGRESSION = "security_regression"
    #: The baseline had a blocking finding the candidate fixed. Reported, not rewarded.
    SECURITY_IMPROVEMENT = "security_improvement"
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    REGRESSED = "regressed"
    #: One or both arms produced nothing scoreable.
    NOT_COMPARABLE = "not_comparable"

    @property
    def is_blocking(self) -> bool:
        return self is ComparisonVerdict.SECURITY_REGRESSION

    @property
    def is_comparable(self) -> bool:
        return self in (ComparisonVerdict.IMPROVED, ComparisonVerdict.UNCHANGED,
                        ComparisonVerdict.REGRESSED,
                        ComparisonVerdict.SECURITY_IMPROVEMENT)


@dataclass(frozen=True)
class PairedComparison:
    """One task, both arms, and the difference between them."""

    task_id: str
    task_hash: str
    family: str
    split: str
    execution_order: str
    paired_status: PairedStatus
    verdict: ComparisonVerdict
    baseline_result_hash: str = ""
    candidate_result_hash: str = ""
    baseline_score: ArmScore | None = None
    candidate_score: ArmScore | None = None
    reward_delta: float = 0.0
    latency_delta_ms: int = 0
    token_delta: int = 0
    new_security_findings: tuple[str, ...] = ()
    fixed_security_findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "comparison_version": COMPARISON_VERSION,
            "task_id": self.task_id, "task_hash": self.task_hash,
            "family": self.family, "split": self.split,
            "execution_order": self.execution_order,
            "paired_status": self.paired_status.value,
            "verdict": self.verdict.value,
            "baseline_result_hash": self.baseline_result_hash,
            "candidate_result_hash": self.candidate_result_hash,
            "baseline_score_hash": (self.baseline_score.score_hash()
                                    if self.baseline_score else ""),
            "candidate_score_hash": (self.candidate_score.score_hash()
                                     if self.candidate_score else ""),
            "baseline_reward": self.baseline_score.reward if self.baseline_score else None,
            "candidate_reward": (self.candidate_score.reward if self.candidate_score
                                 else None),
            "reward_delta": self.reward_delta,
            "baseline_status": (self.baseline_score.status.value if self.baseline_score
                                else "absent"),
            "candidate_status": (self.candidate_score.status.value
                                 if self.candidate_score else "absent"),
            "baseline_refusal": (self.baseline_score.refusal.value
                                 if self.baseline_score else "absent"),
            "candidate_refusal": (self.candidate_score.refusal.value
                                  if self.candidate_score else "absent"),
            "latency_delta_ms": self.latency_delta_ms,
            "token_delta": self.token_delta,
            "new_security_findings": list(self.new_security_findings),
            "fixed_security_findings": list(self.fixed_security_findings),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }

    def comparison_hash(self) -> str:
        return sha256_obj(self.to_dict())


@dataclass(frozen=True)
class FamilySummary:
    """One family's own result. Reported and gated separately from the aggregate."""

    family: str
    task_count: int
    measured_pairs: int
    baseline_reward: float | None
    candidate_reward: float | None
    delta: float | None
    wins: int
    ties: int
    losses: int
    security_regressions: int
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"family": self.family, "task_count": self.task_count,
                "measured_pairs": self.measured_pairs,
                "baseline_reward": self.baseline_reward,
                "candidate_reward": self.candidate_reward, "delta": self.delta,
                "wins": self.wins, "ties": self.ties, "losses": self.losses,
                "security_regressions": self.security_regressions,
                "blockers": list(self.blockers), "warnings": list(self.warnings)}


@dataclass(frozen=True)
class ComparisonSummary:
    """The aggregate, with its composition attached so it cannot be read alone."""

    comparisons: tuple[PairedComparison, ...]
    task_count: int
    measured_pairs: int
    missing_pairs: int
    wins: int
    ties: int
    losses: int
    security_regressions: int
    security_improvements: int
    overall_delta: float | None
    baseline_metrics: ArmMetrics
    candidate_metrics: ArmMetrics
    bootstrap: BootstrapReport
    family_summaries: tuple[FamilySummary, ...] = ()
    split_summaries: tuple[FamilySummary, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "comparison_version": COMPARISON_VERSION,
            "task_count": self.task_count,
            "measured_pairs": self.measured_pairs,
            "missing_pairs": self.missing_pairs,
            "wins": self.wins, "ties": self.ties, "losses": self.losses,
            "security_regressions": self.security_regressions,
            "security_improvements": self.security_improvements,
            "overall_delta": self.overall_delta,
            "baseline_metrics_hash": self.baseline_metrics.metrics_hash(),
            "candidate_metrics_hash": self.candidate_metrics.metrics_hash(),
            "bootstrap": self.bootstrap.to_dict(),
            "family_summaries": [f.to_dict() for f in self.family_summaries],
            "split_summaries": [s.to_dict() for s in self.split_summaries],
            "comparison_manifest_hash": self.comparison_manifest_hash(),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }

    def comparison_manifest_hash(self) -> str:
        """Digest over every per-task comparison, in canonical order."""
        return sha256_obj([c.comparison_hash() for c in
                           sorted(self.comparisons, key=lambda c: c.task_hash)])

    def summary_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def comparison_records(self) -> tuple[dict, ...]:
        """One line per task, for ``paired-comparisons.jsonl``."""
        return tuple(c.to_dict() for c in
                     sorted(self.comparisons, key=lambda c: c.task_hash))


# ══════════════════════════════════════════════════════════════════════════════
#  Building the comparison
# ══════════════════════════════════════════════════════════════════════════════
def _verdict(baseline: ArmScore | None, candidate: ArmScore | None,
             status: PairedStatus) -> tuple[ComparisonVerdict, tuple[str, ...],
                                            tuple[str, ...]]:
    """The per-task verdict, and the security findings each side introduced or fixed."""
    if baseline is None or candidate is None:
        return ComparisonVerdict.NOT_COMPARABLE, (), ()
    new = tuple(sorted(set(candidate.security_findings)
                       - set(baseline.security_findings)))
    fixed = tuple(sorted(set(baseline.security_findings)
                         - set(candidate.security_findings)))
    if new:
        # Deliberately BEFORE the comparability check: a candidate that leaked a secret
        # and also timed out has still leaked a secret, and "not comparable" would hide
        # it behind a missing measurement.
        return ComparisonVerdict.SECURITY_REGRESSION, new, fixed
    if not status.comparable:
        return ComparisonVerdict.NOT_COMPARABLE, new, fixed
    if fixed:
        return ComparisonVerdict.SECURITY_IMPROVEMENT, new, fixed
    delta = round(candidate.reward - baseline.reward, 9)
    if delta > 0:
        return ComparisonVerdict.IMPROVED, new, fixed
    if delta < 0:
        return ComparisonVerdict.REGRESSED, new, fixed
    return ComparisonVerdict.UNCHANGED, new, fixed


def compare_pair(pair: PairedGeneration, *, pack: EvaluationTaskPack,
                 targets: HiddenTargetStore,
                 policies: EvaluationPolicySet) -> PairedComparison:
    """Score both arms of one task and record the difference."""
    task = pack.by_id(pair.task_id)
    target = None
    if task.task_id in targets:
        target = targets.lookup(task.task_id, task_hash=task.task_hash)

    baseline = (score_arm(task, pair.baseline, target=target,
                          policy=policies.graders) if pair.baseline else None)
    candidate = (score_arm(task, pair.candidate, target=target,
                           policy=policies.graders) if pair.candidate else None)
    verdict, new_findings, fixed_findings = _verdict(baseline, candidate, pair.status)

    reward_delta = 0.0
    if baseline is not None and candidate is not None and pair.status.comparable:
        reward_delta = round(candidate.reward - baseline.reward, 9)
    latency_delta = ((candidate.latency_ms - baseline.latency_ms)
                     if baseline and candidate else 0)
    token_delta = 0
    if baseline and candidate and baseline.output_tokens >= 0 >= -candidate.output_tokens \
            and candidate.output_tokens >= 0:
        token_delta = candidate.output_tokens - baseline.output_tokens

    blockers = list(pair.blockers)
    if verdict.is_blocking:
        blockers.append(
            f"the candidate introduced {', '.join(new_findings)} on task "
            f"{task.task_id!r}; a security regression is not offset by a reward gain")
    for label, score in (("baseline", baseline), ("candidate", candidate)):
        if score is not None and score.missing_graders:
            blockers.append(
                f"the {label} arm has no verdict from mandatory grader(s) "
                f"{list(score.missing_graders)} on task {task.task_id!r}")
    if baseline is not None and candidate is not None:
        base_ran = {g for g, v in baseline.grader_statuses.items()
                    if ResultStatus(v).measured}
        cand_ran = {g for g, v in candidate.grader_statuses.items()
                    if ResultStatus(v).measured}
        if policies.graders.require_identical_availability and base_ran != cand_ran:
            blockers.append(
                f"task {task.task_id!r} was checked by {sorted(base_ran)} on the "
                f"baseline and {sorted(cand_ran)} on the candidate; a candidate scored "
                f"by fewer checks is a less-inspected model, not a better one")

    return PairedComparison(
        task_id=task.task_id, task_hash=task.task_hash, family=task.task_family.value,
        split=task.split.value, execution_order=pair.order.value,
        paired_status=pair.status, verdict=verdict,
        baseline_result_hash=pair.baseline.result_hash() if pair.baseline else "",
        candidate_result_hash=pair.candidate.result_hash() if pair.candidate else "",
        baseline_score=baseline, candidate_score=candidate, reward_delta=reward_delta,
        latency_delta_ms=latency_delta, token_delta=token_delta,
        new_security_findings=new_findings, fixed_security_findings=fixed_findings,
        blockers=tuple(blockers), warnings=pair.warnings)


def _group_summary(label: str, comparisons: Sequence[PairedComparison]) -> FamilySummary:
    comparable = [c for c in comparisons if c.verdict.is_comparable]
    base = [c.baseline_score.reward for c in comparable if c.baseline_score]
    cand = [c.candidate_score.reward for c in comparable if c.candidate_score]
    mean_base = round(sum(base) / len(base), 6) if base else None
    mean_cand = round(sum(cand) / len(cand), 6) if cand else None
    delta = (round(mean_cand - mean_base, 6)
             if mean_base is not None and mean_cand is not None else None)
    return FamilySummary(
        family=label, task_count=len(comparisons), measured_pairs=len(comparable),
        baseline_reward=mean_base, candidate_reward=mean_cand, delta=delta,
        wins=sum(1 for c in comparable if c.verdict is ComparisonVerdict.IMPROVED),
        ties=sum(1 for c in comparable if c.verdict is ComparisonVerdict.UNCHANGED),
        losses=sum(1 for c in comparable if c.verdict is ComparisonVerdict.REGRESSED),
        security_regressions=sum(1 for c in comparisons if c.verdict.is_blocking))


def build_comparison(run: PairedRun, *, pack: EvaluationTaskPack,
                     targets: HiddenTargetStore,
                     policies: EvaluationPolicySet) -> ComparisonSummary:
    """The whole comparison: per task, per family, per split, and in aggregate."""
    if not isinstance(pack, EvaluationTaskPack):
        raise ComparisonError("comparison: pack must be an EvaluationTaskPack")
    if not isinstance(targets, HiddenTargetStore):
        raise ComparisonError(
            "comparison: targets must be a HiddenTargetStore; a plain mapping of answers "
            "is the shape that ends up passed to a backend")

    comparisons = tuple(compare_pair(pair, pack=pack, targets=targets,
                                     policies=policies)
                        for pair in run.generations)
    families = sorted({t.task_family.value for t in pack.tasks})
    splits = sorted({t.split.value for t in pack.tasks})

    baseline_scores = [c.baseline_score for c in comparisons if c.baseline_score]
    candidate_scores = [c.candidate_score for c in comparisons if c.candidate_score]
    baseline_metrics = build_arm_metrics(
        baseline_scores, role="baseline", families=families, splits=splits,
        policy=policies.metrics, task_count=len(pack))
    candidate_metrics = build_arm_metrics(
        candidate_scores, role="candidate", families=families, splits=splits,
        policy=policies.metrics, task_count=len(pack))

    comparable = [c for c in comparisons if c.verdict.is_comparable]
    deltas = tuple(PairedDelta(task_id=c.task_id,
                               baseline=c.baseline_score.reward,
                               candidate=c.candidate_score.reward)
                   for c in comparable if c.baseline_score and c.candidate_score)
    missing = len(pack) - len(deltas)
    bootstrap = paired_statistics(
        deltas, policy=policies.statistics, n_missing=missing,
        n_excluded=len(comparisons) - len(comparable))

    overall = (round(sum(d.delta for d in deltas) / len(deltas), 6) if deltas else None)
    blockers = list(run.blockers)
    warnings = list(run.warnings)
    for comparison in comparisons:
        blockers.extend(comparison.blockers)

    family_summaries = tuple(
        _group_summary(f, [c for c in comparisons if c.family == f]) for f in families)
    split_summaries = tuple(
        _group_summary(s, [c for c in comparisons if c.split == s]) for s in splits)

    dominant = _dominant_family(family_summaries, len(pack),
                               policies.families.max_family_share)
    if dominant:
        warnings.append(dominant)

    return ComparisonSummary(
        comparisons=comparisons, task_count=len(pack),
        measured_pairs=len(deltas), missing_pairs=missing,
        wins=sum(1 for c in comparable if c.verdict is ComparisonVerdict.IMPROVED),
        ties=sum(1 for c in comparable if c.verdict is ComparisonVerdict.UNCHANGED),
        losses=sum(1 for c in comparable if c.verdict is ComparisonVerdict.REGRESSED),
        security_regressions=sum(1 for c in comparisons if c.verdict.is_blocking),
        security_improvements=sum(
            1 for c in comparisons
            if c.verdict is ComparisonVerdict.SECURITY_IMPROVEMENT),
        overall_delta=overall, baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics, bootstrap=bootstrap,
        family_summaries=family_summaries, split_summaries=split_summaries,
        blockers=tuple(dict.fromkeys(blockers)), warnings=tuple(dict.fromkeys(warnings)))


def _dominant_family(summaries: Sequence[FamilySummary], total: int,
                     max_share: float) -> str:
    """Warn when one family is so large that the aggregate is really about it."""
    for summary in summaries:
        if total and summary.task_count / total > max_share:
            return (f"the {summary.family} family holds "
                    f"{summary.task_count}/{total} tasks "
                    f"({summary.task_count / total:.0%}); the aggregate is largely a "
                    f"measurement of that one family")
    return ""


def output_budget_exhaustion_matrix(
        comparisons: Sequence[PairedComparison]) -> Mapping[str, object]:
    """The D38 paired diagnostic: which arm ran out of output budget, per task.

    OBSERVATIONAL ONLY, and deliberately so. There is no sign test here, no bootstrap,
    no interval and no PASS/FAIL — inventing a significance claim for a diagnostic is
    how an observation becomes a gate nobody decided to add. No gate reads this, and
    S3M measured three ceiling endings that passed their graders, so exhaustion is not
    a failure by itself.

    Built the same way as :func:`refusal_counts`: a pure function over the already-scored
    comparisons, body-free, counted rather than averaged. ``unmeasured`` is its own
    bucket, never folded into ``neither``, because "this arm errored" and "this arm
    finished inside its budget" are different facts.
    """
    counts = {"both_exhausted": 0, "candidate_only": 0, "baseline_only": 0,
              "neither_exhausted": 0, "unmeasured": 0}
    candidate_only: list[str] = []
    baseline_only: list[str] = []
    both: list[str] = []
    for comparison in comparisons:
        base = comparison.baseline_score
        cand = comparison.candidate_score
        left = base.output_budget_exhausted if base is not None else None
        right = cand.output_budget_exhausted if cand is not None else None
        if left is None or right is None:
            counts["unmeasured"] += 1
            continue
        if left and right:
            counts["both_exhausted"] += 1
            both.append(comparison.task_id)
        elif right:
            counts["candidate_only"] += 1
            candidate_only.append(comparison.task_id)
        elif left:
            counts["baseline_only"] += 1
            baseline_only.append(comparison.task_id)
        else:
            counts["neither_exhausted"] += 1
    return {
        **counts,
        "paired_tasks": len(comparisons),
        # Body-free ids only, exactly as the security counts already publish them.
        "both_exhausted_tasks": sorted(both)[:20],
        "candidate_only_tasks": sorted(candidate_only)[:20],
        "baseline_only_tasks": sorted(baseline_only)[:20],
        "is_a_gate": False,
        "limitations": [
            "diagnostic only: no gate reads this, and no statistical claim is made "
            "about it",
            "reaching the output ceiling is not a failure by itself; only a grader "
            "whose contract a non-terminating response breaks turns it into one",
        ],
    }


def refusal_counts(comparisons: Sequence[PairedComparison]) -> Mapping[str, dict]:
    """Refusal classes per arm, counted rather than averaged."""
    out: dict[str, dict] = {"baseline": {}, "candidate": {}}
    for arm, attribute in (("baseline", "baseline_score"),
                           ("candidate", "candidate_score")):
        counts = {cls.value: 0 for cls in RefusalClass}
        for comparison in comparisons:
            score = getattr(comparison, attribute)
            if score is not None:
                counts[score.refusal.value] += 1
        out[arm] = counts
    return out


__all__ = [
    "COMPARISON_VERSION", "ComparisonError", "ComparisonSummary", "ComparisonVerdict",
    "FamilySummary", "PairedComparison", "build_comparison", "compare_pair",
    "output_budget_exhaustion_matrix", "refusal_counts",
]
