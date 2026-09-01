"""training_gym/evaluation/runner_v4.py — V69 M62 S4E: running two adapters, once each.

WHY THIS EXISTS BESIDE ``runner.py`` RATHER THAN INSIDE IT
---------------------------------------------------------
``runner.run_paired_evaluation`` builds its first arm as
``EvaluationRequest(role=BASELINE, adapter=None)`` and ``backend.py`` refuses a BASELINE
request that carries an adapter. Both are correct: the old protocol's whole claim is
"this arm had no adapter attached", and a runner that could quietly attach one would
make that claim unfalsifiable. So this module does not relax that refusal, does not
import around it, and does not edit it. The old runner keeps its meaning byte-for-byte.

THE ONE STRUCTURAL IDEA
-----------------------
Under Protocol V4 **both** arms are adapter-bearing. At the backend protocol layer that
is not two different kinds of request — it is the same kind twice. So both arms are
built as ``EvaluationRequest(role=CANDIDATE, adapter=<that arm's reference>)`` and the
arm identity (REFERENCE vs CANDIDATE) is carried BESIDE the request, in
:class:`~training_gym.evaluation.protocol_v4.AdapterArmReference`, never inside it.

That is not a trick to get past a validator. It is the strongest single-axis guarantee
available here, and it is worth being explicit about why:

  * both arms enter ``transformers_peft._generate`` through the SAME branch — the same
    chat rendering, the same reasoning policy, the same truncation rule, the same
    ``set_seed``, the same ``generation_kwargs``, the same PEFT attach, the same
    ``active_adapters`` liveness assertion, the same release;
  * both arms therefore receive the full ``_adapter_problems`` verification —
    symlink refusal, ``validate_adapter_directory``, tree-hash comparison against the
    plan-time reference, base-model and tokenizer identity cross-checks. Under the old
    shape that verification was gated on ``request.is_candidate``, so a reference arm
    would have received NONE of it: the arm nobody checked is the arm a swap hides in;
  * the ONLY values that differ between the two requests are ``adapter`` and
    ``adapter_directory``. That is the experiment.

``EvaluationRequest.parity_hash`` deliberately excludes role and adapter, so the two
arms' parity hashes are equal and the existing per-task parity assertion keeps working
unchanged — it is what proves the two adapters were asked the same question.

WHAT THIS RETURNS, AND WHY IT IS THE OLD SHAPE
----------------------------------------------
It returns a plain :class:`~training_gym.evaluation.runner.PairedRun` whose ``baseline``
slot holds the REFERENCE arm's result and whose ``candidate`` slot holds the CANDIDATE
arm's. That is deliberate: it means ``build_comparison``, ``paired_statistics``,
``evaluate_gates``, the metric stack and every frozen policy digest run on a V4 run
COMPLETELY UNCHANGED. Not one line of the measurement layer is V4-aware, which is
exactly why the S4D gate-equivalence analysis holds: those gates are functions of two
arm bundles and the per-task deltas between them, and they never learn which arm is
which.

The slot is named ``baseline`` because that is what the frozen scorer calls arm 0. It
does NOT mean "a bare base model answered". :class:`V4ArmBinding` is returned alongside
and says, in digests, exactly which adapter occupied which slot; the durable
paired-attempt record and the sealed v4 receipt both carry it, and the report carries it
in prose. A reader is never left to infer it from a filename.

WHAT IS COUNTED, AND WHERE
--------------------------
:class:`GenerationLedger` counts at the lowest reliable model-facing boundary — the
statement that calls ``backend.generate`` — not at the top of the task loop. A count
taken anywhere higher counts intentions.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
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
from .protocol_v4 import (
    AdapterArmReference,
    EvaluationArmRole,
    ProtocolV4Error,
    ReferenceAdapterPairing,
    assert_no_cross_arm_context,
)
from .references import AdapterEvaluationReference, BaseModelEvaluationReference, EvaluationRole
from .runner import (
    ORDER_POLICY_BALANCED,
    ExecutionOrder,
    PairedGeneration,
    PairedRun,
    PairedStatus,
    _classify,
    order_assignment_hash,
    order_balance,
)
from .task_pack import EvaluationTask, EvaluationTaskPack

#: Bumped when the V4 arm loop, its ordering or its accounting changes.
RUNNER_V4_VERSION = "m62.evaluation_runner_v4.1"

#: Which arm answers first, in V4 vocabulary. The RULE is the old one, unchanged:
#: ``sha256(task_hash:seed)``. Reusing the rule rather than inventing a second one means
#: ``order_assignment_hash`` keeps its meaning and the counterbalancing is the same
#: preregistered, answer-independent function it has always been.
class V4ArmOrder(str, Enum):
    REFERENCE_FIRST = "reference_first"
    CANDIDATE_FIRST = "candidate_first"


#: The old enum member that maps onto each V4 order. ``BASELINE_FIRST`` means "arm 0
#: answered first", which under V4 is the reference adapter.
_ORDER_MAP = {
    ExecutionOrder.BASELINE_FIRST: V4ArmOrder.REFERENCE_FIRST,
    ExecutionOrder.CANDIDATE_FIRST: V4ArmOrder.CANDIDATE_FIRST,
}


class RunnerV4Error(SchemaError):
    """The paired reference-adapter comparison could not be produced as described."""


def v4_execution_order(task: EvaluationTask, *, seed: int) -> V4ArmOrder:
    """Which adapter answers first. Identical arithmetic to the frozen v1-v3 rule.

    Deliberately re-derived here from the same digest rather than imported through a
    wrapper, so a reader can see that the V4 ordering is not a second policy: it is the
    same one, reported in the vocabulary of the arms that actually ran.
    """
    digest = sha256_text(f"{task.task_hash}:{int(seed)}")
    return (V4ArmOrder.CANDIDATE_FIRST if int(digest[:8], 16) % 2
            else V4ArmOrder.REFERENCE_FIRST)


# ══════════════════════════════════════════════════════════════════════════════
#  Which adapter occupied which slot
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class V4ArmBinding:
    """The slot-to-adapter map, so ``baseline`` is never read as ``bare base model``.

    The frozen scorer calls arm 0 ``baseline``. Under Protocol V4 arm 0 is a LoRA
    adapter, and every surface that could be misread — the receipt, the arms artefact,
    the holdout commit — carries this object's digests instead of relying on a name.
    """

    pairing: ReferenceAdapterPairing
    reference_run_id: str
    candidate_run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.pairing, ReferenceAdapterPairing):
            raise RunnerV4Error("v4 arm binding: pairing must be a ReferenceAdapterPairing")

    def to_dict(self) -> dict:
        return {
            "runner_v4_version": RUNNER_V4_VERSION,
            "protocol_version": self.pairing.protocol_version,
            # The load-bearing sentence of this whole object.
            "baseline_slot_holds": "reference_adapter_arm",
            "candidate_slot_holds": "candidate_adapter_arm",
            "baseline_slot_is_a_bare_base_model": False,
            "reference_arm": self.pairing.reference.to_dict(),
            "candidate_arm": self.pairing.candidate.to_dict(),
            "reference_arm_hash": self.pairing.reference.arm_hash(),
            "candidate_arm_hash": self.pairing.candidate.arm_hash(),
            "pairing_hash": self.pairing.pairing_hash(),
            "reference_run_id": self.reference_run_id,
            "candidate_run_id": self.candidate_run_id,
        }

    def binding_hash(self) -> str:
        return sha256_obj(self.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
#  Accounting
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class GenerationLedger:
    """Model-facing invocations, counted where they actually happen.

    ``record`` is called from the statement that invokes ``backend.generate`` and from
    nowhere else, so the totals are a property of the run rather than of the loop that
    was supposed to drive it. A warmup, a smoke check or a retry would land here too —
    which is the point: there is no accounting hole for one to hide in.
    """

    reference: int = 0
    candidate: int = 0
    per_task: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, *, arm: EvaluationArmRole, task_id: str) -> None:
        if arm is EvaluationArmRole.REFERENCE:
            self.reference += 1
        else:
            self.candidate += 1
        slot = self.per_task.setdefault(task_id, {"reference": 0, "candidate": 0})
        slot[arm.value] += 1

    @property
    def total(self) -> int:
        return self.reference + self.candidate

    def accounting_blockers(self, *, task_count: int) -> tuple[str, ...]:
        """Refuse any run whose generation count is not exactly one per arm per task."""
        problems: list[str] = []
        if self.reference != task_count:
            problems.append(
                f"the reference arm produced {self.reference} generations for "
                f"{task_count} tasks; Protocol V4 is exactly one per arm per task")
        if self.candidate != task_count:
            problems.append(
                f"the candidate arm produced {self.candidate} generations for "
                f"{task_count} tasks; Protocol V4 is exactly one per arm per task")
        for task_id, counts in sorted(self.per_task.items()):
            if counts["reference"] != 1 or counts["candidate"] != 1:
                problems.append(
                    f"task {task_id!r} was generated "
                    f"{counts['reference']}x reference / {counts['candidate']}x "
                    f"candidate; a second answer to one task is a retry, and no retry "
                    f"is authorised")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "reference_generations": self.reference,
            "candidate_generations": self.candidate,
            "total_generations": self.total,
            "generations_per_task": {k: dict(v) for k, v in sorted(self.per_task.items())},
        }


class AdapterActivation(str, Enum):
    """The only states an arm's adapter may be observed in. ``BOTH_ACTIVE`` is absent."""

    NO_ADAPTER = "no_adapter"
    REFERENCE_ACTIVE = "reference_active"
    CANDIDATE_ACTIVE = "candidate_active"
    CLEAN = "clean"


