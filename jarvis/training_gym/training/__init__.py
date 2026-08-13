"""training_gym.training — V69 M62 S3A: the training planner, and only the planner.

WHAT THIS PACKAGE IS
--------------------
The contract and the dry run for a LoRA fine-tune: a strict configuration schema, a
pinned model identity, a verified dataset reference, a passive dependency report, a
passive hardware report, deterministic feasibility estimates, and an immutable
:class:`~training_gym.training.plan.TrainingPlan` with a digest that authorises exactly
one future execution and no other.

WHAT IT IS NOT
--------------
It does not train. There is no trainer here, no optimizer, no adapter, no checkpoint.
Execution ships in S3B and is refused until it does — not by a flag that defaults to off,
but by the absence of any code that could perform it.

Importing this package performs no I/O and contacts no network. It imports none of
``torch``, ``transformers``, ``datasets``, ``accelerate``, ``peft``, ``trl``,
``bitsandbytes``, ``huggingface_hub``, ``tokenizers`` or ``safetensors`` — dependency
availability is inspected through :mod:`importlib.metadata`, which answers the question
without loading the answer.
"""
from __future__ import annotations

from .config import (
    ALLOWED_TRAINING_TRANSITIONS,
    MAX_CONFIG_BYTES,
    PLANNER_VERSION,
    REQUIRED_REVISION_PLACEHOLDER,
    RESOURCE_POLICY_VERSION,
    S3A_REACHABLE_STATES,
    TRAINING_SCHEMA_VERSION,
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
    TrainingConfigError,
    TrainingMethod,
    TrainingResourcePolicy,
    TrainingRunState,
    TrainingTransitionError,
    ValidationStrategy,
    check_training_transition,
    is_immutable_revision,
    legacy_method,
    load_config_document,
    load_training_config,
    refuse_path_like,
    require_model_id,
    require_revision,
)
from .environment import (
    HARDWARE_REPORT_VERSION,
    AcceleratorProbe,
    Availability,
    DeviceSelection,
    HardwareCapabilityReport,
    PrecisionSelection,
    SizeCategory,
    categorize_gb,
    no_accelerator_probe,
    select_device,
    select_precision,
)
from .feasibility import (
    DISK_FORMULA_VERSION,
    FEASIBILITY_VERSION,
    MEMORY_FORMULA_VERSION,
    DiskEstimate,
    MemoryEstimate,
    RuntimeCategory,
    TrainingFeasibilityReport,
    Verdict,
    build_feasibility_report,
    classify_runtime,
    estimate_disk,
    estimate_memory,
)
from .plan import (
    CONFIRMATION_PREFIX,
    EXPECTED_ADAPTER_FILES,
    PLAN_SCHEMA_VERSION,
    TRAINING_LEDGER_FILE,
    ConfirmationRejected,
    ExecutionNotImplemented,
    TrainingPlan,
    TrainingPlanError,
    check_output_root,
    check_training_confirmation,
    download_authorization_note,
    download_required,
    output_root_id,
    plan_state_sequence,
    refuse_execution,
    training_run_directory,
)
from .planner import PlanningResult, plan_training
from .dependencies import (
    DEPENDENCY_REPORT_VERSION,
    MINIMUM_VERSIONS,
    PROFILE_PACKAGES,
    DependencyReport,
    DependencyState,
    PackageAvailability,
    build_dependency_report,
    probe_package,
)
from .dataset_reference import (
    DATASET_REFERENCE_VERSION,
    PLACEHOLDER_PREFIX,
    DatasetEvidence,
    DatasetReferenceError,
    DatasetReferenceVerification,
    ExportFormat,
    TrainingDatasetReference,
    is_placeholder,
    require_digest_or_placeholder,
    split_digests,
    verify_training_dataset_reference,
)
from .model_identity import (
    MODEL_IDENTITY_VERSION,
    CacheStatus,
    ModelIdentity,
    ModelIdentityError,
    RevisionKind,
    cache_directory_name,
    classify_revision,
    identity_from_config,
    probe_cache,
)

__all__ = [
    "CONFIRMATION_PREFIX", "DISK_FORMULA_VERSION", "EXPECTED_ADAPTER_FILES",
    "FEASIBILITY_VERSION", "HARDWARE_REPORT_VERSION", "MEMORY_FORMULA_VERSION",
    "PLAN_SCHEMA_VERSION", "TRAINING_LEDGER_FILE", "AcceleratorProbe",
    "Availability", "ConfirmationRejected", "DeviceSelection", "DiskEstimate",
    "ExecutionNotImplemented", "HardwareCapabilityReport", "MemoryEstimate",
    "PlanningResult", "PrecisionSelection", "RuntimeCategory", "SizeCategory",
    "TrainingFeasibilityReport", "TrainingPlan", "TrainingPlanError", "Verdict",
    "build_feasibility_report", "categorize_gb", "check_output_root",
    "check_training_confirmation", "classify_runtime", "download_authorization_note",
    "download_required", "estimate_disk", "estimate_memory", "no_accelerator_probe",
    "output_root_id", "plan_state_sequence", "plan_training", "refuse_execution",
    "select_device", "select_precision", "training_run_directory",
    "DEPENDENCY_REPORT_VERSION", "MINIMUM_VERSIONS", "PROFILE_PACKAGES",
    "DependencyReport", "DependencyState", "PackageAvailability",
    "build_dependency_report", "probe_package",
    "ALLOWED_TRAINING_TRANSITIONS", "DATASET_REFERENCE_VERSION", "MAX_CONFIG_BYTES",
    "MODEL_IDENTITY_VERSION", "PLACEHOLDER_PREFIX", "PLANNER_VERSION",
    "REQUIRED_REVISION_PLACEHOLDER", "RESOURCE_POLICY_VERSION",
    "S3A_REACHABLE_STATES", "TRAINING_SCHEMA_VERSION", "CacheStatus",
    "CheckpointStrategy", "ValidationStrategy", "DatasetEvidence",
    "DatasetReferenceError",
    "DatasetReferenceVerification", "DependencyProfile", "DevicePolicy",
    "ExportFormat", "LoRABias", "LoRAConfig", "LoRATargetPolicy", "LoggingTarget",
    "ModelDownloadPolicy", "ModelIdentity", "ModelIdentityError", "PrecisionPolicy",
    "RevisionKind", "TrainingConfig", "TrainingConfigError",
    "TrainingDatasetReference", "TrainingMethod", "TrainingResourcePolicy",
    "TrainingRunState", "TrainingTransitionError", "cache_directory_name",
    "check_training_transition", "classify_revision", "identity_from_config",
    "is_immutable_revision", "is_placeholder", "legacy_method",
    "load_config_document", "load_training_config", "probe_cache",
    "refuse_path_like", "require_digest_or_placeholder", "require_model_id",
    "require_revision", "split_digests", "verify_training_dataset_reference",
]
