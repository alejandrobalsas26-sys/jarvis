"""training_gym/training/feasibility.py — V69 M62 S3A: an estimate is not a measurement.

WHAT THIS PRODUCES
------------------
A deterministic, component-by-component memory estimate, a disk estimate, a runtime
*category*, and a verdict. Every number carries the formula version and the assumptions
it was computed under, because a number without those is a claim the reader cannot check
and the author cannot reproduce.

WHY THERE IS STILL ONLY ONE ESTIMATOR
-------------------------------------
:func:`core.training_pipeline.estimate_memory_gb` already exists and M17 keeps reporting
it. Rather than let a second, silently-disagreeing number appear, this module computes
its own breakdown *and* cross-checks it against the M17 figure, recording both and
warning when they diverge by more than a factor of two. The S3A breakdown governs S3A
planning; the M17 scalar remains what the M17 DryRunPlan reports. Which one is authority
for what is written down rather than left to whoever reads the output first.

WHAT AN ESTIMATE IS NOT
-----------------------
It is not a measurement, and a comfortable estimate is not a PASS. Every verdict applies
a safety margin, and ``INSUFFICIENT_EVIDENCE`` is a first-class outcome: a host whose RAM
could not be read has not been shown to be adequate, and saying so is the only honest
answer available.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..schemas import sha256_obj
from .config import PrecisionPolicy, TrainingConfig, TrainingMethod
from .environment import Availability, HardwareCapabilityReport

#: Bumped whenever any constant or term below changes. Recorded in every estimate.
MEMORY_FORMULA_VERSION = "m62.memory_estimate.1"
DISK_FORMULA_VERSION = "m62.disk_estimate.1"
FEASIBILITY_VERSION = "m62.feasibility.1"

#: Bytes per parameter for the *frozen base weights*, by precision.
_WEIGHT_BYTES: dict[str, float] = {
    PrecisionPolicy.FP32.value: 4.0,
    PrecisionPolicy.FP16.value: 2.0,
    PrecisionPolicy.BF16.value: 2.0,
    PrecisionPolicy.INT8_TRAINING.value: 1.0,
    PrecisionPolicy.INT4_QLORA.value: 0.5,
}

#: Adam keeps two fp32 moments per *trainable* parameter, plus the gradient itself.
_OPTIMIZER_BYTES_PER_TRAINABLE = 8.0
_GRADIENT_BYTES_PER_TRAINABLE = 4.0

#: Quantized bases carry dequantization scratch and quantization statistics.
_QUANTIZATION_OVERHEAD_FRACTION = 0.20

#: A dataloader holding a bounded number of tokenized batches in host memory.
_DATALOADER_BASE_GB = 0.25

#: Estimates are wrong in both directions; the margin is applied before any verdict.
_SAFETY_MARGIN_FRACTION = 0.25

#: A gigabyte of activations per (batch x 1024 tokens), before gradient checkpointing.
_ACTIVATION_GB_PER_BATCH_KTOKEN = 0.5

#: Gradient checkpointing trades compute for memory; this is the retained fraction.
_CHECKPOINTING_ACTIVATION_FRACTION = 0.3


class Verdict(str, Enum):
    FEASIBLE = "feasible"
    FEASIBLE_WITH_WARNINGS = "feasible_with_warnings"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class RuntimeCategory(str, Enum):
    """Broad buckets. This milestone does not claim to predict a duration."""

    SHORT_SMOKE = "short_smoke"
    SLOW_LOCAL_SMOKE = "slow_local_smoke"
    IMPRACTICAL_LOCAL = "impractical_local"
    REQUIRES_ACCELERATOR = "requires_accelerator"
    UNKNOWN = "unknown"


def _lora_trainable_parameters(config: TrainingConfig) -> float:
    """Trainable parameter count in billions, as a bounded fraction of the base.

    A deliberately coarse proportional model: rank-r adapters over a transformer's
    linear layers land in the low single-digit percent of the base for the ranks this
    milestone permits. Stated as an assumption on every estimate rather than presented
    as a derivation from a specific architecture, which this module cannot see.
    """
    breadth = {"all_linear": 0.010, "attention_and_mlp": 0.008,
               "attention_only": 0.004}[config.lora.target_policy.value]
    return round(config.base_model_parameters_b * breadth
                 * (config.lora.rank / 8.0), 6)


@dataclass(frozen=True)
class MemoryEstimate:
    """Peak host/accelerator memory, broken into the terms that produce it."""

    base_weights_gb: float
    gradients_gb: float
    optimizer_state_gb: float
    activations_gb: float
    adapter_parameters_gb: float
    quantization_overhead_gb: float
    dataloader_gb: float
    safety_margin_gb: float
    estimated_peak_gb: float
    precision: str
    method: str
    parameters_b: float
    trainable_parameters_b: float
    pipeline_cross_check_gb: float = 0.0
    assumptions: tuple[str, ...] = ()
    formula_version: str = MEMORY_FORMULA_VERSION

    def to_dict(self) -> dict:
        return {
            "base_weights_gb": self.base_weights_gb,
            "gradients_gb": self.gradients_gb,
            "optimizer_state_gb": self.optimizer_state_gb,
            "activations_gb": self.activations_gb,
            "adapter_parameters_gb": self.adapter_parameters_gb,
            "quantization_overhead_gb": self.quantization_overhead_gb,
            "dataloader_gb": self.dataloader_gb,
            "safety_margin_gb": self.safety_margin_gb,
            "estimated_peak_gb": self.estimated_peak_gb,
            "precision": self.precision, "method": self.method,
            "parameters_b": self.parameters_b,
            "trainable_parameters_b": self.trainable_parameters_b,
            "pipeline_cross_check_gb": self.pipeline_cross_check_gb,
            "assumptions": list(self.assumptions),
            "formula_version": self.formula_version,
            "is_an_estimate_not_a_measurement": True,
        }

    def estimate_hash(self) -> str:
        return sha256_obj(self.to_dict())


def _pipeline_cross_check(config: TrainingConfig) -> float:
    """The M17 scalar for the same run, so the two authorities can be compared.

    Imported lazily and defensively: this module must remain usable in a layout where
    ``core`` is not importable, and an unavailable cross-check is reported as zero and
    said so in the assumptions, never silently treated as agreement.
    """
    try:
        from core.training_pipeline import (
            BaseModelReference,
            DatasetReference,
            TrainingConfig as M17Config,
            TrainingMethod as M17Method,
            estimate_memory_gb,
        )

        legacy = M17Config(
            run_id=config.run_id,
            base_model=BaseModelReference(
                model_id=config.base_model_id,
                params_b=config.base_model_parameters_b,
                context_length=max(config.max_sequence_length, 4096)),
            dataset=DatasetReference(version=config.dataset_reference.dataset_version,
                                     path="training/datasets"),
            training_method=M17Method({"sft_lora": "lora", "sft_qlora": "qlora",
                                       "dpo_lora": "dpo"}[config.method.value]),
            batch_size=config.batch_size,
            max_sequence_length=config.max_sequence_length)
        return float(estimate_memory_gb(legacy))
    except Exception:  # noqa: BLE001 — an unavailable cross-check is not agreement
        return 0.0


def estimate_memory(config: TrainingConfig, *, precision: str) -> MemoryEstimate:
    """Deterministic component breakdown. No clock, no host read, no I/O."""
    weight_bytes = _WEIGHT_BYTES.get(precision, _WEIGHT_BYTES[PrecisionPolicy.FP32.value])
    params = config.base_model_parameters_b
    trainable = _lora_trainable_parameters(config)

    base_weights = params * weight_bytes
    # Only the adapters carry gradients and optimizer state — that is what LoRA is.
    gradients = trainable * _GRADIENT_BYTES_PER_TRAINABLE
    optimizer = trainable * _OPTIMIZER_BYTES_PER_TRAINABLE
    adapters = trainable * 4.0
    quantization = (base_weights * _QUANTIZATION_OVERHEAD_FRACTION
                    if precision == PrecisionPolicy.INT4_QLORA.value else 0.0)

    activations = (config.batch_size * config.max_sequence_length / 1024.0
                   ) * _ACTIVATION_GB_PER_BATCH_KTOKEN
    if config.gradient_checkpointing:
        activations *= _CHECKPOINTING_ACTIVATION_FRACTION
    dataloader = _DATALOADER_BASE_GB * (1 + config.dataloader_workers)
    if config.method is TrainingMethod.DPO_LORA:
        # A reference model is held alongside the policy for the KL term.
        base_weights *= 2.0

    subtotal = (base_weights + gradients + optimizer + activations + adapters
                + quantization + dataloader)
    margin = subtotal * _SAFETY_MARGIN_FRACTION

    assumptions = [
        f"lora adapters modelled as {trainable}B trainable parameters "
        f"(policy={config.lora.target_policy.value}, rank={config.lora.rank})",
        "base weights are frozen: only the adapters carry gradients and optimizer state",
        f"adam moments at {_OPTIMIZER_BYTES_PER_TRAINABLE} bytes per trainable parameter",
        f"a {int(_SAFETY_MARGIN_FRACTION * 100)}% safety margin is included in the peak",
        "activations scale linearly with batch x sequence length, which is a "
        "simplification: attention is quadratic in sequence length",
    ]
    if config.gradient_checkpointing:
        assumptions.append(
            f"gradient checkpointing retains ~{_CHECKPOINTING_ACTIVATION_FRACTION:.0%} "
            f"of activations and costs additional compute")
    if config.method is TrainingMethod.DPO_LORA:
        assumptions.append("dpo holds a reference model alongside the policy model")

    cross_check = _pipeline_cross_check(config)
    if cross_check <= 0:
        assumptions.append(
            "the M17 cross-check estimate was unavailable in this layout and is "
            "reported as 0, which is not agreement")

    return MemoryEstimate(
        base_weights_gb=round(base_weights, 3), gradients_gb=round(gradients, 3),
        optimizer_state_gb=round(optimizer, 3), activations_gb=round(activations, 3),
        adapter_parameters_gb=round(adapters, 3),
        quantization_overhead_gb=round(quantization, 3),
        dataloader_gb=round(dataloader, 3), safety_margin_gb=round(margin, 3),
        estimated_peak_gb=round(subtotal + margin, 3), precision=precision,
        method=config.method.value, parameters_b=params,
        trainable_parameters_b=trainable,
        pipeline_cross_check_gb=round(cross_check, 3),
        assumptions=tuple(assumptions))


@dataclass(frozen=True)
class DiskEstimate:
    """Bytes a completed run would leave behind, plus what it needs to get there."""

    model_cache_gb: float
    tokenizer_cache_gb: float
    dataset_gb: float
    adapter_gb: float
    checkpoints_gb: float
    optimizer_state_gb: float
    logs_gb: float
    temporary_gb: float
    safety_margin_gb: float
    estimated_total_gb: float
    required_free_gb: float
    formula_version: str = DISK_FORMULA_VERSION

    def to_dict(self) -> dict:
        return {
            "model_cache_gb": self.model_cache_gb,
            "tokenizer_cache_gb": self.tokenizer_cache_gb,
            "dataset_gb": self.dataset_gb, "adapter_gb": self.adapter_gb,
            "checkpoints_gb": self.checkpoints_gb,
            "optimizer_state_gb": self.optimizer_state_gb,
            "logs_gb": self.logs_gb, "temporary_gb": self.temporary_gb,
            "safety_margin_gb": self.safety_margin_gb,
            "estimated_total_gb": self.estimated_total_gb,
            "required_free_gb": self.required_free_gb,
            "formula_version": self.formula_version,
            "is_an_estimate_not_a_measurement": True,
        }

    def estimate_hash(self) -> str:
        return sha256_obj(self.to_dict())


def estimate_disk(config: TrainingConfig, *, weights_cached: bool) -> DiskEstimate:
    """Deterministic disk estimate. Creates nothing and probes no drive."""
    params = config.base_model_parameters_b
    # Hub snapshots are shipped in bf16/fp16 regardless of the training precision.
    model_cache = 0.0 if weights_cached else params * 2.0
    tokenizer_cache = 0.02
    dataset = min(config.dataset_reference.record_count, 100_000) * 8e-6
    trainable = _lora_trainable_parameters(config)
    adapter = max(trainable * 4.0, 0.005)
    checkpoints = adapter * config.max_checkpoints
    optimizer = trainable * _OPTIMIZER_BYTES_PER_TRAINABLE * (
        1 if config.max_checkpoints else 0)
    logs = 0.05
    temporary = max(model_cache * 0.1, 0.1)
    subtotal = (model_cache + tokenizer_cache + dataset + adapter + checkpoints
                + optimizer + logs + temporary)
    margin = subtotal * _SAFETY_MARGIN_FRACTION
    total = subtotal + margin
    return DiskEstimate(
        model_cache_gb=round(model_cache, 3), tokenizer_cache_gb=round(tokenizer_cache, 3),
        dataset_gb=round(dataset, 4), adapter_gb=round(adapter, 4),
        checkpoints_gb=round(checkpoints, 4), optimizer_state_gb=round(optimizer, 4),
        logs_gb=round(logs, 3), temporary_gb=round(temporary, 3),
        safety_margin_gb=round(margin, 3), estimated_total_gb=round(total, 3),
        required_free_gb=round(total, 3))


def classify_runtime(config: TrainingConfig, *, device: str,
                     record_count: int) -> RuntimeCategory:
    """A category, never a duration. Nothing here has been benchmarked on this host."""
    if not device:
        return RuntimeCategory.UNKNOWN
    if device == "cuda":
        return RuntimeCategory.SHORT_SMOKE
    if device != "cpu":
        return RuntimeCategory.UNKNOWN
    # CPU. Even a very small model is slow; a large one is not worth starting.
    if config.base_model_parameters_b >= 3.0:
        return RuntimeCategory.REQUIRES_ACCELERATOR
    if config.base_model_parameters_b >= 1.0:
        return RuntimeCategory.IMPRACTICAL_LOCAL
    if record_count > 5_000 or config.max_steps > 2_000:
        return RuntimeCategory.IMPRACTICAL_LOCAL
    return RuntimeCategory.SLOW_LOCAL_SMOKE


@dataclass(frozen=True)
class TrainingFeasibilityReport:
    """Every readiness dimension, the verdict, and why it is that verdict."""

    verdict: Verdict
    dependency_ready: bool
    dataset_ready: bool
    model_identity_ready: bool
    tokenizer_ready: bool
    cache_ready: str
    hardware_ready: bool
    precision_ready: bool
    selected_device: str
    selected_precision: str
    method: str
    runtime_category: RuntimeCategory
    memory: MemoryEstimate
    disk: DiskEstimate
    usable_memory_gb: float
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    feasibility_version: str = FEASIBILITY_VERSION

    @property
    def executable(self) -> bool:
        """Whether S3B *could* run this — never whether S3A did."""
        return self.verdict in (Verdict.FEASIBLE, Verdict.FEASIBLE_WITH_WARNINGS)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "dependency_ready": self.dependency_ready,
            "dataset_ready": self.dataset_ready,
            "model_identity_ready": self.model_identity_ready,
            "tokenizer_ready": self.tokenizer_ready,
            "cache_ready": self.cache_ready,
            "hardware_ready": self.hardware_ready,
            "precision_ready": self.precision_ready,
            "selected_device": self.selected_device,
            "selected_precision": self.selected_precision,
            "method": self.method,
            "runtime_category": self.runtime_category.value,
            "memory": self.memory.to_dict(),
            "disk": self.disk.to_dict(),
            "usable_memory_gb": self.usable_memory_gb,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "missing_evidence": list(self.missing_evidence),
            "executable": self.executable,
            "feasibility_version": self.feasibility_version,
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())


def assess_memory(estimate: MemoryEstimate, report: HardwareCapabilityReport
                  ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(blockers, warnings)`` from comparing an estimate against a measured budget."""
    usable = report.usable_memory_gb
    if usable <= 0:
        return ((), (f"the usable memory budget could not be measured, so the "
                     f"{estimate.estimated_peak_gb} GB estimate has nothing to be "
                     f"compared against",))
    if estimate.estimated_peak_gb > usable:
        return ((f"the estimated peak of {estimate.estimated_peak_gb} GB exceeds the "
                 f"{usable} GB usable budget on this host",), ())
    if estimate.estimated_peak_gb > usable * 0.7:
        return ((), (f"the estimated peak of {estimate.estimated_peak_gb} GB is within "
                     f"30% of the {usable} GB budget; an estimate this close to the "
                     f"ceiling is not a guarantee of fitting",))
    if (estimate.pipeline_cross_check_gb > 0
            and (estimate.estimated_peak_gb > estimate.pipeline_cross_check_gb * 2
                 or estimate.estimated_peak_gb * 2 < estimate.pipeline_cross_check_gb)):
        return ((), (f"this estimate ({estimate.estimated_peak_gb} GB) and the M17 "
                     f"estimator ({estimate.pipeline_cross_check_gb} GB) disagree by "
                     f"more than a factor of two; treat both as rough",))
    return ((), ())


