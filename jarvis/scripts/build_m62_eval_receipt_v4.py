#!/usr/bin/env python3
"""build_m62_eval_receipt_v4.py — V69 M62 S4E: sealing a paired reference-adapter run.

WHY THIS COMPOSES `.3` RATHER THAN REPLACING IT
------------------------------------------------
`.3`'s verification is the expensive, hard-won part: it re-verifies the generation tree,
re-derives the eligibility decision from body-free evidence instead of trusting the
report's claim, cross-checks the ledger against the plan and the report, binds the
measurement witness, and refuses a receipt whose canonical bytes are not the bytes on
disk. None of that is protocol-specific, and rewriting it for V4 would mean maintaining
two copies of the same argument and discovering they had diverged from a real receipt.

So :func:`build_receipt_v4` calls ``build_receipt_v3`` and then applies exactly the
delta the fourth protocol needs:

    REMOVE  baseline          five base-model identity fields with nowhere to put an
                              adapter digest. Keeping it would seal a receipt asserting
                              a bare base model answered when one did not; removing it
                              makes that claim UNEXPRESSIBLE rather than merely false.
    ADD     reference_arm     adapter_attached const true, arm_type const ADAPTER
    ADD     candidate_arm     the same shape, symmetric by construction
    ADD     pairing           holdout_spends const 1, generations_per_task const 2
    ADD     protocol_version  pinned

WHERE THE ARMS COME FROM
------------------------
From the DURABLE PAIRED-ATTEMPT RECORD written before the first generation, cross-checked
against both sealed training receipts. Never from the caller. A caller who may name the
arms is a caller who may name them the wrong way round, and the swap would be invisible
precisely because both arms are the same shape.

NOTHING HERE RUNS, RE-SCORES OR RE-GENERATES ANYTHING. No model is loaded, no backend is
constructed, no response is opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Imported through the ``scripts.`` package path, NOT as a bare top-level module.
# ``jarvis/scripts`` is reachable both ways, and importing it both ways creates two
# module objects with two distinct ``ReceiptError`` classes — so a caller's
# ``except ReceiptError`` silently fails to catch the one this module raises. The whole
# suite uses the package path, so this does too.
from scripts.build_m62_eval_receipt import (  # noqa: E402
    ReceiptError,
    build_receipt_v3,
    receipt_hash,
    seal,
)
from scripts.verify_m62_control_plane import (  # noqa: E402
    EVAL_RECEIPT_V4_SCHEMA_VERSION,
    canonical_bytes,
    eval_receipt_v4_schema,
    validate_against_schema,
)

PROTOCOL_V4_VERSION = "m62.evaluation_protocol.4"
GENERATIONS_PER_TASK = 2
HOLDOUT_SPENDS = 1


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _arm(receipt: dict, *, role: str, run_id: str,
         training_receipt_sha256: str, arm_hash: str) -> dict:
    """One arm, built from a SEALED TRAINING RECEIPT and nothing the caller supplied."""
    adapter = receipt.get("adapter", {})
    base = receipt.get("base_model", {})
    return {
        "evaluation_arm_role": role,
        "arm_type": "ADAPTER",
        "adapter_attached": True,
        "identity_source": "training_receipt",
        "candidate_id": str(receipt.get("candidate_id", "")),
        "run_id": str(run_id),
        "adapter_sha256": str(adapter.get("sha256", "")),
        "adapter_manifest_hash": str(adapter.get("manifest_hash", "")),
        "adapter_artifact_set_hash": str(adapter.get("artifact_set_hash", "")),
        "training_receipt_sha256": str(training_receipt_sha256),
        "base_model_id": str(base.get("model_id", "")),
        "base_model_revision": str(base.get("revision", "")),
        "base_model_identity_hash": str(base.get("identity_hash", "")),
        "tokenizer_identity_hash": str(
            base.get("tokenizer_identity_hash", base.get("identity_hash", ""))),
        "tokenizer_chat_template_hash": str(base.get("chat_template_digest", "")),
        "arm_hash": str(arm_hash),
    }


def attempt_evidence(attempts_path: Path, *, plan_hash: str) -> dict:
    """The durable paired-attempt record for THIS plan. One, or a refusal."""
    if not Path(attempts_path).is_file():
        raise ReceiptError(
            f"no paired-attempt record at {Path(attempts_path).name}; a V4 receipt "
            f"describes a spend, and a spend nobody recorded is not one")
    matching = []
    for line in Path(attempts_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if str(entry.get("plan_hash")) == str(plan_hash):
            matching.append(entry)
    if len(matching) != 1:
        raise ReceiptError(
            f"expected exactly ONE paired-attempt record for plan {str(plan_hash)[:12]}, "
            f"found {len(matching)}. One paired attempt, one spend")
    return matching[0]


def build_receipt_v4(generation_directory: str | Path, *,
                     reference_training_receipt: str | Path,
                     candidate_training_receipt: str | Path,
                     reference_run_id: str,
                     candidate_run_id: str,
                     attempts_ledger: str | Path,
                     v4_plan_hash: str,
                     adapter_run_directory: str | Path,
                     evaluation_config: str | Path,
                     ledger: str | Path,
                     measurement_witness: str | Path,
                     repo_root: str | Path,
                     expected_candidate: str = "",
                     expected_evaluation_source_commit: str = "",
                     milestone: str = "S4E",
                     seal_milestone: str = "S4E") -> dict:
    """Seal ONE paired attempt. Every `.3` verification runs first, unchanged."""
    # D-S4F-2. The attempt record is read FIRST, because `.3` has to be told which plan
    # the ledger carries and that claim may not be taken on trust. V4 binds an outer plan;
    # the report publishes the inner plan the outer one contains. The durable record names
    # BOTH, so the containment is proved here, from evidence, before `.3` is allowed to
    # expect anything other than the plan the report names.
    attempt = attempt_evidence(Path(attempts_ledger), plan_hash=v4_plan_hash)
    report_plan_hash = str(json.loads(
        (Path(generation_directory) / "evaluation-report.json").read_text(
            encoding="utf-8"))["plan_hash"])
    recorded_inner = str(attempt.get("inner_plan_hash", ""))
    if recorded_inner != report_plan_hash:
        raise ReceiptError(
            f"the paired-attempt record says plan {str(v4_plan_hash)[:12]} contains inner "
            f"plan {recorded_inner[:12]} and the report published "
            f"{report_plan_hash[:12]}; the outer plan that was authorised did not contain "
            f"the measurement this report describes")

    payload = build_receipt_v3(
        generation_directory,
        training_receipt=candidate_training_receipt,
        adapter_run_directory=adapter_run_directory,
        evaluation_config=evaluation_config, ledger=ledger,
        measurement_witness=measurement_witness, repo_root=repo_root,
        expected_candidate=expected_candidate,
        expected_evaluation_source_commit=expected_evaluation_source_commit,
        ledger_plan_hash=str(v4_plan_hash),
        milestone=milestone, seal_milestone=seal_milestone)
    reference_receipt = json.loads(
        Path(reference_training_receipt).read_text(encoding="utf-8"))
    candidate_receipt = json.loads(
        Path(candidate_training_receipt).read_text(encoding="utf-8"))

    reference_arm = _arm(
        reference_receipt, role="reference", run_id=reference_run_id,
        training_receipt_sha256=sha256_file(Path(reference_training_receipt)),
        arm_hash=str(attempt.get("reference_arm_hash", "")))
    candidate_arm = _arm(
        candidate_receipt, role="candidate", run_id=candidate_run_id,
        training_receipt_sha256=sha256_file(Path(candidate_training_receipt)),
        arm_hash=str(attempt.get("candidate_arm_hash", "")))

    # ── the arms must be the arms the durable record named ───────────────────
    for label, arm, recorded in (
            ("reference", reference_arm, attempt.get("reference_adapter_sha256")),
            ("candidate", candidate_arm, attempt.get("candidate_adapter_sha256"))):
        if arm["adapter_sha256"] != str(recorded):
            raise ReceiptError(
                f"the {label} arm's sealed training receipt names adapter "
                f"{arm['adapter_sha256'][:12]}, the durable paired-attempt record names "
                f"{str(recorded)[:12]}. The receipt would describe a different run")
    if reference_arm["adapter_sha256"] == candidate_arm["adapter_sha256"]:
        raise ReceiptError(
            "both arms name the same adapter; a comparison of an adapter with itself "
            "measures nothing")
    for field in ("base_model_id", "base_model_revision", "base_model_identity_hash",
                  "tokenizer_identity_hash", "tokenizer_chat_template_hash"):
        if reference_arm[field] != candidate_arm[field]:
            raise ReceiptError(
                f"the arms declare different {field}; the measured delta would be a "
                f"function of two variables and the comparison has no single axis")

    task_count = int(attempt.get("task_count", 0))
    expected = GENERATIONS_PER_TASK * task_count
    results = payload.get("results", {})
    produced = int(results.get("total_model_result_count", -1))
    if produced != expected:
        raise ReceiptError(
            f"the run produced {produced} model results and the paired attempt expected "
            f"{expected} ({task_count} tasks x {GENERATIONS_PER_TASK} arms). A receipt "
            f"claiming a count it did not measure is the failure this refuses")
    if int(attempt.get("holdout_spends", 0)) != HOLDOUT_SPENDS:
        raise ReceiptError("a paired attempt spends its holdout exactly once")

    pairing = {
        "protocol_version": PROTOCOL_V4_VERSION,
        "pairing_hash": str(attempt.get("pairing_hash", "")),
        "reference_arm_hash": reference_arm["arm_hash"],
        "candidate_arm_hash": candidate_arm["arm_hash"],
        "shared_base_model_id": reference_arm["base_model_id"],
        "shared_base_model_revision": reference_arm["base_model_revision"],
        "task_count": task_count,
        "generations_per_task": GENERATIONS_PER_TASK,
        "expected_generations": expected,
        "holdout_spends": HOLDOUT_SPENDS,
        "retry_authorized": False,
    }

    payload.pop("baseline", None)
    payload["protocol_version"] = PROTOCOL_V4_VERSION
    payload["reference_arm"] = reference_arm
    payload["candidate_arm"] = candidate_arm
    payload["pairing"] = pairing
    payload["schema_version"] = EVAL_RECEIPT_V4_SCHEMA_VERSION
    payload["receipt_version"] = EVAL_RECEIPT_V4_SCHEMA_VERSION
    payload["evaluation_milestone"] = milestone
    payload["seal_milestone"] = seal_milestone
    payload.pop("receipt_hash", None)
    return seal(payload)


def verify_receipt_v4(payload: dict) -> tuple[str, ...]:
    """Every way a V4 receipt could describe something that did not happen."""
    problems: list[str] = []
    if str(payload.get("schema_version")) != EVAL_RECEIPT_V4_SCHEMA_VERSION:
        return (f"schema_version is {payload.get('schema_version')!r}, not "
                f"{EVAL_RECEIPT_V4_SCHEMA_VERSION!r}",)
    problems.extend(validate_against_schema(eval_receipt_v4_schema(), payload))

    # The seal.
    stated = str(payload.get("receipt_hash", ""))
    recomputed = receipt_hash({k: v for k, v in payload.items() if k != "receipt_hash"})
    if stated != recomputed:
        problems.append("receipt_hash does not match the payload it seals")

    reference = payload.get("reference_arm", {}) or {}
    candidate = payload.get("candidate_arm", {}) or {}
    pairing = payload.get("pairing", {}) or {}

    if "baseline" in payload:
        problems.append(
            "a v4 receipt carries no `baseline` object; its presence would assert a bare "
            "base model answered")
    if reference.get("evaluation_arm_role") != "reference":
        problems.append("the reference arm does not declare the reference role")
    if candidate.get("evaluation_arm_role") != "candidate":
        problems.append("the candidate arm does not declare the candidate role")
    if reference.get("arm_hash") and reference["arm_hash"] == candidate.get("arm_hash"):
        problems.append("both arms carry the same arm_hash; the arms are indistinguishable")
    for field in ("adapter_sha256", "adapter_manifest_hash", "run_id", "candidate_id"):
        if reference.get(field) and reference.get(field) == candidate.get(field):
            problems.append(f"both arms declare the same {field}; a comparison of an "
                            f"adapter with itself measures nothing")
    for field in ("base_model_id", "base_model_revision", "base_model_identity_hash",
                  "tokenizer_identity_hash", "tokenizer_chat_template_hash"):
        if reference.get(field) != candidate.get(field):
            problems.append(f"the arms declare different {field}; the comparison has no "
                            f"single axis")
    if pairing.get("reference_arm_hash") != reference.get("arm_hash"):
        problems.append("the pairing names a different reference arm than the receipt "
                        "carries; the arms may be the wrong way round")
    if pairing.get("candidate_arm_hash") != candidate.get("arm_hash"):
        problems.append("the pairing names a different candidate arm than the receipt "
                        "carries")
    if pairing.get("shared_base_model_id") != reference.get("base_model_id"):
        problems.append("the pairing's shared base model is not the arms' base model")

    task_count = pairing.get("task_count")
    expected = pairing.get("expected_generations")
    if isinstance(task_count, int) and isinstance(expected, int):
        if expected != GENERATIONS_PER_TASK * task_count:
            problems.append(
                f"expected_generations {expected} is not {GENERATIONS_PER_TASK} x "
                f"{task_count}")
        produced = int((payload.get("results") or {}).get("total_model_result_count", -1))
        if produced != expected:
            problems.append(
                f"the receipt records {produced} model results against "
                f"{expected} expected; a claimed count is not a measured one")
    return tuple(problems)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(description="Seal a Protocol V4 paired receipt.")
    parser.add_argument("--generation-directory", required=True)
    parser.add_argument("--reference-training-receipt", required=True)
    parser.add_argument("--candidate-training-receipt", required=True)
    parser.add_argument("--reference-run-id", required=True)
    parser.add_argument("--candidate-run-id", required=True)
    parser.add_argument("--attempts-ledger", required=True)
    parser.add_argument("--v4-plan-hash", required=True)
    parser.add_argument("--adapter-run-directory", required=True)
    parser.add_argument("--evaluation-config", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--measurement-witness", required=True)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    payload = build_receipt_v4(
        args.generation_directory,
        reference_training_receipt=args.reference_training_receipt,
        candidate_training_receipt=args.candidate_training_receipt,
        reference_run_id=args.reference_run_id,
        candidate_run_id=args.candidate_run_id,
        attempts_ledger=args.attempts_ledger, v4_plan_hash=args.v4_plan_hash,
        adapter_run_directory=args.adapter_run_directory,
        evaluation_config=args.evaluation_config, ledger=args.ledger,
        measurement_witness=args.measurement_witness, repo_root=args.repo_root)
    problems = verify_receipt_v4(payload)
    if problems:
        print(json.dumps({"status": "refused", "problems": list(problems)}, indent=2))
        return 2
    text = canonical_bytes(payload)
    if args.out:
        Path(args.out).write_bytes(text)
    print(json.dumps({"status": "sealed",
                      "receipt_hash": payload["receipt_hash"],
                      "bytes": len(text)}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
