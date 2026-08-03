"""training_gym/evaluation/runner.py — V69 M62 S3C: asking both arms the same question.

WHAT THE RUNNER GUARANTEES
--------------------------
For every task in the frozen pack it produces exactly two results — one baseline, one
candidate — and it proves, per task, that the two requests carried the same prompt, the
same tool schemas, the same generation policy and the same tokenizer identity. The proof
is a digest comparison, not a code review: :meth:`EvaluationRequest.parity_hash` covers
everything the model sees and deliberately excludes the role and the adapter, which are
the one thing that is supposed to differ.

ORDER EFFECTS
-------------
Always running the baseline first would make "first" and "baseline" the same variable. So
half the tasks are answered candidate-first, and which half is derived from the task hash
and the evaluation seed: deterministic, reproducible, inside the plan digest, and
independent of anything about the expected answer.

FAILURES STAY IN THE SAMPLE
---------------------------
A task whose arm timed out, errored or returned nothing is recorded with a paired status
that says so, and it stays in the denominator. There is no code path here that drops a
task because it went badly — that is the mechanism by which a model that fails every hard
task reports a perfect score on the easy ones.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..schemas import SchemaError, sha256_obj, sha256_text, short
from .backend import (
    BackendErrorCategory,
    BackendStatus,
    CancellationToken,
    CleanupStatus,
    EvaluationRequest,
    EvaluationResult,
    FinishReason,
    InterruptionRequested,
    check_result_matches_request,
    require_backend,
)
from .generation import GenerationPolicy, assert_identical_policies
from .policy import ResourceCeilings
from .references import AdapterEvaluationReference, BaseModelEvaluationReference, EvaluationRole
from .task_pack import EvaluationTask, EvaluationTaskPack

#: Bumped when the pairing or ordering rule changes.
RUNNER_VERSION = "m62.evaluation_runner.1"

#: The order policy this stage implements. Named in the plan so a report can be read
#: against the rule that produced it.
ORDER_POLICY_BALANCED = "balanced_by_task_hash_and_seed"


class RunnerError(SchemaError):
    """The comparison could not be produced as described."""


class PairedStatus(str, Enum):
    """What was actually obtained for one task. Never collapses two facts into one."""

    #: Both arms produced a response. The only status that supports a per-task delta.
    BOTH_MEASURED = "both_measured"
    BASELINE_ERROR = "baseline_error"
    CANDIDATE_ERROR = "candidate_error"
    BOTH_ERROR = "both_error"
    #: One or both arms produced something that cannot be scored — an empty answer, an
    #: oversized one, or a truncation that removed the evidence the task required.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    @property
    def comparable(self) -> bool:
        return self is PairedStatus.BOTH_MEASURED


class ExecutionOrder(str, Enum):
    BASELINE_FIRST = "baseline_first"
    CANDIDATE_FIRST = "candidate_first"


def execution_order(task: EvaluationTask, *, seed: int) -> ExecutionOrder:
    """Which arm answers first. Deterministic, and independent of the answer.

    Derived from the task hash and the seed rather than from the task's position, so
    re-ordering the pack does not re-order the arms, and from nothing that correlates
    with difficulty or with the expected outcome.
    """
    digest = sha256_text(f"{task.task_hash}:{int(seed)}")
    return (ExecutionOrder.CANDIDATE_FIRST if int(digest[:8], 16) % 2
            else ExecutionOrder.BASELINE_FIRST)


def order_assignment(pack: EvaluationTaskPack, *, seed: int) -> dict[str, str]:
    """The full, reproducible assignment. Enters the plan digest."""
    return {task.task_id: execution_order(task, seed=seed).value for task in pack.tasks}


def order_assignment_hash(pack: EvaluationTaskPack, *, seed: int) -> str:
    return sha256_obj({"policy": ORDER_POLICY_BALANCED, "seed": int(seed),
                       "assignment": dict(sorted(order_assignment(pack, seed=seed).items()))})


def order_balance(pack: EvaluationTaskPack, *, seed: int) -> tuple[int, int]:
    """``(baseline_first, candidate_first)``. Reported so imbalance is visible."""
    values = list(order_assignment(pack, seed=seed).values())
    first = values.count(ExecutionOrder.BASELINE_FIRST.value)
    return first, len(values) - first


# ══════════════════════════════════════════════════════════════════════════════
#  One task's pair of results
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PairedGeneration:
    """Both arms' raw outputs for one task, plus how they were obtained."""

    task_id: str
    task_hash: str
    status: PairedStatus
    order: ExecutionOrder
    parity_hash: str
    baseline: EvaluationResult | None = None
    candidate: EvaluationResult | None = None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return self.status.comparable and not self.blockers

    def to_dict(self) -> dict:
        return {
            "runner_version": RUNNER_VERSION,
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "status": self.status.value,
            "order": self.order.value,
            "parity_hash": self.parity_hash,
            "baseline_result_hash": self.baseline.result_hash() if self.baseline else "",
            "candidate_result_hash": self.candidate.result_hash() if self.candidate
            else "",
            "baseline_status": self.baseline.status.value if self.baseline else "absent",
            "candidate_status": self.candidate.status.value if self.candidate
            else "absent",
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PairedRun:
    """Every task's pair, and the accounting that proves none went missing."""

    generations: tuple[PairedGeneration, ...]
    order_policy: str
    order_assignment_hash: str
    baseline_backend_id: str
    candidate_backend_id: str
    backend_version: str
    cleanup_status: CleanupStatus = CleanupStatus.UNKNOWN
    interrupted: bool = False
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # -- accounting -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.generations)

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in PairedStatus}
        for pair in self.generations:
            counts[pair.status.value] += 1
        return counts

    @property
    def measured_pairs(self) -> int:
        return sum(1 for p in self.generations if p.comparable)

    @property
    def missing_pairs(self) -> int:
        """Every task that did not yield two scoreable answers. Never dropped."""
        return len(self.generations) - self.measured_pairs

    def coverage_blockers(self, pack: EvaluationTaskPack) -> tuple[str, ...]:
        """Refuse a run that did not attempt every task in the frozen pack.

        Partial coverage reported as "the result" is cherry-picking whether or not
        anybody intended it, so the count is checked rather than trusted.
        """
        seen = {p.task_id for p in self.generations}
        expected = {t.task_id for t in pack.tasks}
        problems: list[str] = []
        if seen != expected:
            missing = sorted(expected - seen)
            extra = sorted(seen - expected)
            if missing:
                problems.append(
                    f"{len(missing)} task(s) in the pack were never attempted "
                    f"({', '.join(missing[:5])}); a subset reported as the whole is "
                    f"cherry-picking")
            if extra:
                problems.append(
                    f"{len(extra)} result(s) name tasks that are not in the pack "
                    f"({', '.join(extra[:5])})")
        if not self.cleanup_status.is_safe:
            problems.append(
                f"model cleanup reported {self.cleanup_status.value}; one candidate's "
                f"adapter may still be attached to what a later run would call a "
                f"baseline, so this run is not trustworthy as a measurement")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "runner_version": RUNNER_VERSION,
            "order_policy": self.order_policy,
            "order_assignment_hash": self.order_assignment_hash,
            "baseline_backend_id": self.baseline_backend_id,
            "candidate_backend_id": self.candidate_backend_id,
            "backend_version": self.backend_version,
            "cleanup_status": self.cleanup_status.value,
            "interrupted": self.interrupted,
            "task_count": len(self.generations),
            "measured_pairs": self.measured_pairs,
            "missing_pairs": self.missing_pairs,
            "counts": self.counts(),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }

    def run_hash(self) -> str:
        return sha256_obj({**self.to_dict(),
                           "generations": [g.to_dict() for g in self.generations]})


