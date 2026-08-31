"""V69 M62 S4D — reference-adapter paired evaluation, Protocol V4.

WHAT THESE TESTS ARE FOR
------------------------
The canonical protocol compares a bare base model against one LoRA adapter, and proves
the baseline arm had no adapter attached. That property is correct and stays correct; it
is simply unable to express the comparison a lineage needs once it has two candidates.
Forcing candidate 004 into ``baseline`` would seal a receipt claiming a bare base model
answered when a LoRA adapter did. The operator's S4D ruling adds a reference-adapter mode
rather than weakening the old one.

Everything below runs on SYNTHETIC fixtures. No adapter is loaded, no tokenizer is loaded,
no weight is read, no model is called and no candidate output exists anywhere in this
file. Two tests read the real *sealed training receipts* of candidates 004 and 005 —
tracked JSON identity records, not model behaviour — because an identity protocol that is
never pointed at the identities it must carry is a protocol nobody checked.

The negative half is the load-bearing half. Each refusal below is a way a pairing could
look reasonable and quietly measure two variables at once, compare an adapter with
itself, seal its arms the wrong way round, or spend one holdout twice.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from training_gym.evaluation.protocol_v4 import (
    DISTINCT_ARM_IDENTITY_FIELDS,
    EVALUATION_PROTOCOL_V4_VERSION,
    GENERATIONS_PER_TASK,
    HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT,
    SHARED_ARM_IDENTITY_FIELDS,
    AdapterArmReference,
    EvaluationArmRole,
    PairedSpendPlan,
    ProtocolV4Error,
    ReferenceAdapterPairing,
    arm_from_training_receipt,
    assert_no_cross_arm_context,
    paired_arm_backends,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = REPO_ROOT / "state" / "m62" / "receipts"
SCHEMA_DIR = REPO_ROOT / "state" / "m62" / "schema"


# ── synthetic fixtures ───────────────────────────────────────────────────────────────
def _d(seed: str) -> str:
    """A synthetic 64-hex digest. Deterministic, and of nothing real."""
    return hashlib.sha256(f"s4d-synthetic-{seed}".encode()).hexdigest()


SHARED_BASE = {
    "base_model_id": "synthetic/base-model",
    "base_model_revision": "a" * 40,
    "base_model_identity_hash": _d("base-identity"),
    "tokenizer_identity_hash": _d("tokenizer-identity"),
    "tokenizer_chat_template_hash": _d("chat-template"),
}


def make_arm(role: EvaluationArmRole, tag: str, **overrides) -> AdapterArmReference:
    fields = {
        "role": role,
        "candidate_id": f"synthetic-candidate-{tag}",
        "run_id": f"synthetic-run-{tag}",
        "adapter_sha256": _d(f"adapter-{tag}"),
        "adapter_manifest_hash": _d(f"manifest-{tag}"),
        "adapter_artifact_set_hash": _d(f"artifacts-{tag}"),
        "training_receipt_sha256": _d(f"receipt-{tag}"),
        **SHARED_BASE,
    }
    fields.update(overrides)
    return AdapterArmReference(**fields)


@pytest.fixture
def reference_arm() -> AdapterArmReference:
    return make_arm(EvaluationArmRole.REFERENCE, "ref")


@pytest.fixture
def candidate_arm() -> AdapterArmReference:
    return make_arm(EvaluationArmRole.CANDIDATE, "cand")


@pytest.fixture
def pairing(reference_arm, candidate_arm) -> ReferenceAdapterPairing:
    return ReferenceAdapterPairing(reference=reference_arm, candidate=candidate_arm)


# ══════════════════════════════════════════════════════════════════════════════
#  §11.1-2 — both arms are required, and both are adapters
# ══════════════════════════════════════════════════════════════════════════════
def test_a_reference_adapter_is_required(candidate_arm):
    with pytest.raises(ProtocolV4Error, match="expected an AdapterArmReference"):
        ReferenceAdapterPairing(reference=None, candidate=candidate_arm)


def test_a_candidate_adapter_is_required(reference_arm):
    with pytest.raises(ProtocolV4Error, match="expected an AdapterArmReference"):
        ReferenceAdapterPairing(reference=reference_arm, candidate=None)


def test_both_arms_declare_an_attached_adapter(pairing):
    """A V4 arm cannot serialise itself as a bare base model."""
    for arm in (pairing.reference, pairing.candidate):
        assert arm.to_dict()["adapter_attached"] is True
    record = pairing.to_record()
    assert record["reference_arm_type"] == "ADAPTER"
    assert record["candidate_arm_type"] == "ADAPTER"


# ══════════════════════════════════════════════════════════════════════════════
#  §11.3 + §12 — reference != candidate, and roles are not positional
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field", DISTINCT_ARM_IDENTITY_FIELDS)
def test_an_arm_may_not_be_compared_with_itself(reference_arm, field):
    twin = make_arm(EvaluationArmRole.CANDIDATE, "cand",
                    **{field: getattr(reference_arm, field)})
    with pytest.raises(ProtocolV4Error, match=f"same {field}"):
        ReferenceAdapterPairing(reference=reference_arm, candidate=twin)


def test_swapping_the_two_arms_is_refused(reference_arm, candidate_arm):
    """The swap the whole role field exists to catch."""
    with pytest.raises(ProtocolV4Error, match="reference arm must declare REFERENCE"):
        ReferenceAdapterPairing(reference=candidate_arm, candidate=reference_arm)


def test_the_same_adapter_hashes_differently_as_reference_and_as_candidate():
    as_reference = make_arm(EvaluationArmRole.REFERENCE, "same")
    as_candidate = make_arm(EvaluationArmRole.CANDIDATE, "same")
    assert as_reference.arm_hash() != as_candidate.arm_hash()
    assert as_reference.to_dict()["evaluation_arm_role"] == "reference"
    assert as_candidate.to_dict()["evaluation_arm_role"] == "candidate"


def test_a_role_that_is_a_bare_string_is_refused():
    with pytest.raises(ProtocolV4Error, match="expected an EvaluationArmRole"):
        make_arm("reference", "ref")  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════════════
#  §11.4-6 — the shared identity surface, checked as equality
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field", SHARED_ARM_IDENTITY_FIELDS)
def test_the_arms_must_agree_on_every_shared_identity_field(reference_arm, field):
    differing = "b" * 40 if field == "base_model_revision" else (
        "synthetic/other-model" if field == "base_model_id" else _d(f"other-{field}"))
    other = make_arm(EvaluationArmRole.CANDIDATE, "cand", **{field: differing})
    with pytest.raises(ProtocolV4Error, match=f"different {field}"):
        ReferenceAdapterPairing(reference=reference_arm, candidate=other)


def test_a_branch_or_tag_is_not_a_pinned_revision():
    with pytest.raises(ProtocolV4Error, match="pinned revision"):
        make_arm(EvaluationArmRole.REFERENCE, "ref",
                 base_model_revision="main".ljust(40, "z"))


@pytest.mark.parametrize("field", [
    "adapter_sha256", "adapter_manifest_hash", "adapter_artifact_set_hash",
    "training_receipt_sha256", "base_model_identity_hash", "tokenizer_identity_hash",
    "tokenizer_chat_template_hash",
])
def test_a_missing_identity_digest_is_refused_rather_than_defaulted(field):
    with pytest.raises(Exception):
        make_arm(EvaluationArmRole.REFERENCE, "ref", **{field: ""})


# ══════════════════════════════════════════════════════════════════════════════
#  §11.11-13 — one paired attempt, one spend, 2N generations
# ══════════════════════════════════════════════════════════════════════════════
def test_one_paired_attempt_spends_the_holdout_exactly_once(pairing):
    plan = PairedSpendPlan(pairing=pairing, task_count=36)
    assert plan.holdout_spends == HOLDOUT_SPENDS_PER_PAIRED_ATTEMPT == 1
    assert plan.to_dict()["holdout_spends"] == 1


def test_a_second_independent_spend_remains_forbidden(pairing):
    with pytest.raises(ProtocolV4Error, match="spends its holdout exactly 1 time"):
        PairedSpendPlan(pairing=pairing, task_count=36, holdout_spends=2)


@pytest.mark.parametrize("tasks", [1, 12, 36, 100])
def test_generation_accounting_is_two_per_task(pairing, tasks):
    assert pairing.expected_generations(tasks) == GENERATIONS_PER_TASK * tasks
    assert PairedSpendPlan(pairing=pairing,
                           task_count=tasks).expected_generations == 2 * tasks


def test_thirty_six_canonical_tasks_means_seventy_two_generations(pairing):
    assert PairedSpendPlan(pairing=pairing, task_count=36).expected_generations == 72


def test_a_spend_plan_authorises_nothing(pairing):
    plan = PairedSpendPlan(pairing=pairing, task_count=36).to_dict()
    assert plan["retry_authorized"] is False
    assert plan["grants_authority"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  §11.14-15 — arm independence
# ══════════════════════════════════════════════════════════════════════════════
def test_the_two_arms_get_two_backend_objects():
    made: list[object] = []

    def factory(role: str) -> object:
        made.append(obj := type("Backend", (), {"role": role})())
        return obj

    reference, candidate = paired_arm_backends(factory)
    assert reference is not candidate
    assert [b.role for b in made] == ["reference", "candidate"]


def test_one_shared_backend_object_cannot_prove_which_adapter_answered():
    shared = object()
    with pytest.raises(ProtocolV4Error, match="same backend object"):
        paired_arm_backends(lambda _role: shared)


def test_a_reference_answer_may_not_enter_the_candidate_prompt():
    answer = "The reference arm's full synthetic answer to the frozen task, verbatim."
    with pytest.raises(ProtocolV4Error, match="produced by the other arm"):
        assert_no_cross_arm_context(prompt=f"Earlier answer: {answer}\nNow answer:",
                                    other_arm_outputs=[answer])


def test_a_candidate_answer_may_not_enter_the_reference_prompt():
    answer = "The candidate arm's full synthetic answer to the frozen task, verbatim."
    with pytest.raises(ProtocolV4Error, match="produced by the other arm"):
        assert_no_cross_arm_context(prompt=f"For reference: {answer}",
                                    other_arm_outputs=[answer])


def test_an_independent_prompt_is_accepted():
    assert_no_cross_arm_context(
        prompt="Answer the frozen task from the pack and nothing else.",
        other_arm_outputs=["a wholly unrelated synthetic answer of sufficient length"])


def test_sharing_a_short_phrase_is_not_treated_as_leakage():
    """Two correct answers legitimately share words; containment is the property."""
    assert_no_cross_arm_context(prompt="Answer the task. Be concise.",
                                other_arm_outputs=["Be concise."])


# ══════════════════════════════════════════════════════════════════════════════
#  §11.16-17 + §10 — the old protocol is untouched
# ══════════════════════════════════════════════════════════════════════════════
def test_the_old_base_versus_adapter_references_still_work_unchanged():
    from training_gym.evaluation.references import (
        BaseModelEvaluationReference,
        EvaluationRole,
    )
    assert EvaluationRole.BASELINE.value == "baseline"
    assert EvaluationRole.CANDIDATE.value == "candidate"
    # The old baseline arm still carries no adapter field of any kind.
    fields = set(BaseModelEvaluationReference.__dataclass_fields__)
    assert not any("adapter" in name for name in fields)


def test_protocol_v4_does_not_reuse_the_old_baseline_role():
    """The old BASELINE member carries the no-adapter promise. V4 does not borrow it."""
    assert {r.value for r in EvaluationArmRole} == {"reference", "candidate"}
    assert "baseline" not in {r.value for r in EvaluationArmRole}


def test_the_frozen_gate_policy_digest_is_unmoved_by_protocol_v4():
    from training_gym.evaluation.policy import EvaluationPolicySet
    policies = EvaluationPolicySet()
    assert policies.gates.policy_hash() == (
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
    assert policies.statistics.policy_hash() == (
        "663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18")
    assert policies.families.policy_hash() == (
        "580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1")
    assert policies.metrics.policy_hash() == (
        "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")


def test_every_gate_reads_two_arm_metric_bundles_and_no_model_identity():
    """The §8 equivalence property, asserted against the source rather than promised."""
    source = (REPO_ROOT / "jarvis" / "training_gym" / "evaluation" / "gates.py").read_text()
    for forbidden in ("base_model", "adapter", "bare base", "no adapter"):
        assert forbidden not in source, (
            f"gates.py mentions {forbidden!r}; the gate stack must stay arm-relative so "
            f"the reference arm's identity is not baked into a threshold")


def test_old_receipts_still_verify_against_their_own_schema():
    """No migration. v3 receipts keep validating as v3, unchanged."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_DIR / "m62-eval-receipt-v3.schema.json").read_text())
    for name in ("qwen3-06b-lora-quality-live-003", "qwen3-06b-lora-quality-live-004"):
        receipt = json.loads((RECEIPTS / f"{name}.eval.json").read_text())
        jsonschema.validate(receipt, schema)
        assert receipt["receipt_version"] == "m62.eval_receipt.3"


