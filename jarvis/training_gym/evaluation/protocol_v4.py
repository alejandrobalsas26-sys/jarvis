"""training_gym/evaluation/protocol_v4.py — V69 M62 S4D: reference-adapter evaluation.

WHY A FOURTH PROTOCOL VERSION EXISTS
------------------------------------
The canonical M62 evaluation protocol compares ONE base model against ONE LoRA adapter.
:class:`~training_gym.evaluation.references.BaseModelEvaluationReference` says both arms
load the same base model and that no substitution is possible; ``execution.py`` demands
two distinct backend objects precisely so it can prove the baseline arm ran with **no
adapter attached**; and the portable receipt's ``baseline`` object is closed over five
base-model identity fields with nowhere to record an adapter digest.

Those properties are correct and this module does not weaken any of them. The old
protocol keeps its meaning, its policy digests, its receipts and its executor unchanged.

What it cannot express is the comparison the science actually asks for once a lineage has
more than one candidate: candidate N-1 against candidate N, on the same fresh holdout,
under the same rubric, in one attempt. Forcing that into ``baseline`` would seal a receipt
claiming the reference arm was a bare base model when a LoRA adapter was attached to it.
The operator's S4D ruling is therefore ADDITIVE: a new arm type whose invariant is
"this arm loaded EXACTLY the declared reference adapter", standing beside — never on top
of — the old arm type whose invariant is "this arm loaded no adapter at all".

WHAT THIS MODULE IS AND IS NOT
------------------------------
It is the REPRESENTATION and its refusals: two symmetric typed arms, the pairing that
binds them, the equality invariants that make the two arms comparable, and the spend
accounting that keeps one paired attempt worth exactly one holdout spend.

It is NOT an executor, it loads no weights, it generates nothing, and it creates no
authority. Building a pairing is a description of an experiment, never permission to run
one — the same rule every other object in this package obeys.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..datasets.candidate import require_digest
from ..schemas import (
    SchemaError,
    assert_no_private_content,
    require_id,
    require_int,
    require_text,
    sha256_obj,
)

#: Bumped when either arm's shape or the pairing's invariants change.
EVALUATION_PROTOCOL_V4_VERSION = "m62.evaluation_protocol.4"

#: Generations per task, per paired attempt. Two arms, one answer each, never a retry.
GENERATIONS_PER_TASK = 2

#: A paired attempt spends its holdout exactly once, however many arms it runs.
#:
#: The arms are not two evaluations sharing a corpus. They are one evaluation with two
#: arms, which is why frozen invariant 20 needs no amendment: there is a single
#: ``holdout_model_facing_committed`` event, a single plan and a single spend.
HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT = 1


class ProtocolV4Error(SchemaError):
    """A reference-adapter pairing that does not describe one comparable experiment."""


class EvaluationArmRole(str, Enum):
    """Which arm of a reference-adapter comparison a reference describes.

    Deliberately NOT reusing :class:`references.EvaluationRole`. That enum's
    ``BASELINE`` member carries the "no adapter attached" promise, and a reference arm
    that borrowed the member would inherit a claim it cannot keep.
    """

    REFERENCE = "reference"
    CANDIDATE = "candidate"


#: Every identity field both arms must agree on for the comparison to be about adapters.
#:
#: If any of these differ, the measured delta is a function of two variables and the
#: experiment has no single axis. They are checked as EQUALITY rather than as presence,
#: because "both arms declared a tokenizer" is not the property that matters.
SHARED_ARM_IDENTITY_FIELDS = (
    "base_model_id",
    "base_model_revision",
    "base_model_identity_hash",
    "tokenizer_identity_hash",
    "tokenizer_chat_template_hash",
)

#: Every identity field the two arms must NOT share, because sharing one means the
#: pairing is comparing an adapter with itself.
DISTINCT_ARM_IDENTITY_FIELDS = (
    "candidate_id",
    "run_id",
    "adapter_sha256",
    "adapter_manifest_hash",
    "adapter_artifact_set_hash",
)


@dataclass(frozen=True)
class AdapterArmReference:
    """One arm of a reference-adapter paired evaluation, and its exact subject.

    Symmetric by construction: the reference arm and the candidate arm are the SAME
    type carrying the same fields, differing only in :attr:`role`. That symmetry is the
    point. A shape that gave the reference arm fewer fields would let a reference be
    bound with less proof than a candidate, and the arm nobody checked is the arm a
    swap hides in.

    ``role`` is inside :meth:`arm_hash`, so the identical adapter bound as a reference
    and as a candidate produces two different digests and a swap is detectable.
    """

    role: EvaluationArmRole
    candidate_id: str
    run_id: str
    adapter_sha256: str
    adapter_manifest_hash: str
    adapter_artifact_set_hash: str
    training_receipt_sha256: str
    base_model_id: str
    base_model_revision: str
    base_model_identity_hash: str
    tokenizer_identity_hash: str
    tokenizer_chat_template_hash: str
    protocol_version: str = EVALUATION_PROTOCOL_V4_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.role, EvaluationArmRole):
            raise ProtocolV4Error(
                "arm.role: expected an EvaluationArmRole; an arm whose role is a bare "
                "string can be swapped without anything noticing")
        set_(self, "candidate_id", require_id(self.candidate_id, "arm.candidate_id"))
        set_(self, "run_id", require_id(self.run_id, "arm.run_id"))
        for name in ("adapter_sha256", "adapter_manifest_hash",
                     "adapter_artifact_set_hash", "training_receipt_sha256",
                     "base_model_identity_hash", "tokenizer_identity_hash",
                     "tokenizer_chat_template_hash"):
            set_(self, name, require_digest(getattr(self, name), f"arm.{name}"))
        set_(self, "base_model_id",
             require_text(self.base_model_id, "arm.base_model_id", max_len=320))
        revision = require_text(self.base_model_revision, "arm.base_model_revision",
                                min_len=40, max_len=40).lower()
        if any(c not in "0123456789abcdef" for c in revision):
            raise ProtocolV4Error(
                "arm.base_model_revision: expected an immutable 40-character commit "
                "revision; a branch or tag name is not a pinned revision")
        set_(self, "base_model_revision", revision)
        if self.protocol_version != EVALUATION_PROTOCOL_V4_VERSION:
            raise ProtocolV4Error(
                f"arm.protocol_version: this module speaks "
                f"{EVALUATION_PROTOCOL_V4_VERSION!r}, not {self.protocol_version!r}")

    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "evaluation_arm_role": self.role.value,
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "adapter_sha256": self.adapter_sha256,
            "adapter_manifest_hash": self.adapter_manifest_hash,
            "adapter_artifact_set_hash": self.adapter_artifact_set_hash,
            "training_receipt_sha256": self.training_receipt_sha256,
            "base_model_id": self.base_model_id,
            "base_model_revision": self.base_model_revision,
            "base_model_identity_hash": self.base_model_identity_hash,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "tokenizer_chat_template_hash": self.tokenizer_chat_template_hash,
            "adapter_attached": True,
        }

    def arm_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "arm_hash": self.arm_hash()}
        assert_no_private_content(record, label="evaluation arm reference")
        return record


def arm_from_training_receipt(receipt: dict, *, role: EvaluationArmRole,
                              run_id: str,
                              training_receipt_sha256: str) -> AdapterArmReference:
    """Build one arm from a sealed portable training receipt.

    Identity comes from the receipt rather than from the caller, for the reason the old
    protocol records ``identity_source: "training_receipt"``: a caller who may name the
    adapter is a caller who may name the wrong one. Every digest below is read out of
    the receipt, and the constructor re-validates each of them.
    """
    if not isinstance(receipt, dict):
        raise ProtocolV4Error("training receipt: expected a mapping")
    try:
        adapter = receipt["adapter"]
        base = receipt["base_model"]
        candidate_id = receipt["candidate_id"]
    except (KeyError, TypeError) as exc:
        raise ProtocolV4Error(
            f"training receipt: missing {exc}; an arm whose identity cannot be read "
            f"from a sealed receipt is an arm nobody verified") from None
    return AdapterArmReference(
        role=role,
        candidate_id=candidate_id,
        run_id=run_id,
        adapter_sha256=adapter.get("sha256", ""),
        adapter_manifest_hash=adapter.get("manifest_hash", ""),
        adapter_artifact_set_hash=adapter.get("artifact_set_hash", ""),
        training_receipt_sha256=training_receipt_sha256,
        base_model_id=base.get("model_id", ""),
        base_model_revision=base.get("revision", ""),
        base_model_identity_hash=base.get("identity_hash", ""),
        tokenizer_identity_hash=base.get("tokenizer_identity_hash",
                                         base.get("identity_hash", "")),
        tokenizer_chat_template_hash=base.get("chat_template_digest", ""))


@dataclass(frozen=True)
class ReferenceAdapterPairing:
    """Both arms of ONE paired evaluation, and the invariants that make them comparable.

    Every refusal below is a way the pairing could look reasonable and measure two
    variables at once, or measure one adapter against itself, or seal a receipt whose
    arms are the wrong way round.
    """

    reference: AdapterArmReference
    candidate: AdapterArmReference
    protocol_version: str = EVALUATION_PROTOCOL_V4_VERSION

    def __post_init__(self) -> None:
        for label, arm in (("reference", self.reference), ("candidate", self.candidate)):
            if not isinstance(arm, AdapterArmReference):
                raise ProtocolV4Error(
                    f"pairing.{label}: expected an AdapterArmReference; a pairing that "
                    f"accepts an untyped arm accepts an unverified one")
        if self.reference.role is not EvaluationArmRole.REFERENCE:
            raise ProtocolV4Error(
                f"pairing.reference carries role {self.reference.role.value!r}; the "
                f"reference arm must declare REFERENCE. Roles are not positional")
        if self.candidate.role is not EvaluationArmRole.CANDIDATE:
            raise ProtocolV4Error(
                f"pairing.candidate carries role {self.candidate.role.value!r}; the "
                f"candidate arm must declare CANDIDATE. Roles are not positional")
        for name in SHARED_ARM_IDENTITY_FIELDS:
            ref_value, cand_value = getattr(self.reference, name), getattr(
                self.candidate, name)
            if ref_value != cand_value:
                raise ProtocolV4Error(
                    f"pairing: the arms declare different {name} "
                    f"({ref_value!r} vs {cand_value!r}). The measured delta would be a "
                    f"function of two variables, so the comparison has no single axis")
        for name in DISTINCT_ARM_IDENTITY_FIELDS:
            if getattr(self.reference, name) == getattr(self.candidate, name):
                raise ProtocolV4Error(
                    f"pairing: both arms declare the same {name}; a pairing that "
                    f"compares an adapter with itself measures nothing")
        if self.protocol_version != EVALUATION_PROTOCOL_V4_VERSION:
            raise ProtocolV4Error(
                f"pairing.protocol_version: this module speaks "
                f"{EVALUATION_PROTOCOL_V4_VERSION!r}, not {self.protocol_version!r}")

    # -- derived ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "reference_arm": self.reference.to_dict(),
            "candidate_arm": self.candidate.to_dict(),
            "reference_arm_hash": self.reference.arm_hash(),
            "candidate_arm_hash": self.candidate.arm_hash(),
            "reference_arm_type": "ADAPTER",
            "candidate_arm_type": "ADAPTER",
            "shared_base_model_id": self.reference.base_model_id,
            "shared_base_model_revision": self.reference.base_model_revision,
            "holdout_spends": HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT,
        }

    def pairing_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "pairing_hash": self.pairing_hash()}
        assert_no_private_content(record, label="reference-adapter pairing")
        return record

    def expected_generations(self, task_count: int) -> int:
        """``2 * task_count``. One answer per arm per task, and never a retry."""
        count = require_int(task_count, "pairing.task_count", minimum=1, maximum=100_000)
        return GENERATIONS_PER_TASK * count


@dataclass(frozen=True)
class PairedSpendPlan:
    """What ONE paired attempt is allowed to consume. Preregistered, never adjusted.

    Exists so the accounting is a checkable object rather than a sentence in a document:
    a run that produced a different number of generations, or committed the holdout more
    than once, is refused against this rather than explained afterwards.
    """

    pairing: ReferenceAdapterPairing
    task_count: int
    holdout_spends: int = HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT

    def __post_init__(self) -> None:
        if not isinstance(self.pairing, ReferenceAdapterPairing):
            raise ProtocolV4Error("spend plan.pairing: expected a ReferenceAdapterPairing")
        object.__setattr__(self, "task_count", require_int(
            self.task_count, "spend plan.task_count", minimum=1, maximum=100_000))
        if self.holdout_spends != HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT:
            raise ProtocolV4Error(
                f"spend plan.holdout_spends is {self.holdout_spends}; a paired attempt "
                f"spends its holdout exactly {HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT} time. "
                f"Two spends is two evaluations, which frozen invariant 20 forbids")

    @property
    def expected_generations(self) -> int:
        return self.pairing.expected_generations(self.task_count)

    def to_dict(self) -> dict:
        return {
            "protocol_version": EVALUATION_PROTOCOL_V4_VERSION,
            "pairing_hash": self.pairing.pairing_hash(),
            "task_count": self.task_count,
            "generations_per_task": GENERATIONS_PER_TASK,
            "expected_generations": self.expected_generations,
            "holdout_spends": self.holdout_spends,
            "retry_authorized": False,
            "grants_authority": False,
        }

    def plan_hash(self) -> str:
        return sha256_obj(self.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
#  Arm independence
# ══════════════════════════════════════════════════════════════════════════════
def paired_arm_backends(backend_factory) -> tuple[object, object]:
    """One backend object per arm, and a refusal when they are the same object.

    The same structural argument ``execution.py`` already makes for base-vs-adapter: two
    arms sharing one mutable object cannot prove which adapter answered, because the
    proof would rest on the order in which somebody toggled it.
    """
    reference = backend_factory(EvaluationArmRole.REFERENCE.value)
    candidate = backend_factory(EvaluationArmRole.CANDIDATE.value)
    if reference is candidate:
        raise ProtocolV4Error(
            "protocol v4: both arms were handed the same backend object; a shared "
            "object cannot prove which adapter produced which answer")
    return reference, candidate


def assert_no_cross_arm_context(*, prompt: str,
                                other_arm_outputs: Sequence[str]) -> None:
    """Refuse a prompt carrying any of the other arm's answers.

    Each arm answers the frozen task, not the other arm's attempt at it. If either
    arm's output can reach the other's context the second arm is being graded on a
    different exam, and whichever ran second would carry an advantage no gate measures.

    Deliberately checked over the OTHER arm's outputs rather than over a similarity
    score: this is a containment property, not a plagiarism metric.
    """
    text = require_text(prompt, "arm prompt", max_len=1_000_000)
    for index, output in enumerate(other_arm_outputs):
        candidate_text = str(output).strip()
        # A short fragment is not evidence of leakage: two correct answers to the same
        # task legitimately share words. Only a substantial verbatim span is.
        if len(candidate_text) >= 32 and candidate_text in text:
            raise ProtocolV4Error(
                f"protocol v4: the prompt for one arm contains output {index} produced "
                f"by the other arm; arms are independent and neither may see the "
                f"other's answer")


__all__ = [
    "AdapterArmReference",
    "DISTINCT_ARM_IDENTITY_FIELDS",
    "EVALUATION_PROTOCOL_V4_VERSION",
    "EvaluationArmRole",
    "GENERATIONS_PER_TASK",
    "HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT",
    "PairedSpendPlan",
    "ProtocolV4Error",
    "ReferenceAdapterPairing",
    "SHARED_ARM_IDENTITY_FIELDS",
    "arm_from_training_receipt",
    "assert_no_cross_arm_context",
    "paired_arm_backends",
]