# ══════════════════════════════════════════════════════════════════════════════
#  The runner
# ══════════════════════════════════════════════════════════════════════════════
def _synthetic_error(request: EvaluationRequest, *, backend_id: str,
                     backend_version: str, category: BackendErrorCategory,
                     message: str) -> EvaluationResult:
    """A result standing in for a generation that raised.

    An exception must not remove the task from the sample: a backend that crashes on
    every hard prompt would otherwise look like a backend that only sees easy ones.
    """
    return EvaluationResult(
        backend_id=backend_id, backend_version=backend_version, role=request.role,
        task_id=request.task.task_id, task_hash=request.task.task_hash,
        status=BackendStatus.FAILED, error_category=category,
        error_message=str(message)[:2_000], finish_reason=FinishReason.ERROR,
        cleanup_status=CleanupStatus.UNKNOWN, request_parity_hash=request.parity_hash())


def _classify(baseline: EvaluationResult, candidate: EvaluationResult,
              ceilings: ResourceCeilings) -> tuple[PairedStatus, tuple[str, ...]]:
    """What was actually obtained, and why it may not be comparable."""
    warnings: list[str] = []
    base_ok, cand_ok = baseline.ok, candidate.ok
    if not base_ok and not cand_ok:
        return PairedStatus.BOTH_ERROR, ()
    if not base_ok:
        return PairedStatus.BASELINE_ERROR, ()
    if not cand_ok:
        return PairedStatus.CANDIDATE_ERROR, ()

    insufficient = False
    for label, result in (("baseline", baseline), ("candidate", candidate)):
        if result.empty_output:
            warnings.append(f"the {label} arm returned an empty response")
            insufficient = True
        if result.oversized(ceilings):
            warnings.append(
                f"the {label} arm returned {len(result.response_text)} characters, over "
                f"the {ceilings.max_response_chars} ceiling")
            insufficient = True
        if result.input_truncated:
            warnings.append(
                f"the {label} arm's input was truncated by {result.truncated_tokens} "
                f"tokens; evidence the task required may have been removed")
            insufficient = True
    if insufficient:
        return PairedStatus.INSUFFICIENT_EVIDENCE, tuple(warnings)
    return PairedStatus.BOTH_MEASURED, tuple(warnings)


