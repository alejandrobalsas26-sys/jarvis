"""training_gym/evaluation/registry_bridge.py — V69 M62 S3C: a proposal, and nothing else.

WHAT THIS MODULE DOES
---------------------
It writes a JSON document describing what a Model Registry transition WOULD be asked for,
if somebody later decided to ask. It validates compatibility, records provenance and
states the limitations. Then it stops.

WHAT IT DOES NOT DO
-------------------
It does not import ``core.model_registry`` at module scope, does not construct a
``PromotionDecision``, does not call ``register``, ``attach_evaluation``, ``promote``,
``reject`` or ``rollback``, does not write the registry file, does not assign a role,
does not merge an adapter, does not convert a format and does not contact Ollama. There
is no function here that takes a registry object.

The registry's own vocabulary is read lazily and defensively — only to name the status a
proposal would target — so that this module stays importable in the training virtualenv
where ``core`` is not on the path, and so that reading it can never become writing it.

WHY A PROPOSAL AT ALL
---------------------
Because the alternative is a human transcribing digests by hand. The document is the
handoff; the decision remains a person's.
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
    require_id,
    require_str_tuple,
    sha256_obj,
)
from ..task_spec import require_timestamp
from .human_review import EvaluationHumanReview, ReviewDecision, check_review_admissible
from .reports import CandidateEligibility, EmpiricalStatus

#: Bumped when the proposal's shape changes.
PROPOSAL_SCHEMA_VERSION = "m62.model_candidate_proposal.1"

#: The registry status a proposal would ask for if it were ever acted on. Deliberately
#: the WEAKEST one: "this has been evaluated", not "this is a candidate" and certainly
#: not "this is active". Moving beyond it is a human's decision in another system.
PROPOSED_REGISTRY_STATUS = "evaluated"


class ProposalError(SchemaError):
    """A proposal that would misrepresent what was measured."""


class ProposalStatus(str, Enum):
    """Whether this proposal is worth a human's attention. Never an instruction."""

    #: A complete live measurement, every gate passed, and a human approved the handoff.
    READY_FOR_REGISTRY_REVIEW = "ready_for_registry_review"
    #: The measurement or the review does not support asking for anything yet.
    NOT_ELIGIBLE = "not_eligible"


def registry_vocabulary() -> dict:
    """Read the registry's status names, without importing anything that mutates it.

    Lazy and defensive on purpose. The gym's contracts must stay importable where
    ``core`` is absent, and an unavailable registry is reported as unavailable rather
    than silently defaulted — a proposal that named a status this build does not have
    would be describing a transition nobody could perform.
    """
    try:
        from core.model_registry import ModelStatus
    except Exception:  # noqa: BLE001 — absence must be visible, not silently fine
        return {"available": False, "statuses": [], "proposed_status": "",
                "note": "the model registry was not importable from this environment; "
                        "the proposal records what it would ask for without naming a "
                        "status this build could not confirm exists"}
    statuses = sorted(str(s.value) for s in ModelStatus)
    return {"available": True, "statuses": statuses,
            "proposed_status": (PROPOSED_REGISTRY_STATUS
                                if PROPOSED_REGISTRY_STATUS in statuses else ""),
            "note": "read only; this module never calls a registry mutator"}


