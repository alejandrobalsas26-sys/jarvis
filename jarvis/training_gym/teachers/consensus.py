"""training_gym/teachers/consensus.py — V69 M62: many opinions, one routing decision.

WHY THIS EXISTS
---------------
Two teachers will disagree, and the tempting thing to do with disagreement is average it
away. A high-confidence APPROVE and a high-confidence REJECT average to something in the
middle, and the middle looks like a mild, manageable result — which is the opposite of
what happened. Two competent reviewers reaching opposite conclusions is the single
strongest signal in this whole layer that a human needs to look, and this module exists
to make sure that signal survives the arithmetic.

THE ORDER OF THE RULES IS THE POLICY
------------------------------------
Deterministic evidence is consulted FIRST, before a single review is read. If the
graders blocked the attempt, the outcome is ``DETERMINISTIC_REJECT`` and the reviews are
recorded but change nothing — there is no ordering of teacher opinions that reaches
approval past a blocking finding, because the code never gets there.

Then binding, then safety, then completeness, then agreement:

    deterministic blocked        → DETERMINISTIC_REJECT
    a review not bound to this   → QUARANTINED
    a review flags unsafe        → QUARANTINED (policy) — a human looks
    a required teacher missing   → TEACHER_REVIEW_INCOMPLETE
    all APPROVE                  → TEACHER_AGREEMENT
    all REVISE                   → REVISION_REQUIRED
    all REJECT                   → REJECTED
    all NEEDS_HUMAN_REVIEW       → ELIGIBLE_FOR_HUMAN_REVIEW
    anything else                → TEACHER_DISAGREEMENT

WHAT AGREEMENT BUYS
-------------------
Nothing except a human's attention. ``TEACHER_AGREEMENT`` sets
``eligible_for_human_approval``, which means "an operator may now be asked" — the same
claim :class:`~training_gym.graders.aggregate.AggregationReport` makes and no stronger.
:attr:`ConsensusReport.approved` is hardcoded ``False``, and
:class:`~training_gym.episode.Episode` still refuses ``APPROVED`` without a hash-bound
human decision.

TEACHERS ONLY SUBTRACT
----------------------
:func:`teacher_adjusted_total` can lower a deterministic score by at most the frozen
:data:`~training_gym.rewards.MAX_TEACHER_PENALTY`, and asserts that its own result never
exceeds its input. There is no argument, no policy value and no combination of reviews
that makes it return more than the deterministic total.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from ..graders.aggregate import AggregationReport
from ..rewards import MAX_TEACHER_PENALTY
from ..schemas import SCHEMA_KEY, SCHEMA_VERSION, require_float, sha256_obj
from ..trajectory import Recommendation
from .base import TeacherError, TeacherReviewRecord

#: Bump when a rule below changes. Recorded in every consensus record, so two attempts
#: routed under different rules are never compared as if they were routed the same way.
CONSENSUS_VERSION = "m62.consensus.1"


class ConsensusOutcome(str, Enum):
    """What the teacher layer concluded. Never what happens to the dataset."""

    DETERMINISTIC_REJECT = "deterministic_reject"
    QUARANTINED = "quarantined"
    TEACHER_REVIEW_INCOMPLETE = "teacher_review_incomplete"
    TEACHER_AGREEMENT = "teacher_agreement"
    TEACHER_DISAGREEMENT = "teacher_disagreement"
    REVISION_REQUIRED = "revision_required"
    REJECTED = "rejected"
    ELIGIBLE_FOR_HUMAN_REVIEW = "eligible_for_human_review"


#: The outcomes after which an operator may be asked to consider approval. Note what is
#: absent: ``REVISION_REQUIRED`` and ``REJECTED`` are teacher conclusions AGAINST the
#: attempt, and ``TEACHER_REVIEW_INCOMPLETE`` means the evidence the policy asked for
#: does not exist yet.
_APPROVABLE_OUTCOMES: frozenset[ConsensusOutcome] = frozenset({
    ConsensusOutcome.TEACHER_AGREEMENT,
    ConsensusOutcome.TEACHER_DISAGREEMENT,
    ConsensusOutcome.ELIGIBLE_FOR_HUMAN_REVIEW,
})


@dataclass(frozen=True)
class ConsensusPolicy:
    """Which teachers must have spoken, and what to do when they conflict."""

    #: Provider ids whose review is required before this attempt can be routed onward.
    #: Empty means "no teacher required", which is a legitimate configuration for a
    #: purely deterministic task — and which still cannot approve anything.
    required_providers: tuple[str, ...] = ()
    #: Reviews needed regardless of which providers they came from.
    min_reviews: int = 0
    #: A teacher safety flag quarantines rather than merely lowering a score. Default
    #: True: the cost of quarantining a false positive is one human glance, and the cost
    #: of scoring away a true positive is training on unsafe behaviour.
    quarantine_on_unsafe: bool = True
    #: Whether a unanimous teacher rejection still goes to a human. Default True,
    #: because a teacher's REJECT is an opinion and discarding an attempt on an opinion
    #: alone is the mirror image of approving on one.
    escalate_rejection_to_human: bool = True

    def to_dict(self) -> dict:
        return {"required_providers": list(self.required_providers),
                "min_reviews": self.min_reviews,
                "quarantine_on_unsafe": self.quarantine_on_unsafe,
                "escalate_rejection_to_human": self.escalate_rejection_to_human}


@dataclass(frozen=True)
class Disagreement:
    """Exactly how the teachers conflicted. Recorded in full, never collapsed.

    The fields exist because "they disagreed" is not actionable and "0.55" is worse than
    not actionable: an operator needs to know WHO disagreed, about WHAT, and how sure
    each of them was.
    """

    providers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    recommendations: tuple[tuple[str, str], ...] = ()   # (provider, recommendation)
    confidences: tuple[tuple[str, float], ...] = ()     # (provider, confidence)
    dimensions_in_conflict: tuple[str, ...] = ()
    conflicting_factual_findings: tuple[tuple[str, str], ...] = ()
    conflicting_safety_findings: tuple[tuple[str, str], ...] = ()
    max_confidence_gap: float = 0.0
    human_review_required: bool = False

    @property
    def present(self) -> bool:
        return len({rec for _p, rec in self.recommendations}) > 1

    def to_dict(self) -> dict:
        return {"providers": list(self.providers), "models": list(self.models),
                "recommendations": [list(pair) for pair in self.recommendations],
                "confidences": [[p, round(c, 6)] for p, c in self.confidences],
                "dimensions_in_conflict": list(self.dimensions_in_conflict),
                "conflicting_factual_findings":
                    [list(pair) for pair in self.conflicting_factual_findings],
                "conflicting_safety_findings":
                    [list(pair) for pair in self.conflicting_safety_findings],
                "max_confidence_gap": round(self.max_confidence_gap, 6),
                "human_review_required": self.human_review_required}


@dataclass(frozen=True)
class ConsensusReport:
    """The teacher layer's complete, hashable conclusion about one attempt."""

    version: str
    task_id: str
    attempt_id: str
    deterministic_report_hash: str
    outcome: ConsensusOutcome
    human_review_required: bool
    deterministic_total: float
    teacher_penalty: float
    adjusted_total: float
    reviews: tuple[dict, ...] = field(default_factory=tuple)
    missing_providers: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    disagreement: Disagreement = field(default_factory=Disagreement)
    policy: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_float(self.deterministic_total, "consensus.deterministic_total",
                      minimum=0.0, maximum=1.0)
        require_float(self.adjusted_total, "consensus.adjusted_total",
                      minimum=0.0, maximum=1.0)
        if self.adjusted_total > self.deterministic_total + 1e-9:
            raise TeacherError(
                f"consensus: adjusted total {self.adjusted_total} exceeds the "
                f"deterministic total {self.deterministic_total}; teachers may only "
                f"subtract")
        if self.eligible_for_human_approval and self.outcome in (
                ConsensusOutcome.DETERMINISTIC_REJECT, ConsensusOutcome.QUARANTINED):
            raise TeacherError("consensus: an outcome that blocks cannot be eligible "
                               "for human approval")
        if not self.reasons:
            raise TeacherError("consensus: every outcome must state why")

    @property
    def approved(self) -> bool:
        """Always ``False``. Approval is a human act, never a consensus outcome.

        Present so a caller reaching for ``report.approved`` finds an explicit refusal
        rather than an :class:`AttributeError` it might work around by testing
        ``outcome == TEACHER_AGREEMENT``, which is a different and much weaker claim.
        """
        return False

    @property
    def eligible_for_human_approval(self) -> bool:
        """Whether an operator may now be asked to decide. Not a decision."""
        return self.outcome in _APPROVABLE_OUTCOMES

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "version": self.version,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "deterministic_report_hash": self.deterministic_report_hash,
            "outcome": self.outcome.value,
            "approved": self.approved,
            "eligible_for_human_approval": self.eligible_for_human_approval,
            "human_review_required": self.human_review_required,
            "deterministic_total": round(self.deterministic_total, 6),
            "teacher_penalty": round(self.teacher_penalty, 6),
            "adjusted_total": round(self.adjusted_total, 6),
            "reviews": [dict(r) for r in self.reviews],
            "missing_providers": list(self.missing_providers),
            "reasons": list(self.reasons),
            "disagreement": self.disagreement.to_dict(),
            "policy": dict(self.policy),
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())


