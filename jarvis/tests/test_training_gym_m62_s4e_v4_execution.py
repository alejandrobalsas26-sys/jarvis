"""V69 M62 S4E — the wired Protocol V4 execution path, proved against doubles.

WHAT IS BEING PROVED
--------------------
That a reference-adapter paired evaluation can only run as ONE experiment with ONE axis:
two adapters, one base, one frozen pack, one answer per arm per task, one holdout spend,
no retry, no residue and no cross-arm leakage. Every property is asserted against the
production code path with deterministic doubles standing in for the model.

WHY THE DOUBLES ARE KEYED ON THE ADAPTER, NOT THE ROLE
------------------------------------------------------
``FakeEvaluationBackend`` distinguishes arms by ``request.role``. Under Protocol V4 both
arms carry ``role=CANDIDATE`` — because both are adapter-bearing requests — so a
role-keyed double would emit identical bytes for both arms and could not detect a swap
or a residue at all. :class:`_MarkerBackend` therefore derives its answer from the
ADAPTER it was handed. That is the only arrangement under which "the reference arm never
emitted the candidate's marker" is a measurement rather than a restatement.

WHAT IS NOT BEING PROVED
------------------------
That the production backend generates correctly. No test here imports torch,
transformers or peft, loads a weight, or produces a token. Those claims belong to a live
run under an operator authority, and this file makes none of them.
"""
from __future__ import annotations

import sys
import time

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.evaluation import runner_v4 as V4
from training_gym.evaluation.backend import (
    BackendErrorCategory,
    BackendStatus,
    CleanupStatus,
    EvaluationBackendError,
    EvaluationRequest,
    EvaluationResult,
    FinishReason,
)
from training_gym.evaluation.generation import GenerationPolicy
from training_gym.evaluation.policy import ResourceCeilings
from training_gym.evaluation.protocol_v4 import (
    AdapterArmReference,
    EvaluationArmRole,
    ProtocolV4Error,
    ReferenceAdapterPairing,
)
from training_gym.evaluation.references import (
    AdapterEvaluationReference,
    BaseModelEvaluationReference,
    EvaluationRole,
    base_reference_from_identity,
    reference_from_manifest,
)
from training_gym.evaluation.task_pack import (
    EvaluationTask,
    EvaluationTaskKind,
    EvaluationTaskPack,
)
from training_gym.task_spec import TaskFamily
from training_gym.training.artifacts import AdapterManifest
from training_gym.training.model_identity import CacheStatus, ModelIdentity

_BANNED = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
           "bitsandbytes", "safetensors")
_REV = "c1899de289a04d12100db370d81485cdf75e47ca"

#: The two markers. Distinguishable, deterministic, and long enough that the cross-arm
#: containment check (which ignores fragments under 32 characters) would actually fire.
_REFERENCE_MARKER = "REFERENCE_ARM_MARKER_ALPHA_0123456789"
_CANDIDATE_MARKER = "CANDIDATE_ARM_MARKER_BRAVO_9876543210"


# ── fixtures ──────────────────────────────────────────────────────────────────
def _identity() -> ModelIdentity:
    return ModelIdentity(provider="huggingface", model_id="Qwen/Qwen3-0.6B",
                         revision=_REV, parameters_b=0.6,
                         tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=_REV,
                         cache_status=CacheStatus.PRESENT, cache_evidence="c" * 64)


def _baseline() -> BaseModelEvaluationReference:
    return base_reference_from_identity(_identity())


