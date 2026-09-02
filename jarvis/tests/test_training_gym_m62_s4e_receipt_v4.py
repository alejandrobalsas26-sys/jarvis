"""V69 M62 S4E — the Protocol V4 receipt, built from fake outputs and then attacked.

WHAT IS BEING PROVED
--------------------
That a v4 receipt can be built end to end from a completed run, that it BINDS every fact
an auditor would need, and — the part that matters — that it REFUSES each of the ways it
could describe something that did not happen.

Every mutation below is REHASHED before it is checked. Mutating without rehashing only
proves the digest works; rehashing asks the real question: is this fact CHECKED, or
merely recorded?

NOTHING HERE RUNS A MODEL. The world is the synthetic sealing world S3Q.0.2 already uses,
and the reference arm's training receipt is a synthetic sibling of the candidate's.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.build_m62_eval_receipt import ReceiptError, receipt_hash, seal
from scripts.build_m62_eval_receipt_v4 import (
    GENERATIONS_PER_TASK,
    build_receipt_v4,
    verify_receipt_v4,
)
from scripts.verify_m62_control_plane import (
    EVAL_RECEIPT_V3_SCHEMA_VERSION,
    EVAL_RECEIPT_V4_SCHEMA_VERSION,
    canonical_bytes,
    eval_receipt_v3_schema,
    eval_receipt_v4_schema,
    validate_against_schema,
)

import _s3q02_synthetic as R

_REFERENCE_RUN = "qwen3-06b-lora-quality-live-reference"


_V4_PLAN_HASH = "b" * 64


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    # D-S4F-2: a V4 ledger names the OUTER plan. Passed here rather than rewritten later
    # because the witness binds every ledger line by digest.
    return R.recovery_world(tmp_path_factory, ledger_plan_hash=_V4_PLAN_HASH)


@pytest.fixture(scope="module")
def v4_world(world, tmp_path_factory):
    """The v3 world, plus a reference arm and the durable paired-attempt record."""
    # Written INSIDE the synthetic repository: `.3` refuses a training receipt outside
    # it, because portable evidence may not point at a host-local file.
    directory = Path(world["repo_root"]) / "state" / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    candidate_receipt_path = Path(world["training_receipt"])
    candidate = json.loads(candidate_receipt_path.read_text(encoding="utf-8"))

    # The S3Q.0.2 world's training receipt is a minimal stub with no ``base_model``
    # block. A real M62 train receipt carries one, and ``_arm`` REFUSES to build an arm
    # without it — correctly, since base identity is what makes the two arms comparable.
    # So both receipts here are enriched to the real shape rather than the builder being
    # loosened to accept the stub.
    base_block = {
        "model_id": "Qwen/Qwen3-0.6B",
        "revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "identity_hash": "9701f4f3368dc13b815b7e6553d9ada462ad59fa179b6c93c625be7494bc3a72",
        "tokenizer_id": "Qwen/Qwen3-0.6B",
        "tokenizer_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "tokenizer_identity_hash":
            "9701f4f3368dc13b815b7e6553d9ada462ad59fa179b6c93c625be7494bc3a72",
        "chat_template_digest":
            "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8",
    }
    candidate = {**candidate, "base_model": dict(base_block)}
    candidate_path = directory / "s4e-candidate.train.json"
    candidate_path.write_text(json.dumps(candidate, sort_keys=True), encoding="utf-8")

    # A synthetic REFERENCE training receipt: a different adapter on the SAME base.
    reference = copy.deepcopy(candidate)
    reference["candidate_id"] = _REFERENCE_RUN
    reference["adapter"] = dict(reference["adapter"])
    reference["adapter"]["sha256"] = "a1" * 32
    reference["adapter"]["manifest_hash"] = "a2" * 32
    reference["adapter"]["artifact_set_hash"] = "a3" * 32
    reference_path = directory / "s4e-reference.train.json"
    reference_path.write_text(json.dumps(reference, sort_keys=True), encoding="utf-8")

    report = json.loads(
        (Path(world["directory"]) / "evaluation-report.json").read_text(encoding="utf-8"))
    task_count = int(report["task_count"])
    plan_hash = _V4_PLAN_HASH
    attempt = {
        "record_version": "m62.evaluation_v4_attempt.1",
        "event": "protocol_v4_paired_attempt",
        "plan_hash": plan_hash,
        "inner_plan_hash": str(report["plan_hash"]),
        "evaluation_id": str(report["evaluation_id"]),
        "generation": int(report["generation"]),
        "protocol_version": "m62.evaluation_protocol.4",
        "pairing_hash": "c" * 64,
        "reference_arm_hash": "d" * 64,
        "candidate_arm_hash": "e" * 64,
        "reference_adapter_sha256": reference["adapter"]["sha256"],
        "candidate_adapter_sha256": candidate["adapter"]["sha256"],
        "common_base_model_id": candidate["base_model"]["model_id"],
        "common_base_model_revision": candidate["base_model"]["revision"],
        "dataset_id": "m62-defensive-eval", "dataset_version": "v7",
        "dataset_manifest_hash": "f" * 64, "task_pack_hash": "1" * 64,
        "task_order_hash": "2" * 64, "task_count": task_count,
        "expected_total_generations": GENERATIONS_PER_TASK * task_count,
        "holdout_spends": 1, "runtime_report_sha256": "3" * 64,
        "evaluation_source_commit": "4" * 40,
        "actor": "test", "at": "2026-09-01T00:00:00Z",
    }
    attempts = directory / "protocol-v4-attempts.jsonl"
    attempts.write_text(json.dumps(attempt, sort_keys=True) + "\n", encoding="utf-8")

    # `.3` refuses to seal from a dirty worktree: a receipt built from uncommitted code
    # names a seal implementation source nobody else can obtain. So the fixture commits
    # what it added, exactly as a real sealing session would have to.
    import subprocess  # nosec B404 — fixed argv, shell=False, test-local repository

    root = str(world["repo_root"])
    subprocess.run(["git", "-C", root, "add", "-A"], check=True,  # nosec B603 B607
                   capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m",  # nosec B603 B607
                    "s4e synthetic paired-attempt evidence"], check=True,
                   capture_output=True)
    return {**world, "reference_receipt": reference_path,
            "candidate_receipt": candidate_path, "attempts": attempts,
            "v4_plan_hash": plan_hash, "task_count": task_count}


def _build(v4_world, **overrides):
    kwargs = dict(
        reference_training_receipt=v4_world["reference_receipt"],
        candidate_training_receipt=v4_world["candidate_receipt"],
        reference_run_id=_REFERENCE_RUN,
        candidate_run_id=json.loads(
            Path(v4_world["candidate_receipt"]).read_text())["candidate_id"],
        attempts_ledger=v4_world["attempts"],
        v4_plan_hash=v4_world["v4_plan_hash"],
        adapter_run_directory=v4_world["adapter"]["directory"],
        evaluation_config=v4_world["config_path"], ledger=v4_world["ledger"],
        measurement_witness=v4_world["witness_path"],
        repo_root=v4_world["repo_root"])
    kwargs.update(overrides)
    return build_receipt_v4(v4_world["directory"], **kwargs)


@pytest.fixture(scope="module")
def receipt(v4_world):
    return _build(v4_world)


def _rehashed(receipt: dict, *path, value) -> dict:
    mutated = copy.deepcopy(receipt)
    node = mutated
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    return mutated


def _refused(receipt: dict, *path, value) -> tuple[str, ...]:
    problems = verify_receipt_v4(_rehashed(receipt, *path, value=value))
    assert problems, f"mutating {'.'.join(map(str, path))} was not refused"
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  The dry run: a complete V4 receipt, from fake outputs
# ══════════════════════════════════════════════════════════════════════════════
def test_a_complete_v4_receipt_builds_and_verifies(receipt):
    assert receipt["schema_version"] == EVAL_RECEIPT_V4_SCHEMA_VERSION
    assert receipt["receipt_version"] == EVAL_RECEIPT_V4_SCHEMA_VERSION
    assert verify_receipt_v4(receipt) == ()


def test_the_receipt_validates_against_the_published_v4_schema(receipt):
    assert validate_against_schema(eval_receipt_v4_schema(), receipt) == []


def test_the_receipt_binds_every_identity_an_auditor_needs(receipt):
    for key in ("reference_arm", "candidate_arm", "pairing", "protocol_version",
                "holdout", "holdout_commit", "authority", "ledger", "plan",
                "policies", "outcome", "results", "execution", "evidence",
                "measurement_witness", "training_receipt", "decision_evidence"):
        assert key in receipt, key


def test_the_receipt_cannot_encode_a_bare_base_reference(receipt):
    """The impossibility is by ABSENCE, not by a rule somebody has to remember."""
    assert "baseline" not in receipt
    assert receipt["reference_arm"]["adapter_attached"] is True
    assert receipt["reference_arm"]["arm_type"] == "ADAPTER"
    assert receipt["candidate_arm"]["adapter_attached"] is True
    assert "baseline" not in eval_receipt_v4_schema()["properties"]


def test_both_arms_state_that_their_identity_came_from_a_training_receipt(receipt):
    for arm in ("reference_arm", "candidate_arm"):
        assert receipt[arm]["identity_source"] == "training_receipt"
        assert len(receipt[arm]["training_receipt_sha256"]) == 64


def test_the_pairing_records_one_spend_and_two_generations_per_task(receipt, v4_world):
    pairing = receipt["pairing"]
    assert pairing["holdout_spends"] == 1
    assert pairing["generations_per_task"] == 2
    assert pairing["retry_authorized"] is False
    assert pairing["task_count"] == v4_world["task_count"]
    assert pairing["expected_generations"] == 2 * v4_world["task_count"]


def test_the_canonical_bytes_round_trip(receipt):
    raw = canonical_bytes(receipt)
    assert json.loads(raw.decode("utf-8")) == receipt
    assert canonical_bytes(json.loads(raw.decode("utf-8"))) == raw


def test_the_receipt_carries_no_task_body_and_no_private_path(receipt):
    text = json.dumps(receipt)
    assert "/home/" not in text and "/Users/" not in text
    for marker in ("he7-", "sr7-", "adv7-"):
        assert marker not in text


# ══════════════════════════════════════════════════════════════════════════════
#  Attacks — each must be REFUSED
# ══════════════════════════════════════════════════════════════════════════════
def test_omitting_the_reference_arm_is_refused(receipt):
    mutated = copy.deepcopy(receipt)
    mutated.pop("reference_arm")
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    assert verify_receipt_v4(mutated)


def test_omitting_the_candidate_arm_is_refused(receipt):
    mutated = copy.deepcopy(receipt)
    mutated.pop("candidate_arm")
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    assert verify_receipt_v4(mutated)


def test_swapping_the_arms_is_refused(receipt):
    """Roles are declared, not positional, so a swap contradicts itself."""
    mutated = copy.deepcopy(receipt)
    mutated["reference_arm"], mutated["candidate_arm"] = (
        copy.deepcopy(receipt["candidate_arm"]), copy.deepcopy(receipt["reference_arm"]))
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    problems = verify_receipt_v4(mutated)
    assert problems


def test_duplicating_one_arm_into_both_slots_is_refused(receipt):
    mutated = copy.deepcopy(receipt)
    duplicate = copy.deepcopy(receipt["reference_arm"])
    duplicate["evaluation_arm_role"] = "candidate"
    mutated["candidate_arm"] = duplicate
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    problems = verify_receipt_v4(mutated)
    assert any("with itself" in p for p in problems)


def test_reintroducing_a_baseline_object_is_refused(receipt):
    mutated = copy.deepcopy(receipt)
    mutated["baseline"] = {"model_id": "Qwen/Qwen3-0.6B", "revision": "a" * 40}
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    problems = verify_receipt_v4(mutated)
    assert problems, "a v4 receipt must not carry a bare-base baseline object"


def test_an_arm_claiming_no_adapter_is_refused(receipt):
    assert _refused(receipt, "reference_arm", "adapter_attached", value=False)


def test_an_arm_claiming_a_base_model_type_is_refused(receipt):
    assert _refused(receipt, "reference_arm", "arm_type", value="BASE_MODEL")


def test_arms_on_different_base_revisions_are_refused(receipt):
    problems = _refused(receipt, "candidate_arm", "base_model_revision", value="9" * 40)
    assert any("single axis" in p for p in problems)


def test_arms_on_different_tokenizers_are_refused(receipt):
    problems = _refused(receipt, "candidate_arm", "tokenizer_identity_hash",
                        value="9" * 64)
    assert any("single axis" in p for p in problems)


def test_a_pairing_naming_a_different_reference_arm_is_refused(receipt):
    problems = _refused(receipt, "pairing", "reference_arm_hash", value="9" * 64)
    assert any("wrong way round" in p or "different reference arm" in p
               for p in problems)


def test_a_pairing_claiming_two_holdout_spends_is_refused(receipt):
    assert _refused(receipt, "pairing", "holdout_spends", value=2)


def test_a_pairing_authorising_a_retry_is_refused(receipt):
    assert _refused(receipt, "pairing", "retry_authorized", value=True)


def test_a_pairing_claiming_one_generation_per_task_is_refused(receipt):
    assert _refused(receipt, "pairing", "generations_per_task", value=1)


def test_claiming_more_generations_than_were_measured_is_refused(receipt, v4_world):
    """72 claimed against 71 recorded is the exact shape this must catch."""
    problems = _refused(receipt, "pairing", "expected_generations",
                        value=2 * v4_world["task_count"] + 1)
    assert problems


def test_a_results_block_that_disagrees_with_the_pairing_is_refused(receipt):
    problems = _refused(receipt, "results", "total_model_result_count", value=71)
    assert any("model results" in p for p in problems)


def test_a_tampered_receipt_hash_is_refused(receipt):
    mutated = copy.deepcopy(receipt)
    mutated["receipt_hash"] = "0" * 64
    problems = verify_receipt_v4(mutated)
    assert any("receipt_hash" in p for p in problems)


def test_a_v4_payload_parsed_as_v3_is_refused(receipt):
    """The version decides the contract, in both directions."""
    assert validate_against_schema(eval_receipt_v3_schema(), receipt) != []


def test_a_v3_payload_parsed_as_v4_is_refused(world):
    """An old receipt is not silently acceptable as a paired one."""
    from scripts.build_m62_eval_receipt import build_receipt_v3

    v3 = seal(build_receipt_v3(
        world["directory"], training_receipt=world["training_receipt"],
        adapter_run_directory=world["adapter"]["directory"],
        evaluation_config=world["config_path"], ledger=world["ledger"],
        measurement_witness=world["witness_path"], repo_root=world["repo_root"],
        # This world's ledger is a V4 one (D-S4F-2), so `.3` must be told which plan it
        # carries. What is under test is the SHAPE of the resulting payload, not the plan.
        ledger_plan_hash=_V4_PLAN_HASH))
    assert v3["schema_version"] == EVAL_RECEIPT_V3_SCHEMA_VERSION
    assert verify_receipt_v4(v3)
    assert validate_against_schema(eval_receipt_v4_schema(), v3) != []


# ══════════════════════════════════════════════════════════════════════════════
#  Build-time refusals — the receipt cannot be MADE to lie
# ══════════════════════════════════════════════════════════════════════════════
def test_building_with_no_paired_attempt_record_is_refused(v4_world, tmp_path):
    empty = tmp_path / "protocol-v4-attempts.jsonl"
    with pytest.raises(ReceiptError, match="spend nobody recorded|no paired-attempt"):
        _build(v4_world, attempts_ledger=empty)


def test_building_against_another_plans_attempt_is_refused(v4_world):
    with pytest.raises(ReceiptError, match="exactly ONE paired-attempt"):
        _build(v4_world, v4_plan_hash="9" * 64)


def test_building_with_the_same_adapter_on_both_arms_is_refused(v4_world):
    with pytest.raises(ReceiptError, match="same adapter|different run"):
        _build(v4_world, reference_training_receipt=v4_world["candidate_receipt"],
               reference_run_id="qwen3-06b-lora-quality-live-005")


def test_building_when_the_attempt_record_names_another_adapter_is_refused(
        v4_world, tmp_path):
    """The durable record is the authority; the receipt may not out-vote it."""
    attempt = json.loads(Path(v4_world["attempts"]).read_text().splitlines()[0])
    attempt["reference_adapter_sha256"] = "9" * 64
    path = tmp_path / "protocol-v4-attempts.jsonl"
    path.write_text(json.dumps(attempt, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="durable paired-attempt record"):
        _build(v4_world, attempts_ledger=path)