def run_paired_evaluation(
        pack: EvaluationTaskPack, *,
        baseline_backend: object,
        candidate_backend: object,
        baseline_reference: BaseModelEvaluationReference,
        adapter_reference: AdapterEvaluationReference,
        generation: GenerationPolicy,
        seed: int,
        resources: ResourceCeilings | None = None,
        adapter_directory: Path | None = None,
        model_cache_root: Path | None = None,
        cancellation: CancellationToken | None = None) -> PairedRun:
    """Answer every task with both arms, proving parity per task.

    ``baseline_backend`` and ``candidate_backend`` are separate objects on purpose: it is
    the only arrangement under which "the baseline had no adapter attached" is a
    structural fact rather than a claim about what a shared object was toggled to.
    """
    baseline_backend = require_backend(baseline_backend)
    candidate_backend = require_backend(candidate_backend)
    if baseline_backend is candidate_backend:
        raise RunnerError(
            "evaluation runner: the two arms were handed the same backend object. "
            "'the baseline had no adapter attached' would then be a claim about what "
            "that object was toggled to rather than a structural fact, and a toggle "
            "that failed to reset would measure the candidate twice")
    if not isinstance(pack, EvaluationTaskPack):
        raise RunnerError("evaluation runner: pack must be an EvaluationTaskPack")
    ceilings = resources or ResourceCeilings()
    token = cancellation or CancellationToken()
    if len(pack) > ceilings.max_tasks:
        raise RunnerError(
            f"evaluation runner: {len(pack)} tasks exceeds the ceiling "
            f"{ceilings.max_tasks}")
    # One policy object, handed to both arms. Asserting it against itself looks
    # redundant and is not: it fixes the shared digest that every result is checked
    # against, so a future caller who threads two policies through gets a refusal here
    # rather than a plausible-looking delta.
    assert_identical_policies(generation, generation)

    generations: list[PairedGeneration] = []
    warnings: list[str] = []
    interrupted = False

    for task in pack.tasks:
        order = execution_order(task, seed=seed)
        base_request = EvaluationRequest(
            role=EvaluationRole.BASELINE, task=task, generation=generation,
            baseline=baseline_reference, adapter=None, resources=ceilings,
            cancellation=token, model_cache_root=model_cache_root)
        cand_request = EvaluationRequest(
            role=EvaluationRole.CANDIDATE, task=task, generation=generation,
            baseline=baseline_reference, adapter=adapter_reference, resources=ceilings,
            cancellation=token, adapter_directory=adapter_directory,
            model_cache_root=model_cache_root)

        # The parity assertion. Both arms were built from one task, one policy and one
        # baseline reference, so this must hold — and it is checked rather than assumed,
        # because "must hold" is what every silently-broken comparison also said.
        parity = base_request.parity_hash()
        if parity != cand_request.parity_hash():
            raise RunnerError(
                f"evaluation runner: task {task.task_id!r} would be asked differently of "
                f"the two arms ({short(parity)} vs "
                f"{short(cand_request.parity_hash())}); a comparison between two "
                f"different questions measures nothing")

        try:
            if order is ExecutionOrder.BASELINE_FIRST:
                base_result = _invoke(baseline_backend, base_request)
                cand_result = _invoke(candidate_backend, cand_request)
            else:
                cand_result = _invoke(candidate_backend, cand_request)
                base_result = _invoke(baseline_backend, base_request)
        except InterruptionRequested as exc:
            interrupted = True
            warnings.append(f"interrupted during {task.task_id!r}: {exc}")
            break

        blockers: list[str] = []
        for result, request in ((base_result, base_request),
                                (cand_result, cand_request)):
            try:
                check_result_matches_request(result, request)
            except Exception as exc:  # noqa: BLE001 — the message IS the finding
                blockers.append(str(exc))

        status, pair_warnings = _classify(base_result, cand_result, ceilings)
        if blockers:
            status = PairedStatus.INSUFFICIENT_EVIDENCE
        generations.append(PairedGeneration(
            task_id=task.task_id, task_hash=task.task_hash, status=status, order=order,
            parity_hash=parity, baseline=base_result, candidate=cand_result,
            blockers=tuple(blockers), warnings=pair_warnings))

    cleanup = _release_both(baseline_backend, candidate_backend)
    balance = order_balance(pack, seed=seed)
    warnings.append(f"execution order: {balance[0]} baseline-first, "
                    f"{balance[1]} candidate-first")
    run = PairedRun(
        generations=tuple(generations), order_policy=ORDER_POLICY_BALANCED,
        order_assignment_hash=order_assignment_hash(pack, seed=seed),
        baseline_backend_id=str(getattr(baseline_backend, "backend_id", "")),
        candidate_backend_id=str(getattr(candidate_backend, "backend_id", "")),
        backend_version=str(baseline_backend.version()), cleanup_status=cleanup,
        interrupted=interrupted, warnings=tuple(warnings))
    return PairedRun(**{**run.__dict__,
                        "blockers": run.coverage_blockers(pack) if not interrupted
                        else (("the run was interrupted; a partial comparison is not a "
                               "measurement of the adapter",)
                              + run.coverage_blockers(pack))})