def _manifest(run_id: str, *, seed_char: str) -> AdapterManifest:
    base = _identity()
    return AdapterManifest(
        run_id=run_id, run_state="completed", plan_hash=seed_char * 64,
        training_config_hash="2" * 64, method="sft_lora",
        backend_id="transformers_peft", backend_version="m62.1",
        base_model_id="Qwen/Qwen3-0.6B", base_model_revision=_REV,
        base_model_identity_hash=base.identity_hash(),
        tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=_REV,
        tokenizer_identity_hash=base.tokenizer_identity_hash(),
        tokenizer_chat_template_hash="3" * 64, dataset_id="m62-defensive-quality-train",
        dataset_version="v2", dataset_reference_hash="4" * 64,
        dataset_manifest_hash="5" * 64, train_shard_hash="6" * 64,
        validation_shard_hash="7" * 64, hidden_evaluation_hash="8" * 64,
        security_regression_hash="9" * 64, export_manifest_hash="a" * 64,
        split_algorithm_version="m62.split.1", seed=11,
        lora={"r": 16, "lora_alpha": 32}, assistant_only_loss={"enabled": True},
        package_versions={"peft": "0.20.0"}, device_category="cpu", precision="fp32",
        requested_steps=10, completed_steps=10, epochs_completed=1.0, train_loss=0.5,
        eval_loss=0.6, truncated_records=0, duration_seconds=12.0, files=(),
        total_bytes=1024, tree_hash=seed_char * 64,
        created_at_utc="2026-08-02T00:00:00Z")


def _reference_adapter() -> AdapterEvaluationReference:
    return reference_from_manifest(_manifest("qwen3-06b-lora-quality-live-004",
                                             seed_char="a"), artifact_verified=True)


def _candidate_adapter() -> AdapterEvaluationReference:
    return reference_from_manifest(_manifest("qwen3-06b-lora-quality-live-005",
                                             seed_char="b"), artifact_verified=True)


def _arm(adapter: AdapterEvaluationReference, role: EvaluationArmRole,
         *, digest: str) -> AdapterArmReference:
    base = _identity()
    return AdapterArmReference(
        role=role, candidate_id=adapter.run_id, run_id=adapter.run_id,
        adapter_sha256=digest * 64, adapter_manifest_hash=adapter.adapter_manifest_hash,
        adapter_artifact_set_hash=adapter.adapter_artifact_tree_hash,
        training_receipt_sha256=digest * 64,
        base_model_id="Qwen/Qwen3-0.6B", base_model_revision=_REV,
        base_model_identity_hash=base.identity_hash(),
        tokenizer_identity_hash=base.tokenizer_identity_hash(),
        tokenizer_chat_template_hash="3" * 64)


def _pairing() -> ReferenceAdapterPairing:
    return ReferenceAdapterPairing(
        reference=_arm(_reference_adapter(), EvaluationArmRole.REFERENCE, digest="a"),
        candidate=_arm(_candidate_adapter(), EvaluationArmRole.CANDIDATE, digest="b"))


def _task(index: int) -> EvaluationTask:
    kind = EvaluationTaskKind.COMPLETION
    split = DatasetSplit.HIDDEN_EVALUATION
    family = TaskFamily.SOC_TRIAGE
    if index % 6 == 4:
        kind, split, family = (EvaluationTaskKind.REQUIRED_REFUSAL,
                               DatasetSplit.SECURITY_REGRESSION,
                               TaskFamily.SAFETY_REFUSAL)
    elif index % 6 == 5:
        kind, split = EvaluationTaskKind.REQUIRED_COMPLETION, DatasetSplit.SECURITY_REGRESSION
    return EvaluationTask(
        task_id=f"synthetic-{index:04d}", task_family=family,
        task_hash=f"{index:064x}", split=split, kind=kind,
        system_prompt="You are a SOC analyst.",
        user_prompt=f"Triage synthetic alert {index}.",
        source_dataset_manifest_hash="a" * 64, source_shard_hash="b" * 64,
        input_record_hash="c" * 64, evidence_ids=("e" * 64, "f" * 64),
        grader_ids=("json_schema", "secret_pii"),
        mandatory_grader_ids=("json_schema", "secret_pii"),
        refusal_expected=kind is EvaluationTaskKind.REQUIRED_REFUSAL,
        security_required=split is DatasetSplit.SECURITY_REGRESSION,
        lineage_group="alerts")


def _pack(n: int = 36) -> EvaluationTaskPack:
    return EvaluationTaskPack(tasks=tuple(_task(i) for i in range(n)),
                              dataset_id="m62-defensive-eval", dataset_version="v7")


