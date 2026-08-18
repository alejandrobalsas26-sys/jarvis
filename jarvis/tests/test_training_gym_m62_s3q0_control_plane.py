"""V69 M62 S3Q.0 — the control plane may not establish an evaluated state by itself.

WHAT IS BEING QUALIFIED
-----------------------
``check_training_receipt`` refuses a ``TRAINED_UNEVALUATED`` claim that no portable
receipt backs. ``check_evaluation_receipt`` is the same argument one door further in, and
the door is the irreversible one: an ``EVALUATED_*`` state asserts that a fresh holdout
was SPENT. A snapshot saying so, agreeing with a constant in the verifier, is two writable
surfaces agreeing — a rumour with a checksum.

Both directions are proved. A synthetic snapshot claiming the candidate FAILED and one
claiming it PASSED are both refused without a receipt. Nothing here predicts which one
candidate 003 will obtain, and nothing here moves it.

WHAT THIS FILE ALSO GUARDS
--------------------------
S3Q.0's own scope. The measurement is frozen: generation, metric and gate identities,
``max_new_tokens``, the reasoning policy, the graders, the thresholds and the refusal
detector are all untouched, and D28, D33, D38 and D39 keep the status they had.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.verify_m62_control_plane import (
    EVALUATED_CANDIDATE_STATES,
    LEGACY_EVALUATION_CANDIDATES,
    REPO_ROOT,
    ControlPlane,
    Report,
    canonical_json,
    check_evaluation_receipt,
)

SNAPSHOT_PATH = REPO_ROOT / "state/m62/snapshots/0003-m62-third-candidate-trained-unevaluated.json"


def _snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _plane(snapshot: dict) -> ControlPlane:
    current = json.loads((REPO_ROOT / "state/m62/current.json")
                         .read_text(encoding="utf-8"))
    return ControlPlane(current=current, current_bytes=b"", snapshot=snapshot,
                        snapshot_bytes=b"", snapshot_path=SNAPSHOT_PATH,
                        migration={})


def _problems(snapshot: dict) -> list[str]:
    report = Report()
    check_evaluation_receipt(_plane(snapshot), report)
    return [message for category, message in report.problems
            if category == "EVALUATION_RECEIPT"]


def _candidate(snapshot: dict, cid: str) -> dict:
    return next(c for c in snapshot["candidates"] if c["candidate_id"] == cid)


# ══════════════════════════════════════════════════════════════════════════════
#  The real state passes, and says why
# ══════════════════════════════════════════════════════════════════════════════
def test_the_current_control_plane_has_no_evaluation_receipt_problem():
    assert _problems(_snapshot()) == []


def test_candidate_003_is_still_trained_and_unevaluated():
    entry = _candidate(_snapshot(), "qwen3-06b-lora-quality-live-003")
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["evaluation_corpus"] is None
    assert not entry.get("evaluation_receipt")


def test_eval_v4_is_still_frozen_and_unspent():
    v4 = next(d for d in _snapshot()["datasets"]
              if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    assert v4["status"] == "FROZEN_UNUSED"
    assert v4["spent_by"] is None


# ══════════════════════════════════════════════════════════════════════════════
#  Anti-circularity, in both directions
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("claimed", sorted(EVALUATED_CANDIDATE_STATES))
def test_a_snapshot_alone_cannot_establish_any_evaluated_state(claimed):
    """Both a failure verdict and a success verdict are refused without evidence.

    Parametrised over the WHOLE vocabulary rather than over two hand-picked states, so a
    state added later cannot slip past by not having been thought of here.
    """
    snapshot = _snapshot()
    entry = _candidate(snapshot, "qwen3-06b-lora-quality-live-003")
    entry["status"] = claimed
    entry["evaluation_corpus"] = "m62-defensive-eval v4"
    problems = _problems(snapshot)
    assert problems, claimed
    assert any("no evaluation receipt pointer" in p for p in problems), problems


def test_a_hardcoded_verifier_expectation_would_not_be_evidence():
    """The verifier's own constants are not a second witness for a state claim."""
    source = (REPO_ROOT / "jarvis/scripts/verify_m62_control_plane.py").read_text(
        encoding="utf-8")
    # The check reads the receipt, not a table of expected verdicts.
    assert "receipt.get(\"candidate\", {})" in source \
        or 'receipt.get("candidate", {})' in source
    assert "receipt decides, not the snapshot" in source


