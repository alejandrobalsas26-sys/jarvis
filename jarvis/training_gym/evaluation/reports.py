"""training_gym/evaluation/reports.py — V69 M62 S3C: the report, and what it entitles.

THE CENTRAL DISTINCTION
-----------------------
:class:`EmpiricalStatus` separates "this evaluation ran correctly" from "this evaluation
measured a model". A fake-backed run produces a real report about a fake model; that is
useful for testing the machinery and it is not evidence about an adapter. So a report
whose status is ``SYNTHETIC_ONLY`` can never reach ``ELIGIBLE_FOR_HUMAN_REVIEW``, no
matter how good every number in it looks.

That rule is enforced here rather than in the CLI, because a check in a command-line
front end is a check that a library caller skips.

WHAT ELIGIBILITY MEANS
----------------------
The strongest verdict this subsystem can reach is ``ELIGIBLE_FOR_HUMAN_REVIEW``: a human
may now be asked to look. It is not approval, not promotion and not activation, and
there is no state in this package that means any of those.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    assert_no_private_content,
    sha256_obj,
)
from . import EVALUATION_SCHEMA_VERSION, EVALUATOR_VERSION
from .comparison import (
    ComparisonSummary,
    output_budget_exhaustion_matrix,
    refusal_counts,
)
from .config import EvaluationRunState
from .gates import GateReport
from .plan import EvaluationPlan
from .policy import EvaluationPolicySet
from .references import AdapterEvaluationReference, BaseModelEvaluationReference

#: Bumped when the report's shape changes.
REPORT_SCHEMA_VERSION = "m62.evaluation_report.1"

#: Backends whose output is synthetic by construction. Listed by id so a report cannot
#: claim to be live merely because a caller said so.
SYNTHETIC_BACKEND_IDS: frozenset[str] = frozenset({
    "fake_evaluation", "fake_deterministic", "mock", "stub"})


class ReportError(SchemaError):
    """A report that would claim more than it measured."""


class EmpiricalStatus(str, Enum):
    """Whether a real model was actually run, and how completely."""

    #: Produced by a test double. Real machinery, no model. Never eligibility-grade.
    SYNTHETIC_ONLY = "synthetic_only"
    #: Every task was answered by a real model on both arms.
    LIVE_MEASURED = "live_measured"
    #: A real model ran, but not over the whole pack.
    PARTIAL_LIVE = "partial_live"
    #: Something prevented a measurement from being obtained at all.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    @property
    def supports_eligibility(self) -> bool:
        """Only a complete live measurement can make a candidate eligible."""
        return self is EmpiricalStatus.LIVE_MEASURED


class CandidateEligibility(str, Enum):
    """The strongest thing this subsystem may conclude. None of these is a promotion."""

    #: A human may now be asked to look. Not approval.
    ELIGIBLE_FOR_HUMAN_REVIEW = "eligible_for_human_review"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"
    QUARANTINED = "quarantined"

    @property
    def permits_human_review(self) -> bool:
        return self is CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW


def classify_empirical_status(*, backend_ids: Sequence[str], task_count: int,
                              measured_pairs: int,
                              interrupted: bool) -> EmpiricalStatus:
    """What kind of evidence this run produced. Derived, never asserted by a caller."""
    ids = {str(b).strip().lower() for b in backend_ids if str(b).strip()}
    if not ids:
        return EmpiricalStatus.INSUFFICIENT_EVIDENCE
    if ids & SYNTHETIC_BACKEND_IDS:
        return EmpiricalStatus.SYNTHETIC_ONLY
    if measured_pairs <= 0:
        return EmpiricalStatus.INSUFFICIENT_EVIDENCE
    if interrupted or measured_pairs < task_count:
        return EmpiricalStatus.PARTIAL_LIVE
    return EmpiricalStatus.LIVE_MEASURED


@dataclass(frozen=True)
class EligibilityDecision:
    """The decision, and every reason behind it. Never a bare enum."""

    eligibility: CandidateEligibility
    empirical_status: EmpiricalStatus
    human_review_required: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    rationale: str = ""

    def to_dict(self) -> dict:
        return {"eligibility": self.eligibility.value,
                "empirical_status": self.empirical_status.value,
                "human_review_required": self.human_review_required,
                "blockers": list(self.blockers), "warnings": list(self.warnings),
                "rationale": self.rationale,
                "promotes_model": False, "activates_model": False}

    def decision_hash(self) -> str:
        return sha256_obj(self.to_dict())


#: States a report may legitimately carry at the moment it is serialised.
#:
#: A report cannot be written in ``COMPLETED``. ``EvaluationManifest`` binds
#: ``report_hash``, so the report must be final BEFORE the manifest is sealed, and the
#: manifest must be sealed and re-verified BEFORE ``ARTIFACT_VALIDATION`` may move to
#: ``COMPLETED``. Asking the report to already say "completed" is a circular demand: the
#: only way to satisfy it is to rewrite the report after the manifest that binds it, or
#: to assert a terminal state the run has not reached.
#:
#: So ``run_state`` on a report answers "where was this serialised", not "how did this
#: end", and the two must not be conflated. Treating the serialisation state as an
#: outcome put ``"the evaluation ended in comparing, not completed"`` on the blocker list
#: of every live run — observed in V69 M62 S3E.2, where it was spurious and changed
#: nothing only because two independent security blockers already decided the outcome.
#:
#: A TERMINAL state that is not ``COMPLETED`` is still a real blocker, and a non-terminal
#: state outside this set is still an anomaly worth blocking on: a report serialised in
#: ``RUNNING_BASELINE`` describes a run that never scored anything.
REPORT_SERIALISATION_STATES: frozenset[EvaluationRunState] = frozenset({
    EvaluationRunState.COMPARING,
    EvaluationRunState.ARTIFACT_VALIDATION,
})


def decide_eligibility(*, gates: GateReport, empirical: EmpiricalStatus,
                       summary: ComparisonSummary,
                       run_state: EvaluationRunState) -> EligibilityDecision:
    """The one place a candidate's eligibility is decided.

    The order matters: quarantine first (a run set aside is never eligible), then the
    empirical gate (a synthetic run is never evidence about an adapter), then the
    deterministic gates. A caller cannot reach eligibility by satisfying the last check
    alone.
    """
    blockers = [f"{f.gate}: {f.message}" for f in gates.blocking]
    warnings = [f"{f.gate}: {f.message}" for f in gates.warnings]

    if run_state is EvaluationRunState.QUARANTINED:
        return EligibilityDecision(
            eligibility=CandidateEligibility.QUARANTINED, empirical_status=empirical,
            human_review_required=True, blockers=tuple(blockers),
            warnings=tuple(warnings),
            rationale="the evaluation was quarantined; a quarantined run is never "
                      "eligible, whatever its numbers say")
    if run_state.is_terminal:
        if not run_state.is_successful:
            blockers.insert(0,
                            f"the evaluation ended in {run_state.value}, not completed")
    elif run_state not in REPORT_SERIALISATION_STATES:
        blockers.insert(0, f"the report was serialised in {run_state.value}, which is "
                           f"not a state a run reaches on the way to completion")

    if gates.security_blockers:
        return EligibilityDecision(
            eligibility=CandidateEligibility.NOT_ELIGIBLE, empirical_status=empirical,
            human_review_required=True, blockers=tuple(blockers),
            warnings=tuple(warnings),
            rationale=(f"{len(gates.security_blockers)} security gate(s) blocked; a "
                       f"quality gain does not offset a security regression, because "
                       f"the two are not in the same units"))

    if not empirical.supports_eligibility:
        reason = {
            EmpiricalStatus.SYNTHETIC_ONLY:
                "this report was produced by a test double. The machinery ran "
                "correctly and no model was measured, so it is not evidence about an "
                "adapter",
            EmpiricalStatus.PARTIAL_LIVE:
                "a real model ran, but not over the whole task pack; a subset reported "
                "as the whole is not a measurement of the adapter",
            EmpiricalStatus.INSUFFICIENT_EVIDENCE:
                "no measurement was obtained",
        }[empirical]
        return EligibilityDecision(
            eligibility=CandidateEligibility.NEEDS_MORE_EVIDENCE,
            empirical_status=empirical, human_review_required=False,
            blockers=tuple(blockers), warnings=tuple(warnings), rationale=reason)

    if blockers:
        return EligibilityDecision(
            eligibility=CandidateEligibility.NOT_ELIGIBLE, empirical_status=empirical,
            human_review_required=True, blockers=tuple(blockers),
            warnings=tuple(warnings),
            rationale=f"{len(blockers)} deterministic gate(s) blocked")

    if not summary.bootstrap.verdict.supports_a_directional_claim:
        return EligibilityDecision(
            eligibility=CandidateEligibility.NEEDS_MORE_EVIDENCE,
            empirical_status=empirical, human_review_required=False,
            blockers=tuple(blockers), warnings=tuple(warnings),
            rationale=summary.bootstrap.claim())

    return EligibilityDecision(
        eligibility=CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW,
        empirical_status=empirical, human_review_required=True,
        blockers=(), warnings=tuple(warnings),
        rationale=("every deterministic gate passed on a complete live measurement; a "
                   "human may now be asked to review. This is not approval, not "
                   "promotion and not activation"))


# ══════════════════════════════════════════════════════════════════════════════
#  The report
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EvaluationReport:
    """The immutable record of one evaluation generation."""

    evaluation_id: str
    generation: int
    plan_hash: str
    baseline_reference_hash: str
    candidate_adapter_reference_hash: str
    tokenizer_identity_hash: str
    task_pack_hash: str
    hidden_target_store_hash: str
    dataset_manifest_hash: str
    split_manifest_hashes: dict
    generation_policy_hash: str
    grader_policy_hash: str
    metric_policy_hash: str
    statistical_policy_hash: str
    gate_policy_hash: str
    family_policy_hash: str
    backend_ids: tuple[str, ...]
    backend_version: str
    dependency_report_hash: str
    hardware_report_hash: str
    task_count: int
    measured_pairs: int
    comparison_manifest_hash: str
    summary: ComparisonSummary
    gates: GateReport
    decision: EligibilityDecision
    run_state: EvaluationRunState
    created_at_utc: str
    limitations: tuple[str, ...] = ()
    report_version: str = REPORT_SCHEMA_VERSION
    evaluator_version: str = EVALUATOR_VERSION
    evaluation_schema_version: str = EVALUATION_SCHEMA_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.decision.empirical_status.supports_eligibility and (
                set(self.backend_ids) & SYNTHETIC_BACKEND_IDS):
            raise ReportError(
                f"evaluation report: backend(s) "
                f"{sorted(set(self.backend_ids) & SYNTHETIC_BACKEND_IDS)} are test "
                f"doubles, so this report may not claim "
                f"{self.decision.empirical_status.value}")
        if self.decision.eligibility.permits_human_review and \
                not self.decision.empirical_status.supports_eligibility:
            raise ReportError(
                "evaluation report: a candidate cannot be eligible for human review on "
                "a report that did not measure a model")
        if self.measured_pairs > self.task_count:
            raise ReportError(
                "evaluation report: more measured pairs than tasks is a counting error")

    # -- derived ---------------------------------------------------------------
    @property
    def empirical_status(self) -> EmpiricalStatus:
        return self.decision.empirical_status

    @property
    def eligibility(self) -> CandidateEligibility:
        return self.decision.eligibility

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: self.schema_version,
            "report_version": self.report_version,
            "evaluation_schema_version": self.evaluation_schema_version,
            "evaluator_version": self.evaluator_version,
            "evaluation_id": self.evaluation_id,
            "generation": self.generation,
            "plan_hash": self.plan_hash,
            "baseline_reference_hash": self.baseline_reference_hash,
            "candidate_adapter_reference_hash": self.candidate_adapter_reference_hash,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "task_pack_hash": self.task_pack_hash,
            "hidden_target_store_hash": self.hidden_target_store_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "split_manifest_hashes": dict(sorted(self.split_manifest_hashes.items())),
            "generation_policy_hash": self.generation_policy_hash,
            "grader_policy_hash": self.grader_policy_hash,
            "metric_policy_hash": self.metric_policy_hash,
            "statistical_policy_hash": self.statistical_policy_hash,
            "gate_policy_hash": self.gate_policy_hash,
            "family_policy_hash": self.family_policy_hash,
            "backend_ids": sorted(self.backend_ids),
            "backend_version": self.backend_version,
            "dependency_report_hash": self.dependency_report_hash,
            "hardware_report_hash": self.hardware_report_hash,
            "task_count": self.task_count,
            "measured_pairs": self.measured_pairs,
            "missing_pairs": self.task_count - self.measured_pairs,
            "comparison_manifest_hash": self.comparison_manifest_hash,
            "quality_summary": self.summary.candidate_metrics.to_dict()["quality"],
            "baseline_quality_summary":
                self.summary.baseline_metrics.to_dict()["quality"],
            "security_summary": {
                "baseline": self.summary.baseline_metrics.to_dict()["security"],
                "candidate": self.summary.candidate_metrics.to_dict()["security"],
                "new_security_regressions": self.summary.security_regressions,
                "security_improvements": self.summary.security_improvements},
            "refusal_summary": {
                "baseline": self.summary.baseline_metrics.to_dict()["refusal"],
                "candidate": self.summary.candidate_metrics.to_dict()["refusal"],
                "counts": dict(refusal_counts(self.summary.comparisons))},
            "operational_summary": {
                "baseline": self.summary.baseline_metrics.to_dict()["operational"],
                "candidate": self.summary.candidate_metrics.to_dict()["operational"]},
            # D38. The per-arm counts, rates and per-family breakdown already travel in
            # ``operational_summary`` beside every other operational metric, under
            # ``output_budget_exhaustion_rate`` / ``_count``; duplicating them into a
            # second block of a hash-bound document would create two numbers to keep in
            # step. What is genuinely new is the PAIRED view, so that alone is added.
            # Diagnostic: no gate reads it and it carries no verdict.
            "output_budget_exhaustion_paired": output_budget_exhaustion_matrix(
                self.summary.comparisons),
            "family_summaries": [f.to_dict() for f in self.summary.family_summaries],
            "split_summaries": [s.to_dict() for s in self.summary.split_summaries],
            "overall_delta": self.summary.overall_delta,
            "wins": self.summary.wins, "ties": self.summary.ties,
            "losses": self.summary.losses,
            "bootstrap": self.summary.bootstrap.to_dict(),
            "gate_report": self.gates.to_dict(),
            "blockers": list(self.decision.blockers),
            "warnings": list(self.decision.warnings),
            "eligibility": self.decision.to_dict(),
            "empirical_status": self.decision.empirical_status.value,
            "human_review_required": self.decision.human_review_required,
            "run_state": self.run_state.value,
            "created_at_utc": self.created_at_utc,
            "limitations": list(self.limitations),
            "promotes_model": False,
            "activates_model": False,
            "mutates_model_registry": False,
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "report_hash": self.report_hash()}
        assert_no_private_content(record, label="evaluation report")
        return record


def build_report(*, plan: EvaluationPlan, summary: ComparisonSummary,
                 gates: GateReport, baseline: BaseModelEvaluationReference,
                 adapter: AdapterEvaluationReference, policies: EvaluationPolicySet,
                 backend_ids: Sequence[str], backend_version: str,
                 split_manifest_hashes: dict, run_state: EvaluationRunState,
                 created_at_utc: str, interrupted: bool = False,
                 limitations: Sequence[str] = ()) -> EvaluationReport:
    """Assemble the report and decide eligibility. The status is derived, not supplied."""
    empirical = classify_empirical_status(
        backend_ids=backend_ids, task_count=summary.task_count,
        measured_pairs=summary.measured_pairs, interrupted=interrupted)
    decision = decide_eligibility(gates=gates, empirical=empirical, summary=summary,
                                  run_state=run_state)
    notes = list(limitations)
    notes.extend(gates.limitations)
    notes.extend(summary.bootstrap.limitations)
    if empirical is EmpiricalStatus.SYNTHETIC_ONLY:
        notes.append(
            "SYNTHETIC_ONLY: no model was loaded and no token was generated by a real "
            "backend; every number here describes a test double")
    return EvaluationReport(
        evaluation_id=plan.evaluation_id, generation=plan.generation,
        plan_hash=plan.plan_hash(),
        baseline_reference_hash=baseline.reference_hash(),
        candidate_adapter_reference_hash=adapter.reference_hash(),
        tokenizer_identity_hash=baseline.tokenizer_identity_hash,
        task_pack_hash=plan.task_pack_hash,
        hidden_target_store_hash=plan.hidden_target_store_hash,
        dataset_manifest_hash=plan.dataset_manifest_hash,
        split_manifest_hashes=dict(split_manifest_hashes),
        generation_policy_hash=plan.generation_policy_hash,
        grader_policy_hash=policies.graders.policy_hash(),
        metric_policy_hash=policies.metrics.policy_hash(),
        statistical_policy_hash=policies.statistics.policy_hash(),
        gate_policy_hash=policies.gates.policy_hash(),
        family_policy_hash=policies.families.policy_hash(),
        backend_ids=tuple(sorted({str(b) for b in backend_ids})),
        backend_version=str(backend_version),
        dependency_report_hash=plan.dependency_report_hash,
        hardware_report_hash=plan.hardware_report_hash,
        task_count=summary.task_count, measured_pairs=summary.measured_pairs,
        comparison_manifest_hash=summary.comparison_manifest_hash(),
        summary=summary, gates=gates, decision=decision, run_state=run_state,
        created_at_utc=created_at_utc, limitations=tuple(dict.fromkeys(notes)))


def verify_report_payload(payload: object, *, expected_hash: str = "") -> dict:
    """Re-derive a persisted report's digest and refuse a tampered one.

    The stored ``report_hash`` is recomputed over the rest of the document, so editing
    any field — a metric, a blocker, the eligibility — is detected. Editing the digest
    to match is detected too, when the caller knows what it should be.
    """
    from ..schemas import require_mapping
    data = dict(require_mapping(payload, "evaluation report"))
    stored = str(data.pop("report_hash", "")).strip().lower()
    if not stored:
        raise ReportError(
            "evaluation report: no report_hash; a report that names no digest cannot be "
            "verified against one")
    recomputed = sha256_obj(data)
    if recomputed != stored:
        raise ReportError(
            f"evaluation report: the stored digest {stored[:12]} does not match the "
            f"document ({recomputed[:12]}); the report has been edited since it was "
            f"written")
    if expected_hash and stored != str(expected_hash).strip().lower():
        raise ReportError(
            f"evaluation report: digest {stored[:12]} is not the expected "
            f"{str(expected_hash)[:12]}")
    return {**data, "report_hash": stored}


__all__ = [
    "REPORT_SCHEMA_VERSION", "REPORT_SERIALISATION_STATES", "SYNTHETIC_BACKEND_IDS",
    "CandidateEligibility",
    "EligibilityDecision", "EmpiricalStatus", "EvaluationReport", "ReportError",
    "build_report", "classify_empirical_status", "decide_eligibility",
    "verify_report_payload",
]
