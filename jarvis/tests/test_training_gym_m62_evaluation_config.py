"""V69 M62 S3C — the evaluation configuration, its references and its plan.

WHAT THESE TESTS ARE FOR
------------------------
S3C decides whether an adapter is better AND still safe. Every one of those words is a
place a mistake hides: "better" can be manufactured by comparing against a different
baseline, "safe" by skipping a grader, and "this adapter" by pointing at a directory
nobody re-verified.

So these tests are mostly refusals. A config that cannot express a dangerous request, a
reference that cannot be built from a bare path, and a token that authorises exactly one
plan are the three properties everything downstream rests on.
"""
from __future__ import annotations

import json
import sys

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.evaluation import config as C
from training_gym.evaluation import plan as P
from training_gym.evaluation import references as R
from training_gym.evaluation.generation import (
    GenerationMode,
    GenerationPolicy,
    GenerationPolicyError,
    assert_identical_policies,
)
from training_gym.evaluation.policy import (
    EvaluationPolicyError,
    GatePolicy,
    GraderPolicy,
    MetricPolicy,
    StatisticalPolicy,
)
from training_gym.schemas import SchemaError
from training_gym.training.artifacts import AdapterManifest
from training_gym.training.model_identity import CacheStatus, ModelIdentity, RevisionKind

_REV = "a" * 40
_OTHER_REV = "b" * 40


def _config_payload(**overrides) -> dict:
    payload = {
        "schema_version": "m62.1",
        "evaluation_id": "qwen3-adapter-eval",
        "baseline_model": {
            "model_id": "Qwen/Qwen3-0.6B", "revision": _REV, "parameters_b": 0.6,
            "tokenizer_id": "Qwen/Qwen3-0.6B", "tokenizer_revision": _REV,
        },
        "candidate_adapter": {"run_id": "run-0001"},
        "dataset": {"dataset_id": "soc-gym", "dataset_version": "v1"},
        "created_at_utc": "2026-08-03T00:00:00Z",
        "seed": 11,
        "generation": {"seed": 11},
    }
    payload.update(overrides)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════════════════════
def test_a_valid_evaluation_config_parses_and_hashes_deterministically():
    first = C.config_from_dict(_config_payload())
    second = C.config_from_dict(_config_payload())
    assert first.config_hash() == second.config_hash()
    assert len(first.config_hash()) == 64
    assert first.eligibility_blockers() == ()


def test_the_config_hash_does_not_depend_on_key_order():
    payload = _config_payload()
    reordered = dict(reversed(list(payload.items())))
    assert C.config_from_dict(reordered).config_hash() == \
        C.config_from_dict(payload).config_hash()


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(SchemaError, match="unknown field"):
        C.config_from_dict(_config_payload(sampling_temperature=0.7))


@pytest.mark.parametrize("field_name", sorted(C.FORBIDDEN_CONFIG_FIELDS))
def test_every_capability_granting_field_is_refused_by_name(field_name):
    """The refusal explains WHY. An operator who tried deserves the reason, not a
    generic 'unknown field' that reads like a typo."""
    with pytest.raises(SchemaError) as exc:
        C.config_from_dict(_config_payload(**{field_name: "anything"}))
    assert "refused" in str(exc.value)


def test_a_duplicate_json_key_is_refused(tmp_path):
    path = tmp_path / "eval.json"
    path.write_text('{"schema_version": "m62.1", "seed": 0, "seed": 7}',
                    encoding="utf-8")
    with pytest.raises(SchemaError, match="duplicate key"):
        C.load_config(path)


def test_an_unsupported_evaluation_schema_version_is_refused():
    with pytest.raises(SchemaError, match="incompatible"):
        C.config_from_dict(_config_payload(evaluation_schema_version="m99.evaluation.1"))


@pytest.mark.parametrize("evaluation_id", ["../escape", "a/b", "CON", "", ".hidden"])
def test_an_unsafe_evaluation_id_is_refused(evaluation_id):
    with pytest.raises(SchemaError):
        C.config_from_dict(_config_payload(evaluation_id=evaluation_id))


def test_a_negative_seed_is_refused():
    with pytest.raises(SchemaError, match="seed"):
        C.config_from_dict(_config_payload(seed=-1, generation={"seed": 0}))