#: The complete transition table. Any edge not listed is a refusal, which is how
#: ``REFERENCE_ACTIVE -> CANDIDATE_ACTIVE`` — the shape of a residue bug — is caught.
_ACTIVATION_EDGES: dict[AdapterActivation, frozenset[AdapterActivation]] = {
    AdapterActivation.NO_ADAPTER: frozenset(
        {AdapterActivation.REFERENCE_ACTIVE, AdapterActivation.CANDIDATE_ACTIVE}),
    AdapterActivation.REFERENCE_ACTIVE: frozenset({AdapterActivation.CLEAN}),
    AdapterActivation.CANDIDATE_ACTIVE: frozenset({AdapterActivation.CLEAN}),
    AdapterActivation.CLEAN: frozenset(
        {AdapterActivation.REFERENCE_ACTIVE, AdapterActivation.CANDIDATE_ACTIVE}),
}


@dataclass
class AdapterActivationLog:
    """An explicit state machine over adapter activation, asserted per generation.

    The backend already guarantees isolation structurally — ``LoadStrategy.ISOLATED``
    reloads the base model inside every ``generate()`` and releases it in a ``finally``,
    so two arms cannot share a model object at all. This log does not replace that
    guarantee; it makes it OBSERVABLE, so "no residue" is a recorded sequence rather
    than an argument about a call graph.
    """

    state: AdapterActivation = AdapterActivation.NO_ADAPTER
    events: list[dict] = field(default_factory=list)

    def activate(self, arm: EvaluationArmRole, *, task_id: str) -> None:
        target = (AdapterActivation.REFERENCE_ACTIVE
                  if arm is EvaluationArmRole.REFERENCE
                  else AdapterActivation.CANDIDATE_ACTIVE)
        self._to(target, task_id=task_id)

    def clean(self, *, task_id: str) -> None:
        self._to(AdapterActivation.CLEAN, task_id=task_id)

    def _to(self, target: AdapterActivation, *, task_id: str) -> None:
        if target not in _ACTIVATION_EDGES[self.state]:
            raise RunnerV4Error(
                f"adapter activation: {self.state.value} -> {target.value} is not a "
                f"legal transition (task {task_id!r}). Reaching one arm's active state "
                f"without passing through CLEAN is adapter residue, and a measurement "
                f"taken in that state attributes one adapter's output to the other")
        self.events.append({"task_id": task_id, "from": self.state.value,
                            "to": target.value})
        self.state = target

    def to_dict(self) -> dict:
        return {
            "final_state": self.state.value,
            "transition_count": len(self.events),
            "both_active_observed": False,
            "states_observed": sorted({e["to"] for e in self.events}),
        }

    def activation_hash(self) -> str:
        return sha256_obj({"events": self.events, **self.to_dict()})


