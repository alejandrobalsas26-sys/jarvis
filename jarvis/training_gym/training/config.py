"""training_gym/training/config.py — V69 M62 S3A: the training configuration authority.

WHAT THIS IS
------------
The closed, typed description of a training experiment: which method, which model at
which immutable revision, which verified dataset version, which hyperparameters, which
ceilings. It is the *only* thing the planner reads, and it is deliberately incapable of
expressing the things that turn a config file into a remote-code-execution primitive.

WHAT A CONFIG CANNOT SAY
------------------------
There is no field for an executable name, a Python module path, a shell command, an
environment variable, an extra CLI argument, an absolute output path, a Hugging Face
token, a cache directory or a telemetry endpoint. Those are not validated-and-rejected;
they are *unrepresentable*, which is a stronger property than a denylist. Unknown keys
fail closed, so a misspelt ``trust_remote_code`` cannot load with the safe default
silently applied.

``trust_remote_code`` exists as a field for one reason: so a config that asks for it is
refused loudly instead of being silently reinterpreted. It may only ever be ``false``.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not import torch, transformers, peft, trl, datasets, accelerate or
huggingface_hub. It does not read a model cache, resolve a revision, touch the network,
create a directory or write a file. Importing it performs no I/O.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from ..datasets.candidate import DatasetSplit
from ..schemas import (
    RESERVED_DEVICE_NAMES,
    SchemaError,
    assert_no_private_content,
    reject_unknown_fields,
    require_bool,
    require_enum,
    require_float,
    require_id,
    require_int,
    require_mapping,
    require_text,
    sha256_obj,
)
from ..task_spec import require_timestamp
from .dataset_reference import (
    PLACEHOLDER_PREFIX,
    TrainingDatasetReference,
    is_placeholder,
)

#: The config schema this build speaks. The MAJOR component gates compatibility.
TRAINING_SCHEMA_VERSION = "m62.training_config.1"

#: Bumped whenever the planner's semantics change. Part of every plan hash.
PLANNER_VERSION = "m62.s3a.1"

#: Bumped whenever a ceiling changes. Part of every plan hash, so a plan approved under
#: one set of limits cannot be executed under a laxer one.
RESOURCE_POLICY_VERSION = "m62.training_resource_policy.1"


class TrainingConfigError(SchemaError):
    """A training configuration that will not be used as written."""


# ══════════════════════════════════════════════════════════════════════════════
#  Closed vocabularies
# ══════════════════════════════════════════════════════════════════════════════
class TrainingMethod(str, Enum):
    """The three methods S3A will plan. Full fine-tuning is deliberately absent.

    Named distinctly from :class:`core.training_pipeline.TrainingMethod` (``lora``,
    ``qlora``, ``dpo``) because the M17 enum also carries ``sft`` — a *full* supervised
    fine-tune — and an adapter-only milestone must not inherit a member that means
    "update every weight". :func:`legacy_method` maps in one direction only.
    """

    SFT_LORA = "sft_lora"
    SFT_QLORA = "sft_qlora"
    DPO_LORA = "dpo_lora"

    @property
    def needs_preference_data(self) -> bool:
        return self is TrainingMethod.DPO_LORA

    @property
    def needs_quantization(self) -> bool:
        return self is TrainingMethod.SFT_QLORA

    @property
    def required_packages(self) -> tuple[str, ...]:
        return {
            TrainingMethod.SFT_LORA: ("torch", "transformers", "datasets",
                                      "accelerate", "peft", "trl", "safetensors"),
            TrainingMethod.SFT_QLORA: ("torch", "transformers", "datasets",
                                       "accelerate", "peft", "trl", "safetensors",
                                       "bitsandbytes"),
            TrainingMethod.DPO_LORA: ("torch", "transformers", "datasets",
                                      "accelerate", "peft", "trl", "safetensors"),
        }[self]


def legacy_method(method: TrainingMethod) -> str:
    """The ``core.training_pipeline.TrainingMethod`` value this method corresponds to.

    One-way on purpose: the M17 vocabulary is broader than S3A's, so there is no inverse
    that could turn an M17 ``sft`` into an S3A method by accident.
    """
    return {TrainingMethod.SFT_LORA: "lora",
            TrainingMethod.SFT_QLORA: "qlora",
            TrainingMethod.DPO_LORA: "dpo"}[method]


class DevicePolicy(str, Enum):
    """Where training would run. ``AUTO_SAFE`` selects only from measured evidence."""

    AUTO_SAFE = "auto_safe"
    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"
    DIRECTML = "directml"


class PrecisionPolicy(str, Enum):
    """Numeric format. ``AUTO_SAFE`` derives from measured hardware, never from hope."""

    AUTO_SAFE = "auto_safe"
    FP32 = "fp32"
    FP16 = "fp16"
    BF16 = "bf16"
    INT8_TRAINING = "int8_training"
    INT4_QLORA = "int4_qlora"


class ModelDownloadPolicy(str, Enum):
    """Whether a future execution stage would be permitted to fetch weights.

    There is no ``AUTO``. A policy that decides for itself whether to reach the network
    is a policy that reaches the network.
    """

    DENY = "deny"
    ALLOW_WITH_EXPLICIT_FLAG = "allow_with_explicit_flag"


class DependencyProfile(str, Enum):
    """The optional install profile a run declares. Mirrors ``requirements/<name>.txt``."""

    TRAINING = "training"
    TRAINING_CUDA = "training-cuda"


class CheckpointStrategy(str, Enum):
    """Whether the trainer writes intermediate checkpoints. Only ``NO`` is supported.

    ``EPOCH`` and ``STEPS`` remain named so the refusal can say what was asked for
    rather than "not a valid strategy". They are refused because of what the trainer
    writes when they are on: a nested ``checkpoint-<step>/`` directory holding
    ``optimizer.pt``, ``scheduler.pt``, ``rng_state.pth`` and ``training_args.bin`` --
    pickle-shaped runtime state that :mod:`training_gym.training.artifacts` correctly
    refuses, both by suffix and by absence from the adapter allowlist. Accepting the
    value would therefore buy a run that trains to completion and is then rejected at
    artifact verification. The refusal belongs here, before anything is spent.
    """

    NO = "no"
    EPOCH = "epoch"
    STEPS = "steps"


#: The only checkpoint strategy the adapter artifact contract can accept.
SUPPORTED_CHECKPOINT_STRATEGIES: frozenset[CheckpointStrategy] = frozenset(
    {CheckpointStrategy.NO})


class ValidationStrategy(str, Enum):
    """Whether the trainer measures the VALIDATION split DURING the run, and how often.

    This is a *diagnostic* arm, not an eligibility measurement. VALIDATION is train-side
    steering material by this repository's ``TRAIN_SIDE_SPLITS`` definition: its loss says
    whether the run is fitting or memorising, and it never decides whether a candidate may
    be promoted. Held-out eligibility evidence is ``m62-defensive-eval``, it is
    ``evaluation_only``, and it is never read by a training run.

    ``NO`` is the default and is exactly the historical behaviour: no ``eval_dataset``
    reaches ``Trainer``, and the config's canonical form is byte-identical to what it was
    before this field existed — so no configuration written before it silently changes
    meaning, and no historical config hash moves.

    ``EPOCH`` evaluates once at the end of each epoch. ``STEPS`` is named so the refusal
    can say what was asked for rather than "not a valid strategy", and it is refused: the
    corpus this exists for holds nine validation rows against a forty-step run, so
    per-step evaluation would re-measure the same nine rows forty times, add its cost to
    every step, and produce a curve whose movement is sampling noise rather than
    learning. A cadence that expensive needs its own evidence, not a default.
    """

    NO = "no"
    EPOCH = "epoch"
    STEPS = "steps"


#: The cadences a run may actually use. See :class:`ValidationStrategy` on why not STEPS.
SUPPORTED_VALIDATION_STRATEGIES: frozenset[ValidationStrategy] = frozenset(
    {ValidationStrategy.NO, ValidationStrategy.EPOCH})


class LoggingTarget(str, Enum):
    """Where run logs go. Both members are local.

    ``wandb``/``mlflow``/``comet`` are not "unsupported yet" — they are absent, because a
    training config that can name a reporting backend can name an exfiltration endpoint,
    and the JSONL a human can read is the one that cannot phone home.
    """

    NONE = "none"
    LOCAL_JSONL = "local_jsonl"


class LoRATargetPolicy(str, Enum):
    """A closed set of module-selection policies — never a caller-supplied regex.

    ``ALL_LINEAR`` is PEFT's ``"all-linear"`` shorthand. It is recorded here as a *tested
    starting point* for smoke runs, not as a claim that it is optimal for any model.
    """

    ALL_LINEAR = "all_linear"
    ATTENTION_ONLY = "attention_only"
    ATTENTION_AND_MLP = "attention_and_mlp"

    @property
    def modules(self) -> tuple[str, ...]:
        return {
            LoRATargetPolicy.ALL_LINEAR: ("all-linear",),
            LoRATargetPolicy.ATTENTION_ONLY: ("q_proj", "k_proj", "v_proj", "o_proj"),
            LoRATargetPolicy.ATTENTION_AND_MLP: (
                "q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj"),
        }[self]


class LoRABias(str, Enum):
    NONE = "none"
    ALL = "all"
    LORA_ONLY = "lora_only"


class TrainingRunState(str, Enum):
    """The full run lifecycle, including the states only S3B may ever reach."""

    CREATED = "created"
    CONFIG_VALIDATED = "config_validated"
    DATASET_VERIFIED = "dataset_verified"
    DEPENDENCIES_VERIFIED = "dependencies_verified"
    HARDWARE_VERIFIED = "hardware_verified"
    PLANNED = "planned"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PREFLIGHT_VERIFIED = "preflight_verified"
    STARTING = "starting"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    ARTIFACT_VALIDATION = "artifact_validation"
    COMPLETED = "completed"
    EVALUATED = "evaluated"
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


#: The only states a planning-only stage may occupy. ``RUNNING`` and everything after it
#: assert that work happened; S3A did none, so it may not claim them.
S3A_REACHABLE_STATES: frozenset[TrainingRunState] = frozenset({
    TrainingRunState.CREATED,
    TrainingRunState.CONFIG_VALIDATED,
    TrainingRunState.DATASET_VERIFIED,
    TrainingRunState.DEPENDENCIES_VERIFIED,
    TrainingRunState.HARDWARE_VERIFIED,
    TrainingRunState.PLANNED,
    TrainingRunState.AWAITING_CONFIRMATION,
})

ALLOWED_TRAINING_TRANSITIONS: dict[TrainingRunState, frozenset[TrainingRunState]] = {
    TrainingRunState.CREATED: frozenset({
        TrainingRunState.CONFIG_VALIDATED, TrainingRunState.FAILED,
        TrainingRunState.REJECTED}),
    TrainingRunState.CONFIG_VALIDATED: frozenset({
        TrainingRunState.DATASET_VERIFIED, TrainingRunState.FAILED,
        TrainingRunState.REJECTED}),
    TrainingRunState.DATASET_VERIFIED: frozenset({
        TrainingRunState.DEPENDENCIES_VERIFIED, TrainingRunState.FAILED,
        TrainingRunState.REJECTED}),
    TrainingRunState.DEPENDENCIES_VERIFIED: frozenset({
        TrainingRunState.HARDWARE_VERIFIED, TrainingRunState.FAILED,
        TrainingRunState.REJECTED}),
    TrainingRunState.HARDWARE_VERIFIED: frozenset({
        TrainingRunState.PLANNED, TrainingRunState.FAILED,
        TrainingRunState.REJECTED}),
    TrainingRunState.PLANNED: frozenset({
        TrainingRunState.AWAITING_CONFIRMATION, TrainingRunState.REJECTED,
        TrainingRunState.FAILED}),
    # A confirmed plan is not a started run. Everything the plan asserted about the
    # world is re-derived in PREFLIGHT_VERIFIED, because the config, the dataset bytes,
    # the model revision, the installed packages and the measured hardware are all free
    # to have changed between the operator reading the plan and typing the token.
    TrainingRunState.AWAITING_CONFIRMATION: frozenset({
        TrainingRunState.PREFLIGHT_VERIFIED, TrainingRunState.REJECTED,
        TrainingRunState.FAILED}),
    # STARTING is where the plan is spent and the frameworks are imported. It is a
    # separate state because those are the first irreversible acts, and a run that dies
    # between them must not be indistinguishable from one that never began.
    TrainingRunState.PREFLIGHT_VERIFIED: frozenset({
        TrainingRunState.STARTING, TrainingRunState.REJECTED,
        TrainingRunState.FAILED}),
    TrainingRunState.STARTING: frozenset({
        TrainingRunState.RUNNING, TrainingRunState.INTERRUPTED,
        TrainingRunState.FAILED}),
    # RUNNING has no edge to COMPLETED. A backend reporting success is a claim; the
    # adapter on disk is the evidence, and ARTIFACT_VALIDATION is where the claim is
    # checked against it. Removing this edge is what makes "the trainer said it worked"
    # insufficient to produce a completed run.
    TrainingRunState.RUNNING: frozenset({
        TrainingRunState.ARTIFACT_VALIDATION, TrainingRunState.INTERRUPTING,
        TrainingRunState.FAILED}),
    TrainingRunState.INTERRUPTING: frozenset({
        TrainingRunState.INTERRUPTED, TrainingRunState.FAILED}),
    TrainingRunState.ARTIFACT_VALIDATION: frozenset({
        TrainingRunState.COMPLETED, TrainingRunState.FAILED,
        TrainingRunState.QUARANTINED}),
    TrainingRunState.INTERRUPTED: frozenset({
        TrainingRunState.FAILED, TrainingRunState.QUARANTINED}),
    TrainingRunState.COMPLETED: frozenset({
        TrainingRunState.EVALUATED, TrainingRunState.QUARANTINED}),
    TrainingRunState.EVALUATED: frozenset({
        TrainingRunState.CANDIDATE, TrainingRunState.REJECTED,
        TrainingRunState.QUARANTINED}),
    TrainingRunState.CANDIDATE: frozenset({
        TrainingRunState.REJECTED, TrainingRunState.QUARANTINED}),
    TrainingRunState.FAILED: frozenset({TrainingRunState.QUARANTINED}),
    TrainingRunState.REJECTED: frozenset({TrainingRunState.QUARANTINED}),
    TrainingRunState.QUARANTINED: frozenset(),
}


class TrainingTransitionError(TrainingConfigError):
    """A run state change that the lifecycle does not permit."""


def check_training_transition(current: TrainingRunState,
                              target: TrainingRunState) -> None:
    """Fail closed on any edge the state machine does not declare."""
    allowed = ALLOWED_TRAINING_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise TrainingTransitionError(
            f"training run: {current.value} -> {target.value} is not a permitted "
            f"transition (allowed: {sorted(s.value for s in allowed) or 'none'})")


# ══════════════════════════════════════════════════════════════════════════════
#  Identifier and path safety
# ══════════════════════════════════════════════════════════════════════════════
#: ``org/name`` exactly — two components, no third, no separator inside a component.
_MODEL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: An immutable Hugging Face revision is a full commit sha. A tag is not one.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: The placeholder a template config carries where an immutable revision must go.
REQUIRED_REVISION_PLACEHOLDER = "REQUIRED_IMMUTABLE_REVISION"


def require_model_id(value: object, field: str) -> str:
    """A ``org/name`` model identifier that cannot become a path or a device.

    Rejects backslashes, ``..``, absolute paths, drive letters, UNC prefixes and Windows
    reserved device names in either component, because this string is compared, logged
    and — in a later stage — handed to a library that will join it onto a cache root.
    """
    if not isinstance(value, str):
        raise TrainingConfigError(
            f"{field}: expected a string model id, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise TrainingConfigError(f"{field}: required")
    if not _MODEL_ID_RE.match(text):
        raise TrainingConfigError(
            f"{field}: invalid model id {text!r}; expected exactly 'org/name' "
            f"({_MODEL_ID_RE.pattern})")
    for part in text.split("/"):
        if part.endswith("."):
            raise TrainingConfigError(
                f"{field}: component {part!r} ends with a dot (Windows strips it, "
                f"aliasing a different name)")
        if part.split(".", 1)[0].lower() in RESERVED_DEVICE_NAMES:
            raise TrainingConfigError(
                f"{field}: component {part!r} is a Windows reserved device name")
    return text


def require_revision(value: object, field: str) -> str:
    """A revision string. Emptiness is refused; mutability is *reported*, not refused.

    A missing revision is a malformed config. A revision that is a branch name is a
    well-formed config that is not executable, and the difference matters: the template
    the operator starts from is the second kind, and it must load so the planner can say
    exactly what is missing.
    """
    if not isinstance(value, str):
        raise TrainingConfigError(
            f"{field}: expected a string revision, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise TrainingConfigError(
            f"{field}: required; a model reference with no revision names whatever the "
            f"upstream repository happens to hold today")
    if len(text) > 128:
        raise TrainingConfigError(f"{field}: too long ({len(text)} > 128)")
    if any(c in text for c in "/\\ \t\n\r") or ".." in text:
        raise TrainingConfigError(
            f"{field}: {text!r} contains a path separator, whitespace or '..'")
    return text


def is_immutable_revision(revision: str) -> bool:
    """True only for a full 40-character LOWERCASE commit sha. A tag pins nothing.

    Not lower-cased before matching: an uppercase string is a different string, it will
    not compare equal to the revision any tool reports, and accepting it here would make
    a plan hash depend on how the operator happened to type it.
    """
    return bool(_COMMIT_SHA_RE.match(str(revision or "").strip()))


def refuse_path_like(value: str, field: str) -> str:
    """Refuse anything that is a path rather than an identifier.

    Covers POSIX absolute paths, Windows drive paths, UNC shares, extended-length
    ``\\\\?\\`` prefixes, traversal, ``~`` expansion and embedded NULs. Applied to every
    operator-supplied string that a later stage might join onto a root.
    """
    text = str(value)
    if "\x00" in text:
        raise TrainingConfigError(f"{field}: contains a NUL byte")
    if text.startswith(("\\\\", "//")):
        raise TrainingConfigError(f"{field}: {text!r} is a UNC path")
    if re.match(r"^[A-Za-z]:", text):
        raise TrainingConfigError(f"{field}: {text!r} is a Windows drive path")
    if text.startswith(("/", "\\", "~")):
        raise TrainingConfigError(f"{field}: {text!r} is an absolute or home path")
    if ".." in text:
        raise TrainingConfigError(f"{field}: {text!r} contains a traversal segment")
    if "/" in text or "\\" in text:
        raise TrainingConfigError(f"{field}: {text!r} contains a path separator")
    return text


# ══════════════════════════════════════════════════════════════════════════════
#  Resource ceilings
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TrainingResourcePolicy:
    """Every bound a request is checked against. No member means "unlimited".

    Zero is never a sentinel for "no limit": a ceiling of zero forbids everything, which
    is a refusal, and a refusal is a safe failure. The version string is part of the plan
    hash so a plan approved under these limits cannot be executed under looser ones.
    """

    max_model_parameters_b: float = 1.5
    max_dataset_records: int = 100_000
    max_sequence_length: int = 4096
    max_epochs: int = 10
    max_steps: int = 100_000
    max_batch_size: int = 16
    max_gradient_accumulation: int = 128
    max_dataloader_workers: int = 8
    max_learning_rate: float = 1e-2
    max_output_bytes_estimate: int = 8 * 1024 * 1024 * 1024
    min_free_disk_bytes: int = 16 * 1024 * 1024 * 1024
    max_wall_clock_seconds: int = 24 * 3600
    max_checkpoint_count: int = 5
    max_retained_log_files: int = 32
    max_generated_files: int = 4096
    policy_version: str = RESOURCE_POLICY_VERSION

    _NUMERIC_FIELDS = (
        "max_model_parameters_b", "max_dataset_records", "max_sequence_length",
        "max_epochs", "max_steps", "max_batch_size", "max_gradient_accumulation",
        "max_dataloader_workers", "max_learning_rate", "max_output_bytes_estimate",
        "min_free_disk_bytes", "max_wall_clock_seconds", "max_checkpoint_count",
        "max_retained_log_files", "max_generated_files",
    )

    def __post_init__(self) -> None:
        for name in self._NUMERIC_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TrainingConfigError(
                    f"resource_policy.{name}: expected a number, got "
                    f"{type(value).__name__}")
            if value != value or value in (float("inf"), float("-inf")):
                raise TrainingConfigError(f"resource_policy.{name}: not finite")
            if value <= 0:
                raise TrainingConfigError(
                    f"resource_policy.{name}: {value} — a ceiling of zero or less is not "
                    f"'unlimited', and this policy has no way to express unlimited")
        object.__setattr__(self, "policy_version",
                           require_id(self.policy_version,
                                      "resource_policy.policy_version"))

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in self._NUMERIC_FIELDS} | {
            "policy_version": self.policy_version}

    def policy_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "TrainingResourcePolicy":
        data = require_mapping(payload, "resource_policy")
        allowed = set(cls._NUMERIC_FIELDS) | {"policy_version"}
        reject_unknown_fields(data, allowed, label="resource_policy")
        defaults = cls()
        kwargs: dict[str, object] = {}
        for name in cls._NUMERIC_FIELDS:
            if name not in data:
                continue
            current = getattr(defaults, name)
            if isinstance(current, float):
                kwargs[name] = require_float(data[name], f"resource_policy.{name}",
                                             minimum=1e-12, maximum=1e15)
            else:
                kwargs[name] = require_int(data[name], f"resource_policy.{name}",
                                           minimum=1, maximum=1 << 62)
        if "policy_version" in data:
            kwargs["policy_version"] = require_id(data["policy_version"],
                                                  "resource_policy.policy_version")
        return replace(defaults, **kwargs)  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════════════
#  LoRA
# ══════════════════════════════════════════════════════════════════════════════
MAX_LORA_RANK = 256
MAX_LORA_ALPHA = 1024
MAX_MODULES_TO_SAVE = 8


@dataclass(frozen=True)
class LoRAConfig:
    """The adapter shape. Every value is bounded and every value is hashed."""

    rank: int = 8
    alpha: int = 16
    dropout: float = 0.05
    bias: LoRABias = LoRABias.NONE
    target_policy: LoRATargetPolicy = LoRATargetPolicy.ALL_LINEAR
    modules_to_save: tuple[str, ...] = ()
    use_rslora: bool = False
    task_type: str = "CAUSAL_LM"

    _ALLOWED_TASK_TYPES = ("CAUSAL_LM",)

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "rank", require_int(self.rank, "lora.rank",
                                           minimum=1, maximum=MAX_LORA_RANK))
        setattr_(self, "alpha", require_int(self.alpha, "lora.alpha",
                                            minimum=1, maximum=MAX_LORA_ALPHA))
        dropout = require_float(self.dropout, "lora.dropout", minimum=0.0, maximum=1.0)
        if dropout >= 1.0:
            raise TrainingConfigError(
                "lora.dropout: must be < 1.0 (a dropout of 1 zeroes every adapter "
                "activation, which trains nothing while reporting that it trained)")
        setattr_(self, "dropout", dropout)
        setattr_(self, "bias", require_enum(self.bias, LoRABias, "lora.bias"))
        setattr_(self, "target_policy",
                 require_enum(self.target_policy, LoRATargetPolicy, "lora.target_policy"))
        modules = tuple(self.modules_to_save or ())
        if len(modules) > MAX_MODULES_TO_SAVE:
            raise TrainingConfigError(
                f"lora.modules_to_save: too many entries "
                f"({len(modules)} > {MAX_MODULES_TO_SAVE})")
        setattr_(self, "modules_to_save",
                 tuple(require_id(m, "lora.modules_to_save[]") for m in modules))
        if self.task_type not in self._ALLOWED_TASK_TYPES:
            raise TrainingConfigError(
                f"lora.task_type: {self.task_type!r} is not in "
                f"{list(self._ALLOWED_TASK_TYPES)}")
        setattr_(self, "use_rslora", require_bool(self.use_rslora, "lora.use_rslora"))
        if not self.target_modules:
            raise TrainingConfigError(
                "lora.target_policy: resolved to an empty module set; an adapter with no "
                "target modules trains no parameters")

    @property
    def target_modules(self) -> tuple[str, ...]:
        return self.target_policy.modules

    @property
    def scaling(self) -> float:
        return round(self.alpha / self.rank, 6)

    def to_dict(self) -> dict:
        return {
            "rank": self.rank, "alpha": self.alpha, "dropout": self.dropout,
            "bias": self.bias.value, "target_policy": self.target_policy.value,
            "target_modules": list(self.target_modules),
            "modules_to_save": list(self.modules_to_save),
            "use_rslora": self.use_rslora, "task_type": self.task_type,
            "scaling": self.scaling,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "LoRAConfig":
        data = require_mapping(payload, "lora")
        allowed = {"rank", "alpha", "dropout", "bias", "target_policy",
                   "modules_to_save", "use_rslora", "task_type",
                   # Derived, and therefore verified rather than read: a record may
                   # carry them for a human reader, but it may not disagree with them.
                   "target_modules", "scaling"}
        reject_unknown_fields(data, allowed, label="lora")
        defaults = cls()
        built = cls(
            rank=data.get("rank", defaults.rank),
            alpha=data.get("alpha", defaults.alpha),
            dropout=data.get("dropout", defaults.dropout),
            bias=data.get("bias", defaults.bias),
            target_policy=data.get("target_policy", defaults.target_policy),
            modules_to_save=tuple(data.get("modules_to_save", ()) or ()),
            use_rslora=data.get("use_rslora", defaults.use_rslora),
            task_type=data.get("task_type", defaults.task_type),
        )
        if "target_modules" in data and list(data["target_modules"] or ()) != list(
                built.target_modules):
            raise TrainingConfigError(
                f"lora.target_modules: the record lists "
                f"{list(data['target_modules'] or ())}, but policy "
                f"{built.target_policy.value!r} resolves to {list(built.target_modules)}; "
                f"the module set is chosen by the policy, never by the file")
        if "scaling" in data and float(data["scaling"]) != built.scaling:
            raise TrainingConfigError(
                f"lora.scaling: the record claims {data['scaling']}, alpha/rank is "
                f"{built.scaling}")
        return built


# ══════════════════════════════════════════════════════════════════════════════
#  The configuration
# ══════════════════════════════════════════════════════════════════════════════
#: Splits a model may be fitted or steered on.
_TRAIN_SIDE = (DatasetSplit.TRAIN, DatasetSplit.VALIDATION)

MAX_NOTES_CHARS = 4000


def _check_schema_version(payload: dict) -> str:
    declared = str(payload.get("schema_version", "")).strip()
    if not declared:
        raise TrainingConfigError("training_config: missing schema_version")
    expected_major = TRAINING_SCHEMA_VERSION.rsplit(".", 1)[0]
    if declared.rsplit(".", 1)[0] != expected_major:
        raise TrainingConfigError(
            f"training_config: incompatible schema_version {declared!r} "
            f"(this build speaks {TRAINING_SCHEMA_VERSION!r})")
    return declared


@dataclass(frozen=True)
class TrainingConfig:
    """A complete, bounded training experiment description. Frozen and hashable."""

    run_id: str
    experiment_name: str
    method: TrainingMethod
    base_model_id: str
    base_model_revision: str
    base_model_parameters_b: float
    tokenizer_id: str
    tokenizer_revision: str
    dataset_reference: TrainingDatasetReference
    output_root_id: str
    created_at_utc: str
    seed: int = 42
    epochs: int = 1
    max_steps: int = 1000
    batch_size: int = 1
    gradient_accumulation_steps: int = 8
    learning_rate: float = 2e-4
    weight_decay: float = 0.0
    warmup_ratio: float = 0.03
    max_sequence_length: int = 512
    gradient_checkpointing: bool = True
    precision_policy: PrecisionPolicy = PrecisionPolicy.AUTO_SAFE
    device_policy: DevicePolicy = DevicePolicy.AUTO_SAFE
    lora: LoRAConfig = LoRAConfig()
    dataloader_workers: int = 0
    checkpoint_strategy: CheckpointStrategy = CheckpointStrategy.NO
    checkpoint_interval_steps: int = 0
    max_checkpoints: int = 1
    logging_target: LoggingTarget = LoggingTarget.LOCAL_JSONL
    logging_interval_steps: int = 10
    model_download_policy: ModelDownloadPolicy = ModelDownloadPolicy.DENY
    dependency_profile: DependencyProfile = DependencyProfile.TRAINING
    resource_policy: TrainingResourcePolicy = TrainingResourcePolicy()
    training_split: DatasetSplit = DatasetSplit.TRAIN
    validation_split: DatasetSplit = DatasetSplit.VALIDATION
    validation_strategy: ValidationStrategy = ValidationStrategy.NO
    hidden_evaluation_reference: DatasetSplit = DatasetSplit.HIDDEN_EVALUATION
    security_regression_reference: DatasetSplit = DatasetSplit.SECURITY_REGRESSION
    trust_remote_code: bool = False
    base_model_family: str = ""
    notes: str = ""
    schema_version: str = TRAINING_SCHEMA_VERSION

    #: Every key a config document may carry. Anything else fails closed.
    FIELDS = (
        "schema_version", "run_id", "experiment_name", "method", "base_model_id",
        "base_model_revision", "base_model_parameters_b", "base_model_family",
        "tokenizer_id", "tokenizer_revision", "trust_remote_code", "dataset_reference",
        "training_split", "validation_split", "validation_strategy",
        "hidden_evaluation_reference",
        "security_regression_reference", "output_root_id", "seed", "epochs",
        "max_steps", "batch_size", "gradient_accumulation_steps", "learning_rate",
        "weight_decay", "warmup_ratio", "max_sequence_length",
        "gradient_checkpointing", "precision_policy", "device_policy", "lora",
        "dataloader_workers", "checkpoint_strategy", "checkpoint_interval_steps",
        "max_checkpoints", "logging_target", "logging_interval_steps",
        "model_download_policy", "dependency_profile", "resource_policy",
        "created_at_utc", "notes",
    )

    def __post_init__(self) -> None:  # noqa: C901 — one validator per invariant
        setattr_ = object.__setattr__
        policy = self.resource_policy
        if not isinstance(policy, TrainingResourcePolicy):
            raise TrainingConfigError("resource_policy: expected a TrainingResourcePolicy")

        setattr_(self, "run_id", require_id(self.run_id, "run_id"))
        refuse_path_like(self.run_id, "run_id")
        setattr_(self, "experiment_name",
                 require_text(self.experiment_name, "experiment_name", max_len=200))
        setattr_(self, "method", require_enum(self.method, TrainingMethod, "method"))
        setattr_(self, "output_root_id", require_id(self.output_root_id,
                                                    "output_root_id"))
        refuse_path_like(self.output_root_id, "output_root_id")
        setattr_(self, "created_at_utc",
                 require_timestamp(self.created_at_utc, "created_at_utc"))

        # ── model / tokenizer identity ──────────────────────────────────────
        setattr_(self, "base_model_id", require_model_id(self.base_model_id,
                                                         "base_model_id"))
        setattr_(self, "tokenizer_id", require_model_id(self.tokenizer_id,
                                                        "tokenizer_id"))
        setattr_(self, "base_model_revision",
                 require_revision(self.base_model_revision, "base_model_revision"))
        setattr_(self, "tokenizer_revision",
                 require_revision(self.tokenizer_revision, "tokenizer_revision"))
        if self.tokenizer_id != self.base_model_id:
            raise TrainingConfigError(
                f"tokenizer_id: {self.tokenizer_id!r} is not the base model "
                f"{self.base_model_id!r}. A tokenizer from a different repository "
                f"silently changes what every token id means; if a separate tokenizer is "
                f"ever needed it will be a reviewed capability, not a config field")
        if self.tokenizer_revision != self.base_model_revision:
            raise TrainingConfigError(
                "tokenizer_revision: must equal base_model_revision; the same repository "
                "at two revisions is two different vocabularies")
        if self.base_model_family:
            setattr_(self, "base_model_family",
                     require_id(self.base_model_family, "base_model_family"))
        setattr_(self, "base_model_parameters_b",
                 require_float(self.base_model_parameters_b, "base_model_parameters_b",
                               minimum=1e-4, maximum=1e4))

        setattr_(self, "trust_remote_code",
                 require_bool(self.trust_remote_code, "trust_remote_code"))
        if self.trust_remote_code:
            raise TrainingConfigError(
                "trust_remote_code: must be false. Enabling it executes Python fetched "
                "from a model repository inside this process; no configuration file is "
                "permitted to grant that, and there is no flag that overrides this")

        # ── dataset binding ─────────────────────────────────────────────────
        if not isinstance(self.dataset_reference, TrainingDatasetReference):
            raise TrainingConfigError(
                "dataset_reference: expected a TrainingDatasetReference")
        for name in ("training_split", "validation_split",
                     "hidden_evaluation_reference", "security_regression_reference"):
            setattr_(self, name, require_enum(getattr(self, name), DatasetSplit, name))
        if self.training_split not in _TRAIN_SIDE:
            raise TrainingConfigError(
                f"training_split: {self.training_split.value!r} is not a train-side "
                f"split; evaluation material is never fitted on")
        if self.validation_split not in _TRAIN_SIDE:
            raise TrainingConfigError(
                f"validation_split: {self.validation_split.value!r} is not a train-side "
                f"split")
        if self.training_split is self.validation_split:
            raise TrainingConfigError(
                "validation_split: must differ from training_split; validating on the "
                "training split measures memorisation and calls it generalisation")
        setattr_(self, "validation_strategy",
                 require_enum(self.validation_strategy, ValidationStrategy,
                              "validation_strategy"))
        if self.validation_strategy not in SUPPORTED_VALIDATION_STRATEGIES:
            raise TrainingConfigError(
                f"validation_strategy: {self.validation_strategy.value!r} is not "
                f"supported (supported: "
                f"{sorted(s.value for s in SUPPORTED_VALIDATION_STRATEGIES)}). "
                f"Per-step evaluation re-measures the same small validation split once "
                f"per optimizer step and charges its cost to every one of them; the "
                f"movement it produces is sampling noise, not learning. A cadence that "
                f"expensive needs its own evidence rather than a default")
        if self.hidden_evaluation_reference in _TRAIN_SIDE:
            raise TrainingConfigError(
                "hidden_evaluation_reference: names a train-side split; a hidden "
                "evaluation the model was fitted on is not hidden")
        if self.security_regression_reference in _TRAIN_SIDE:
            raise TrainingConfigError(
                "security_regression_reference: names a train-side split")
        if self.hidden_evaluation_reference is self.security_regression_reference:
            raise TrainingConfigError(
                "security_regression_reference: must differ from "
                "hidden_evaluation_reference")

        # ── hyperparameters, each against the declared ceiling ──────────────
        setattr_(self, "seed", require_int(self.seed, "seed", minimum=0,
                                           maximum=2**32 - 1))
        setattr_(self, "epochs", require_int(self.epochs, "epochs", minimum=1,
                                             maximum=policy.max_epochs))
        setattr_(self, "max_steps", require_int(self.max_steps, "max_steps", minimum=1,
                                                maximum=policy.max_steps))
        setattr_(self, "batch_size", require_int(self.batch_size, "batch_size",
                                                 minimum=1,
                                                 maximum=policy.max_batch_size))
        setattr_(self, "gradient_accumulation_steps",
                 require_int(self.gradient_accumulation_steps,
                             "gradient_accumulation_steps", minimum=1,
                             maximum=policy.max_gradient_accumulation))
        lr = require_float(self.learning_rate, "learning_rate", minimum=0.0,
                           maximum=policy.max_learning_rate)
        if lr <= 0.0:
            raise TrainingConfigError("learning_rate: must be > 0")
        setattr_(self, "learning_rate", lr)
        setattr_(self, "weight_decay",
                 require_float(self.weight_decay, "weight_decay", minimum=0.0,
                               maximum=1.0))
        setattr_(self, "warmup_ratio",
                 require_float(self.warmup_ratio, "warmup_ratio", minimum=0.0,
                               maximum=0.5))
        setattr_(self, "max_sequence_length",
                 require_int(self.max_sequence_length, "max_sequence_length",
                             minimum=16, maximum=policy.max_sequence_length))
        setattr_(self, "dataloader_workers",
                 require_int(self.dataloader_workers, "dataloader_workers", minimum=0,
                             maximum=policy.max_dataloader_workers))
        setattr_(self, "gradient_checkpointing",
                 require_bool(self.gradient_checkpointing, "gradient_checkpointing"))
        setattr_(self, "max_checkpoints",
                 require_int(self.max_checkpoints, "max_checkpoints", minimum=0,
                             maximum=policy.max_checkpoint_count))
        setattr_(self, "checkpoint_interval_steps",
                 require_int(self.checkpoint_interval_steps,
                             "checkpoint_interval_steps", minimum=0,
                             maximum=policy.max_steps))
        setattr_(self, "logging_interval_steps",
                 require_int(self.logging_interval_steps, "logging_interval_steps",
                             minimum=1, maximum=policy.max_steps))
        if self.base_model_parameters_b > policy.max_model_parameters_b:
            raise TrainingConfigError(
                f"base_model_parameters_b: {self.base_model_parameters_b}B exceeds the "
                f"policy ceiling of {policy.max_model_parameters_b}B")

        # ── closed policies ─────────────────────────────────────────────────
        setattr_(self, "precision_policy",
                 require_enum(self.precision_policy, PrecisionPolicy,
                              "precision_policy"))
        setattr_(self, "device_policy",
                 require_enum(self.device_policy, DevicePolicy, "device_policy"))
        setattr_(self, "checkpoint_strategy",
                 require_enum(self.checkpoint_strategy, CheckpointStrategy,
                              "checkpoint_strategy"))
        setattr_(self, "logging_target",
                 require_enum(self.logging_target, LoggingTarget, "logging_target"))
        setattr_(self, "model_download_policy",
                 require_enum(self.model_download_policy, ModelDownloadPolicy,
                              "model_download_policy"))
        setattr_(self, "dependency_profile",
                 require_enum(self.dependency_profile, DependencyProfile,
                              "dependency_profile"))
        if self.checkpoint_strategy not in SUPPORTED_CHECKPOINT_STRATEGIES:
            raise TrainingConfigError(
                f"checkpoint_strategy: {self.checkpoint_strategy.value!r} is "
                f"unsupported. Intermediate trainer checkpoints write a nested "
                f"checkpoint directory containing pickle-shaped optimizer and runtime "
                f"state (optimizer.pt, scheduler.pt, rng_state.pth, training_args.bin) "
                f"that cannot enter the adapter artifact allowlist, so the run would "
                f"train to completion and then be refused at artifact verification. "
                f"Use 'no'.")
        if (self.validation_strategy is not ValidationStrategy.NO
                and self.checkpoint_strategy is not CheckpointStrategy.NO):
            # Belt and braces: `checkpoint_strategy` is already restricted to NO by the
            # check immediately above, so this is unreachable today. It is written anyway
            # because the ONE route by which enabling evaluation could reintroduce
            # pickle-shaped trainer state is arriving alongside checkpointing, and the
            # refusal should exist before the combination does rather than after somebody
            # widens the other enum.
            raise TrainingConfigError(
                f"validation_strategy: {self.validation_strategy.value!r} with "
                f"checkpoint_strategy {self.checkpoint_strategy.value!r}; train-time "
                f"validation never authorises checkpoint writing, and the adapter "
                f"artifact policy refuses the pickle-shaped state a checkpoint holds")
        if not isinstance(self.lora, LoRAConfig):
            raise TrainingConfigError("lora: expected a LoRAConfig")
        if (self.method.needs_quantization
                and self.precision_policy not in (PrecisionPolicy.AUTO_SAFE,
                                                  PrecisionPolicy.INT4_QLORA)):
            raise TrainingConfigError(
                f"precision_policy: {self.precision_policy.value!r} contradicts "
                f"{self.method.value!r}, which is defined by its 4-bit base")
        if (self.precision_policy is PrecisionPolicy.INT4_QLORA
                and not self.method.needs_quantization):
            raise TrainingConfigError(
                f"precision_policy: int4_qlora requires a QLoRA method, not "
                f"{self.method.value!r}")

        if self.notes:
            setattr_(self, "notes", require_text(self.notes, "notes", min_len=0,
                                                 max_len=MAX_NOTES_CHARS))
            assert_no_private_content(self.notes, label="training_config.notes")
        setattr_(self, "schema_version", _check_schema_version(
            {"schema_version": self.schema_version}))

    # ── derived ─────────────────────────────────────────────────────────────
    @property
    def effective_batch_size(self) -> int:
        return self.batch_size * self.gradient_accumulation_steps

    @property
    def train_time_validation_enabled(self) -> bool:
        """Whether a run would hand its VALIDATION split to the trainer as an eval arm.

        One property rather than ``!= NO`` scattered across the backend, the execution
        stage and the CLI, so "is validation on" has a single answer.
        """
        return self.validation_strategy is not ValidationStrategy.NO

    @property
    def has_immutable_revision(self) -> bool:
        return is_immutable_revision(self.base_model_revision)

    @property
    def revision_is_placeholder(self) -> bool:
        return is_placeholder(self.base_model_revision)

    def to_dict(self) -> dict:
        """The canonical, exportable form. Contains no path and no host detail.

        ``validation_strategy`` appears only when it is not ``NO``, and that is
        deliberate. The key is what makes a run measure its validation split, so the
        canonical form — and therefore ``config_hash``, and therefore every plan hash
        derived from it — gains it at exactly the moment the run's behaviour gains it. A
        config that performs no train-time validation hashes to what it always hashed to,
        so no configuration written before this field existed is re-identified, and a
        config that does perform it can never share an identity with one that does not.

        The same rule already governs :func:`training_gym.datasets.export.sft_row`, which
        emits a system turn only for a candidate that has one, for the same reason: an
        always-present key whose value means "absent" makes two different things look
        like one.
        """
        strategy = ({"validation_strategy": self.validation_strategy.value}
                    if self.validation_strategy is not ValidationStrategy.NO else {})
        return {
            **strategy,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "experiment_name": self.experiment_name,
            "method": self.method.value,
            "base_model_id": self.base_model_id,
            "base_model_revision": self.base_model_revision,
            "base_model_parameters_b": self.base_model_parameters_b,
            "base_model_family": self.base_model_family,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "trust_remote_code": self.trust_remote_code,
            "dataset_reference": self.dataset_reference.to_dict(),
            "training_split": self.training_split.value,
            "validation_split": self.validation_split.value,
            "hidden_evaluation_reference": self.hidden_evaluation_reference.value,
            "security_regression_reference": self.security_regression_reference.value,
            "output_root_id": self.output_root_id,
            "seed": self.seed,
            "epochs": self.epochs,
            "max_steps": self.max_steps,
            "batch_size": self.batch_size,
            "gradient_accumulation_steps": self.gradient_accumulation_steps,
            "learning_rate": self.learning_rate,
            "weight_decay": self.weight_decay,
            "warmup_ratio": self.warmup_ratio,
            "max_sequence_length": self.max_sequence_length,
            "gradient_checkpointing": self.gradient_checkpointing,
            "precision_policy": self.precision_policy.value,
            "device_policy": self.device_policy.value,
            "lora": self.lora.to_dict(),
            "dataloader_workers": self.dataloader_workers,
            "checkpoint_strategy": self.checkpoint_strategy.value,
            "checkpoint_interval_steps": self.checkpoint_interval_steps,
            "max_checkpoints": self.max_checkpoints,
            "logging_target": self.logging_target.value,
            "logging_interval_steps": self.logging_interval_steps,
            "model_download_policy": self.model_download_policy.value,
            "dependency_profile": self.dependency_profile.value,
            "resource_policy": self.resource_policy.to_dict(),
            "created_at_utc": self.created_at_utc,
            "notes": self.notes,
        }

    def config_hash(self) -> str:
        """SHA-256 over the canonical form. Key order cannot change it."""
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "TrainingConfig":
        """Parse a config document. Unknown keys and bad versions fail closed."""
        data = require_mapping(payload, "training_config")
        _check_schema_version(data)
        reject_unknown_fields(data, cls.FIELDS, label="training_config")
        for required in ("run_id", "experiment_name", "method", "base_model_id",
                         "base_model_revision", "base_model_parameters_b",
                         "tokenizer_id", "tokenizer_revision", "dataset_reference",
                         "output_root_id", "created_at_utc", "seed"):
            if required not in data:
                raise TrainingConfigError(f"training_config: missing {required!r}")
        defaults = cls(
            run_id="placeholder", experiment_name="placeholder",
            method=TrainingMethod.SFT_LORA, base_model_id="org/name",
            base_model_revision=REQUIRED_REVISION_PLACEHOLDER,
            base_model_parameters_b=0.6, tokenizer_id="org/name",
            tokenizer_revision=REQUIRED_REVISION_PLACEHOLDER,
            dataset_reference=TrainingDatasetReference.placeholder(),
            output_root_id="placeholder", created_at_utc="1970-01-01T00:00:00Z")
        known: dict[str, object] = {}
        for name in cls.FIELDS:
            if name in data:
                known[name] = data[name]
        return cls(
            run_id=known["run_id"],  # type: ignore[arg-type]
            experiment_name=known["experiment_name"],  # type: ignore[arg-type]
            method=known["method"],  # type: ignore[arg-type]
            base_model_id=known["base_model_id"],  # type: ignore[arg-type]
            base_model_revision=known["base_model_revision"],  # type: ignore[arg-type]
            base_model_parameters_b=known["base_model_parameters_b"],  # type: ignore[arg-type]
            base_model_family=known.get("base_model_family", ""),  # type: ignore[arg-type]
            tokenizer_id=known["tokenizer_id"],  # type: ignore[arg-type]
            tokenizer_revision=known["tokenizer_revision"],  # type: ignore[arg-type]
            trust_remote_code=known.get("trust_remote_code", False),  # type: ignore[arg-type]
            dataset_reference=TrainingDatasetReference.from_dict(
                known["dataset_reference"]),
            training_split=known.get("training_split", defaults.training_split),  # type: ignore[arg-type]
            validation_split=known.get("validation_split", defaults.validation_split),  # type: ignore[arg-type]
            # Absent means NO, which is what every config written before this field
            # existed meant. A document that does name it is read as written; a document
            # that names it under a build too old to know it is refused by
            # `reject_unknown_fields` rather than silently losing the setting.
            validation_strategy=known.get("validation_strategy",
                                          defaults.validation_strategy),  # type: ignore[arg-type]
            hidden_evaluation_reference=known.get(
                "hidden_evaluation_reference", defaults.hidden_evaluation_reference),  # type: ignore[arg-type]
            security_regression_reference=known.get(
                "security_regression_reference",
                defaults.security_regression_reference),  # type: ignore[arg-type]
            output_root_id=known["output_root_id"],  # type: ignore[arg-type]
            seed=known["seed"],  # type: ignore[arg-type]
            epochs=known.get("epochs", defaults.epochs),  # type: ignore[arg-type]
            max_steps=known.get("max_steps", defaults.max_steps),  # type: ignore[arg-type]
            batch_size=known.get("batch_size", defaults.batch_size),  # type: ignore[arg-type]
            gradient_accumulation_steps=known.get(
                "gradient_accumulation_steps",
                defaults.gradient_accumulation_steps),  # type: ignore[arg-type]
            learning_rate=known.get("learning_rate", defaults.learning_rate),  # type: ignore[arg-type]
            weight_decay=known.get("weight_decay", defaults.weight_decay),  # type: ignore[arg-type]
            warmup_ratio=known.get("warmup_ratio", defaults.warmup_ratio),  # type: ignore[arg-type]
            max_sequence_length=known.get("max_sequence_length",
                                          defaults.max_sequence_length),  # type: ignore[arg-type]
            gradient_checkpointing=known.get("gradient_checkpointing",
                                             defaults.gradient_checkpointing),  # type: ignore[arg-type]
            precision_policy=known.get("precision_policy", defaults.precision_policy),  # type: ignore[arg-type]
            device_policy=known.get("device_policy", defaults.device_policy),  # type: ignore[arg-type]
            lora=LoRAConfig.from_dict(known["lora"]) if "lora" in known
            else defaults.lora,
            dataloader_workers=known.get("dataloader_workers",
                                         defaults.dataloader_workers),  # type: ignore[arg-type]
            checkpoint_strategy=known.get("checkpoint_strategy",
                                          defaults.checkpoint_strategy),  # type: ignore[arg-type]
            checkpoint_interval_steps=known.get("checkpoint_interval_steps",
                                                defaults.checkpoint_interval_steps),  # type: ignore[arg-type]
            max_checkpoints=known.get("max_checkpoints", defaults.max_checkpoints),  # type: ignore[arg-type]
            logging_target=known.get("logging_target", defaults.logging_target),  # type: ignore[arg-type]
            logging_interval_steps=known.get("logging_interval_steps",
                                             defaults.logging_interval_steps),  # type: ignore[arg-type]
            model_download_policy=known.get("model_download_policy",
                                            defaults.model_download_policy),  # type: ignore[arg-type]
            dependency_profile=known.get("dependency_profile",
                                         defaults.dependency_profile),  # type: ignore[arg-type]
            resource_policy=TrainingResourcePolicy.from_dict(known["resource_policy"])
            if "resource_policy" in known else defaults.resource_policy,
            created_at_utc=known["created_at_utc"],  # type: ignore[arg-type]
            notes=known.get("notes", ""),  # type: ignore[arg-type]
            schema_version=str(data["schema_version"]),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Safe document loading
# ══════════════════════════════════════════════════════════════════════════════
#: A training config is a reviewable document, not a data file.
MAX_CONFIG_BYTES = 256 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """``json.loads`` keeps the LAST duplicate key silently.

    A document carrying ``"trust_remote_code": false`` followed by
    ``"trust_remote_code": true`` parses cleanly and means the opposite of what a
    reviewer reading the top of the file believes it means. Refuse the document.
    """
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise TrainingConfigError(
                f"training_config: duplicate key {key!r}; a document that states a "
                f"field twice does not have one meaning")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name: str) -> object:
    """``json.loads`` accepts bare ``NaN``/``Infinity``; a hyperparameter may not be one.

    A learning rate of ``Infinity`` passes every ``> 0`` check ever written, and ``NaN``
    fails every comparison including the ones that were supposed to refuse it.
    """
    raise TrainingConfigError(
        f"config: the non-finite JSON constant {name!r} is not a hyperparameter")


def load_config_document(path: str | Path) -> dict:
    """Read a config file safely and return the raw mapping. Parses only; never plans.

    Refuses a symlink, a multiply-linked file, a non-regular file, an oversized file and
    any JSON that is not a single object with unique keys. There is no include
    directive, no environment interpolation and no URL: whatever the file says is
    exactly what it says.
    """
    target = Path(path)
    if target.is_symlink():
        raise TrainingConfigError(
            f"config: {target.name!r} is a symlink; the file that is reviewed and the "
            f"file that is read must be the same file")
    if not target.is_file():
        raise TrainingConfigError(f"config: {target.name!r} is not a regular file")
    try:
        stat = target.stat()
    except OSError as exc:
        raise TrainingConfigError(f"config: cannot stat {target.name!r} ({exc})") from None
    if getattr(stat, "st_nlink", 1) > 1:
        raise TrainingConfigError(
            f"config: {target.name!r} has more than one hard link; another name for the "
            f"same bytes can change them after review")
    if stat.st_size > MAX_CONFIG_BYTES:
        raise TrainingConfigError(
            f"config: {target.name!r} is {stat.st_size} bytes "
            f"(> {MAX_CONFIG_BYTES}); a training config is a reviewable document")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrainingConfigError(
            f"config: {target.name!r} is not readable UTF-8 ({type(exc).__name__})"
        ) from None
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                             parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise TrainingConfigError(
            f"config: {target.name!r} is not valid JSON (line {exc.lineno})") from None
    if not isinstance(payload, dict):
        raise TrainingConfigError(
            f"config: {target.name!r} must contain a JSON object, got "
            f"{type(payload).__name__}")
    return payload


def load_training_config(path: str | Path) -> TrainingConfig:
    """Load and validate a config file in one fail-closed step."""
    return TrainingConfig.from_dict(load_config_document(path))


__all__ = [
    "ALLOWED_TRAINING_TRANSITIONS", "MAX_CONFIG_BYTES", "MAX_LORA_ALPHA",
    "MAX_LORA_RANK", "MAX_NOTES_CHARS", "PLACEHOLDER_PREFIX", "PLANNER_VERSION",
    "REQUIRED_REVISION_PLACEHOLDER", "RESOURCE_POLICY_VERSION",
    "S3A_REACHABLE_STATES", "SUPPORTED_CHECKPOINT_STRATEGIES",
    "SUPPORTED_VALIDATION_STRATEGIES", "TRAINING_SCHEMA_VERSION", "CheckpointStrategy",
    "DependencyProfile", "DevicePolicy", "LoRABias", "LoRAConfig",
    "LoRATargetPolicy", "LoggingTarget", "ModelDownloadPolicy", "PrecisionPolicy",
    "TrainingConfig", "TrainingConfigError", "TrainingMethod",
    "TrainingResourcePolicy", "TrainingRunState", "TrainingTransitionError",
    "ValidationStrategy",
    "check_training_transition", "is_immutable_revision", "is_placeholder",
    "legacy_method", "load_config_document", "load_training_config",
    "refuse_path_like", "require_model_id", "require_revision",
]