@dataclass(frozen=True)
class ModelCandidateProposal:
    """A description of a transition that has NOT been requested and NOT performed."""

    proposal_status: ProposalStatus
    evaluation_id: str
    generation: int
    base_model_id: str
    base_model_revision: str
    base_model_identity_hash: str
    tokenizer_identity_hash: str
    adapter_run_id: str
    adapter_manifest_hash: str
    candidate_adapter_reference_hash: str
    baseline_reference_hash: str
    evaluation_report_hash: str
    evaluation_plan_hash: str
    human_review_hash: str
    empirical_status: str
    eligibility: str
    proposed_registry_status: str
    registry_schema: dict
    provenance: dict
    limitations: tuple[str, ...]
    blockers: tuple[str, ...]
    created_at_utc: str
    proposal_version: str = PROPOSAL_SCHEMA_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "proposal_status", ProposalStatus(self.proposal_status))
        set_(self, "evaluation_id", require_id(self.evaluation_id,
                                               "proposal.evaluation_id"))
        set_(self, "adapter_run_id", require_id(self.adapter_run_id,
                                                "proposal.adapter_run_id"))
        set_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                       "proposal.created_at_utc"))
        for name in ("base_model_identity_hash", "tokenizer_identity_hash",
                     "adapter_manifest_hash", "candidate_adapter_reference_hash",
                     "baseline_reference_hash", "evaluation_report_hash",
                     "evaluation_plan_hash"):
            set_(self, name, require_digest(getattr(self, name), f"proposal.{name}"))
        if self.human_review_hash:
            set_(self, "human_review_hash",
                 require_digest(self.human_review_hash, "proposal.human_review_hash"))
        for name in ("limitations", "blockers"):
            set_(self, name, require_str_tuple(getattr(self, name), f"proposal.{name}",
                                               max_items=64))
        if self.proposal_status is ProposalStatus.READY_FOR_REGISTRY_REVIEW:
            if self.empirical_status != EmpiricalStatus.LIVE_MEASURED.value:
                raise ProposalError(
                    f"proposal: cannot be ready for registry review with empirical "
                    f"status {self.empirical_status!r}; a synthetic evaluation is not "
                    f"evidence about an adapter")
            if not self.human_review_hash:
                raise ProposalError(
                    "proposal: cannot be ready for registry review without a human "
                    "review; this handoff is a person's decision, not a computation")
            if self.blockers:
                raise ProposalError(
                    "proposal: cannot be ready for registry review while blockers stand")

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: self.schema_version,
            "proposal_version": self.proposal_version,
            "proposal_status": self.proposal_status.value,
            "evaluation_id": self.evaluation_id, "generation": self.generation,
            "base_model_id": self.base_model_id,
            "base_model_revision": self.base_model_revision,
            "base_model_identity_hash": self.base_model_identity_hash,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "adapter_run_id": self.adapter_run_id,
            "adapter_manifest_hash": self.adapter_manifest_hash,
            "candidate_adapter_reference_hash": self.candidate_adapter_reference_hash,
            "baseline_reference_hash": self.baseline_reference_hash,
            "evaluation_report_hash": self.evaluation_report_hash,
            "evaluation_plan_hash": self.evaluation_plan_hash,
            "human_review_hash": self.human_review_hash,
            "empirical_status": self.empirical_status,
            "eligibility": self.eligibility,
            "proposed_registry_status": self.proposed_registry_status,
            "registry_schema": dict(sorted(self.registry_schema.items())),
            "provenance": dict(sorted(self.provenance.items())),
            "limitations": list(self.limitations),
            "blockers": list(self.blockers),
            "created_at_utc": self.created_at_utc,
            # The effect list, stated so an operator reading the document does not have
            # to infer it from what the document omits.
            "effects": {
                "registry_written": False,
                "model_promoted": False,
                "model_activated": False,
                "role_assigned": False,
                "adapter_merged": False,
                "format_converted": False,
                "ollama_contacted": False,
                "requested_transition": (
                    f"a human could ask the registry to record this adapter as "
                    f"{self.proposed_registry_status or 'evaluated'}; nothing here does"),
            },
        }

    def proposal_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "proposal_hash": self.proposal_hash()}
        assert_no_private_content(record, label="model candidate proposal")
        return record