# ── the bounded penalty ───────────────────────────────────────────────────────
def teacher_penalty(records: Sequence[TeacherReviewRecord]) -> float:
    """The deduction a teacher ensemble may apply. Never a bonus, always bounded.

    Driven by the LOWEST overall score, not the mean: averaging lets one enthusiastic
    reviewer dilute another's serious concern, which is precisely the arithmetic this
    module exists to refuse.
    """
    if not records:
        return 0.0
    worst = min(float(r.review.overall_score) for r in records)
    return round((1.0 - worst) * MAX_TEACHER_PENALTY, 6)


def teacher_adjusted_total(deterministic_total: float,
                           records: Sequence[TeacherReviewRecord]) -> float:
    """Apply the bounded penalty, and prove the result did not go up.

    The final ``min`` is not defensive clutter. It is the invariant stated in code: no
    combination of reviews, penalties or rounding can make this function return more
    than it was given.
    """
    base = require_float(deterministic_total, "consensus.deterministic_total",
                         minimum=0.0, maximum=1.0)
    adjusted = max(0.0, base - teacher_penalty(records))
    return round(min(adjusted, base), 6)


# ── the decision ──────────────────────────────────────────────────────────────
def decide(report: AggregationReport, records: Sequence[TeacherReviewRecord] = (), *,
           policy: ConsensusPolicy | None = None) -> ConsensusReport:
    """Route one attempt, given deterministic evidence and zero or more reviews.

    Never mutates *report* or any record: a teacher cannot change a measurement, and the
    cheapest way to guarantee that is for this function to have no write path at all.
    Never raises on a bad review set either — an unbound record becomes a QUARANTINED
    outcome, because a caller that wrapped this in a ``try`` and logged the exception
    would have turned a refusal into a shrug.
    """
    if not isinstance(report, AggregationReport):
        raise TeacherError(f"consensus: expected an AggregationReport, got "
                           f"{type(report).__name__}; a raw dictionary is never an "
                           f"authoritative deterministic verdict")
    active = policy or ConsensusPolicy()
    items = list(records)
    for item in items:
        if not isinstance(item, TeacherReviewRecord):
            raise TeacherError(
                f"consensus: {type(item).__name__} is not a TeacherReviewRecord; a raw "
                f"dictionary is never an authoritative review")

    deterministic_total = float(report.reward.deterministic_total)
    report_hash = report.report_hash()
    summaries = tuple(_summarise(r) for r in items)
    common = {
        "version": CONSENSUS_VERSION,
        "task_id": report.task_id,
        "attempt_id": report.attempt_id,
        "deterministic_report_hash": report_hash,
        "reviews": summaries,
        "policy": active.to_dict(),
    }

    # -- 1. deterministic evidence, before a single opinion is read -------------
    if report.reward.blocked or report.integrity_violations or not report.eligible_for_review:
        reasons = list(report.blockers) or list(report.integrity_violations) or [
            "the deterministic report is not eligible for review"]
        return ConsensusReport(
            outcome=ConsensusOutcome.DETERMINISTIC_REJECT,
            human_review_required=False,
            deterministic_total=0.0 if report.reward.blocked else deterministic_total,
            teacher_penalty=0.0,
            adjusted_total=0.0,
            reasons=tuple(
                ["deterministic evidence blocks this attempt; no teacher opinion is "
                 "consulted, because none can change a measurement"] + reasons)[:32],
            **common)

    # -- 2. every record must answer THIS attempt -------------------------------
    unbound = [r for r in items
               if r.attempt_hash != report.attempt_id
               or r.deterministic_report_hash != report_hash]
    if unbound:
        return ConsensusReport(
            outcome=ConsensusOutcome.QUARANTINED,
            human_review_required=True,
            deterministic_total=deterministic_total,
            teacher_penalty=0.0,
            adjusted_total=deterministic_total,
            reasons=tuple(
                f"review from {r.provider} is bound to another subject "
                f"(attempt {r.attempt_hash[:12]}, report "
                f"{r.deterministic_report_hash[:12]}) and cannot inform this decision"
                for r in unbound),
            **common)

    penalty = teacher_penalty(items)
    adjusted = teacher_adjusted_total(deterministic_total, items)
    disagreement = _disagreement(items)

    # -- 3. a safety flag outranks agreement ------------------------------------
    unsafe = [r for r in items if r.flags_unsafe]
    if unsafe and active.quarantine_on_unsafe:
        return ConsensusReport(
            outcome=ConsensusOutcome.QUARANTINED,
            human_review_required=True,
            deterministic_total=deterministic_total,
            teacher_penalty=penalty,
            adjusted_total=adjusted,
            disagreement=disagreement,
            reasons=tuple(
                [f"{r.provider} flagged unsafe behaviour: "
                 f"{list(r.review.unsafe_behavior)}" for r in unsafe]
                + ["a teacher safety flag routes an attempt to a human; it is never "
                   "scored away"]),
            **common)

    # -- 4. completeness --------------------------------------------------------
    present = {r.provider for r in items}
    missing = tuple(sorted(set(active.required_providers) - present))
    if missing or len(items) < active.min_reviews:
        reasons = []
        if missing:
            reasons.append(f"required teacher review(s) absent: {list(missing)}; an "
                           f"absent opinion is not a favourable one")
        if len(items) < active.min_reviews:
            reasons.append(f"{len(items)} review(s) present, policy requires "
                           f"{active.min_reviews}")
        return ConsensusReport(
            outcome=ConsensusOutcome.TEACHER_REVIEW_INCOMPLETE,
            human_review_required=True,
            deterministic_total=deterministic_total,
            teacher_penalty=penalty,
            adjusted_total=adjusted,
            missing_providers=missing,
            disagreement=disagreement,
            reasons=tuple(reasons),
            **common)

    if not items:
        return ConsensusReport(
            outcome=ConsensusOutcome.TEACHER_REVIEW_INCOMPLETE,
            human_review_required=True,
            deterministic_total=deterministic_total,
            teacher_penalty=0.0,
            adjusted_total=deterministic_total,
            reasons=("no teacher review is attached; the deterministic evidence is "
                     "clean but nothing has been reviewed",),
            **common)

    # -- 5. agreement -----------------------------------------------------------
    verdicts = {r.recommendation for r in items}
    if len(verdicts) == 1:
        only = next(iter(verdicts))
        if only is Recommendation.APPROVE:
            outcome = ConsensusOutcome.TEACHER_AGREEMENT
            reasons = (f"all {len(items)} teacher(s) recommend approve; that makes an "
                       f"operator eligible to be asked, and nothing more — a teacher "
                       f"cannot approve a dataset",)
        elif only is Recommendation.REVISE:
            outcome = ConsensusOutcome.REVISION_REQUIRED
            reasons = (f"all {len(items)} teacher(s) recommend revision",)
        elif only is Recommendation.REJECT:
            outcome = ConsensusOutcome.REJECTED
            reasons = (f"all {len(items)} teacher(s) recommend reject",)
        else:
            outcome = ConsensusOutcome.ELIGIBLE_FOR_HUMAN_REVIEW
            reasons = (f"all {len(items)} teacher(s) defer to a human",)
        human_required = (outcome is not ConsensusOutcome.REJECTED
                          or active.escalate_rejection_to_human)
        return ConsensusReport(
            outcome=outcome, human_review_required=human_required,
            deterministic_total=deterministic_total, teacher_penalty=penalty,
            adjusted_total=adjusted, disagreement=disagreement, reasons=reasons,
            **common)

    # -- 6. disagreement is a result, not noise to average away -----------------
    return ConsensusReport(
        outcome=ConsensusOutcome.TEACHER_DISAGREEMENT,
        human_review_required=True,
        deterministic_total=deterministic_total,
        teacher_penalty=penalty,
        adjusted_total=adjusted,
        disagreement=disagreement,
        reasons=(
            "teachers disagree: "
            + ", ".join(f"{p}={rec}" for p, rec in disagreement.recommendations)
            + ". A confident approval and a confident rejection are not a moderate "
              "score; they are a reason for a human to look.",),
        **common)


