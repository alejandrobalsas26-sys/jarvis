"""V69 M62 S3C — the backends, the closed registry and the paired runner.

WHAT IS BEING PROVED
--------------------
That the two arms are asked the same question, that the fake backend cannot be reached
from production, that the production backend's safety-critical arguments are pinned in
its source, and that a task which went badly stays in the sample.

WHAT IS NOT BEING PROVED
------------------------
That the production backend works. It has never been run against a model, and no test
here imports a framework — "it generates" is a claim only a live run can support.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.evaluation import runner as RUN
from training_gym.evaluation.backend import (
    BackendErrorCategory,
    BackendStatus,
    CancellationToken,
    CleanupStatus,
    EvaluationBackendError,
    EvaluationRequest,
    EvaluationResult,
    FinishReason,
    check_result_matches_request,
    require_backend,
)
from training_gym.evaluation.backends import (
    PRODUCTION_BACKEND_IDS,
    available_backends,
    get_backend,
)
from training_gym.evaluation.backends.fake import FakeEvaluationBackend, FakeMode
from training_gym.evaluation.generation import GenerationPolicy
from training_gym.evaluation.policy import ResourceCeilings
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
           "bitsandbytes", "huggingface_hub", "tokenizers", "safetensors")
_REV = "a" * 40


def _ml_modules() -> list[str]:
    return sorted(set(_BANNED) & set(sys.modules))


# ── fixtures ──────────────────────────────────────────────────────────────────
def _identity() -> ModelIdentity:
    return ModelIdentity(provider="huggingface", model_id="Qwen/Qwen3-0.6B",
                         revision=_REV, parameters_b=0.6,
                         tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=_REV,
                         cache_status=CacheStatus.PRESENT, cache_evidence="c" * 64)


def _baseline() -> BaseModelEvaluationReference:
    return base_reference_from_identity(_identity())


def _adapter() -> AdapterEvaluationReference:
    base = _identity()
    manifest = AdapterManifest(
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
        split_algorithm_version="m62.split.1", seed=11,
        lora={"r": 8, "lora_alpha": 16}, assistant_only_loss={"enabled": True},
        package_versions={"peft": "0.12.0"}, device_category="cpu", precision="fp32",
        requested_steps=10, completed_steps=10, epochs_completed=1.0, train_loss=0.5,
        eval_loss=0.6, truncated_records=0, duration_seconds=12.0, files=(),
        total_bytes=1024, tree_hash="b" * 64, created_at_utc="2026-08-02T00:00:00Z")
    return reference_from_manifest(manifest, artifact_verified=True)


def _task(index: int = 0, *, kind=EvaluationTaskKind.COMPLETION,
          split=DatasetSplit.HIDDEN_EVALUATION, family=TaskFamily.SOC_TRIAGE,
          tool_schemas=()) -> EvaluationTask:
    return EvaluationTask(
        task_id=f"task-{index:04d}", task_family=family, task_hash=f"{index:064x}",
        split=split, kind=kind,
        system_prompt="You are a SOC analyst.",
        user_prompt=f"Triage alert {index}.",
        source_dataset_manifest_hash="a" * 64, source_shard_hash="b" * 64,
        input_record_hash="c" * 64, evidence_ids=("e" * 64, "f" * 64),
        tool_schemas=tuple(tool_schemas),
        grader_ids=("json_schema", "secret_pii"),
        mandatory_grader_ids=("json_schema", "secret_pii"),
        refusal_expected=kind is EvaluationTaskKind.REQUIRED_REFUSAL,
        security_required=split is DatasetSplit.SECURITY_REGRESSION,
        lineage_group="alerts")


def _pack(n: int = 6) -> EvaluationTaskPack:
    tasks = [_task(i) for i in range(n - 2)]
    tasks.append(_task(n - 2, kind=EvaluationTaskKind.REQUIRED_REFUSAL,
                       split=DatasetSplit.SECURITY_REGRESSION,
                       family=TaskFamily.SAFETY_REFUSAL))
    tasks.append(_task(n - 1, kind=EvaluationTaskKind.REQUIRED_COMPLETION,
                       split=DatasetSplit.SECURITY_REGRESSION))
    return EvaluationTaskPack(tasks=tuple(tasks), dataset_id="soc-gym",
                              dataset_version="v1")


def _run(mode: FakeMode = FakeMode.IDENTICAL, *, pack=None, seed: int = 11,
         **kwargs) -> RUN.PairedRun:
    return RUN.run_paired_evaluation(
        pack or _pack(), baseline_backend=FakeEvaluationBackend(mode),
        candidate_backend=FakeEvaluationBackend(mode),
        baseline_reference=_baseline(), adapter_reference=_adapter(),
        generation=GenerationPolicy(seed=seed), seed=seed, **kwargs)


def _request(role=EvaluationRole.BASELINE, **kwargs) -> EvaluationRequest:
    defaults = dict(role=role, task=_task(), generation=GenerationPolicy(),
                    baseline=_baseline(),
                    adapter=_adapter() if role is EvaluationRole.CANDIDATE else None)
    defaults.update(kwargs)
    return EvaluationRequest(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
#  The closed registry
# ══════════════════════════════════════════════════════════════════════════════
def test_only_reviewed_backends_are_selectable():
    assert available_backends() == PRODUCTION_BACKEND_IDS == ("transformers_peft",)


@pytest.mark.parametrize("name", [
    "fake", "fake_evaluation", "fake_deterministic", "mock", "stub", "null", "echo",
    "", "  ", "../fake", "backends.fake", "training_gym.evaluation.backends.fake",
    "training_gym.evaluation.backends.fake:FakeEvaluationBackend",
    "/usr/bin/python", "transformers_peft.py", None, 0,
])
def test_no_name_outside_the_reviewed_list_resolves(name):
    with pytest.raises(EvaluationBackendError):
        get_backend(name)


def test_the_fake_backend_is_unreachable_from_a_production_configuration():
    """Two independent reasons it can never make a candidate eligible; this is the
    first. The second — a synthetic report cannot yield eligibility — is asserted in
    the gates tests."""
    with pytest.raises(EvaluationBackendError, match="test double"):
        get_backend(FakeEvaluationBackend().backend_id)


def test_importing_the_registry_loads_no_framework():
    import importlib
    before = set(sys.modules)
    importlib.reload(importlib.import_module("training_gym.evaluation.backends"))
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  The request is the access-control policy
# ══════════════════════════════════════════════════════════════════════════════
def test_the_request_has_no_field_that_could_carry_an_answer():
    fields = {f.name for f in dataclasses.fields(EvaluationRequest)}
    forbidden = {"hidden_target", "hidden_targets", "target", "target_text", "expected",
                 "expected_output", "answer", "rubric", "gold", "solution",
                 "ground_truth", "counterpart_response", "baseline_response",
                 "candidate_response", "teacher_answer", "human_review",
                 "registry_decision", "store", "hidden_target_store"}
    assert fields & forbidden == set()


def test_the_baseline_arm_may_not_carry_an_adapter():
    """If the baseline is measured with the adapter attached, the comparison is between
    two identical models and the delta is noise."""
    with pytest.raises(EvaluationBackendError, match="delta is noise"):
        _request(EvaluationRole.BASELINE, adapter=_adapter())


def test_the_candidate_arm_requires_the_adapter_it_is_measuring():
    with pytest.raises(EvaluationBackendError, match="requires the adapter"):
        EvaluationRequest(role=EvaluationRole.CANDIDATE, task=_task(),
                          generation=GenerationPolicy(), baseline=_baseline())


def test_an_adapter_may_not_be_supplied_as_a_bare_path():
    with pytest.raises(EvaluationBackendError, match="never a path"):
        EvaluationRequest(role=EvaluationRole.CANDIDATE, task=_task(),
                          generation=GenerationPolicy(), baseline=_baseline(),
                          adapter="/tmp/adapters/run-0001")  # type: ignore[arg-type]


def test_a_raw_dictionary_is_never_an_acceptable_task():
    with pytest.raises(EvaluationBackendError, match="answer-exposure screen"):
        EvaluationRequest(role=EvaluationRole.BASELINE,
                          task={"task_id": "x"},  # type: ignore[arg-type]
                          generation=GenerationPolicy(), baseline=_baseline())


def test_the_parity_hash_covers_the_question_and_excludes_the_arm():
    """The two arms must differ in exactly one respect, and it is not the question."""
    base = _request(EvaluationRole.BASELINE)
    cand = _request(EvaluationRole.CANDIDATE)
    assert base.parity_hash() == cand.parity_hash()
    assert base.parity_hash() != _request(
        EvaluationRole.BASELINE, task=_task(1)).parity_hash()
    assert base.parity_hash() != _request(
        EvaluationRole.BASELINE,
        generation=GenerationPolicy(max_new_tokens=64)).parity_hash()


def test_the_fake_backend_cannot_reach_a_hidden_target_from_a_request():
    """Asserted from the attacker's side: the double actively probes every surface an
    answer could hide behind, and raises if any of them exists."""
    backend = FakeEvaluationBackend(FakeMode.HIDDEN_TARGET_PROBE)
    result = backend.generate(_request())
    assert result.ok


# ══════════════════════════════════════════════════════════════════════════════
#  Result binding
# ══════════════════════════════════════════════════════════════════════════════
def test_a_result_for_the_wrong_task_is_refused():
    request = _request()
    result = FakeEvaluationBackend(FakeMode.WRONG_TASK).generate(request)
    with pytest.raises(EvaluationBackendError, match="another question"):
        check_result_matches_request(result, request)


def test_a_result_labelled_with_the_wrong_arm_is_refused():
    """Swapped arms would give the reported delta the wrong sign."""
    request = _request(EvaluationRole.CANDIDATE)
    result = FakeEvaluationBackend(FakeMode.WRONG_ROLE).generate(request)
    with pytest.raises(EvaluationBackendError, match="wrong sign"):
        check_result_matches_request(result, request)


def test_a_result_produced_under_a_different_request_is_refused():
    request = _request()
    result = FakeEvaluationBackend(FakeMode.WRONG_PARITY).generate(request)
    with pytest.raises(EvaluationBackendError, match="not asked the same question"):
        check_result_matches_request(result, request)


def test_a_succeeded_result_may_not_also_report_an_error():
    with pytest.raises(EvaluationBackendError, match="did not happen"):
        EvaluationResult(backend_id="x", backend_version="1",
                         role=EvaluationRole.BASELINE, task_id="t", task_hash="0" * 64,
                         status=BackendStatus.SUCCEEDED,
                         error_category=BackendErrorCategory.TIMEOUT)


def test_a_failure_with_no_category_is_refused():
    """A failure nobody categorised aggregates into nothing."""
    with pytest.raises(EvaluationBackendError, match="aggregates into nothing"):
        EvaluationResult(backend_id="x", backend_version="1",
                         role=EvaluationRole.BASELINE, task_id="t", task_hash="0" * 64,
                         status=BackendStatus.FAILED)


def test_a_truncation_without_a_count_is_refused():
    with pytest.raises(EvaluationBackendError, match="cannot be checked"):
        EvaluationResult(backend_id="x", backend_version="1",
                         role=EvaluationRole.BASELINE, task_id="t", task_hash="0" * 64,
                         status=BackendStatus.SUCCEEDED, response_text="hi",
                         input_truncated=True, truncated_tokens=0)


def test_a_result_record_publishes_a_digest_rather_than_the_response_text():
    result = FakeEvaluationBackend().generate(_request())
    record = json.dumps(result.to_record())
    assert "response_text" not in record
    assert result.to_dict()["response_sha256"] in record
    assert len(result.result_hash()) == 64


def test_the_result_hash_covers_the_response_text_itself():
    """Two results that look identical in the record but answered differently must not
    share an identity."""
    base = FakeEvaluationBackend().generate(_request())
    altered = dataclasses.replace(base, response_text=base.response_text + " ")
    assert altered.result_hash() != base.result_hash()


def test_an_object_that_only_looks_like_a_backend_is_refused():
    class NotABackend:
        backend_id = "pretend"

    with pytest.raises(EvaluationBackendError, match="missing callable"):
        require_backend(NotABackend())


def test_a_backend_that_cannot_name_itself_is_refused():
    class Anonymous:
        backend_id = ""

        def version(self):
            return "1"

    with pytest.raises(EvaluationBackendError, match="cannot name itself"):
        require_backend(Anonymous())


# ══════════════════════════════════════════════════════════════════════════════
#  Execution order
# ══════════════════════════════════════════════════════════════════════════════
def test_the_execution_order_is_deterministic():
    pack = _pack(20)
    assert RUN.order_assignment(pack, seed=11) == RUN.order_assignment(pack, seed=11)
    assert RUN.order_assignment_hash(pack, seed=11) == \
        RUN.order_assignment_hash(pack, seed=11)


def test_the_execution_order_changes_with_the_seed():
    pack = _pack(20)
    assert RUN.order_assignment_hash(pack, seed=11) != \
        RUN.order_assignment_hash(pack, seed=12)


def test_neither_arm_always_answers_first():
    """Always running the baseline first would make 'first' and 'baseline' the same
    variable, and any order effect would be read as an adapter effect."""
    first, second = RUN.order_balance(_pack(60), seed=11)
    assert first > 5 and second > 5, (first, second)


def test_the_order_does_not_depend_on_a_tasks_position_in_the_pack():
    pack = _pack(12)
    reversed_pack = EvaluationTaskPack(tasks=tuple(reversed(pack.tasks)),
                                       dataset_id="soc-gym", dataset_version="v1")
    assert RUN.order_assignment(pack, seed=11) == \
        RUN.order_assignment(reversed_pack, seed=11)


def test_the_order_is_recorded_in_every_pair():
    run = _run()
    assert {p.order for p in run.generations} <= set(RUN.ExecutionOrder)
    assert run.order_policy == RUN.ORDER_POLICY_BALANCED


# ══════════════════════════════════════════════════════════════════════════════
#  The paired runner
# ══════════════════════════════════════════════════════════════════════════════
def test_every_task_yields_exactly_one_pair():
    pack = _pack(8)
    run = _run(pack=pack)
    assert len(run) == len(pack)
    assert {p.task_id for p in run.generations} == {t.task_id for t in pack.tasks}
    assert run.blockers == ()


def test_both_arms_are_asked_an_identical_question():
    for pair in _run().generations:
        assert pair.baseline is not None and pair.candidate is not None
        assert pair.baseline.request_parity_hash == pair.parity_hash
        assert pair.candidate.request_parity_hash == pair.parity_hash


def test_the_baseline_arm_is_a_separate_object_that_never_receives_an_adapter():
    """Separate backend objects make 'the baseline had no adapter' a structural fact
    rather than a claim about what a shared object was toggled to."""
    seen: list[tuple[str, bool]] = []

    class Recording(FakeEvaluationBackend):
        def generate(self, request):
            seen.append((request.role.value, request.adapter is not None))
            return super().generate(request)

    RUN.run_paired_evaluation(
        _pack(4), baseline_backend=Recording(), candidate_backend=Recording(),
        baseline_reference=_baseline(), adapter_reference=_adapter(),
        generation=GenerationPolicy(seed=11), seed=11)
    assert ("baseline", True) not in seen
    assert ("candidate", False) not in seen
    assert seen.count(("baseline", False)) == seen.count(("candidate", True)) == 4


def test_the_run_is_deterministic():
    assert _run().run_hash() == _run().run_hash()


def test_a_nondeterministic_backend_is_visible_in_the_run_hash():
    """Not a refusal — the runner cannot know which draw was 'right'. It is VISIBLE,
    which is what a report needs in order to be honest about it."""
    backend = FakeEvaluationBackend(FakeMode.NONDETERMINISTIC)
    request = _request()
    assert backend.generate(request).result_hash() != \
        backend.generate(request).result_hash()
    assert _run(FakeMode.NONDETERMINISTIC).run_hash() != \
        _run(FakeMode.NONDETERMINISTIC).run_hash()


def test_the_two_arms_may_not_be_the_same_backend_object():
    """A shared object would make 'the baseline had no adapter' a claim about what it
    was toggled to, and a toggle that failed to reset measures the candidate twice."""
    shared = FakeEvaluationBackend()
    with pytest.raises(RUN.RunnerError, match="same backend object"):
        RUN.run_paired_evaluation(
            _pack(2), baseline_backend=shared, candidate_backend=shared,
            baseline_reference=_baseline(), adapter_reference=_adapter(),
            generation=GenerationPolicy(seed=11), seed=11)


@pytest.mark.parametrize("mode,expected", [
    (FakeMode.IDENTICAL, RUN.PairedStatus.BOTH_MEASURED),
    (FakeMode.BASELINE_ERROR, RUN.PairedStatus.BASELINE_ERROR),
    (FakeMode.BOTH_ERROR, RUN.PairedStatus.BOTH_ERROR),
    (FakeMode.CANDIDATE_TIMEOUT, RUN.PairedStatus.CANDIDATE_ERROR),
    (FakeMode.CANDIDATE_EXCEPTION, RUN.PairedStatus.CANDIDATE_ERROR),
    (FakeMode.CANDIDATE_EMPTY, RUN.PairedStatus.INSUFFICIENT_EVIDENCE),
    (FakeMode.CANDIDATE_OVERSIZED, RUN.PairedStatus.INSUFFICIENT_EVIDENCE),
    (FakeMode.CANDIDATE_TRUNCATED, RUN.PairedStatus.INSUFFICIENT_EVIDENCE),
])
def test_each_failure_shape_is_reported_as_itself(mode, expected):
    run = _run(mode)
    statuses = {p.status for p in run.generations}
    assert expected in statuses, statuses


def test_a_timeout_stays_in_the_denominator():
    """Dropping it is the mechanism by which a model that times out on every hard task
    reports a perfect score on the easy ones."""
    pack = _pack(6)
    run = _run(FakeMode.CANDIDATE_TIMEOUT, pack=pack)
    assert len(run) == len(pack)
    assert run.measured_pairs + run.missing_pairs == len(pack)
    assert run.missing_pairs == len(pack)


def test_an_exception_becomes_a_recorded_failure_and_not_a_disappearance():
    pack = _pack(5)
    run = _run(FakeMode.CANDIDATE_EXCEPTION, pack=pack)
    assert len(run) == len(pack)
    failures = [p for p in run.generations if p.candidate and not p.candidate.ok]
    assert failures
    assert all(f.candidate.error_category is BackendErrorCategory.BACKEND
               for f in failures)


def test_a_backend_that_returns_the_wrong_type_is_recorded_as_a_failure():
    class Broken(FakeEvaluationBackend):
        def generate(self, request):
            return {"response": "not an EvaluationResult"}

    run = RUN.run_paired_evaluation(
        _pack(3), baseline_backend=Broken(), candidate_backend=Broken(),
        baseline_reference=_baseline(), adapter_reference=_adapter(),
        generation=GenerationPolicy(seed=11), seed=11)
    assert len(run) == 3
    assert all(p.status is RUN.PairedStatus.BOTH_ERROR for p in run.generations)
    assert "never authoritative" in run.generations[0].baseline.error_message


def test_a_misfiled_result_blocks_the_pair_rather_than_being_accepted():
    run = _run(FakeMode.WRONG_TASK)
    assert all(p.blockers for p in run.generations)
    assert run.measured_pairs == 0


def test_partial_coverage_is_refused_as_cherry_picking():
    pack = _pack(6)
    run = _run(pack=pack)
    short_run = dataclasses.replace(run, generations=run.generations[:3])
    problems = short_run.coverage_blockers(pack)
    assert any("cherry-picking" in p for p in problems)


def test_a_result_naming_a_task_outside_the_pack_is_refused():
    pack = _pack(4)
    run = _run(pack=pack)
    stray = dataclasses.replace(run.generations[0], task_id="not-in-the-pack")
    altered = dataclasses.replace(run, generations=run.generations + (stray,))
    assert any("not in the pack" in p for p in altered.coverage_blockers(pack))


def test_an_interruption_stops_the_run_and_is_never_a_completion():
    token = CancellationToken()
    token.request("operator pressed ctrl-c")
    run = _run(cancellation=token)
    assert run.interrupted is True
    assert any("not a measurement" in b for b in run.blockers)


def test_a_cleanup_failure_makes_the_whole_run_untrustworthy():
    """The failure mode is one candidate's adapter still being attached to what a later
    run calls a baseline."""
    run = _run(FakeMode.CLEANUP_FAILURE)
    assert run.cleanup_status is CleanupStatus.FAILED
    assert any("still be attached" in b for b in run.blockers)


def test_the_worst_cleanup_status_wins():
    assert RUN._release_both(FakeEvaluationBackend(FakeMode.CLEANUP_FAILURE),
                             FakeEvaluationBackend()) is CleanupStatus.FAILED


def test_a_pack_larger_than_the_ceiling_is_refused():
    with pytest.raises(RUN.RunnerError, match="exceeds the ceiling"):
        _run(pack=_pack(8), resources=ResourceCeilings(max_tasks=2))


def test_a_proposed_tool_call_is_recorded_and_never_executed():
    """The runner reads the proposal. Nothing anywhere here runs it."""
    import subprocess

    def _refuse(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("an evaluation must never execute a proposed tool call")

    tools = ({"name": "shell_exec", "parameters": {"type": "object"}},)
    pack = EvaluationTaskPack(
        tasks=(_task(0, tool_schemas=tools), _task(1, tool_schemas=tools)),
        dataset_id="soc-gym", dataset_version="v1")
    saved = subprocess.Popen, subprocess.run
    subprocess.Popen, subprocess.run = _refuse, _refuse
    try:
        run = _run(FakeMode.CANDIDATE_UNSAFE_TOOL_CALL, pack=pack)
    finally:
        subprocess.Popen, subprocess.run = saved
    proposals = [p.candidate.proposed_tool_calls for p in run.generations]
    assert any(calls and calls[0]["name"] == "shell_exec" for calls in proposals)


def test_no_run_opens_a_socket_or_spawns_a_process():
    import socket
    import subprocess

    def _refuse(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("an evaluation must not do this")

    saved = (socket.socket, socket.create_connection, subprocess.Popen, subprocess.run)
    socket.socket, socket.create_connection = _refuse, _refuse
    subprocess.Popen, subprocess.run = _refuse, _refuse
    try:
        run = _run()
    finally:
        (socket.socket, socket.create_connection, subprocess.Popen,
         subprocess.run) = saved
    assert run.measured_pairs == len(run)


def test_a_complete_fake_backed_run_imports_no_framework():
    before = set(sys.modules)
    _run().run_hash()
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []
    assert _ml_modules() == []


# ══════════════════════════════════════════════════════════════════════════════
#  The production backend — structure only
# ══════════════════════════════════════════════════════════════════════════════
def _backend_module():
    from training_gym.evaluation.backends import transformers_peft
    return transformers_peft


def _backend_code() -> str:
    """The module's source with every docstring stripped, so a substring assertion
    cannot be satisfied by prose that merely describes the safe behaviour."""
    module = _backend_module()
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_the_production_backend_imports_no_framework_at_module_level():
    before = set(sys.modules)
    module = _backend_module()
    module.TransformersPeftEvaluationBackend()
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []
    assert _ml_modules() == []


def test_every_framework_import_lives_in_exactly_one_function():
    code = _backend_code()
    assert code.count("__import__(name)") == 1
    for name in ("import torch", "import transformers", "import peft"):
        assert f"\n{name}" not in code, f"{name} appears at module scope"


@pytest.mark.parametrize("fragment", [
    "trust_remote_code=TRUST_REMOTE_CODE",
    "local_files_only=LOCAL_FILES_ONLY",
    "revision=baseline.base_model_revision",
    "revision=baseline.tokenizer_revision",
    "return_dict=False",
])
def test_the_production_backend_pins_every_safety_critical_argument(fragment):
    assert fragment in _backend_code()


def test_every_weight_load_binds_the_reviewed_model_cache_root():
    """``readiness`` refuses a request naming no ``model_cache_root``; the loads must
    then actually read THAT root.

    Left unbound, ``from_pretrained`` resolves against the default hub cache. With
    ``local_files_only=True`` a model cached in the reviewed root then reports as not
    cached at all, so the run consumes its plan and fails every task -- measured against
    transformers 5.14.1, where the unbound call raises OSError for a model that is
    present in the reviewed cache. Checked by AST rather than by substring: the value
    must reach the call, not merely appear in the file.
    """
    tree = ast.parse(Path(_backend_module().__file__).read_text(encoding="utf-8"))
    loads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "from_pretrained"
             and ast.unparse(node.func).startswith("transformers.")]
    assert len(loads) == 2, "expected exactly the tokenizer load and the model load"
    for call in loads:
        assert "cache_dir" in {kw.arg for kw in call.keywords}, (
            f"{ast.unparse(call.func)} does not bind cache_dir, so it would read the "
            f"default hub cache instead of the reviewed root")

    bound = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "cache_dir"
                     for t in node.targets)]
    assert bound, "cache_dir must be bound from the request"
    assert "request.model_cache_root" in ast.unparse(bound[0].value), (
        "cache_dir must derive from the reviewed root the plan verified")


def test_the_chat_template_is_read_as_a_tensor_not_a_batch_encoding():
    """transformers 5 defaults ``apply_chat_template`` to ``return_dict=True``.

    The surrounding code reads ``encoded.shape[-1]`` and passes ``encoded`` positionally
    to ``generate``. A BatchEncoding satisfies neither, so the default must be pinned
    rather than inherited -- measured against transformers 5.14.1, where the unpinned
    call returns a BatchEncoding and the token count raises AttributeError.
    """
    code = _backend_code()
    assert "apply_chat_template(" in code
    call = code[code.index("apply_chat_template("):]
    call = call[:call.index(")") + 1]
    assert "return_dict=False" in call, (
        "apply_chat_template must pin return_dict=False; the library default is True "
        "and returns a BatchEncoding this code cannot consume")


@pytest.mark.parametrize("forbidden", [
    "push_to_hub", "wandb", "mlflow", "report_to", "shell=True", "subprocess",
    "os.system", "pip", "requests.", "urlopen", "socket", "torch.load", "pickle",
    "Trainer(", "TrainingArguments", "DPOTrainer", "SFTTrainer", "get_peft_model",
    "merge_and_unload", "save_pretrained",
])
def test_the_production_backend_contains_no_forbidden_construct(forbidden):
    assert forbidden not in _backend_code()


def test_the_production_backend_calls_no_dynamic_evaluation_builtin():
    """Checked by AST rather than by substring: ``model.eval()`` sets inference mode and
    contains the characters ``eval(``, so a text search would either miss the builtin or
    ban the method."""
    tree = ast.parse(Path(_backend_module().__file__).read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert called & {"eval", "exec", "compile", "open", "input"} == set()


def test_trust_remote_code_is_a_constant_and_never_a_configuration_read():
    module = _backend_module()
    assert module.TRUST_REMOTE_CODE is False
    assert module.LOCAL_FILES_ONLY is True
    code = _backend_code()
    assert "TRUST_REMOTE_CODE = False" in code
    assert "LOCAL_FILES_ONLY = True" in code


def test_the_baseline_path_attaches_no_adapter_and_the_candidate_path_does():
    code = _backend_code()
    assert "if request.is_candidate:" in code
    assert "PeftModel.from_pretrained" in code
    # The adapter load is inside the candidate branch, not before it.
    assert code.index("if request.is_candidate:") < code.index("PeftModel.from_pretrained")


def test_the_backend_verifies_the_adapter_is_actually_active():
    """An adapter that loaded but is not active would produce baseline output under a
    candidate label — the most dangerous silent failure available here."""
    code = _backend_code()
    assert "active_adapter" in code
    assert "no active adapter" in code


def test_the_shared_base_strategy_is_not_selectable():
    module = _backend_module()
    with pytest.raises(ValueError, match="leaves no residue"):
        module.TransformersPeftEvaluationBackend(
            load_strategy=module.LoadStrategy.SHARED_BASE)


def test_the_production_backend_satisfies_the_protocol_without_a_framework():
    backend = require_backend(_backend_module().TransformersPeftEvaluationBackend())
    assert backend.backend_id == "transformers_peft"
    assert backend.version()
    assert backend.release() is CleanupStatus.NOT_REQUIRED
    assert _ml_modules() == []


def test_preflight_refuses_before_importing_anything():
    """Every readiness gate is checkable on a host with no torch installed at all."""
    backend = _backend_module().TransformersPeftEvaluationBackend()
    before = set(sys.modules)
    problems = backend.readiness(_request())
    assert any("model cache root" in p for p in problems)
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []


def test_a_mutable_base_revision_is_refused_at_preflight(tmp_path):
    backend = _backend_module().TransformersPeftEvaluationBackend()
    mutable = base_reference_from_identity(
        ModelIdentity(provider="huggingface", model_id="Qwen/Qwen3-0.6B",
                      revision="main", parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B",
                      tokenizer_revision="main"))
    problems = backend.readiness(_request(baseline=mutable,
                                          model_cache_root=tmp_path))
    assert any("immutable commit" in p for p in problems)


def test_a_missing_adapter_directory_is_refused_at_preflight(tmp_path):
    backend = _backend_module().TransformersPeftEvaluationBackend()
    problems = backend.readiness(_request(
        EvaluationRole.CANDIDATE, model_cache_root=tmp_path,
        adapter_directory=tmp_path / "nope"))
    assert any("not a directory" in p for p in problems)


def test_a_refused_request_returns_a_blocked_result_rather_than_raising(tmp_path):
    """A refusal is a RESULT: a raised exception is how a refused task quietly leaves
    the sample."""
    backend = _backend_module().TransformersPeftEvaluationBackend()
    result = backend.generate(_request())
    assert result.status is BackendStatus.BLOCKED
    assert result.error_category is BackendErrorCategory.CONFIGURATION
    assert result.finish_reason is FinishReason.ERROR
    assert result.task_id == "task-0000"
    assert _ml_modules() == []


def test_a_missing_framework_is_a_named_refusal_and_never_an_install(monkeypatch):
    module = _backend_module()
    monkeypatch.setattr(module, "RUNTIME_PACKAGES", ("definitely_not_installed_pkg",))
    with pytest.raises(module.RuntimeUnavailable, match="nothing here installs"):
        module._runtime()


def test_the_greedy_generation_kwargs_carry_no_sampling_parameter():
    """A temperature passed alongside do_sample=False makes transformers warn about an
    unused setting, and a warning nobody reads is how a sampling parameter survives
    into a run documented as greedy."""
    kwargs = _backend_module().generation_kwargs(GenerationPolicy())
    assert kwargs["do_sample"] is False
    assert "temperature" not in kwargs and "top_p" not in kwargs
    assert kwargs["num_beams"] == 1


def test_both_arms_would_receive_the_same_generation_kwargs():
    module = _backend_module()
    policy = GenerationPolicy(seed=7, max_new_tokens=256)
    assert module.generation_kwargs(policy) == module.generation_kwargs(policy)


def test_a_missing_weights_error_is_not_reported_as_a_full_disk():
    """local_files_only reports a missing model as an OS error; calling that 'the disk
    is full' sends an operator to the wrong problem."""
    module = _backend_module()
    assert module._looks_like_missing_weights(
        OSError("... is not the path to a directory containing a file named "
                "config.json. local_files_only=True"))
    assert not module._looks_like_missing_weights(OSError("No space left on device"))


# ══════════════════════════════════════════════════════════════════════════════
#  Package purity — the non-recursive scan the backends live one level below
# ══════════════════════════════════════════════════════════════════════════════
def _code_identifiers(path: Path) -> set[str]:
    """Every name, dotted attribute, import and non-docstring literal in a module.

    Over the AST rather than the raw text: these modules' docstrings explain at length
    why they never open a socket or import a framework, and that explanation must not
    itself trip the test. The dotted form is kept alongside the bare attribute because
    ``platform.system`` is not ``os.system``, and a check that cannot tell them apart is
    one that has to be weakened until it stops meaning anything.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                found.add(f"{node.value.id}.{node.attr}")
            else:
                found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[0])
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            found.add(node.value)
    return found


def _evaluation_modules() -> list[Path]:
    """The evaluation package's own modules. Deliberately NON-recursive: the production
    backend must eventually import torch, which is exactly why it lives one level
    deeper, in ``backends/``."""
    import training_gym.evaluation as package
    return sorted(Path(package.__file__).parent.glob("*.py"))


@pytest.mark.parametrize("forbidden", [
    "subprocess", "os.system", "os.popen", "os.spawnv", "os.execv", "Popen",
    "check_call", "check_output", "shell", "pip", "uv", "poetry", "conda", "winget",
    "apt", "sudo", "urllib", "requests", "httpx", "socket", "urlopen", "eval", "exec",
    "torch", "transformers", "peft", "trl", "datasets", "accelerate", "bitsandbytes",
    "huggingface_hub", "ollama", "gguf",
])
def test_no_evaluation_module_can_reach_a_framework_or_a_package_manager(forbidden):
    """The DENYLIST is not a capability.

    ``config.FORBIDDEN_CONFIG_FIELDS`` names the fields a config may not carry —
    ``shell``, ``pip``, ``env`` and so on — so those words appear in the source as string
    literals precisely because the code refuses them. Subtracting exactly those keys keeps
    the scan strict everywhere else, and it is derived from the module rather than hard
    coded, so adding a refusal cannot silently widen the exemption to anything else.
    """
    from training_gym.evaluation.config import FORBIDDEN_CONFIG_FIELDS
    denylist = set(FORBIDDEN_CONFIG_FIELDS)
    offenders = []
    for path in _evaluation_modules():
        identifiers = _code_identifiers(path)
        if path.name == "config.py":
            identifiers -= denylist
        if forbidden in identifiers:
            offenders.append(path.name)
    assert offenders == [], f"{forbidden!r} appears in {offenders}"


def test_the_config_denylist_exemption_covers_only_refused_field_names():
    """The exemption above must not be a hole. Every word it excuses is a key whose
    value is the REASON that field is refused, so each one is a rule rather than a use."""
    from training_gym.evaluation.config import FORBIDDEN_CONFIG_FIELDS
    assert "shell" in FORBIDDEN_CONFIG_FIELDS
    assert all(isinstance(reason, str) and reason
               for reason in FORBIDDEN_CONFIG_FIELDS.values())
    # And the exempted words really are absent as executable constructs.
    source = Path(
        __import__("training_gym.evaluation.config", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    for construct in ("shell=True", "subprocess.", "os.system(", "import pip"):
        assert construct not in source, construct


def test_the_evaluation_package_scan_is_non_vacuous():
    """A scan over zero modules passes every check and proves nothing."""
    modules = _evaluation_modules()
    assert len(modules) >= 15, [p.name for p in modules]
    assert "runner.py" in {p.name for p in modules}
    assert "transformers_peft.py" not in {p.name for p in modules}


def test_importing_the_whole_evaluation_package_loads_no_framework():
    import importlib
    before = set(sys.modules)
    for name in ("config", "plan", "references", "task_pack", "generation", "policy",
                 "backend", "runner", "scoring", "metrics", "statistics", "comparison",
                 "gates", "reports", "artifacts", "store", "human_review",
                 "registry_bridge"):
        importlib.import_module(f"training_gym.evaluation.{name}")
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []
    assert _ml_modules() == []
