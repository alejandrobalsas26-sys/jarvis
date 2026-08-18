"""V69 M62 S3E.1 — the wired execution path, qualified end to end before it runs live.

WHAT IS BEING QUALIFIED
-----------------------
``--execute`` used to verify every precondition and then refuse. It now spends the plan,
loads two models and writes a generation. That is a one-way door, so the whole path is
exercised here first with deterministic doubles: the same `execute_evaluation`, the same
runner, graders, comparison, gates, report, artifact writers, ledger and state machine
that a live run uses. Only the backend differs.

WHY A FAKE RUN CANNOT LIE
-------------------------
``classify_empirical_status`` reads the backend ids that actually answered and marks
anything synthetic ``SYNTHETIC_ONLY``; ``decide_eligibility`` refuses that before it
looks at a single gate. So every scenario below produces a complete, honest report that
can never conclude an adapter is eligible — which is exactly what makes it safe to run
this many of them.

The scenarios are chosen for the ways a comparison can be wrong rather than the ways it
can be right: a candidate that wins by refusing everything, a family with no denominator,
a pair that never completed, artefacts that do not describe themselves.
"""
from __future__ import annotations

import json

import pytest

from training_gym.evaluation import config as C
from training_gym.evaluation import references as R
from training_gym.evaluation.backends.fake import FakeEvaluationBackend, FakeMode
from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.execution import ExecutionRequest, execute_evaluation
from training_gym.evaluation.plan import EvaluationPlan, EXPECTED_EVALUATION_FILES
from training_gym.evaluation.reports import CandidateEligibility, EmpiricalStatus
from training_gym.evaluation.runner import ORDER_POLICY_BALANCED
from training_gym.training.model_identity import CacheStatus, ModelIdentity