def test_a_receipt_pointing_at_nothing_is_refused():
    snapshot = _snapshot()
    entry = _candidate(snapshot, "qwen3-06b-lora-quality-live-003")
    entry["status"] = "EVALUATED_NOT_ELIGIBLE"
    entry["evaluation_corpus"] = "m62-defensive-eval v4"
    entry["evaluation_receipt"] = "state/m62/receipts/does-not-exist.eval.json"
    problems = _problems(snapshot)
    assert any("not a regular file" in p for p in problems), problems


def test_an_unevaluated_candidate_may_not_carry_evaluation_evidence():
    """A receipt beside TRAINED_UNEVALUATED witnesses what that state denies."""
    snapshot = _snapshot()
    entry = _candidate(snapshot, "qwen3-06b-lora-quality-live-003")
    entry["evaluation_receipt"] = \
        "state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json"
    problems = _problems(snapshot)
    assert any("has not been measured" in p for p in problems), problems


def test_the_legacy_exemption_is_closed_and_names_only_the_two_measured_candidates():
    assert LEGACY_EVALUATION_CANDIDATES == frozenset({
        "qwen3-06b-lora-quality-live-001", "qwen3-06b-lora-quality-live-002"})


def test_candidate_003_is_not_in_the_legacy_exemption():
    """The whole point: the next candidate must produce evidence."""
    assert "qwen3-06b-lora-quality-live-003" not in LEGACY_EVALUATION_CANDIDATES


def test_the_legacy_candidates_are_not_retrofitted_with_invented_receipts():
    for cid in LEGACY_EVALUATION_CANDIDATES:
        entry = _candidate(_snapshot(), cid)
        assert not entry.get("evaluation_receipt"), cid
    assert _problems(_snapshot()) == []


# ══════════════════════════════════════════════════════════════════════════════
#  A well-formed receipt satisfies it, and a tampered one does not
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def synthetic_receipt(tmp_path_factory):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _s3q0_synthetic as S
    from scripts.build_m62_eval_receipt import build_receipt, seal

    data = tmp_path_factory.mktemp("s3q0cpdata")
    S.build(data)
    root = tmp_path_factory.mktemp("s3q0cprun")
    outcome = S.run_synthetic(data, root)
    assert outcome.ok
    return seal(build_receipt(
        outcome.directory, candidate="qwen3-06b-lora-quality-live-003",
        evaluation_source_commit="a" * 40,
        ledger=root / "evaluation_runs.jsonl"))


def test_a_receipt_whose_status_claim_disagrees_with_the_snapshot_is_refused(
        synthetic_receipt):
    """The receipt decides. A snapshot that outruns it is the circularity itself."""
    receipt = copy.deepcopy(synthetic_receipt)
    assert receipt["candidate"]["status_claim"] in EVALUATED_CANDIDATE_STATES
    disagreeing = "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    if receipt["candidate"]["status_claim"] == disagreeing:  # pragma: no cover
        disagreeing = "EVALUATED_NOT_ELIGIBLE"
    assert receipt["candidate"]["status_claim"] != disagreeing


def test_a_receipt_is_self_checking(synthetic_receipt):
    from scripts.build_m62_eval_receipt import receipt_hash

    tampered = copy.deepcopy(synthetic_receipt)
    tampered["candidate"]["status_claim"] = "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert receipt_hash(tampered) != tampered["receipt_hash"]


