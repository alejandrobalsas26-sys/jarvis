"""V69 M62 S4F — the state a SPENT eval-v7 leaves behind, asserted from durable evidence.

The S4C suite asserts what was true while the experiment was open, read from the generation
that owns it. This file asserts what is true NOW: the exam is spent, the candidate is
measured and not eligible, and nothing was promoted.

Every assertion here reads tracked evidence or the gitignored ledger the receipt binds.
Nothing in this file loads a model, constructs a backend, opens a response, spends a
holdout, or reads held-out material -- and one test proves the last of those about the file
itself.
"""
from __future__ import annotations

import json
import subprocess  # nosec B404 — fixed argv, shell=False
import sys
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))
if str(_PACKAGE_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT / "scripts"))

from scripts import verify_m62_control_plane as V  # noqa: E402
from scripts.build_m62_eval_receipt_v4 import verify_receipt_v4  # noqa: E402

REPO = V.REPO_ROOT
CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"
CANDIDATE_005_ID = "qwen3-06b-lora-quality-live-005"

EVALUATION_ID = "m62-s4e-reference-pair-live"
EVALUATION_SOURCE = "c524931dadefae77e484bdf2d75a96aa46dbc038"
#: The OUTER plan. The EVAL authority was `EVAL:<this>`, and the ledger recorded it.
V4_PLAN_HASH = "54488fb3b58457b03c4bd64daed5aacb503c02e889e9b6ef03d19c2eac67eb43"
#: The INNER plan the report published. A different number by construction, never a
#: disagreement -- the outer plan contains it, and the attempt record names both.
INNER_PLAN_HASH = "3fc3b9616968e86c9319a2d70ce49ad60dc790165eb70153e833e3ce2b5b8ef7"
REPORT_HASH = "d13fc339969b135a649af128eea33e1f8e7c409aa2a17fafc87ac0ad5708d080"

RECEIPT_PATH = f"state/m62/receipts/{CANDIDATE_005_ID}.eval.json"
WITNESS_PATH = "state/m62/witnesses/0003-s4f-live-measurement-witness.json"
OUTPUT_ROOT = _PACKAGE_ROOT / "evaluation"
ATTEMPTS = OUTPUT_ROOT / "protocol-v4-attempts.jsonl"
GENERATION_DIR = OUTPUT_ROOT / "evaluations" / EVALUATION_ID / "gen-1"

TASK_COUNT = 36
TOTAL_GENERATIONS = 72


@pytest.fixture(scope="module")
def snapshot() -> dict:
    plane = V.load(V.Report())
    assert plane is not None
    return plane.snapshot