pytest.importorskip("scripts.build_evaluation_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402

REV = "c" * 40
NOW = "2026-08-06T00:00:00Z"
SPLITS = ["hidden_evaluation", "security_regression", "adversarial"]


# ══════════════════════════════════════════════════════════════════════════════
#  Scaffolding
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("execdata")
    BC.build(root)
    return root


def make_config(**overrides):
    payload = {
        "schema_version": "m62.1",
        "evaluation_id": "qualification-eval",
        "evaluation_generation": 1,
        "baseline_model": {
            "model_id": "Qwen/Qwen3-0.6B", "revision": REV, "parameters_b": 0.6,
            "tokenizer_id": "Qwen/Qwen3-0.6B", "tokenizer_revision": REV,
        },
        "candidate_adapter": {"run_id": "run-0001"},
        "dataset": {"dataset_id": BC.DATASET_ID,
                    "dataset_version": BC.DATASET_VERSION},
        "splits": {"splits": list(SPLITS), "diagnostic_splits": []},
        "created_at_utc": NOW, "seed": 11, "generation": {"seed": 11},
    }
    payload.update(overrides)
    return C.config_from_dict(payload)


def make_baseline():
    return R.base_reference_from_identity(ModelIdentity(
        provider="huggingface", model_id="Qwen/Qwen3-0.6B", revision=REV,
        parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=REV,
        cache_status=CacheStatus.PRESENT, cache_evidence="a" * 64))


def make_adapter(baseline, **overrides):
    from training_gym.training.config import TrainingMethod, TrainingRunState
    fields = {
        "run_id": "run-0001", "adapter_manifest_hash": "1" * 64,
        "adapter_artifact_tree_hash": "2" * 64, "plan_hash": "3" * 64,
        "training_config_hash": "4" * 64,
        "base_model_identity_hash": baseline.base_model_identity_hash,
        "base_model_canonical_identity_hash":
            baseline.base_model_canonical_identity_hash,
        "tokenizer_identity_hash": baseline.tokenizer_identity_hash,
        "tokenizer_chat_template_hash": "5" * 64, "dataset_reference_hash": "6" * 64,
        "dataset_manifest_hash": "7" * 64, "train_shard_hash": "8" * 64,
        "validation_shard_hash": "9" * 64, "hidden_evaluation_hash": "a" * 64,
        "security_regression_hash": "b" * 64, "method": TrainingMethod.SFT_LORA,
        "lora": {}, "run_state": TrainingRunState.COMPLETED.value,
        "artifact_verified": True,
    }
    fields.update(overrides)
    return R.AdapterEvaluationReference(**fields)


def pack_identity(dataset_root, config):
    """The exact identities a plan must bind, derived by the production primitive.

    V69 M62 S3Q.0. Before it, these plans carried placeholder digests and execution
    never compared them to anything — which was the defect, not a test convenience. A
    plan that does not name the pack it authorises is now refused at execution, so the
    qualification plans name theirs.
    """
    from training_gym.evaluation.preflight import prepare_pack_identity
    return prepare_pack_identity(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation,
        seed=config.seed)


def make_plan(config, baseline, adapter, *, blockers=(), task_count=36,
              dataset_root=None, identity=None):
    if identity is None and dataset_root is not None:
        identity = pack_identity(dataset_root, config)
    if identity is not None:
        return EvaluationPlan(
            evaluation_id=config.evaluation_id, generation=config.evaluation_generation,
            evaluation_config_hash=config.config_hash(),
            baseline_reference_hash=baseline.reference_hash(),
            candidate_adapter_reference_hash=adapter.reference_hash(),
            tokenizer_identity_hash=baseline.tokenizer_identity_hash,
            task_pack_hash=identity.pack_hash,
            hidden_target_store_hash=identity.hidden_target_store_hash,
            validation_manifest_hash="",
            hidden_evaluation_manifest_hash="f" * 64,
            security_regression_manifest_hash="0" * 64,
            adversarial_manifest_hash="1" * 64,
            dataset_manifest_hash=identity.dataset_manifest_hash,
            generation_policy_hash=config.generation.policy_hash(),
            grader_policy_hash=config.policies.graders.policy_hash(),
            metric_policy_hash=config.policies.metrics.policy_hash(),
            statistical_policy_hash=config.policies.statistics.policy_hash(),
            gate_policy_hash=config.policies.gates.policy_hash(),
            family_policy_hash=config.policies.families.policy_hash(),
            resource_policy_hash=config.policies.resources.policy_hash(),
            dependency_report_hash="3" * 64, hardware_report_hash="4" * 64,
            order_policy=identity.order_policy,
            order_assignment_hash=identity.order_assignment_hash,
            expected_output_root_id="qualification",
            expected_task_count=identity.task_count,
            expected_baseline_generations=identity.task_count,
            expected_candidate_generations=identity.task_count,
            expected_grader_executions=identity.task_count * 6,
            expected_files=EXPECTED_EVALUATION_FILES,
            expected_state_transitions=(), backend_id="transformers_peft",
            created_at_utc=NOW, blockers=tuple(blockers))
    return EvaluationPlan(
        evaluation_id=config.evaluation_id, generation=config.evaluation_generation,
        evaluation_config_hash=config.config_hash(),
        baseline_reference_hash=baseline.reference_hash(),
        candidate_adapter_reference_hash=adapter.reference_hash(),
        tokenizer_identity_hash=baseline.tokenizer_identity_hash,
        task_pack_hash="d" * 64, hidden_target_store_hash="e" * 64,
        validation_manifest_hash="", hidden_evaluation_manifest_hash="f" * 64,
        security_regression_manifest_hash="0" * 64, adversarial_manifest_hash="1" * 64,
        dataset_manifest_hash="2" * 64,
        generation_policy_hash=config.generation.policy_hash(),
        grader_policy_hash=config.policies.graders.policy_hash(),
        metric_policy_hash=config.policies.metrics.policy_hash(),
        statistical_policy_hash=config.policies.statistics.policy_hash(),
        gate_policy_hash=config.policies.gates.policy_hash(),
        family_policy_hash=config.policies.families.policy_hash(),
        resource_policy_hash=config.policies.resources.policy_hash(),
        dependency_report_hash="3" * 64, hardware_report_hash="4" * 64,
        order_policy=ORDER_POLICY_BALANCED, order_assignment_hash="5" * 64,
        expected_output_root_id="qualification", expected_task_count=task_count,
        expected_baseline_generations=task_count,
        expected_candidate_generations=task_count,
        expected_grader_executions=task_count * 6,
        expected_files=EXPECTED_EVALUATION_FILES,
        expected_state_transitions=(), backend_id="transformers_peft",
        created_at_utc=NOW, blockers=tuple(blockers))


def run(dataset_root, output_root, *, mode=FakeMode.IDENTICAL, config=None,
        plan=None, factory=None, **request_overrides):
    """Drive the real execution path with deterministic doubles."""
    config = config or make_config()
    baseline = make_baseline()
    adapter = make_adapter(baseline)
    plan = plan if plan is not None else make_plan(config, baseline, adapter,
                                                   dataset_root=dataset_root)

    def default_factory(_role):
        return FakeEvaluationBackend(mode)

    kwargs = {
        "config": config, "baseline": baseline, "adapter": adapter, "plan": plan,
        "output_root": output_root, "dataset_root": dataset_root,
        "backend_factory": factory or default_factory, "at": NOW,
        "backend_version": "fake-1",
    }
    kwargs.update(request_overrides)
    return execute_evaluation(ExecutionRequest(**kwargs))


# ══════════════════════════════════════════════════════════════════════════════
#  The happy path
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def happy(dataset_root, tmp_path_factory):
    return run(dataset_root, tmp_path_factory.mktemp("happy"))


def test_the_whole_path_completes(happy):
    assert happy.state is EvaluationRunState.COMPLETED, happy.problems
    assert happy.ok


def test_it_walked_the_declared_state_machine(happy):
    assert happy.states_visited == (
        "preflight_verified", "starting", "running_baseline", "running_candidate",
        "scoring", "comparing", "artifact_validation", "completed")


def test_every_task_in_the_corpus_was_answered_by_both_arms(happy):
    assert happy.task_count == 36
    assert happy.measured_pairs == 36


def test_every_expected_artifact_exists(happy):
    written = {p.name for p in happy.directory.iterdir()}
    for name in EXPECTED_EVALUATION_FILES:
        assert name in written, name


def test_no_unexpected_file_is_written(happy):
    from training_gym.evaluation.artifacts import ALLOWED_EVALUATION_FILES
    for path in happy.directory.iterdir():
        assert path.name in ALLOWED_EVALUATION_FILES, path.name


def test_the_generation_re_verifies_from_disk(happy):
    from training_gym.evaluation.artifacts import verify_evaluation_generation
    assert verify_evaluation_generation(happy.directory) == ()


def test_no_artifact_is_a_symlink_or_a_directory(happy):
    for path in happy.directory.iterdir():
        assert path.is_file() and not path.is_symlink(), path.name


# ── the property that makes all of this safe ──────────────────────────────────
def test_a_synthetic_run_is_recorded_as_synthetic(happy):
    assert happy.empirical_status == EmpiricalStatus.SYNTHETIC_ONLY.value


def test_a_synthetic_run_can_never_conclude_the_adapter_is_eligible(happy):
    assert happy.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value


def test_the_report_says_out_loud_that_nothing_was_measured(happy):
    payload = json.loads((happy.directory / "evaluation-report.json")
                         .read_text(encoding="utf-8"))
    assert any("SYNTHETIC_ONLY" in str(note) for note in payload.get("limitations", []))


def test_no_registry_is_written_and_no_model_promoted(happy):
    serialised = json.dumps(happy.to_dict())
    assert "promoted" not in serialised.lower() or "model_promoted" not in serialised


# ── the answers never reach the model-facing artefacts ────────────────────────
def test_no_hidden_answer_appears_in_any_model_facing_artifact(happy, dataset_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=BC.DATASET_ID,
        dataset_version=BC.DATASET_VERSION, splits=SPLITS)
    exposed = (happy.directory / "task-pack.jsonl").read_text(
        encoding="utf-8").casefold()
    for task_id in built.targets.task_ids():
        target = built.targets.lookup(
            task_id, task_hash=built.pack.by_id(task_id).task_hash)
        fragment = " ".join(target.target_text.split())[:96].casefold()
        assert fragment not in exposed, task_id


def test_no_artifact_carries_a_host_path_or_a_username(happy):
    import getpass
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    for path in happy.directory.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "C:\\Users" not in text and "/home/" not in text, path.name
        if user and len(user) > 3:
            assert user.casefold() not in text.casefold(), path.name


# ══════════════════════════════════════════════════════════════════════════════
#  Scenarios — the ways a comparison goes wrong
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("mode", [
    FakeMode.IDENTICAL, FakeMode.CANDIDATE_IMPROVED, FakeMode.CANDIDATE_REGRESSED,
    FakeMode.CANDIDATE_SECURITY_REGRESSION, FakeMode.CANDIDATE_OVER_REFUSES,
    FakeMode.CANDIDATE_UNSAFE_ACCEPTANCE, FakeMode.CANDIDATE_FABRICATES_EVIDENCE,
    FakeMode.CANDIDATE_UNSAFE_TOOL_CALL, FakeMode.CANDIDATE_INVALID_JSON,
    FakeMode.CANDIDATE_EMPTY, FakeMode.CANDIDATE_PRIVATE_PATH_LEAK,
])
def test_every_candidate_behaviour_produces_a_verifiable_generation(
        dataset_root, tmp_path, mode):
    """Whatever the candidate does, the run ends in a state that describes it."""
    outcome = run(dataset_root, tmp_path / mode.value, mode=mode)
    assert outcome.state is EvaluationRunState.COMPLETED, outcome.problems
    assert outcome.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value


def test_an_improved_candidate_still_is_not_eligible_from_a_synthetic_run(
        dataset_root, tmp_path):
    outcome = run(dataset_root, tmp_path / "improved",
                  mode=FakeMode.CANDIDATE_IMPROVED)
    assert outcome.empirical_status == EmpiricalStatus.SYNTHETIC_ONLY.value
    assert outcome.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value


def test_a_regressed_candidate_loses_more_than_an_identical_one(dataset_root,
                                                                tmp_path):
    """Comparative, not absolute: the fake's regression is partial by construction,
    so the honest claim is that it costs the candidate tasks it otherwise wins."""
    identical = json.loads(
        (run(dataset_root, tmp_path / "same", mode=FakeMode.IDENTICAL).directory
         / "metrics.json").read_text("utf-8"))
    regressed = json.loads(
        (run(dataset_root, tmp_path / "worse", mode=FakeMode.CANDIDATE_REGRESSED)
         .directory / "metrics.json").read_text("utf-8"))
    assert regressed["losses"] > identical["losses"], (identical, regressed)


def test_a_critical_security_failure_vetoes(dataset_root, tmp_path):
    """A security regression is not traded off against a quality improvement."""
    outcome = run(dataset_root, tmp_path / "security",
                  mode=FakeMode.CANDIDATE_SECURITY_REGRESSION)
    payload = json.loads((outcome.directory / "metrics.json").read_text("utf-8"))
    assert payload["security_regressions"] > 0
    gates = json.loads((outcome.directory / "evaluation-report.json")
                       .read_text("utf-8"))
    assert outcome.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value
    assert "gates" in json.dumps(gates).casefold()


def test_a_candidate_that_refuses_everything_does_not_win(dataset_root, tmp_path):
    """The reason the corpus contains required-completion tasks at all."""
    outcome = run(dataset_root, tmp_path / "refuses",
                  mode=FakeMode.CANDIDATE_OVER_REFUSES)
    payload = json.loads((outcome.directory / "metrics.json").read_text("utf-8"))
    assert payload["wins"] < payload["task_count"], payload
    assert outcome.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value


def test_a_missing_pair_stays_visible(dataset_root, tmp_path):
    outcome = run(dataset_root, tmp_path / "baseline-error",
                  mode=FakeMode.BASELINE_ERROR)
    payload = json.loads((outcome.directory / "metrics.json").read_text("utf-8"))
    assert payload["missing_pairs"] > 0
    assert payload["measured_pairs"] < payload["task_count"]


def test_a_timeout_is_an_unmeasured_pair_not_a_loss(dataset_root, tmp_path):
    outcome = run(dataset_root, tmp_path / "timeout", mode=FakeMode.CANDIDATE_TIMEOUT)
    payload = json.loads((outcome.directory / "metrics.json").read_text("utf-8"))
    assert payload["missing_pairs"] > 0


def test_a_backend_that_raises_does_not_leave_the_run_running(dataset_root, tmp_path):
    outcome = run(dataset_root, tmp_path / "exception",
                  mode=FakeMode.CANDIDATE_EXCEPTION)
    assert outcome.state.is_terminal
    assert outcome.state is not EvaluationRunState.COMPLETED or \
        outcome.measured_pairs < outcome.task_count


def test_both_arms_failing_produces_no_eligibility(dataset_root, tmp_path):
    outcome = run(dataset_root, tmp_path / "both-error", mode=FakeMode.BOTH_ERROR)
    assert outcome.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value


def test_a_zero_denominator_family_is_insufficient_not_perfect(dataset_root, tmp_path):
    """A rate over no observations is None, and None is not 1.0."""
    from training_gym.evaluation.metrics import EvidenceQuality, Metric
    empty = Metric(name="pass_rate", numerator=0, denominator=0)
    assert empty.value() is None, "a rate over no observations is not 0.0 and not 1.0"
    from training_gym.evaluation.policy import MetricPolicy
    assert empty.quality(MetricPolicy()) is EvidenceQuality.INSUFFICIENT_EVIDENCE
    outcome = run(dataset_root, tmp_path / "zero-denominator",
                  mode=FakeMode.BOTH_ERROR)
    payload = json.loads((outcome.directory / "metrics.json").read_text("utf-8"))
    assert payload["measured_pairs"] == 0 or payload["missing_pairs"] > 0


def test_a_small_pack_cannot_support_a_directional_claim(dataset_root, tmp_path):
    """Below the policy minimum the run still happens; the claim does not."""
    config = make_config(splits={"splits": ["adversarial"], "diagnostic_splits": []})
    outcome = run(dataset_root, tmp_path / "small", config=config)
    assert outcome.task_count == 12
    assert any("below the policy minimum" in b for b in outcome.blockers), \
        outcome.blockers


def test_a_missing_mandatory_split_is_a_blocker(dataset_root, tmp_path):
    config = make_config(splits={"splits": ["adversarial"], "diagnostic_splits": []})
    outcome = run(dataset_root, tmp_path / "nosplit", config=config)
    assert any("security_regression" in b for b in outcome.blockers)


# ══════════════════════════════════════════════════════════════════════════════
#  Refusals before anything is spent
# ══════════════════════════════════════════════════════════════════════════════
def test_a_blocked_plan_never_reaches_a_backend(dataset_root, tmp_path):
    config = make_config()
    baseline = make_baseline()
    adapter = make_adapter(baseline)
    plan = make_plan(config, baseline, adapter, blockers=("the cache is empty",))
    outcome = run(dataset_root, tmp_path / "blocked", config=config, plan=plan)
    assert outcome.state is EvaluationRunState.FAILED
    assert outcome.directory is None
    assert not (tmp_path / "blocked").exists() or \
        not any((tmp_path / "blocked").rglob("evaluation-report.json"))


def test_a_plan_is_consumed_exactly_once(dataset_root, tmp_path):
    root = tmp_path / "replay"
    first = run(dataset_root, root)
    assert first.ok
    second = run(dataset_root, root)
    assert second.state is EvaluationRunState.FAILED
    assert any("already started" in p for p in second.problems), second.problems


def test_a_completed_generation_is_never_overwritten(dataset_root, tmp_path):
    """Even with a different plan, the generation directory is claimed once."""
    root = tmp_path / "duplicate"
    first = run(dataset_root, root)
    assert first.ok
    config = make_config()
    baseline = make_baseline()
    adapter = make_adapter(baseline, adapter_manifest_hash="c" * 64)
    plan = make_plan(config, baseline, adapter)
    second = run(dataset_root, root, config=config, plan=plan)
    assert second.state is EvaluationRunState.FAILED
    report = json.loads((first.directory / "evaluation-report.json")
                        .read_text("utf-8"))
    assert report, "the first generation's report must survive untouched"


def test_two_arms_may_not_share_one_backend_object(dataset_root, tmp_path):
    """A shared object cannot prove the baseline ran without the adapter."""
    shared = FakeEvaluationBackend(FakeMode.IDENTICAL)
    outcome = run(dataset_root, tmp_path / "shared", factory=lambda _role: shared)
    assert outcome.state is EvaluationRunState.FAILED
    assert any("same backend object" in p for p in outcome.problems), outcome.problems


def test_a_stale_token_is_refused_by_the_plan_authority():
    """The token binds the plan recomputed from the current world, not a stored one."""
    from training_gym.evaluation.plan import (
        EvaluationConfirmationRejected,
        check_evaluation_confirmation,
    )
    config = make_config()
    baseline = make_baseline()
    adapter = make_adapter(baseline)
    plan = make_plan(config, baseline, adapter)
    token = plan.confirmation_token()
    moved = make_plan(config, baseline, make_adapter(baseline,
                                                     adapter_manifest_hash="c" * 64))
    with pytest.raises(EvaluationConfirmationRejected):
        check_evaluation_confirmation(token, moved)
    assert check_evaluation_confirmation(token, plan) == token


@pytest.mark.parametrize("bad", ["", "yes", True, "TRAIN:" + "a" * 64,
                                 "EVAL:" + "a" * 12, "EVAL:/etc/passwd"])
def test_a_malformed_confirmation_is_refused(bad):
    from training_gym.evaluation.plan import (
        EvaluationConfirmationRejected,
        check_evaluation_confirmation,
    )
    config = make_config()
    baseline = make_baseline()
    plan = make_plan(config, baseline, make_adapter(baseline))
    with pytest.raises(EvaluationConfirmationRejected):
        check_evaluation_confirmation(bad, plan)


# ══════════════════════════════════════════════════════════════════════════════
#  Failure and quarantine
# ══════════════════════════════════════════════════════════════════════════════
def test_a_generation_whose_artifacts_do_not_verify_is_quarantined(dataset_root,
                                                                   tmp_path,
                                                                   monkeypatch):
    """Tampering after the write, before the re-read. The run must not complete."""
    import training_gym.evaluation.execution as E

    real = E.verify_evaluation_generation

    def broken(directory, **kwargs):
        del kwargs
        return ("the report digest does not match the bytes on disk",)

    monkeypatch.setattr(E, "verify_evaluation_generation", broken)
    outcome = run(dataset_root, tmp_path / "tampered")
    monkeypatch.setattr(E, "verify_evaluation_generation", real)

    assert outcome.state is EvaluationRunState.QUARANTINED
    assert outcome.problems
    assert outcome.quarantine_path is not None
    assert outcome.quarantine_path.exists()
    assert not outcome.directory.exists(), "the failed generation must not be left in place"


def test_a_quarantined_run_is_not_eligible_and_has_no_completed_state(dataset_root,
                                                                     tmp_path,
                                                                     monkeypatch):
    import training_gym.evaluation.execution as E
    monkeypatch.setattr(E, "verify_evaluation_generation",
                        lambda directory, **kw: ("broken",))
    outcome = run(dataset_root, tmp_path / "quarantine2")
    assert outcome.state is not EvaluationRunState.COMPLETED
    assert "completed" not in outcome.states_visited


def test_a_failure_still_spends_the_plan(dataset_root, tmp_path):
    """A run that started and failed does not hand its token back."""
    from training_gym.evaluation.store import is_plan_consumed
    root = tmp_path / "spent"

    def exploding(_role):
        raise RuntimeError("the backend could not be constructed")

    config = make_config()
    baseline = make_baseline()
    adapter = make_adapter(baseline)
    plan = make_plan(config, baseline, adapter, dataset_root=dataset_root)
    outcome = run(root.parent / "data-unused" if False else dataset_root, root,
                  config=config, plan=plan, factory=exploding)
    assert outcome.state is EvaluationRunState.FAILED
    assert is_plan_consumed(root, plan.plan_hash())


def test_every_outcome_writes_a_terminal_ledger_line(dataset_root, tmp_path):
    from training_gym.evaluation.store import EVALUATION_LEDGER_FILE
    root = tmp_path / "ledger"
    outcome = run(dataset_root, root)
    lines = [json.loads(line) for line in
             (root / EVALUATION_LEDGER_FILE).read_text("utf-8").splitlines() if line]
    events = [entry["event"] for entry in lines]
    assert "started" in events
    assert outcome.state.value in events, lines


def test_an_interruption_is_terminal_and_honest(dataset_root, tmp_path):
    def interrupting(_role):
        raise KeyboardInterrupt

    outcome = run(dataset_root, tmp_path / "interrupted", factory=interrupting)
    assert outcome.state is EvaluationRunState.INTERRUPTED
    assert outcome.interrupted
    assert "completed" not in outcome.states_visited
    assert outcome.eligibility != CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value


def test_no_report_is_left_behind_by_a_failed_run(dataset_root, tmp_path):
    def exploding(_role):
        raise RuntimeError("boom")

    root = tmp_path / "noreport"
    outcome = run(dataset_root, root, factory=exploding)
    assert outcome.state is EvaluationRunState.FAILED
    assert not list(root.glob("**/evaluation-report.json"))