# ══════════════════════════════════════════════════════════════════════════════
#  §9 + §12 — the V4 receipt schema, and the two ways it must refuse
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v4_schema_has_no_baseline_field_at_all():
    schema = json.loads((SCHEMA_DIR / "m62-eval-receipt-v4.schema.json").read_text())
    assert "baseline" not in schema["properties"]
    assert "baseline" not in schema["required"]
    assert schema["additionalProperties"] is False
    for arm in ("reference_arm", "candidate_arm"):
        block = schema["properties"][arm]
        assert block["properties"]["adapter_attached"] == {"const": True}
        assert block["properties"]["arm_type"] == {"const": "ADAPTER"}
        assert "adapter_sha256" in block["required"]
        assert "training_receipt_sha256" in block["required"]


def test_a_v3_receipt_is_refused_when_read_as_v4():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_DIR / "m62-eval-receipt-v4.schema.json").read_text())
    receipt = json.loads(
        (RECEIPTS / "qwen3-06b-lora-quality-live-004.eval.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, schema)


def test_a_v4_shaped_receipt_is_refused_when_read_as_v3():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_DIR / "m62-eval-receipt-v3.schema.json").read_text())
    receipt = json.loads(
        (RECEIPTS / "qwen3-06b-lora-quality-live-004.eval.json").read_text())
    receipt.pop("baseline")
    receipt["reference_arm"] = {"arm_type": "ADAPTER"}
    receipt["receipt_version"] = "m62.eval_receipt.4"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, schema)