def _invoke(backend: object, request: EvaluationRequest) -> EvaluationResult:
    """Call a backend, turning any crash into a recorded failure for THIS task."""
    try:
        result = backend.generate(request)  # type: ignore[attr-defined]
    except InterruptionRequested:
        raise
    except Exception as exc:  # noqa: BLE001 — a crash is a datum, not a disappearance
        return _synthetic_error(
            request, backend_id=str(getattr(backend, "backend_id", "unknown")),
            backend_version=str(getattr(backend, "version", lambda: "unknown")()),
            category=BackendErrorCategory.BACKEND,
            message=f"{type(exc).__name__}: {exc}")
    if not isinstance(result, EvaluationResult):
        return _synthetic_error(
            request, backend_id=str(getattr(backend, "backend_id", "unknown")),
            backend_version="unknown", category=BackendErrorCategory.BACKEND,
            message=f"the backend returned {type(result).__name__}, not an "
                    f"EvaluationResult; a raw dictionary is never authoritative")
    return result


def _release_both(*backends: object) -> CleanupStatus:
    """The worst status wins. A cleanup that half-worked did not work."""
    statuses: list[CleanupStatus] = []
    for backend in backends:
        try:
            statuses.append(CleanupStatus(backend.release()))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a cleanup that raised proved nothing
            statuses.append(CleanupStatus.UNKNOWN)
    if any(s is CleanupStatus.FAILED for s in statuses):
        return CleanupStatus.FAILED
    if any(s is CleanupStatus.UNKNOWN for s in statuses):
        return CleanupStatus.UNKNOWN
    if all(s is CleanupStatus.NOT_REQUIRED for s in statuses):
        return CleanupStatus.NOT_REQUIRED
    return CleanupStatus.RELEASED


def results_by_role(run: PairedRun, role: EvaluationRole
                    ) -> tuple[EvaluationResult, ...]:
    """Every result for one arm, in pack order. Includes failures."""
    out: list[EvaluationResult] = []
    for pair in run.generations:
        result = pair.baseline if role is EvaluationRole.BASELINE else pair.candidate
        if result is not None:
            out.append(result)
    return tuple(out)


def result_records(results: Sequence[EvaluationResult]) -> tuple[dict, ...]:
    """One line per result, for the per-arm ``*-results.jsonl`` artefacts."""
    return tuple(r.to_record() for r in results)


__all__ = [
    "ORDER_POLICY_BALANCED", "RUNNER_VERSION", "ExecutionOrder", "PairedGeneration",
    "PairedRun", "PairedStatus", "RunnerError", "execution_order", "order_assignment",
    "order_assignment_hash", "order_balance", "result_records", "results_by_role",
    "run_paired_evaluation",
]