# ══════════════════════════════════════════════════════════════════════════════
#  Timeout enforcement (D33 is closed for the V4 path, and only for it)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TimeoutOutcome:
    """What a deadline-bounded invocation produced."""

    result: EvaluationResult
    timed_out: bool
    elapsed_ms: int
    abandoned_worker: bool = False


def _timeout_result(request: EvaluationRequest, *, backend_id: str, timeout_s: float,
                    elapsed_ms: int) -> EvaluationResult:
    """A timed-out generation, recorded as a failure that STAYS IN THE SAMPLE."""
    return EvaluationResult(
        backend_id=backend_id, backend_version="unknown", role=request.role,
        task_id=request.task.task_id, task_hash=request.task.task_hash,
        status=BackendStatus.FAILED, error_category=BackendErrorCategory.TIMEOUT,
        error_message=(f"the arm exceeded the plan-bound wall-clock deadline of "
                       f"{timeout_s:.0f}s and was abandoned"),
        finish_reason=FinishReason.TIMEOUT, latency_ms=elapsed_ms,
        cleanup_status=CleanupStatus.UNKNOWN,
        request_parity_hash=request.parity_hash())


def invoke_with_deadline(backend: object, request: EvaluationRequest, *,
                         timeout_s: float) -> TimeoutOutcome:
    """Call one backend under a REAL wall-clock deadline.

    D33 recorded that ``timeout_s`` was plan-bound and unenforced, which made
    ``timeout_rate`` structurally vacuous. This closes that for the V4 path by measuring
    the clock rather than trusting the backend to.

    A Python thread cannot be killed, so a straggler is ABANDONED rather than stopped and
    the fact is reported as ``abandoned_worker``. That is the honest shape: the run
    stops waiting, the task stays in the denominator as a TIMEOUT, and the caller learns
    that a worker may still be holding a model. Claiming the work was cancelled would be
    a stronger statement than the runtime can support.

    A fresh executor per call means an abandoned worker can never serve a later
    invocation, which is the property that matters for arm isolation.
    """
    if timeout_s is None or float(timeout_s) <= 0:
        raise RunnerV4Error(
            "v4 runner: a non-positive timeout is not enforcement. The plan binds "
            "timeout_s and this path measures it")
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=1,
                                  thread_name_prefix="m62-v4-arm")
    try:
        future = executor.submit(backend.generate, request)  # type: ignore[attr-defined]
        try:
            result = future.result(timeout=float(timeout_s))
        except FutureTimeout:
            elapsed = int((time.monotonic() - started) * 1000)
            future.cancel()
            return TimeoutOutcome(
                result=_timeout_result(
                    request, backend_id=str(getattr(backend, "backend_id", "unknown")),
                    timeout_s=float(timeout_s), elapsed_ms=elapsed),
                timed_out=True, elapsed_ms=elapsed, abandoned_worker=True)
    finally:
        # Never block on a straggler: shutting down with wait=True would reintroduce the
        # unbounded wait the deadline exists to remove.
        executor.shutdown(wait=False, cancel_futures=True)
    return TimeoutOutcome(result=result, timed_out=False,
                          elapsed_ms=int((time.monotonic() - started) * 1000))


