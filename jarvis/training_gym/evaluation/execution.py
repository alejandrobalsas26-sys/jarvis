"""training_gym/evaluation/execution.py — V69 M62 S3E.1: the wired execution path.

WHAT THIS REPLACES
------------------
``scripts/evaluate_adapter.py --execute`` verified every precondition it could and then
returned a hard-coded refusal, because no execution path existed. Everything it would
have needed already did: a runner, graders, a comparison, gates, a report, artifact
writers, a ledger and a state machine. None of them had a caller.

This is that caller, and deliberately nothing more. It invents no scoring, writes no
artifact format of its own and reaches no verdict — every one of those belongs to a
module that already owns it. What it adds is *order*, and the guarantees that come from
order: a generation directory created before a plan is spent, a plan spent before a model
is loaded, and a report written only after the artefacts it describes verified on reload.

THE FOUR EVENTS, WHICH ARE NOT SYNONYMS — V69 M62 S3Q.0
-------------------------------------------------------
    PLAN_CONSUMED                     one approval began one run
    HOLDOUT_MODEL_FACING_COMMITTED    a held-out task is about to reach a model
    EVALUATION_COMPLETED              artefacts exist and re-verify from disk
    TERMINAL_LEDGER_RECORDED          how it ended is durable

Each is recorded separately on :class:`ExecutionOutcome` because each fails separately.
A run can spend its plan and read nothing; it can spend the holdout and crash; it can
measure perfectly and lose the record of having done so. Collapsing any pair of them is
how a spent holdout gets re-spent or a completed measurement gets reported as clean when
its evidence is missing.

Between the rebuild and the commit sits the TOCTOU check: the pack, the store, the order
assignment, both references and the generation policy are re-derived and compared against
the confirmed plan. A mismatch stops the run while the holdout is still unread.

WHY THE BACKENDS ARE PASSED IN
------------------------------
``backend_factory`` is a parameter so the qualification suite can drive this exact code
path with deterministic doubles. That is not a hole: ``classify_empirical_status`` reads
the backend ids that actually ran and marks anything synthetic ``SYNTHETIC_ONLY``, which
``decide_eligibility`` refuses before it looks at a single gate. A fake run therefore
produces a complete, honest report that can never say an adapter is eligible — and
``get_backend`` still refuses to resolve a double from any configuration.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..schemas import SchemaError
from .artifacts import (
    EvaluationManifest,
    verify_evaluation_generation,
    write_evaluation_artifacts,
)
from .comparison import build_comparison
from .config import EvaluationRunState, check_evaluation_transition
from .gates import evaluate_gates
from .pack_builder import build_task_pack_from_dataset, pack_blockers
from .references import EvaluationRole
from .reports import build_report
from .runner import result_records, results_by_role, run_paired_evaluation
from .score_evidence import build_score_evidence, response_digests
from .preflight import derive_pack_identity, execution_binding_mismatches
from .store import (
    consume_plan,
    create_generation_directory,
    is_plan_consumed,
    quarantine_generation,
    record_holdout_commit,
    record_terminal,
)
from .task_pack import EvaluationTaskKind

#: S3Q.0. The commit body's own version, carried inside the ledger line so a reader can
#: tell which set of body-free facts a given crossing recorded.
HOLDOUT_COMMIT_SCHEMA_VERSION = "m62.evaluation_holdout_commit_body.1"


class EvaluationExecutionError(SchemaError):
    """An execution that will not start, or one that did not finish honestly."""


@dataclass
class ExecutionOutcome:
    """What a run did, including the parts that did not work.

    ``state`` is the state actually reached. There is no field a caller can set to make
    a partial run look complete: ``COMPLETED`` is written by exactly one line in this
    module, after the artefacts verified on reload.
    """

    state: EvaluationRunState
    evaluation_id: str
    generation: int
    plan_hash: str
    directory: Path | None = None
    report: object | None = None
    manifest: object | None = None
    task_count: int = 0
    measured_pairs: int = 0
    empirical_status: str = ""
    eligibility: str = ""
    blockers: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    quarantine_path: Path | None = None
    states_visited: tuple[str, ...] = ()
    interrupted: bool = False

    # ── S3Q.0 durability facts, each recorded separately ────────────────────
    #: Whether the approval was spent. Says nothing about whether a model read anything.
    plan_consumed: bool = False
    #: Whether a held-out task durably crossed the model-facing boundary. Once true the
    #: holdout is USED_IMMUTABLE whatever happened next, and no rerun is permitted.
    holdout_committed: bool = False
    #: Whether the terminal ledger line is on disk. A completed measurement whose outcome
    #: was never recorded is not a clean success.
    terminal_recorded: bool = False
    #: Obligations that failed and that no amount of valid measurement compensates for.
    #: Kept apart from ``problems`` so a diagnostic warning cannot be mistaken for one
    #: and a lost durability guarantee cannot be buried among them.
    durability_problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Success requires the measurement AND every durability obligation it owes.

        ``state is COMPLETED`` alone used to be the whole test, which meant a run whose
        terminal ledger append failed reported clean success and exited zero. A one-shot
        evaluation whose durable evidence is missing is precisely the case an operator
        must be told about, so it is a failure here and a recovery condition at the CLI.
        """
        return (self.state is EvaluationRunState.COMPLETED
                and self.holdout_committed
                and self.terminal_recorded
                and not self.durability_problems)

    @property
    def recovery_required(self) -> bool:
        """A measurement that happened but cannot be reported as clean.

        Distinct from a failed run: the artefacts are valid, the holdout is spent, and
        the answer is operator recovery — never a rerun.
        """
        return bool(self.durability_problems) or (
            self.state is EvaluationRunState.COMPLETED and not self.terminal_recorded)

    @property
    def rerun_permitted(self) -> bool:
        """False the moment a held-out task crossed the model-facing boundary."""
        return not self.holdout_committed

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "evaluation_id": self.evaluation_id,
            "generation": self.generation,
            "plan_hash": self.plan_hash,
            "task_count": self.task_count,
            "measured_pairs": self.measured_pairs,
            "empirical_status": self.empirical_status,
            "eligibility": self.eligibility,
            "blockers": list(self.blockers),
            "problems": list(self.problems),
            "states_visited": list(self.states_visited),
            "interrupted": self.interrupted,
            "quarantined": self.quarantine_path is not None,
            "plan_consumed": self.plan_consumed,
            "holdout_model_facing_committed": self.holdout_committed,
            "holdout_scientifically_spent": self.holdout_committed,
            "terminal_ledger_recorded": self.terminal_recorded,
            "durability_problems": list(self.durability_problems),
            "recovery_required": self.recovery_required,
            "rerun_permitted": self.rerun_permitted,
        }


