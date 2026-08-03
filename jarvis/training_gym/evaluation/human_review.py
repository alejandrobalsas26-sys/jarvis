"""training_gym/evaluation/human_review.py — V69 M62 S3C: the sign-off, bound to one report.

WHAT A REVIEW CAN AND CANNOT DO
-------------------------------
It can say a human looked at a specific report and formed a view. It cannot erase a
deterministic blocker, change a metric, promote a model or activate one — an approval
that could overrule a security gate would make the gate advisory, and this repository
does not have advisory security gates.

WHY THE BINDINGS ARE PLURAL
---------------------------
A review names the report hash, the adapter reference hash AND the baseline reference
hash. One binding would let a favourable review be lifted off one evaluation and re-filed
against another; three make replay detectable rather than merely discouraged. The same
shape S2d's dataset review already uses, for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..datasets.candidate import require_digest
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    assert_no_private_content,
    reject_unknown_fields,
    require_binding,
    require_id,
    require_mapping,
    require_str_tuple,
    require_text,
    sha256_obj,
)
from ..task_spec import require_timestamp
from .reports import CandidateEligibility, EmpiricalStatus

#: Bumped when the review's shape changes.
REVIEW_SCHEMA_VERSION = "m62.evaluation_human_review.1"

_REVIEW_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "review_version", "reviewer_id", "evaluation_id",
    "evaluation_report_hash", "candidate_adapter_reference_hash",
    "baseline_reference_hash", "decision", "rationale_categories",
    "accepted_limitations", "notes", "created_at_utc",
)

#: A closed vocabulary. Free text is permitted in ``notes`` and never in the reason,
#: so a decision can be aggregated and audited rather than only read.
RATIONALE_CATEGORIES: frozenset[str] = frozenset({
    "quality_improvement_observed", "security_posture_unchanged",
    "security_posture_improved", "refusal_behaviour_acceptable",
    "evidence_grounding_acceptable", "tool_call_behaviour_acceptable",
    "sample_size_insufficient", "family_coverage_insufficient",
    "regression_observed", "security_regression_observed",
    "operational_cost_unacceptable", "synthetic_evidence_only",
    "awaiting_live_measurement", "artifact_integrity_concern",
})


class EvaluationReviewError(SchemaError):
    """A review that does not bind to exactly one evaluation."""


class ReviewDecision(str, Enum):
    """What the reviewer concluded. None of these promotes or activates anything."""

    #: The reviewer supports proposing this adapter as a registry candidate. It is a
    #: PROPOSAL: nothing in this repository acts on it automatically.
    APPROVE_CANDIDATE_PROPOSAL = "approve_candidate_proposal"
    REJECT = "reject"
    REQUEST_MORE_EVIDENCE = "request_more_evidence"
    QUARANTINE = "quarantine"

    @property
    def is_approval(self) -> bool:
        return self is ReviewDecision.APPROVE_CANDIDATE_PROPOSAL


@dataclass(frozen=True)
class EvaluationHumanReview:
    """One human's decision about one report, bound to it by three hashes."""

    reviewer_id: str
    evaluation_id: str
    evaluation_report_hash: str
    candidate_adapter_reference_hash: str
    baseline_reference_hash: str
    decision: ReviewDecision
    created_at_utc: str
    rationale_categories: tuple[str, ...] = ()
    accepted_limitations: tuple[str, ...] = ()
    notes: str = ""
    review_version: str = REVIEW_SCHEMA_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        # A safe operator label, never a username: a review record is exportable, and a
        # host account name in one publishes who is logged in on which machine.
        set_(self, "reviewer_id", require_id(self.reviewer_id, "review.reviewer_id"))
        set_(self, "evaluation_id", require_id(self.evaluation_id,
                                               "review.evaluation_id"))
        set_(self, "decision", ReviewDecision(self.decision))
        set_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                       "review.created_at_utc"))
        set_(self, "notes", require_text(self.notes, "review.notes", min_len=0,
                                         max_len=4_000))
        for name in ("evaluation_report_hash", "candidate_adapter_reference_hash",
                     "baseline_reference_hash"):
            set_(self, name, require_digest(getattr(self, name), f"review.{name}"))
        for name in ("rationale_categories", "accepted_limitations"):
            values = require_str_tuple(getattr(self, name), f"review.{name}",
                                       max_items=32)
            set_(self, name, tuple(sorted(set(values))))
        unknown = sorted(set(self.rationale_categories) - RATIONALE_CATEGORIES)
        if unknown:
            raise EvaluationReviewError(
                f"review.rationale_categories: unknown {unknown}; the vocabulary is "
                f"closed so a decision can be audited rather than only read")
        if not self.rationale_categories:
            raise EvaluationReviewError(
                "review.rationale_categories: required. A decision with no recorded "
                "reason cannot be reviewed by the next person")

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: self.schema_version,
            "review_version": self.review_version,
            "reviewer_id": self.reviewer_id,
            "evaluation_id": self.evaluation_id,
            "evaluation_report_hash": self.evaluation_report_hash,
            "candidate_adapter_reference_hash": self.candidate_adapter_reference_hash,
            "baseline_reference_hash": self.baseline_reference_hash,
            "decision": self.decision.value,
            "rationale_categories": list(self.rationale_categories),
            "accepted_limitations": list(self.accepted_limitations),
            "notes": self.notes,
            "created_at_utc": self.created_at_utc,
            "promotes_model": False,
            "activates_model": False,
            "overrides_deterministic_gates": False,
        }

    def review_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "review_hash": self.review_hash()}
        assert_no_private_content(record, label="evaluation human review")
        return record

    @classmethod
    def from_dict(cls, payload: object) -> "EvaluationHumanReview":
        data = dict(require_mapping(payload, "evaluation human review"))
        stored = str(data.pop("review_hash", "")).strip().lower()
        for derived in ("promotes_model", "activates_model",
                        "overrides_deterministic_gates"):
            declared = data.pop(derived, False)
            if declared:
                raise EvaluationReviewError(
                    f"evaluation human review: {derived} may not be true; a review that "
                    f"could do this would make the gates advisory")
        reject_unknown_fields(data, _REVIEW_FIELDS, label="evaluation human review")
        review = cls(
            reviewer_id=data["reviewer_id"], evaluation_id=data["evaluation_id"],
            evaluation_report_hash=data["evaluation_report_hash"],
            candidate_adapter_reference_hash=data["candidate_adapter_reference_hash"],
            baseline_reference_hash=data["baseline_reference_hash"],
            decision=data["decision"], created_at_utc=data["created_at_utc"],
            rationale_categories=tuple(data.get("rationale_categories", ())),
            accepted_limitations=tuple(data.get("accepted_limitations", ())),
            notes=str(data.get("notes", "")),
            schema_version=str(data.get(SCHEMA_KEY, SCHEMA_VERSION)))
        if stored and stored != review.review_hash():
            raise EvaluationReviewError(
                "evaluation human review: the stored digest does not match the "
                "document; the review has been edited since it was signed")
        return review