# ══════════════════════════════════════════════════════════════════════════════
#  The V4 paired run
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class V4RunEvidence:
    """Everything the receipt needs about HOW the pair was obtained. Body-free."""

    binding: V4ArmBinding
    ledger: GenerationLedger
    activation: AdapterActivationLog
    arm_order_policy: str
    arm_order_assignment_hash: str
    reference_first_count: int
    candidate_first_count: int
    timeout_s: float
    timeout_enforced: bool
    timed_out_generations: int
    abandoned_workers: int
    parity_proved_tasks: int
    cross_arm_context_checks: int

    def to_dict(self) -> dict:
        return {
            "runner_v4_version": RUNNER_V4_VERSION,
            "arm_binding": self.binding.to_dict(),
            "arm_binding_hash": self.binding.binding_hash(),
            "generation_accounting": self.ledger.to_dict(),
            "adapter_activation": self.activation.to_dict(),
            "adapter_activation_hash": self.activation.activation_hash(),
            "arm_order_policy": self.arm_order_policy,
            "arm_order_assignment_hash": self.arm_order_assignment_hash,
            "reference_first_count": self.reference_first_count,
            "candidate_first_count": self.candidate_first_count,
            "timeout_s": self.timeout_s,
            "timeout_enforced": self.timeout_enforced,
            "timed_out_generations": self.timed_out_generations,
            "abandoned_workers": self.abandoned_workers,
            "parity_proved_tasks": self.parity_proved_tasks,
            "cross_arm_context_checks": self.cross_arm_context_checks,
            "retry_authorized": False,
            "quality_retries": 0,
        }

    def evidence_hash(self) -> str:
        return sha256_obj(self.to_dict())