def assess_disk(estimate: DiskEstimate, report: HardwareCapabilityReport,
                *, minimum_free_gb: float) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(blockers, warnings)`` for free space at the configured output root."""
    free = report.output_disk_free_gb
    if free <= 0:
        return ((), ("free space at the output root could not be measured, so the disk "
                     "estimate has nothing to be compared against",))
    if free < estimate.required_free_gb:
        return ((f"the run needs about {estimate.required_free_gb} GB and the output "
                 f"root has {free} GB free",), ())
    if free < minimum_free_gb:
        return ((), (f"the output root has {free} GB free, below the {minimum_free_gb} "
                     f"GB the resource policy asks for as headroom",))
    return ((), ())


def build_feasibility_report(
    config: TrainingConfig, *, hardware: HardwareCapabilityReport,
    device: str, precision: str, dependency_ready: bool, dataset_ready: bool,
    model_identity_ready: bool, cache_status: str,
    blockers: tuple[str, ...] = (), warnings: tuple[str, ...] = (),
    missing_evidence: tuple[str, ...] = (),
) -> TrainingFeasibilityReport:
    """Combine every readiness dimension into one verdict. Deterministic; no I/O."""
    weights_cached = cache_status == "present"
    memory = estimate_memory(config, precision=precision or PrecisionPolicy.FP32.value)
    disk = estimate_disk(config, weights_cached=weights_cached)

    all_blockers = list(blockers)
    all_warnings = list(warnings)
    all_missing = list(missing_evidence)

    memory_blockers, memory_warnings = assess_memory(memory, hardware)
    all_blockers += memory_blockers
    all_warnings += memory_warnings
    disk_blockers, disk_warnings = assess_disk(
        disk, hardware, minimum_free_gb=config.resource_policy.min_free_disk_bytes / 1e9)
    all_blockers += disk_blockers
    all_warnings += disk_warnings

    runtime = classify_runtime(config, device=device,
                               record_count=config.dataset_reference.record_count)
    if runtime is RuntimeCategory.REQUIRES_ACCELERATOR:
        all_blockers.append(
            f"a {config.base_model_parameters_b}B model on the {device} is not a "
            f"practical local run; this requires an accelerator")
    elif runtime is RuntimeCategory.IMPRACTICAL_LOCAL:
        all_warnings.append(
            f"a {config.base_model_parameters_b}B model on the {device} is slow enough "
            f"that this should be treated as impractical rather than merely patient")
    elif runtime is RuntimeCategory.SLOW_LOCAL_SMOKE:
        all_warnings.append(
            "a CPU smoke run is slow; this validates the pipeline, and it is not a "
            "route to a production adapter")
    if hardware.usable_memory_gb <= 0:
        all_missing.append("the host memory budget could not be measured")

    if all_blockers:
        verdict = Verdict.BLOCKED
    elif not precision or not device:
        verdict = Verdict.UNSUPPORTED
    elif all_missing or not dataset_ready or not model_identity_ready:
        verdict = Verdict.INSUFFICIENT_EVIDENCE
    elif all_warnings or not dependency_ready:
        verdict = Verdict.FEASIBLE_WITH_WARNINGS
    else:
        verdict = Verdict.FEASIBLE

    return TrainingFeasibilityReport(
        verdict=verdict, dependency_ready=dependency_ready,
        dataset_ready=dataset_ready, model_identity_ready=model_identity_ready,
        tokenizer_ready=model_identity_ready, cache_ready=cache_status,
        hardware_ready=bool(device) and hardware.usable_memory_gb > 0,
        precision_ready=bool(precision), selected_device=device,
        selected_precision=precision, method=config.method.value,
        runtime_category=runtime, memory=memory, disk=disk,
        usable_memory_gb=hardware.usable_memory_gb,
        blockers=tuple(all_blockers), warnings=tuple(all_warnings),
        missing_evidence=tuple(all_missing))


__all__ = [
    "DISK_FORMULA_VERSION", "FEASIBILITY_VERSION", "MEMORY_FORMULA_VERSION",
    "Availability", "DiskEstimate", "MemoryEstimate", "RuntimeCategory",
    "TrainingFeasibilityReport", "Verdict", "assess_disk", "assess_memory",
    "build_feasibility_report", "classify_runtime", "estimate_disk", "estimate_memory",
]
