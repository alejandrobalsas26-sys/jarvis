#!/usr/bin/env python3
"""build_m62_measurement_witness.py — V69 M62 S4F: witnessing a measurement at its source.

WHY THIS EXISTS
---------------
`build_m62_eval_receipt.py` binds an evaluation's source commit through a PRE-REPAIR
MEASUREMENT WITNESS rather than through `git rev-parse HEAD`, because by the time a
receipt is sealed HEAD has usually moved on to the sealing commit. The witness is the
document that fixes the evaluation source: it is written while the worktree is still
clean at that commit, and `measurement_witness_evidence` later requires that the commit
introducing it has the evaluation source as its FIRST PARENT.

The first two witnesses (S3Q, S3Y) were authored by hand. Doing that a third time means
hand-computing a self-excluding digest over a nested document whose every field is
cross-checked against a runtime derivation — so this script derives each field with the
SAME helper the receipt verifier will check it against, and refuses to write a witness
whose own contract it cannot satisfy.

NOTHING HERE RUNS, RE-SCORES OR RE-GENERATES ANYTHING. No model is loaded, no backend is
constructed, no response is opened. Every number is read from durable evidence that
already exists, and every file is opened through `file_evidence`, which can return only
a digest, a byte count and a line count.
"""
from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 — fixed argv, shell=False
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.build_m62_eval_receipt import (  # noqa: E402
    MEASUREMENT_WITNESS_SCHEMA_VERSION,
    RECEIPT_V2_SEAL_FAILURE_CLASSES,
    RESULT_COUNT_FILES,
    WITNESS_PURPOSE,
    WITNESS_SOURCE_DIGEST_METHOD,
    WITNESS_SOURCE_SCOPE,
    ReceiptError,
    _sha256_bytes,
    _sha256_obj,
    comparison_partitions,
    file_evidence,
    ledger_evidence,
    read_measurement_witness,
)
from scripts.verify_m62_control_plane import canonical_json  # noqa: E402

WITNESS_EVIDENCE_LEVEL = (
    "the witness was written in a clean worktree at this commit; it establishes "
    "repository provenance and does not prove which bytes executed")

#: When the witness and the tooling that writes it land in the SAME commit — which they
#: must, because the witness's Git first parent has to be the evaluation source and the
#: tooling does not exist at that commit — the worktree cannot be clean at witness time.
#: Recording `worktree_clean_at_witness: true` there would be false, so the weaker,
#: accurate claim is recorded instead, and it still says which bytes were verified against
#: HEAD. S3Q and S3Y were authored by hand and carry the stronger sentence.
WITNESS_EVIDENCE_LEVEL_SEAL = (
    "the witness was written at this commit with the sealing tooling already in the "
    "worktree, so the worktree was not clean; the evaluation machinery the digest covers "
    "was verified byte-identical to this commit before the digest was taken. That is "
    "repository provenance and does not prove which bytes executed")

GRANTS_NOTE = "a witness records facts; it authorises nothing"