def test_a_config_seed_that_disagrees_with_the_generation_seed_is_refused():
    """One run has one seed. Two spellings mean the report carries whichever the code
    happened to read."""
    with pytest.raises(SchemaError, match="one run has one seed"):
        C.config_from_dict(_config_payload(seed=1, generation={"seed": 2}))


def test_a_symlinked_config_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_config_payload()), encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    with pytest.raises(SchemaError, match="symlink"):
        C.load_config(link)


def test_an_oversized_config_is_refused(tmp_path):
    path = tmp_path / "big.json"
    path.write_text(" " * (C.MAX_CONFIG_BYTES + 1), encoding="utf-8")
    with pytest.raises(SchemaError, match="exceeds"):
        C.load_config(path)


# ── splits ────────────────────────────────────────────────────────────────────
def test_a_train_only_evaluation_is_refused():
    """A model scoring well on what it was fitted on is the expected outcome."""
    with pytest.raises(SchemaError, match="not an authoritative evaluation split"):
        C.config_from_dict(_config_payload(splits={"splits": ["train"]}))


def test_quarantine_is_never_evaluable():
    with pytest.raises(SchemaError, match="never model input"):
        C.config_from_dict(_config_payload(splits={"splits": ["quarantine"]}))


def test_train_is_permitted_only_as_a_labelled_diagnostic():
    cfg = C.config_from_dict(_config_payload(splits={
        "splits": ["hidden_evaluation", "security_regression"],
        "diagnostic_splits": ["train"]}))
    assert DatasetSplit.TRAIN in cfg.splits.diagnostic_splits
    assert DatasetSplit.TRAIN not in cfg.splits.splits


def test_an_authoritative_split_cannot_be_relabelled_as_a_diagnostic():
    with pytest.raises(SchemaError, match="would hide a result that counts"):
        C.config_from_dict(_config_payload(splits={
            "splits": ["hidden_evaluation"],
            "diagnostic_splits": ["security_regression"]}))


def test_a_missing_mandatory_split_blocks_eligibility_without_blocking_the_load():
    """It must still LOAD: an operator running a validation-only sweep is doing
    something legitimate. What it may not do is decide eligibility."""
    cfg = C.config_from_dict(_config_payload(splits={"splits": ["hidden_evaluation"]}))
    blockers = cfg.eligibility_blockers()
    assert any("security_regression" in b for b in blockers)
    assert any("INSUFFICIENT_EVIDENCE, never a pass" in b for b in blockers)


# ══════════════════════════════════════════════════════════════════════════════
#  Generation policy
# ══════════════════════════════════════════════════════════════════════════════
def test_the_default_generation_policy_is_greedy_and_eligibility_grade():
    policy = GenerationPolicy()
    assert policy.mode is GenerationMode.GREEDY_DETERMINISTIC
    assert policy.do_sample is False
    assert policy.temperature == 0.0
    assert policy.eligibility_grade is True
    assert policy.blockers() == ()


def test_greedy_with_sampling_enabled_is_refused():
    with pytest.raises(GenerationPolicyError, match="is not greedy"):
        GenerationPolicy(mode=GenerationMode.GREEDY_DETERMINISTIC, do_sample=True)


def test_greedy_with_a_non_zero_temperature_is_refused():
    with pytest.raises(GenerationPolicyError, match="temperature"):
        GenerationPolicy(mode=GenerationMode.GREEDY_DETERMINISTIC, temperature=0.7)


def test_sampling_is_diagnostic_only_and_cannot_decide_eligibility():
    policy = GenerationPolicy(mode=GenerationMode.SEEDED_SAMPLING_DIAGNOSTIC,
                              do_sample=True, temperature=0.8, top_p=0.9)
    assert policy.eligibility_grade is False
    assert any("diagnostic only" in b for b in policy.blockers())


@pytest.mark.parametrize("kwargs", [
    {"max_new_tokens": 0}, {"max_new_tokens": 10 ** 9}, {"timeout_s": 0},
    {"timeout_s": 10 ** 9}, {"seed": -1}, {"batch_size": 0}, {"temperature": 9.0},
])
def test_an_unbounded_or_zero_generation_setting_is_refused(kwargs):
    with pytest.raises(SchemaError):
        GenerationPolicy(**{"seed": 0, **kwargs})


def test_an_empty_stop_sequence_is_refused():
    """It matches at position zero, so every response would be empty."""
    with pytest.raises(GenerationPolicyError, match="position zero"):
        GenerationPolicy(stop_sequences=("",))


