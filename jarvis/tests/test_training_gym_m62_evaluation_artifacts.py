"""V69 M62 S3C — the report, the artefact tree, and proving neither has been edited.

THE PROPERTY UNDER TEST
-----------------------
Tamper detection that is re-derived rather than trusted. Every digest in a manifest is
recomputed from the bytes on disk, so editing a metric, a blocker or the eligibility
verdict is detected — and so is editing the digest to match, when the caller knows what
it should be.

Plus the rule that everything else rests on: a report produced by a test double can
never reach ELIGIBLE_FOR_HUMAN_REVIEW.
"""
from __future__ import annotations

import json
import sys

import pytest

from training_gym.evaluation import artifacts as A
from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.backends.fake import FakeMode
from training_gym.evaluation.gates import evaluate_gates
from training_gym.evaluation.plan import EvaluationPlan, plan_state_sequence
from training_gym.evaluation.policy import EvaluationPolicySet, ResourceCeilings
from training_gym.evaluation.score_evidence import (
    build_score_evidence,
    response_digests,
)
from training_gym.evaluation.reports import (
    CandidateEligibility,
    EmpiricalStatus,
    EvaluationReport,
    ReportError,
    build_report,
    classify_empirical_status,
    verify_report_payload,
)
from training_gym.evaluation.store import (
    EvaluationStoreError,
    PlanAlreadyConsumed,
    consume_plan,
    create_generation_directory,
    evaluation_ledger_path,
    is_plan_consumed,
    quarantine_generation,
    record_terminal,
)

from _m62_evaluation_fixtures import (
    adapter_reference,
    baseline_reference,
    make_pack,
    make_store,
    run_fake,
    summarize,
)

_POLICIES = EvaluationPolicySet()
_SPLITS = ["hidden_evaluation", "security_regression"]
_NOW = "2026-08-03T00:00:00Z"


def _plan(**overrides) -> EvaluationPlan:
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
        order_policy="balanced_by_task_hash_and_seed", order_assignment_hash="3" * 64,
        expected_output_root_id="4" * 64, expected_task_count=40,
        expected_baseline_generations=40, expected_candidate_generations=40,
        expected_grader_executions=240, expected_files=A.EXPECTED_EVALUATION_FILES,
        expected_state_transitions=plan_state_sequence(awaiting_confirmation=True),
        backend_id="fake_evaluation", created_at_utc=_NOW)
    kwargs.update(overrides)
    return EvaluationPlan(**kwargs)


