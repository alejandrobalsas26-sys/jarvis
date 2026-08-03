"""training_gym/evaluation/config.py — V69 M62 S3C: the strict evaluation configuration.

WHAT A CONFIG MAY SAY
---------------------
Which base model, which adapter run, which dataset version, which splits, how generation
is decoded, which policies bind the result, and nothing else. The field list IS the
capability list: there is no field for an executable, a module path, a backend class, a
shell argument, an environment variable, a callback, a URL, a credential, a host path, an
expected answer or a promotion request — so none of those can be requested, whatever a
config file says.

Unknown fields are refused rather than ignored. A config carrying ``"promote": true``
under a key nobody implemented must stop the load, not sail through while the operator
believes it asked for something.

WHY THE OUTPUT ROOT IS NOT IN THE CONFIG
----------------------------------------
The same reason S3A keeps it out of the plan: a config is a document that is reviewed,
diffed and pasted into a ticket, and the training tree lives inside the operator's home
directory. The root is supplied at invocation and enters the plan as a digest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..datasets.candidate import DatasetSplit
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    assert_no_private_content,
    check_schema_version,
    reject_unknown_fields,
    require_enum,
    require_id,
    require_int,
    require_mapping,
    require_str_tuple,
    require_text,
    sha256_obj,
)
from ..task_spec import require_timestamp
from ..training.model_identity import ModelIdentity
from . import EVALUATION_SCHEMA_VERSION, EVALUATOR_VERSION
from .generation import GenerationPolicy
from .policy import (
    EvaluationPolicySet,
    GatePolicy,
    GraderPolicy,
    MetricPolicy,
    ResourceCeilings,
    StatisticalPolicy,
    TaskFamilyPolicy,
)
from .task_pack import (
    DIAGNOSTIC_ONLY_SPLITS,
    EVALUABLE_SPLITS,
    MANDATORY_SPLITS,
    NEVER_EVALUABLE_SPLITS,
)

#: A config file larger than this is not a config.
MAX_CONFIG_BYTES = 256 * 1024

#: Field names that would grant a capability this stage refuses to have. Rejected by
#: name, with a message that says why, so an operator who tried gets an explanation
#: rather than a generic "unknown field".
FORBIDDEN_CONFIG_FIELDS: dict[str, str] = {
    "backend": "the evaluation backend is chosen from a closed internal registry",
    "backend_class": "a class named in a config is arbitrary code execution",
    "backend_module": "a module path named in a config is arbitrary code execution",
    "entrypoint": "there is no configurable entrypoint",
    "executable": "this stage runs no external program",
    "command": "this stage runs no external program",
    "shell": "there is no shell",
    "args": "there are no pass-through arguments",
    "env": "this stage reads no configured environment",
    "environment": "this stage reads no configured environment",
    "callback": "there is no configurable callback",
    "hook": "there is no configurable hook",
    "url": "this stage contacts no network",
    "endpoint": "this stage contacts no network",
    "api_key": "this stage authenticates to nothing",
    # These three are the NAMES of fields that are refused, mapped to the reason. They
    # are a denylist, not a credential — the value is an explanation shown to an
    # operator. Asserted by test_every_capability_granting_field_is_refused_by_name.
    "token": "this stage authenticates to nothing",  # nosec B105
    "credential": "this stage authenticates to nothing",  # nosec B105
    "secret": "this stage authenticates to nothing",  # nosec B105
    "cache_dir": "a host path may not travel inside a reviewable config",
    "cache_path": "a host path may not travel inside a reviewable config",
    "output_root": "the output root is supplied at invocation, never persisted",
    "expected_answers": "an expected answer in a config is the answer handed to the model",
    "targets": "an expected answer in a config is the answer handed to the model",
    "rubric": "an expected answer in a config is the answer handed to the model",
    "disable_graders": "a grader cannot be switched off; that is what mandatory means",
    "skip_graders": "a grader cannot be switched off; that is what mandatory means",
    "exclude_tasks": "a caller-supplied exclusion list is how failures disappear",
    "exclude_failures": "a caller-supplied exclusion list is how failures disappear",
    "promote": "this stage promotes nothing",
    "auto_promote": "this stage promotes nothing",
    "activate": "this stage activates nothing",
    "register": "this stage writes no registry",
    "fake_backend": "the fake backend is reachable from tests only",
    "allow_model_download": "this stage downloads nothing",
    "trust_remote_code": "remote code is refused, not configured",
}

#: Every key a config file may carry.
CONFIG_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "evaluation_schema_version", "evaluator_version", "evaluation_id",
    "evaluation_generation", "evaluation_policy_version", "baseline_model",
    "candidate_adapter", "dataset", "splits", "generation", "graders", "metrics",
    "statistics", "families", "gates", "resources", "seed", "created_at_utc",
    "limitations", "notes",
)

_BASELINE_FIELDS: tuple[str, ...] = (
    "provider", "model_id", "revision", "parameters_b", "family", "tokenizer_id",
    "tokenizer_revision", "license_reference",
)

_ADAPTER_FIELDS: tuple[str, ...] = ("run_id", "expected_manifest_hash",
                                    "expected_plan_hash")

_DATASET_FIELDS: tuple[str, ...] = ("dataset_id", "dataset_version")


class EvaluationConfigError(SchemaError):
    """A configuration that will not be loaded or acted on as described."""


# ══════════════════════════════════════════════════════════════════════════════
#  The state machine
# ══════════════════════════════════════════════════════════════════════════════
class EvaluationRunState(str, Enum):
    """Where an evaluation is. There is no state here that means PROMOTED or ACTIVE."""

    CREATED = "created"
    CONFIG_VALIDATED = "config_validated"
    REFERENCES_VERIFIED = "references_verified"
    TASK_PACK_VERIFIED = "task_pack_verified"
    DEPENDENCIES_VERIFIED = "dependencies_verified"
    HARDWARE_VERIFIED = "hardware_verified"
    PLANNED = "planned"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PREFLIGHT_VERIFIED = "preflight_verified"
    STARTING = "starting"
    RUNNING_BASELINE = "running_baseline"
    RUNNING_CANDIDATE = "running_candidate"
    SCORING = "scoring"
    COMPARING = "comparing"
    ARTIFACT_VALIDATION = "artifact_validation"
    COMPLETED = "completed"
    INTERRUPTING = "interrupting"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    QUARANTINED = "quarantined"

    @property
    def is_terminal(self) -> bool:
        return self in (EvaluationRunState.COMPLETED, EvaluationRunState.INTERRUPTED,
                        EvaluationRunState.FAILED, EvaluationRunState.QUARANTINED)

    @property
    def is_successful(self) -> bool:
        """COMPLETED alone. Interrupted is not completed, and failed is not completed."""
        return self is EvaluationRunState.COMPLETED


_S = EvaluationRunState

#: The only legal moves. Notably absent: PLANNED → RUNNING (preflight and confirmation sit
#: between them), and any RUNNING → COMPLETED (scoring, comparison and artifact validation
#: sit between them, because a backend that returned is not an evaluation that verified).
ALLOWED_EVALUATION_TRANSITIONS: dict[EvaluationRunState,
                                     frozenset[EvaluationRunState]] = {
    _S.CREATED: frozenset({_S.CONFIG_VALIDATED, _S.FAILED}),
    _S.CONFIG_VALIDATED: frozenset({_S.REFERENCES_VERIFIED, _S.FAILED}),
    _S.REFERENCES_VERIFIED: frozenset({_S.TASK_PACK_VERIFIED, _S.FAILED}),
    _S.TASK_PACK_VERIFIED: frozenset({_S.DEPENDENCIES_VERIFIED, _S.FAILED}),
    _S.DEPENDENCIES_VERIFIED: frozenset({_S.HARDWARE_VERIFIED, _S.FAILED}),
    _S.HARDWARE_VERIFIED: frozenset({_S.PLANNED, _S.FAILED}),
    _S.PLANNED: frozenset({_S.AWAITING_CONFIRMATION, _S.PREFLIGHT_VERIFIED, _S.FAILED}),
    _S.AWAITING_CONFIRMATION: frozenset({_S.PREFLIGHT_VERIFIED, _S.FAILED}),
    _S.PREFLIGHT_VERIFIED: frozenset({_S.STARTING, _S.FAILED}),
    _S.STARTING: frozenset({_S.RUNNING_BASELINE, _S.INTERRUPTING, _S.FAILED}),
    _S.RUNNING_BASELINE: frozenset({_S.RUNNING_CANDIDATE, _S.INTERRUPTING, _S.FAILED}),
    _S.RUNNING_CANDIDATE: frozenset({_S.SCORING, _S.INTERRUPTING, _S.FAILED}),
    _S.SCORING: frozenset({_S.COMPARING, _S.INTERRUPTING, _S.FAILED}),
    _S.COMPARING: frozenset({_S.ARTIFACT_VALIDATION, _S.INTERRUPTING, _S.FAILED}),
    _S.ARTIFACT_VALIDATION: frozenset({_S.COMPLETED, _S.FAILED, _S.QUARANTINED}),
    _S.INTERRUPTING: frozenset({_S.INTERRUPTED, _S.FAILED, _S.QUARANTINED}),
    _S.COMPLETED: frozenset(),
    _S.INTERRUPTED: frozenset({_S.QUARANTINED}),
    _S.FAILED: frozenset({_S.QUARANTINED}),
    _S.QUARANTINED: frozenset(),
}


class EvaluationTransitionError(EvaluationConfigError):
    """An evaluation tried to move somewhere it may not go."""


def check_evaluation_transition(current: EvaluationRunState,
                                target: EvaluationRunState) -> EvaluationRunState:
    """Permit a move or refuse it. Fail closed: an unlisted move is not a move."""
    current = require_enum(current, EvaluationRunState, "evaluation.state")
    target = require_enum(target, EvaluationRunState, "evaluation.target_state")
    allowed = ALLOWED_EVALUATION_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise EvaluationTransitionError(
            f"evaluation: {current.value} -> {target.value} is not a legal transition "
            f"(legal: {sorted(s.value for s in allowed) or 'none — terminal'})")
    return target


# ══════════════════════════════════════════════════════════════════════════════
#  Split selection
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SplitSelection:
    """Which splits this evaluation draws from, and what each is allowed to decide."""

    splits: tuple[DatasetSplit, ...] = (DatasetSplit.HIDDEN_EVALUATION,
                                        DatasetSplit.SECURITY_REGRESSION)
    #: Splits included as labelled diagnostics. Never in a headline claim.
    diagnostic_splits: tuple[DatasetSplit, ...] = ()

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        resolved = tuple(require_enum(s, DatasetSplit, "splits[]") for s in self.splits)
        diagnostics = tuple(require_enum(s, DatasetSplit, "diagnostic_splits[]")
                            for s in self.diagnostic_splits)
        if not resolved:
            raise EvaluationConfigError(
                "splits: an evaluation over no split measures nothing")
        for split in resolved:
            if split in NEVER_EVALUABLE_SPLITS:
                raise EvaluationConfigError(
                    f"splits: {split.value} is never model input")
            if split not in EVALUABLE_SPLITS:
                raise EvaluationConfigError(
                    f"splits: {split.value} is not an authoritative evaluation split; "
                    f"allowed {sorted(s.value for s in EVALUABLE_SPLITS)}. TRAIN belongs "
                    f"in diagnostic_splits, where it cannot decide eligibility")
        for split in diagnostics:
            if split not in DIAGNOSTIC_ONLY_SPLITS:
                raise EvaluationConfigError(
                    f"diagnostic_splits: {split.value} is authoritative; naming it a "
                    f"diagnostic would hide a result that counts")
        overlap = set(resolved) & set(diagnostics)
        if overlap:
            raise EvaluationConfigError(
                f"splits: {sorted(s.value for s in overlap)} appear as both "
                f"authoritative and diagnostic")
        if len(set(resolved)) != len(resolved):
            raise EvaluationConfigError("splits: duplicated entry")
        set_(self, "splits", tuple(sorted(set(resolved), key=lambda s: s.value)))
        set_(self, "diagnostic_splits",
             tuple(sorted(set(diagnostics), key=lambda s: s.value)))

    def missing_mandatory(self) -> tuple[DatasetSplit, ...]:
        return tuple(sorted(MANDATORY_SPLITS - set(self.splits), key=lambda s: s.value))

    def eligibility_blockers(self) -> tuple[str, ...]:
        return tuple(
            f"the {split.value} split is mandatory and is not selected; a missing "
            f"mandatory split is INSUFFICIENT_EVIDENCE, never a pass"
            for split in self.missing_mandatory())

    def to_dict(self) -> dict:
        return {"splits": [s.value for s in self.splits],
                "diagnostic_splits": [s.value for s in self.diagnostic_splits]}


# ══════════════════════════════════════════════════════════════════════════════
#  The config
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AdapterConfigReference:
    """Which adapter run the config names, and what it expects to find there."""

    run_id: str
    expected_manifest_hash: str = ""
    expected_plan_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_id(self.run_id,
                                                      "candidate_adapter.run_id"))
        for name in ("expected_manifest_hash", "expected_plan_hash"):
            value = str(getattr(self, name) or "").strip().lower()
            if value and (len(value) != 64
                          or any(c not in "0123456789abcdef" for c in value)):
                raise EvaluationConfigError(
                    f"candidate_adapter.{name}: expected a 64-character hex digest")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict:
        return {"run_id": self.run_id,
                "expected_manifest_hash": self.expected_manifest_hash,
                "expected_plan_hash": self.expected_plan_hash}


@dataclass(frozen=True)
class DatasetConfigReference:
    """Which dataset version the held-out material comes from."""

    dataset_id: str
    dataset_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", require_id(self.dataset_id,
                                                          "dataset.dataset_id"))
        object.__setattr__(self, "dataset_version",
                           require_id(self.dataset_version, "dataset.dataset_version"))

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "dataset_version": self.dataset_version}


@dataclass(frozen=True)
class EvaluationConfig:
    """A complete, immutable evaluation configuration."""

    evaluation_id: str
    baseline_model: ModelIdentity
    candidate_adapter: AdapterConfigReference
    dataset: DatasetConfigReference
    created_at_utc: str
    seed: int = 0
    evaluation_generation: int = 1
    splits: SplitSelection = field(default_factory=SplitSelection)
    generation: GenerationPolicy = field(default_factory=GenerationPolicy)
    policies: EvaluationPolicySet = field(default_factory=EvaluationPolicySet)
    limitations: tuple[str, ...] = ()
    notes: str = ""
    schema_version: str = SCHEMA_VERSION
    evaluation_schema_version: str = EVALUATION_SCHEMA_VERSION
    evaluator_version: str = EVALUATOR_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "evaluation_id", require_id(self.evaluation_id,
                                               "config.evaluation_id"))
        set_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                       "config.created_at_utc"))
        set_(self, "seed", require_int(self.seed, "config.seed", minimum=0,
                                       maximum=2**31 - 1))
        set_(self, "evaluation_generation",
             require_int(self.evaluation_generation, "config.evaluation_generation",
                         minimum=1, maximum=10_000))
        set_(self, "limitations", require_str_tuple(self.limitations,
                                                    "config.limitations", max_items=64))
        set_(self, "notes", require_text(self.notes, "config.notes", min_len=0,
                                         max_len=4_000))
        if not isinstance(self.baseline_model, ModelIdentity):
            raise EvaluationConfigError(
                "config.baseline_model: expected a ModelIdentity; a model named by a "
                "bare string is a name, not a revision")
        if not isinstance(self.candidate_adapter, AdapterConfigReference):
            raise EvaluationConfigError("config.candidate_adapter: expected a reference")
        if not isinstance(self.dataset, DatasetConfigReference):
            raise EvaluationConfigError("config.dataset: expected a dataset reference")
        if self.baseline_model.requires_remote_code:
            raise EvaluationConfigError(
                "config.baseline_model: remote code is refused, not configured")
        # The generation seed and the config seed must agree, or two digests describe
        # one run and the report carries whichever the code happened to read.
        if self.generation.seed != self.seed:
            raise EvaluationConfigError(
                f"config: seed {self.seed} disagrees with generation.seed "
                f"{self.generation.seed}; one run has one seed")

    # -- derived ---------------------------------------------------------------
    def eligibility_blockers(self) -> tuple[str, ...]:
        """Reasons this config could not, even on a perfect run, decide eligibility."""
        problems = list(self.splits.eligibility_blockers())
        problems.extend(self.generation.blockers())
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: self.schema_version,
            "evaluation_schema_version": self.evaluation_schema_version,
            "evaluator_version": self.evaluator_version,
            "evaluation_id": self.evaluation_id,
            "evaluation_generation": self.evaluation_generation,
            "evaluation_policy_version": self.policies.policy_version,
            "baseline_model": {
                "provider": self.baseline_model.provider,
                "model_id": self.baseline_model.model_id,
                "revision": self.baseline_model.revision,
                "parameters_b": self.baseline_model.parameters_b,
                "family": self.baseline_model.family,
                "tokenizer_id": self.baseline_model.tokenizer_id,
                "tokenizer_revision": self.baseline_model.tokenizer_revision,
                "identity_hash": self.baseline_model.identity_hash(),
                "tokenizer_identity_hash": self.baseline_model.tokenizer_identity_hash(),
            },
            "candidate_adapter": self.candidate_adapter.to_dict(),
            "dataset": self.dataset.to_dict(),
            "splits": self.splits.to_dict(),
            "generation": self.generation.to_dict(),
            "policies": self.policies.to_dict(),
            "seed": self.seed,
            "created_at_utc": self.created_at_utc,
            "limitations": list(self.limitations),
            "notes": self.notes,
        }

    def config_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "config_hash": self.config_hash(),
                  "eligibility_blockers": list(self.eligibility_blockers())}
        assert_no_private_content(record, label="evaluation config")
        return record


# ══════════════════════════════════════════════════════════════════════════════
#  Loading
# ══════════════════════════════════════════════════════════════════════════════
def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """``json`` keeps the LAST duplicate key. A config with ``"seed": 0`` followed by
    ``"seed": 7`` would load as 7 while reading as 0 — so both are refused."""
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise EvaluationConfigError(
                f"evaluation config: duplicate key {key!r}; a reader keeps one value and "
                f"a human reads the other")
        seen[key] = value
    return seen


def _forbidden(payload: dict, *, label: str) -> None:
    for name in sorted(payload):
        reason = FORBIDDEN_CONFIG_FIELDS.get(name.strip().lower())
        if reason:
            raise EvaluationConfigError(f"{label}: field {name!r} is refused — {reason}")


def config_from_dict(payload: object) -> EvaluationConfig:
    """Build a config from a mapping, refusing anything unknown or forbidden."""
    data = require_mapping(payload, "evaluation config")
    _forbidden(data, label="evaluation config")
    check_schema_version(data, label="evaluation config")
    declared = str(data.get("evaluation_schema_version",
                            EVALUATION_SCHEMA_VERSION)).strip()
    if declared.split(".")[:2] != EVALUATION_SCHEMA_VERSION.split(".")[:2]:
        raise EvaluationConfigError(
            f"evaluation config: incompatible evaluation_schema_version {declared!r} "
            f"(this build speaks {EVALUATION_SCHEMA_VERSION!r})")
    reject_unknown_fields(data, CONFIG_FIELDS, label="evaluation config")

    baseline_raw = require_mapping(data.get("baseline_model", {}), "baseline_model")
    _forbidden(baseline_raw, label="baseline_model")
    reject_unknown_fields(baseline_raw, _BASELINE_FIELDS, label="baseline_model")
    # Built with CacheStatus.UNKNOWN on purpose: whether weights are on THIS host is a
    # fact about the host, not about the configuration, and probing it here would put a
    # cache root into a document that is reviewed and shared. The reference-verification
    # step re-probes against the root the operator supplies at invocation.
    baseline = ModelIdentity(
        provider=str(baseline_raw.get("provider", "huggingface")),
        model_id=baseline_raw.get("model_id", ""),
        revision=baseline_raw.get("revision", ""),
        parameters_b=baseline_raw.get("parameters_b", 0.0),
        family=str(baseline_raw.get("family", "")),
        tokenizer_id=str(baseline_raw.get("tokenizer_id", "")),
        tokenizer_revision=str(baseline_raw.get("tokenizer_revision", "")),
        license_reference=str(baseline_raw.get("license_reference", "")),
    )

    adapter_raw = require_mapping(data.get("candidate_adapter", {}), "candidate_adapter")
    _forbidden(adapter_raw, label="candidate_adapter")
    reject_unknown_fields(adapter_raw, _ADAPTER_FIELDS, label="candidate_adapter")
    adapter = AdapterConfigReference(**adapter_raw)  # type: ignore[arg-type]

    dataset_raw = require_mapping(data.get("dataset", {}), "dataset")
    reject_unknown_fields(dataset_raw, _DATASET_FIELDS, label="dataset")
    dataset = DatasetConfigReference(**dataset_raw)  # type: ignore[arg-type]

    splits_raw = require_mapping(data.get("splits", {}), "splits") if "splits" in data \
        else {}
    reject_unknown_fields(splits_raw, ("splits", "diagnostic_splits"), label="splits")
    selection = SplitSelection(
        splits=tuple(DatasetSplit(s) for s in splits_raw.get(
            "splits", [DatasetSplit.HIDDEN_EVALUATION.value,
                       DatasetSplit.SECURITY_REGRESSION.value])),
        diagnostic_splits=tuple(DatasetSplit(s) for s in
                                splits_raw.get("diagnostic_splits", [])))

    generation = GenerationPolicy.from_dict(data["generation"]) if "generation" in data \
        else GenerationPolicy(seed=int(data.get("seed", 0)))

    policies = EvaluationPolicySet(
        graders=_policy(GraderPolicy, data.get("graders"), "graders"),
        metrics=_policy(MetricPolicy, data.get("metrics"), "metrics"),
        statistics=_policy(StatisticalPolicy, data.get("statistics"), "statistics"),
        families=_policy(TaskFamilyPolicy, data.get("families"), "families"),
        gates=_policy(GatePolicy, data.get("gates"), "gates"),
        resources=_policy(ResourceCeilings, data.get("resources"), "resources"),
    )
    return EvaluationConfig(
        evaluation_id=data["evaluation_id"],
        baseline_model=baseline,
        candidate_adapter=adapter,
        dataset=dataset,
        created_at_utc=data["created_at_utc"],
        seed=int(data.get("seed", 0)),
        evaluation_generation=int(data.get("evaluation_generation", 1)),
        splits=selection,
        generation=generation,
        policies=policies,
        limitations=tuple(data.get("limitations", ())),
        notes=str(data.get("notes", "")),
        schema_version=str(data.get(SCHEMA_KEY, SCHEMA_VERSION)),
    )


def _policy(cls, payload: object, label: str):
    """Build one policy, refusing unknown keys and the derived read-only ones."""
    if payload is None:
        return cls()
    data = require_mapping(payload, label)
    _forbidden(data, label=label)
    allowed = set(cls().to_dict()) - {"family_mandatory", "thresholds_are_calibrated",
                                      "security_gates_have_no_margin"}
    reject_unknown_fields(data, allowed, label=label)
    known = {k: v for k, v in data.items() if k != "policy_version"}
    return cls(**known)


def load_config(path: str | Path) -> EvaluationConfig:
    """Read a config from disk. Refuses a link, an oversized file and a duplicate key."""
    p = Path(path)
    if p.is_symlink():
        raise EvaluationConfigError(
            f"evaluation config: {p.name!r} is a symlink; the file that is reviewed and "
            f"the file that is loaded must be the same file")
    if not p.is_file():
        raise EvaluationConfigError(f"evaluation config: not a file ({p.name!r})")
    size = p.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise EvaluationConfigError(
            f"evaluation config: {size} bytes exceeds {MAX_CONFIG_BYTES}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"),
                             object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise EvaluationConfigError(
            f"evaluation config: {p.name!r} is not valid JSON ({exc.msg} at line "
            f"{exc.lineno})") from None
    except OSError as exc:
        raise EvaluationConfigError(
            f"evaluation config: {p.name!r} is unreadable ({exc.strerror})") from None
    return config_from_dict(payload)


__all__ = [
    "ALLOWED_EVALUATION_TRANSITIONS", "CONFIG_FIELDS", "FORBIDDEN_CONFIG_FIELDS",
    "MAX_CONFIG_BYTES", "AdapterConfigReference", "DatasetConfigReference",
    "EvaluationConfig", "EvaluationConfigError", "EvaluationRunState",
    "EvaluationTransitionError", "SplitSelection", "check_evaluation_transition",
    "config_from_dict", "load_config",
]