def test_a_training_only_precision_is_refused_for_evaluation():
    from training_gym.training.config import PrecisionPolicy
    with pytest.raises(GenerationPolicyError, match="FITTED"):
        GenerationPolicy(precision_policy=PrecisionPolicy.INT4_QLORA)


def test_two_arms_configured_differently_cannot_be_compared():
    left = GenerationPolicy(max_new_tokens=256)
    right = GenerationPolicy(max_new_tokens=512)
    with pytest.raises(GenerationPolicyError, match="measures the decoder"):
        assert_identical_policies(left, right)
    assert assert_identical_policies(left, GenerationPolicy(max_new_tokens=256)) \
        == left.policy_hash()


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded policies
# ══════════════════════════════════════════════════════════════════════════════
def test_a_grader_cannot_be_made_non_blocking():
    with pytest.raises(EvaluationPolicyError, match="may not be false"):
        GraderPolicy(missing_grader_blocks=False)


def test_a_rate_cannot_be_reported_without_its_denominator():
    with pytest.raises(EvaluationPolicyError, match="different claims"):
        MetricPolicy(require_denominator=False)


def test_a_zero_denominator_may_not_become_a_score():
    with pytest.raises(EvaluationPolicyError, match="nothing measured is not everything"):
        MetricPolicy(zero_denominator_is_insufficient=False)


def test_there_is_no_acceptable_number_of_new_unsafe_acceptances():
    with pytest.raises(EvaluationPolicyError, match="there is no such number"):
        GatePolicy(max_unsafe_acceptance_increase=0.01)


def test_an_unknown_grader_cannot_be_named_as_a_security_grader():
    with pytest.raises(EvaluationPolicyError, match="unknown grader"):
        GraderPolicy(security_graders=("nmap_grader",))


def test_the_bootstrap_iteration_ceiling_is_enforced():
    with pytest.raises(SchemaError):
        StatisticalPolicy(bootstrap_iterations=10 ** 7)
    with pytest.raises(SchemaError):
        StatisticalPolicy(bootstrap_iterations=1)


def test_a_security_grader_is_always_requested_alongside_a_familys_own_checks():
    from training_gym.task_spec import TaskFamily
    policy = GraderPolicy()
    requested = policy.requested_for(TaskFamily.SIGMA_RULE)
    assert "detection_rule" in requested          # the family's own mandatory check
    assert "safety_policy" in requested           # and the security floor, always
    assert "secret_pii" in policy.mandatory_for(TaskFamily.SIGMA_RULE)


# ══════════════════════════════════════════════════════════════════════════════
#  References
# ══════════════════════════════════════════════════════════════════════════════
def _identity(**overrides) -> ModelIdentity:
    kwargs = dict(provider="huggingface", model_id="Qwen/Qwen3-0.6B", revision=_REV,
                  parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B",
                  tokenizer_revision=_REV, cache_status=CacheStatus.PRESENT,
                  cache_evidence="c" * 64)
    kwargs.update(overrides)
    return ModelIdentity(**kwargs)


def _adapter_manifest(**overrides) -> AdapterManifest:
    base = _identity()
    kwargs = dict(
        run_id="run-0001", run_state="completed", plan_hash="1" * 64,
        training_config_hash="2" * 64, method="sft_lora", backend_id="transformers_peft",
        backend_version="m62.1", base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision=_REV, base_model_identity_hash=base.identity_hash(),
        tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=_REV,
        tokenizer_identity_hash=base.tokenizer_identity_hash(),
        tokenizer_chat_template_hash="3" * 64, dataset_id="soc-gym",
        dataset_version="v1", dataset_reference_hash="4" * 64,
        dataset_manifest_hash="5" * 64, train_shard_hash="6" * 64,
        validation_shard_hash="7" * 64, hidden_evaluation_hash="8" * 64,
        security_regression_hash="9" * 64, export_manifest_hash="a" * 64,
        split_algorithm_version="m62.split.1", seed=11, lora={"r": 8, "lora_alpha": 16},
        assistant_only_loss={"enabled": True}, package_versions={"peft": "0.12.0"},
        device_category="cpu", precision="fp32", requested_steps=10, completed_steps=10,
        epochs_completed=1.0, train_loss=0.5, eval_loss=0.6, truncated_records=0,
        duration_seconds=12.0, files=(), total_bytes=1024, tree_hash="b" * 64,
        created_at_utc="2026-08-02T00:00:00Z", interrupted=False, completed=True)
    kwargs.update(overrides)
    return AdapterManifest(**kwargs)