def test_the_v4_schema_pins_one_spend_and_two_generations_per_task():
    schema = json.loads((SCHEMA_DIR / "m62-eval-receipt-v4.schema.json").read_text())
    pairing = schema["properties"]["pairing"]["properties"]
    assert pairing["holdout_spends"] == {"const": 1}
    assert pairing["generations_per_task"] == {"const": 2}
    assert pairing["retry_authorized"] == {"const": False}
    assert pairing["protocol_version"] == {"const": EVALUATION_PROTOCOL_V4_VERSION}


# ══════════════════════════════════════════════════════════════════════════════
#  §11.9-10 — identity comes from the sealed training receipt, never the caller
# ══════════════════════════════════════════════════════════════════════════════
def _real_arm(candidate: str, role: EvaluationArmRole) -> AdapterArmReference:
    path = RECEIPTS / f"qwen3-06b-lora-quality-live-{candidate}.train.json"
    raw = path.read_bytes()
    return arm_from_training_receipt(
        json.loads(raw), role=role, run_id=f"s4d-{candidate}",
        training_receipt_sha256=hashlib.sha256(raw).hexdigest())


def test_the_reference_arm_records_candidate_004s_real_adapter_identity():
    arm = _real_arm("004", EvaluationArmRole.REFERENCE)
    assert arm.candidate_id == "qwen3-06b-lora-quality-live-004"
    assert arm.adapter_sha256 == (
        "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67")
    assert arm.adapter_manifest_hash == (
        "162e93e36f284b651051a93e22cfc6cb15adef3f457038297ca72774e276b510")
    assert arm.role is EvaluationArmRole.REFERENCE