def verify_review_bindings(review: EvaluationHumanReview, *, report_record: dict,
                           adapter_reference_hash: str,
                           baseline_reference_hash: str) -> None:
    """Refuse a review that was not written about THIS evaluation.

    Every check is a replay defence. The report hash catches a review lifted onto a
    different evaluation; the adapter hash catches one lifted onto a different adapter;
    the baseline hash catches one where the comparison's other side changed.
    """
    require_binding({"evaluation_report_hash": review.evaluation_report_hash},
                    field="evaluation_report_hash",
                    expected=str(report_record.get("report_hash", "")),
                    label="evaluation human review")
    require_binding(
        {"candidate_adapter_reference_hash": review.candidate_adapter_reference_hash},
        field="candidate_adapter_reference_hash", expected=adapter_reference_hash,
        label="evaluation human review")
    require_binding({"baseline_reference_hash": review.baseline_reference_hash},
                    field="baseline_reference_hash", expected=baseline_reference_hash,
                    label="evaluation human review")
    if str(report_record.get("evaluation_id", "")) != review.evaluation_id:
        raise EvaluationReviewError(
            f"evaluation human review: names evaluation {review.evaluation_id!r} but "
            f"the report is {report_record.get('evaluation_id')!r}")


def check_review_admissible(review: EvaluationHumanReview,
                            report_record: dict) -> tuple[str, ...]:
    """Reasons this decision cannot stand, whatever the reviewer chose.

    An approval is admissible only on a report that could support one. This is where
    "a human said yes" stops being able to overrule "the machine measured a leak".
    """
    problems: list[str] = []
    status = str(report_record.get("empirical_status", ""))
    eligibility = str(report_record.get("eligibility", {}).get("eligibility", ""))
    blockers = list(report_record.get("blockers", ()))

    if not review.decision.is_approval:
        return ()
    if status == EmpiricalStatus.SYNTHETIC_ONLY.value:
        problems.append(
            "the report is SYNTHETIC_ONLY: no model was measured, so there is nothing "
            "for an approval to be about. Approving it would record a human decision "
            "about a test double as a decision about an adapter")
    elif status != EmpiricalStatus.LIVE_MEASURED.value:
        problems.append(
            f"the report's empirical status is {status!r}; an approval requires a "
            f"complete live measurement")
    if eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value:
        problems.append(
            f"the report's eligibility is {eligibility!r}; a human review cannot raise "
            f"it, because a review that could would make the deterministic gates "
            f"advisory")
    if blockers:
        problems.append(
            f"{len(blockers)} deterministic blocker(s) stand; a human decision does not "
            f"clear them — the first is: {blockers[0][:160]}")
    return tuple(problems)


__all__ = [
    "RATIONALE_CATEGORIES", "REVIEW_SCHEMA_VERSION", "EvaluationHumanReview",
    "EvaluationReviewError", "ReviewDecision", "check_review_admissible",
    "verify_review_bindings",
]