class _Machine:
    """The state walk, recorded. Every move is checked against the transition table."""

    def __init__(self, start: EvaluationRunState) -> None:
        self.state = start
        self.visited: list[str] = [start.value]

    def to(self, target: EvaluationRunState) -> EvaluationRunState:
        self.state = check_evaluation_transition(self.state, target)
        self.visited.append(self.state.value)
        return self.state

    def fail_to(self, target: EvaluationRunState) -> EvaluationRunState:
        """Move to a terminal state, tolerating a machine already sitting on one.

        A failure handler that raised a transition error while handling a failure would
        replace the real reason with a bookkeeping one.
        """
        try:
            return self.to(target)
        except SchemaError:
            return self.state


@dataclass(frozen=True)
class ExecutionRequest:
    """Everything an execution needs, so nothing is read from ambient state."""

    config: object
    baseline: object
    adapter: object
    plan: object
    output_root: Path
    dataset_root: Path
    backend_factory: Callable[[str], object]
    adapter_directory: Path | None = None
    model_cache_root: Path | None = None
    actor: str = "local-operator"
    at: str = ""
    backend_version: str = ""
    limitations: Sequence[str] = ()
    mandatory_families: Sequence[str] = ()
    extra_blockers: Sequence[str] = field(default_factory=tuple)