def test_the_candidate_arm_records_candidate_005s_real_adapter_identity():
    arm = _real_arm("005", EvaluationArmRole.CANDIDATE)
    assert arm.candidate_id == "qwen3-06b-lora-quality-live-005"
    assert arm.adapter_sha256 == (
        "52d6da26dca20dce93de8845fa08e0b3e452d86472fd6e06d756a30e52688f2a")
    assert arm.adapter_manifest_hash == (
        "7442246c3d85f1007fe6885714ffbdbe7c53c6bfd251e3c36ca29ab7b489f78f")
    assert arm.role is EvaluationArmRole.CANDIDATE


def test_the_real_pairing_shares_one_base_model_and_differs_only_by_adapter():
    pairing = ReferenceAdapterPairing(
        reference=_real_arm("004", EvaluationArmRole.REFERENCE),
        candidate=_real_arm("005", EvaluationArmRole.CANDIDATE))
    assert pairing.reference.base_model_id == pairing.candidate.base_model_id
    assert pairing.reference.base_model_revision == (
        "c1899de289a04d12100db370d81485cdf75e47ca")
    assert pairing.expected_generations(36) == 72
    assert pairing.to_dict()["holdout_spends"] == 1


def test_a_receipt_missing_its_adapter_block_is_refused():
    with pytest.raises(ProtocolV4Error, match="missing"):
        arm_from_training_receipt({"candidate_id": "x"},
                                  role=EvaluationArmRole.REFERENCE,
                                  run_id="r", training_receipt_sha256=_d("r"))