def _arm_request(*, task: EvaluationTask, generation: GenerationPolicy,
                 baseline: BaseModelEvaluationReference,
                 adapter: AdapterEvaluationReference, directory: Path | None,
                 ceilings: ResourceCeilings, token: CancellationToken,
                 model_cache_root: Path | None) -> EvaluationRequest:
    """One arm's request. BOTH arms are built by this function, from one call site.

    Two call sites would be two chances for the arms to drift apart. One function called
    twice with different adapters is the single-axis property expressed as code.
    """
    return EvaluationRequest(
        role=EvaluationRole.CANDIDATE, task=task, generation=generation,
        baseline=baseline, adapter=adapter, resources=ceilings, cancellation=token,
        adapter_directory=directory, model_cache_root=model_cache_root)


def run_paired_v4_evaluation(
        pack: EvaluationTaskPack, *,
        reference_backend: object,
        candidate_backend: object,
        pairing: ReferenceAdapterPairing,
        baseline_reference: BaseModelEvaluationReference,
        reference_adapter: AdapterEvaluationReference,
        candidate_adapter: AdapterEvaluationReference,
        reference_adapter_directory: Path,
        candidate_adapter_directory: Path,
        generation: GenerationPolicy,
        seed: int,
        resources: ResourceCeilings | None = None,
        model_cache_root: Path | None = None,
        cancellation: CancellationToken | None = None,
        before_first_model_facing_invoke: "Callable[[dict], None] | None" = None
        ) -> tuple[PairedRun, V4RunEvidence]:
    """Answer every frozen task with BOTH adapters, exactly once each.

    Returns the ordinary :class:`PairedRun` the frozen measurement layer consumes — with
    the reference arm in the ``baseline`` slot — plus the V4 evidence that says which
    adapter that slot held and proves the accounting.

    ``before_first_model_facing_invoke`` fires exactly once, after the first task's two
    requests exist and their parity is proved, and immediately before the first backend
    call. It is where a caller makes the holdout spend durable. If it raises, no backend
    is called and nothing is spent.
    """
    reference_backend = require_backend(reference_backend)
    candidate_backend = require_backend(candidate_backend)
    if reference_backend is candidate_backend:
        raise RunnerV4Error(
            "v4 runner: both arms were handed the same backend object. Which adapter "
            "produced which answer would then be a claim about what one object was "
            "toggled to, and a toggle that failed to reset would measure one adapter "
            "twice")
    if not isinstance(pack, EvaluationTaskPack):
        raise RunnerV4Error("v4 runner: pack must be an EvaluationTaskPack")
    if not isinstance(pairing, ReferenceAdapterPairing):
        raise RunnerV4Error("v4 runner: pairing must be a ReferenceAdapterPairing")

    # Ordered worst-first. "the two arms are the same thing" is a more fundamental
    # failure than "the plan describes a different experiment", and reporting the
    # fundamental one first is what stops an operator fixing the wrong problem.
    if reference_adapter.adapter_manifest_hash == candidate_adapter.adapter_manifest_hash:
        raise RunnerV4Error(
            "v4 runner: both arms resolve to the same adapter manifest; a comparison of "
            "an adapter with itself measures nothing")
    ref_dir = Path(reference_adapter_directory).resolve()
    cand_dir = Path(candidate_adapter_directory).resolve()
    if ref_dir == cand_dir:
        raise RunnerV4Error(
            "v4 runner: both arms were pointed at the same adapter directory; the "
            "digests differ but the bytes that would load are the same")
    # The pairing is the authority on which adapter is which. Cross-check it against the
    # references that will actually be loaded, so a caller cannot describe one experiment
    # and run another.
    if pairing.reference.adapter_manifest_hash != reference_adapter.adapter_manifest_hash:
        raise RunnerV4Error(
            "v4 runner: the pairing's reference arm and the reference adapter reference "
            "name different manifests; the plan describes a different experiment from "
            "the one about to run")
    if pairing.candidate.adapter_manifest_hash != candidate_adapter.adapter_manifest_hash:
        raise RunnerV4Error(
            "v4 runner: the pairing's candidate arm and the candidate adapter reference "
            "name different manifests")

    ceilings = resources or ResourceCeilings()
    token = cancellation or CancellationToken()
    if len(pack) > ceilings.max_tasks:
        raise RunnerV4Error(
            f"v4 runner: {len(pack)} tasks exceeds the ceiling {ceilings.max_tasks}")
    # One policy object, handed to both arms, asserted against itself to fix the shared
    # digest every result is checked against.
    assert_identical_policies(generation, generation)
    timeout_s = float(getattr(generation, "timeout_s", 0) or 0)

    binding = V4ArmBinding(pairing=pairing,
                           reference_run_id=reference_adapter.run_id,
                           candidate_run_id=candidate_adapter.run_id)
    ledger = GenerationLedger()
    activation = AdapterActivationLog()

    generations: list[PairedGeneration] = []
    warnings: list[str] = []
    interrupted = False
    committed = False
    timed_out = 0
    abandoned = 0
    parity_proved = 0
    context_checks = 0
    # Kept ONLY to prove containment. Never fed to a model, never written to an artefact,
    # and dropped when the run returns.
    reference_outputs: list[str] = []
    candidate_outputs: list[str] = []

    def invoke(backend: object, request: EvaluationRequest,
               arm: EvaluationArmRole) -> EvaluationResult:
        """The lowest model-facing boundary. Counted here and nowhere else."""
        nonlocal timed_out, abandoned
        activation.activate(arm, task_id=request.task.task_id)
        ledger.record(arm=arm, task_id=request.task.task_id)
        try:
            outcome = invoke_with_deadline(backend, request, timeout_s=timeout_s)
        except InterruptionRequested:
            activation.clean(task_id=request.task.task_id)
            raise
        except Exception as exc:  # noqa: BLE001 — a crash is a datum, not a disappearance
            activation.clean(task_id=request.task.task_id)
            return EvaluationResult(
                backend_id=str(getattr(backend, "backend_id", "unknown")),
                backend_version="unknown", role=request.role,
                task_id=request.task.task_id, task_hash=request.task.task_hash,
                status=BackendStatus.FAILED,
                error_category=BackendErrorCategory.BACKEND,
                error_message=f"{type(exc).__name__}: {exc}",
                finish_reason=FinishReason.ERROR,
                cleanup_status=CleanupStatus.UNKNOWN,
                request_parity_hash=request.parity_hash())
        activation.clean(task_id=request.task.task_id)
        if outcome.timed_out:
            timed_out += 1
        if outcome.abandoned_worker:
            abandoned += 1
        result = outcome.result
        if not isinstance(result, EvaluationResult):
            return EvaluationResult(
                backend_id=str(getattr(backend, "backend_id", "unknown")),
                backend_version="unknown", role=request.role,
                task_id=request.task.task_id, task_hash=request.task.task_hash,
                status=BackendStatus.FAILED,
                error_category=BackendErrorCategory.BACKEND,
                error_message=(f"the backend returned {type(result).__name__}, not an "
                               f"EvaluationResult; a raw dictionary is never "
                               f"authoritative"),
                finish_reason=FinishReason.ERROR,
                cleanup_status=CleanupStatus.UNKNOWN,
                request_parity_hash=request.parity_hash())
        return result

    for task in pack.tasks:
        order = v4_execution_order(task, seed=seed)
        ref_request = _arm_request(
            task=task, generation=generation, baseline=baseline_reference,
            adapter=reference_adapter, directory=ref_dir, ceilings=ceilings,
            token=token, model_cache_root=model_cache_root)
        cand_request = _arm_request(
            task=task, generation=generation, baseline=baseline_reference,
            adapter=candidate_adapter, directory=cand_dir, ceilings=ceilings,
            token=token, model_cache_root=model_cache_root)

        # Parity. Both arms were built by one function from one task, one policy and one
        # base reference, so this must hold — and it is checked rather than assumed,
        # because "must hold" is what every silently-broken comparison also said.
        parity = ref_request.parity_hash()
        if parity != cand_request.parity_hash():
            raise RunnerV4Error(
                f"v4 runner: task {task.task_id!r} would be asked differently of the two "
                f"adapters ({short(parity)} vs {short(cand_request.parity_hash())}); a "
                f"comparison between two different questions measures nothing")
        parity_proved += 1

        # The containment property, checked in BOTH directions before either arm runs.
        # Each arm answers the frozen task, never the other arm's attempt at it.
        try:
            assert_no_cross_arm_context(prompt=task.user_prompt,
                                        other_arm_outputs=candidate_outputs)
            assert_no_cross_arm_context(prompt=task.user_prompt,
                                        other_arm_outputs=reference_outputs)
            if task.system_prompt:
                assert_no_cross_arm_context(prompt=task.system_prompt,
                                            other_arm_outputs=candidate_outputs)
                assert_no_cross_arm_context(prompt=task.system_prompt,
                                            other_arm_outputs=reference_outputs)
        except ProtocolV4Error as exc:
            raise RunnerV4Error(
                f"v4 runner: cross-arm containment failed on task {task.task_id!r}: "
                f"{exc}") from None
        context_checks += 1

        # ── the model-facing boundary ────────────────────────────────────────
        if before_first_model_facing_invoke is not None and not committed:
            before_first_model_facing_invoke({
                "task_id": task.task_id,
                "task_hash": task.task_hash,
                "first_arm": (EvaluationArmRole.REFERENCE.value
                              if order is V4ArmOrder.REFERENCE_FIRST
                              else EvaluationArmRole.CANDIDATE.value),
                "order": order.value,
                "parity_hash": parity,
                "task_count": len(pack),
                "order_policy": ORDER_POLICY_BALANCED,
                "order_assignment_hash": order_assignment_hash(pack, seed=seed),
                "pairing_hash": pairing.pairing_hash(),
                "arm_binding_hash": binding.binding_hash(),
            })
            committed = True

        try:
            if order is V4ArmOrder.REFERENCE_FIRST:
                ref_result = invoke(reference_backend, ref_request,
                                    EvaluationArmRole.REFERENCE)
                cand_result = invoke(candidate_backend, cand_request,
                                     EvaluationArmRole.CANDIDATE)
            else:
                cand_result = invoke(candidate_backend, cand_request,
                                     EvaluationArmRole.CANDIDATE)
                ref_result = invoke(reference_backend, ref_request,
                                    EvaluationArmRole.REFERENCE)
        except InterruptionRequested as exc:
            interrupted = True
            warnings.append(f"interrupted during {task.task_id!r}: {exc}")
            break

        if ref_result.response_text:
            reference_outputs.append(ref_result.response_text)
        if cand_result.response_text:
            candidate_outputs.append(cand_result.response_text)

        blockers: list[str] = []
        for result, request in ((ref_result, ref_request), (cand_result, cand_request)):
            try:
                check_result_matches_request(result, request)
            except Exception as exc:  # noqa: BLE001 — the message IS the finding
                blockers.append(str(exc))

        # ── request role -> ARM SLOT, after the request check and never before ──
        # Two different things share the name "role" and this is where they part.
        #
        # On the REQUEST it means "what kind of arm is this", and under Protocol V4 both
        # arms are CANDIDATE because both are adapter-bearing. That is what lets both
        # arms take the identical backend branch, and ``check_result_matches_request``
        # above has just used it to prove each backend answered the request it was given.
        #
        # On the RESULT, every downstream consumer reads it as WHICH SLOT OF THE
        # COMPARISON this is: ``compare_pair`` fills ``baseline_score``/``candidate_score``
        # from it, ``build_arm_metrics`` labels the bundle with it, and
        # ``build_score_evidence`` REFUSES to file a score whose role disagrees with its
        # slot -- correctly, because that refusal is what stops one arm's answer being
        # attributed to the other.
        #
        # So arm 0 is relabelled to BASELINE here, meaning "arm 0", which is exactly what
        # the frozen measurement layer means by the word. It is not a claim that no
        # adapter was attached: that claim lives in ``V4ArmBinding``, which records
        # ``baseline_slot_is_a_bare_base_model: False`` and both arms' digests.
        ref_result = _as_arm_slot(ref_result, EvaluationRole.BASELINE)
        cand_result = _as_arm_slot(cand_result, EvaluationRole.CANDIDATE)

        status, pair_warnings = _classify(ref_result, cand_result, ceilings)
        if blockers:
            status = PairedStatus.INSUFFICIENT_EVIDENCE
        generations.append(PairedGeneration(
            task_id=task.task_id, task_hash=task.task_hash, status=status,
            order=(ExecutionOrder.BASELINE_FIRST if order is V4ArmOrder.REFERENCE_FIRST
                   else ExecutionOrder.CANDIDATE_FIRST),
            parity_hash=parity, baseline=ref_result, candidate=cand_result,
            blockers=tuple(blockers), warnings=pair_warnings))

    cleanup = _release_both_v4(reference_backend, candidate_backend)
    balance = order_balance(pack, seed=seed)
    warnings.append(f"arm order: {balance[0]} reference-first, "
                    f"{balance[1]} candidate-first")
    if timed_out:
        warnings.append(f"{timed_out} generation(s) exceeded the {timeout_s:.0f}s "
                        f"wall-clock deadline and stayed in the denominator")

    run = PairedRun(
        generations=tuple(generations), order_policy=ORDER_POLICY_BALANCED,
        order_assignment_hash=order_assignment_hash(pack, seed=seed),
        baseline_backend_id=str(getattr(reference_backend, "backend_id", "")),
        candidate_backend_id=str(getattr(candidate_backend, "backend_id", "")),
        backend_version=str(reference_backend.version()), cleanup_status=cleanup,
        interrupted=interrupted, warnings=tuple(warnings))

    accounting = ledger.accounting_blockers(task_count=len(pack)) if not interrupted else ()
    coverage = run.coverage_blockers(pack)
    extra = (("the run was interrupted; a partial comparison is not a measurement of "
              "either adapter",) if interrupted else ())
    run = PairedRun(**{**run.__dict__,
                       "blockers": extra + coverage + accounting})

    evidence = V4RunEvidence(
        binding=binding, ledger=ledger, activation=activation,
        arm_order_policy=ORDER_POLICY_BALANCED,
        arm_order_assignment_hash=order_assignment_hash(pack, seed=seed),
        reference_first_count=balance[0], candidate_first_count=balance[1],
        timeout_s=timeout_s, timeout_enforced=True, timed_out_generations=timed_out,
        abandoned_workers=abandoned, parity_proved_tasks=parity_proved,
        cross_arm_context_checks=context_checks)
    return run, evidence


