"""V69 M62 S3Q.0.1 — portable evaluation evidence, closed before the irreversible act.

THE QUESTION THIS MILESTONE HAD TO ANSWER
-----------------------------------------
If ``eval-v4`` is spent exactly once, can the repository later prove, from tracked
BODY-FREE evidence alone — no gitignored runtime tree, no adapter bytes, no holdout —
which candidate was measured, which weights it used, which training run produced them,
what the baseline was, what was approved, what durably happened, how much completed, and
WHY the final ``EVALUATED_*`` state follows?

S3Q.0's ``m62.eval_receipt.1`` was audited against that question and the answer was no,
in nine separate ways. Every one is reproduced here against the ``.1`` code as it stands,
and then closed by ``.2``. A test that only asserted the fix would pass on a repository
where the defect never existed; asserting the defect first is what makes the fix mean
something.

WHAT IS *NOT* HERE
------------------
No eval-v4. No model. No generation. No plan consumption. Every fixture is synthetic, the
adapter is four tiny LoRA tensors nothing deserialises, and the only evaluation that runs
is backed by a test double whose ``SYNTHETIC_ONLY`` status makes eligibility structurally
unreachable.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.build_m62_eval_receipt import (
    DECISION_REDERIVER,
    RECEIPT_SCHEMA_VERSION,
    RECEIPT_V2_SCHEMA_VERSION,
    SUCCESSFUL_TERMINAL_EVENT,
    TERMINAL_EVALUATION_EVENTS,
    ReceiptError,
    build_receipt_v2,
    emit_receipt,
    ledger_evidence,
    main,
    read_receipt_file,
    seal,
    source_identity,
    verify_receipt_payload,
)
from scripts.verify_m62_control_plane import (
    EVAL_RECEIPT_V2_SCHEMA_PATH,
    EVAL_RECEIPT_V2_SCHEMA_VERSION,
    MODERN_EVAL_RECEIPT_VERSIONS,
    REPO_ROOT,
    canonical_bytes,
    canonical_json,
    eval_receipt_v2_schema,
    validate_against_schema,
)

import _s3q0_synthetic as S
import _s3q01_synthetic as W


# ══════════════════════════════════════════════════════════════════════════════
#  The world: one real synthetic adapter, evaluated once
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return W.evaluated_world(tmp_path_factory)


def _build(world, **overrides):
    kwargs = {
        "training_receipt": world["training_receipt"],
        "adapter_run_directory": world["adapter"]["directory"],
        "evaluation_config": world["config_path"],
        "ledger": world["ledger"],
        "repo_root": world["repo_root"],
    }
    kwargs.update(overrides)
    directory = kwargs.pop("generation_directory", world["directory"])
    return seal(build_receipt_v2(directory, **kwargs))


@pytest.fixture(scope="module")
def receipt(world):
    return _build(world)


def _rehashed(receipt: dict, *path, value) -> dict:
    """Mutate one field and RECOMPUTE the digest.

    Every mutation test below does this. A tampered receipt whose ``receipt_hash`` no
    longer matches is caught by one line and proves nothing about the check under test;
    the interesting question is always whether a receipt that is internally consistent
    and still wrong is refused.
    """
    tampered = copy.deepcopy(receipt)
    target = tampered
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    tampered.pop("receipt_hash", None)
    return seal(tampered)


def _problems(receipt: dict) -> list[str]:
    return list(verify_receipt_payload(receipt))


# ══════════════════════════════════════════════════════════════════════════════
#  Section 7 — the nine findings, reproduced against `.1` as it stands
# ══════════════════════════════════════════════════════════════════════════════
def test_finding_a_v1_emitted_empty_adapter_identities(world):
    """`.1` recorded a BLANK where 'which weights were measured' belongs."""
    from scripts.build_m62_eval_receipt import build_receipt, verify_receipt

    legacy = seal(build_receipt(
        world["directory"], candidate="anything-at-all",
        evaluation_source_commit="a" * 40, ledger=world["ledger"]))
    assert legacy["candidate"]["adapter_sha256"] == ""
    assert legacy["candidate"]["adapter_manifest_hash"] == ""
    assert verify_receipt(legacy) == ()          # and `.1` was content with that


def test_finding_a_v2_requires_all_four_adapter_identities(receipt):
    for field in ("adapter_sha256", "adapter_manifest_hash",
                  "adapter_artifact_set_hash", "adapter_reference_hash"):
        assert len(receipt["candidate"][field]) == 64, field


def test_finding_b_v1_counted_an_authority_creation_event_that_does_not_exist(world):
    """The ledger owns plan start and consumption. Nothing durable records a creation."""
    from scripts.build_m62_eval_receipt import build_receipt

    legacy = build_receipt(world["directory"], candidate="x",
                           evaluation_source_commit="a" * 40, ledger=world["ledger"])
    assert "creations" in legacy["authority"]
    events = {e["event"] for e in S.ledger_lines(world["output_root"])}
    assert not any("creat" in event for event in events)


def test_finding_b_v2_removes_the_fiction_without_inventing_an_event(receipt):
    assert "creations" not in receipt["authority"]
    assert receipt["authority"]["plan_consumption_count"] == 1
    assert receipt["authority"]["holdout_commit_count"] == 1
    assert receipt["authority"]["form"] == "EVAL:<plan-hash>"


def test_finding_c_v1_standalone_verify_skipped_schema_validation(tmp_path):
    """A document that was not a receipt at all printed 'verified'."""
    from scripts.build_m62_eval_receipt import seal as v1_seal
    from scripts.build_m62_eval_receipt import verify_receipt

    junk = v1_seal({
        "schema_version": "m62.eval_receipt.99", "totally": "made up",
        "ledger": {"plan_started_count": 1, "holdout_commit_count": 1,
                   "terminal_event": "x"},
        "plan": {"binds_exact_pack_identity": True},
        "execution": {"artifact_verification": "PASS"}, "authority": {}})
    assert verify_receipt(junk) == ()            # `.1`'s semantic check alone
    assert verify_receipt_payload(junk)          # `.2`'s strict entry point refuses


def test_finding_d_v2_verify_mode_needs_no_build_arguments(receipt, tmp_path, capsys):
    destination = tmp_path / "receipt.json"
    destination.write_text(canonical_json(receipt), encoding="utf-8")
    assert main(["verify", str(destination)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"


def test_finding_e_a_caller_may_not_rename_evidence(world):
    """Section 50. `.1` accepted `--candidate anything` and verified."""
    with pytest.raises(ReceiptError, match="may never rename evidence"):
        _build(world, expected_candidate="some-other-candidate")


def test_finding_e_the_candidate_is_derived_when_no_assertion_is_made(receipt, world):
    assert receipt["candidate"]["candidate_id"] == W.CANDIDATE_ID
    assert receipt["candidate"]["identity_source"] == "training_receipt"
    assert receipt["training_receipt"]["candidate_id"] == W.CANDIDATE_ID


def test_finding_f_the_source_commit_is_derived_from_a_real_head(receipt, world):
    assert receipt["source"]["evaluation_source_commit"] == world["head"]
    assert receipt["source"]["derived_from_repository_head"] is True
    assert len(receipt["source"]["evaluation_source_tree_oid"]) == 40


def test_finding_f_a_wrong_source_assertion_is_refused(world):
    """Section 51."""
    with pytest.raises(ReceiptError, match="this worktree is at"):
        _build(world, expected_evaluation_source_commit="9" * 40)


def test_finding_f_a_directory_that_is_not_a_repository_refuses(tmp_path):
    with pytest.raises(ReceiptError, match="could not be derived"):
        source_identity(tmp_path / "not-a-repo")


def test_finding_g_v1_let_an_unknown_event_become_the_terminal_witness(world, tmp_path):
    """And, arriving last, OVERWRITE the real one."""
    from scripts.build_m62_eval_receipt import ledger_events

    lines = S.ledger_lines(world["output_root"])
    future = {**lines[-1], "event": "some_future_body_free_note"}
    path = tmp_path / "future.jsonl"
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n"
                            for e in [*lines, future]), encoding="utf-8")
    stale = ledger_events(path, evaluation_id="s3q0-synthetic-ceremony", generation=1)
    assert stale["terminal_event"] == "some_future_body_free_note"


def test_finding_g_v2_draws_the_terminal_witness_from_a_closed_vocabulary(world,
                                                                          tmp_path):
    lines = S.ledger_lines(world["output_root"])
    future = {**lines[-1], "event": "some_future_body_free_note"}
    path = tmp_path / "future.jsonl"
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n"
                            for e in [*lines, future]), encoding="utf-8")
    evidence = ledger_evidence(path, evaluation_id="s3q0-synthetic-ceremony",
                               generation=1)
    assert evidence["terminal_event"] == "completed"
    assert evidence["unrecognised_events"] == ["some_future_body_free_note"]


def test_finding_h_v1_accepted_ledger_lines_naming_different_plans(world, tmp_path):
    from scripts.build_m62_eval_receipt import build_receipt, verify_receipt

    lines = copy.deepcopy(S.ledger_lines(world["output_root"]))
    lines[0]["plan_hash"] = "b" * 64
    path = tmp_path / "mixed.jsonl"
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in lines),
                    encoding="utf-8")
    legacy = seal(build_receipt(world["directory"], candidate="x",
                                evaluation_source_commit="a" * 40, ledger=path))
    assert len(legacy["ledger"]["plan_hashes"]) == 2
    assert verify_receipt(legacy) == ()


def test_finding_i_v1_copied_the_verdict_instead_of_evidencing_it(world):
    from scripts.build_m62_eval_receipt import build_receipt

    legacy = build_receipt(world["directory"], candidate="x",
                           evaluation_source_commit="a" * 40, ledger=world["ledger"])
    assert "decision_evidence" not in legacy
    assert "gate_report" not in json.dumps(legacy)


def test_finding_i_v2_carries_the_evidence_the_decision_was_made_from(receipt):
    evidence = receipt["decision_evidence"]
    assert set(evidence) >= {"gate_report", "bootstrap", "empirical_status",
                             "report_serialization_state", "canonical_decision",
                             "decision_hash"}
    assert evidence["rederived_by"] == DECISION_REDERIVER


def test_finding_j_v1_would_have_named_a_holdout_task_id_in_a_tracked_file(world):
    """Found while closing the others, and it would have blocked the live run.

    `.1` bound ``holdout_commit.first_task_id``. On eval-v4 that is one of the 36 frozen
    task ids, and ``check_evaluation_receipt`` refuses any tracked receipt that names
    one — so a live `.1` receipt could never have been accepted by the control plane it
    was written for. `.2` binds ``first_task_hash`` instead, which is strictly stronger
    and names nothing.
    """
    from scripts.build_m62_eval_receipt import build_receipt

    legacy = build_receipt(world["directory"], candidate="x",
                           evaluation_source_commit="a" * 40, ledger=world["ledger"])
    assert legacy["holdout_commit"]["first_task_id"]


def test_finding_j_v2_binds_the_first_task_by_hash_and_names_none(receipt):
    assert "first_task_id" not in receipt["holdout_commit"]
    assert "first_task_id" not in canonical_json(receipt)
    assert len(receipt["holdout_commit"]["first_task_hash"]) == 64


# ══════════════════════════════════════════════════════════════════════════════
#  Section 49 — the synthetic v2 receipt is valid, deterministic and body-free
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_verifies_end_to_end(receipt):
    assert _problems(receipt) == []


def test_the_receipt_validates_against_the_tracked_schema(receipt):
    assert validate_against_schema(eval_receipt_v2_schema(), receipt) == []
    assert receipt["schema_version"] == EVAL_RECEIPT_V2_SCHEMA_VERSION
    assert RECEIPT_V2_SCHEMA_VERSION == EVAL_RECEIPT_V2_SCHEMA_VERSION


def test_the_tracked_schema_file_is_the_one_the_verifier_enforces():
    published = (REPO_ROOT / EVAL_RECEIPT_V2_SCHEMA_PATH).read_bytes()
    assert published == canonical_bytes(eval_receipt_v2_schema())


def test_the_same_evidence_produces_the_same_bytes(world, receipt):
    again = _build(world)
    assert canonical_json(again) == canonical_json(receipt)
    assert again["receipt_hash"] == receipt["receipt_hash"]


def test_the_receipt_carries_no_timestamp_of_its_own(receipt):
    text = canonical_json(receipt)
    assert "created_at" not in text and "generated_at" not in text


def test_the_receipt_carries_no_task_body(receipt):
    assert S.leaked_canaries(canonical_json(receipt)) == []


def test_the_canaries_really_are_in_the_material_that_was_measured(world):
    """Otherwise the test above passes on an empty implementation."""
    pack = Path(world["directory"]) / "task-pack.jsonl"
    assert S.leaked_canaries(pack.read_text(encoding="utf-8"))


def test_the_receipt_reproduces_no_spendable_confirmation(receipt):
    from scripts.verify_m62_control_plane import TOKEN_LITERAL_RE

    assert TOKEN_LITERAL_RE.search(canonical_json(receipt)) is None
    assert receipt["authority"]["token_literal_recorded"] is False


def test_the_receipt_carries_no_private_path(receipt):
    from scripts.verify_m62_control_plane import PRIVATE_PATH_RE

    assert PRIVATE_PATH_RE.findall(canonical_json(receipt)) == []


def test_the_receipt_is_ascii(receipt):
    canonical_json(receipt).encode("ascii")


def test_the_receipt_names_no_eval_v4_task(receipt):
    from scripts.verify_m62_control_plane import EVAL_V4_TASK_IDS

    text = canonical_json(receipt)
    assert [tid for tid in EVAL_V4_TASK_IDS if tid in text] == []


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 11-15 — the evidence chain, each link cross-bound
# ══════════════════════════════════════════════════════════════════════════════
def test_the_training_receipt_is_bound_by_digest_not_by_path(receipt, world):
    import hashlib

    sealed = Path(world["training_receipt"]).read_bytes()
    assert receipt["training_receipt"]["training_receipt_sha256"] == \
        hashlib.sha256(sealed).hexdigest()
    assert receipt["training_receipt"]["path"].startswith("state/")


def test_the_adapter_identities_come_from_the_training_receipt_and_the_bytes(receipt,
                                                                            world):
    sealed = json.loads(Path(world["training_receipt"]).read_text(encoding="utf-8"))
    assert receipt["candidate"]["adapter_sha256"] == sealed["adapter"]["sha256"]
    assert receipt["candidate"]["adapter_manifest_hash"] == \
        sealed["adapter"]["manifest_hash"]
    assert receipt["candidate"]["adapter_artifact_set_hash"] == \
        sealed["adapter"]["artifact_set_hash"]
    assert receipt["candidate"]["adapter_sha256"] == world["adapter"]["adapter_sha256"]


def test_the_adapter_reference_is_the_one_the_plan_and_report_bound(receipt, world):
    report = json.loads((Path(world["directory"]) / "evaluation-report.json")
                        .read_text(encoding="utf-8"))
    assert receipt["candidate"]["adapter_reference_hash"] == \
        report["candidate_adapter_reference_hash"]
    assert receipt["candidate"]["adapter_reference_hash"] == \
        world["reference"].reference_hash()


def test_a_training_receipt_sealing_other_weights_is_refused(world, tmp_path):
    """The cross-check is real: change one sealed digest and the build refuses."""
    forged = W.write_training_receipt(
        tmp_path / "forged.train.json", world["adapter"],
        adapter={"sha256": "9" * 64,
                 "manifest_hash": world["adapter"]["adapter_manifest_hash"],
                 "artifact_set_hash": world["adapter"]["adapter_artifact_set_hash"]})
    # Placed inside the repository so the path check is not what refuses it.
    destination = Path(world["repo_root"]) / "state" / "receipts" / "forged.train.json"
    destination.write_bytes(forged.read_bytes())
    with pytest.raises(ReceiptError, match="not the same weights"):
        _build(world, training_receipt=destination)


def test_a_training_receipt_for_another_candidate_is_refused(world, tmp_path):
    other = W.write_training_receipt(
        tmp_path / "other.train.json", world["adapter"],
        candidate_id="a-completely-different-candidate")
    destination = Path(world["repo_root"]) / "state" / "receipts" / "other.train.json"
    destination.write_bytes(other.read_bytes())
    with pytest.raises(ReceiptError, match="adapter run directory belongs to"):
        _build(world, training_receipt=destination)


def test_an_empty_sealed_adapter_digest_is_refused(world, tmp_path):
    """The exact `.1` shape: a receipt whose adapter identity is the empty string."""
    blank = W.write_training_receipt(
        tmp_path / "blank.train.json", world["adapter"],
        adapter={"sha256": "", "manifest_hash": "", "artifact_set_hash": ""})
    destination = Path(world["repo_root"]) / "state" / "receipts" / "blank.train.json"
    destination.write_bytes(blank.read_bytes())
    with pytest.raises(ReceiptError, match="no usable adapter_sha256"):
        _build(world, training_receipt=destination)


def test_the_baseline_is_named_directly_and_not_only_hashed(receipt, world):
    assert receipt["baseline"]["model_id"] == W.BASE_MODEL_ID
    assert receipt["baseline"]["revision"] == W.BASE_REVISION
    assert len(receipt["baseline"]["reference_hash"]) == 64
    assert len(receipt["baseline"]["base_model_identity_hash"]) == 64


def test_a_configuration_the_plan_did_not_bind_is_refused(world, tmp_path):
    other = W.write_config(
        tmp_path / "other-config.json",
        W.live_config_payload(created_at_utc="2026-08-18T00:00:00Z"))
    with pytest.raises(ReceiptError, match="approved plan bound"):
        _build(world, evaluation_config=other)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 52 — the ledger, every mutation rehashed
# ══════════════════════════════════════════════════════════════════════════════
def _ledger(world, tmp_path, transform) -> Path:
    lines = copy.deepcopy(S.ledger_lines(world["output_root"]))
    lines = transform(lines)
    path = tmp_path / "ledger.jsonl"
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in lines),
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("name,transform,match", [
    ("started plan hash differs",
     lambda lines: [{**lines[0], "plan_hash": "b" * 64}, *lines[1:]],
     "distinct plan hash"),
    ("holdout plan hash differs",
     lambda lines: [lines[0], {**lines[1], "plan_hash": "c" * 64}, *lines[2:]],
     "distinct plan hash"),
    ("terminal plan hash differs",
     lambda lines: [*lines[:-1], {**lines[-1], "plan_hash": "d" * 64}],
     "distinct plan hash"),
    ("empty plan hash",
     lambda lines: [{**lines[0], "plan_hash": ""}, *lines[1:]],
     "distinct plan hash"),
    ("no start line", lambda lines: lines[1:], "0 start line"),
    ("two start lines", lambda lines: [lines[0], *lines], "2 start line"),
    ("no holdout commit",
     lambda lines: [e for e in lines
                    if e["event"] != "holdout_model_facing_committed"],
     "0 model-facing commit"),
    ("two holdout commits",
     lambda lines: [*lines, next(e for e in lines
                                 if e["event"] == "holdout_model_facing_committed")],
     "2 model-facing commit"),
    ("no terminal line",
     lambda lines: [e for e in lines if e["event"] != "completed"],
     "0 recognised terminal"),
    ("two terminal lines",
     lambda lines: [*lines, next(e for e in lines if e["event"] == "completed")],
     "2 recognised terminal"),
    ("an unknown event substituted for the terminal one",
     lambda lines: [*(e for e in lines if e["event"] != "completed"),
                    {**lines[-1], "event": "some_future_note"}],
     "0 recognised terminal"),
])
def test_a_ledger_that_does_not_describe_one_run_is_refused(world, tmp_path, name,
                                                            transform, match):
    with pytest.raises(ReceiptError, match=match):
        _build(world, ledger=_ledger(world, tmp_path, transform))


def test_every_critical_event_is_bound_by_its_own_digest(receipt):
    ledger = receipt["ledger"]
    digests = {ledger["plan_started_event_hash"],
               ledger["holdout_commit_event_hash"],
               ledger["terminal_event_hash"]}
    assert len(digests) == 3
    assert all(len(d) == 64 for d in digests)


def test_the_event_digests_are_the_digests_of_the_actual_ledger_lines(receipt, world):
    import hashlib

    lines = S.ledger_lines(world["output_root"])
    expected = {
        "plan_started_event_hash": next(e for e in lines if e["event"] == "started"),
        "holdout_commit_event_hash": next(
            e for e in lines if e["event"] == "holdout_model_facing_committed"),
        "terminal_event_hash": next(e for e in lines if e["event"] == "completed"),
    }
    for field, entry in expected.items():
        digest = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
        assert receipt["ledger"][field] == digest, field


def test_a_receipt_claiming_more_than_one_plan_hash_is_refused(receipt):
    assert _problems(_rehashed(receipt, "ledger", "unique_plan_hashes", value=2))


def test_a_receipt_whose_terminal_event_is_outside_the_vocabulary_is_refused(receipt):
    tampered = _rehashed(receipt, "ledger", "terminal_event", value="some_future_note")
    assert _problems(tampered)


def test_the_terminal_vocabulary_is_the_production_one():
    from training_gym.evaluation.config import EvaluationRunState

    derived = {s.value for s in EvaluationRunState if s.is_terminal}
    assert derived == set(TERMINAL_EVALUATION_EVENTS)
    assert SUCCESSFUL_TERMINAL_EVENT == EvaluationRunState.COMPLETED.value


# ══════════════════════════════════════════════════════════════════════════════
#  Section 53 — the counts
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_accounts_for_every_result(receipt):
    results = receipt["results"]
    expected = results["expected_task_count"]
    assert results["task_count"] == expected
    for field in ("baseline_result_count", "candidate_result_count",
                  "paired_result_count", "baseline_score_count",
                  "candidate_score_count", "measured_pairs"):
        assert results[field] == expected, field
    assert results["total_model_result_count"] == 2 * expected
    assert results["missing_pairs"] == 0


@pytest.mark.parametrize("field", [
    "baseline_result_count", "candidate_result_count", "paired_result_count",
    "baseline_score_count", "candidate_score_count", "measured_pairs",
])
def test_a_short_count_under_a_completed_ending_is_refused(receipt, field):
    short = receipt["results"][field] - 1
    assert _problems(_rehashed(receipt, "results", field, value=short))


def test_a_total_that_is_not_the_two_arms_summed_is_refused(receipt):
    assert _problems(_rehashed(receipt, "results", "total_model_result_count",
                               value=receipt["results"]["total_model_result_count"] + 1))


def test_wins_ties_and_losses_must_account_for_every_measured_pair(receipt):
    assert _problems(_rehashed(receipt, "results", "ties",
                               value=receipt["results"]["ties"] - 1))


def test_a_task_count_that_is_not_the_planned_one_is_refused(receipt):
    assert _problems(_rehashed(receipt, "results", "expected_task_count",
                               value=receipt["results"]["expected_task_count"] + 1))


# ══════════════════════════════════════════════════════════════════════════════
#  Section 54 — eligibility, rederived rather than trusted
# ══════════════════════════════════════════════════════════════════════════════
def test_the_verdict_is_rederived_by_the_production_algorithm(receipt):
    from training_gym.evaluation.reports import decision_from_evidence

    evidence = receipt["decision_evidence"]
    decision = decision_from_evidence(
        gate_report=evidence["gate_report"], bootstrap=evidence["bootstrap"],
        empirical_status=evidence["empirical_status"],
        run_state=evidence["report_serialization_state"])
    assert decision.to_dict() == evidence["canonical_decision"]
    assert decision.eligibility.value == receipt["outcome"]["eligibility"]


def test_there_is_exactly_one_eligibility_algorithm():
    """Section 33. The receipt code must not carry a second copy of the decision."""
    builder = (REPO_ROOT / "jarvis/scripts/build_m62_eval_receipt.py").read_text(
        encoding="utf-8")
    assert "security_blockers" not in builder
    assert "supports_a_directional_claim" not in builder
    assert "decision_from_evidence" in builder
    reports = (REPO_ROOT / "jarvis/training_gym/evaluation/reports.py").read_text(
        encoding="utf-8")
    assert reports.count("def decide_eligibility(") == 1


def _claim(receipt: dict, eligibility: str, state: str) -> dict:
    """Rewrite BOTH the verdict and the state it supports, then rehash.

    The weak version of this test changes the outcome and watches the digest break. The
    real question is whether a receipt that is internally consistent about a verdict its
    own evidence does not support is refused — so everything downstream of the evidence
    is made to agree, and only the evidence is left telling the truth.
    """
    tampered = copy.deepcopy(receipt)
    tampered["outcome"]["eligibility"] = eligibility
    tampered["outcome"]["human_review_required"] = True
    tampered["candidate"]["status_claim"] = state
    tampered["decision_evidence"]["canonical_decision"]["eligibility"] = eligibility
    tampered["decision_evidence"]["canonical_decision"]["human_review_required"] = True
    tampered.pop("receipt_hash", None)
    return seal(tampered)


def test_a_synthetic_run_may_never_claim_eligibility(receipt):
    """Empirical-status contradiction. The whole reason synthetic ceremonies are safe."""
    assert receipt["decision_evidence"]["empirical_status"] == "synthetic_only"
    assert receipt["outcome"]["eligibility"] != "eligible_for_human_review"
    forged = _claim(receipt, "eligible_for_human_review",
                    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW")
    assert _problems(forged)


def test_a_partial_live_run_may_never_claim_eligibility(receipt):
    tampered = _claim(receipt, "eligible_for_human_review",
                      "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW")
    tampered["decision_evidence"]["empirical_status"] = "partial_live"
    tampered.pop("receipt_hash", None)
    assert _problems(seal(tampered))


def test_a_security_blocker_makes_eligibility_impossible(receipt):
    """The veto, exercised through the canonical algorithm rather than restated."""
    tampered = copy.deepcopy(receipt)
    gates = tampered["decision_evidence"]["gate_report"]
    gates["findings"].append({
        "gate": "new_secret_leaks", "kind": "security", "severity": "blocking",
        "message": "the candidate produced 1 secret leak(s) against the baseline's 0",
        "observed": 1, "threshold": 0, "threshold_calibrated": False})
    gates["blocking_count"] += 1
    gates["security_blocking_count"] += 1
    gates["passed"] = False
    tampered["decision_evidence"]["empirical_status"] = "live_measured"
    tampered["outcome"]["eligibility"] = "eligible_for_human_review"
    tampered["candidate"]["status_claim"] = "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    tampered.pop("receipt_hash", None)
    problems = _problems(seal(tampered))
    assert problems
    assert any("not_eligible" in p or "supports" in p for p in problems)


def test_a_deterministic_blocker_makes_eligibility_impossible(receipt):
    tampered = _claim(receipt, "eligible_for_human_review",
                      "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW")
    tampered["decision_evidence"]["empirical_status"] = "live_measured"
    tampered.pop("receipt_hash", None)
    # The gate report still carries its blocking statistical finding.
    assert tampered["decision_evidence"]["gate_report"]["blocking_count"] >= 1
    assert _problems(seal(tampered))


def test_absent_bootstrap_support_makes_eligibility_impossible(receipt):
    """Every deterministic gate passing is still not a directional claim."""
    tampered = copy.deepcopy(receipt)
    gates = tampered["decision_evidence"]["gate_report"]
    gates["findings"] = []
    gates["blocking_count"] = 0
    gates["security_blocking_count"] = 0
    gates["warning_count"] = 0
    gates["passed"] = True
    tampered["decision_evidence"]["empirical_status"] = "live_measured"
    tampered["outcome"]["eligibility"] = "eligible_for_human_review"
    tampered["outcome"]["gate_blockers"] = []
    tampered["candidate"]["status_claim"] = "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    tampered.pop("receipt_hash", None)
    # The bootstrap verdict is still `small_sample`, which supports no direction.
    assert tampered["decision_evidence"]["bootstrap"]["verdict"] == "small_sample"
    assert _problems(seal(tampered))


def test_the_same_evidence_with_a_sufficient_sample_does_reach_eligibility(receipt):
    """Non-vacuity in the OTHER direction: the refusals above are not unconditional."""
    from training_gym.evaluation.reports import decision_from_evidence

    evidence = copy.deepcopy(receipt["decision_evidence"])
    evidence["gate_report"].update({"findings": [], "blocking_count": 0,
                                    "security_blocking_count": 0, "warning_count": 0,
                                    "passed": True})
    evidence["empirical_status"] = "live_measured"
    bootstrap = evidence["bootstrap"]
    # Built through the production object so every DERIVED field -- the claim sentence,
    # `excludes_regression_margin`, `indicates_regression` -- is the one that sample
    # really produces. Hand-writing them would be asserting against a fixture.
    from training_gym.evaluation.statistics import BootstrapReport, StatisticalVerdict
    report = BootstrapReport(
        verdict=StatisticalVerdict.SUFFICIENT, n_pairs=36, n_excluded=0, n_missing=0,
        mean_delta=0.1, median_delta=0.1, wins=20, ties=10, losses=6, ci_low=0.05,
        ci_high=0.15, confidence_level=bootstrap["confidence_level"],
        iterations=bootstrap["iterations"], seed=bootstrap["seed"],
        method=bootstrap["method"], regression_margin=bootstrap["regression_margin"],
        error_accounting=bootstrap["error_accounting"], limitations=())
    evidence["bootstrap"] = report.to_dict()
    decision = decision_from_evidence(
        gate_report=evidence["gate_report"], bootstrap=evidence["bootstrap"],
        empirical_status=evidence["empirical_status"],
        run_state=evidence["report_serialization_state"])
    assert decision.eligibility.value == "eligible_for_human_review"


def test_a_receipt_whose_status_claim_disagrees_with_its_verdict_is_refused(receipt):
    assert _problems(_rehashed(receipt, "candidate", "status_claim",
                               value="EVALUATED_NOT_ELIGIBLE"))


def test_a_decision_hash_that_is_not_the_decision_is_refused(receipt):
    assert _problems(_rehashed(receipt, "decision_evidence", "decision_hash",
                               value="9" * 64))


def test_gate_evidence_whose_summary_contradicts_its_findings_is_refused(receipt):
    tampered = copy.deepcopy(receipt)
    tampered["decision_evidence"]["gate_report"]["blocking_count"] = 99
    tampered.pop("receipt_hash", None)
    assert _problems(seal(tampered))


def test_blockers_that_are_not_the_ones_the_evidence_produces_are_refused(receipt):
    assert _problems(_rehashed(receipt, "outcome", "gate_blockers",
                               value=["nothing at all went wrong"]))


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 36-37 — direct policy values, and D33 told truthfully
# ══════════════════════════════════════════════════════════════════════════════
def test_the_direct_policy_values_are_the_live_ones(receipt):
    policy = receipt["policies"]["generation_policy"]
    assert policy["reasoning_policy"] == "disabled"
    assert policy["max_new_tokens"] == 512
    assert policy["seed"] == 11
    assert policy["device_policy"] == "cpu"
    assert policy["precision_policy"] == "fp32"
    assert policy["mode"] == "greedy_deterministic"


def test_the_configured_timeout_is_recorded_and_not_called_enforced(receipt):
    """D33 stays OPEN_UNCHANGED. 300 is what was configured; nothing watches it."""
    assert receipt["policies"]["configured_timeout_s"] == 300
    assert receipt["policies"]["timeout_enforced"] is False
    assert receipt["policies"]["generation_policy"]["timeout_s"] == 300


def test_a_receipt_claiming_the_timeout_was_enforced_is_refused(receipt):
    assert _problems(_rehashed(receipt, "policies", "timeout_enforced", value=True))


def test_the_direct_policy_values_rederive_the_policy_hash(receipt):
    from training_gym.evaluation.generation import GenerationPolicy

    rebuilt = GenerationPolicy.from_dict(receipt["policies"]["generation_policy"])
    assert rebuilt.policy_hash() == receipt["policies"]["generation_policy_hash"]


def test_a_policy_value_that_does_not_match_the_digest_is_refused(receipt):
    tampered = copy.deepcopy(receipt)
    tampered["policies"]["generation_policy"]["max_new_tokens"] = 256
    tampered.pop("receipt_hash", None)
    assert _problems(seal(tampered))


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 22 & 55 — strict schema before semantics
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unknown_schema_version_is_refused(receipt):
    tampered = copy.deepcopy(receipt)
    tampered["schema_version"] = "m62.eval_receipt.99"
    tampered["receipt_version"] = "m62.eval_receipt.99"
    tampered.pop("receipt_hash", None)
    problems = _problems(seal(tampered))
    assert problems and "not one this repository knows" in problems[0]


def test_a_v1_payload_relabelled_as_v2_is_refused(world):
    from scripts.build_m62_eval_receipt import build_receipt
    from scripts.build_m62_eval_receipt import seal as v1_seal

    legacy = build_receipt(world["directory"], candidate="x",
                           evaluation_source_commit="a" * 40, ledger=world["ledger"])
    legacy["schema_version"] = RECEIPT_V2_SCHEMA_VERSION
    legacy["receipt_version"] = RECEIPT_V2_SCHEMA_VERSION
    problems = _problems(v1_seal(legacy))
    assert problems and all(p.startswith("schema:") for p in problems)


def test_disagreeing_version_fields_are_refused(receipt):
    tampered = copy.deepcopy(receipt)
    tampered["receipt_version"] = RECEIPT_SCHEMA_VERSION
    tampered.pop("receipt_hash", None)
    assert _problems(seal(tampered))


def test_an_additional_property_is_refused(receipt):
    tampered = copy.deepcopy(receipt)
    tampered["something_extra"] = "not in the contract"
    tampered.pop("receipt_hash", None)
    problems = _problems(seal(tampered))
    assert problems and any("unknown key" in p for p in problems)


@pytest.mark.parametrize("section,field", [
    ("candidate", "adapter_sha256"),
    ("candidate", "adapter_manifest_hash"),
    ("candidate", "adapter_artifact_set_hash"),
    ("candidate", "adapter_reference_hash"),
    ("training_receipt", "training_receipt_sha256"),
    ("baseline", "model_id"),
    ("source", "evaluation_source_commit"),
    ("decision_evidence", "decision_hash"),
])
def test_a_missing_mandatory_field_is_refused(receipt, section, field):
    tampered = copy.deepcopy(receipt)
    del tampered[section][field]
    tampered.pop("receipt_hash", None)
    problems = _problems(seal(tampered))
    assert problems and any("missing required key" in p for p in problems)


@pytest.mark.parametrize("field,value", [
    ("adapter_sha256", ""),
    ("adapter_sha256", "not-a-digest"),
    ("adapter_manifest_hash", ""),
    ("adapter_artifact_set_hash", "9" * 63),
])
def test_a_malformed_adapter_identity_is_refused(receipt, field, value):
    problems = _problems(_rehashed(receipt, "candidate", field, value=value))
    assert problems


def test_a_missing_decision_evidence_section_is_refused(receipt):
    tampered = copy.deepcopy(receipt)
    del tampered["decision_evidence"]
    tampered.pop("receipt_hash", None)
    assert _problems(seal(tampered))


def test_a_receipt_hash_that_does_not_match_the_bytes_is_refused(receipt):
    tampered = copy.deepcopy(receipt)
    tampered["receipt_hash"] = "0" * 64
    problems = _problems(tampered)
    assert problems and any("does not match the bytes" in p for p in problems)


def test_the_semantic_checks_do_not_run_on_a_document_that_failed_its_schema(receipt):
    """A payload that broke its contract has no semantics worth reporting on."""
    tampered = copy.deepcopy(receipt)
    tampered["extra"] = 1
    tampered.pop("receipt_hash", None)
    problems = _problems(seal(tampered))
    assert all(p.startswith("schema:") for p in problems)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 56 — the write is atomic, and a failed one leaves nothing
# ══════════════════════════════════════════════════════════════════════════════
def test_a_successful_write_leaves_one_valid_file(receipt, tmp_path):
    destination = tmp_path / "out" / "receipt.json"
    result = emit_receipt(destination, canonical_json(receipt))
    assert destination.is_file()
    assert result["bytes"] == len(canonical_json(receipt).encode("utf-8"))
    assert sorted(p.name for p in destination.parent.iterdir()) == ["receipt.json"]
    assert verify_receipt_payload(read_receipt_file(destination)) == ()


def test_an_existing_destination_is_refused_rather_than_overwritten(receipt, tmp_path):
    destination = tmp_path / "receipt.json"
    destination.write_text("the evidence for another run\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="already exists"):
        emit_receipt(destination, canonical_json(receipt))
    assert destination.read_text(encoding="utf-8") == "the evidence for another run\n"


def test_a_symlink_destination_is_refused_rather_than_followed(receipt, tmp_path):
    real = tmp_path / "elsewhere.json"
    link = tmp_path / "receipt.json"
    link.symlink_to(real)
    with pytest.raises(ReceiptError, match="symlink"):
        emit_receipt(link, canonical_json(receipt))
    assert not real.exists()


def test_a_payload_the_verifier_would_refuse_never_becomes_a_file(receipt, tmp_path):
    """The property that matters: no success is reported for bytes that do not verify."""
    broken = copy.deepcopy(receipt)
    broken["candidate"]["adapter_sha256"] = ""
    broken.pop("receipt_hash", None)
    destination = tmp_path / "receipt.json"
    with pytest.raises(ReceiptError, match="does not verify"):
        emit_receipt(destination, canonical_json(seal(broken)))
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_a_failed_write_leaves_no_temporary_residue(receipt, tmp_path):
    destination = tmp_path / "receipt.json"
    with pytest.raises(ReceiptError):
        emit_receipt(destination, "{not json at all")
    assert list(tmp_path.iterdir()) == []


def test_the_final_bytes_are_re_read_and_verified(receipt, tmp_path):
    destination = tmp_path / "receipt.json"
    result = emit_receipt(destination, canonical_json(receipt))
    import hashlib
    assert result["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_verify_mode_writes_nothing(receipt, tmp_path, capsys):
    destination = tmp_path / "receipt.json"
    emit_receipt(destination, canonical_json(receipt))
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    assert main(["verify", str(destination)]) == 0
    capsys.readouterr()
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    assert before == after


def test_verify_mode_refuses_a_symlinked_receipt(receipt, tmp_path, capsys):
    real = tmp_path / "receipt.json"
    emit_receipt(real, canonical_json(receipt))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert main(["verify", str(link)]) == 1
    assert "symlink" in capsys.readouterr().out


def test_verify_mode_refuses_a_directory(tmp_path, capsys):
    assert main(["verify", str(tmp_path)]) == 1
    assert "regular file" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════════
#  The CLI
# ══════════════════════════════════════════════════════════════════════════════
def test_the_build_subcommand_emits_a_receipt_that_verifies(world, tmp_path, capsys):
    destination = tmp_path / "receipt.json"
    code = main(["build",
                 "--generation-directory", str(world["directory"]),
                 "--training-receipt", str(world["training_receipt"]),
                 "--adapter-run-directory", str(world["adapter"]["directory"]),
                 "--evaluation-config", str(world["config_path"]),
                 "--ledger", str(world["ledger"]),
                 "--repo-root", str(world["repo_root"]),
                 "--expected-candidate", W.CANDIDATE_ID,
                 "--expected-evaluation-source-commit", world["head"],
                 "--emit", str(destination)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "ok"
    assert main(["verify", str(destination)]) == 0
    capsys.readouterr()


def test_the_build_subcommand_refuses_without_a_traceback(world, capsys):
    code = main(["build",
                 "--generation-directory", str(world["directory"]),
                 "--training-receipt", str(world["training_receipt"]),
                 "--adapter-run-directory", str(world["adapter"]["directory"]),
                 "--evaluation-config", str(world["config_path"]),
                 "--ledger", str(world["ledger"]),
                 "--repo-root", str(world["repo_root"]),
                 "--expected-candidate", "the-wrong-one"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["status"] == "refused"
    assert "Traceback" not in payload["error"]


def test_a_mode_must_be_named(capsys):
    with pytest.raises(SystemExit):
        main([])


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 45, 19 — what the receipt does NOT prove
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_does_not_claim_human_authorisation(receipt):
    assert receipt["authority"]["human_authorization"] == "external_milestone_authority"
    assert "human_authorized" not in canonical_json(receipt)


def test_a_receipt_asserting_human_authorisation_is_refused(receipt):
    assert _problems(_rehashed(receipt, "authority", "human_authorization",
                               value="a human said yes"))


def test_the_receipt_states_the_evidence_level_of_its_source_commit(receipt):
    assert "does not by itself prove which bytes executed" in \
        receipt["source"]["evidence_level"]


def test_the_receipt_grants_nothing(receipt):
    assert receipt["authority"]["grants_no_further_authority"] is True
    assert receipt["authority"]["retry_authorized"] is False
    for flag in ("promotes_model", "activates_model", "mutates_model_registry"):
        assert receipt["outcome"][flag] is False


def test_no_self_asserted_spend_boolean_is_treated_as_evidence(receipt):
    """Section 39. `.1` wrote `spent_by_this_evaluation: True` as a constant."""
    assert "spent_by_this_evaluation" not in canonical_json(receipt)
    assert receipt["ledger"]["holdout_commit_count"] == 1


# ══════════════════════════════════════════════════════════════════════════════
#  Section 20 — `.1` is not mutated
# ══════════════════════════════════════════════════════════════════════════════
def test_the_legacy_version_is_untouched():
    from scripts.verify_m62_control_plane import (
        EVAL_RECEIPT_SCHEMA_PATH,
        eval_receipt_schema,
    )

    assert RECEIPT_SCHEMA_VERSION == "m62.eval_receipt.1"
    published = (REPO_ROOT / EVAL_RECEIPT_SCHEMA_PATH).read_bytes()
    assert published == canonical_bytes(eval_receipt_schema())


def test_only_the_modern_version_is_accepted_from_a_modern_candidate():
    assert MODERN_EVAL_RECEIPT_VERSIONS == {RECEIPT_V2_SCHEMA_VERSION}
    assert RECEIPT_SCHEMA_VERSION not in MODERN_EVAL_RECEIPT_VERSIONS