def execute_evaluation(request: ExecutionRequest) -> ExecutionOutcome:
    """Run one paired evaluation, or fail in a state that says so.

    The plan is assumed already recomputed and its confirmation already verified by the
    caller: this function spends it. Everything after that point is written to a
    generation directory that did not exist a moment earlier.
    """
    config, plan = request.config, request.plan
    plan_hash = plan.plan_hash()
    machine = _Machine(EvaluationRunState.PREFLIGHT_VERIFIED)
    outcome = ExecutionOutcome(
        state=machine.state, evaluation_id=config.evaluation_id,
        generation=config.evaluation_generation, plan_hash=plan_hash)

    def finish(state: EvaluationRunState, **fields) -> ExecutionOutcome:
        outcome.state = machine.fail_to(state)
        outcome.states_visited = tuple(machine.visited)
        for key, value in fields.items():
            setattr(outcome, key, value)
        return outcome

    if plan.blockers:
        return finish(EvaluationRunState.FAILED, blockers=tuple(plan.blockers))
    if is_plan_consumed(request.output_root, plan_hash):
        return finish(EvaluationRunState.FAILED,
                      problems=(f"plan {plan_hash[:12]} has already started an "
                                f"evaluation; a token authorises exactly one run",))

    # The directory is the mutual exclusion. `mkdir(exist_ok=False)` is what makes two
    # concurrent processes unable to share a generation, and it happens before the plan
    # is spent so a lost race spends nothing.
    try:
        directory = create_generation_directory(
            request.output_root, config.evaluation_id, config.evaluation_generation)
    except Exception as exc:  # noqa: BLE001 — a taken generation is a refusal, not a crash
        return finish(EvaluationRunState.FAILED,
                      problems=(f"{type(exc).__name__}: {exc}",))
    outcome.directory = directory

    # A ledger append that fails here used to escape as a bare exception AFTER the
    # filesystem had been mutated: the caller saw an internal error, the plan was still
    # unspent, and the directory left behind then refused every later attempt at the same
    # generation. The failure is now structured, and the empty directory this invocation
    # created a moment ago is withdrawn.
    try:
        consume_plan(request.output_root, plan_hash=plan_hash,
                     evaluation_id=config.evaluation_id,
                     generation=config.evaluation_generation,
                     actor=request.actor, at=request.at)
    except Exception as exc:  # noqa: BLE001 — an unrecorded start is a refusal
        rollback = _withdraw_empty_directory(directory)
        outcome.directory = None if rollback == "removed" else directory
        return finish(EvaluationRunState.FAILED, problems=(
            f"the plan could not be spent ({type(exc).__name__}: {exc}); no model was "
            f"called and no held-out task was read",
            f"the generation directory this attempt created was {rollback}",
            "the plan is UNSPENT: no start line was appended, so its confirmation still "
            "authorises one run. Nothing here retries automatically"))
    outcome.plan_consumed = True

    machine.to(EvaluationRunState.STARTING)
    try:
        return _run(request, machine, outcome, directory)
    except KeyboardInterrupt:
        # Interruption is terminal and honest. The plan stays spent.
        machine.fail_to(EvaluationRunState.INTERRUPTING)
        outcome.interrupted = True
        _quarantine(request, outcome, directory, reason="interrupted")
        return finish(EvaluationRunState.INTERRUPTED,
                      problems=("the run was interrupted; a partial comparison is not "
                                "a result and no report was written",))
    except BaseException as exc:  # noqa: BLE001 — no exception may leave a run RUNNING
        _quarantine(request, outcome, directory, reason="failed")
        return finish(EvaluationRunState.FAILED,
                      problems=(f"{type(exc).__name__}: {exc}",))
    finally:
        outcome.states_visited = tuple(machine.visited)
        _record(request, outcome)