def test_a_baseline_reference_is_derived_from_the_frozen_model_identity():
    ref = R.base_reference_from_identity(_identity())
    assert ref.base_model_identity_hash == _identity().identity_hash()
    assert ref.trust_remote_code is False
    assert ref.revision_kind is RevisionKind.IMMUTABLE_COMMIT
    assert ref.is_live_executable is True
    assert len(ref.reference_hash()) == 64


def test_a_baseline_reference_cannot_be_built_from_a_bare_string():
    with pytest.raises(R.EvaluationReferenceError, match="ModelIdentity"):
        R.base_reference_from_identity("Qwen/Qwen3-0.6B")  # type: ignore[arg-type]


def test_a_mutable_revision_is_not_live_executable():
    ref = R.base_reference_from_identity(_identity(revision="main",
                                                   tokenizer_revision="main"))
    assert ref.is_live_executable is False
    assert any("mutable tag" in b for b in ref.live_blockers())


def test_an_absent_cache_is_a_blocker_and_never_a_download():
    ref = R.base_reference_from_identity(_identity(cache_status=CacheStatus.ABSENT,
                                                   cache_evidence=""))
    assert any("never downloads" in b for b in ref.live_blockers())


def test_no_private_path_survives_into_a_baseline_reference():
    ref = R.base_reference_from_identity(
        _identity(cache_evidence="C:/Users/someone/.cache/huggingface"))
    assert "Users" not in ref.cache_evidence
    assert "/" not in ref.cache_evidence and "\\" not in ref.cache_evidence
    assert "someone" not in json.dumps(ref.to_record())


def test_a_reference_carrying_a_path_in_its_cache_evidence_is_refused():
    ref = R.base_reference_from_identity(_identity())
    with pytest.raises(R.EvaluationReferenceError, match="never where"):
        R.BaseModelEvaluationReference(
            base_model_id=ref.base_model_id, base_model_revision=ref.base_model_revision,
            base_model_identity_hash=ref.base_model_identity_hash,
            tokenizer_id=ref.tokenizer_id, tokenizer_revision=ref.tokenizer_revision,
            tokenizer_identity_hash=ref.tokenizer_identity_hash,
            cache_evidence="/home/operator/.cache")


def test_remote_code_is_refused_in_a_baseline_reference():
    ref = R.base_reference_from_identity(_identity())
    with pytest.raises(R.EvaluationReferenceError, match="trust_remote_code is refused"):
        R.BaseModelEvaluationReference(
            base_model_id=ref.base_model_id, base_model_revision=ref.base_model_revision,
            base_model_identity_hash=ref.base_model_identity_hash,
            tokenizer_id=ref.tokenizer_id, tokenizer_revision=ref.tokenizer_revision,
            tokenizer_identity_hash=ref.tokenizer_identity_hash,
            trust_remote_code=True)


# ── adapter references ────────────────────────────────────────────────────────
def test_an_adapter_reference_requires_a_verified_manifest_not_a_dictionary():
    with pytest.raises(R.EvaluationReferenceError, match="may not be built from a bare"):
        R.reference_from_manifest({"run_id": "run-0001"},  # type: ignore[arg-type]
                                  artifact_verified=True)


def test_an_unverified_adapter_reference_is_refused():
    """A manifest nobody re-checked proves the manifest is well-formed, not that the
    files still match it."""
    with pytest.raises(R.EvaluationReferenceError, match="re-verified from disk"):
        R.reference_from_manifest(_adapter_manifest(), artifact_verified=False)


def test_a_valid_adapter_reference_hashes_deterministically():
    ref = R.reference_from_manifest(_adapter_manifest(), artifact_verified=True)
    again = R.reference_from_manifest(_adapter_manifest(), artifact_verified=True)
    assert ref.reference_hash() == again.reference_hash()
    assert ref.run_id == "run-0001"


def test_an_interrupted_run_may_not_be_evaluated():
    with pytest.raises(R.EvaluationReferenceError, match="was interrupted"):
        R.reference_from_manifest(_adapter_manifest(interrupted=True, completed=False),
                                  artifact_verified=True)


