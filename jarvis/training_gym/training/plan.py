"""training_gym/training/plan.py — V69 M62 S3A: the plan, its digest, and its token.

WHAT A PLAN IS
--------------
A complete, immutable description of what a training run *would* do, digested so that a
later stage can prove nothing changed between approval and execution. It is a direct
transliteration of :class:`training_gym.datasets.promotion_plan.PromotionPlan`, which
already solved this problem for datasets, down to the confirmation semantics.

The digest is not a signature over an intention. It is a signature over a state of the
world: the config, the dataset digests, the model revision, the dependency evidence and
the hardware evidence are all inside it, so a plan approved on Tuesday cannot execute on
Wednesday against a dataset that was edited on Wednesday morning.

WHAT S3A DECLARES ABOUT ITSELF
------------------------------
``performs_training``, ``downloads_model``, ``installs_dependencies``,
``contacts_network`` and ``creates_adapter`` are all ``false`` — not as a promise, but as
a description of a stage that contains no code capable of any of them. The output root
is never published: the plan carries a digest of it, so two roots produce two plans and
neither plan carries the operator's home directory into a ticket.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path

from ..schemas import (
    SchemaError,
    assert_no_private_content,
    require_id,
    sha256_obj,
    sha256_text,
    short,
)
from .config import (
    PLANNER_VERSION,
    ModelDownloadPolicy,
    TrainingConfig,
    TrainingRunState,
)

#: Bumped when the plan's shape changes, invalidating every outstanding confirmation.
PLAN_SCHEMA_VERSION = "m62.training_plan.1"

#: The one legal confirmation shape. Exact case, full digest, no separator variants.
CONFIRMATION_PREFIX = "TRAIN:"

#: The append-only ledger a future execution stage would consult and extend.
TRAINING_LEDGER_FILE = "training_runs.jsonl"
MAX_TRAINING_LEDGER_LINES = 100_000

#: The files an execution stage would create, relative to the run directory.
EXPECTED_ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors",
                          "training_args.json", "run.json", "training_log.jsonl")


class TrainingPlanError(SchemaError):
    """A training plan that will not be produced or acted on as described."""


class ConfirmationRejected(TrainingPlanError):
    """A confirmation token that authorises this plan was not supplied."""


class ExecutionNotImplemented(TrainingPlanError):
    """S3A refuses execution because no execution backend exists to refuse *from*."""


def output_root_id(root: str | Path) -> str:
    """A digest of the output root. Identifies it without publishing it.

    The plan is a document an operator pastes into a ticket. The absolute path of the
    training tree is inside their home directory and has no business travelling with it —
    but two different roots must still produce two different plans.
    """
    return sha256_text(Path(root).resolve().as_posix())


def training_run_directory(root: str | Path, run_id: str) -> Path:
    """``<root>/runs/<run_id>``, with the id validated BEFORE the join.

    A check applied to the result has already built the path. ``require_id`` refuses
    separators, traversal, leading dots, trailing dots and Windows device names on every
    platform, so this join cannot escape and cannot open the console.
    """
    return Path(root) / "runs" / require_id(run_id, "run_id")


def check_output_root(root: str | Path, run_id: str) -> tuple[str, ...]:
    """Problems with the destination. Creates nothing, and never returns a path.

    An existing run directory is a refusal rather than a resume: two runs under one id
    produce one set of artifacts and no way to tell which run made them.
    """
    problems: list[str] = []
    try:
        base = Path(root)
        run_dir = training_run_directory(root, run_id)
    except SchemaError as exc:
        return (str(exc),)
    if base.exists():
        if base.is_symlink():
            problems.append("the output root is a symlink; the directory that is "
                            "reviewed and the directory that is written must be the same")
        elif not base.is_dir():
            problems.append("the output root exists and is not a directory")
    if run_dir.is_symlink():
        problems.append(f"the run directory for {run_id!r} is a symlink")
    elif run_dir.exists():
        problems.append(
            f"a run directory for {run_id!r} already exists; a run id can never silently "
            f"overwrite another, so choose a new one")
    return tuple(problems)


@dataclass(frozen=True)
class TrainingPlan:
    """Exactly what an execution would do, and the digest that would authorise it."""

    run_id: str
    method: str
    training_config_hash: str
    base_model_identity_hash: str
    tokenizer_identity_hash: str
    base_model_revision_kind: str
    dataset_reference_hash: str
    dataset_manifest_hash: str
    training_split_hash: str
    validation_split_hash: str
    hidden_evaluation_hash: str
    security_regression_hash: str
    dataset_verification_hash: str
    dependency_report_hash: str
    hardware_report_hash: str
    feasibility_report_hash: str
    selected_device: str
    selected_precision: str
    hyperparameters: dict
    lora: dict
    resource_policy_version: str
    resource_policy_hash: str
    model_download_required: bool
    model_download_policy: str
    expected_output_root_id: str
    expected_files: tuple[str, ...]
    expected_state_transitions: tuple[str, ...]
    expected_package_profile: str
    created_at_utc: str
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    feasibility_verdict: str = ""
    planner_version: str = PLANNER_VERSION
    plan_schema_version: str = PLAN_SCHEMA_VERSION

    # ── the honest self-description S3A can make ────────────────────────────
    performs_training: bool = False
    downloads_model: bool = False
    installs_dependencies: bool = False
    contacts_network: bool = False
    creates_adapter: bool = False

    def __post_init__(self) -> None:
        for flag in ("performs_training", "downloads_model", "installs_dependencies",
                     "contacts_network", "creates_adapter"):
            if getattr(self, flag) is not False:
                raise TrainingPlanError(
                    f"training plan: {flag} may not be true in a planning-only stage; "
                    f"there is no code in S3A that could make it true, so a plan "
                    f"claiming it would be describing something that did not happen")

    @property
    def is_executable(self) -> bool:
        """Whether a future stage could act on this. Never whether this stage did."""
        return not self.blockers

    def expected_effects(self) -> dict:
        """The complete list of effects, in the vocabulary an operator can check."""
        return {
            "trains_a_model": self.performs_training,
            "downloads_anything": self.downloads_model,
            "installs_dependencies": self.installs_dependencies,
            "contacts_the_network": self.contacts_network,
            "creates_an_adapter": self.creates_adapter,
            "creates_files": [],
            "would_create_files_on_execution": list(self.expected_files),
            "would_append_ledger": TRAINING_LEDGER_FILE,
            "would_require_model_download": self.model_download_required,
            "adapter_digest_algorithm": "sha256 over the validated (name, digest, "
                                        "size) list; a link inside an adapter "
                                        "directory is a refusal rather than an "
                                        "omission, and the manifest is excluded "
                                        "because a digest cannot cover itself",
            # S3B executes: a real adapter has been trained from a plan like this one.
            # The claim this key still has to make is about authority, not absence --
            # nothing here runs until an operator spends the matching TRAIN token.
            "execution_stage": "s3b_implemented_requires_confirmation",
        }

    def to_dict(self) -> dict:
        """The canonical plan. Excludes ``plan_hash`` — that is computed over it."""
        return {
            "plan_schema_version": self.plan_schema_version,
            "planner_version": self.planner_version,
            "run_id": self.run_id,
            "method": self.method,
            "training_config_hash": self.training_config_hash,
            "base_model_identity_hash": self.base_model_identity_hash,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "base_model_revision_kind": self.base_model_revision_kind,
            "dataset_reference_hash": self.dataset_reference_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "training_split_hash": self.training_split_hash,
            "validation_split_hash": self.validation_split_hash,
            "hidden_evaluation_hash": self.hidden_evaluation_hash,
            "security_regression_hash": self.security_regression_hash,
            "dataset_verification_hash": self.dataset_verification_hash,
            "dependency_report_hash": self.dependency_report_hash,
            "hardware_report_hash": self.hardware_report_hash,
            "feasibility_report_hash": self.feasibility_report_hash,
            "feasibility_verdict": self.feasibility_verdict,
            "selected_device": self.selected_device,
            "selected_precision": self.selected_precision,
            "hyperparameters": dict(sorted(self.hyperparameters.items())),
            "lora": dict(sorted(self.lora.items())),
            "resource_policy_version": self.resource_policy_version,
            "resource_policy_hash": self.resource_policy_hash,
            "model_download_required": self.model_download_required,
            "model_download_policy": self.model_download_policy,
            "expected_output_root_id": self.expected_output_root_id,
            "expected_files": list(self.expected_files),
            "expected_state_transitions": list(self.expected_state_transitions),
            "expected_package_profile": self.expected_package_profile,
            "created_at_utc": self.created_at_utc,
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "performs_training": self.performs_training,
            "downloads_model": self.downloads_model,
            "installs_dependencies": self.installs_dependencies,
            "contacts_network": self.contacts_network,
            "creates_adapter": self.creates_adapter,
        }

    def plan_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def confirmation_token(self) -> str:
        """The exact string that would authorise this plan and no other."""
        return f"{CONFIRMATION_PREFIX}{self.plan_hash()}"

    def to_record(self) -> dict:
        """The plan plus its digest, its effects and the token that would authorise it."""
        record = {**self.to_dict(), "plan_hash": self.plan_hash(),
                  "is_executable": self.is_executable,
                  "expected_effects": self.expected_effects(),
                  "confirmation_required": self.confirmation_token()}
        assert_no_private_content(record, label="training plan")
        return record


def check_training_confirmation(confirmation: object, plan: TrainingPlan) -> str:
    """Refuse anything that is not the exact token for exactly this plan.

    A ``bool`` is refused by type rather than stringified: ``confirm=True`` is the shape
    of every accidental automation, and turning it into ``"True"`` and then failing the
    comparison would work by luck rather than by rule. A value that looks like a file
    reference is refused too — a confirmation read out of a persisted config, or out of
    an environment variable a wrapper script set, is a confirmation nobody typed for this
    plan, which is the one property the token exists to have.
    """
    if isinstance(confirmation, bool) or not isinstance(confirmation, str):
        raise ConfirmationRejected(
            f"training: confirmation must be the string "
            f"{CONFIRMATION_PREFIX}<plan-hash>, got {type(confirmation).__name__}; a "
            f"boolean authorises every plan equally and therefore authorises none")
    token = confirmation.strip()
    if token.startswith("@") or "/" in token or "\\" in token:
        raise ConfirmationRejected(
            "training: a confirmation that names a file is refused; the token must be "
            "supplied directly, because one read from persisted config is one nobody "
            "typed for this plan")
    if not token.startswith(CONFIRMATION_PREFIX):
        raise ConfirmationRejected(
            f"training: confirmation must start with {CONFIRMATION_PREFIX!r}; "
            f"{short(token)!r} authorises nothing in particular")
    digest = token[len(CONFIRMATION_PREFIX):]
    if len(digest) != 64:
        raise ConfirmationRejected(
            f"training: confirmation carries a {len(digest)}-character digest; the full "
            f"64 are required, because a prefix is a claim about a hash and not the hash")
    if not hmac.compare_digest(token, plan.confirmation_token()):
        raise ConfirmationRejected(
            f"training: this confirmation authorises plan {short(digest)}, but the plan "
            f"recomputed from the current inputs is {short(plan.plan_hash())}. Something "
            f"changed since it was issued — the config, the dataset, the model revision, "
            f"the installed packages or the measured hardware — and the operator "
            f"approved the earlier state")
    return token


def refuse_execution(plan: TrainingPlan) -> str:
    """The message S3A returns for every execution request. There is no other path.

    Deliberately a function that always raises rather than a flag that is checked: there
    is no branch anywhere in this stage that leads to a trainer, so there is no branch to
    get wrong.
    """
    raise ExecutionNotImplemented(
        f"training: execution is not implemented in this stage. The plan "
        f"{short(plan.plan_hash())} is complete and its confirmation token is valid "
        f"syntax, but M62 S3A contains no trainer, no optimizer and no adapter writer — "
        f"the execution backend ships in S3B. Nothing was started, nothing was "
        f"downloaded and no output was created.")


def plan_state_sequence(*, awaiting_confirmation: bool) -> tuple[str, ...]:
    """The states this planning run passed through. Never RUNNING and never beyond."""
    states = [TrainingRunState.CREATED, TrainingRunState.CONFIG_VALIDATED,
              TrainingRunState.DATASET_VERIFIED,
              TrainingRunState.DEPENDENCIES_VERIFIED,
              TrainingRunState.HARDWARE_VERIFIED, TrainingRunState.PLANNED]
    if awaiting_confirmation:
        states.append(TrainingRunState.AWAITING_CONFIRMATION)
    return tuple(s.value for s in states)


def download_required(*, cache_status: str, config: TrainingConfig) -> bool:
    """Would a future execution need to fetch weights?

    ``UNKNOWN`` counts as "yes, it might": a planner that reports "no download needed"
    from a cache it could not read has told the operator the one thing they cannot
    verify. The answer never causes a download here under any policy.
    """
    del config  # policy affects authorisation, not the physical need for the bytes
    return cache_status != "present"


def download_authorization_note(policy: ModelDownloadPolicy, *, required: bool,
                                flag_supplied: bool) -> str:
    """What a future execution would need in order to be allowed to fetch weights."""
    if not required:
        return "the weights appear to be cached locally; no download would be needed"
    if policy is ModelDownloadPolicy.DENY:
        return ("weights are not known to be cached and the config's download policy is "
                "deny; a future execution would refuse rather than fetch them")
    if flag_supplied:
        return ("weights are not known to be cached; a future execution would require "
                "--allow-model-download, which was supplied — no download happened here, "
                "because this stage has no code that could perform one")
    return ("weights are not known to be cached; a future execution would require "
            "--allow-model-download to be supplied explicitly")


__all__ = [
    "CONFIRMATION_PREFIX", "EXPECTED_ADAPTER_FILES", "MAX_TRAINING_LEDGER_LINES",
    "PLAN_SCHEMA_VERSION", "TRAINING_LEDGER_FILE", "ConfirmationRejected",
    "ExecutionNotImplemented", "TrainingPlan", "TrainingPlanError",
    "check_output_root", "check_training_confirmation", "download_authorization_note",
    "download_required", "output_root_id", "plan_state_sequence", "refuse_execution",
    "training_run_directory",
]