def _run(request: ExecutionRequest, machine: _Machine, outcome: ExecutionOutcome,
         directory: Path) -> ExecutionOutcome:
    """The body, with every failure route owned by the caller above."""
    config, plan = request.config, request.plan
    baseline, adapter = request.baseline, request.adapter

    built = build_task_pack_from_dataset(
        root=request.dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation)
    outcome.task_count = len(built.pack)

    # ── TOCTOU: the pack that was approved must be the pack that runs ────────
    # The plan was hashed against identities derived from the material as it stood when
    # an operator looked at it. This rebuild is the material as it stands now. If the two
    # disagree, the confirmation authorised something else — a different pack, a
    # different answer key, a different execution order, a different adapter or a
    # different decoding policy — and the run stops here, with the plan spent and the
    # holdout still unread.
    identity = derive_pack_identity(built, seed=config.seed)
    drift = execution_binding_mismatches(
        plan=plan, identity=identity, baseline=baseline, adapter=adapter,
        generation_policy=config.generation)
    if drift:
        raise EvaluationExecutionError(
            "execution: the approved plan does not describe the material about to be "
            "measured, so the confirmation authorises a different run. No model was "
            "called and the holdout was not committed. " + "; ".join(drift[:4]))

    blockers = list(request.extra_blockers)
    blockers.extend(pack_blockers(
        built, min_tasks=config.policies.statistics.min_pairs_for_claim,
        mandatory_families=request.mandatory_families
        or config.policies.families.mandatory_families))

    # Two backends, two objects. "The baseline had no adapter attached" is a structural
    # fact about which object answered, not a claim about what one object was toggled to.
    baseline_backend = request.backend_factory(EvaluationRole.BASELINE.value)
    candidate_backend = request.backend_factory(EvaluationRole.CANDIDATE.value)
    if baseline_backend is candidate_backend:
        raise EvaluationExecutionError(
            "execution: both arms were handed the same backend object; a shared object "
            "cannot prove the baseline ran without the adapter")

    machine.to(EvaluationRunState.RUNNING_BASELINE)
    machine.to(EvaluationRunState.RUNNING_CANDIDATE)
    run = run_paired_evaluation(
        built.pack, baseline_backend=baseline_backend,
        candidate_backend=candidate_backend, baseline_reference=baseline,
        adapter_reference=adapter, generation=config.generation, seed=config.seed,
        resources=config.policies.resources,
        adapter_directory=request.adapter_directory,
        model_cache_root=request.model_cache_root,
        before_first_model_facing_invoke=_holdout_commit_callback(
            request, outcome, identity=identity,
            backend_id=str(getattr(candidate_backend, "backend_id", ""))))

    machine.to(EvaluationRunState.SCORING)
    summary = build_comparison(run, pack=built.pack, targets=built.targets,
                               policies=config.policies)
    outcome.measured_pairs = summary.measured_pairs

    machine.to(EvaluationRunState.COMPARING)
    gates = evaluate_gates(summary, policies=config.policies,
                           present_splits=sorted(built.pack.counts_by_split()))

    backend_ids = sorted({getattr(baseline_backend, "backend_id", ""),
                          getattr(candidate_backend, "backend_id", "")} - {""})
    report = build_report(
        plan=plan, summary=summary, gates=gates, baseline=baseline, adapter=adapter,
        policies=config.policies, backend_ids=backend_ids,
        backend_version=request.backend_version,
        split_manifest_hashes=dict(built.shard_hashes),
        run_state=EvaluationRunState.COMPARING, created_at_utc=request.at,
        interrupted=run.interrupted,
        limitations=list(request.limitations) + blockers)
    outcome.report = report
    outcome.empirical_status = getattr(report.empirical_status, "value",
                                       str(report.empirical_status))
    outcome.eligibility = getattr(report.eligibility, "value", str(report.eligibility))
    outcome.blockers = tuple(blockers)

    machine.to(EvaluationRunState.ARTIFACT_VALIDATION)
    manifest = EvaluationManifest(
        evaluation_id=config.evaluation_id,
        generation=config.evaluation_generation,
        plan_hash=plan.plan_hash(), report_hash=report.report_hash(),
        task_pack_hash=built.pack.pack_hash(),
        hidden_target_store_hash=built.targets.store_hash(),
        baseline_reference_hash=baseline.reference_hash(),
        candidate_adapter_reference_hash=adapter.reference_hash(),
        comparison_manifest_hash=summary.comparison_manifest_hash(),
        backend_ids=tuple(backend_ids),
        empirical_status=outcome.empirical_status,
        eligibility=outcome.eligibility,
        task_count=summary.task_count, measured_pairs=summary.measured_pairs,
        files=(), total_bytes=0, tree_hash="", created_at_utc=request.at)

    # Built once and passed to both the results artefact and the review evidence, so the
    # response digest a reviewer reads is literally the one the results file recorded
    # rather than a second computation that could disagree with it.
    baseline_records = result_records(results_by_role(run, EvaluationRole.BASELINE))
    candidate_records = result_records(results_by_role(run, EvaluationRole.CANDIDATE))
    evidence = {
        role: build_score_evidence(
            summary.comparisons, role=role, evaluation_id=config.evaluation_id,
            generation=config.evaluation_generation,
            response_digests=response_digests(records))
        for role, records in (("baseline", baseline_records),
                              ("candidate", candidate_records))
    }

    validation = write_evaluation_artifacts(
        directory,
        plan_record=plan.to_record(),
        task_pack_records=built.pack.task_records(),
        task_pack_manifest={**built.pack.to_record(), **built.manifest()},
        baseline_records=baseline_records,
        candidate_records=candidate_records,
        comparison_records=summary.comparison_records(),
        baseline_score_records=evidence["baseline"],
        candidate_score_records=evidence["candidate"],
        report_record=report.to_record(),
        # Both arms in one record, each carrying its own denominators, so a rate that
        # was computed from nothing stays visibly computed from nothing.
        metrics_record={
            "summary_hash": summary.summary_hash(),
            "task_count": summary.task_count,
            "measured_pairs": summary.measured_pairs,
            "missing_pairs": summary.missing_pairs,
            "wins": summary.wins, "ties": summary.ties, "losses": summary.losses,
            "security_regressions": summary.security_regressions,
            "security_improvements": summary.security_improvements,
            "baseline": summary.baseline_metrics.to_dict(),
            "candidate": summary.candidate_metrics.to_dict(),
            "statistics": summary.bootstrap.to_dict(),
        },
        manifest=manifest,
        ceilings=config.policies.resources)
    outcome.manifest = manifest

    problems = tuple(getattr(validation, "problems", ()) or ())
    problems += tuple(verify_evaluation_generation(
        directory, ceilings=config.policies.resources) or ())
    if problems:
        # The artefacts do not describe themselves. Nothing here may be reported as a
        # completed evaluation, whatever the comparison said.
        outcome.problems = problems
        machine.fail_to(EvaluationRunState.QUARANTINED)
        _quarantine(request, outcome, directory, reason="artifact_validation_failed")
        outcome.state = EvaluationRunState.QUARANTINED
        return outcome

    outcome.state = machine.to(EvaluationRunState.COMPLETED)
    return outcome