def test_an_incomplete_run_may_not_be_evaluated():
    with pytest.raises(R.EvaluationReferenceError, match="did not complete"):
        R.reference_from_manifest(_adapter_manifest(completed=False),
                                  artifact_verified=True)


@pytest.mark.parametrize("state", ["failed", "quarantined", "running", "interrupted"])
def test_only_a_completed_run_may_be_evaluated(state):
    """Two independent layers refuse this. The manifest will not even be constructed
    with a non-completed state while claiming completion, and the reference refuses the
    state on its own — so a reference hand-built around a bypassed manifest still fails.
    """
    with pytest.raises(Exception, match="must agree"):
        _adapter_manifest(run_state=state)

    verified = R.reference_from_manifest(_adapter_manifest(), artifact_verified=True)
    fields = {k: v for k, v in verified.__dict__.items()}
    fields["run_state"] = state
    fields["method"] = verified.method
    with pytest.raises(R.EvaluationReferenceError, match="only 'completed'"):
        R.AdapterEvaluationReference(**fields)


def test_an_unevaluable_training_method_is_refused():
    with pytest.raises(R.EvaluationReferenceError, match="never built"):
        R.reference_from_manifest(_adapter_manifest(method="dpo_lora"),
                                  artifact_verified=True)


def test_building_a_reference_from_a_bare_path_is_impossible(tmp_path):
    """There is no constructor that takes a directory and trusts it. The one that takes
    a directory re-verifies it, and refuses when the verification fails."""
    empty = tmp_path / "not-an-adapter"
    empty.mkdir()
    with pytest.raises(R.EvaluationReferenceError, match="does not verify"):
        R.build_adapter_reference(empty, lora={"r": 8, "lora_alpha": 16},
                                  base_model_id="Qwen/Qwen3-0.6B")


# ── pairing and contamination ─────────────────────────────────────────────────
def test_a_pair_on_the_same_base_and_tokenizer_verifies():
    pair = R.verify_reference_pair(
        R.base_reference_from_identity(_identity()),
        R.reference_from_manifest(_adapter_manifest(), artifact_verified=True))
    assert pair.ok is True
    assert len(pair.pair_hash()) == 64


def test_an_adapter_trained_on_a_different_base_model_cannot_be_attached():
    other = _identity(model_id="Qwen/Qwen3-1.7B", tokenizer_id="Qwen/Qwen3-1.7B",
                      parameters_b=1.7)
    problems = R.pairing_blockers(
        R.base_reference_from_identity(other),
        R.reference_from_manifest(_adapter_manifest(), artifact_verified=True))
    assert any("measures the mismatch" in p for p in problems)


def test_a_substituted_tokenizer_invalidates_the_pair():
    base = R.base_reference_from_identity(_identity())
    swapped = R.reference_from_manifest(
        _adapter_manifest(tokenizer_identity_hash="f" * 64), artifact_verified=True)
    assert any("two prompts" in p for p in R.pairing_blockers(base, swapped))


def test_an_adapter_evaluated_on_the_shard_it_trained_on_is_refused():
    adapter = R.reference_from_manifest(_adapter_manifest(), artifact_verified=True)
    problems = R.contamination_blockers(
        adapter, hidden_evaluation_hash=adapter.train_shard_hash,
        security_regression_hash="9" * 64)
    assert any("memorisation" in p for p in problems)


def test_evaluating_against_a_different_held_out_set_than_the_approved_one_is_refused():
    adapter = R.reference_from_manifest(_adapter_manifest(), artifact_verified=True)
    problems = R.contamination_blockers(adapter, hidden_evaluation_hash="e" * 64,
                                        security_regression_hash="9" * 64)
    assert any("not the shard the adapter was bound to" in p for p in problems)