def _report(mode: FakeMode = FakeMode.IDENTICAL, *, backend_ids=("fake_evaluation",),
            run_state=EvaluationRunState.COMPLETED, **kwargs) -> EvaluationReport:
    summary = summarize(mode)
    gates = evaluate_gates(summary, policies=_POLICIES, present_splits=_SPLITS)
    return build_report(
        plan=_plan(), summary=summary, gates=gates, baseline=baseline_reference(),
        adapter=adapter_reference(), policies=_POLICIES, backend_ids=backend_ids,
        backend_version="m62.fake_evaluation.1",
        split_manifest_hashes={"hidden_evaluation": "7" * 64,
                               "security_regression": "8" * 64},
        run_state=run_state, created_at_utc=_NOW, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════
#  Empirical status
# ══════════════════════════════════════════════════════════════════════════════
def test_a_fake_backed_run_is_synthetic_only():
    assert classify_empirical_status(backend_ids=["fake_evaluation"], task_count=10,
                                     measured_pairs=10, interrupted=False) \
        is EmpiricalStatus.SYNTHETIC_ONLY


def test_a_complete_real_run_is_live_measured():
    assert classify_empirical_status(backend_ids=["transformers_peft"], task_count=10,
                                     measured_pairs=10, interrupted=False) \
        is EmpiricalStatus.LIVE_MEASURED


def test_a_partial_real_run_is_partial_live_and_not_live_measured():
    assert classify_empirical_status(backend_ids=["transformers_peft"], task_count=10,
                                     measured_pairs=6, interrupted=False) \
        is EmpiricalStatus.PARTIAL_LIVE


def test_an_interrupted_real_run_is_never_live_measured():
    assert classify_empirical_status(backend_ids=["transformers_peft"], task_count=10,
                                     measured_pairs=10, interrupted=True) \
        is EmpiricalStatus.PARTIAL_LIVE


def test_only_a_complete_live_measurement_supports_eligibility():
    supporting = [s for s in EmpiricalStatus if s.supports_eligibility]
    assert supporting == [EmpiricalStatus.LIVE_MEASURED]


# ══════════════════════════════════════════════════════════════════════════════
#  Synthetic evidence can never make a candidate eligible
# ══════════════════════════════════════════════════════════════════════════════
def test_a_synthetic_report_yields_needs_more_evidence_however_good_the_numbers():
    """The candidate improves on every measurable dimension and passes every gate. It
    is still not eligible, because no model was measured."""
    report = _report(FakeMode.CANDIDATE_IMPROVED)
    assert report.gates.passed is True
    assert report.summary.overall_delta > 0
    assert report.empirical_status is EmpiricalStatus.SYNTHETIC_ONLY
    assert report.eligibility is CandidateEligibility.NEEDS_MORE_EVIDENCE
    assert "test double" in report.decision.rationale


def test_a_report_cannot_claim_a_live_status_while_naming_a_test_double():
    """The check lives in the report rather than the CLI, because a check in a
    command-line front end is a check a library caller skips."""
    summary = summarize()
    gates = evaluate_gates(summary, policies=_POLICIES, present_splits=_SPLITS)
    with pytest.raises(ReportError, match="test doubles"):
        EvaluationReport(
            evaluation_id="e", generation=1, plan_hash="1" * 64,
            baseline_reference_hash="2" * 64,
            candidate_adapter_reference_hash="3" * 64, tokenizer_identity_hash="4" * 64,
            task_pack_hash="5" * 64, hidden_target_store_hash="6" * 64,
            dataset_manifest_hash="7" * 64, split_manifest_hashes={},
            generation_policy_hash="8" * 64, grader_policy_hash="9" * 64,
            metric_policy_hash="a" * 64, statistical_policy_hash="b" * 64,
            gate_policy_hash="c" * 64, family_policy_hash="d" * 64,
            backend_ids=("fake_evaluation",), backend_version="1",
            dependency_report_hash="e" * 64, hardware_report_hash="f" * 64,
            task_count=40, measured_pairs=40,
            comparison_manifest_hash=summary.comparison_manifest_hash(),
            summary=summary, gates=gates,
            decision=__import__(
                "training_gym.evaluation.reports", fromlist=["EligibilityDecision"]
            ).EligibilityDecision(
                eligibility=CandidateEligibility.NEEDS_MORE_EVIDENCE,
                empirical_status=EmpiricalStatus.LIVE_MEASURED,
                human_review_required=False),
            run_state=EvaluationRunState.COMPLETED, created_at_utc=_NOW)


def test_the_synthetic_limitation_is_recorded_in_the_report():
    report = _report()
    assert any("SYNTHETIC_ONLY" in limitation for limitation in report.limitations)
    assert any("no model was loaded" in limitation for limitation in report.limitations)


def test_a_quarantined_run_is_never_eligible():
    report = _report(FakeMode.CANDIDATE_IMPROVED,
                     run_state=EvaluationRunState.QUARANTINED)
    assert report.eligibility is CandidateEligibility.QUARANTINED
    assert "never eligible" in report.decision.rationale


def test_a_security_blocker_yields_not_eligible_rather_than_needs_more_evidence():
    """The order matters: a security failure is a refusal, not a request for more
    data."""
    report = _report(FakeMode.CANDIDATE_SECURITY_REGRESSION)
    assert report.eligibility is CandidateEligibility.NOT_ELIGIBLE
    assert "not in the same units" in report.decision.rationale


def test_no_report_ever_claims_to_promote_or_activate_anything():
    payload = _report().to_dict()
    assert payload["promotes_model"] is False
    assert payload["activates_model"] is False
    assert payload["mutates_model_registry"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  Report integrity
# ══════════════════════════════════════════════════════════════════════════════
def test_the_report_hash_is_deterministic():
    assert _report().report_hash() == _report().report_hash()


def test_a_valid_report_verifies():
    record = _report().to_record()
    assert verify_report_payload(record)["report_hash"] == record["report_hash"]


@pytest.mark.parametrize("field_name,value", [
    ("overall_delta", 0.99), ("measured_pairs", 999), ("empirical_status",
                                                        "live_measured"),
    ("human_review_required", True), ("wins", 40), ("task_count", 1),
])
def test_editing_any_field_of_a_report_is_detected(field_name, value):
    record = _report().to_record()
    record[field_name] = value
    with pytest.raises(ReportError, match="edited since it was written"):
        verify_report_payload(record)


def test_editing_the_blocker_list_of_a_report_is_detected():
    record = _report(FakeMode.CANDIDATE_SECURITY_REGRESSION).to_record()
    record["blockers"] = []
    with pytest.raises(ReportError, match="edited since it was written"):
        verify_report_payload(record)


def test_a_report_with_no_digest_is_refused():
    record = _report().to_record()
    record.pop("report_hash")
    with pytest.raises(ReportError, match="cannot be verified"):
        verify_report_payload(record)


def test_a_report_whose_digest_is_not_the_expected_one_is_refused():
    record = _report().to_record()
    with pytest.raises(ReportError, match="not the expected"):
        verify_report_payload(record, expected_hash="f" * 64)


def test_a_report_record_carries_no_private_content():
    record = json.dumps(_report().to_record())
    assert "C:/Users" not in record and "/home/" not in record


# ══════════════════════════════════════════════════════════════════════════════
#  Artefacts
# ══════════════════════════════════════════════════════════════════════════════
def _write(tmp_path, mode: FakeMode = FakeMode.IDENTICAL, **overrides):
    pack = make_pack()
    run = run_fake(mode, pack=pack)
    from training_gym.evaluation.comparison import build_comparison
    from training_gym.evaluation.runner import result_records, results_by_role
    from training_gym.evaluation.references import EvaluationRole
    summary = build_comparison(run, pack=pack, targets=make_store(pack),
                               policies=_POLICIES)
    gates = evaluate_gates(summary, policies=_POLICIES, present_splits=_SPLITS)
    report = build_report(
        plan=_plan(), summary=summary, gates=gates, baseline=baseline_reference(),
        adapter=adapter_reference(), policies=_POLICIES,
        backend_ids=("fake_evaluation",), backend_version="m62.fake_evaluation.1",
        split_manifest_hashes={"hidden_evaluation": "7" * 64},
        run_state=EvaluationRunState.COMPLETED, created_at_utc=_NOW)
    report_record = report.to_record()
    manifest = A.EvaluationManifest(
        evaluation_id=report.evaluation_id, generation=report.generation,
        plan_hash=report.plan_hash, report_hash=report_record["report_hash"],
        task_pack_hash=pack.pack_hash(),
        hidden_target_store_hash=make_store(pack).store_hash(),
        baseline_reference_hash=report.baseline_reference_hash,
        candidate_adapter_reference_hash=report.candidate_adapter_reference_hash,
        comparison_manifest_hash=summary.comparison_manifest_hash(),
        backend_ids=("fake_evaluation",),
        empirical_status=report.empirical_status.value,
        eligibility=report.eligibility.value, task_count=report.task_count,
        measured_pairs=report.measured_pairs, files=(), total_bytes=0, tree_hash="",
        created_at_utc=_NOW)
    directory = tmp_path / "gen-1"
    baseline_records = result_records(results_by_role(run, EvaluationRole.BASELINE))
    candidate_records = result_records(results_by_role(run, EvaluationRole.CANDIDATE))
    payload = dict(
        plan_record=_plan().to_record(), task_pack_records=pack.task_records(),
        task_pack_manifest=pack.to_record(),
        baseline_records=baseline_records,
        candidate_records=candidate_records,
        comparison_records=summary.comparison_records(),
        baseline_score_records=build_score_evidence(
            summary.comparisons, role="baseline", evaluation_id=manifest.evaluation_id,
            generation=manifest.generation,
            response_digests=response_digests(baseline_records)),
        candidate_score_records=build_score_evidence(
            summary.comparisons, role="candidate", evaluation_id=manifest.evaluation_id,
            generation=manifest.generation,
            response_digests=response_digests(candidate_records)),
        metrics_record={"baseline": summary.baseline_metrics.to_dict(),
                        "candidate": summary.candidate_metrics.to_dict()},
        report_record=report_record, manifest=manifest)
    payload.update(overrides)
    validation = A.write_evaluation_artifacts(directory, **payload)
    return directory, validation, report_record


def test_a_written_generation_verifies(tmp_path):
    directory, validation, _ = _write(tmp_path)
    assert validation.ok
    assert A.verify_evaluation_generation(directory) == ()
    assert set(p.name for p in directory.iterdir()) == set(
        A.required_evaluation_files(A.EVALUATION_MANIFEST_VERSION))


def test_the_manifest_is_written_last(tmp_path):
    """A crash halfway leaves a directory with no manifest, which is unambiguously
    incomplete rather than plausibly finished."""
    directory, _, _ = _write(tmp_path)
    manifest = directory / A.EVALUATION_MANIFEST_FILE
    others = [p for p in directory.iterdir() if p.name != A.EVALUATION_MANIFEST_FILE]
    assert manifest.stat().st_mtime_ns >= max(p.stat().st_mtime_ns for p in others)


@pytest.mark.parametrize("filename", sorted(
    A.REQUIRED_EVALUATION_FILES - {A.EVALUATION_MANIFEST_FILE}))
def test_editing_any_artifact_is_detected(tmp_path, filename):
    directory, _, _ = _write(tmp_path)
    target = directory / filename
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    problems = A.verify_evaluation_generation(directory)
    assert problems
    assert any(filename in p for p in problems)


def test_a_missing_artifact_is_detected(tmp_path):
    directory, _, _ = _write(tmp_path)
    (directory / "metrics.json").unlink()
    assert any("metrics.json" in p for p in A.verify_evaluation_generation(directory))


def test_an_unexpected_file_is_detected(tmp_path):
    """An unexpected file in a reviewed output tree is a refusal, not a curiosity."""
    directory, _, _ = _write(tmp_path)
    (directory / "notes.txt").write_text("scratch", encoding="utf-8")
    problems = A.verify_evaluation_generation(directory)
    assert any("not an artefact this subsystem writes" in p for p in problems)


def test_an_empty_artifact_is_rejected(tmp_path):
    directory, _, _ = _write(tmp_path)
    (directory / "metrics.json").write_text("", encoding="utf-8")
    assert any("is empty" in p for p in A.verify_evaluation_generation(directory))


def test_a_symlinked_artifact_is_rejected(tmp_path):
    directory, _, _ = _write(tmp_path)
    target = directory / "metrics.json"
    real = tmp_path / "elsewhere.json"
    real.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    assert any("symlink" in p for p in A.verify_evaluation_generation(directory))


def test_a_hard_linked_artifact_is_rejected_where_the_host_supports_it(tmp_path):
    directory, _, _ = _write(tmp_path)
    target = directory / "metrics.json"
    second = tmp_path / "second-name.json"
    try:
        import os
        os.link(target, second)
    except (OSError, AttributeError, NotImplementedError):
        pytest.skip("this host does not report hard links")
    if target.stat().st_nlink <= 1:
        pytest.skip("this filesystem does not track link counts")
    assert any("hard link" in p for p in A.verify_evaluation_generation(directory))


def test_an_oversized_artifact_is_rejected(tmp_path):
    directory, _, _ = _write(tmp_path)
    tiny = ResourceCeilings(max_artifact_bytes=1024)
    problems = A.verify_evaluation_generation(directory, ceilings=tiny)
    assert any("exceeds the" in p for p in problems)


def test_a_line_count_mismatch_is_detected(tmp_path):
    directory, _, _ = _write(tmp_path)
    target = directory / "paired-comparisons.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    target.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    problems = A.verify_evaluation_generation(directory)
    assert any("lines against the manifest" in p or "digest" in p for p in problems)


def test_a_manifest_naming_a_file_that_is_not_there_is_detected(tmp_path):
    directory, _, _ = _write(tmp_path)
    manifest_path = directory / A.EVALUATION_MANIFEST_FILE
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append({"name": "task-pack.jsonl", "sha256": "f" * 64,
                             "size_bytes": 1, "line_count": 1})
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert A.verify_evaluation_generation(directory)


def test_a_tampered_manifest_digest_is_detected(tmp_path):
    directory, _, _ = _write(tmp_path)
    manifest_path = directory / A.EVALUATION_MANIFEST_FILE
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["measured_pairs"] = 999
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert any("does not match the document" in p
               for p in A.verify_evaluation_generation(directory))


def test_a_completed_generation_is_never_overwritten(tmp_path):
    _write(tmp_path)
    with pytest.raises(A.EvaluationArtifactError, match="never overwritten"):
        _write(tmp_path)


def test_the_hidden_targets_never_reach_a_generation_artifact(tmp_path):
    """The store exports digests, so even a caller who serialised it into the tree
    would publish hashes rather than answers."""
    directory, _, _ = _write(tmp_path)
    from _m62_evaluation_fixtures import TARGET_TEXT
    for path in directory.iterdir():
        assert TARGET_TEXT not in path.read_text(encoding="utf-8"), path.name


def test_no_generation_artifact_carries_a_private_path(tmp_path):
    directory, _, _ = _write(tmp_path)
    for path in directory.iterdir():
        body = path.read_text(encoding="utf-8")
        assert "C:/Users/" not in body and "/home/" not in body, path.name


def test_the_report_digest_the_manifest_sealed_is_checked(tmp_path):
    directory, _, _ = _write(tmp_path)
    report_path = directory / "evaluation-report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["overall_delta"] = 0.99
    report_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    assert A.verify_evaluation_generation(directory)


# ══════════════════════════════════════════════════════════════════════════════
#  The ledger
# ══════════════════════════════════════════════════════════════════════════════
def test_a_plan_can_be_consumed_once(tmp_path):
    plan_hash = "a" * 64
    assert is_plan_consumed(tmp_path, plan_hash) is False
    consume_plan(tmp_path, plan_hash=plan_hash, evaluation_id="e1", generation=1,
                 actor="operator", at=_NOW)
    assert is_plan_consumed(tmp_path, plan_hash) is True


def test_a_consumed_plan_cannot_be_replayed(tmp_path):
    plan_hash = "a" * 64
    consume_plan(tmp_path, plan_hash=plan_hash, evaluation_id="e1", generation=1,
                 actor="operator", at=_NOW)
    with pytest.raises(PlanAlreadyConsumed, match="nobody gave twice"):
        consume_plan(tmp_path, plan_hash=plan_hash, evaluation_id="e1", generation=1,
                     actor="operator", at=_NOW)


def test_a_failed_run_leaves_the_plan_consumed(tmp_path):
    """A failed evaluation read the held-out data and may have written bytes. Neither
    is undone by the failure."""
    plan_hash = "a" * 64
    consume_plan(tmp_path, plan_hash=plan_hash, evaluation_id="e1", generation=1,
                 actor="operator", at=_NOW)
    record_terminal(tmp_path, plan_hash=plan_hash, evaluation_id="e1", generation=1,
                    actor="operator", at=_NOW, state=EvaluationRunState.FAILED)
    assert is_plan_consumed(tmp_path, plan_hash) is True


def test_deleting_the_output_directory_does_not_unconsume_a_plan(tmp_path):
    plan_hash = "a" * 64
    directory = create_generation_directory(tmp_path, "e1", 1)
    consume_plan(tmp_path, plan_hash=plan_hash, evaluation_id="e1", generation=1,
                 actor="operator", at=_NOW)
    directory.rmdir()
    assert is_plan_consumed(tmp_path, plan_hash) is True


def test_a_non_terminal_state_is_not_a_terminal_outcome(tmp_path):
    with pytest.raises(EvaluationStoreError, match="not a terminal outcome"):
        record_terminal(tmp_path, plan_hash="a" * 64, evaluation_id="e1", generation=1,
                        actor="operator", at=_NOW,
                        state=EvaluationRunState.RUNNING_BASELINE)


def test_a_symlinked_ledger_is_refused(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    link = evaluation_ledger_path(tmp_path)
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    with pytest.raises(EvaluationStoreError, match="un-consumes every plan"):
        is_plan_consumed(tmp_path, "a" * 64)


def test_a_generation_directory_cannot_be_created_twice(tmp_path):
    create_generation_directory(tmp_path, "e1", 1)
    with pytest.raises(EvaluationStoreError, match="raise the generation"):
        create_generation_directory(tmp_path, "e1", 1)
    assert create_generation_directory(tmp_path, "e1", 2).is_dir()


def test_a_failed_generation_can_be_quarantined(tmp_path):
    directory = create_generation_directory(tmp_path, "e1", 1)
    moved = quarantine_generation(tmp_path, directory, evaluation_id="e1", generation=1,
                                  nonce="abc123")
    assert moved.is_dir() and not directory.exists()
    assert "evaluation_quarantine" in moved.as_posix()


def test_the_ledger_carries_no_username_or_host_path(tmp_path):
    consume_plan(tmp_path, plan_hash="a" * 64, evaluation_id="e1", generation=1,
                 actor="operator", at=_NOW)
    body = evaluation_ledger_path(tmp_path).read_text(encoding="utf-8")
    assert "C:/Users" not in body and "/home/" not in body


# ══════════════════════════════════════════════════════════════════════════════
#  No effects
# ══════════════════════════════════════════════════════════════════════════════
def test_writing_and_verifying_artifacts_imports_no_framework(tmp_path):
    banned = ("torch", "transformers", "peft", "trl", "datasets", "accelerate")
    before = set(sys.modules)
    directory, _, _ = _write(tmp_path)
    A.verify_evaluation_generation(directory)
    assert sorted(set(banned) & (set(sys.modules) - before)) == []
