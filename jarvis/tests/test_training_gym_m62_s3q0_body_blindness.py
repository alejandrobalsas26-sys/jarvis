"""V69 M62 S3Q.0 — three different read boundaries, kept apart and tested apart.

THE DISTINCTION THAT "BODY READ" HIDES
--------------------------------------
A. ORCHESTRATOR_SEMANTIC_ACCESS
   A human or an AI orchestrator sees prompt, target or response text. FORBIDDEN for a
   held-out corpus, and the thing every firewall in this repository exists to prevent.

B. BODY_OPAQUE_PROGRAMMATIC_ACCESS
   Reviewed code opens body bytes to validate, hash or canonicalise them, and returns
   digests and counts. PERMITTED, because a pack cannot be identified without being
   built. The trust boundary is the return type, not the caller's good intentions.

C. MODEL_FACING_ACCESS
   A held-out task crosses the backend boundary. This is the scientific spend, and it is
   what the durable commit event records.

They are not synonyms, and conflating B with A is how a firewall gets argued away.

HOW THIS IS MADE NON-VACUOUS
----------------------------
Every synthetic prompt, target and response carries a canary. A test that asserted "no
body appears" would pass against an implementation that emitted nothing at all; a test
that asserts a canary KNOWN to be in the material is absent from a given surface fails the
moment that surface starts carrying material.

``task-pack.jsonl`` is expected to contain the prompt canary. It is what the model is
handed, so it is BODY_BEARING by design and is classified as such rather than excused.
"""
from __future__ import annotations

import json

import pytest

from training_gym.evaluation.preflight import (
    PreflightError,
    assert_body_free,
    body_free_problems,
    preflight_report,
)

import _s3q0_synthetic as S


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0body")
    S.build(root)
    return root