# ══════════════════════════════════════════════════════════════════════════════
#  Plan and confirmation
# ══════════════════════════════════════════════════════════════════════════════
def _plan(**overrides) -> P.EvaluationPlan:
    kwargs = dict(
        evaluation_id="qwen3-adapter-eval", generation=1,
        evaluation_config_hash="1" * 64, baseline_reference_hash="2" * 64,
        candidate_adapter_reference_hash="3" * 64, tokenizer_identity_hash="4" * 64,
        task_pack_hash="5" * 64, hidden_target_store_hash="6" * 64,
        validation_manifest_hash="", hidden_evaluation_manifest_hash="7" * 64,
        security_regression_manifest_hash="8" * 64, adversarial_manifest_hash="",
        dataset_manifest_hash="9" * 64, generation_policy_hash="a" * 64,
        grader_policy_hash="b" * 64, metric_policy_hash="c" * 64,
        statistical_policy_hash="d" * 64, gate_policy_hash="e" * 64,
        family_policy_hash="f" * 64, resource_policy_hash="0" * 64,
        dependency_report_hash="1" * 64, hardware_report_hash="2" * 64,
        order_policy="balanced_by_task_hash", order_assignment_hash="3" * 64,
        expected_output_root_id="4" * 64, expected_task_count=8,
        expected_baseline_generations=8, expected_candidate_generations=8,
        expected_grader_executions=32, expected_files=P.EXPECTED_EVALUATION_FILES,
        expected_state_transitions=P.plan_state_sequence(awaiting_confirmation=True),
        backend_id="fake_evaluation", created_at_utc="2026-08-03T00:00:00Z")
    kwargs.update(overrides)
    return P.EvaluationPlan(**kwargs)


def test_the_plan_hash_is_deterministic():
    assert _plan().plan_hash() == _plan().plan_hash()
    assert len(_plan().plan_hash()) == 64


@pytest.mark.parametrize("changed", [
    {"candidate_adapter_reference_hash": "f" * 64},
    {"baseline_reference_hash": "f" * 64},
    {"task_pack_hash": "f" * 64},
    {"generation_policy_hash": "f" * 64},
    {"gate_policy_hash": "f" * 64},
    {"hardware_report_hash": "f" * 64},
    {"expected_output_root_id": "f" * 64},
    {"order_assignment_hash": "f" * 64},
])
def test_changing_any_bound_input_changes_the_plan_and_invalidates_the_token(changed):
    original = _plan()
    altered = _plan(**changed)
    assert altered.plan_hash() != original.plan_hash()
    with pytest.raises(P.EvaluationConfirmationRejected, match="Something changed"):
        P.check_evaluation_confirmation(original.confirmation_token(), altered)


def test_the_exact_token_for_this_plan_is_accepted():
    plan = _plan()
    assert P.check_evaluation_confirmation(plan.confirmation_token(), plan) \
        == plan.confirmation_token()


@pytest.mark.parametrize("confirmation", [
    True, False, 1, None, "yes", "y", "EVAL", "eval:" + "a" * 64,
    "EVAL:" + "a" * 12, "EVAL:", "@token.txt", "EVAL:/tmp/token", "--force",
])
def test_no_generic_affirmation_authorises_an_evaluation(confirmation):
    with pytest.raises(P.EvaluationConfirmationRejected):
        P.check_evaluation_confirmation(confirmation, _plan())


def test_a_training_confirmation_is_refused_with_its_own_explanation():
    """Pasting a TRAIN token into an evaluation is a plausible mistake; the operator is
    told which one they used rather than being given a generic prefix error."""
    from training_gym.training.plan import CONFIRMATION_PREFIX as TRAIN_PREFIX
    token = f"{TRAIN_PREFIX}{_plan().plan_hash()}"
    with pytest.raises(P.EvaluationConfirmationRejected, match="that is a TRAIN"):
        P.check_evaluation_confirmation(token, _plan())


def test_the_evaluation_prefix_is_distinct_from_the_training_prefix():
    from training_gym.training.plan import CONFIRMATION_PREFIX as TRAIN_PREFIX
    assert P.CONFIRMATION_PREFIX == "EVAL:"
    assert P.CONFIRMATION_PREFIX != TRAIN_PREFIX


@pytest.mark.parametrize("flag", list(P.EvaluationPlan._IMPOSSIBLE))
def test_a_plan_cannot_claim_an_effect_this_subsystem_cannot_produce(flag):
    with pytest.raises(P.EvaluationPlanError, match="may not be true"):
        _plan(**{flag: True})


def test_a_plan_over_zero_tasks_is_refused():
    with pytest.raises(P.EvaluationPlanError, match="empty denominator"):
        _plan(expected_task_count=0, expected_baseline_generations=0,
              expected_candidate_generations=0)


def test_an_unpaired_plan_is_refused():
    """One arm measured on tasks the other never saw is not a comparison."""
    with pytest.raises(P.EvaluationPlanError, match="not a paired comparison"):
        _plan(expected_candidate_generations=7)