def build_proposal(*, report_record: dict, adapter_reference, baseline_reference,
                   created_at_utc: str,
                   review: EvaluationHumanReview | None = None
                   ) -> ModelCandidateProposal:
    """Produce the proposal document. Writes no registry, and returns no decision.

    Every reason the proposal is not ready is collected rather than raised, so an
    operator gets the whole list at once instead of discovering it one refusal at a time.
    """
    from .reports import verify_report_payload
    verified = verify_report_payload(report_record)

    blockers: list[str] = []
    status = str(verified.get("empirical_status", ""))
    eligibility = str(verified.get("eligibility", {}).get("eligibility", ""))

    if status != EmpiricalStatus.LIVE_MEASURED.value:
        blockers.append(
            f"the evaluation's empirical status is {status!r}; a registry proposal "
            f"requires a complete live measurement of a real model")
    if eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value:
        blockers.append(
            f"the evaluation's eligibility is {eligibility!r}, not "
            f"{CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value!r}")
    blockers.extend(str(b) for b in verified.get("blockers", ()))

    review_hash = ""
    if review is None:
        blockers.append(
            "no human review is attached; the handoff to a registry is a person's "
            "decision and this stage does not make it")
    else:
        review_hash = review.review_hash()
        if str(verified.get("report_hash", "")) != review.evaluation_report_hash:
            blockers.append(
                "the attached review was written about a different report; a review "
                "lifted onto another evaluation authorises nothing")
        if review.candidate_adapter_reference_hash != \
                adapter_reference.reference_hash():
            blockers.append(
                "the attached review names a different adapter than this proposal")
        if review.decision is not ReviewDecision.APPROVE_CANDIDATE_PROPOSAL:
            blockers.append(
                f"the reviewer's decision was {review.decision.value!r}, not an "
                f"approval to propose")
        blockers.extend(check_review_admissible(review, verified))

    vocabulary = registry_vocabulary()
    if not vocabulary["available"]:
        blockers.append(
            "the model registry's vocabulary could not be read from this environment, "
            "so the transition this proposal describes cannot be confirmed to exist")

    ready = (ProposalStatus.READY_FOR_REGISTRY_REVIEW if not blockers
             else ProposalStatus.NOT_ELIGIBLE)
    limitations = list(verified.get("limitations", ()))
    limitations.append(
        "this document is a proposal. It writes no registry, promotes nothing, "
        "activates nothing, assigns no role, merges no adapter and converts no format")
    if status == EmpiricalStatus.SYNTHETIC_ONLY.value:
        limitations.append(
            "the evaluation behind this proposal was SYNTHETIC_ONLY; it describes a "
            "test double and says nothing about a model")

    return ModelCandidateProposal(
        proposal_status=ready,
        evaluation_id=str(verified["evaluation_id"]),
        generation=int(verified.get("generation", 1)),
        base_model_id=baseline_reference.base_model_id,
        base_model_revision=baseline_reference.base_model_revision,
        base_model_identity_hash=baseline_reference.base_model_identity_hash,
        tokenizer_identity_hash=baseline_reference.tokenizer_identity_hash,
        adapter_run_id=adapter_reference.run_id,
        adapter_manifest_hash=adapter_reference.adapter_manifest_hash,
        candidate_adapter_reference_hash=adapter_reference.reference_hash(),
        baseline_reference_hash=baseline_reference.reference_hash(),
        evaluation_report_hash=str(verified["report_hash"]),
        evaluation_plan_hash=str(verified["plan_hash"]),
        human_review_hash=review_hash,
        empirical_status=status, eligibility=eligibility,
        proposed_registry_status=str(vocabulary.get("proposed_status", "")),
        registry_schema=vocabulary,
        provenance={
            "training_run_id": adapter_reference.run_id,
            "training_plan_hash": adapter_reference.plan_hash,
            "training_config_hash": adapter_reference.training_config_hash,
            "training_method": adapter_reference.method.value,
            "dataset_manifest_hash": adapter_reference.dataset_manifest_hash,
            "dataset_reference_hash": adapter_reference.dataset_reference_hash,
            "hidden_evaluation_hash": adapter_reference.hidden_evaluation_hash,
            "security_regression_hash": adapter_reference.security_regression_hash,
            "evaluation_task_pack_hash": str(verified.get("task_pack_hash", "")),
            "evaluation_backends": list(verified.get("backend_ids", ())),
            "measured_pairs": verified.get("measured_pairs"),
            "task_count": verified.get("task_count"),
            "overall_delta": verified.get("overall_delta"),
        },
        limitations=tuple(dict.fromkeys(limitations)),
        blockers=tuple(dict.fromkeys(blockers)),
        created_at_utc=created_at_utc)


__all__ = [
    "PROPOSAL_SCHEMA_VERSION", "PROPOSED_REGISTRY_STATUS", "ModelCandidateProposal",
    "ProposalError", "ProposalStatus", "build_proposal", "registry_vocabulary",
]
