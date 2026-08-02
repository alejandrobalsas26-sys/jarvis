"""training_gym/training/planner.py — V69 M62 S3A: the dry run, and only the dry run.

This is the composition point: config -> model identity -> dataset verification ->
dependency report -> hardware report -> device -> precision -> feasibility -> plan.

Nothing in this module creates a directory, opens a file for writing, appends to a
ledger, downloads a byte, imports a training framework, spawns a process or opens a
socket. A dry run that spent any part of the decision it was previewing would not be a
dry run — the same sentence :func:`training_gym.datasets.promotion_plan.plan_promotion`
is written under, for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import TrainingConfig
from .dataset_reference import (
    DatasetEvidence,
    DatasetReferenceVerification,
    verify_training_dataset_reference,
)
from .dependencies import DependencyReport, build_dependency_report
from .environment import (
    DeviceSelection,
    HardwareCapabilityReport,
    PrecisionSelection,
    select_device,
    select_precision,
)
from .feasibility import TrainingFeasibilityReport, Verdict, build_feasibility_report
from .model_identity import ModelIdentity, identity_from_config
from .plan import (
    EXPECTED_ADAPTER_FILES,
    TrainingPlan,
    check_output_root,
    download_authorization_note,
    download_required,
    output_root_id,
    plan_state_sequence,
)


@dataclass(frozen=True)
class PlanningResult:
    """The plan and every report it was derived from, so a reader can check the work."""

    plan: TrainingPlan
    identity: ModelIdentity
    dataset: DatasetReferenceVerification
    dependencies: DependencyReport
    hardware: HardwareCapabilityReport
    device: DeviceSelection
    precision: PrecisionSelection
    feasibility: TrainingFeasibilityReport
    download_note: str

    @property
    def ok(self) -> bool:
        return self.plan.is_executable

    def to_dict(self) -> dict:
        """The full dry-run document. Bounded, sanitized, and free of any path."""
        return {
            "plan": self.plan.to_record(),
            "model_identity": self.identity.to_dict(),
            "dataset_verification": self.dataset.to_dict(),
            "dependencies": self.dependencies.to_dict(),
            "hardware": self.hardware.to_dict(),
            "device_selection": self.device.to_dict(),
            "precision_selection": self.precision.to_dict(),
            "feasibility": self.feasibility.to_dict(),
            "model_download": self.download_note,
            "dry_run": True,
            "wrote_anything": False,
            "trained_anything": False,
        }


def plan_training(
    config: TrainingConfig, *, dataset_root: str | Path,
    output_root: str | Path, model_cache_root: str | Path | None = None,
    export_root: str | Path | None = None,
    accelerator_probe=None, allow_model_download_flag: bool = False,
    revoked_candidate_ids: frozenset[str] = frozenset(),
) -> PlanningResult:
    """Produce a deterministic dry-run plan. Writes nothing, downloads nothing.

    ``accelerator_probe`` is the only way a CUDA/Metal runtime is ever consulted, and it
    is off by default: the library that could answer the question is the library that
    could also start training.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []

    # 1. Model and tokenizer identity. A mutable revision is a blocker, not a warning.
    identity = identity_from_config(config, cache_root=model_cache_root)
    identity_blockers = identity.blockers
    missing.extend(identity_blockers)

    # 2. The dataset, verified by re-deriving every digest from the artifacts on disk.
    dataset = verify_training_dataset_reference(
        config.dataset_reference, root=dataset_root,
        needs_preference_data=config.method.needs_preference_data,
        training_split=config.training_split,
        validation_split=config.validation_split,
        hidden_evaluation_split=config.hidden_evaluation_reference,
        security_regression_split=config.security_regression_reference,
        revoked_candidate_ids=revoked_candidate_ids,
        out_root=export_root)
    if dataset.evidence is DatasetEvidence.REFUSED:
        blockers.extend(f"dataset: {p}" for p in dataset.problems)
    missing.extend(f"dataset: {m}" for m in dataset.missing)

    # 3. Dependencies, by metadata inspection only.
    dependencies = build_dependency_report(profile=config.dependency_profile,
                                           method=config.method)
    dependency_blockers = dependencies.blockers()
    missing.extend(f"dependency: {b}" for b in dependency_blockers)

    # 4. Hardware. The output root is the only path consulted, and only for free space.
    hardware = HardwareCapabilityReport.detect(output_root=output_root,
                                               accelerator_probe=accelerator_probe)

    # 5. Device and precision, from measured evidence only.
    device = select_device(config.device_policy, hardware)
    blockers.extend(f"device: {b}" for b in device.blockers)
    precision = select_precision(config.precision_policy, device=device.selected,
                                 report=hardware, method=config.method)
    blockers.extend(f"precision: {b}" for b in precision.blockers)

    # 6. The destination. Nothing is created; an existing run directory is a refusal.
    blockers.extend(f"output: {p}" for p in check_output_root(output_root, config.run_id))

    # 7. Feasibility, memory and disk.
    feasibility = build_feasibility_report(
        config, hardware=hardware, device=device.selected,
        precision=precision.selected,
        dependency_ready=dependencies.ready,
        dataset_ready=dataset.ok,
        model_identity_ready=identity.is_executable_reference,
        cache_status=identity.cache_status.value,
        blockers=tuple(blockers), warnings=tuple(warnings),
        missing_evidence=tuple(missing))

    needs_download = download_required(cache_status=identity.cache_status.value,
                                       config=config)
    note = download_authorization_note(config.model_download_policy,
                                       required=needs_download,
                                       flag_supplied=allow_model_download_flag)
    if needs_download and config.model_download_policy.value == "deny":
        missing.append(
            "model weights are not known to be cached and the download policy is deny")

    plan_blockers = tuple(feasibility.blockers) + tuple(
        m for m in feasibility.missing_evidence)
    plan = TrainingPlan(
        run_id=config.run_id, method=config.method.value,
        training_config_hash=config.config_hash(),
        base_model_identity_hash=identity.identity_hash(),
        tokenizer_identity_hash=identity.tokenizer_identity_hash(),
        base_model_revision_kind=identity.revision_kind.value,
        dataset_reference_hash=config.dataset_reference.reference_hash(),
        dataset_manifest_hash=config.dataset_reference.dataset_manifest_hash,
        training_split_hash=config.dataset_reference.train_split_manifest_hash,
        validation_split_hash=config.dataset_reference.validation_split_manifest_hash,
        hidden_evaluation_hash=(
            config.dataset_reference.hidden_evaluation_manifest_hash),
        security_regression_hash=(
            config.dataset_reference.security_regression_manifest_hash),
        dataset_verification_hash=dataset.verification_hash(),
        dependency_report_hash=dependencies.report_hash(),
        hardware_report_hash=hardware.report_hash(),
        feasibility_report_hash=feasibility.report_hash(),
        feasibility_verdict=feasibility.verdict.value,
        selected_device=device.selected, selected_precision=precision.selected,
        hyperparameters={
            "seed": config.seed, "epochs": config.epochs,
            "max_steps": config.max_steps, "batch_size": config.batch_size,
            "gradient_accumulation_steps": config.gradient_accumulation_steps,
            "effective_batch_size": config.effective_batch_size,
            "learning_rate": config.learning_rate,
            "weight_decay": config.weight_decay,
            "warmup_ratio": config.warmup_ratio,
            "max_sequence_length": config.max_sequence_length,
            "gradient_checkpointing": config.gradient_checkpointing,
            "dataloader_workers": config.dataloader_workers,
            "checkpoint_strategy": config.checkpoint_strategy.value,
            "checkpoint_interval_steps": config.checkpoint_interval_steps,
            "max_checkpoints": config.max_checkpoints,
            "logging_target": config.logging_target.value,
            "logging_interval_steps": config.logging_interval_steps,
        },
        lora=config.lora.to_dict(),
        resource_policy_version=config.resource_policy.policy_version,
        resource_policy_hash=config.resource_policy.policy_hash(),
        model_download_required=needs_download,
        model_download_policy=config.model_download_policy.value,
        expected_output_root_id=output_root_id(output_root),
        expected_files=EXPECTED_ADAPTER_FILES,
        expected_state_transitions=plan_state_sequence(
            awaiting_confirmation=not plan_blockers),
        expected_package_profile=config.dependency_profile.value,
        created_at_utc=config.created_at_utc,
        warnings=tuple(feasibility.warnings),
        blockers=plan_blockers)

    return PlanningResult(plan=plan, identity=identity, dataset=dataset,
                          dependencies=dependencies, hardware=hardware, device=device,
                          precision=precision, feasibility=feasibility,
                          download_note=note)


__all__ = ["PlanningResult", "Verdict", "plan_training"]