def test_a_plan_may_not_expect_an_artifact_outside_the_allowlist():
    with pytest.raises(P.EvaluationPlanError, match="allowlist"):
        _plan(expected_files=("evaluation-report.json", "adapter_model.safetensors"))


def test_the_plan_never_carries_the_output_root_itself(tmp_path):
    root_id = P.output_root_id(tmp_path)
    plan = _plan(expected_output_root_id=root_id)
    record = json.dumps(plan.to_record())
    assert str(tmp_path) not in record
    assert root_id in record
    assert P.output_root_id(tmp_path) != P.output_root_id(tmp_path / "other")


def test_the_plan_declares_the_strongest_verdict_it_could_ever_reach():
    effects = _plan().expected_effects()
    assert effects["strongest_possible_verdict"] == "eligible_for_human_review"
    assert effects["promotes_a_model"] is False
    assert effects["writes_the_model_registry"] is False
    assert effects["executes_model_proposed_tool_calls"] is False


def test_an_existing_generation_directory_is_never_overwritten(tmp_path):
    directory = P.evaluation_run_directory(tmp_path, "eval-1", 1)
    directory.mkdir(parents=True)
    problems = P.check_output_root(tmp_path, "eval-1", 1)
    assert any("already exists" in p for p in problems)
    assert P.check_output_root(tmp_path, "eval-1", 2) == ()


@pytest.mark.parametrize("evaluation_id", ["../escape", "a/b", "NUL"])
def test_the_run_directory_validates_the_id_before_joining(evaluation_id):
    with pytest.raises(SchemaError):
        P.evaluation_run_directory("/tmp/root", evaluation_id, 1)


# ══════════════════════════════════════════════════════════════════════════════
#  The state machine
# ══════════════════════════════════════════════════════════════════════════════
def test_there_is_no_direct_route_from_planned_to_running():
    with pytest.raises(C.EvaluationTransitionError):
        C.check_evaluation_transition(C.EvaluationRunState.PLANNED,
                                      C.EvaluationRunState.RUNNING_BASELINE)


@pytest.mark.parametrize("state", [
    C.EvaluationRunState.RUNNING_BASELINE, C.EvaluationRunState.RUNNING_CANDIDATE,
    C.EvaluationRunState.SCORING, C.EvaluationRunState.COMPARING,
])
def test_no_running_state_reaches_completed_without_artifact_validation(state):
    with pytest.raises(C.EvaluationTransitionError):
        C.check_evaluation_transition(state, C.EvaluationRunState.COMPLETED)
    assert C.EvaluationRunState.COMPLETED in C.ALLOWED_EVALUATION_TRANSITIONS[
        C.EvaluationRunState.ARTIFACT_VALIDATION]


@pytest.mark.parametrize("state", [
    C.EvaluationRunState.INTERRUPTED, C.EvaluationRunState.FAILED,
    C.EvaluationRunState.QUARANTINED,
])
def test_a_terminal_failure_never_becomes_completed(state):
    with pytest.raises(C.EvaluationTransitionError):
        C.check_evaluation_transition(state, C.EvaluationRunState.COMPLETED)
    assert state.is_successful is False


def test_completed_is_the_only_successful_state():
    successful = [s for s in C.EvaluationRunState if s.is_successful]
    assert successful == [C.EvaluationRunState.COMPLETED]


def test_no_evaluation_state_means_promoted_or_active():
    """S3C cannot represent a promotion, so it cannot accidentally record one."""
    names = {s.value for s in C.EvaluationRunState}
    assert not (names & {"promoted", "active", "registered", "merged", "deployed"})


# ══════════════════════════════════════════════════════════════════════════════
#  No effects
# ══════════════════════════════════════════════════════════════════════════════
_BANNED_MODULES = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
                   "bitsandbytes", "huggingface_hub", "tokenizers", "safetensors")


def test_none_of_these_contracts_import_a_machine_learning_framework():
    """Asserted over ``sys.modules`` AFTER this module's imports have all run: every
    symbol these tests exercise is already loaded by the time this executes."""
    assert sorted(set(_BANNED_MODULES) & set(sys.modules)) == []


def test_building_and_hashing_a_plan_imports_no_framework():
    before = set(sys.modules)
    plan = _plan()
    plan.plan_hash()
    plan.to_record()
    C.config_from_dict(_config_payload())
    R.base_reference_from_identity(_identity())
    assert sorted(set(_BANNED_MODULES) & (set(sys.modules) - before)) == []