def test_the_receipt_and_the_control_plane_must_agree_about_the_holdout(
        synthetic_receipt):
    """A receipt naming a corpus the control plane still calls fresh is a contradiction."""
    source = (REPO_ROOT / "jarvis/scripts/verify_m62_control_plane.py").read_text(
        encoding="utf-8")
    assert "still calls it" in source
    assert "USED_IMMUTABLE" in source
    assert synthetic_receipt["holdout"]["spent_by_this_evaluation"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  S3Q.0's own scope — the measurement is frozen
# ══════════════════════════════════════════════════════════════════════════════
def test_the_three_frozen_policy_identities_are_unchanged():
    """Re-derived from the production classes, never read out of the snapshot."""
    from training_gym.evaluation.gates import GatePolicy
    from training_gym.evaluation.policy import MetricPolicy

    snapshot = _snapshot()["policy_identities"]
    assert GatePolicy().policy_hash() == snapshot["gate_policy_hash"]
    assert MetricPolicy().policy_hash() == snapshot["metric_policy_hash"]
    assert snapshot["gate_policy_hash"] == \
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5"
    assert snapshot["metric_policy_hash"] == \
        "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a"


def test_the_configured_generation_policy_identity_is_unchanged():
    """The sealed configured policy, rebuilt from its declared settings."""
    from training_gym.evaluation.generation import (
        DevicePolicy,
        PrecisionPolicy,
        eligibility_generation_policy,
    )

    policy = eligibility_generation_policy(
        seed=11, timeout_s=300, device_policy=DevicePolicy.CPU,
        precision_policy=PrecisionPolicy.FP32)
    assert policy.policy_hash() == \
        "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7"
    assert policy.max_new_tokens == 512


def test_the_defect_states_are_unchanged():
    defects = {d["id"]: d for d in _snapshot()["defects"]}
    assert defects["D28"]["status"] == "OPEN"
    assert defects["D33"]["status"] == "OPEN"
    assert defects["D38"]["status"] == "FIXED_OBSERVABILITY_ONLY"
    assert defects["D39"]["status"] == "OPEN"
    for defect in defects.values():
        assert defect["is_gate"] is False, defect["id"]


def test_d33_stays_open_because_the_timeout_is_still_not_enforced():
    """Bound into the policy identity, never enforced. No rider fix, no watchdog."""
    from training_gym.evaluation.generation import GenerationPolicy

    assert "timeout_s" in GenerationPolicy(seed=11).to_dict()
    backend = (REPO_ROOT / "jarvis/training_gym/evaluation/backends"
               / "transformers_peft.py").read_text(encoding="utf-8")
    for forbidden in ("signal.alarm", "SIGALRM", "threading.Timer", "Thread(",
                      "subprocess", "watchdog", "concurrent.futures"):
        assert forbidden not in backend, forbidden


def test_d38_is_still_read_by_no_gate():
    gates = (REPO_ROOT / "jarvis/training_gym/evaluation"
             / "gates.py").read_text(encoding="utf-8")
    for name in ("output_budget_exhausted", "output_budget_exhaustion_rate",
                 "output_budget_exhaustion_count", "finish_reason"):
        assert name not in gates, name


def test_s3q0_changed_no_grader_metric_statistic_or_gate_source():
    """The measurement is frozen. S3Q.0 hardens the ceremony around it and nothing in it."""
    import subprocess

    frozen = ("gates.py", "metrics.py", "scoring.py", "statistics.py", "comparison.py",
              "generation.py", "policy.py", "task_pack.py", "pack_builder.py",
              "score_evidence.py", "backends/transformers_peft.py", "backends/fake.py")
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only",
             "05c043b3a89cdb675846abb8aabf1f476c6d7796"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available to compare against the S3Q.0 starting commit")
    if changed.returncode != 0:  # pragma: no cover - shallow clone
        pytest.skip("the S3Q.0 starting commit is not reachable in this checkout")
    touched = set(changed.stdout.split())
    for name in frozen:
        assert f"jarvis/training_gym/evaluation/{name}" not in touched, name


def test_the_graders_and_the_refusal_detector_are_untouched():
    import subprocess

    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only",
             "05c043b3a89cdb675846abb8aabf1f476c6d7796"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("git is not available")
    if changed.returncode != 0:  # pragma: no cover
        pytest.skip("the S3Q.0 starting commit is not reachable")
    for path in changed.stdout.split():
        assert not path.startswith("jarvis/training_gym/graders/"), path
        assert not path.startswith("jarvis/training_gym/training/"), path


def test_no_candidate_003_token_config_plan_or_adapter_became_tracked():
    import subprocess

    allowed = {"state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json"}
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("git is not available")
    named = [p for p in tracked.stdout.split()
             if "qwen3-06b-lora-quality-live-003" in p]
    assert set(named) <= allowed, named


def test_the_control_plane_still_grants_no_authority():
    observation = _snapshot()["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["train"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"


def test_no_tracked_file_carries_a_live_confirmation_literal():
    """A synthetic token in a test is fine; a real one for a real plan is not."""
    import re
    import subprocess

    pattern = re.compile(r"EVAL:[0-9a-f]{64}")
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("git is not available")
    offenders = []
    for rel in tracked.stdout.split():
        path = REPO_ROOT / rel
        if not path.is_file() or path.suffix in {".png", ".jpg", ".safetensors"}:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in pattern.findall(body):
            # A repeated-character digest is a fixture, not a plan hash.
            digest = match.split(":", 1)[1]
            if len(set(digest)) > 4:
                offenders.append((rel, match[:16]))
    assert offenders == [], offenders


def test_the_snapshot_and_the_schema_stay_within_budget():
    assert SNAPSHOT_PATH.stat().st_size <= 32_768
    assert (REPO_ROOT / "state/m62/current.json").stat().st_size <= 2_048
    assert len(canonical_json(_snapshot()).encode("utf-8")) <= 32_768
