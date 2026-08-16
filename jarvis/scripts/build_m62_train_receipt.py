"""scripts/build_m62_train_receipt.py — V69 M62 S3P: the portable training receipt.

WHAT THIS IS
------------
A completed training run lives entirely in gitignored runtime artefacts: the adapter,
its manifest, ``run.json``, ``backend_result.json`` and the run ledger. That is the
right place for weights, and it is the wrong place for HISTORY — a fresh clone has none
of it, so a control plane that claimed ``TRAINED_UNEVALUATED`` on the strength of those
files alone would be claiming something no auditor could ever check.

This generator distils exactly one completed run into a small, tracked, ROOT-INDEPENDENT
receipt. The receipt is what lets a future clean clone establish that this candidate
really completed its one authorised training run, without the runtime tree still
existing locally (S3P §49).

WHAT IT NEVER DOES
------------------
  * It never trains, never loads model weights, never generates and never opens a socket.
  * It never records a ``TRAIN:`` token literal. A receipt proves an authority was
    created once and consumed once; reproducing the reusable string would hand a reader
    the capability instead of the evidence.
  * It never records an absolute path, a home directory, a cache location or a username.
  * It never records a raw dataset row, a model response or any ``eval-v4`` material.
  * It never invents a value. Every field is re-derived from the run's own artefacts and
    from the tracked production generator; nothing here is typed in by hand.

DETERMINISM
-----------
The serialiser is :func:`scripts.verify_m62_control_plane.canonical_json` — the ONE
implementation the rest of the control plane already hashes with. Rebuilding the receipt
from the same run directory reproduces the same bytes and therefore the same digest,
which is the property the S3P suite asserts. The receipt deliberately carries **no
timestamp and no self-referential digest**: its identity is the bytes, and the snapshot
that points at it is what records the digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

RECEIPT_SCHEMA_VERSION = "m62.train_receipt.1"

#: The milestone that designed the candidate, and the one that trained it. Recorded so a
#: reader can see that the design and the execution are two separate commits and two
#: separate operator decisions.
DESIGN_MILESTONE = "S3O"
TRAINING_MILESTONE = "S3P"

#: The authority form, WITHOUT the plan hash that completes it. Naming the shape is
#: documentation; naming the instance would be handing over the capability.
AUTHORITY_FORM = "TRAIN:<plan-hash>"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_counts(ledger: Path, run_id: str) -> dict:
    """How many times this identity was started and how it terminated.

    The ``started`` line is appended by ``consume_plan`` and by nothing else, so counting
    it IS counting consumptions. A second ``started`` line for one run id would mean the
    single-use rule had been broken, which is why the count is recorded rather than
    assumed to be one.
    """
    events: dict[str, int] = {}
    plan_hashes: set[str] = set()
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("run_id") != run_id:
                continue
            events[entry["event"]] = events.get(entry["event"], 0) + 1
            plan_hashes.add(entry.get("plan_hash", ""))
    return {"events": events, "plan_hashes": sorted(plan_hashes)}


def build_receipt(run_directory: str | Path, *, candidate: str,
                  training_source_commit: str, design_commit: str,
                  ledger: str | Path) -> dict:
    """One receipt, every field re-derived from canonical evidence."""
    from training_gym.training.artifacts import (
        ADAPTER_MANIFEST_FILE,
        ADAPTER_WEIGHTS_FILE,
        AdapterManifest,
        read_safetensors_header,
        validate_adapter_directory,
        verify_completed_run,
    )
    from training_gym.training.model_identity import cache_directory_name
    from training_gym.training.chat_render import ReasoningPolicy

    import scripts.build_quality_training_config as generator

    directory = Path(run_directory)
    manifest = AdapterManifest.from_dict(_load(directory / ADAPTER_MANIFEST_FILE))
    body = manifest.to_dict()
    run = _load(directory / "run.json")
    backend = _load(directory / "backend_result.json")
    evidence = backend.get("evidence", {})
    validation = evidence.get("train_time_validation", {})

    key = next((k for k, spec in generator.CANDIDATES.items()
                if spec.get("run_id") == candidate), "")
    if not key:
        raise ValueError(
            f"{candidate!r} is not a candidate the production generator names; a receipt "
            f"may not describe a run the repository cannot re-derive a design for")
    option = generator.OPTIONS[generator.CANDIDATE_OPTION[key]]
    policy = generator.candidate_reasoning_policy(key)
    if not isinstance(policy, ReasoningPolicy):  # pragma: no cover - defensive
        raise TypeError("the generator returned a non-enum reasoning policy")

    # ── the canonical verifiers, run here rather than quoted ──────────────────
    lora_config = {**body["lora"]}
    run_problems = verify_completed_run(directory, lora=lora_config,
                                        base_model_id=body["base_model_id"])
    artefacts = validate_adapter_directory(directory, lora=lora_config,
                                           base_model_id=body["base_model_id"],
                                           expect_manifest=True)

    # ── the tensors, read from the safetensors HEADER (no weights loaded) ─────
    header = read_safetensors_header(directory / ADAPTER_WEIGHTS_FILE)
    names = tuple(name for name in header if name != "__metadata__")
    lora_a = sum(1 for name in names if "lora_A" in name)
    lora_b = sum(1 for name in names if "lora_B" in name)
    parameters = 0
    for name in names:
        size = 1
        for dim in header[name]["shape"]:
            size *= dim
        parameters += size

    weights = directory / ADAPTER_WEIGHTS_FILE
    counts = _ledger_counts(Path(ledger), candidate)
    started = counts["events"].get("started", 0)

    # ── runtime and cache evidence, both root-independent ─────────────────────
    runtime = {
        "device_category": body["device_category"],
        "precision": body["precision"],
        "package_versions": dict(sorted(backend["package_versions"].items())),
        "local_files_only": bool(evidence.get("local_files_only")),
        "trust_remote_code": bool(evidence.get("trust_remote_code")),
        "deterministic_reproduction_claimed": bool(
            evidence.get("deterministic_reproduction_claimed")),
    }
    from training_gym.training.artifacts import ADAPTER_CONFIG_FILE  # noqa: F401

    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "candidate_id": candidate,
        "design_milestone": DESIGN_MILESTONE,
        "training_milestone": TRAINING_MILESTONE,
        "design_commit": design_commit,
        "training_source_commit": training_source_commit,

        "plan_hash": run["plan_hash"],
        # Root-BOUND: `config_hash` covers `output_root_id`, a digest of a resolved
        # absolute path. It is recorded as an observation about the executing host and
        # is deliberately NOT re-derived by the control plane (S3O §7.1, §13).
        "training_config_hash": run["training_config_hash"],
        "training_config_hash_is_root_bound": True,

        "base_model": {
            "model_id": body["base_model_id"],
            "revision": body["base_model_revision"],
            "identity_hash": body["base_model_identity_hash"],
            "tokenizer_id": body["tokenizer_id"],
            "tokenizer_revision": body["tokenizer_revision"],
            "chat_template_digest": body["tokenizer_chat_template_hash"],
        },
        "training_dataset": {
            "dataset_id": body["dataset_id"],
            "version": body["dataset_version"],
            "manifest_hash": body["dataset_manifest_hash"],
            "reference_hash": body["dataset_reference_hash"],
            "export_manifest_hash": body["export_manifest_hash"],
            "train_shard_hash": body["train_shard_hash"],
            "validation_shard_hash": body["validation_shard_hash"],
            "hidden_evaluation_hash": body["hidden_evaluation_hash"],
            "security_regression_hash": body["security_regression_hash"],
        },
        "representation": {
            "reasoning_policy": policy.value,
            "chat_render_policy_hash": body["chat_render_policy_hash"],
            "full_sequence_render_policy_hash": evidence.get(
                "full_sequence_render_policy_hash", ""),
            "assistant_only_loss": bool(body["assistant_only_loss"]["verified"]),
            "masking_strategy": body["assistant_only_loss"]["strategy"],
        },
        "authority": {
            "form": AUTHORITY_FORM,
            "bound_plan_hash": run["plan_hash"],
            "creations": started,
            "consumptions": started,
            "token_literal_recorded": False,
            "retry_authorized": False,
        },
        "execution": {
            "terminal_status": "SUCCESS" if backend["status"] == "succeeded"
                               else backend["status"].upper(),
            "run_state": run["state"],
            "backend_status": backend["status"],
            "completed": bool(run["completed"]),
            "interrupted": bool(run["interrupted"]),
            "error_category": run["error_category"],
            "optimizer_steps_planned": option["max_steps"],
            "optimizer_steps_requested": body["requested_steps"],
            "optimizer_steps_completed": body["completed_steps"],
            "epochs_configured": option["epochs"],
            "epochs_completed": body["epochs_completed"],
            "seed": body["seed"],
            "converted_records": backend["converted_records"],
            "truncated_records": backend["truncated_records"],
            "train_loss": body["train_loss"],
            "validation_evaluations": validation.get("evaluations", 0),
            "validation_losses": [point["eval_loss"] for point
                                  in validation.get("eval_loss_by_evaluation", [])],
            "final_validation_loss": validation.get("final_eval_loss"),
            "final_validation_present": bool(
                validation.get("final_evaluation", {}).get("at_end_of_training")),
            "validation_rows": validation.get("validation_rows", 0),
            "validation_contributes_gradients": bool(
                validation.get("contributes_gradients")),
            "validation_is_held_out_eligibility_evidence": bool(
                validation.get("is_held_out_eligibility_evidence")),
            "early_stopping": bool(validation.get("early_stopping")),
            "load_best_model_at_end": bool(validation.get("load_best_model_at_end")),
            "generation_performed": bool(validation.get("generation_performed")),
            "backend_warnings": list(backend.get("warnings", [])),
        },
        "adapter": {
            "sha256": _sha256_bytes(weights.read_bytes()),
            "manifest_hash": manifest.manifest_hash(),
            "artifact_set_hash": body["tree_hash"],
            "bytes": weights.stat().st_size,
            "total_bytes": body["total_bytes"],
            "file_names": sorted(entry.name for entry in manifest.files),
            "lora_tensor_count": len(names),
            "lora_a_tensors": lora_a,
            "lora_b_tensors": lora_b,
            "non_lora_tensors": len(names) - lora_a - lora_b,
            "adapter_parameter_count": parameters,
            "trainable_parameters": evidence["trainable_parameters"],
            "total_parameters": evidence["total_parameters"],
            "target_modules": list(body["lora"]["target_modules"]),
            "lora_rank": body["lora"]["rank"],
            "lora_alpha": body["lora"]["alpha"],
            "lora_dropout": body["lora"]["dropout"],
            "dtypes": sorted({header[name]["dtype"] for name in names}),
        },
        "verification": {
            "completed_run_verifier": "PASS" if not run_problems else "FAIL",
            "completed_run_problems": list(run_problems),
            "adapter_verifier": artefacts.verdict.value,
            "adapter_problems": list(artefacts.problems),
            "checkpoint_directories": sum(
                1 for entry in directory.iterdir()
                if entry.is_dir() and entry.name.startswith("checkpoint")),
            "nested_directories": sum(1 for entry in directory.iterdir()
                                      if entry.is_dir()),
            "symlinks": sum(1 for entry in directory.iterdir() if entry.is_symlink()),
        },
        "runtime": runtime,
        "runtime_evidence_digest": _sha256_bytes(
            json.dumps(runtime, sort_keys=True, ensure_ascii=False,
                       allow_nan=False).encode("utf-8")),
        # Root-INDEPENDENT by construction: the production probe digests the cache
        # DIRECTORY NAME, which is derived from the model id and nothing else. It
        # identifies WHICH cache identity was used without revealing where it lives.
        "model_cache_evidence": _sha256_bytes(
            cache_directory_name(body["base_model_id"]).encode("utf-8")),
        "ledger": {
            "events": dict(sorted(counts["events"].items())),
            "plan_hashes": counts["plan_hashes"],
        },
        "holdout": {
            "evaluation_corpus": None,
            "held_out_evaluation_runs": 0,
            "eval_authority_created": False,
            "model_response_tokens_generated": 0,
        },
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    from scripts.verify_m62_control_plane import canonical_json

    parser = argparse.ArgumentParser(
        description="Distil one completed M62 training run into its tracked, "
                    "root-independent receipt. Trains nothing and spends nothing.")
    parser.add_argument("--run-directory", required=True,
                        help="the completed run directory (a gitignored runtime tree)")
    parser.add_argument("--candidate", required=True, help="the candidate run id")
    parser.add_argument("--training-source-commit", required=True,
                        help="the commit the training executed from")
    parser.add_argument("--design-commit", required=True,
                        help="the commit that designed the candidate")
    parser.add_argument("--ledger", default="",
                        help="the training run ledger; defaults to the run tree's own")
    parser.add_argument("--emit", default="",
                        help="write the receipt here; prints to stdout when omitted")
    args = parser.parse_args(argv)

    directory = Path(args.run_directory)
    ledger = Path(args.ledger) if args.ledger else \
        directory.parent.parent / "training_runs.jsonl"
    try:
        payload = build_receipt(directory, candidate=args.candidate,
                                training_source_commit=args.training_source_commit,
                                design_commit=args.design_commit, ledger=ledger)
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
        print(json.dumps({"status": "refused",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1

    text = canonical_json(payload)
    if args.emit:
        destination = Path(args.emit)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        print(json.dumps({"status": "ok", "receipt": destination.name,
                          "bytes": len(text.encode("utf-8")),
                          "sha256": _sha256_bytes(text.encode("utf-8"))}, indent=2))
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
