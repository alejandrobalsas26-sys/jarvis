"""training_gym/evaluation/plan_v4.py — V69 M62 S4E: what ONE paired attempt binds.

WHY A WRAPPER RATHER THAN NEW FIELDS ON ``EvaluationPlan``
----------------------------------------------------------
``EvaluationPlan.to_dict`` is an explicit payload and ``plan_hash`` is taken over it, so
adding a field to that dataclass moves the digest of every plan ever built — including
the two sealed receipts that pin the S3Q and S3Y plan hashes. A protocol extension that
retroactively invalidates historical evidence is not additive.

So :class:`V4EvaluationPlan` COMPOSES an ordinary ``EvaluationPlan`` and binds the
Protocol V4 facts beside it. The inner plan keeps its exact v1-v3 meaning and its exact
digest; the V4 digest is taken over the inner digest plus the reference-adapter
bindings. ``EVAL:<v4 plan hash>`` therefore authorises the paired attempt and nothing
else — a v1-v3 token for the inner plan does not authorise a V4 run, and a V4 token does
not authorise a v1-v3 run, because the two hashes are over different documents.

WHAT THE INNER PLAN ALREADY BINDS, AND IS NOT RESTATED HERE
-----------------------------------------------------------
The base model revision, the exact task pack, the hidden-target store, every split
manifest, the generation policy, all six policy digests, the dependency evidence, the
hardware evidence and the order assignment. Restating any of them in the V4 layer would
create two places for the same fact to be wrong.

WHAT THIS ADDS
--------------
The reference arm — which the inner plan structurally cannot name, because its
``baseline_reference_hash`` is a base-model reference with nowhere to record an adapter
digest. Plus the task-ID sequence, the arm order, the runtime report, the source head,
and the exact generation and spend accounting.

STABILITY
---------
Nothing volatile enters the digest. ``created_at_utc`` comes from the sealed config
rather than the clock, and ``HardwareCapabilityReport.identity`` already drops
``available_ram_gb`` and ``output_disk_free_gb`` and keeps only their categories, for
exactly the reason recorded there: a token that expires spontaneously between being
printed and being typed teaches an operator to paste without reading.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from ..schemas import SchemaError, assert_no_private_content, require_id, sha256_obj, short
from .plan import EvaluationPlan
from .protocol_v4 import (
    EVALUATION_PROTOCOL_V4_VERSION,
    GENERATIONS_PER_TASK,
    HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT,
    PairedSpendPlan,
    ReferenceAdapterPairing,
)
from .runner_v4 import RUNNER_V4_VERSION

#: Bumped when the V4 plan payload changes shape. Inside the digest, so a reader can
#: tell which contract a given hash was taken under.
EVALUATION_PLAN_V4_SCHEMA_VERSION = "m62.evaluation_plan_v4.1"

#: The one prefix that authorises a live evaluation. Deliberately the SAME word as the
#: v1-v3 token: an operator is authorising an evaluation either way, and the thing that
#: makes the two non-substitutable is the hash, not the prefix.
CONFIRMATION_PREFIX = "EVAL:"


class PlanV4Error(SchemaError):
    """A V4 plan that does not describe exactly one paired attempt."""


class PlanV4ConfirmationRejected(PlanV4Error):
    """The confirmation was not the exact token for exactly this plan."""


def task_order_hash(task_ids: tuple[str, ...]) -> str:
    """Bind the exact task-ID SEQUENCE, without publishing it.

    The sequence is load-bearing (§20: it may not be reordered after outcomes are seen)
    and it is also the exam's contents list — the control-plane firewall fails any
    scanned surface that names an individual eval-v7 task. A digest binds the order
    exactly while naming nothing, which is the only way to have both.

    Order-sensitive by construction: this is a digest of a LIST, never of a set.
    """
    ids = tuple(str(t) for t in task_ids)
    if not ids:
        raise PlanV4Error("v4 plan: the task order is empty")
    if len(set(ids)) != len(ids):
        raise PlanV4Error(
            "v4 plan: the task order repeats an id; a pack that asks one task twice "
            "measures it twice and the pair accounting would still look complete")
    return sha256_obj({"order_version": "m62.task_order.1", "task_count": len(ids),
                       "task_ids": list(ids)})


@dataclass(frozen=True)
class V4EvaluationPlan:
    """The complete, deterministic description of ONE reference-adapter attempt."""

    inner: EvaluationPlan
    pairing: ReferenceAdapterPairing
    spend: PairedSpendPlan
    reference_adapter_reference_hash: str
    candidate_adapter_reference_hash: str
    task_order_hash: str
    arm_order_policy: str
    arm_order_assignment_hash: str
    runtime_report_sha256: str
    evaluation_source_commit: str
    holdout_dataset_id: str
    holdout_dataset_version: str
    holdout_manifest_hash: str
    holdout_pack_hash: str
    holdout_preregistration_sha256: str
    receipt_path_class: str
    artifact_path_class: str
    plan_v4_schema_version: str = EVALUATION_PLAN_V4_SCHEMA_VERSION
    protocol_version: str = EVALUATION_PROTOCOL_V4_VERSION
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.inner, EvaluationPlan):
            raise PlanV4Error("v4 plan: inner must be an EvaluationPlan")
        if not isinstance(self.pairing, ReferenceAdapterPairing):
            raise PlanV4Error("v4 plan: pairing must be a ReferenceAdapterPairing")
        if not isinstance(self.spend, PairedSpendPlan):
            raise PlanV4Error("v4 plan: spend must be a PairedSpendPlan")
        if self.spend.pairing.pairing_hash() != self.pairing.pairing_hash():
            raise PlanV4Error(
                "v4 plan: the spend plan describes a different pairing from the plan; "
                "the accounting would be for an experiment nobody is running")
        # The arms the plan binds must be the arms the inner plan can see. The inner
        # plan has no reference-adapter field, so only the candidate side is checkable
        # there -- and it IS checked, because the half nobody compares is the half a
        # swap hides in.
        if self.candidate_adapter_reference_hash != \
                self.inner.candidate_adapter_reference_hash:
            raise PlanV4Error(
                "v4 plan: the candidate adapter the V4 layer binds is not the adapter "
                "the inner plan binds; two documents describe two experiments")
        if self.reference_adapter_reference_hash == \
                self.candidate_adapter_reference_hash:
            raise PlanV4Error(
                "v4 plan: both arms bind the same adapter reference; a comparison of an "
                "adapter with itself measures nothing")
        if self.spend.task_count != self.inner.expected_task_count:
            raise PlanV4Error(
                f"v4 plan: the spend plan counts {self.spend.task_count} tasks and the "
                f"inner plan {self.inner.expected_task_count}")
        if self.spend.holdout_spends != HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT:
            raise PlanV4Error(
                "v4 plan: a paired attempt spends its holdout exactly once")
        for name in ("reference_adapter_reference_hash",
                     "candidate_adapter_reference_hash", "task_order_hash",
                     "arm_order_assignment_hash", "runtime_report_sha256",
                     "holdout_manifest_hash", "holdout_pack_hash",
                     "holdout_preregistration_sha256"):
            value = str(getattr(self, name))
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise PlanV4Error(
                    f"v4 plan.{name}: expected a full lowercase sha256; a plan that "
                    f"binds a proxy for the material it authorises binds nothing")
        object.__setattr__(self, "evaluation_source_commit",
                           require_id(self.evaluation_source_commit,
                                      "v4 plan.evaluation_source_commit"))
        if self.protocol_version != EVALUATION_PROTOCOL_V4_VERSION:
            raise PlanV4Error(
                f"v4 plan: this module speaks {EVALUATION_PROTOCOL_V4_VERSION!r}")

    # -- derived ---------------------------------------------------------------
    @property
    def expected_reference_generations(self) -> int:
        return self.spend.task_count

    @property
    def expected_candidate_generations(self) -> int:
        return self.spend.task_count

    @property
    def expected_total_generations(self) -> int:
        return GENERATIONS_PER_TASK * self.spend.task_count

    @property
    def is_executable(self) -> bool:
        return not self.blockers and not self.inner.blockers

    def to_dict(self) -> dict:
        """The canonical V4 plan. Excludes ``plan_hash`` — that is computed over it."""
        payload = {
            "plan_v4_schema_version": self.plan_v4_schema_version,
            "protocol_version": self.protocol_version,
            "runner_v4_version": RUNNER_V4_VERSION,
            # The whole v1-v3 binding, by reference. One fact, one place.
            "inner_plan_hash": self.inner.plan_hash(),
            "evaluation_id": self.inner.evaluation_id,
            "evaluation_generation": self.inner.generation,
            "evaluation_source_commit": self.evaluation_source_commit,
            # Both arms, symmetrically.
            "pairing_hash": self.pairing.pairing_hash(),
            "reference_arm_hash": self.pairing.reference.arm_hash(),
            "candidate_arm_hash": self.pairing.candidate.arm_hash(),
            "reference_adapter_sha256": self.pairing.reference.adapter_sha256,
            "candidate_adapter_sha256": self.pairing.candidate.adapter_sha256,
            "reference_adapter_manifest_hash":
                self.pairing.reference.adapter_manifest_hash,
            "candidate_adapter_manifest_hash":
                self.pairing.candidate.adapter_manifest_hash,
            "reference_training_receipt_sha256":
                self.pairing.reference.training_receipt_sha256,
            "candidate_training_receipt_sha256":
                self.pairing.candidate.training_receipt_sha256,
            "reference_adapter_reference_hash": self.reference_adapter_reference_hash,
            "candidate_adapter_reference_hash": self.candidate_adapter_reference_hash,
            "reference_arm_type": "ADAPTER",
            "candidate_arm_type": "ADAPTER",
            "reference_arm_is_bare_base_model": False,
            # The shared base. Equality across arms is enforced by the pairing itself.
            "common_base_model_id": self.pairing.reference.base_model_id,
            "common_base_model_revision": self.pairing.reference.base_model_revision,
            "common_base_model_identity_hash":
                self.pairing.reference.base_model_identity_hash,
            "common_tokenizer_identity_hash":
                self.pairing.reference.tokenizer_identity_hash,
            "common_tokenizer_chat_template_hash":
                self.pairing.reference.tokenizer_chat_template_hash,
            # The holdout, bound by digest and by ORDER, never by content.
            "holdout_dataset_id": self.holdout_dataset_id,
            "holdout_dataset_version": self.holdout_dataset_version,
            "holdout_manifest_hash": self.holdout_manifest_hash,
            "holdout_pack_hash": self.holdout_pack_hash,
            "holdout_preregistration_sha256": self.holdout_preregistration_sha256,
            "task_order_hash": self.task_order_hash,
            "task_count": self.spend.task_count,
            # Execution.
            "arm_order_policy": self.arm_order_policy,
            "arm_order_assignment_hash": self.arm_order_assignment_hash,
            "runtime_report_sha256": self.runtime_report_sha256,
            "spend_plan_hash": self.spend.plan_hash(),
            "expected_reference_generations": self.expected_reference_generations,
            "expected_candidate_generations": self.expected_candidate_generations,
            "expected_total_generations": self.expected_total_generations,
            "generations_per_task": GENERATIONS_PER_TASK,
            "holdout_spends": self.spend.holdout_spends,
            "paired_attempts": 1,
            "retry_authorized": False,
            "quality_retry_authorized": False,
            # Output, as a CLASS rather than a host path.
            "receipt_path_class": self.receipt_path_class,
            "artifact_path_class": self.artifact_path_class,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "grants_authority": False,
        }
        return dict(sorted(payload.items()))

    def plan_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def confirmation_token(self) -> str:
        """The exact string that would authorise this plan and no other."""
        return f"{CONFIRMATION_PREFIX}{self.plan_hash()}"

    def to_record(self) -> dict:
        record = {**self.to_dict(), "plan_hash": self.plan_hash(),
                  "is_executable": self.is_executable}
        assert_no_private_content(record, label="v4 evaluation plan")
        return record


def check_v4_confirmation(confirmation: object, plan: V4EvaluationPlan) -> str:
    """Refuse anything that is not the exact token for exactly this plan.

    Mirrors the v1-v3 refusals deliberately, including the ones that look paranoid:
    ``True`` authorises every plan equally and therefore authorises none, and a value
    that looks like a file reference is a confirmation nobody typed for this plan.
    """
    if isinstance(confirmation, bool):
        raise PlanV4ConfirmationRejected(
            "evaluation: confirm=True is not a confirmation. A boolean authorises every "
            "plan equally, which is the shape of every accidental automation")
    if not isinstance(confirmation, str):
        raise PlanV4ConfirmationRejected(
            f"evaluation: a confirmation is the exact token string, not "
            f"{type(confirmation).__name__}")
    text = confirmation.strip()
    if text.startswith("@") or "/" in text or "\\" in text:
        raise PlanV4ConfirmationRejected(
            "evaluation: a confirmation read out of a file or a path is a confirmation "
            "nobody typed for this plan")
    if text.startswith("TRAIN:"):
        raise PlanV4ConfirmationRejected(
            "evaluation: that is a TRAIN token. Training and evaluation authorise "
            "different compute against different artefacts and are never substitutable")
    expected = plan.confirmation_token()
    if not hmac.compare_digest(text, expected):
        raise PlanV4ConfirmationRejected(
            f"evaluation: the confirmation does not authorise this plan. This plan is "
            f"{short(plan.plan_hash())} and requires its own exact token; a token for "
            f"any other plan, or a truncated digest, authorises nothing here")
    return text


__all__ = [
    "CONFIRMATION_PREFIX", "EVALUATION_PLAN_V4_SCHEMA_VERSION", "PlanV4ConfirmationRejected",
    "PlanV4Error", "V4EvaluationPlan", "check_v4_confirmation", "task_order_hash",
]
