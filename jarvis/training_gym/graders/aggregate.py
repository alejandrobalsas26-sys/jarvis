"""training_gym/graders/aggregate.py — V69 M62: one decision from many measurements.

WHY THIS EXISTS
---------------
:func:`~training_gym.rewards.score_attempt` already knows how to turn results into a
number without letting a weighted average dissolve a blocking failure. What it does
NOT know is whether the set of results it was handed is the set the task asked for.
That gap is the one this module closes, and it is a real one: a caller that ran nine
of ten mandatory graders and passed the nine produces a perfectly well-formed
``RewardBreakdown`` in which the tenth simply does not appear.

So aggregation happens in two stages, and the first one is about the SET:

  * every required grader maps to exactly ONE result — a duplicate is refused rather
    than resolved by "last wins", because "last wins" makes a second, friendlier run
    of the same grader a way to overwrite a failure;
  * a result for a grader the task never requested is refused, not merged — an
    unrequested PASS is evidence about a question nobody asked, and counting it
    inflates the affirmative-measurement count that rule 3 depends on;
  * a required grader with no result at all is a blocker, not an omission.

Only then does the second stage run, which delegates the arithmetic to the frozen
reward rules rather than restating them.

WHAT A CLEAN AGGREGATION MEANS
------------------------------
``eligible_for_review`` — nothing more. It says the deterministic evidence is complete
and unobjectionable enough for a human to be asked. It is not approval, it does not
imply approval, and :class:`~training_gym.episode.Episode` still refuses to reach
``APPROVED`` without a recorded human decision bound by hash to the attempt.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..rewards import RewardBreakdown, score_attempt
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    ResultStatus,
    SchemaError,
    sha256_obj,
)
from ..task_spec import GRADER_IDS, TaskSpec
from ..trajectory import GraderResult, Trajectory

#: Bump when a rule below changes. Recorded in every aggregation record.
AGGREGATION_VERSION = "m62.aggregate.1"


@dataclass(frozen=True)
class AggregationReport:
    """The complete deterministic verdict for one attempt.

    ``eligible_for_review`` is the only boolean here, and it is deliberately the
    weakest claim the evidence supports: *a human may now be asked*.
    """

    version: str
    task_id: str
    attempt_id: str
    reward: RewardBreakdown
    eligible_for_review: bool = False
    blockers: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    integrity_violations: tuple[str, ...] = ()
    required_graders: tuple[str, ...] = ()
    missing_graders: tuple[str, ...] = ()
    affirmative_graders: int = 0
    results: tuple[dict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.eligible_for_review and (self.blockers or self.integrity_violations):
            raise SchemaError(
                "aggregation: marked eligible for review while blockers remain "
                f"({list(self.blockers) + list(self.integrity_violations)}); a blocking "
                f"finding dominates every positive score")
        if self.eligible_for_review and self.reward.blocked:
            raise SchemaError("aggregation: marked eligible for review with a blocked "
                              "reward")

    @property
    def approved(self) -> bool:
        """Always ``False``. Approval is a human act, never an aggregation outcome.

        Present so a caller reaching for ``report.approved`` finds an explicit refusal
        rather than an :class:`AttributeError` it might work around."""
        return False

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "version": self.version,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "reward": self.reward.to_dict(),
            "eligible_for_review": self.eligible_for_review,
            "approved": self.approved,
            "blockers": list(self.blockers),
            "advisories": list(self.advisories),
            "integrity_violations": list(self.integrity_violations),
            "required_graders": list(self.required_graders),
            "missing_graders": list(self.missing_graders),
            "affirmative_graders": self.affirmative_graders,
            "results": [dict(r) for r in self.results],
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())


def requested_graders(spec: TaskSpec) -> tuple[str, ...]:
    """Every grader id this task asked for. Mandatory ids are always a subset."""
    return tuple(sorted(set(spec.required_graders) | set(spec.scoring.mandatory_graders)))


def validate_result_set(spec: TaskSpec,
                        results: Sequence[GraderResult]) -> tuple[GraderResult, ...]:
    """Refuse a result set that is not the one this task asked for.

    Raises :class:`~training_gym.schemas.SchemaError` on a duplicate, an id outside the
    frozen registry, or a result for a grader the task never requested. Returns the
    results in a deterministic order so two aggregations of the same evidence produce
    the same record.
    """
    requested = set(requested_graders(spec))
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, GraderResult):
            raise SchemaError(
                f"aggregation: {type(result).__name__} is not a GraderResult; a raw "
                f"dictionary is never an authoritative verdict")
        if result.grader_id not in GRADER_IDS:
            raise SchemaError(f"aggregation: unknown grader id "
                              f"{result.grader_id!r}; known ids are {list(GRADER_IDS)}")
        if result.grader_id in seen:
            raise SchemaError(
                f"aggregation: duplicate result for {result.grader_id!r}. Exactly one "
                f"result per grader — resolving a duplicate by 'last wins' would make "
                f"a second, friendlier run a way to overwrite a failure.")
        if result.grader_id not in requested:
            raise SchemaError(
                f"aggregation: task {spec.task_id} did not request "
                f"{result.grader_id!r}; an unrequested verdict is evidence about a "
                f"question nobody asked and may not raise the measurement count")
        seen.add(result.grader_id)
    return tuple(sorted(results, key=lambda r: r.grader_id))


def attach_results(spec: TaskSpec, trajectory: Trajectory,
                   results: Sequence[GraderResult]) -> tuple[GraderResult, ...]:
    """Validate a result set and install it on *trajectory*, replacing what was there."""
    validated = validate_result_set(spec, results)
    trajectory.grader_results = list(validated)
    return validated


def aggregate(spec: TaskSpec, trajectory: Trajectory, *,
              results: Sequence[GraderResult] | None = None) -> AggregationReport:
    """Turn one attempt's deterministic evidence into one defensible decision.

    Never raises on a bad result set: an integrity violation becomes a blocked report,
    because a caller that wrapped this in a ``try`` and logged the exception would
    have turned a refusal into a shrug.
    """
    attempt_id = trajectory.attempt_hash()
    try:
        validated = (attach_results(spec, trajectory, results) if results is not None
                     else validate_result_set(spec, trajectory.grader_results))
    except SchemaError as exc:
        return _blocked_report(spec, trajectory, attempt_id, violations=(str(exc),))

    required = requested_graders(spec)
    mandatory = frozenset(spec.scoring.mandatory_graders)
    present = {r.grader_id: r for r in validated}
    missing = tuple(sorted(set(required) - set(present)))

    blockers: list[str] = []
    advisories: list[str] = []

    for grader_id in missing:
        if grader_id in mandatory:
            blockers.append(f"mandatory grader {grader_id} produced no result; absence "
                            f"of objection is not evidence of correctness")
        else:
            advisories.append(f"required grader {grader_id} produced no result")

    for grader_id in sorted(present):
        result = present[grader_id]
        is_mandatory = grader_id in mandatory
        detail = f"{grader_id}: {result.status.value}"
        if result.error:
            detail += f" ({result.error})"

        if result.blocking and result.status is ResultStatus.FAIL:
            blockers.append(f"{detail} [blocking finding — dominates every positive "
                            f"dimension]")
        elif result.severity.blocks and result.status is not ResultStatus.PASS:
            blockers.append(f"{detail} [severity {result.severity.value} — dominates "
                            f"every positive dimension]")
        elif is_mandatory and result.status.is_blocking_when_mandatory:
            blockers.append(f"{detail} [mandatory]")
        elif is_mandatory and result.status is ResultStatus.NOT_APPLICABLE:
            # A grader the task declared mandatory cannot honestly not apply: the task
            # asserted that this evidence is required for approval.
            blockers.append(f"{grader_id}: not_applicable, but this task declares it "
                            f"mandatory; an attempt nothing measured is not approvable")
        elif result.status is not ResultStatus.PASS:
            advisories.append(f"{detail} [advisory]")

    affirmative = sum(1 for gid, r in present.items()
                      if gid in mandatory and r.status is ResultStatus.PASS)
    if affirmative == 0 and not blockers:
        blockers.append("no mandatory grader produced an affirmative measurement; an "
                        "attempt nothing applied to has not been assessed")

    reward = score_attempt(spec, trajectory)
    if blockers and not reward.blocked:
        # The evidence blockers above and the frozen reward rules do not have to agree
        # on every case, and where they differ this side must win. A mandatory grader
        # that returned NOT_APPLICABLE is the live example: the reward rules treat it
        # as an honest non-measurement and keep scoring the others, which leaves a
        # record reading "total 1.000" beside "cannot be approved". Publishing both
        # numbers is how a reader ends up quoting the flattering one.
        reward = _dominated(reward, tuple(blockers))
    if reward.blocked:
        for reason in reward.block_reasons:
            entry = f"reward: {reason}"
            if entry not in blockers and reason not in blockers:
                blockers.append(entry)
    elif not reward.meets(spec.scoring.min_total_score):
        blockers.append(f"reward {reward.total:.3f} is below the task minimum "
                        f"{spec.scoring.min_total_score:.3f}")

    return AggregationReport(
        version=AGGREGATION_VERSION,
        task_id=spec.task_id,
        attempt_id=attempt_id,
        reward=reward,
        eligible_for_review=not blockers,
        blockers=tuple(blockers),
        advisories=tuple(advisories),
        required_graders=required,
        missing_graders=missing,
        affirmative_graders=affirmative,
        results=tuple(r.to_dict() for r in validated),
    )


def _dominated(reward: RewardBreakdown, reasons: tuple[str, ...]) -> RewardBreakdown:
    """Zero a score that an evidence blocker has already invalidated.

    The per-dimension detail is KEPT, because an operator reading the report still
    needs to know which dimensions were measured and how they came out. Only the two
    totals go to zero, which is the one number a downstream consumer compares against
    a threshold.
    """
    return RewardBreakdown(
        version=reward.version,
        dimensions=dict(reward.dimensions),
        deterministic_total=0.0,
        teacher_penalty=reward.teacher_penalty,
        total=0.0,
        blocked=True,
        block_reasons=reasons,
        measured_dimensions=reward.measured_dimensions,
        unmeasured_dimensions=reward.unmeasured_dimensions,
        affirmative_graders=reward.affirmative_graders,
        notes=(*reward.notes, "score zeroed: a blocking finding dominates every "
                              "positive dimension"),
    )


def _blocked_report(spec: TaskSpec, trajectory: Trajectory, attempt_id: str, *,
                    violations: tuple[str, ...]) -> AggregationReport:
    """The fail-closed report for a result set that could not be trusted at all."""
    return AggregationReport(
        version=AGGREGATION_VERSION,
        task_id=spec.task_id,
        attempt_id=attempt_id,
        reward=RewardBreakdown(blocked=True, block_reasons=violations),
        eligible_for_review=False,
        blockers=violations,
        integrity_violations=violations,
        required_graders=requested_graders(spec),
        missing_graders=(),
        affirmative_graders=0,
        results=(),
    )


__all__ = [
    "AGGREGATION_VERSION", "AggregationReport", "aggregate", "attach_results",
    "requested_graders", "validate_result_set",
]