def _as_arm_slot(result: EvaluationResult, slot: EvaluationRole) -> EvaluationResult:
    """Stamp the comparison SLOT onto a result, changing nothing else.

    Rebuilt through the dataclass rather than mutated, so every field keeps its
    validation and the parity hash the backend recorded travels with it untouched.
    """
    if result.role is slot:
        return result
    return replace(result, role=slot)


def _release_both_v4(*backends: object) -> CleanupStatus:
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


def v4_arm_results(run: PairedRun, arm: EvaluationArmRole
                   ) -> tuple[EvaluationResult, ...]:
    """Every result for one V4 arm, in pack order. Includes failures.

    Reads the slot rather than the result's ``role``: under Protocol V4 both arms carry
    ``role=CANDIDATE`` at the backend layer, because both are adapter-bearing requests.
    The arm is the slot, and the slot is bound by :class:`V4ArmBinding`.
    """
    out: list[EvaluationResult] = []
    for pair in run.generations:
        result = (pair.baseline if arm is EvaluationArmRole.REFERENCE else pair.candidate)
        if result is not None:
            out.append(result)
    return tuple(out)


__all__ = [
    "AdapterActivation", "AdapterActivationLog", "GenerationLedger",
    "RUNNER_V4_VERSION", "RunnerV4Error", "TimeoutOutcome", "V4ArmBinding",
    "V4ArmOrder", "V4RunEvidence", "invoke_with_deadline", "run_paired_v4_evaluation",
    "v4_arm_results", "v4_execution_order",
]