@pytest.fixture(scope="module")
def completed(dataset_root, tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0bodyrun")
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.ok, outcome.problems
    return outcome, root


def _text(path):
    return path.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  The canaries are really in the material — or nothing below means anything
# ══════════════════════════════════════════════════════════════════════════════
def test_the_prompt_and_target_canaries_are_present_in_the_source_material(
        dataset_root):
    """The instrument check. Without it every firewall test is vacuous."""
    found = set()
    for path in dataset_root.rglob("*.jsonl"):
        body = _text(path)
        found.update(S.leaked_canaries(body))
    assert S.PROMPT_CANARY in found
    assert S.TARGET_CANARY in found


def test_the_response_canary_is_really_produced_by_the_double(dataset_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.backend import EvaluationRequest
    from training_gym.evaluation.references import EvaluationRole

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    baseline = S.make_baseline()
    request = EvaluationRequest(
        role=EvaluationRole.BASELINE, task=built.pack.tasks[0],
        generation=config.generation, baseline=baseline)
    result = S.CanaryBackend().generate(request)
    assert S.RESPONSE_CANARY in result.response_text


# ══════════════════════════════════════════════════════════════════════════════
#  A — the orchestrator surfaces carry nothing
# ══════════════════════════════════════════════════════════════════════════════
def test_the_live_preflight_carries_no_canary(dataset_root):
    config = S.make_config()
    baseline = S.make_baseline()
    adapter = S.make_adapter(baseline)
    identity = S.pack_identity(dataset_root, config)
    plan = S.make_plan(config, baseline, adapter, identity)
    payload = preflight_report(plan=plan, identity=identity, dependency_ready=True,
                               generation_policy=config.generation)
    assert S.leaked_canaries(json.dumps(payload, sort_keys=True)) == []


def test_the_pack_identity_carries_no_canary(dataset_root):
    identity = S.pack_identity(dataset_root, S.make_config())
    assert S.leaked_canaries(json.dumps(identity.to_dict(), sort_keys=True)) == []


def test_the_ledger_carries_no_canary(completed):
    _outcome, root = completed
    body = _text(root / "evaluation_runs.jsonl")
    assert S.leaked_canaries(body) == []


def test_the_execution_outcome_carries_no_canary(completed):
    outcome, _root = completed
    assert S.leaked_canaries(json.dumps(outcome.to_dict(), sort_keys=True)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  The artefact classification, measured rather than declared
# ══════════════════════════════════════════════════════════════════════════════
#: What each normal evaluation artefact is, checked against the canaries below.
EXPECTED_CLASSIFICATION = {
    "evaluation-plan.json": "BODY_FREE",
    "task-pack.jsonl": "BODY_BEARING",
    "task-pack-manifest.json": "BODY_FREE",
    "baseline-results.jsonl": "BODY_FREE",
    "candidate-results.jsonl": "BODY_FREE",
    "paired-comparisons.jsonl": "BODY_FREE",
    "baseline-scores.jsonl": "BODY_FREE",
    "candidate-scores.jsonl": "BODY_FREE",
    "metrics.json": "BODY_FREE",
    "evaluation-report.json": "BODY_FREE",
    "evaluation-manifest.json": "BODY_FREE",
}


def test_every_written_artifact_has_a_declared_classification(completed):
    outcome, _root = completed
    written = {p.name for p in outcome.directory.iterdir()}
    assert written == set(EXPECTED_CLASSIFICATION), written


@pytest.mark.parametrize("name", sorted(
    n for n, kind in EXPECTED_CLASSIFICATION.items() if kind == "BODY_FREE"))
def test_the_body_free_artifacts_carry_no_canary(completed, name):
    outcome, _root = completed
    assert S.leaked_canaries(_text(outcome.directory / name)) == [], name


def test_the_task_pack_is_body_bearing_and_is_classified_as_such(completed):
    """Expected, documented, and never read for meaning by an orchestrator."""
    outcome, _root = completed
    body = _text(outcome.directory / "task-pack.jsonl")
    assert S.PROMPT_CANARY in body
    assert EXPECTED_CLASSIFICATION["task-pack.jsonl"] == "BODY_BEARING"


def test_the_task_pack_still_carries_no_target_or_response(completed):
    """Model-facing does not mean unfiltered: the answer key is not in the pack."""
    outcome, _root = completed
    body = _text(outcome.directory / "task-pack.jsonl")
    assert S.TARGET_CANARY not in body
    assert S.RESPONSE_CANARY not in body


def test_no_result_artifact_persists_a_model_response(completed):
    """``response_sha256`` and a character count, never ``response_text``."""
    outcome, _root = completed
    for name in ("baseline-results.jsonl", "candidate-results.jsonl"):
        for line in _text(outcome.directory / name).splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            assert "response_text" not in record, name
            assert len(record["response_sha256"]) == 64
            assert record["response_chars"] > 0


def test_the_hidden_target_store_is_never_serialised_into_a_generation(completed):
    outcome, _root = completed
    for path in outcome.directory.iterdir():
        assert S.TARGET_CANARY not in _text(path), path.name


# ══════════════════════════════════════════════════════════════════════════════
#  B — the body-opaque boundary returns digests and nothing else
# ══════════════════════════════════════════════════════════════════════════════
def test_the_file_evidence_helper_returns_only_numbers(completed):
    from scripts.build_m62_eval_receipt import file_evidence

    outcome, _root = completed
    evidence = file_evidence(outcome.directory / "task-pack.jsonl")
    assert set(evidence) == {"sha256", "bytes", "record_count"}
    assert S.leaked_canaries(json.dumps(evidence, sort_keys=True)) == []
    assert evidence["record_count"] == outcome.task_count


def test_a_body_free_payload_is_refused_by_field_name_not_only_by_content():
    """A field called ``user_prompt`` holding "" today holds a prompt tomorrow."""
    assert body_free_problems({"task_pack_hash": "a" * 64}) == ()
    assert body_free_problems({"user_prompt": ""})
    assert body_free_problems({"nested": [{"target_text": "anything"}]})
    with pytest.raises(PreflightError, match="body-bearing field name"):
        assert_body_free({"response_text": "x"})


# ══════════════════════════════════════════════════════════════════════════════
#  Exception safety — a refusal must not quote the material it refused
# ══════════════════════════════════════════════════════════════════════════════
def test_a_pack_build_failure_does_not_echo_the_prompt(dataset_root, tmp_path):
    """A traceback is a body-free surface too, and it is the easiest one to forget."""
    from training_gym.evaluation.pack_builder import (
        PackBuilderError,
        build_task_pack_from_dataset,
    )

    config = S.make_config()
    with pytest.raises(PackBuilderError) as caught:
        build_task_pack_from_dataset(
            root=dataset_root, dataset_id=config.dataset.dataset_id,
            dataset_version=config.dataset.dataset_version,
            splits=["train"], generation=1)
    assert S.leaked_canaries(str(caught.value)) == []


def test_a_binding_mismatch_refusal_does_not_echo_the_material(dataset_root, tmp_path):
    config = S.make_config()
    baseline = S.make_baseline()
    adapter = S.make_adapter(baseline)
    identity = S.pack_identity(dataset_root, config)
    plan = S.make_plan(config, baseline, adapter, identity, task_pack_hash="9" * 64)
    outcome = S.run_synthetic(dataset_root, tmp_path / "mismatch", config=config,
                              plan=plan, baseline=baseline, adapter=adapter)
    assert S.leaked_canaries(" ".join(outcome.problems)) == []


def test_an_artifact_verification_failure_does_not_echo_the_material(dataset_root,
                                                                     tmp_path):
    from training_gym.evaluation.artifacts import verify_evaluation_generation

    outcome = S.run_synthetic(dataset_root, tmp_path / "corrupt")
    assert outcome.ok
    pack = outcome.directory / "task-pack.jsonl"
    pack.write_text(_text(pack) + '{"broken": true}\n', encoding="utf-8")
    problems = verify_evaluation_generation(outcome.directory)
    assert problems, "the tampered generation must not verify"
    assert S.leaked_canaries(" ".join(problems)) == []


def test_a_hidden_target_lookup_failure_does_not_echo_the_answer(dataset_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.task_pack import HiddenTargetError

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    task = built.pack.tasks[0]
    with pytest.raises(HiddenTargetError) as caught:
        built.targets.lookup(task.task_id, task_hash="9" * 64)
    assert S.leaked_canaries(str(caught.value)) == []


def test_the_hidden_target_store_exports_digests_and_never_text(dataset_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    assert S.leaked_canaries(json.dumps(built.targets.to_dict(), sort_keys=True)) == []


def test_the_pack_manifest_exports_digests_and_never_text(dataset_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    assert S.leaked_canaries(json.dumps(built.manifest(), sort_keys=True)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  C — the model-facing boundary is the only place a body legitimately travels
# ══════════════════════════════════════════════════════════════════════════════
def test_the_request_a_backend_receives_has_no_field_for_an_answer():
    from training_gym.evaluation.backend import EvaluationRequest

    fields = set(EvaluationRequest.__dataclass_fields__)
    for forbidden in ("hidden_target", "expected", "rubric", "target",
                      "counterpart_response", "teacher_answer"):
        assert forbidden not in fields, forbidden


def test_the_public_request_description_carries_no_body(dataset_root):
    from training_gym.evaluation.backend import EvaluationRequest
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.references import EvaluationRole

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    request = EvaluationRequest(
        role=EvaluationRole.BASELINE, task=built.pack.tasks[0],
        generation=config.generation, baseline=S.make_baseline())
    assert S.leaked_canaries(
        json.dumps(request.to_public_dict(), sort_keys=True)) == []
