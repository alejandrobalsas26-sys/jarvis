"""V69 M62 S3C — human review, and the proposal seam that writes no registry.

THE TWO PROPERTIES
------------------
A human decision binds to exactly one report and cannot overrule a deterministic gate.
And the registry bridge produces a document, not an effect: it never imports a mutator,
never constructs a promotion decision, and never writes the registry file.

The strongest thing anything in this file can produce is a JSON document saying what a
person could later choose to ask for.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from training_gym.evaluation import registry_bridge as RB
from training_gym.evaluation.backends.fake import FakeMode
from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.gates import evaluate_gates
from training_gym.evaluation.human_review import (
    RATIONALE_CATEGORIES,
    EvaluationHumanReview,
    EvaluationReviewError,
    ReviewDecision,
    check_review_admissible,
    verify_review_bindings,
)
from training_gym.evaluation.policy import EvaluationPolicySet
from training_gym.evaluation.reports import (
    CandidateEligibility,
    EmpiricalStatus,
    build_report,
)
from training_gym.schemas import SchemaError

from _m62_evaluation_fixtures import adapter_reference, baseline_reference, summarize
from test_training_gym_m62_evaluation_artifacts import _plan

_POLICIES = EvaluationPolicySet()
_SPLITS = ["hidden_evaluation", "security_regression"]
_NOW = "2026-08-03T00:00:00Z"


def _report_record(mode: FakeMode = FakeMode.IDENTICAL,
                   backend_ids=("fake_evaluation",)) -> dict:
    summary = summarize(mode)
    gates = evaluate_gates(summary, policies=_POLICIES, present_splits=_SPLITS)
    report = build_report(
        plan=_plan(), summary=summary, gates=gates, baseline=baseline_reference(),
        adapter=adapter_reference(), policies=_POLICIES, backend_ids=backend_ids,
        backend_version="m62.fake_evaluation.1",
        split_manifest_hashes={"hidden_evaluation": "7" * 64},
        run_state=EvaluationRunState.COMPLETED, created_at_utc=_NOW)
    return report.to_record()


def _review(record: dict, *, decision=ReviewDecision.APPROVE_CANDIDATE_PROPOSAL,
            **overrides) -> EvaluationHumanReview:
    kwargs = dict(
        reviewer_id="operator-1", evaluation_id=str(record["evaluation_id"]),
        evaluation_report_hash=str(record["report_hash"]),
        candidate_adapter_reference_hash=adapter_reference().reference_hash(),
        baseline_reference_hash=baseline_reference().reference_hash(),
        decision=decision, created_at_utc=_NOW,
        rationale_categories=("quality_improvement_observed",
                              "security_posture_unchanged"))
    kwargs.update(overrides)
    return EvaluationHumanReview(**kwargs)


# ══════════════════════════════════════════════════════════════════════════════
#  The review record
# ══════════════════════════════════════════════════════════════════════════════
def test_a_valid_review_parses_and_hashes_deterministically():
    record = _report_record()
    assert _review(record).review_hash() == _review(record).review_hash()
    assert EvaluationHumanReview.from_dict(_review(record).to_record()).review_hash() \
        == _review(record).review_hash()


def test_an_unknown_field_is_refused():
    payload = _review(_report_record()).to_record()
    payload["approved_by_default"] = True
    with pytest.raises(SchemaError, match="unknown field"):
        EvaluationHumanReview.from_dict(payload)


def test_a_review_may_not_claim_to_promote_or_override():
    """A review that could do this would make the deterministic gates advisory."""
    for field_name in ("promotes_model", "activates_model",
                       "overrides_deterministic_gates"):
        payload = _review(_report_record()).to_record()
        payload[field_name] = True
        with pytest.raises(EvaluationReviewError, match="advisory"):
            EvaluationHumanReview.from_dict(payload)


def test_a_decision_with_no_recorded_reason_is_refused():
    with pytest.raises(EvaluationReviewError, match="no recorded reason"):
        _review(_report_record(), rationale_categories=())


def test_a_rationale_outside_the_closed_vocabulary_is_refused():
    with pytest.raises(EvaluationReviewError, match="vocabulary is closed"):
        _review(_report_record(), rationale_categories=("looked_fine_to_me",))
    assert "quality_improvement_observed" in RATIONALE_CATEGORIES


def test_an_edited_review_is_detected_by_its_own_digest():
    payload = _review(_report_record()).to_record()
    payload["decision"] = ReviewDecision.REJECT.value
    with pytest.raises(EvaluationReviewError, match="edited since it was signed"):
        EvaluationHumanReview.from_dict(payload)


def test_the_reviewer_id_is_a_safe_label_and_never_a_host_account():
    with pytest.raises(SchemaError):
        _review(_report_record(), reviewer_id="DOMAIN\\alejandro")


# ══════════════════════════════════════════════════════════════════════════════
#  Binding and replay
# ══════════════════════════════════════════════════════════════════════════════
def test_a_review_binds_to_the_exact_report():
    record = _report_record()
    verify_review_bindings(_review(record), report_record=record,
                           adapter_reference_hash=adapter_reference().reference_hash(),
                           baseline_reference_hash=baseline_reference().reference_hash())


def test_a_review_written_about_another_report_is_refused():
    """A favourable review lifted off one evaluation and re-filed against another."""
    first, second = _report_record(), _report_record(FakeMode.CANDIDATE_REGRESSED)
    assert first["report_hash"] != second["report_hash"]
    with pytest.raises(SchemaError, match="does not match the expected"):
        verify_review_bindings(
            _review(first), report_record=second,
            adapter_reference_hash=adapter_reference().reference_hash(),
            baseline_reference_hash=baseline_reference().reference_hash())


def test_a_review_naming_a_different_adapter_is_refused():
    record = _report_record()
    with pytest.raises(SchemaError, match="does not match the expected"):
        verify_review_bindings(_review(record), report_record=record,
                               adapter_reference_hash="f" * 64,
                               baseline_reference_hash=baseline_reference()
                               .reference_hash())


def test_a_review_naming_a_different_baseline_is_refused():
    record = _report_record()
    with pytest.raises(SchemaError, match="does not match the expected"):
        verify_review_bindings(
            _review(record), report_record=record,
            adapter_reference_hash=adapter_reference().reference_hash(),
            baseline_reference_hash="f" * 64)


def test_a_review_for_another_evaluation_id_is_refused():
    record = _report_record()
    review = _review(record, evaluation_id="some-other-evaluation")
    with pytest.raises(EvaluationReviewError, match="but the report is"):
        verify_review_bindings(
            review, report_record=record,
            adapter_reference_hash=adapter_reference().reference_hash(),
            baseline_reference_hash=baseline_reference().reference_hash())


# ══════════════════════════════════════════════════════════════════════════════
#  What a human may and may not decide
# ══════════════════════════════════════════════════════════════════════════════
def test_approving_a_synthetic_report_is_inadmissible():
    """Approving it would record a human decision about a test double as a decision
    about an adapter."""
    record = _report_record()
    problems = check_review_admissible(_review(record), record)
    assert any("SYNTHETIC_ONLY" in p for p in problems)


def test_a_human_cannot_clear_a_deterministic_blocker():
    record = _report_record(FakeMode.CANDIDATE_SECURITY_REGRESSION)
    problems = check_review_admissible(_review(record), record)
    assert any("does not clear them" in p for p in problems)


def test_a_human_cannot_raise_the_eligibility_verdict():
    record = _report_record()
    problems = check_review_admissible(_review(record), record)
    assert any("would make the deterministic gates advisory" in p for p in problems)


def test_requesting_more_evidence_is_always_admissible():
    """Only an approval has to clear anything. Asking for more never does."""
    record = _report_record(FakeMode.CANDIDATE_SECURITY_REGRESSION)
    assert check_review_admissible(
        _review(record, decision=ReviewDecision.REQUEST_MORE_EVIDENCE,
                rationale_categories=("sample_size_insufficient",)), record) == ()


def test_quarantining_is_always_admissible():
    record = _report_record()
    assert check_review_admissible(
        _review(record, decision=ReviewDecision.QUARANTINE,
                rationale_categories=("artifact_integrity_concern",)), record) == ()


def test_rejecting_is_always_admissible():
    record = _report_record(FakeMode.CANDIDATE_IMPROVED)
    assert check_review_admissible(
        _review(record, decision=ReviewDecision.REJECT,
                rationale_categories=("operational_cost_unacceptable",)), record) == ()


# ══════════════════════════════════════════════════════════════════════════════
#  The registry bridge
# ══════════════════════════════════════════════════════════════════════════════
def test_a_synthetic_report_produces_a_not_eligible_proposal():
    record = _report_record(FakeMode.CANDIDATE_IMPROVED)
    proposal = RB.build_proposal(
        report_record=record, adapter_reference=adapter_reference(),
        baseline_reference=baseline_reference(), created_at_utc=_NOW,
        review=_review(record))
    assert proposal.proposal_status is RB.ProposalStatus.NOT_ELIGIBLE
    assert any("complete live measurement" in b for b in proposal.blockers)
    assert any("SYNTHETIC_ONLY" in limitation for limitation in proposal.limitations)


def test_a_proposal_without_a_human_review_is_not_eligible():
    """The handoff to a registry is a person's decision and this stage does not make
    it."""
    proposal = RB.build_proposal(
        report_record=_report_record(), adapter_reference=adapter_reference(),
        baseline_reference=baseline_reference(), created_at_utc=_NOW, review=None)
    assert proposal.proposal_status is RB.ProposalStatus.NOT_ELIGIBLE
    assert any("person's decision" in b for b in proposal.blockers)


def test_a_proposal_cannot_be_ready_while_synthetic():
    with pytest.raises(RB.ProposalError, match="not evidence about an adapter"):
        RB.ModelCandidateProposal(
            proposal_status=RB.ProposalStatus.READY_FOR_REGISTRY_REVIEW,
            evaluation_id="e1", generation=1, base_model_id="Qwen/Qwen3-0.6B",
            base_model_revision="a" * 40, base_model_identity_hash="1" * 64,
            tokenizer_identity_hash="2" * 64, adapter_run_id="run-0001",
            adapter_manifest_hash="3" * 64,
            candidate_adapter_reference_hash="4" * 64,
            baseline_reference_hash="5" * 64, evaluation_report_hash="6" * 64,
            evaluation_plan_hash="7" * 64, human_review_hash="8" * 64,
            empirical_status=EmpiricalStatus.SYNTHETIC_ONLY.value,
            eligibility=CandidateEligibility.ELIGIBLE_FOR_HUMAN_REVIEW.value,
            proposed_registry_status="evaluated", registry_schema={}, provenance={},
            limitations=(), blockers=(), created_at_utc=_NOW)


def test_a_tampered_report_invalidates_the_proposal():
    record = _report_record()
    record["overall_delta"] = 0.99
    with pytest.raises(Exception, match="edited since it was written"):
        RB.build_proposal(report_record=record, adapter_reference=adapter_reference(),
                          baseline_reference=baseline_reference(), created_at_utc=_NOW)


def test_a_review_for_another_report_invalidates_the_proposal():
    first, second = _report_record(), _report_record(FakeMode.CANDIDATE_REGRESSED)
    proposal = RB.build_proposal(
        report_record=second, adapter_reference=adapter_reference(),
        baseline_reference=baseline_reference(), created_at_utc=_NOW,
        review=_review(first))
    assert any("lifted onto another evaluation" in b for b in proposal.blockers)


def test_a_review_that_was_not_an_approval_invalidates_the_proposal():
    record = _report_record()
    proposal = RB.build_proposal(
        report_record=record, adapter_reference=adapter_reference(),
        baseline_reference=baseline_reference(), created_at_utc=_NOW,
        review=_review(record, decision=ReviewDecision.REJECT,
                       rationale_categories=("regression_observed",)))
    assert any("not an approval" in b for b in proposal.blockers)


def test_the_proposal_retains_full_training_and_evaluation_provenance():
    record = _report_record()
    proposal = RB.build_proposal(
        report_record=record, adapter_reference=adapter_reference(),
        baseline_reference=baseline_reference(), created_at_utc=_NOW)
    provenance = proposal.provenance
    assert provenance["training_run_id"] == "run-0001"
    assert provenance["training_method"] == "sft_lora"
    for key in ("training_plan_hash", "dataset_manifest_hash",
                "hidden_evaluation_hash", "security_regression_hash",
                "evaluation_task_pack_hash", "evaluation_backends"):
        assert provenance[key], key


def test_the_proposal_states_every_effect_it_did_not_have():
    record = _report_record()
    proposal = RB.build_proposal(
        report_record=record, adapter_reference=adapter_reference(),
        baseline_reference=baseline_reference(), created_at_utc=_NOW)
    effects = proposal.to_dict()["effects"]
    assert effects == {
        "registry_written": False, "model_promoted": False, "model_activated": False,
        "role_assigned": False, "adapter_merged": False, "format_converted": False,
        "ollama_contacted": False,
        "requested_transition": effects["requested_transition"]}
    assert "nothing here does" in effects["requested_transition"]


def test_the_proposal_is_deterministic():
    record = _report_record()
    kwargs = dict(report_record=record, adapter_reference=adapter_reference(),
                  baseline_reference=baseline_reference(), created_at_utc=_NOW)
    assert RB.build_proposal(**kwargs).proposal_hash() == \
        RB.build_proposal(**kwargs).proposal_hash()


def test_the_proposal_targets_the_weakest_registry_status():
    """"This has been evaluated" — not "this is a candidate" and certainly not
    "this is active"."""
    assert RB.PROPOSED_REGISTRY_STATUS == "evaluated"
    vocabulary = RB.registry_vocabulary()
    if vocabulary["available"]:
        assert vocabulary["proposed_status"] == "evaluated"
        assert "active" in vocabulary["statuses"]


# ══════════════════════════════════════════════════════════════════════════════
#  The bridge writes nothing
# ══════════════════════════════════════════════════════════════════════════════
def _bridge_code() -> str:
    tree = ast.parse(Path(RB.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.mark.parametrize("mutator", [
    ".register(", ".promote(", ".reject(", ".rollback(", ".attach_evaluation(",
    ".save(", "get_model_registry", "ModelRegistry", "PromotionDecision",
    "PromotionPolicy", "import ollama", "ollama.", "gguf", "merge_and_unload",
    "requests.", "urlopen", "subprocess", "shell=True", "open(",
])
def test_the_bridge_contains_no_registry_mutator_or_side_effect(mutator):
    assert mutator not in _bridge_code()


def test_the_bridge_imports_the_registry_lazily_and_reads_only_one_symbol():
    code = _bridge_code()
    assert "from core.model_registry import ModelStatus" in code
    assert "\nfrom core.model_registry" not in code, "the import must not be at module scope"


def test_building_a_proposal_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    record = _report_record()
    RB.build_proposal(report_record=record, adapter_reference=adapter_reference(),
                      baseline_reference=baseline_reference(), created_at_utc=_NOW,
                      review=_review(record))
    assert set(tmp_path.rglob("*")) == before


def test_building_a_proposal_opens_no_socket_and_spawns_no_process():
    import socket
    import subprocess

    def _refuse(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("the registry bridge must not do this")

    saved = (socket.socket, socket.create_connection, subprocess.Popen, subprocess.run)
    socket.socket, socket.create_connection = _refuse, _refuse
    subprocess.Popen, subprocess.run = _refuse, _refuse
    try:
        record = _report_record()
        proposal = RB.build_proposal(
            report_record=record, adapter_reference=adapter_reference(),
            baseline_reference=baseline_reference(), created_at_utc=_NOW)
    finally:
        (socket.socket, socket.create_connection, subprocess.Popen,
         subprocess.run) = saved
    assert proposal.proposal_status is RB.ProposalStatus.NOT_ELIGIBLE


def test_the_registry_is_unchanged_after_a_proposal():
    """Read the registry's own state before and after; nothing here touches it."""
    try:
        from core.model_registry import get_model_registry
    except Exception:
        pytest.skip("the model registry is not importable in this environment")
    registry = get_model_registry()
    before = sorted(registry.records) if hasattr(registry, "records") else None
    record = _report_record()
    RB.build_proposal(report_record=record, adapter_reference=adapter_reference(),
                      baseline_reference=baseline_reference(), created_at_utc=_NOW,
                      review=_review(record))
    after = sorted(registry.records) if hasattr(registry, "records") else None
    assert before == after


def test_the_bridge_imports_no_machine_learning_framework():
    banned = ("torch", "transformers", "peft", "trl", "datasets", "accelerate")
    before = set(sys.modules)
    record = _report_record()
    RB.build_proposal(report_record=record, adapter_reference=adapter_reference(),
                      baseline_reference=baseline_reference(), created_at_utc=_NOW)
    assert sorted(set(banned) & (set(sys.modules) - before)) == []
