"""V69 M62 S3Q.0.1 — the control plane may not accept a weaker evidence form.

WHAT S3Q.0 ESTABLISHED, AND WHAT WAS STILL MISSING
---------------------------------------------------
``check_evaluation_receipt`` already refused an ``EVALUATED_*`` state that no tracked
receipt backed. What it could not do was notice that the receipt backing it said almost
nothing: ``m62.eval_receipt.1`` could carry an EMPTY adapter digest, no training-receipt
binding, a candidate name the caller typed and a verdict copied out of a report. A
snapshot and a receipt agreeing on all of that is still two writable surfaces agreeing.

So the modern battery reaches OUTSIDE the receipt: to the tracked training receipt that
sealed the weights, to the snapshot's own adapter digests, to the base model the control
plane names, to Git history, and to the production eligibility algorithm — which is asked
what the receipt's body-free evidence concludes rather than told what the receipt claims.

WHAT THIS FILE ALSO GUARDS
--------------------------
That generation 4 stays valid. Candidate 003 is ``TRAINED_UNEVALUATED`` with no
evaluation receipt, and a milestone that hardens the evaluated-state contract must not
retroactively invalidate a state in which nothing has been evaluated.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.verify_m62_control_plane import (
    EVALUATED_CANDIDATE_STATES,
    LEGACY_EVALUATION_CANDIDATES,
    MODERN_EVAL_RECEIPT_VERSIONS,
    REPO_ROOT,
    SUCCESSFUL_TERMINAL_EVALUATION_EVENT,
    TERMINAL_EVALUATION_EVENTS,
    ControlPlane,
    Report,
    canonical_json,
    check_evaluation_receipt,
)

import _s3q01_synthetic as W

SNAPSHOT_PATH = REPO_ROOT / "state/m62/current.json"


def _live_current() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _live_snapshot() -> dict:
    current = _live_current()
    return json.loads((REPO_ROOT / current["latest_snapshot_path"])
                      .read_text(encoding="utf-8"))


def _plane(snapshot: dict) -> ControlPlane:
    return ControlPlane(
        current=_live_current(), current_bytes=b"", snapshot=snapshot,
        snapshot_bytes=b"",
        snapshot_path=REPO_ROOT / _live_current()["latest_snapshot_path"],
        migration={})


#: The sandbox receipts below are written into the working tree and deliberately NOT
#: committed, so the tracking check reports them. That check is real and is exercised on
#: its own in `test_untracked_evidence_is_refused`; here it is set aside so the other
#: findings are not drowned by an artefact of the fixture.
_UNTRACKED = "untracked; evidence Git does not carry"


def _problems(snapshot: dict, *, ignore_untracked: bool = True) -> list[str]:
    report = Report()
    check_evaluation_receipt(_plane(snapshot), report)
    return [message for category, message in report.problems
            if category == "EVALUATION_RECEIPT"
            and not (ignore_untracked and _UNTRACKED in message)]


def _candidate(snapshot: dict, cid: str) -> dict:
    return next(c for c in snapshot["candidates"] if c["candidate_id"] == cid)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 47 — the current generation stays valid
# ══════════════════════════════════════════════════════════════════════════════
def test_the_live_snapshot_still_passes_the_evaluation_receipt_check():
    assert _problems(_live_snapshot()) == []


def test_candidate_003_is_trained_and_unevaluated_with_no_receipt():
    entry = _candidate(_live_snapshot(), "qwen3-06b-lora-quality-live-003")
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["evaluation_receipt"] is None
    assert entry["evaluation_corpus"] is None
    assert entry["training_receipt"] == \
        "state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json"


def test_eval_v4_is_still_frozen_and_unspent():
    entry = next(d for d in _live_snapshot()["datasets"]
                 if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None


def test_the_legacy_candidates_are_still_not_retrofitted():
    """Section 48. Inventing a receipt for a run that emitted none manufactures evidence."""
    snapshot = _live_snapshot()
    for cid in LEGACY_EVALUATION_CANDIDATES:
        assert _candidate(snapshot, cid).get("evaluation_receipt") in (None, "")
    assert _problems(snapshot) == []


def test_candidate_003_is_not_in_the_legacy_exemption():
    assert "qwen3-06b-lora-quality-live-003" not in LEGACY_EVALUATION_CANDIDATES


# ══════════════════════════════════════════════════════════════════════════════
#  A modern receipt, placed in a sandbox control plane
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return W.evaluated_world(tmp_path_factory)


@pytest.fixture(scope="module")
def sandbox(world, tmp_path_factory):
    """The synthetic receipt and training receipt, tracked-shaped, inside the repository.

    They are written under ``state/m62/receipts`` because the verifier resolves receipt
    pointers relative to the repository root and re-reads the training receipt from
    there. They are removed afterwards; nothing here is committed and nothing here is a
    control-plane state change.
    """
    from scripts.build_m62_eval_receipt import build_receipt_v2, seal

    directory = REPO_ROOT / "state/m62/receipts"
    train = directory / "_s3q01-sandbox.train.json"
    evaluation = directory / "_s3q01-sandbox.eval.json"
    train.write_bytes(Path(world["training_receipt"]).read_bytes())

    receipt = seal(build_receipt_v2(
        world["directory"], training_receipt=train,
        adapter_run_directory=world["adapter"]["directory"],
        evaluation_config=world["config_path"], ledger=world["ledger"],
        repo_root=REPO_ROOT))
    evaluation.write_text(canonical_json(receipt), encoding="utf-8")
    yield {"receipt": receipt,
           "receipt_path": "state/m62/receipts/_s3q01-sandbox.eval.json",
           "training_path": "state/m62/receipts/_s3q01-sandbox.train.json",
           "evaluation_file": evaluation, "training_file": train}
    for path in (train, evaluation):
        if path.is_file():
            path.unlink()


def _evaluated_snapshot(sandbox: dict, **candidate_overrides) -> dict:
    """The live snapshot with candidate 003 rewritten as the synthetic evaluated one.

    A SANDBOX object. Nothing is written to `state/`, and the real candidate 003 is
    untouched — the point is to exercise the checks that will run when it really is
    evaluated, without pre-judging what that evaluation will conclude.
    """
    snapshot = copy.deepcopy(_live_snapshot())
    entry = _candidate(snapshot, "qwen3-06b-lora-quality-live-003")
    receipt = sandbox["receipt"]
    entry.update({
        "candidate_id": W.CANDIDATE_ID,
        "status": receipt["candidate"]["status_claim"],
        "adapter_sha256": receipt["candidate"]["adapter_sha256"],
        "adapter_manifest_hash": receipt["candidate"]["adapter_manifest_hash"],
        "training_receipt": sandbox["training_path"],
        "evaluation_receipt": sandbox["receipt_path"],
        "evaluation_corpus": f"{receipt['holdout']['dataset_id']} "
                             f"{receipt['holdout']['dataset_version']}",
    })
    entry.update(candidate_overrides)
    snapshot["datasets"].append({
        "dataset_id": receipt["holdout"]["dataset_id"],
        "version": receipt["holdout"]["dataset_version"],
        "role": "EVALUATION_HOLDOUT", "status": "USED_IMMUTABLE",
        "manifest_hash": receipt["holdout"]["dataset_manifest_hash"],
        "parent_manifest_hash": None,
        "pack_hash": receipt["holdout"]["task_pack_hash"],
        "task_count": receipt["holdout"]["task_count"],
        "spent_by": f"S3Q.0.1 sandbox, {W.CANDIDATE_ID}",
        "evidence": "jarvis/docs/V69_M62_S3Q01_EVAL_RECEIPT_HARDENING.md",
    })
    snapshot["base_model"] = {**snapshot["base_model"],
                              "model_id": receipt["baseline"]["model_id"],
                              "revision": receipt["baseline"]["revision"]}
    return snapshot


def test_a_modern_receipt_satisfies_the_control_plane(sandbox):
    assert _problems(_evaluated_snapshot(sandbox)) == []


def test_the_receipt_is_the_modern_version(sandbox):
    assert sandbox["receipt"]["schema_version"] in MODERN_EVAL_RECEIPT_VERSIONS


def _refused(sandbox, mutate, **candidate_overrides) -> list[str]:
    """Mutate the RECEIPT ON DISK, rehash it, and ask the control plane."""
    from scripts.build_m62_eval_receipt import seal

    tampered = copy.deepcopy(sandbox["receipt"])
    mutate(tampered)
    tampered.pop("receipt_hash", None)
    tampered = seal(tampered)
    original = sandbox["evaluation_file"].read_bytes()
    sandbox["evaluation_file"].write_text(canonical_json(tampered), encoding="utf-8")
    try:
        snapshot = _evaluated_snapshot(sandbox, **candidate_overrides)
        return _problems(snapshot)
    finally:
        sandbox["evaluation_file"].write_bytes(original)


# ── the checks that reach outside the receipt ────────────────────────────────
def test_a_snapshot_adapter_that_disagrees_with_the_receipt_is_refused(sandbox):
    assert _refused(sandbox, lambda r: None, adapter_sha256="9" * 64)


def test_a_snapshot_adapter_manifest_that_disagrees_is_refused(sandbox):
    assert _refused(sandbox, lambda r: None, adapter_manifest_hash="9" * 64)


def test_a_receipt_binding_a_different_training_receipt_is_refused(sandbox):
    def mutate(receipt):
        receipt["training_receipt"]["path"] = "state/m62/receipts/_elsewhere.train.json"
    assert _refused(sandbox, mutate)


def test_a_training_receipt_digest_that_does_not_match_the_file_is_refused(sandbox):
    def mutate(receipt):
        receipt["training_receipt"]["training_receipt_sha256"] = "9" * 64
    problems = _refused(sandbox, mutate)
    assert problems and any("does not hash to the digest" in p for p in problems)


def test_a_baseline_the_control_plane_does_not_name_is_refused(sandbox):
    def mutate(receipt):
        receipt["baseline"]["model_id"] = "Qwen/Qwen3-32B"
    problems = _refused(sandbox, mutate)
    assert problems and any("base model" in p for p in problems)


def test_a_source_commit_that_is_no_object_here_is_refused(sandbox):
    def mutate(receipt):
        receipt["source"]["evaluation_source_commit"] = "9" * 40
    problems = _refused(sandbox, mutate)
    assert problems and any("not an object in this repository" in p for p in problems)


def test_a_verdict_the_evidence_does_not_support_is_refused(sandbox):
    """The heart of it. Everything downstream agrees; only the evidence disagrees."""
    def mutate(receipt):
        receipt["candidate"]["status_claim"] = "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
        receipt["outcome"]["eligibility"] = "eligible_for_human_review"
        receipt["decision_evidence"]["canonical_decision"]["eligibility"] = \
            "eligible_for_human_review"
    problems = _refused(sandbox, mutate,
                        status="EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW")
    assert problems
    assert any("rederived from its own evidence" in p for p in problems)


def test_a_legacy_receipt_from_a_modern_candidate_is_refused(sandbox, world):
    """Section 20. `.1` is history; it is not an accepted form going forward."""
    from scripts.build_m62_eval_receipt import build_receipt, seal

    legacy = seal(build_receipt(
        world["directory"], candidate=W.CANDIDATE_ID,
        evaluation_source_commit="a" * 40, ledger=world["ledger"]))
    original = sandbox["evaluation_file"].read_bytes()
    sandbox["evaluation_file"].write_text(canonical_json(legacy), encoding="utf-8")
    try:
        problems = _problems(_evaluated_snapshot(sandbox))
    finally:
        sandbox["evaluation_file"].write_bytes(original)
    assert problems and any("not a modern evaluation receipt" in p for p in problems)


def test_a_candidate_that_was_not_evaluated_may_not_carry_a_receipt(sandbox):
    snapshot = _evaluated_snapshot(sandbox)
    entry = _candidate(snapshot, W.CANDIDATE_ID)
    entry["status"] = "TRAINED_UNEVALUATED"
    problems = _problems(snapshot)
    assert problems and any("has not been measured" in p for p in problems)


def test_an_evaluated_state_without_a_receipt_is_refused(sandbox):
    snapshot = _evaluated_snapshot(sandbox)
    entry = _candidate(snapshot, W.CANDIDATE_ID)
    entry["evaluation_receipt"] = None
    problems = _problems(snapshot)
    assert problems and any("no evaluation receipt pointer" in p for p in problems)


# ══════════════════════════════════════════════════════════════════════════════
#  The terminal vocabulary is the production one, not a restatement
# ══════════════════════════════════════════════════════════════════════════════
def test_the_control_planes_terminal_vocabulary_is_re_derived():
    from training_gym.evaluation.config import EvaluationRunState

    assert set(TERMINAL_EVALUATION_EVENTS) == {
        s.value for s in EvaluationRunState if s.is_terminal}
    assert SUCCESSFUL_TERMINAL_EVALUATION_EVENT == EvaluationRunState.COMPLETED.value


def test_every_evaluated_state_is_reachable_from_some_eligibility():
    from scripts.build_m62_eval_receipt import ELIGIBILITY_TO_CANDIDATE_STATE

    assert set(ELIGIBILITY_TO_CANDIDATE_STATE.values()) == EVALUATED_CANDIDATE_STATES


# ══════════════════════════════════════════════════════════════════════════════
#  Section 59 — the measurement did not move
# ══════════════════════════════════════════════════════════════════════════════
def test_the_policy_identities_are_unchanged():
    identities = _live_snapshot()["policy_identities"]
    assert identities["generation_policy_hash"] == \
        "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7"
    assert identities["metric_policy_hash"] == \
        "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a"
    assert identities["gate_policy_hash"] == \
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5"
    assert identities["max_new_tokens"] == 512
    assert identities["reasoning_policy"] == "DISABLED"


def test_the_plan_and_evaluator_versions_did_not_move():
    """Section 59. Receipt-only hardening moves the receipt version and nothing else."""
    from training_gym.evaluation import EVALUATION_SCHEMA_VERSION, EVALUATOR_VERSION
    from training_gym.evaluation.plan import EVALUATION_PLAN_SCHEMA_VERSION

    assert EVALUATION_PLAN_SCHEMA_VERSION == "m62.evaluation_plan.2"
    assert EVALUATOR_VERSION == "m62.s3q0.1"
    assert EVALUATION_SCHEMA_VERSION == "m62.evaluation.1"


def test_untracked_evidence_is_refused(sandbox):
    """The check the fixture has to set aside, exercised deliberately."""
    problems = _problems(_evaluated_snapshot(sandbox), ignore_untracked=False)
    assert any(_UNTRACKED in p for p in problems)


def test_the_live_execution_machinery_was_not_edited():
    """Section 6. The receipt is evidence about a run; it does not change one."""
    import subprocess

    frozen = [
        "jarvis/training_gym/evaluation/runner.py",
        "jarvis/training_gym/evaluation/store.py",
        "jarvis/training_gym/evaluation/execution.py",
        "jarvis/training_gym/evaluation/preflight.py",
        "jarvis/training_gym/evaluation/generation.py",
        "jarvis/training_gym/evaluation/gates.py",
        "jarvis/training_gym/evaluation/policy.py",
    ]
    done = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only",
         "b928f9d485a49b90a3be9eee7fd5de5a50e54230", "--", *frozen],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.skip("the S3Q.0 subject commit is not present on this host")
    assert done.stdout.strip() == ""