def _holdout_commit_callback(request: ExecutionRequest, outcome: ExecutionOutcome, *,
                             identity: object, backend_id: str):
    """The one-shot durable commit the runner fires before the first backend call.

    Execution owns the ledger, so execution owns this callback; the runner knows only
    that something must succeed at that seam. The body is assembled from the plan, the
    re-derived pack identity and the runner's body-free facts about the first crossing —
    never from a task, a target or a response, none of which this closure can reach.
    """
    plan, config = request.plan, request.config

    def commit(first: dict) -> None:
        record_holdout_commit(
            request.output_root, plan_hash=outcome.plan_hash,
            evaluation_id=outcome.evaluation_id, generation=outcome.generation,
            actor=request.actor, at=request.at,
            commit={
                "commit_schema_version": HOLDOUT_COMMIT_SCHEMA_VERSION,
                "dataset_id": identity.dataset_id,
                "dataset_version": identity.dataset_version,
                "dataset_manifest_hash": identity.dataset_manifest_hash,
                "task_pack_hash": identity.pack_hash,
                "hidden_target_store_hash": identity.hidden_target_store_hash,
                "pack_identity_hash": identity.identity_hash(),
                "order_policy": str(first["order_policy"]),
                "order_assignment_hash": str(first["order_assignment_hash"]),
                "task_count": int(first["task_count"]),
                "target_count": int(identity.target_count),
                "first_task_id": str(first["task_id"]),
                "first_task_hash": str(first["task_hash"]),
                "first_arm": str(first["first_arm"]),
                "first_request_parity_hash": str(first["parity_hash"]),
                "baseline_reference_hash": request.baseline.reference_hash(),
                "candidate_adapter_reference_hash": request.adapter.reference_hash(),
                "generation_policy_hash": config.generation.policy_hash(),
                "backend_id": backend_id or str(getattr(plan, "backend_id", "")),
                "performs_inference": bool(getattr(plan, "performs_inference", False)),
            })
        # Reached only when the append returned. From this instant the holdout is
        # USED_IMMUTABLE and no rerun is permitted, whatever the next statement does.
        outcome.holdout_committed = True

    return commit