# ── the adapter-keyed double ──────────────────────────────────────────────────
class _MarkerBackend:
    """Emits a marker derived from the ADAPTER it was handed, never from the role.

    This is the instrument for the contamination test: if the two arms ever share model
    state, or an arm is handed the wrong adapter, the marker in the recorded response is
    the wrong one and the assertion fails.
    """

    backend_id = "marker_double"

    def __init__(self, *, sleep_s: float = 0.0, raises: bool = False) -> None:
        self.sleep_s = sleep_s
        self.raises = raises
        self.seen: list[tuple[str, str]] = []
        self.released = 0

    def version(self) -> str:
        return "m62.marker_double.1"

    def readiness(self, request: EvaluationRequest) -> tuple[str, ...]:
        return ()

    def generate(self, request: EvaluationRequest) -> EvaluationResult:
        if self.raises:
            raise RuntimeError("synthetic backend failure")
        if self.sleep_s:
            time.sleep(self.sleep_s)
        adapter = request.adapter
        assert adapter is not None, (
            "under Protocol V4 every arm is adapter-bearing; an arm reaching a backend "
            "with adapter=None is a bare-base arm mislabelled as a reference")
        marker = (_REFERENCE_MARKER if adapter.run_id.endswith("004")
                  else _CANDIDATE_MARKER)
        self.seen.append((request.task.task_id, adapter.run_id))
        return EvaluationResult(
            backend_id=self.backend_id, backend_version=self.version(),
            role=request.role, task_id=request.task.task_id,
            task_hash=request.task.task_hash, status=BackendStatus.SUCCEEDED,
            response_text=('{"summary": "' + marker + '", "severity": "low", '
                           '"evidence": ["' + "e" * 64 + '"]}'),
            input_tokens=42, output_tokens=24, latency_ms=5,
            finish_reason=FinishReason.END_OF_SEQUENCE,
            cleanup_status=CleanupStatus.RELEASED,
            request_parity_hash=request.parity_hash())

    def release(self) -> CleanupStatus:
        self.released += 1
        return CleanupStatus.RELEASED


