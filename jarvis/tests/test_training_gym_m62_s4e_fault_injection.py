"""V69 M62 S4E — the fault-injection gauntlet for the Protocol V4 execution path.

WHAT IS BEING PROVED
--------------------
That every way this experiment could become invalid FAILS SAFELY: it stops, it says why,
and — where it matters most — it stops with the holdout still unread. "Fails safely" is
asserted separately from "fails", because a refusal that happens after the first model
call has already spent the corpus it was meant to protect.

THE DATASET USED HERE
---------------------
``m62-defensive-eval v1`` — spent by S3E.2, ``USED_IMMUTABLE`` since, and under D35
development evidence that may never decide eligibility again. It is used as PLUMBING: a
real corpus shape for a real pack builder, driven by deterministic doubles that are not
models. Nothing here decides eligibility, and ``eval-v7`` is never built, read or named.

NOTHING HERE LOADS A WEIGHT OR GENERATES A TOKEN. Every ledger, output root and dataset
root is a temporary directory.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from training_gym.evaluation import store_v4 as SV4
from training_gym.evaluation.backend import CleanupStatus
from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.execution_v4 import (
    V4_ARM_LIMITATION,
    ExecutionV4Error,
    V4ExecutionRequest,
    execute_v4_evaluation,
)
from training_gym.evaluation.plan_v4 import PlanV4Error, V4EvaluationPlan, task_order_hash
from training_gym.evaluation.protocol_v4 import (
    EvaluationArmRole,
    PairedSpendPlan,
    ProtocolV4Error,
    ReferenceAdapterPairing,
)
from training_gym.evaluation.store import HoldoutAlreadyCommitted

_BANNED = ("torch", "transformers", "peft", "trl", "accelerate", "bitsandbytes",
           "safetensors")


# ══════════════════════════════════════════════════════════════════════════════
#  A world: a real corpus, real plumbing, deterministic doubles
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from scripts.build_evaluation_corpus import build as build_corpus

    root = tmp_path_factory.mktemp("s4e-dataset")
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        build_corpus(root, dataset_version="v1")
    return {"dataset_root": root}


def _config(tmp_path, *, evaluation_id="m62-s4e-fault", generation=1):
    """A real EvaluationConfig, loaded through the production loader."""
    from training_gym.evaluation.config import load_config

    payload = {
        "schema_version": "m62.1",
        "evaluation_schema_version": "m62.evaluation.1",
        "evaluation_id": evaluation_id,
        "evaluation_generation": generation,
        "baseline_model": {
            "provider": "huggingface", "model_id": "Qwen/Qwen3-0.6B",
            "revision": "c1899de289a04d12100db370d81485cdf75e47ca",
            "parameters_b": 0.6, "family": "qwen3",
            "tokenizer_id": "Qwen/Qwen3-0.6B",
            "tokenizer_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
            "license_reference": "see the model card on the hub"},
        "candidate_adapter": {"run_id": "qwen3-06b-lora-quality-live-005",
                              "expected_manifest_hash": "", "expected_plan_hash": ""},
        "dataset": {"dataset_id": "m62-defensive-eval", "dataset_version": "v1"},
        "splits": {"splits": ["hidden_evaluation", "security_regression"],
                   "diagnostic_splits": []},
        "generation": {
            "mode": "greedy_deterministic", "max_new_tokens": 512,
            "max_input_tokens": 4096, "do_sample": False, "temperature": 0.0,
            "top_p": 1.0, "top_k": 0, "repetition_penalty": 1.0,
            "stop_sequences": [], "seed": 11, "timeout_s": 300, "batch_size": 1,
            "truncation_side": "refuse", "reasoning_policy": "disabled",
            "device_policy": "cpu", "precision_policy": "fp32"},
        "seed": 11,
        "created_at_utc": "2026-09-01T00:00:00Z",
        "limitations": ["synthetic fault-injection world"],
        "notes": "S4E fault injection. Deterministic doubles only; no model is loaded.",
    }
    path = Path(tmp_path) / f"{evaluation_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_config(str(path))


# Reuse the arm/reference fixtures from the execution suite so the two files cannot
# describe two different experiments.
from test_training_gym_m62_s4e_v4_execution import (  # noqa: E402
    _MarkerBackend,
    _baseline,
    _candidate_adapter,
    _pairing,
    _reference_adapter,
)


def _plan(config, world, tmp_path, *, order_hash=None, blockers=()):
    from training_gym.evaluation.plan import (
        EXPECTED_EVALUATION_FILES,
        EvaluationPlan,
        output_root_id,
        plan_state_sequence,
    )
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.preflight import prepare_pack_identity
    from training_gym.evaluation.runner import ORDER_POLICY_BALANCED

    identity = prepare_pack_identity(
        root=world["dataset_root"], dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version, splits=config.splits.splits,
        generation=config.evaluation_generation, seed=config.seed)
    built = build_task_pack_from_dataset(
        root=world["dataset_root"], dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version, splits=config.splits.splits,
        generation=config.evaluation_generation)
    order = tuple(t.task_id for t in built.pack.tasks)
    baseline, reference, candidate = _baseline(), _reference_adapter(), _candidate_adapter()
    inner = EvaluationPlan(
        evaluation_id=config.evaluation_id, generation=config.evaluation_generation,
        evaluation_config_hash=config.config_hash(),
        baseline_reference_hash=baseline.reference_hash(),
        candidate_adapter_reference_hash=candidate.reference_hash(),
        tokenizer_identity_hash=baseline.tokenizer_identity_hash,
        task_pack_hash=identity.pack_hash,
        hidden_target_store_hash=identity.hidden_target_store_hash,
        validation_manifest_hash="", hidden_evaluation_manifest_hash="",
        security_regression_manifest_hash="", adversarial_manifest_hash="",
        dataset_manifest_hash=identity.dataset_manifest_hash,
        generation_policy_hash=config.generation.policy_hash(),
        grader_policy_hash=config.policies.graders.policy_hash(),
        metric_policy_hash=config.policies.metrics.policy_hash(),
        statistical_policy_hash=config.policies.statistics.policy_hash(),
        gate_policy_hash=config.policies.gates.policy_hash(),
        family_policy_hash=config.policies.families.policy_hash(),
        resource_policy_hash=config.policies.resources.policy_hash(),
        dependency_report_hash="d" * 64, hardware_report_hash="e" * 64,
        order_policy=ORDER_POLICY_BALANCED,
        order_assignment_hash=identity.order_assignment_hash,
        expected_output_root_id=output_root_id(str(tmp_path)),
        expected_task_count=identity.task_count,
        expected_baseline_generations=identity.task_count,
        expected_candidate_generations=identity.task_count,
        expected_grader_executions=identity.task_count * 6,
        expected_files=EXPECTED_EVALUATION_FILES,
        expected_state_transitions=plan_state_sequence(awaiting_confirmation=True),
        backend_id="marker_double", created_at_utc=config.created_at_utc,
        performs_inference=True, blockers=tuple(blockers))
    pairing = _pairing()
    plan = V4EvaluationPlan(
        inner=inner, pairing=pairing,
        spend=PairedSpendPlan(pairing=pairing, task_count=identity.task_count),
        reference_adapter_reference_hash=reference.reference_hash(),
        candidate_adapter_reference_hash=candidate.reference_hash(),
        task_order_hash=order_hash or task_order_hash(order),
        arm_order_policy=ORDER_POLICY_BALANCED,
        arm_order_assignment_hash=identity.order_assignment_hash,
        runtime_report_sha256="f" * 64,
        evaluation_source_commit="0" * 40,
        holdout_dataset_id=config.dataset.dataset_id,
        holdout_dataset_version=config.dataset.dataset_version,
        holdout_manifest_hash=identity.dataset_manifest_hash,
        holdout_pack_hash=identity.pack_hash,
        holdout_preregistration_sha256="1" * 64,
        receipt_path_class="state/m62/receipts/<candidate_id>.eval.json",
        artifact_path_class="jarvis/evaluation/evaluations/<id>/gen-<n>/",
        blockers=tuple(blockers))
    return plan, baseline, reference, candidate


def _request(config, plan, baseline, reference, candidate, world, tmp_path, **overrides):
    kwargs = dict(
        config=config, plan=plan, baseline=baseline,
        reference_adapter=reference, candidate_adapter=candidate,
        reference_adapter_directory=Path(tmp_path) / "adapters" / "reference",
        candidate_adapter_directory=Path(tmp_path) / "adapters" / "candidate",
        output_root=Path(tmp_path), dataset_root=world["dataset_root"],
        backend_factory=lambda _role: _MarkerBackend(),
        at="2026-09-01T00:00:00Z", backend_version="marker_double")
    kwargs.update(overrides)
    return V4ExecutionRequest(**kwargs)


def _run(world, tmp_path, **overrides):
    config = _config(tmp_path, **{k: overrides.pop(k) for k in
                                  ("evaluation_id", "generation") if k in overrides})
    plan_kwargs = {k: overrides.pop(k) for k in ("order_hash", "blockers")
                   if k in overrides}
    plan, baseline, reference, candidate = _plan(config, world, tmp_path, **plan_kwargs)
    request = _request(config, plan, baseline, reference, candidate, world, tmp_path,
                       **overrides)
    return execute_v4_evaluation(request), plan


# ══════════════════════════════════════════════════════════════════════════════
#  The happy path, so every refusal below is measured against something that works
# ══════════════════════════════════════════════════════════════════════════════
def test_a_clean_paired_run_completes_and_accounts_for_every_generation(world, tmp_path):
    outcome, plan = _run(world, tmp_path)
    assert outcome.state is EvaluationRunState.COMPLETED, outcome.problems
    assert outcome.plan_consumed and outcome.paired_attempt_recorded
    assert outcome.holdout_committed and outcome.terminal_recorded
    assert outcome.reference_generations == outcome.task_count
    assert outcome.candidate_generations == outcome.task_count
    assert outcome.total_generations == 2 * outcome.task_count
    assert outcome.problems == ()


def test_the_run_imports_no_machine_learning_framework(world, tmp_path):
    _run(world, tmp_path)
    assert sorted(set(_BANNED) & set(sys.modules)) == []


def test_exactly_one_paired_attempt_and_one_commit_are_recorded(world, tmp_path):
    _run(world, tmp_path)
    assert len(SV4.v4_attempt_entries(tmp_path)) == 1
    from training_gym.evaluation.store import holdout_commit_entries
    assert len(holdout_commit_entries(tmp_path)) == 1


def test_the_report_states_that_arm_zero_is_not_a_bare_base_model(world, tmp_path):
    outcome, _ = _run(world, tmp_path)
    record = outcome.report.to_record()
    assert any("NOT a bare base model" in str(line)
               for line in record.get("limitations", []))
    assert V4_ARM_LIMITATION.split(".")[0] in json.dumps(record)


def test_a_synthetic_backend_can_never_produce_an_eligible_result(world, tmp_path):
    """The double is honest about being a double, whatever the numbers say."""
    outcome, _ = _run(world, tmp_path)
    assert outcome.empirical_status == "synthetic_only"
    assert outcome.eligibility != "eligible_for_human_review"


# ══════════════════════════════════════════════════════════════════════════════
#  PRE-SPEND failures — the holdout must still be unread
# ══════════════════════════════════════════════════════════════════════════════
def _assert_unspent(outcome, tmp_path):
    assert not outcome.paired_attempt_recorded
    assert not outcome.holdout_committed
    assert not outcome.holdout_is_spent
    assert SV4.v4_attempt_entries(tmp_path) == ()


def test_a_blocked_plan_spends_nothing(world, tmp_path):
    outcome, _ = _run(world, tmp_path, blockers=("a preflight blocker",))
    assert outcome.state is EvaluationRunState.FAILED
    assert not outcome.plan_consumed
    _assert_unspent(outcome, tmp_path)


def test_a_reordered_task_pack_stops_the_run_with_the_holdout_unread(world, tmp_path):
    """Order is bound. A pack reordered after approval is a different experiment."""
    outcome, _ = _run(world, tmp_path, order_hash="9" * 64)
    assert outcome.state is EvaluationRunState.FAILED
    assert any("task ORDER" in p for p in outcome.problems)
    _assert_unspent(outcome, tmp_path)


def test_a_swapped_reference_adapter_stops_the_run_with_the_holdout_unread(world, tmp_path):
    """The arm the inner plan cannot see is re-checked here or nowhere."""
    outcome, _ = _run(world, tmp_path, reference_adapter=_candidate_adapter())
    assert outcome.state is EvaluationRunState.FAILED
    assert any("reference adapter" in p for p in outcome.problems)
    _assert_unspent(outcome, tmp_path)


def test_a_second_run_of_the_same_plan_is_refused_before_anything_is_spent(world, tmp_path):
    first, plan = _run(world, tmp_path)
    assert first.state is EvaluationRunState.COMPLETED
    config = _config(tmp_path)
    plan2, baseline, reference, candidate = _plan(config, world, tmp_path)
    request = _request(config, plan2, baseline, reference, candidate, world, tmp_path)
    second = execute_v4_evaluation(request)
    assert second.state is EvaluationRunState.FAILED
    assert any("already started" in p for p in second.problems)


def test_a_renamed_second_attempt_is_refused_and_the_first_stays_the_only_spend(
        world, tmp_path):
    """The scientific property: ONE spend per corpus, whatever the run calls itself."""
    first, _ = _run(world, tmp_path)
    assert first.state is EvaluationRunState.COMPLETED
    second, _ = _run(world, tmp_path, evaluation_id="m62-s4e-fault-take-two")
    assert second.state is EvaluationRunState.FAILED
    assert len(SV4.v4_attempt_entries(tmp_path)) == 1


def test_a_spend_that_cannot_be_recorded_calls_no_backend(world, tmp_path):
    """A holdout that could not be recorded as spent is not spent."""
    seen: list = []

    class _Recording(_MarkerBackend):
        def generate(self, request):
            seen.append(request.task.task_id)
            return super().generate(request)

    # The attempt ledger exists and is unreadable. Recording the spend must fail, and it
    # must fail BEFORE the first generation rather than after it.
    SV4.v4_attempt_path(tmp_path).write_text("{corrupt\n", encoding="utf-8")
    outcome, _ = _run(world, tmp_path, backend_factory=lambda _role: _Recording())
    assert outcome.state is EvaluationRunState.FAILED
    assert seen == [], "a model was called for a holdout nobody could record as spent"
    assert not outcome.holdout_committed


def test_a_readable_but_already_spent_ledger_calls_no_backend(world, tmp_path):
    """The same property for the ordinary case: this corpus was already spent."""
    seen: list = []

    class _Recording(_MarkerBackend):
        def generate(self, request):
            seen.append(request.task.task_id)
            return super().generate(request)

    first, _ = _run(world, tmp_path)
    assert first.state is EvaluationRunState.COMPLETED
    seen.clear()
    second, _ = _run(world, tmp_path, evaluation_id="m62-s4e-fault-again",
                     backend_factory=lambda _role: _Recording())
    assert second.state is EvaluationRunState.FAILED
    assert seen == [], "a second attempt reached a model on an already-spent corpus"


# ══════════════════════════════════════════════════════════════════════════════
#  Plan mutation matrix — every bound field must change the hash
# ══════════════════════════════════════════════════════════════════════════════
def test_every_bound_field_changes_the_plan_hash(world, tmp_path):
    config = _config(tmp_path)
    plan, _, _, _ = _plan(config, world, tmp_path)
    base = plan.plan_hash()
    import dataclasses

    mutations = {
        "evaluation_source_commit": "1" * 40,
        "task_order_hash": "2" * 64,
        "runtime_report_sha256": "3" * 64,
        "holdout_manifest_hash": "4" * 64,
        "holdout_pack_hash": "5" * 64,
        "holdout_preregistration_sha256": "6" * 64,
        "arm_order_assignment_hash": "7" * 64,
        "holdout_dataset_version": "v8",
        "receipt_path_class": "somewhere/else/<candidate_id>.json",
        "artifact_path_class": "somewhere/else/",
    }
    for field, value in mutations.items():
        mutated = dataclasses.replace(plan, **{field: value})
        assert mutated.plan_hash() != base, f"{field} is not inside the plan hash"


def test_mutating_the_inner_plan_changes_the_v4_hash(world, tmp_path):
    """The V4 digest covers the inner digest, so every v1-v3 binding is still bound."""
    import dataclasses

    config = _config(tmp_path)
    plan, _, _, _ = _plan(config, world, tmp_path)
    base = plan.plan_hash()
    for field, value in (("gate_policy_hash", "a" * 64),
                         ("statistical_policy_hash", "b" * 64),
                         ("generation_policy_hash", "c" * 64),
                         ("task_pack_hash", "d" * 64),
                         ("expected_task_count", 35),
                         ("backend_id", "something_else")):
        from training_gym.evaluation.plan import EvaluationPlanError
        try:
            inner = dataclasses.replace(plan.inner, **{field: value})
            mutated = dataclasses.replace(plan, inner=inner)
        except (PlanV4Error, EvaluationPlanError):
            continue          # refused outright, which is stronger than a hash change
        assert mutated.plan_hash() != base, f"inner.{field} is not bound"


def test_swapping_the_arms_changes_the_plan_hash(world, tmp_path):
    import dataclasses

    config = _config(tmp_path)
    plan, _, _, _ = _plan(config, world, tmp_path)
    swapped = ReferenceAdapterPairing(
        reference=plan.pairing.candidate.__class__(
            **{**plan.pairing.candidate.to_dict_kwargs()}
            if hasattr(plan.pairing.candidate, "to_dict_kwargs") else
            dataclasses.asdict(plan.pairing.candidate)
            | {"role": EvaluationArmRole.REFERENCE}),
        candidate=plan.pairing.reference.__class__(
            **dataclasses.asdict(plan.pairing.reference)
            | {"role": EvaluationArmRole.CANDIDATE}))
    assert swapped.pairing_hash() != plan.pairing.pairing_hash()


def test_a_pairing_whose_arms_share_a_base_field_is_refused():
    """Single axis, enforced at construction rather than checked afterwards."""
    import dataclasses

    pairing = _pairing()
    drifted = dataclasses.replace(pairing.candidate,
                                  base_model_revision="d" * 40)
    with pytest.raises(ProtocolV4Error, match="single axis"):
        ReferenceAdapterPairing(reference=pairing.reference, candidate=drifted)


def test_a_plan_binding_one_adapter_on_both_arms_is_refused(world, tmp_path):
    import dataclasses

    config = _config(tmp_path)
    plan, _, _, candidate = _plan(config, world, tmp_path)
    with pytest.raises(PlanV4Error, match="same adapter reference"):
        dataclasses.replace(
            plan, reference_adapter_reference_hash=candidate.reference_hash())


def test_a_plan_whose_spend_describes_another_pairing_is_refused(world, tmp_path):
    import dataclasses

    config = _config(tmp_path)
    plan, _, _, _ = _plan(config, world, tmp_path)
    other = ReferenceAdapterPairing(
        reference=dataclasses.replace(plan.pairing.reference,
                                      adapter_sha256="9" * 64),
        candidate=plan.pairing.candidate)
    with pytest.raises(PlanV4Error, match="different pairing"):
        dataclasses.replace(plan, spend=PairedSpendPlan(pairing=other, task_count=36))


def test_a_plan_that_claims_two_holdout_spends_is_refused():
    pairing = _pairing()
    with pytest.raises(ProtocolV4Error, match="exactly 1 time"):
        PairedSpendPlan(pairing=pairing, task_count=36, holdout_spends=2)


def test_a_plan_binding_a_proxy_instead_of_a_digest_is_refused(world, tmp_path):
    import dataclasses

    config = _config(tmp_path)
    plan, _, _, _ = _plan(config, world, tmp_path)
    with pytest.raises(PlanV4Error, match="binds a proxy"):
        dataclasses.replace(plan, runtime_report_sha256="not-a-digest")


# ══════════════════════════════════════════════════════════════════════════════
#  Body-freeness of everything this path writes
# ══════════════════════════════════════════════════════════════════════════════
def test_no_recorded_artefact_outside_the_generation_directory_carries_a_prompt(
        world, tmp_path):
    """The ledger and the attempt record are digests and counts, by shape."""
    _run(world, tmp_path)
    for name in ("protocol-v4-attempts.jsonl", "evaluation_runs.jsonl"):
        path = Path(tmp_path) / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            for value in json.loads(json.dumps(payload)).values():
                if isinstance(value, str):
                    assert len(value) <= 128, f"{name} carries a long free-text value"


def test_the_outcome_summary_carries_no_response_text(world, tmp_path):
    outcome, _ = _run(world, tmp_path)
    assert "REFERENCE_ARM_MARKER" not in json.dumps(outcome.to_dict())
    assert "CANDIDATE_ARM_MARKER" not in json.dumps(outcome.to_dict())