def _summarise(record: TeacherReviewRecord) -> dict:
    """One review, as the consensus record carries it. No corrected answer, no prose."""
    review = record.review
    return {
        "provider": record.provider,
        "model": record.model,
        "provider_version": record.provider_version,
        "review_mode": record.review_mode.value,
        "recommendation": review.recommendation.value,
        "overall_score": round(float(review.overall_score), 6),
        "confidence": round(float(review.confidence), 6),
        "dimension_scores": {k: round(float(v), 6)
                             for k, v in sorted(review.dimension_scores.items())},
        "factual_errors": list(review.factual_errors)[:16],
        "unsafe_behavior": list(review.unsafe_behavior)[:16],
        "missing_evidence": list(review.missing_evidence)[:16],
        "review_hash": record.review_hash(),
        "packet_id": record.packet_id,
    }


def _disagreement(records: Sequence[TeacherReviewRecord]) -> Disagreement:
    """Describe the conflict in full, whether or not there is one.

    Built even when the teachers agree, because "they agreed, and here is each one's
    confidence" is exactly what an operator needs in order to notice that a unanimous
    approval came from two reviewers who were both barely sure.
    """
    if not records:
        return Disagreement()
    recommendations = tuple((r.provider, r.recommendation.value) for r in records)
    confidences = tuple((r.provider, float(r.review.confidence)) for r in records)
    values = [c for _p, c in confidences]
    gap = round(max(values) - min(values), 6) if values else 0.0

    conflicting_dimensions: list[str] = []
    for dimension in sorted({d for r in records for d in r.review.dimension_scores}):
        scored = [r.review.dimension_scores[dimension] for r in records
                  if dimension in r.review.dimension_scores]
        # A 0.4 spread is a genuine difference of opinion about one dimension, not
        # two reviewers rounding differently.
        if len(scored) > 1 and (max(scored) - min(scored)) >= 0.4:
            conflicting_dimensions.append(dimension)

    factual = tuple((r.provider, finding) for r in records
                    for finding in r.review.factual_errors[:8])
    safety = tuple((r.provider, finding) for r in records
                   for finding in r.review.unsafe_behavior[:8])
    disagreed = len({rec for _p, rec in recommendations}) > 1
    return Disagreement(
        providers=tuple(sorted({r.provider for r in records})),
        models=tuple(sorted({r.model for r in records})),
        recommendations=recommendations,
        confidences=confidences,
        dimensions_in_conflict=tuple(conflicting_dimensions),
        conflicting_factual_findings=factual,
        conflicting_safety_findings=safety,
        max_confidence_gap=gap,
        human_review_required=disagreed or bool(safety),
    )


__all__ = [
    "CONSENSUS_VERSION", "ConsensusOutcome", "ConsensusPolicy", "ConsensusReport",
    "Disagreement", "decide", "teacher_adjusted_total", "teacher_penalty",
]