def _run(pack=None, *, seed: int = 11, timeout_s: float = 300.0,
         reference_backend=None, candidate_backend=None, **kwargs):
    pack = pack or _pack()
    return V4.run_paired_v4_evaluation(
        pack,
        reference_backend=reference_backend or _MarkerBackend(),
        candidate_backend=candidate_backend or _MarkerBackend(),
        pairing=_pairing(), baseline_reference=_baseline(),
        reference_adapter=_reference_adapter(),
        candidate_adapter=_candidate_adapter(),
        reference_adapter_directory="/tmp/m62-synthetic/reference",
        candidate_adapter_directory="/tmp/m62-synthetic/candidate",
        generation=GenerationPolicy(seed=seed, timeout_s=int(timeout_s)),
        seed=seed, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════
#  No framework is imported by this path
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v4_runner_imports_no_machine_learning_framework():
    _run()
    assert sorted(set(_BANNED) & set(sys.modules)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  Single axis
# ══════════════════════════════════════════════════════════════════════════════
def test_both_arms_are_adapter_bearing_and_differ_only_in_the_adapter():
    """The whole experiment, expressed as a diff between two requests."""
    reference, candidate = _MarkerBackend(), _MarkerBackend()
    _run(_pack(4), reference_backend=reference, candidate_backend=candidate)
    # Every reference invocation saw 004; every candidate invocation saw 005.
    assert {run_id for _, run_id in reference.seen} == {"qwen3-06b-lora-quality-live-004"}
    assert {run_id for _, run_id in candidate.seen} == {"qwen3-06b-lora-quality-live-005"}


def test_the_two_arms_parity_hashes_are_equal_for_every_task():
    """Parity excludes role and adapter, so two adapters on one base agree exactly."""
    pack = _pack(8)
    for task in pack.tasks:
        common = dict(task=task, generation=GenerationPolicy(seed=11),
                      baseline=_baseline(), role=EvaluationRole.CANDIDATE)
        ref = EvaluationRequest(adapter=_reference_adapter(), **common)
        cand = EvaluationRequest(adapter=_candidate_adapter(), **common)
        assert ref.parity_hash() == cand.parity_hash()


def test_a_baseline_role_arm_still_cannot_carry_an_adapter():
    """The v1-v3 refusal is untouched. V4 goes around it, never through it."""
    with pytest.raises(EvaluationBackendError, match="baseline arm may not carry"):
        EvaluationRequest(role=EvaluationRole.BASELINE, task=_task(0),
                          generation=GenerationPolicy(), baseline=_baseline(),
                          adapter=_candidate_adapter())


# ══════════════════════════════════════════════════════════════════════════════
#  Adapter contamination
# ══════════════════════════════════════════════════════════════════════════════
def test_the_reference_arm_never_emits_the_candidate_marker():
    run, _ = _run()
    for pair in run.generations:
        assert _REFERENCE_MARKER in pair.baseline.response_text
        assert _CANDIDATE_MARKER not in pair.baseline.response_text


def test_the_candidate_arm_never_emits_the_reference_marker():
    run, _ = _run()
    for pair in run.generations:
        assert _CANDIDATE_MARKER in pair.candidate.response_text
        assert _REFERENCE_MARKER not in pair.candidate.response_text


def test_swapping_arms_across_36_tasks_leaves_no_residue_in_either_direction():
    """Both orders occur in one run, so a residue would show up whichever way round."""
    run, evidence = _run()
    assert evidence.reference_first_count > 0 and evidence.candidate_first_count > 0
    for pair in run.generations:
        assert _REFERENCE_MARKER in pair.baseline.response_text
        assert _CANDIDATE_MARKER in pair.candidate.response_text


def test_the_activation_state_machine_never_reaches_both_active():
    _, evidence = _run()
    assert evidence.activation.state is V4.AdapterActivation.CLEAN
    assert evidence.activation.to_dict()["both_active_observed"] is False
    # Every activation is separated from the next by a CLEAN.
    states = [e["to"] for e in evidence.activation.events]
    for previous, following in zip(states, states[1:]):
        assert not (previous.endswith("_active") and following.endswith("_active"))


def test_reaching_one_arm_without_passing_through_clean_is_refused():
    log = V4.AdapterActivationLog()
    log.activate(EvaluationArmRole.REFERENCE, task_id="t")
    with pytest.raises(V4.RunnerV4Error, match="not a legal transition"):
        log.activate(EvaluationArmRole.CANDIDATE, task_id="t")


# ══════════════════════════════════════════════════════════════════════════════
#  Structural refusals
# ══════════════════════════════════════════════════════════════════════════════
def test_one_backend_object_for_both_arms_is_refused():
    shared = _MarkerBackend()
    with pytest.raises(V4.RunnerV4Error, match="same backend object"):
        _run(reference_backend=shared, candidate_backend=shared)


def test_the_same_adapter_on_both_arms_is_refused():
    with pytest.raises(V4.RunnerV4Error, match="same adapter manifest"):
        V4.run_paired_v4_evaluation(
            _pack(2), reference_backend=_MarkerBackend(),
            candidate_backend=_MarkerBackend(), pairing=_pairing(),
            baseline_reference=_baseline(),
            reference_adapter=_candidate_adapter(),
            candidate_adapter=_candidate_adapter(),
            reference_adapter_directory="/tmp/m62-synthetic/a",
            candidate_adapter_directory="/tmp/m62-synthetic/b",
            generation=GenerationPolicy(seed=11), seed=11)


def test_one_directory_for_both_arms_is_refused_even_when_the_digests_differ():
    with pytest.raises(V4.RunnerV4Error, match="same adapter directory"):
        V4.run_paired_v4_evaluation(
            _pack(2), reference_backend=_MarkerBackend(),
            candidate_backend=_MarkerBackend(), pairing=_pairing(),
            baseline_reference=_baseline(),
            reference_adapter=_reference_adapter(),
            candidate_adapter=_candidate_adapter(),
            reference_adapter_directory="/tmp/m62-synthetic/same",
            candidate_adapter_directory="/tmp/m62-synthetic/same",
            generation=GenerationPolicy(seed=11), seed=11)


def test_a_pairing_that_names_a_different_adapter_than_will_load_is_refused():
    """The plan must describe the experiment that is about to run."""
    with pytest.raises(V4.RunnerV4Error, match="different manifests"):
        V4.run_paired_v4_evaluation(
            _pack(2), reference_backend=_MarkerBackend(),
            candidate_backend=_MarkerBackend(), pairing=_pairing(),
            baseline_reference=_baseline(),
            # arms swapped relative to the pairing
            reference_adapter=_candidate_adapter(),
            candidate_adapter=_reference_adapter(),
            reference_adapter_directory="/tmp/m62-synthetic/a",
            candidate_adapter_directory="/tmp/m62-synthetic/b",
            generation=GenerationPolicy(seed=11), seed=11)


# ══════════════════════════════════════════════════════════════════════════════
#  Generation accounting
# ══════════════════════════════════════════════════════════════════════════════
def test_thirty_six_tasks_produce_exactly_seventy_two_generations():
    _, evidence = _run(_pack(36))
    assert evidence.ledger.reference == 36
    assert evidence.ledger.candidate == 36
    assert evidence.ledger.total == 72


def test_every_task_is_generated_exactly_once_per_arm():
    _, evidence = _run(_pack(36))
    assert len(evidence.ledger.per_task) == 36
    assert all(counts == {"reference": 1, "candidate": 1}
               for counts in evidence.ledger.per_task.values())
    assert evidence.ledger.accounting_blockers(task_count=36) == ()


def test_a_second_answer_to_one_task_is_reported_as_a_retry():
    ledger = V4.GenerationLedger()
    ledger.record(arm=EvaluationArmRole.REFERENCE, task_id="t")
    ledger.record(arm=EvaluationArmRole.REFERENCE, task_id="t")
    ledger.record(arm=EvaluationArmRole.CANDIDATE, task_id="t")
    problems = ledger.accounting_blockers(task_count=1)
    assert any("retry" in p for p in problems)


def test_the_run_declares_no_retry_authority():
    _, evidence = _run(_pack(4))
    payload = evidence.to_dict()
    assert payload["retry_authorized"] is False
    assert payload["quality_retries"] == 0


def test_no_warmup_or_smoke_generation_happens_before_the_task_loop():
    """The first model-facing call is task 0's, and the ledger proves it."""
    reference, candidate = _MarkerBackend(), _MarkerBackend()
    pack = _pack(3)
    _run(pack, reference_backend=reference, candidate_backend=candidate)
    seen = sorted(t for t, _ in reference.seen + candidate.seen)
    assert seen == sorted([t.task_id for t in pack.tasks] * 2)


# ══════════════════════════════════════════════════════════════════════════════
#  Arm order
# ══════════════════════════════════════════════════════════════════════════════
def test_arm_order_is_deterministic_and_reproducible():
    first = [V4.v4_execution_order(t, seed=11) for t in _pack(36).tasks]
    second = [V4.v4_execution_order(t, seed=11) for t in _pack(36).tasks]
    assert first == second


def test_arm_order_is_counterbalanced_rather_than_always_reference_first():
    orders = [V4.v4_execution_order(t, seed=11) for t in _pack(36).tasks]
    assert V4.V4ArmOrder.REFERENCE_FIRST in orders
    assert V4.V4ArmOrder.CANDIDATE_FIRST in orders


def test_arm_order_depends_on_the_task_hash_and_seed_and_nothing_else():
    """Not on position, not on difficulty, not on anything about the answer."""
    task = _task(3)
    assert V4.v4_execution_order(task, seed=11) == V4.v4_execution_order(task, seed=11)
    flipped = [V4.v4_execution_order(t, seed=12) for t in _pack(36).tasks]
    straight = [V4.v4_execution_order(t, seed=11) for t in _pack(36).tasks]
    assert flipped != straight


# ══════════════════════════════════════════════════════════════════════════════
#  Cross-arm context
# ══════════════════════════════════════════════════════════════════════════════
def test_neither_arms_output_can_enter_the_other_arms_prompt():
    _, evidence = _run(_pack(36))
    assert evidence.cross_arm_context_checks == 36


def test_a_prompt_carrying_the_other_arms_answer_is_refused():
    with pytest.raises(ProtocolV4Error, match="contains output"):
        from training_gym.evaluation.protocol_v4 import assert_no_cross_arm_context
        assert_no_cross_arm_context(
            prompt="Given the earlier answer " + _CANDIDATE_MARKER + " continue.",
            other_arm_outputs=[_CANDIDATE_MARKER])


def test_a_task_whose_prompt_contains_a_prior_arm_output_stops_the_run():
    """Containment is enforced inside the loop, not merely available as a helper."""
    class _EchoBackend(_MarkerBackend):
        def generate(self, request):
            result = super().generate(request)
            return EvaluationResult(**{**result.__dict__,
                                       "response_text": request.task.user_prompt * 4})

    pack = EvaluationTaskPack(
        tasks=(_task(0),
               EvaluationTask(**{**_task(1).__dict__,
                                 "user_prompt": "Triage synthetic alert 0." * 4})),
        dataset_id="m62-defensive-eval", dataset_version="v7")
    with pytest.raises(V4.RunnerV4Error, match="cross-arm containment"):
        _run(pack, reference_backend=_EchoBackend(), candidate_backend=_EchoBackend())


# ══════════════════════════════════════════════════════════════════════════════
#  Timeout enforcement — D33 closed for this path
# ══════════════════════════════════════════════════════════════════════════════
def test_a_sleeping_backend_is_stopped_by_the_wall_clock_not_by_a_config_value():
    slow = _MarkerBackend(sleep_s=2.0)
    started = time.monotonic()
    outcome = V4.invoke_with_deadline(
        slow,
        EvaluationRequest(role=EvaluationRole.CANDIDATE, task=_task(0),
                          generation=GenerationPolicy(), baseline=_baseline(),
                          adapter=_candidate_adapter()),
        timeout_s=0.25)
    elapsed = time.monotonic() - started
    assert outcome.timed_out is True
    assert elapsed < 1.5, "the deadline did not actually bound the wait"
    assert outcome.result.finish_reason is FinishReason.TIMEOUT
    assert outcome.result.error_category is BackendErrorCategory.TIMEOUT
    assert outcome.abandoned_worker is True


def test_a_timed_out_task_stays_in_the_denominator():
    """``timeout_s`` is an integer >= 1 in the frozen policy, so this uses the floor."""
    run, evidence = _run(_pack(2), timeout_s=1,
                         reference_backend=_MarkerBackend(sleep_s=2.5),
                         candidate_backend=_MarkerBackend())
    assert len(run.generations) == 2, "a timed-out task was dropped from the sample"
    assert evidence.timed_out_generations == 2
    assert evidence.ledger.total == 4
    # The failure is recorded against the arm that actually timed out, not the pair.
    assert all(p.baseline.finish_reason is FinishReason.TIMEOUT for p in run.generations)
    assert all(p.candidate.status is BackendStatus.SUCCEEDED for p in run.generations)


def test_a_non_positive_timeout_is_refused_rather_than_treated_as_infinite():
    with pytest.raises(V4.RunnerV4Error, match="not enforcement"):
        V4.invoke_with_deadline(
            _MarkerBackend(),
            EvaluationRequest(role=EvaluationRole.CANDIDATE, task=_task(0),
                              generation=GenerationPolicy(), baseline=_baseline(),
                              adapter=_candidate_adapter()),
            timeout_s=0)


def test_timeout_enforcement_is_declared_true_in_the_evidence():
    _, evidence = _run(_pack(2))
    assert evidence.to_dict()["timeout_enforced"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  Failures stay in the sample
# ══════════════════════════════════════════════════════════════════════════════
def test_a_backend_that_raises_is_recorded_rather_than_dropped():
    run, evidence = _run(_pack(3), reference_backend=_MarkerBackend(raises=True))
    assert len(run.generations) == 3
    assert all(p.baseline.status is BackendStatus.FAILED for p in run.generations)
    assert evidence.ledger.reference == 3, "a crashed generation must still be counted"


# ══════════════════════════════════════════════════════════════════════════════
#  The model-facing boundary
# ══════════════════════════════════════════════════════════════════════════════
def test_the_commit_callback_fires_exactly_once_and_before_any_backend_call():
    calls: list[dict] = []
    reference = _MarkerBackend()

    def commit(body: dict) -> None:
        assert reference.seen == [], "a model was called before the holdout was committed"
        calls.append(body)

    _run(_pack(5), reference_backend=reference,
         before_first_model_facing_invoke=commit)
    assert len(calls) == 1
    assert calls[0]["task_count"] == 5
    assert calls[0]["first_arm"] in {"reference", "candidate"}
    assert set(calls[0]) >= {"pairing_hash", "arm_binding_hash", "order_assignment_hash"}


def test_a_commit_that_raises_stops_the_run_before_a_single_generation():
    reference, candidate = _MarkerBackend(), _MarkerBackend()

    def commit(_body: dict) -> None:
        raise RuntimeError("the ledger could not be appended")

    with pytest.raises(RuntimeError, match="ledger could not be appended"):
        _run(_pack(4), reference_backend=reference, candidate_backend=candidate,
             before_first_model_facing_invoke=commit)
    assert reference.seen == [] and candidate.seen == [], (
        "a holdout that could not be recorded as spent must not be spent")


def test_the_commit_body_carries_no_prompt_or_response(monkeypatch):
    bodies: list[dict] = []
    _run(_pack(4), before_first_model_facing_invoke=bodies.append)
    text = repr(bodies[0])
    assert "Triage synthetic alert" not in text
    assert _REFERENCE_MARKER not in text and _CANDIDATE_MARKER not in text


# ══════════════════════════════════════════════════════════════════════════════
#  The arm binding says what the slot holds
# ══════════════════════════════════════════════════════════════════════════════
def test_the_binding_states_the_baseline_slot_is_not_a_bare_base_model():
    _, evidence = _run(_pack(2))
    payload = evidence.binding.to_dict()
    assert payload["baseline_slot_is_a_bare_base_model"] is False
    assert payload["baseline_slot_holds"] == "reference_adapter_arm"
    assert payload["reference_arm"]["adapter_attached"] is True
    assert payload["candidate_arm"]["adapter_attached"] is True


def test_swapping_the_arms_changes_the_binding_hash():
    """Role is inside the arm hash, so a swap is detectable rather than plausible."""
    straight = _pairing()
    swapped = ReferenceAdapterPairing(
        reference=_arm(_candidate_adapter(), EvaluationArmRole.REFERENCE, digest="b"),
        candidate=_arm(_reference_adapter(), EvaluationArmRole.CANDIDATE, digest="a"))
    assert straight.pairing_hash() != swapped.pairing_hash()


def test_the_reference_arm_result_lands_in_the_slot_the_frozen_scorer_reads():
    run, evidence = _run(_pack(4))
    reference_results = V4.v4_arm_results(run, EvaluationArmRole.REFERENCE)
    assert len(reference_results) == 4
    assert all(_REFERENCE_MARKER in r.response_text for r in reference_results)
    assert reference_results == tuple(p.baseline for p in run.generations)


# ══════════════════════════════════════════════════════════════════════════════
#  Body-free evidence
# ══════════════════════════════════════════════════════════════════════════════
def test_the_run_evidence_carries_no_prompt_or_response_text():
    _, evidence = _run(_pack(6))
    text = repr(evidence.to_dict())
    assert "Triage synthetic alert" not in text
    assert _REFERENCE_MARKER not in text and _CANDIDATE_MARKER not in text


def test_the_evidence_hash_is_stable_across_two_identical_runs():
    _, first = _run(_pack(6))
    _, second = _run(_pack(6))
    assert first.evidence_hash() == second.evidence_hash()