def _withdraw_empty_directory(directory: Path) -> str:
    """Remove the generation directory THIS invocation created, if it is still empty.

    Deliberately ``rmdir`` and never a recursive delete. The only thing safe to withdraw
    is a directory that was created moments ago and into which nothing has been written;
    anything with a file in it might be somebody else's evidence, and an evaluation
    subsystem that can delete trees is one bad path join away from deleting a completed
    generation. A directory that cannot be withdrawn is left exactly where it is and
    reported, because a stuck generation an operator can see beats one that vanished.
    """
    try:
        if directory.is_symlink() or not directory.is_dir():
            return "left in place (not a plain directory)"
        if any(directory.iterdir()):
            return ("left in place because it is not empty; it must be inspected before "
                    "this generation is attempted again")
        directory.rmdir()
    except OSError as exc:
        return f"left in place ({exc.strerror})"
    return "removed"


def _quarantine(request: ExecutionRequest, outcome: ExecutionOutcome,
                directory: Path, *, reason: str) -> None:
    """Move a generation aside. A failure here is recorded, never raised over the cause."""
    try:
        outcome.quarantine_path = quarantine_generation(
            request.output_root, directory,
            evaluation_id=outcome.evaluation_id, generation=outcome.generation,
            nonce=outcome.plan_hash[:8])
    except Exception as exc:  # noqa: BLE001
        outcome.problems += (f"quarantine failed ({type(exc).__name__}: {exc}); the "
                             f"generation directory was left in place for inspection",)


def _record(request: ExecutionRequest, outcome: ExecutionOutcome) -> None:
    """Append the terminal ledger line. Every outcome gets one, including the bad ones.

    A failure here is DURABILITY-CRITICAL, not a diagnostic. The measurement may be
    perfectly valid and its artefacts may verify, and the run still has no durable record
    of how it ended — which is exactly the evidence a one-shot ceremony is judged on. It
    is recorded as such so ``ok`` refuses and the CLI reports recovery rather than
    success. The artefacts are NOT discarded and the run is NOT repeated: the holdout is
    already spent.
    """
    if not outcome.state.is_terminal:
        return
    try:
        record_terminal(request.output_root, plan_hash=outcome.plan_hash,
                        evaluation_id=outcome.evaluation_id,
                        generation=outcome.generation, actor=request.actor,
                        at=request.at, state=outcome.state)
    except Exception as exc:  # noqa: BLE001
        outcome.durability_problems += (
            f"the terminal ledger line could not be appended "
            f"({type(exc).__name__}: {exc}); this run has no durable record of how it "
            f"ended",)
        outcome.problems += (
            "RECOVERY REQUIRED, NOT A RERUN: the artefacts on disk are retained and must "
            "not be overwritten, the plan stays spent, and the holdout stays spent if it "
            "was committed. Re-running this evaluation is forbidden",)
        return
    outcome.terminal_recorded = True


def production_backend_factory(backend_id: str) -> Callable[[str], object]:
    """A factory that builds a fresh reviewed backend per arm. Never a shared object."""
    from .backends import get_backend

    def factory(_role: str) -> object:
        return get_backend(backend_id)

    return factory


__all__ = [
    "HOLDOUT_COMMIT_SCHEMA_VERSION", "EvaluationExecutionError", "EvaluationTaskKind",
    "ExecutionOutcome", "ExecutionRequest", "execute_evaluation",
    "production_backend_factory",
]
