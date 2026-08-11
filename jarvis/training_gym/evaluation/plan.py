"""training_gym/evaluation/plan.py — V69 M62 S3C: the evaluation plan and its token.

WHAT THE DIGEST COVERS
----------------------
Not an intention — a state of the world. The base model revision, the adapter's verified
manifest digest, the exact task pack, the hidden-target store, every split manifest, the
generation policy, all five bound policies, the dependency evidence and the hardware
evidence are inside :meth:`EvaluationPlan.plan_hash`. A plan approved against one adapter
cannot execute against another, and a plan approved on Tuesday cannot execute on Wednesday
against a dataset that was rebuilt on Wednesday morning.

``EVAL:<full-plan-hash>`` is the only string that authorises a future live evaluation. It
is deliberately a different prefix from S3A's ``TRAIN:`` so that a training confirmation
can never be pasted into an evaluation and vice versa: the two authorise different kinds
of compute against different artefacts, and a shared prefix would make them substitutable.

WHAT THIS STAGE DECLARES ABOUT ITSELF
-------------------------------------
``contacts_network``, ``downloads_model``, ``installs_dependencies``, ``promotes_model``
and ``activates_model`` are ``false`` — not as a promise, but as a description of a
subsystem that contains no code capable of any of them. ``performs_inference`` is the one
flag that may legitimately be true, and only for a plan that a future live evaluation
would act on; every plan produced in this milestone's tests has it false.
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
    short,
)
from ..training.plan import output_root_id
from . import EVALUATION_SCHEMA_VERSION, EVALUATOR_VERSION
from .config import EvaluationRunState

#: Bumped when the plan's shape changes, invalidating every outstanding confirmation.
EVALUATION_PLAN_SCHEMA_VERSION = "m62.evaluation_plan.1"

#: The one legal confirmation shape. Exact case, full digest, no separator variants, and
#: deliberately distinct from S3A's ``TRAIN:``.
CONFIRMATION_PREFIX = "EVAL:"

#: The append-only ledger a live evaluation would consult and extend.
EVALUATION_LEDGER_FILE = "evaluation_runs.jsonl"
MAX_EVALUATION_LEDGER_LINES = 100_000

#: The files a completed evaluation writes, relative to its run directory.
EXPECTED_EVALUATION_FILES: tuple[str, ...] = (
    "evaluation-plan.json", "task-pack.jsonl", "task-pack-manifest.json",
    "baseline-results.jsonl", "candidate-results.jsonl", "paired-comparisons.jsonl",
    "metrics.json", "evaluation-report.json", "evaluation-manifest.json",
    # V69 M62 S3F.2: body-free per-arm review evidence. PERMITTED here, not defaulted —
    # this tuple is the set a plan may declare, and no plan's ``expected_files`` is
    # derived from it, so widening it moves no existing plan hash.
    "baseline-scores.jsonl", "candidate-scores.jsonl",
)


class EvaluationPlanError(SchemaError):
    """A plan that will not be produced or acted on as described."""


class EvaluationConfirmationRejected(EvaluationPlanError):
    """A confirmation token that authorises this exact plan was not supplied."""


def evaluation_run_directory(root: str | Path, evaluation_id: str,
                             generation: int) -> Path:
    """``<root>/evaluations/<evaluation_id>/gen-<n>``, id validated BEFORE the join.

    The generation is part of the path because a re-run is a new generation, never an
    overwrite: two evaluations under one directory produce one set of artefacts and no
    way to tell which run made them.
    """
    safe_id = require_id(evaluation_id, "evaluation_id")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise EvaluationPlanError(
            f"evaluation generation: expected a positive integer, got {generation!r}")
    return Path(root) / "evaluations" / safe_id / f"gen-{generation}"


def check_output_root(root: str | Path, evaluation_id: str,
                      generation: int) -> tuple[str, ...]:
    """Problems with the destination. Creates nothing, and never returns a path."""
    problems: list[str] = []
    try:
        base = Path(root)
        run_dir = evaluation_run_directory(root, evaluation_id, generation)
    except SchemaError as exc:
        return (str(exc),)
    if base.exists():
        if base.is_symlink():
            problems.append("the output root is a symlink; the directory that is "
                            "reviewed and the directory that is written must be the same")
        elif not base.is_dir():
            problems.append("the output root exists and is not a directory")
    if run_dir.is_symlink():
        problems.append(
            f"the run directory for {evaluation_id!r} generation {generation} is a symlink")
    elif run_dir.exists():
        problems.append(
            f"generation {generation} of evaluation {evaluation_id!r} already exists; a "
            f"completed generation is never overwritten, so raise the generation")
    return tuple(problems)


@dataclass(frozen=True)
class EvaluationPlan:
    """Exactly what an evaluation would do, and the digest that would authorise it."""

    evaluation_id: str
    generation: int
    evaluation_config_hash: str
    baseline_reference_hash: str
    candidate_adapter_reference_hash: str
    tokenizer_identity_hash: str
    task_pack_hash: str
    hidden_target_store_hash: str
    validation_manifest_hash: str
    hidden_evaluation_manifest_hash: str
    security_regression_manifest_hash: str
    adversarial_manifest_hash: str
    dataset_manifest_hash: str
    generation_policy_hash: str
    grader_policy_hash: str
    metric_policy_hash: str
    statistical_policy_hash: str
    gate_policy_hash: str
    family_policy_hash: str
    resource_policy_hash: str
    dependency_report_hash: str
    hardware_report_hash: str
    order_policy: str
    order_assignment_hash: str
    expected_output_root_id: str
    expected_task_count: int
    expected_baseline_generations: int
    expected_candidate_generations: int
    expected_grader_executions: int
    expected_files: tuple[str, ...]
    expected_state_transitions: tuple[str, ...]
    backend_id: str
    created_at_utc: str
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    evaluator_version: str = EVALUATOR_VERSION
    plan_schema_version: str = EVALUATION_PLAN_SCHEMA_VERSION
    evaluation_schema_version: str = EVALUATION_SCHEMA_VERSION

    # ── the honest self-description ─────────────────────────────────────────
    #: True only for a plan a future LIVE evaluation would act on. Every plan this
    #: milestone produces in a test has it false, because the fake backend performs no
    #: inference and a plan claiming otherwise would describe something that did not
    #: happen.
    performs_inference: bool = False
    contacts_network: bool = False
    downloads_model: bool = False
    installs_dependencies: bool = False
    executes_tool_calls: bool = False
    promotes_model: bool = False
    activates_model: bool = False
    mutates_model_registry: bool = False

    #: The flags that are false in every plan this subsystem can produce, because no
    #: code path exists that could set them.
    _IMPOSSIBLE = ("contacts_network", "downloads_model", "installs_dependencies",
                   "executes_tool_calls", "promotes_model", "activates_model",
                   "mutates_model_registry")

    def __post_init__(self) -> None:
        object.__setattr__(self, "evaluation_id",
                           require_id(self.evaluation_id, "plan.evaluation_id"))
        for flag in self._IMPOSSIBLE:
            if getattr(self, flag) is not False:
                raise EvaluationPlanError(
                    f"evaluation plan: {flag} may not be true. There is no code in this "
                    f"subsystem that could make it true, so a plan claiming it would be "
                    f"describing something that cannot happen")
        if self.expected_task_count < 1:
            raise EvaluationPlanError(
                "evaluation plan: an evaluation over zero tasks produces a perfect score "
                "over an empty denominator")
        if self.expected_baseline_generations != self.expected_candidate_generations:
            raise EvaluationPlanError(
                f"evaluation plan: {self.expected_baseline_generations} baseline "
                f"generations against {self.expected_candidate_generations} candidate "
                f"generations is not a paired comparison; one arm would be measured on "
                f"tasks the other never saw")
        if self.expected_baseline_generations != self.expected_task_count:
            raise EvaluationPlanError(
                f"evaluation plan: {self.expected_task_count} tasks but "
                f"{self.expected_baseline_generations} baseline generations; every task "
                f"is answered exactly once per arm, and a count that disagrees is how a "
                f"subset gets reported as the whole")
        unexpected = sorted(set(self.expected_files) - set(EXPECTED_EVALUATION_FILES))
        if unexpected:
            raise EvaluationPlanError(
                f"evaluation plan: expected_files names {unexpected}, which is not in "
                f"the allowlist of artefacts this subsystem writes")

    # -- derived ---------------------------------------------------------------
    @property
    def is_executable(self) -> bool:
        """Whether a future stage could act on this. Never whether this stage did."""
        return not self.blockers

    def expected_effects(self) -> dict:
        """The complete effect list, in the vocabulary an operator can check."""
        return {
            "runs_a_model": self.performs_inference,
            "contacts_the_network": self.contacts_network,
            "downloads_anything": self.downloads_model,
            "installs_dependencies": self.installs_dependencies,
            "executes_model_proposed_tool_calls": self.executes_tool_calls,
            "promotes_a_model": self.promotes_model,
            "activates_a_model": self.activates_model,
            "writes_the_model_registry": self.mutates_model_registry,
            "creates_files": [],
            "would_create_files_on_execution": list(self.expected_files),
            "would_append_ledger": EVALUATION_LEDGER_FILE,
            "strongest_possible_verdict": "eligible_for_human_review",
        }

    def to_dict(self) -> dict:
        """The canonical plan. Excludes ``plan_hash`` — that is computed over it."""
        payload = {
            "plan_schema_version": self.plan_schema_version,
            "evaluation_schema_version": self.evaluation_schema_version,
            "evaluator_version": self.evaluator_version,
            "evaluation_id": self.evaluation_id,
            "generation": self.generation,
            "evaluation_config_hash": self.evaluation_config_hash,
            "baseline_reference_hash": self.baseline_reference_hash,
            "candidate_adapter_reference_hash": self.candidate_adapter_reference_hash,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "task_pack_hash": self.task_pack_hash,
            "hidden_target_store_hash": self.hidden_target_store_hash,
            "validation_manifest_hash": self.validation_manifest_hash,
            "hidden_evaluation_manifest_hash": self.hidden_evaluation_manifest_hash,
            "security_regression_manifest_hash": self.security_regression_manifest_hash,
            "adversarial_manifest_hash": self.adversarial_manifest_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "generation_policy_hash": self.generation_policy_hash,
            "grader_policy_hash": self.grader_policy_hash,
            "metric_policy_hash": self.metric_policy_hash,
            "statistical_policy_hash": self.statistical_policy_hash,
            "gate_policy_hash": self.gate_policy_hash,
            "family_policy_hash": self.family_policy_hash,
            "resource_policy_hash": self.resource_policy_hash,
            "dependency_report_hash": self.dependency_report_hash,
            "hardware_report_hash": self.hardware_report_hash,
            "order_policy": self.order_policy,
            "order_assignment_hash": self.order_assignment_hash,
            "expected_output_root_id": self.expected_output_root_id,
            "expected_task_count": self.expected_task_count,
            "expected_baseline_generations": self.expected_baseline_generations,
            "expected_candidate_generations": self.expected_candidate_generations,
            "expected_grader_executions": self.expected_grader_executions,
            "expected_files": list(self.expected_files),
            "expected_state_transitions": list(self.expected_state_transitions),
            "backend_id": self.backend_id,
            "created_at_utc": self.created_at_utc,
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "performs_inference": self.performs_inference,
        }
        payload.update({flag: getattr(self, flag) for flag in self._IMPOSSIBLE})
        return dict(sorted(payload.items()))

    def plan_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def confirmation_token(self) -> str:
        """The exact string that would authorise this plan and no other."""
        return f"{CONFIRMATION_PREFIX}{self.plan_hash()}"

    def to_record(self) -> dict:
        record = {**self.to_dict(), "plan_hash": self.plan_hash(),
                  "is_executable": self.is_executable,
                  "expected_effects": self.expected_effects(),
                  "confirmation_required": self.confirmation_token()}
        assert_no_private_content(record, label="evaluation plan")
        return record


def check_evaluation_confirmation(confirmation: object, plan: EvaluationPlan) -> str:
    """Refuse anything that is not the exact token for exactly this plan.

    A ``bool`` is refused by type rather than stringified: ``confirm=True`` is the shape
    of every accidental automation, and a boolean authorises every plan equally and
    therefore authorises none. A value that looks like a file reference is refused too — a
    confirmation read out of a persisted config or an environment variable a wrapper
    script set is a confirmation nobody typed for this plan, which is the one property
    the token exists to have.

    A ``TRAIN:`` token is refused with its own message rather than a generic prefix error,
    because pasting a training confirmation into an evaluation is a plausible mistake and
    the operator deserves to be told which one they used.
    """
    if isinstance(confirmation, bool) or not isinstance(confirmation, str):
        raise EvaluationConfirmationRejected(
            f"evaluation: confirmation must be the string "
            f"{CONFIRMATION_PREFIX}<plan-hash>, got {type(confirmation).__name__}; a "
            f"boolean authorises every plan equally and therefore authorises none")
    token = confirmation.strip()
    if token.startswith("@") or "/" in token or "\\" in token:
        raise EvaluationConfirmationRejected(
            "evaluation: a confirmation that names a file is refused; the token must be "
            "supplied directly, because one read from persisted config or an environment "
            "variable is one nobody typed for this plan")
    if token.startswith("TRAIN:"):
        raise EvaluationConfirmationRejected(
            "evaluation: that is a TRAIN confirmation. It authorises spending training "
            "compute against an adapter plan, not evaluating an adapter that already "
            f"exists; this stage requires {CONFIRMATION_PREFIX}<plan-hash>")
    if not token.startswith(CONFIRMATION_PREFIX):
        raise EvaluationConfirmationRejected(
            f"evaluation: confirmation must start with {CONFIRMATION_PREFIX!r}; "
            f"{short(token)!r} authorises nothing in particular")
    digest = token[len(CONFIRMATION_PREFIX):]
    if len(digest) != 64:
        raise EvaluationConfirmationRejected(
            f"evaluation: confirmation carries a {len(digest)}-character digest; the full "
            f"64 are required, because a prefix is a claim about a hash and not the hash")
    if not hmac.compare_digest(token, plan.confirmation_token()):
        raise EvaluationConfirmationRejected(
            f"evaluation: this confirmation authorises plan {short(digest)}, but the plan "
            f"recomputed from the current inputs is {short(plan.plan_hash())}. Something "
            f"changed since it was issued — the adapter, the baseline, the task pack, the "
            f"generation policy, the graders, the dataset or the measured hardware — and "
            f"the operator approved the earlier state")
    return token


def plan_state_sequence(*, awaiting_confirmation: bool) -> tuple[str, ...]:
    """The states a planning run passes through. Never RUNNING and never beyond."""
    states = [EvaluationRunState.CREATED, EvaluationRunState.CONFIG_VALIDATED,
              EvaluationRunState.REFERENCES_VERIFIED,
              EvaluationRunState.TASK_PACK_VERIFIED,
              EvaluationRunState.DEPENDENCIES_VERIFIED,
              EvaluationRunState.HARDWARE_VERIFIED, EvaluationRunState.PLANNED]
    if awaiting_confirmation:
        states.append(EvaluationRunState.AWAITING_CONFIRMATION)
    return tuple(s.value for s in states)


def execution_state_sequence() -> tuple[str, ...]:
    """The states a completed live evaluation would pass through, in order."""
    return tuple(s.value for s in (
        EvaluationRunState.PREFLIGHT_VERIFIED, EvaluationRunState.STARTING,
        EvaluationRunState.RUNNING_BASELINE, EvaluationRunState.RUNNING_CANDIDATE,
        EvaluationRunState.SCORING, EvaluationRunState.COMPARING,
        EvaluationRunState.ARTIFACT_VALIDATION, EvaluationRunState.COMPLETED))


__all__ = [
    "CONFIRMATION_PREFIX", "EVALUATION_LEDGER_FILE",
    "EVALUATION_PLAN_SCHEMA_VERSION", "EXPECTED_EVALUATION_FILES",
    "MAX_EVALUATION_LEDGER_LINES", "EvaluationConfirmationRejected",
    "EvaluationPlan", "EvaluationPlanError", "check_evaluation_confirmation",
    "check_output_root", "evaluation_run_directory", "execution_state_sequence",
    "output_root_id", "plan_state_sequence",
]