@pytest.fixture(scope="module")
def receipt() -> dict:
    return json.loads((REPO / RECEIPT_PATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def attempt() -> dict:
    lines = [line for line in ATTEMPTS.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 1, "one paired attempt, one spend"
    return json.loads(lines[0])


# ── the attempt that already happened ────────────────────────────────────────
def test_exactly_one_paired_attempt_exists(attempt):
    assert attempt["event"] == "protocol_v4_paired_attempt"
    assert attempt["plan_hash"] == V4_PLAN_HASH
    assert attempt["evaluation_source_commit"] == EVALUATION_SOURCE
    assert attempt["holdout_spends"] == 1


def test_the_attempt_spent_seventy_two_generations(attempt):
    assert attempt["task_count"] == TASK_COUNT
    assert attempt["expected_total_generations"] == TOTAL_GENERATIONS


def test_the_outer_plan_contains_the_inner_plan_the_report_published(attempt):
    """D-S4F-2's invariant. Two plan hashes is the protocol, not a disagreement."""
    assert attempt["inner_plan_hash"] == INNER_PLAN_HASH
    assert attempt["plan_hash"] != attempt["inner_plan_hash"]


def test_both_arms_produced_thirty_six_generations_each():
    for arm in ("baseline", "candidate"):
        rows = [json.loads(line) for line
                in (GENERATION_DIR / f"{arm}-results.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == TASK_COUNT
        assert {r["status"] for r in rows} == {"succeeded"}
        assert not any(r["timed_out"] for r in rows)
        assert not any(r["input_truncated"] for r in rows)
        assert not any(r["interrupted"] for r in rows)


def test_the_report_is_the_one_the_receipt_binds(receipt):
    from training_gym.evaluation.reports import verify_report_payload

    payload = json.loads(
        (GENERATION_DIR / "evaluation-report.json").read_text(encoding="utf-8"))
    verified = verify_report_payload(payload, expected_hash=REPORT_HASH)
    assert verified["report_hash"] == REPORT_HASH
    assert receipt["evidence"]["report_hash"] == REPORT_HASH


# ── the 35/36 artefact, and that it decided nothing ──────────────────────────
def test_every_pair_was_measured_on_both_arms():
    """`measured_pairs` 35 is a quality-denominator fact, never a missing measurement."""
    from training_gym.evaluation.comparison import ComparisonVerdict

    rows = [json.loads(line) for line
            in (GENERATION_DIR / "paired-comparisons.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == TASK_COUNT
    assert {r["paired_status"] for r in rows} == {"both_measured"}
    assert all(r["baseline_reward"] is not None for r in rows)
    assert all(r["candidate_reward"] is not None for r in rows)

    blocking = [r for r in rows if not ComparisonVerdict(r["verdict"]).is_comparable]
    assert len(blocking) == 1
    report = json.loads(
        (GENERATION_DIR / "evaluation-report.json").read_text(encoding="utf-8"))
    assert report["measured_pairs"] == len(rows) - len(blocking) == 35
    assert report["missing_pairs"] == 1


def test_the_security_veto_and_not_the_coverage_label_decided_it():
    """The empirical gate never ran: security blockers return first, and did."""
    import inspect

    from training_gym.evaluation import reports as R

    src = inspect.getsource(R.decide_eligibility)
    assert src.index("gates.security_blockers") < src.index(
        "empirical.supports_eligibility")

    report = json.loads(
        (GENERATION_DIR / "evaluation-report.json").read_text(encoding="utf-8"))
    assert report["gate_report"]["security_blocking_count"] == 2
    assert report["eligibility"]["eligibility"] == "not_eligible"
    assert "security gate(s) blocked" in report["eligibility"]["rationale"]


# ── the receipt ──────────────────────────────────────────────────────────────
def test_the_receipt_verifies(receipt):
    assert verify_receipt_v4(receipt) == ()


def test_the_receipt_reseals_to_its_own_digest(receipt):
    from scripts.build_m62_eval_receipt import receipt_hash

    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    assert receipt_hash(body) == receipt["receipt_hash"]


def test_the_receipt_binds_the_authority_the_ledger_recorded(receipt):
    """D-S4F-3. A receipt naming a plan no token carried describes an authority nobody
    could have granted."""
    assert receipt["authority"]["bound_plan_hash"] == V4_PLAN_HASH
    assert receipt["ledger"]["plan_hash"] == V4_PLAN_HASH
    assert receipt["plan"]["plan_hash"] == INNER_PLAN_HASH


def test_the_receipt_names_the_evaluation_source_not_the_seal_source(receipt):
    assert receipt["evaluation_source"]["evaluation_source_commit"] == EVALUATION_SOURCE
    assert receipt["seal_implementation_source"][
        "seal_implementation_source_commit"] != EVALUATION_SOURCE
    assert receipt["seal_implementation_source"]["differs_from_evaluation_source"] is True


def test_the_receipt_records_that_sealing_measured_nothing(receipt):
    assert receipt["execution"]["model_loads_during_seal"] == 0
    assert receipt["execution"]["model_generations_during_seal"] == 0
    assert receipt["execution"]["sealed_from_existing_measurement"] is True


def test_the_receipt_is_the_verdict_and_not_a_promotion(receipt):
    assert receipt["outcome"]["eligibility"] == "not_eligible"
    assert receipt["outcome"]["human_review_required"] is True
    for flag in ("promotes_model", "activates_model", "mutates_model_registry"):
        assert receipt["outcome"][flag] is False
    assert receipt["candidate"]["status_claim"] == "EVALUATED_NOT_ELIGIBLE"


def test_the_receipt_names_no_held_out_task_identifier():
    """D-S4F-4. The blocker keeps its sentence; the identifier does not survive into a
    tracked file."""
    text = (REPO / RECEIPT_PATH).read_text(encoding="utf-8")
    for version, task_ids in V.HELD_OUT_TASK_IDS.items():
        named = sorted(tid for tid in task_ids if tid in text)
        assert not named, f"the receipt names eval-{version} task(s) {named[:4]}"
    assert "redacted" in text
    blockers = json.loads(text)["outcome"]["gate_blockers"]
    assert any("secret_pii" in b for b in blockers), "the reason must survive redaction"


# ── the witness ──────────────────────────────────────────────────────────────
def test_the_witness_commit_first_parent_is_the_evaluation_source():
    done = subprocess.run(  # nosec B603 — fixed argv, shell=False
        ["git", "-C", str(REPO), "rev-list", "--max-count=1", "HEAD", "--", WITNESS_PATH],
        capture_output=True, text=True, check=False)
    assert done.returncode == 0
    commit = done.stdout.strip()
    parents = subprocess.run(  # nosec B603 — fixed argv, shell=False
        ["git", "-C", str(REPO), "rev-list", "--parents", "-n", "1", commit],
        capture_output=True, text=True, check=False).stdout.split()
    assert parents[1] == EVALUATION_SOURCE


def test_the_witness_grants_nothing():
    witness = json.loads((REPO / WITNESS_PATH).read_text(encoding="utf-8"))
    for flag in ("candidate_state", "promotion", "activation", "registry_mutation",
                 "retry_or_rerun", "is_an_evaluation_receipt"):
        assert witness["grants"][flag] is False


# ── the control plane ────────────────────────────────────────────────────────
def test_eval_v7_is_spent_and_names_its_spender(snapshot):
    v7 = next(d for d in snapshot["datasets"]
              if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v7")
    assert v7["status"] == "USED_IMMUTABLE"
    assert v7["spent_by"] is not None
    assert EVALUATION_ID in v7["spent_by"]
    assert V4_PLAN_HASH[:8] in v7["spent_by"]
    assert v7["task_count"] == TASK_COUNT


def test_the_control_plane_spender_is_the_attempt_the_ledger_recorded(snapshot, attempt):
    v7 = next(d for d in snapshot["datasets"]
              if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v7")
    assert attempt["dataset_version"] == "v7"
    assert attempt["task_pack_hash"] == v7["pack_hash"]
    assert attempt["dataset_manifest_hash"] == v7["manifest_hash"]


def test_candidate_005_is_evaluated_and_not_eligible(snapshot):
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == CANDIDATE_005_ID)
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["evaluation_corpus"] == "m62-defensive-eval v7"
    assert entry["evaluation_receipt"] == RECEIPT_PATH


def test_candidate_004_is_still_held_and_still_not_promoted(snapshot):
    """Serving as the REFERENCE arm reopens nothing, and 005 failing promotes nobody."""
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == CANDIDATE_004_ID)
    assert entry["status"] == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert entry["evaluation_corpus"] == "m62-defensive-eval v6"


def test_no_candidate_is_promoted(snapshot):
    for entry in snapshot["candidates"]:
        assert entry["status"] != "PROMOTED"
        assert "PROMOTED" not in str(entry.get("status"))


def test_no_authority_is_observed(snapshot):
    observation = snapshot["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for kind in ("train", "eval", "promotion"):
        assert observation[kind] == "NONE_OBSERVED_IN_REPOSITORY"


def test_the_generation_is_twenty_seven_and_chains_to_twenty_six(snapshot):
    """RESCOPED AT S4H: read generation 27 itself, not the live pointer.

    What S4F recorded is that IT wrote generation 27 and that 27 chains to 26, and that is
    permanent. Reading `current.json` also asserted, silently, that no LATER generation
    exists — which was true by coincidence until a later milestone wrote one, and is a
    property of the experiment being open rather than of the seal. S4H wrote generation 28,
    which changes nothing S4F claimed. Same precedent as the S4D freeze test S4F itself
    rescoped; the live pointer's own chain is checked by the control-plane verifier and by
    test_training_gym_m62_s4h_control_plane.py.
    """
    stored = json.loads(
        (REPO / V.SNAPSHOT_DIR / "0027-m62-s4f-eval-v7-spent.json").read_text("utf-8"))
    assert stored["state_generation"] == 27
    parent = (REPO / V.SNAPSHOT_DIR / "0026-m62-s4e-exec.json").read_bytes()
    assert stored["parent_snapshot_sha256"] == V.sha256_bytes(parent)


# ── the firewall ─────────────────────────────────────────────────────────────
def test_a_second_spend_of_eval_v7_is_refused(attempt):
    """The record and the mechanism agree: the exam is single-use and already used."""
    from scripts.build_m62_eval_receipt import ReceiptError  # noqa: F401
    from training_gym.evaluation.store_v4 import (
        HoldoutAlreadyCommitted,
        assert_holdout_never_spent,
    )

    with pytest.raises(HoldoutAlreadyCommitted):
        assert_holdout_never_spent(
            OUTPUT_ROOT, dataset_id="m62-defensive-eval", dataset_version="v7",
            task_pack_hash=attempt["task_pack_hash"])


def test_this_suite_names_no_held_out_task_identifier():
    """The file asserting the firewall is held to it."""
    text = Path(__file__).read_text(encoding="utf-8")
    for version, task_ids in V.HELD_OUT_TASK_IDS.items():
        named = sorted(tid for tid in task_ids if tid in text)
        assert not named, f"this suite names eval-{version} task(s) {named[:4]}"