def _git(root: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(  # nosec B603 — fixed argv, shell=False
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout.strip()


def evaluation_source_identity(repo_root: Path) -> dict:
    """Digest the tracked evaluation machinery, exactly as the witness method string says.

    `sha256(canonical_json({tracked_path: sha256(bytes)}))` over every tracked file under
    the witness source scope. A reader with the repository and that sentence can rederive
    this number; that is the whole point of recording the method beside the digest.
    """
    code, head = _git(repo_root, "rev-parse", "HEAD")
    if code != 0:
        raise ReceiptError("HEAD could not be derived")
    code, tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    if code != 0:
        raise ReceiptError("the HEAD tree oid could not be derived")
    code, listing = _git(repo_root, "ls-files", "--", WITNESS_SOURCE_SCOPE)
    if code != 0:
        raise ReceiptError(f"the tracked file list under {WITNESS_SOURCE_SCOPE} failed")
    paths = [p for p in listing.splitlines() if p.strip()]
    if not paths:
        raise ReceiptError(f"no tracked files under {WITNESS_SOURCE_SCOPE}")
    digests = {p: _sha256_bytes((repo_root / p).read_bytes()) for p in sorted(paths)}

    # THE INVARIANT THAT MAKES THE DIGEST MEAN ANYTHING: the bytes just digested off the
    # working tree must be the bytes HEAD recorded. A witness has to be written before its
    # own commit exists, so the tooling that writes it is necessarily already in the
    # worktree; what may NOT differ is the evaluation machinery the digest covers.
    code, scope_diff = _git(repo_root, "status", "--porcelain", "--", WITNESS_SOURCE_SCOPE)
    if code != 0 or scope_diff.strip():
        raise ReceiptError(
            f"{WITNESS_SOURCE_SCOPE} differs from HEAD; a source digest taken over edited "
            f"evaluation machinery describes a state no commit carries")

    code, dirty = _git(repo_root, "status", "--porcelain")
    clean = code == 0 and not dirty.strip()
    return {
        "derived_from_repository_head": True,
        "evaluation_source_commit": head,
        "evaluation_source_digest": _sha256_bytes(
            canonical_json(digests).encode("utf-8")),
        "evaluation_source_digest_method": WITNESS_SOURCE_DIGEST_METHOD,
        "evaluation_source_file_count": len(digests),
        "evaluation_source_tree_oid": tree,
        "evidence_level": WITNESS_EVIDENCE_LEVEL if clean else WITNESS_EVIDENCE_LEVEL_SEAL,
        "worktree_clean_at_witness": clean,
    }


def build_witness(generation_directory: str | Path, *, ledger: str | Path,
                  attempts_ledger: str | Path, candidate_id: str, milestone: str,
                  repo_root: str | Path = REPO_ROOT) -> dict:
    """Derive the whole witness from durable evidence. Nothing is accepted as prose."""
    root, directory = Path(repo_root), Path(generation_directory)
    report = json.loads((directory / "evaluation-report.json").read_text("utf-8"))
    manifest = json.loads((directory / "evaluation-manifest.json").read_text("utf-8"))
    metrics = json.loads((directory / "metrics.json").read_text("utf-8"))
    plan = json.loads((directory / "evaluation-plan.json").read_text("utf-8"))

    # The corpus IDENTITY comes from the durable paired-attempt record, not from the
    # persisted plan: what is on disk beside the results is the INNER v1-v3 plan, and it
    # has no field naming which holdout the V4 wrapper spent.
    attempts = [json.loads(line) for line
                in Path(attempts_ledger).read_text("utf-8").splitlines() if line.strip()]
    if len(attempts) != 1:
        raise ReceiptError(
            f"the attempts ledger carries {len(attempts)} paired attempt(s); a witness "
            f"describes exactly one measurement")
    attempt = attempts[0]

    evaluation_id = str(report["evaluation_id"])
    generation = int(report["generation"])
    events = ledger_evidence(ledger, evaluation_id=evaluation_id, generation=generation)

    measured_pairs = int(report["measured_pairs"])
    partitions = comparison_partitions(directory, measured_pairs=measured_pairs)
    counts = {name: file_evidence(directory / source)["record_count"]
              for source, name in RESULT_COUNT_FILES}

    source = evaluation_source_identity(root)

    body = {
        "candidate_id": candidate_id,
        "eval_corpus": {
            "dataset_id": str(attempt["dataset_id"]),
            "dataset_manifest_hash": str(report["dataset_manifest_hash"]),
            "dataset_version": str(attempt["dataset_version"]),
            "hidden_target_store_hash": str(report["hidden_target_store_hash"]),
            "pack_manifest_hash": str(report["task_pack_hash"]),
            "spent_once": True,
            "status_claim": "USED_IMMUTABLE",
            "task_pack_hash": str(report["task_pack_hash"]),
        },
        "evaluation_generation": generation,
        "evaluation_id": evaluation_id,
        "evaluation_source": source,
        "evidence": {
            "bootstrap_report_hash": _sha256_obj(report["bootstrap"]),
            "comparison_manifest_hash": str(report["comparison_manifest_hash"]),
            "evaluation_artifact_tree_hash": str(manifest["tree_hash"]),
            "evaluation_manifest_hash": str(manifest["manifest_hash"]),
            "files": {p.name: file_evidence(p)
                      for p in sorted(directory.iterdir()) if p.is_file()},
            "gate_report_hash": _sha256_obj(report["gate_report"]),
            "metrics_summary_hash": str(metrics["summary_hash"]),
            "report_hash": str(report["report_hash"]),
        },
        "grants": {
            "activation": False, "candidate_state": False,
            "is_an_evaluation_receipt": False, "note": GRANTS_NOTE,
            "promotion": False, "registry_mutation": False, "retry_or_rerun": False,
        },
        "ledger": {
            "holdout_commit_count": int(events["counts"].get(
                "holdout_model_facing_committed", 0)),
            "holdout_commit_event_hash":
                events["event_hashes"]["holdout_commit_event_hash"],
            "plan_hash": str(events["plan_hash"]),
            "plan_started_count": int(events["counts"].get("started", 0)),
            "plan_started_event_hash":
                events["event_hashes"]["plan_started_event_hash"],
            "terminal_count": int(events["counts"].get(events["terminal_event"], 0)),
            "terminal_event": str(events["terminal_event"]),
            "terminal_event_hash": events["event_hashes"]["terminal_event_hash"],
            "unique_plan_hashes": 1,
            "unrecognised_events": list(events["unrecognised_events"]),
        },
        "milestone": milestone,
        "outcome": {
            "activates_model": bool(report["activates_model"]),
            "canonical_eligibility": str(report["eligibility"]["eligibility"]),
            "decision_hash": _sha256_obj(report["eligibility"]),
            "human_review_required": bool(report["eligibility"]["human_review_required"]),
            "mutates_model_registry": bool(report["mutates_model_registry"]),
            "promotes_model": bool(report["promotes_model"]),
            "rederived_by": "training_gym.evaluation.reports.decision_from_evidence",
        },
        "plan": {
            "evaluation_config_hash": str(plan["evaluation_config_hash"]),
            "expected_task_count": int(plan["expected_task_count"]),
            "order_assignment_hash": str(plan["order_assignment_hash"]),
            "performs_inference": bool(plan["performs_inference"]),
            # The INNER plan hash, which is what the report publishes and what the
            # receipt cross-checks this field against. The V4 plan hash that carried the
            # authority is a different number and is bound under `ledger.plan_hash`.
            "plan_hash": str(report["plan_hash"]),
            "plan_schema_version": str(plan["plan_schema_version"]),
        },
        "purpose": WITNESS_PURPOSE,
        "receipt_v2_seal_failure_classes": list(RECEIPT_V2_SEAL_FAILURE_CLASSES),
        "results": {
            "baseline_result_count": counts["baseline_result_count"],
            "baseline_score_count": counts["baseline_score_count"],
            "candidate_result_count": counts["candidate_result_count"],
            "candidate_score_count": counts["candidate_score_count"],
            "measured_pairs": measured_pairs,
            "missing_pairs": int(report["missing_pairs"]),
            "numeric_delta_counts": dict(partitions["numeric_delta_counts"]),
            "paired_result_count": counts["paired_result_count"],
            "task_count": int(report["task_count"]),
            "total_model_result_count": (counts["baseline_result_count"]
                                         + counts["candidate_result_count"]),
            "verdict_counts": dict(partitions["verdict_counts"]),
        },
        "schema_version": MEASUREMENT_WITNESS_SCHEMA_VERSION,
        "witness_kind": "pre_repair_measurement_witness",
        "witness_version": MEASUREMENT_WITNESS_SCHEMA_VERSION,
    }
    return {**body,
            "witness_hash": _sha256_bytes(canonical_json(body).encode("utf-8"))}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(description="Witness a measurement at its source.")
    parser.add_argument("--generation-directory", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--attempts-ledger", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--milestone", required=True)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    payload = build_witness(args.generation_directory, ledger=args.ledger,
                            attempts_ledger=args.attempts_ledger,
                            candidate_id=args.candidate_id, milestone=args.milestone,
                            repo_root=args.repo_root)
    text = canonical_json(payload).encode("utf-8")
    if args.out:
        Path(args.out).write_bytes(text)
        # Read it back through the contract it must satisfy, so a witness that cannot be
        # consumed is never left on disk looking sealed.
        read_measurement_witness(args.out)
    print(json.dumps({"status": "witnessed",
                      "witness_hash": payload["witness_hash"],
                      "bytes": len(text)}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
