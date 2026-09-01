"""training_gym/evaluation/execution_v4.py — V69 M62 S4E: the wired V4 caller.

WHAT THIS ADDS TO ``execution.py``, AND WHAT IT COPIES FROM IT
--------------------------------------------------------------
It adds ORDER, and the guarantees that come from order — the same thing ``execution.py``
adds for the old protocol, which is why the sequence below is deliberately the same
sequence with two extra steps:

    directory created            before a plan is spent
    plan spent                   before a paired attempt is recorded
    PAIRED ATTEMPT RECORDED      before the holdout is committed          <- S4E
    holdout committed            before a model is loaded
    generations counted          at the model-facing boundary             <- S4E
    report written               only after the artefacts re-verify from disk

``execution.py`` is FROZEN against the S3Q.0 subject commit by a live scientific guard,
so this is a sibling rather than an edit. It calls the frozen primitives — the ledger,
the pack builder, the TOCTOU binding check, the comparison, the gates, the report, the
artifact writer — completely unchanged.

THE FIVE EVENTS, WHICH ARE NOT SYNONYMS
----------------------------------------
    PLAN_CONSUMED                  one approval began one run
    PAIRED_ATTEMPT_RECORDED        this corpus now has an attempt against it
    HOLDOUT_MODEL_FACING_COMMITTED a held-out task is about to reach a model
    EVALUATION_COMPLETED           artefacts exist and re-verify from disk
    TERMINAL_LEDGER_RECORDED       how it ended is durable

The old protocol has four. S4E splits the third because a paired attempt has to be
durable BEFORE the ledger commit: if the process dies between them, the corpus reads as
"attempted, never reached a model", the next run is refused, and an operator rules. The
reverse order can lose the record of an attempt whose model calls had already begun.

WHAT THE REPORT SAYS ABOUT ARM 0, AND WHY IT IS SAID IN THE LIMITATIONS
-----------------------------------------------------------------------
``reports.build_report`` is frozen and takes one ``BaseModelEvaluationReference`` plus
one ``AdapterEvaluationReference``. Under V4 the base reference is TRUE — it is the
shared base both arms load — but the report has no field that says arm 0 also carried an
adapter. Rather than edit a frozen builder or let a reader infer it from a filename, the
executor pushes an explicit, unmissable line into the report's ``limitations``, and the
sealed v4 receipt carries the machine-readable binding.
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
from .config import EvaluationRunState
from .gates import evaluate_gates
from .pack_builder import build_task_pack_from_dataset, pack_blockers
from .plan_v4 import V4EvaluationPlan
from .preflight import derive_pack_identity, execution_binding_mismatches
from .protocol_v4 import EvaluationArmRole
from .reports import build_report
from .runner import result_records
from .runner_v4 import V4RunEvidence, run_paired_v4_evaluation, v4_arm_results
from .score_evidence import build_score_evidence, response_digests
from .store import (
    consume_plan,
    create_generation_directory,
    is_plan_consumed,
    quarantine_generation,
    record_terminal,
)
from .store_v4 import commit_v4_holdout, record_v4_paired_attempt

#: The commit body version a V4 crossing writes. The FROZEN ledger shape, used exactly:
#: every field keeps its v1 meaning, and ``baseline_reference_hash`` is the SHARED BASE
#: MODEL both arms load, which is what it has always meant. The two-adapter facts live
#: in the paired-attempt record, which has fields for them.
HOLDOUT_COMMIT_SCHEMA_VERSION = "m62.evaluation_holdout_commit_body.1"

#: Pushed into every V4 report so no reader can mistake arm 0 for a bare base model.
V4_ARM_LIMITATION = (
    "PROTOCOL V4: the 'baseline' arm of this comparison is NOT a bare base model. It is "
    "the declared REFERENCE ADAPTER attached to the shared base. Every metric, gate and "
    "delta named 'baseline' here is measured against that trained adapter, so a null "
    "result is a stronger claim than the same null under the v1-v3 protocol and must "
    "not be read as one. The sealed v4 receipt carries both arms' digests.")


class ExecutionV4Error(SchemaError):
    """A paired execution that will not start, or one that did not finish honestly."""


@dataclass
class V4ExecutionOutcome:
    """What happened, with every durability fact recorded separately."""

    state: EvaluationRunState
    evaluation_id: str
    generation: int
    plan_hash: str
    directory: Path | None = None
    plan_consumed: bool = False
    paired_attempt_recorded: bool = False
    holdout_committed: bool = False
    terminal_recorded: bool = False
    task_count: int = 0
    measured_pairs: int = 0
    reference_generations: int = 0
    candidate_generations: int = 0
    total_generations: int = 0
    empirical_status: str = ""
    eligibility: str = ""
    report: object = None
    manifest: object = None
    evidence: V4RunEvidence | None = None
    blockers: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    interrupted: bool = False
    states_visited: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (self.state is EvaluationRunState.COMPLETED and not self.problems
                and self.terminal_recorded)

    @property
    def holdout_is_spent(self) -> bool:
        """Once EITHER durable record exists the corpus is spent, whatever happened next."""
        return self.paired_attempt_recorded or self.holdout_committed

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "evaluation_id": self.evaluation_id,
            "generation": self.generation,
            "plan_hash": self.plan_hash,
            "plan_consumed": self.plan_consumed,
            "paired_attempt_recorded": self.paired_attempt_recorded,
            "holdout_committed": self.holdout_committed,
            "holdout_is_spent": self.holdout_is_spent,
            "terminal_recorded": self.terminal_recorded,
            "task_count": self.task_count,
            "measured_pairs": self.measured_pairs,
            "reference_generations": self.reference_generations,
            "candidate_generations": self.candidate_generations,
            "total_generations": self.total_generations,
            "empirical_status": self.empirical_status,
            "eligibility": self.eligibility,
            "interrupted": self.interrupted,
            "blockers": list(self.blockers),
            "problems": list(self.problems),
            "states_visited": list(self.states_visited),
        }


@dataclass(frozen=True)
class V4ExecutionRequest:
    """Everything a paired execution needs, so nothing is read from ambient state."""

    config: object
    plan: V4EvaluationPlan
    baseline: object
    reference_adapter: object
    candidate_adapter: object
    reference_adapter_directory: Path
    candidate_adapter_directory: Path
    output_root: Path
    dataset_root: Path
    backend_factory: Callable[[str], object]
    model_cache_root: Path | None = None
    actor: str = "local-operator"
    at: str = ""
    backend_version: str = ""
    limitations: Sequence[str] = ()
    mandatory_families: Sequence[str] = ()
    extra_blockers: Sequence[str] = field(default_factory=tuple)


def execute_v4_evaluation(request: V4ExecutionRequest) -> V4ExecutionOutcome:
    """Run ONE reference-adapter paired evaluation, or fail in a state that says so."""
    config, plan = request.config, request.plan
    plan_hash = plan.plan_hash()
    outcome = V4ExecutionOutcome(
        state=EvaluationRunState.PREFLIGHT_VERIFIED,
        evaluation_id=config.evaluation_id,
        generation=config.evaluation_generation, plan_hash=plan_hash)
    visited: list[str] = [outcome.state.value]

    def finish(state: EvaluationRunState, **fields) -> V4ExecutionOutcome:
        outcome.state = state
        visited.append(state.value)
        outcome.states_visited = tuple(visited)
        for key, value in fields.items():
            setattr(outcome, key, value)
        return outcome

    if plan.blockers or plan.inner.blockers:
        return finish(EvaluationRunState.FAILED,
                      blockers=tuple(plan.blockers) + tuple(plan.inner.blockers))
    if is_plan_consumed(request.output_root, plan_hash):
        return finish(EvaluationRunState.FAILED,
                      problems=(f"plan {plan_hash[:12]} has already started an "
                                f"evaluation; a token authorises exactly one run",))

    # The directory is the mutual exclusion, and it happens before the plan is spent so a
    # lost race spends nothing.
    try:
        directory = create_generation_directory(
            request.output_root, config.evaluation_id, config.evaluation_generation)
    except Exception as exc:  # noqa: BLE001 — a taken generation is a refusal
        return finish(EvaluationRunState.FAILED,
                      problems=(f"{type(exc).__name__}: {exc}",))
    outcome.directory = directory

    try:
        consume_plan(request.output_root, plan_hash=plan_hash,
                     evaluation_id=config.evaluation_id,
                     generation=config.evaluation_generation,
                     actor=request.actor, at=request.at)
    except Exception as exc:  # noqa: BLE001 — an unrecorded start is a refusal
        return finish(EvaluationRunState.FAILED, problems=(
            f"the plan could not be spent ({type(exc).__name__}: {exc}); no model was "
            f"called and no held-out task was read",
            "the plan is UNSPENT: no start line was appended, so its confirmation still "
            "authorises one run. Nothing here retries automatically"))
    outcome.plan_consumed = True
    visited.append(EvaluationRunState.STARTING.value)
    outcome.state = EvaluationRunState.STARTING

    try:
        return _run(request, outcome, directory, visited, finish)
    except KeyboardInterrupt:
        outcome.interrupted = True
        _quarantine(request, outcome, directory, reason="interrupted")
        return finish(EvaluationRunState.INTERRUPTED, problems=(
            "the run was interrupted; a partial comparison is not a result and no report "
            "was written",))
    except BaseException as exc:  # noqa: BLE001 — no exception may leave a run RUNNING
        _quarantine(request, outcome, directory, reason="failed")
        return finish(EvaluationRunState.FAILED,
                      problems=(f"{type(exc).__name__}: {exc}",))
    finally:
        outcome.states_visited = tuple(visited)
        _record_terminal(request, outcome)


def _run(request: V4ExecutionRequest, outcome: V4ExecutionOutcome, directory: Path,
         visited: list, finish) -> V4ExecutionOutcome:
    """The body, with every failure route owned by the caller above."""
    config, plan = request.config, request.plan

    built = build_task_pack_from_dataset(
        root=request.dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation)
    outcome.task_count = len(built.pack)

    # ── TOCTOU: the pack that was approved must be the pack that runs ────────
    identity = derive_pack_identity(built, seed=config.seed)
    drift = execution_binding_mismatches(
        plan=plan.inner, identity=identity, baseline=request.baseline,
        adapter=request.candidate_adapter, generation_policy=config.generation)
    if drift:
        raise ExecutionV4Error(
            "execution: the approved plan does not describe the material about to be "
            "measured, so the confirmation authorises a different run. No model was "
            "called and the holdout was not committed. " + "; ".join(drift[:4]))
    # The V4 half of the same check. The inner plan has no field for the reference arm,
    # so the reference adapter is compared HERE or nowhere.
    if plan.reference_adapter_reference_hash != request.reference_adapter.reference_hash():
        raise ExecutionV4Error(
            "execution: the reference adapter about to be loaded is not the reference "
            "adapter the confirmed plan bound. The arm nobody re-checked is the arm a "
            "swap hides in")
    order = tuple(t.task_id for t in built.pack.tasks)
    from .plan_v4 import task_order_hash as _order_hash
    if plan.task_order_hash != _order_hash(order):
        raise ExecutionV4Error(
            "execution: the frozen task ORDER is not the order the plan bound; a pack "
            "reordered after approval is a different experiment")

    blockers = list(request.extra_blockers)
    blockers.extend(pack_blockers(
        built, min_tasks=config.policies.statistics.min_pairs_for_claim,
        mandatory_families=request.mandatory_families
        or config.policies.families.mandatory_families))

    # Two backends, two objects, one per arm.
    reference_backend = request.backend_factory(EvaluationArmRole.REFERENCE.value)
    candidate_backend = request.backend_factory(EvaluationArmRole.CANDIDATE.value)

    visited.append(EvaluationRunState.RUNNING_BASELINE.value)
    visited.append(EvaluationRunState.RUNNING_CANDIDATE.value)
    run, evidence = run_paired_v4_evaluation(
        built.pack,
        reference_backend=reference_backend, candidate_backend=candidate_backend,
        pairing=plan.pairing, baseline_reference=request.baseline,
        reference_adapter=request.reference_adapter,
        candidate_adapter=request.candidate_adapter,
        reference_adapter_directory=request.reference_adapter_directory,
        candidate_adapter_directory=request.candidate_adapter_directory,
        generation=config.generation, seed=config.seed,
        resources=config.policies.resources,
        model_cache_root=request.model_cache_root,
        before_first_model_facing_invoke=_spend_callback(
            request, outcome, identity=identity,
            backend_id=str(getattr(candidate_backend, "backend_id", ""))))
    outcome.evidence = evidence
    outcome.reference_generations = evidence.ledger.reference
    outcome.candidate_generations = evidence.ledger.candidate
    outcome.total_generations = evidence.ledger.total

    visited.append(EvaluationRunState.SCORING.value)
    summary = build_comparison(run, pack=built.pack, targets=built.targets,
                               policies=config.policies)
    outcome.measured_pairs = summary.measured_pairs

    visited.append(EvaluationRunState.COMPARING.value)
    gates = evaluate_gates(summary, policies=config.policies,
                           present_splits=sorted(built.pack.counts_by_split()))

    backend_ids = sorted({getattr(reference_backend, "backend_id", ""),
                          getattr(candidate_backend, "backend_id", "")} - {""})
    report = build_report(
        plan=plan.inner, summary=summary, gates=gates, baseline=request.baseline,
        adapter=request.candidate_adapter, policies=config.policies,
        backend_ids=backend_ids, backend_version=request.backend_version,
        split_manifest_hashes=dict(built.shard_hashes),
        run_state=EvaluationRunState.COMPARING, created_at_utc=request.at,
        interrupted=run.interrupted,
        # The V4 arm statement goes FIRST so it cannot be skimmed past.
        limitations=[V4_ARM_LIMITATION] + list(request.limitations) + blockers)
    outcome.report = report
    outcome.empirical_status = getattr(report.empirical_status, "value",
                                       str(report.empirical_status))
    outcome.eligibility = getattr(report.eligibility, "value", str(report.eligibility))
    outcome.blockers = tuple(blockers)

    visited.append(EvaluationRunState.ARTIFACT_VALIDATION.value)
    manifest = EvaluationManifest(
        evaluation_id=config.evaluation_id,
        generation=config.evaluation_generation,
        plan_hash=plan.inner.plan_hash(), report_hash=report.report_hash(),
        task_pack_hash=built.pack.pack_hash(),
        hidden_target_store_hash=built.targets.store_hash(),
        baseline_reference_hash=request.baseline.reference_hash(),
        candidate_adapter_reference_hash=request.candidate_adapter.reference_hash(),
        comparison_manifest_hash=summary.comparison_manifest_hash(),
        backend_ids=tuple(backend_ids),
        empirical_status=outcome.empirical_status, eligibility=outcome.eligibility,
        task_count=summary.task_count, measured_pairs=summary.measured_pairs,
        files=(), total_bytes=0, tree_hash="", created_at_utc=request.at)

    reference_records = result_records(v4_arm_results(run, EvaluationArmRole.REFERENCE))
    candidate_records = result_records(v4_arm_results(run, EvaluationArmRole.CANDIDATE))
    score_evidence = {
        role: build_score_evidence(
            summary.comparisons, role=role, evaluation_id=config.evaluation_id,
            generation=config.evaluation_generation,
            response_digests=response_digests(records))
        for role, records in (("baseline", reference_records),
                              ("candidate", candidate_records))
    }

    validation = write_evaluation_artifacts(
        directory,
        plan_record=plan.inner.to_record(),
        task_pack_records=built.pack.task_records(),
        task_pack_manifest={**built.pack.to_record(), **built.manifest()},
        baseline_records=reference_records,
        candidate_records=candidate_records,
        comparison_records=summary.comparison_records(),
        baseline_score_records=score_evidence["baseline"],
        candidate_score_records=score_evidence["candidate"],
        report_record=report.to_record(),
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
        manifest=manifest, ceilings=config.policies.resources)
    outcome.manifest = manifest

    problems = tuple(getattr(validation, "problems", ()) or ())
    problems += tuple(verify_evaluation_generation(
        directory, ceilings=config.policies.resources) or ())
    # The accounting is part of finishing honestly, not a warning beside it.
    problems += tuple(evidence.ledger.accounting_blockers(task_count=len(built.pack)))
    if problems:
        outcome.problems = problems
        _quarantine(request, outcome, directory, reason="artifact_validation_failed")
        return finish(EvaluationRunState.QUARANTINED)
    return finish(EvaluationRunState.COMPLETED)


def _spend_callback(request: V4ExecutionRequest, outcome: V4ExecutionOutcome, *,
                    identity: object, backend_id: str):
    """The one-shot durable spend, fired before the first backend call.

    Two records, in this order, both before any model runs:
      1. the PAIRED ATTEMPT — fsynced, and carrying both arms;
      2. the frozen LEDGER COMMIT — the canonical model-facing crossing.

    If either raises, the exception propagates, the runner calls no backend, and the run
    stops. A holdout that could not be recorded as spent is not spent.
    """
    plan, config = request.plan, request.config

    def commit(first: dict) -> None:
        record_v4_paired_attempt(
            request.output_root, plan_hash=plan.plan_hash(),
            inner_plan_hash=plan.inner.plan_hash(),
            evaluation_id=config.evaluation_id,
            generation=config.evaluation_generation,
            actor=request.actor, at=request.at,
            attempt={
                "protocol_version": plan.protocol_version,
                "pairing_hash": plan.pairing.pairing_hash(),
                "reference_arm_hash": plan.pairing.reference.arm_hash(),
                "candidate_arm_hash": plan.pairing.candidate.arm_hash(),
                "reference_adapter_sha256": plan.pairing.reference.adapter_sha256,
                "candidate_adapter_sha256": plan.pairing.candidate.adapter_sha256,
                "common_base_model_id": plan.pairing.reference.base_model_id,
                "common_base_model_revision":
                    plan.pairing.reference.base_model_revision,
                "dataset_id": plan.holdout_dataset_id,
                "dataset_version": plan.holdout_dataset_version,
                "dataset_manifest_hash": plan.holdout_manifest_hash,
                "task_pack_hash": plan.holdout_pack_hash,
                "task_order_hash": plan.task_order_hash,
                "task_count": plan.spend.task_count,
                "expected_total_generations": plan.expected_total_generations,
                "holdout_spends": plan.spend.holdout_spends,
                "runtime_report_sha256": plan.runtime_report_sha256,
                "evaluation_source_commit": plan.evaluation_source_commit,
            })
        outcome.paired_attempt_recorded = True

        commit_v4_holdout(
            request.output_root, plan_hash=plan.plan_hash(),
            evaluation_id=config.evaluation_id,
            generation=config.evaluation_generation,
            actor=request.actor, at=request.at,
            commit={
                "commit_schema_version": HOLDOUT_COMMIT_SCHEMA_VERSION,
                "dataset_id": plan.holdout_dataset_id,
                "dataset_version": plan.holdout_dataset_version,
                "dataset_manifest_hash": plan.holdout_manifest_hash,
                "task_pack_hash": plan.holdout_pack_hash,
                "hidden_target_store_hash": getattr(
                    identity, "hidden_target_store_hash", ""),
                "pack_identity_hash": identity.identity_hash(),
                "order_policy": first["order_policy"],
                "order_assignment_hash": first["order_assignment_hash"],
                "task_count": first["task_count"],
                "target_count": getattr(identity, "target_count", 0),
                "first_task_id": first["task_id"],
                "first_task_hash": first["task_hash"],
                "first_arm": first["first_arm"],
                "first_request_parity_hash": first["parity_hash"],
                # The SHARED BASE both arms load. Its v1 meaning, unchanged.
                "baseline_reference_hash": request.baseline.reference_hash(),
                "candidate_adapter_reference_hash":
                    request.candidate_adapter.reference_hash(),
                "generation_policy_hash": config.generation.policy_hash(),
                "backend_id": backend_id,
                "performs_inference": True,
            })
        outcome.holdout_committed = True

    return commit


def _quarantine(request: V4ExecutionRequest, outcome: V4ExecutionOutcome,
                directory: Path, *, reason: str) -> None:
    try:
        quarantine_generation(request.output_root, directory, reason=reason)
    except Exception:  # noqa: BLE001 — quarantine failure must not mask the real fault
        outcome.problems = outcome.problems + (
            f"the generation directory could not be quarantined after {reason}",)


def _record_terminal(request: V4ExecutionRequest, outcome: V4ExecutionOutcome) -> None:
    if not outcome.plan_consumed or outcome.terminal_recorded:
        return
    if not outcome.state.is_terminal:
        return
    try:
        record_terminal(request.output_root, plan_hash=outcome.plan_hash,
                        evaluation_id=outcome.evaluation_id,
                        generation=outcome.generation, actor=request.actor,
                        at=request.at, state=outcome.state)
        outcome.terminal_recorded = True
    except Exception as exc:  # noqa: BLE001 — an unrecorded ending is a problem, not a crash
        outcome.problems = outcome.problems + (
            f"the terminal state could not be recorded ({type(exc).__name__}: {exc}); "
            f"how this run ended is NOT durable",)


__all__ = [
    "ExecutionV4Error", "HOLDOUT_COMMIT_SCHEMA_VERSION", "V4ExecutionOutcome",
    "V4ExecutionRequest", "V4_ARM_LIMITATION", "execute_v4_evaluation",
]