# ══════════════════════════════════════════════════════════════════════════════
#  §11.19-20 — building V4 creates no authority and loads no weights
# ══════════════════════════════════════════════════════════════════════════════
def test_building_a_pairing_creates_no_authority(pairing):
    """No authority form, and no field that could carry one, reaches the record.

    ``token`` as a bare substring is deliberately NOT the check: it matches
    ``tokenizer_identity_hash``, which is an identity digest and exactly the sort of
    field this record is supposed to carry.
    """
    record = pairing.to_record()
    flat = json.dumps(record)
    for form in ("EVAL:", "TRAIN:", "PROMOTE:", "authority", "confirmation_token",
                 "plan_token"):
        assert form not in flat
    assert "grants_authority" not in record
    assert record["holdout_spends"] == 1


def test_this_module_imports_no_model_runtime():
    source = (REPO_ROOT / "jarvis" / "training_gym" / "evaluation"
              / "protocol_v4.py").read_text()
    for forbidden in ("import torch", "transformers", "peft", "from_pretrained",
                      "safetensors", "generate("):
        assert forbidden not in source, (
            f"protocol_v4.py references {forbidden!r}; the representation layer must "
            f"not be able to load a model or produce a generation")


def test_the_protocol_version_is_pinned_and_additive():
    assert EVALUATION_PROTOCOL_V4_VERSION == "m62.evaluation_protocol.4"
    with pytest.raises(ProtocolV4Error, match="this module speaks"):
        make_arm(EvaluationArmRole.REFERENCE, "ref",
                 protocol_version="m62.evaluation_protocol.5")


def test_the_pairing_record_carries_no_holdout_body_and_no_candidate_output(pairing):
    record = pairing.to_record()
    flat = json.dumps(record)
    assert "prompt" not in flat and "target" not in flat and "answer" not in flat
    # Every value is an identifier, a digest, a count or a closed constant.
    for key, value in record.items():
        if isinstance(value, str):
            assert len(value) <= 320, f"{key} is long enough to smuggle a body"
