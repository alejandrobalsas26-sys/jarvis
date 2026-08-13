"""scripts/build_quality_training_config.py — V69 M62 S3G: the candidate's configuration.

WHAT THIS IS
------------
The tracked generator for the first QUALITY-oriented training candidate's configuration
and its dry-run plan. The configuration document itself is a runtime artefact and stays
untracked, like every plan, token and adapter in this milestone; what is tracked is the
code that produces it, so a future session reproduces the configuration instead of
finding a JSON file nobody can re-derive.

It offers three complete options and recommends one. The options differ only in adapter
capacity, optimisation and how many passes are taken over the corpus — the model, the
revision, the corpus, the precision, the device and the artefact policy are identical
across all three, because those are the parts that are already qualified and changing
them would put a second variable into the first quality measurement.

WHAT IT NEVER DOES
------------------
  * It does not train. ``plan_training`` is a pure dry run: it creates no directory,
    opens no file for writing, imports no training framework and contacts no network.
  * It does not consume authority. A ``TRAIN:<plan-hash>`` token is DERIVED from a plan,
    not issued by one; nothing is spent by computing a plan, and this script never
    passes ``--execute`` to anything.
  * It does not print the token. The plan hash is printed and the token is derivable
    from it by whoever holds the plan; printing it here would put a single-use
    authorisation into console scrollback for no benefit.
  * It writes no absolute path into its output.

THE CANDIDATE IS NOT RUN-004
----------------------------
Operator ruling **H5** placed ``qwen3-06b-lora-smoke-live-004`` permanently outside
quality promotion. This configuration names a NEW run id, a NEW corpus and a NEW output
directory. Nothing here resumes, continues, mutates or reads run-004.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

NOW = "2026-08-12T00:00:00Z"

#: The new candidate's identity. Never ``smoke``, never ``active``, never ``promoted``.
RUN_ID = "qwen3-06b-lora-quality-live-001"
EXPERIMENT_NAME = "m62-s3g-defensive-quality-001"
RUN_INTENT = "QUALITY_CANDIDATE"

#: Pinned by §5 of PROGRESS.md and by the S3D.1 smoke record. An immutable commit sha,
#: never a branch or tag: a moving reference makes every downstream digest a claim about
#: whatever the reference pointed at that day.
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
BASE_MODEL_PARAMETERS_B = 0.6

TRAINING_DATASET_ID = "m62-defensive-quality-train"
TRAINING_DATASET_VERSION = "v1"


# ══════════════════════════════════════════════════════════════════════════════
#  The three options
# ══════════════════════════════════════════════════════════════════════════════
#  Shared across all three, and deliberately not a variable:
#
#    method                SFT_LORA         the only method with a live-proven backend
#    precision             fp32             what run-004 actually trained at; explicit
#                                           rather than auto_safe so the plan records an
#                                           input, not a host-dependent outcome
#    device                cpu              this host has no accelerator, and auto_safe
#                                           would make the plan hash depend on a probe
#    batch_size            1                run-004's shape; with a per-batch collator,
#                                           batch 1 means no padding and no wasted compute
#    max_sequence_length   512              measured p95 sequence is ~180 estimated
#                                           tokens, so 512 truncates nothing; it is a cap,
#                                           not a pad width
#    checkpoint_strategy   no               D16: epoch/steps write pickle-shaped trainer
#                                           state the adapter artefact policy refuses
#    gradient_checkpointing false           it trades ~25% more compute for memory this
#                                           host does not need for a 0.6B fp32 adapter run
#    logging               local_jsonl      the only non-phone-home target in the schema
#    download policy       deny             offline by invariant
#    validation_strategy   epoch            S3G.2: the promoted VALIDATION split (9 rows)
#                                           is handed to the trainer as an eval arm and
#                                           measured once per epoch. Three evaluations
#                                           over three epochs -- enough to see a
#                                           train/eval divergence, few enough that the
#                                           cost is negligible. It steers NOTHING
#                                           automatically: no early stopping, no
#                                           load_best_model_at_end, no checkpoint
#
#  ``optimizer`` and ``lr_scheduler`` are NOT fields of this schema. The backend passes
#  ``TrainingArguments`` without overriding either, so the installed transformers
#  defaults apply: ``adamw_torch`` and a linear decay schedule with the configured
#  warmup ratio. That is recorded here as an OBSERVATION about the backend, not as a
#  setting this file controls.
OPTIONS: dict[str, dict] = {
    "A": {
        "label": "conservative",
        "rationale":
            "smallest change that could show a signal: half the adapter capacity of B "
            "and two passes. Cheapest to run and the least likely to overfit 107 rows; "
            "also the most likely to move nothing measurable.",
        "lora_rank": 8, "lora_alpha": 16, "lora_dropout": 0.05,
        "learning_rate": 1e-4, "weight_decay": 0.0, "warmup_ratio": 0.1,
        "epochs": 2, "max_steps": 27, "gradient_accumulation_steps": 8,
        "overfitting_risk": "low",
        "signal_expectation": "weak; may not move any gate",
    },
    "B": {
        "label": "recommended",
        "rationale":
            "enough adapter capacity to change formatting and refusal phrasing, enough "
            "passes to converge on 107 rows, and a runtime that fits inside one local "
            "session. Rank 16 over the seven Qwen3 linear projections is ~10M trainable "
            "parameters against 601M frozen — large enough to learn a response style, "
            "far too small to learn new knowledge, which is exactly the intended scope.",
        "lora_rank": 16, "lora_alpha": 32, "lora_dropout": 0.05,
        "learning_rate": 2e-4, "weight_decay": 0.0, "warmup_ratio": 0.1,
        "epochs": 3, "max_steps": 40, "gradient_accumulation_steps": 8,
        "overfitting_risk": "moderate and monitored by the validation split",
        "signal_expectation":
            "the format and refusal-phrasing gates are reachable; the security gates "
            "are veto conditions, not targets",
    },
    "C": {
        "label": "aggressive",
        "rationale":
            "double the rank again and six passes. On a 107-row corpus this is the "
            "option most likely to memorise the corpus rather than learn from it, which "
            "would show up as a good validation loss and a worse held-out result — the "
            "failure mode that is hardest to detect and easiest to believe.",
        "lora_rank": 32, "lora_alpha": 64, "lora_dropout": 0.1,
        "learning_rate": 2e-4, "weight_decay": 0.01, "warmup_ratio": 0.1,
        "epochs": 6, "max_steps": 80, "gradient_accumulation_steps": 8,
        "overfitting_risk": "high on a corpus this size",
        "signal_expectation":
            "largest movement, least trustworthy attribution",
    },
}

RECOMMENDED_OPTION = "B"

#: Measured on this host in S3G, from the promoted corpus:
#: 107 TRAIN rows, mean sequence ~470 characters (~105-135 estimated tokens plus the
#: chat template's ~30). Cost model: a LoRA forward+backward is ~4 x parameters x tokens
#: floating-point operations, and this CPU sustains an estimated 40-100 GFLOP/s in fp32.
#: Calibrated against the ONE datapoint that exists: run-004's 32 micro-batches of ~51
#: tokens completed in the "minutes" category, which the same model predicts as 40-100s.
#: These are RANGES derived from a model, not measurements of a run that happened.
RUNTIME_SECONDS_PER_MICROBATCH = (3.5, 9.0)

#: What an operator should refuse to let a first candidate exceed. Not enforced here —
#: nothing here runs — and deliberately well above the estimate, so it catches a wrong
#: cost model rather than ordinary variance.
HARD_RUNTIME_CEILING_HOURS = 4


def compute_budget(option: str, train_rows: int) -> dict:
    """Micro-batch count, realised epochs and an estimated runtime RANGE."""
    spec = OPTIONS[option]
    micro_batches = spec["max_steps"] * spec["gradient_accumulation_steps"]
    low, high = RUNTIME_SECONDS_PER_MICROBATCH
    return {
        "optimizer_steps": spec["max_steps"],
        "effective_batch_size": spec["gradient_accumulation_steps"] * 1,
        "micro_batches": micro_batches,
        "realised_epochs": round(micro_batches / max(1, train_rows), 2),
        "estimated_runtime_minutes": [round(micro_batches * low / 60),
                                      round(micro_batches * high / 60)],
        "estimate_basis":
            "~4 x params x tokens FLOPs per micro-batch at 40-100 GFLOP/s fp32 CPU; "
            "calibrated against run-004's recorded duration category, not measured",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  The dataset reference, re-derived from the promoted artefacts
# ══════════════════════════════════════════════════════════════════════════════
def dataset_reference(root: Path):
    """Fill every digest from the artefacts on disk. Nothing here is hand-copied.

    ``load_manifest`` is the only loader used: it re-hashes every shard from disk,
    refuses symlinks and hard links, and re-derives each record's digest. A reference
    assembled from a weaker reading of the same files would bind a claim rather than a
    fact.
    """
    from training_gym.datasets.candidate import DatasetSplit
    from training_gym.datasets.export import verify_sft_export
    from training_gym.datasets.manifests import load_manifest
    from training_gym.training.dataset_reference import (
        ExportFormat,
        TrainingDatasetReference,
        split_digests,
    )

    manifest = load_manifest(root=root, dataset_id=TRAINING_DATASET_ID,
                            dataset_version=TRAINING_DATASET_VERSION)
    export = verify_sft_export(out_root=root, dataset_id=TRAINING_DATASET_ID,
                               dataset_version=TRAINING_DATASET_VERSION)
    if not export.ok or export.manifest is None:
        raise RuntimeError(f"the SFT export does not verify: {export.to_dict()}")

    train = split_digests(manifest, DatasetSplit.TRAIN)
    validation = split_digests(manifest, DatasetSplit.VALIDATION)
    hidden = split_digests(manifest, DatasetSplit.HIDDEN_EVALUATION)
    security = split_digests(manifest, DatasetSplit.SECURITY_REGRESSION)
    for name, value in (("train", train), ("validation", validation),
                        ("hidden_evaluation", hidden),
                        ("security_regression", security)):
        if value is None:
            raise RuntimeError(f"the promoted version declares no {name} shard")

    return TrainingDatasetReference(
        dataset_id=TRAINING_DATASET_ID, dataset_version=TRAINING_DATASET_VERSION,
        dataset_manifest_hash=manifest.manifest_hash(),
        export_format=ExportFormat.SFT_JSONL,
        export_manifest_hash=export.manifest.export_hash(),
        source_dataset_content_hash=export.manifest.row_hashes_hash,
        train_split_manifest_hash=train[0], train_shard_hash=train[1],
        validation_split_manifest_hash=validation[0],
        validation_shard_hash=validation[1],
        hidden_evaluation_manifest_hash=hidden[0],
        security_regression_manifest_hash=security[0],
        record_count=manifest.total_records,
        dataset_schema_version=manifest.dataset_manifest_version)


def validation_export(root: Path):
    """The verified VALIDATION export's manifest, or a refusal.

    Checked here because the configuration this script emits sets
    ``validation_strategy=epoch``, and a config that asks for train-time validation
    against an export that does not verify is a config whose plan would be refused at
    preflight. Better to fail while producing the document than to hand over one that
    cannot be executed.
    """
    from training_gym.datasets.export import verify_sft_validation_export

    verified = verify_sft_validation_export(
        out_root=root, dataset_id=TRAINING_DATASET_ID,
        dataset_version=TRAINING_DATASET_VERSION)
    if not verified.ok or verified.manifest is None:
        raise RuntimeError(
            f"the validation export does not verify: {verified.to_dict()}. Rebuild the "
            f"corpus with build_training_corpus.py --root <the same dataset root>; the "
            f"build is deterministic and must reproduce the promoted manifest hash")
    return verified.manifest


def build_config(option: str, *, dataset_root: Path, output_root: Path):
    """One complete, strictly-validated :class:`TrainingConfig`."""
    from training_gym.training.config import (
        CheckpointStrategy,
        DependencyProfile,
        DevicePolicy,
        LoggingTarget,
        LoRABias,
        LoRAConfig,
        LoRATargetPolicy,
        ModelDownloadPolicy,
        PrecisionPolicy,
        TrainingConfig,
        TrainingMethod,
        ValidationStrategy,
    )
    from training_gym.training.plan import output_root_id

    spec = OPTIONS[option]
    return TrainingConfig(
        run_id=RUN_ID, experiment_name=EXPERIMENT_NAME,
        method=TrainingMethod.SFT_LORA,
        base_model_id=BASE_MODEL_ID, base_model_revision=BASE_MODEL_REVISION,
        base_model_parameters_b=BASE_MODEL_PARAMETERS_B,
        tokenizer_id=BASE_MODEL_ID, tokenizer_revision=BASE_MODEL_REVISION,
        dataset_reference=dataset_reference(dataset_root),
        output_root_id=output_root_id(output_root),
        created_at_utc=NOW,
        seed=42,
        epochs=spec["epochs"], max_steps=spec["max_steps"],
        batch_size=1,
        gradient_accumulation_steps=spec["gradient_accumulation_steps"],
        learning_rate=spec["learning_rate"], weight_decay=spec["weight_decay"],
        warmup_ratio=spec["warmup_ratio"],
        max_sequence_length=512,
        # Off deliberately: recomputing activations costs roughly a quarter more compute
        # to save memory that a 0.6B fp32 adapter run on this host does not need.
        gradient_checkpointing=False,
        precision_policy=PrecisionPolicy.FP32,
        device_policy=DevicePolicy.CPU,
        lora=LoRAConfig(
            rank=spec["lora_rank"], alpha=spec["lora_alpha"],
            dropout=spec["lora_dropout"], bias=LoRABias.NONE,
            # Explicit, not the ``all-linear`` sentinel. run-004 established what the
            # sentinel resolves to on this model — the seven projections below, 392 LoRA
            # tensors over 28 layers — so naming them makes the adapted module set an
            # INPUT to the plan rather than an outcome discovered after training, and
            # D15's exact-match rule then has something real to check.
            target_policy=LoRATargetPolicy.ATTENTION_AND_MLP,
            task_type="CAUSAL_LM"),
        dataloader_workers=0,
        checkpoint_strategy=CheckpointStrategy.NO,
        max_checkpoints=1,
        # S3G.2. Once per epoch, over the 9 promoted VALIDATION rows, for visibility into
        # eval_loss and train/eval divergence. `epoch` rather than `steps` because 40
        # optimizer steps over 9 rows would re-measure the same rows 40 times and produce
        # a curve made of sampling noise. It is DIAGNOSTIC: the held-out eligibility
        # corpus is still `m62-defensive-eval v2`, post-training and separately
        # authorised, and no gate in the S3G acceptance set reads this number.
        validation_strategy=ValidationStrategy.EPOCH,
        logging_target=LoggingTarget.LOCAL_JSONL, logging_interval_steps=5,
        model_download_policy=ModelDownloadPolicy.DENY,
        dependency_profile=DependencyProfile.TRAINING,
        trust_remote_code=False,
        base_model_family="qwen3",
        notes=(f"M62 S3G first quality candidate, option {option} "
               f"({spec['label']}). RUN_INTENT={RUN_INTENT}. Not run-004; nothing is "
               f"resumed, continued or promoted."))


def plan_for(option: str, *, dataset_root: Path, output_root: Path,
             model_cache_root: Path | None):
    """The dry run. Creates nothing, imports no training framework, spends nothing."""
    from training_gym.training.planner import plan_training

    config = build_config(option, dataset_root=dataset_root, output_root=output_root)
    return config, plan_training(config, dataset_root=dataset_root,
                                 output_root=output_root,
                                 model_cache_root=model_cache_root,
                                 export_root=dataset_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the M62 S3G quality candidate's training configuration and "
                    "its dry-run plan. Never trains and never spends a token.")
    parser.add_argument("--dataset-root", required=True,
                        help="root holding the promoted training corpus and its export")
    parser.add_argument("--output-root", required=True,
                        help="where a FUTURE run would write; nothing is created here")
    parser.add_argument("--model-cache-root", default="",
                        help="the reviewed model cache; omitted means the plan reports "
                             "the weights as unverified rather than guessing a location")
    parser.add_argument("--option", default=RECOMMENDED_OPTION, choices=sorted(OPTIONS),
                        help=f"default {RECOMMENDED_OPTION}, the recommended option")
    parser.add_argument("--all-options", action="store_true",
                        help="summarise every option's hyperparameters and budget")
    parser.add_argument("--emit", default="",
                        help="write the configuration document to this path; it is a "
                             "runtime artefact and must stay outside the repository")
    parser.add_argument("--plan", action="store_true",
                        help="produce the dry-run plan and report its blockers")
    args = parser.parse_args(argv)

    try:
        dataset_root = Path(args.dataset_root)
        output_root = Path(args.output_root)
        cache_root = Path(args.model_cache_root) if args.model_cache_root else None

        if args.all_options:
            reference = dataset_reference(dataset_root)
            train_rows = 0
            from training_gym.datasets.candidate import DatasetSplit
            from training_gym.datasets.manifests import load_manifest
            manifest = load_manifest(root=dataset_root, dataset_id=TRAINING_DATASET_ID,
                                     dataset_version=TRAINING_DATASET_VERSION)
            train_rows = manifest.counts().get(DatasetSplit.TRAIN.value, 0)
            payload = {
                "recommended_option": RECOMMENDED_OPTION,
                "train_rows": train_rows,
                "hard_runtime_ceiling_hours": HARD_RUNTIME_CEILING_HOURS,
                "dataset_reference_hash": reference.reference_hash(),
                "options": {
                    name: {**{k: v for k, v in spec.items()},
                           "config_hash": build_config(
                               name, dataset_root=dataset_root,
                               output_root=output_root).config_hash(),
                           "budget": compute_budget(name, train_rows)}
                    for name, spec in sorted(OPTIONS.items())},
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        config = build_config(args.option, dataset_root=dataset_root,
                              output_root=output_root)
        payload: dict = {
            "option": args.option, "run_id": RUN_ID, "run_intent": RUN_INTENT,
            "config_hash": config.config_hash(),
            "dataset_reference_hash": config.dataset_reference.reference_hash(),
            "effective_batch_size": config.effective_batch_size,
            "validation_strategy": config.validation_strategy.value,
            "validation_split": config.validation_split.value,
            "train_time_validation_enabled": config.train_time_validation_enabled,
            "early_stopping": False,
            "load_best_model_at_end": False,
            "checkpoint_strategy": config.checkpoint_strategy.value,
            "trained_anything": False, "wrote_adapter": False,
        }
        if config.train_time_validation_enabled:
            manifest = validation_export(dataset_root)
            payload.update({
                "validation_export_rows": manifest.record_count,
                "validation_export_hash": manifest.export_hash(),
                "validation_export_file_sha256": manifest.sha256_file,
                "validation_export_source_split": manifest.source_split,
            })

        if args.emit:
            destination = Path(args.emit)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(config.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8")
            payload["config_written"] = destination.name

        if args.plan:
            _config, result = plan_for(args.option, dataset_root=dataset_root,
                                       output_root=output_root,
                                       model_cache_root=cache_root)
            plan = result.plan
            payload.update({
                "plan_hash": plan.plan_hash(),
                "plan_is_executable": plan.is_executable,
                "plan_blockers": list(plan.blockers),
                "plan_warnings": list(plan.warnings),
                "feasibility_verdict": result.feasibility.verdict.value,
                "selected_device": plan.selected_device,
                "selected_precision": plan.selected_precision,
                "dataset_evidence": result.dataset.evidence.value,
                "dataset_problems": list(result.dataset.problems),
                "dataset_missing": list(result.dataset.missing),
                "dependency_ready": result.dependencies.ready,
                "dependency_blockers": list(result.dependencies.blockers()),
                "model_cache_status": result.identity.cache_status.value,
                "memory_peak_estimate_gb": result.feasibility.memory.estimated_peak_gb,
                "adapter_estimate_gb": result.feasibility.disk.adapter_gb,
                "disk_estimate_gb": result.feasibility.disk.estimated_total_gb,
                "disk_required_free_gb": result.feasibility.disk.required_free_gb,
                # The token is DERIVED from the plan hash above by whoever holds the
                # plan. It is not printed: a single-use authorisation does not belong in
                # console scrollback, and nothing here consumes one.
                "train_token_created": False,
                "train_token_consumed": False,
                "training_executed": False,
            })
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
        print(json.dumps({"status": "refused",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps({"status": "ok", **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
