"""V69 M62 S3X.1 — generation 13: a FRESH eval-v6 frozen, and nothing measured.

Generation 12 recorded the D44 recovery and left the repository with no usable eligibility
holdout: ``eval-v4`` spent, ``eval-v5`` frozen-but-RETIRED, and ``FRESH_V6_REQUIRED`` on
the invariant surface. Generation 13 records that the replacement exists.

What it must NOT record is the thing a reader is most likely to assume follows a freeze.
Candidate 004 is still ``TRAINED_UNEVALUATED`` with both evaluation fields null, no EVAL
authority exists, no holdout is spent, and no evaluation receipt was written. A corpus was
created; nothing was measured against it. These tests assert both halves, because the
failure mode of this milestone is a state that quietly reads as an evaluation.

They also assert the two capacity facts the generation depended on: that it cleared the
1 024-byte headroom floor without the budget being raised, and that the lossless compaction
which made room really was lossless.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS OR GENERATES A TOKEN.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("scripts.build_training_corpus")
from scripts import project_m62_gen13_capacity as CAP  # noqa: E402

EVAL_V5_MANIFEST = (
    "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c")
EVAL_V6_MANIFEST = (
    "413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6")
EVAL_V6_PACK = (
    "41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16")
GATE_POLICY_HASH = (
    "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
METRIC_POLICY_HASH = (
    "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")
GENERATION_POLICY_HASH = (
    "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")

CANDIDATE_004 = "qwen3-06b-lora-quality-live-004"


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


#: RESCOPED AT S3Y. Every assertion in this file is about the generation S3X.1 WROTE.
#: Reading it through `current.json` silently added a second claim -- that no later
#: generation exists -- which S3Y's authorised spend made false. Pinned to the sealed
#: path, per the rescoping pattern S3Q.0 established.
GEN13_SNAPSHOT_PATH = "state/m62/snapshots/0013-m62-s3x1-fresh-eval-v6-frozen.json"


def _snapshot():
    return json.loads((_repo_root() / GEN13_SNAPSHOT_PATH).read_text("utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  42-50. The control plane: what S3X.1 recorded, and what it did NOT
# ══════════════════════════════════════════════════════════════════════════════
def test_the_control_plane_records_v6_exactly_once_frozen_and_unspent():
    entries = [d for d in _snapshot()["datasets"]
               if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v6"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None
    assert entry["role"] == "EVALUATION_HOLDOUT"
    assert entry["task_count"] == 36
    assert entry["manifest_hash"] == EVAL_V6_MANIFEST
    assert entry["pack_hash"] == EVAL_V6_PACK
    assert entry["parent_manifest_hash"] == EVAL_V5_MANIFEST


def test_v5_is_still_frozen_unspent_and_retired_from_eligibility_use():
    """Creating v6 does not rehabilitate v5, and does not forge a spend into it either."""
    snapshot = _snapshot()
    entries = [d for d in snapshot["datasets"]
               if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v5"]
    assert len(entries) == 1
    assert entries[0]["status"] == "FROZEN_UNUSED"
    assert entries[0]["spent_by"] is None
    invariants = " ".join(snapshot["frozen_invariants"])
    assert "ELIGIBILITY_USE: RETIRED" in invariants
    assert "FRESH_V6_REQUIRED" in invariants
    assert "NO MODEL EVER SAW IT" in invariants


def test_no_candidate_names_the_retired_holdout_as_its_evaluation_corpus():
    for candidate in _snapshot()["candidates"]:
        assert candidate["evaluation_corpus"] != "m62-defensive-eval v5"


def test_candidate_004_is_still_trained_and_unevaluated():
    """A freeze measures nothing. Both evaluation fields stay null."""
    entry = next(c for c in _snapshot()["candidates"]
                 if c["candidate_id"] == CANDIDATE_004)
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["evaluation_corpus"] is None
    assert entry["evaluation_receipt"] is None
    assert entry["ordinal"] == 4


def test_the_candidate_004_evaluation_receipt_records_the_v6_spend():
    """RESCOPED AT S3Y. At S3X.1 no evaluation receipt existed and that was the point:
    the milestone froze a corpus and measured nothing. S3Y then spent `eval-v6` under one
    human EVAL authority. Both receipts are now real and neither may be removed; what
    stays guarded is that the spend names v6 and never the retired v5."""
    import json as _json
    receipts = _repo_root() / "state/m62/receipts"
    assert (receipts / f"{CANDIDATE_004}.train.json").exists(), (
        "the TRAINING receipt is real and must not be removed by this milestone")
    eval_path = receipts / f"{CANDIDATE_004}.eval.json"
    assert eval_path.exists(), "S3Y sealed this receipt; it may not be removed"
    assert _json.loads(eval_path.read_text("utf-8"))["holdout"]["dataset_version"] == "v6"


def test_no_eval_or_promotion_authority_is_observed_in_the_repository():
    observation = _snapshot()["authority_observation"]
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["train"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["control_plane_can_grant_authority"] is False


def test_no_holdout_is_recorded_as_spent_by_candidate_004():
    """v5 and v6 are the two a confused session could reach for. Neither moved.

    Matching on the bare string ``004`` would be wrong, not merely loose: eval-v1's
    ``spent_by`` is ``S3E.2, run-004 smoke evaluation``, where ``run-004`` is an S3E.2 RUN
    ordinal that has nothing to do with the fourth candidate. The claim under test is that
    no holdout names CANDIDATE 004 as having spent it, so that is what is asserted.
    """
    for entry in _snapshot()["datasets"]:
        spent = str(entry["spent_by"] or "")
        assert "candidate 004" not in spent.lower()
        assert CANDIDATE_004 not in spent
    for version in ("v5", "v6"):
        entry = next(d for d in _snapshot()["datasets"]
                     if d["dataset_id"] == "m62-defensive-eval" and d["version"] == version)
        assert entry["spent_by"] is None
        assert entry["status"] == "FROZEN_UNUSED"


def test_the_snapshot_is_generation_thirteen_and_descends_from_generation_twelve():
    snapshot = _snapshot()
    assert snapshot["state_generation"] == 13
    assert snapshot["subject_state_milestone"] == "S3X.1"
    assert snapshot["parent_snapshot_sha256"] == CAP.EXPECTED_PARENT_SHA256


def test_the_next_milestone_requires_a_new_session_and_grants_no_authority():
    nxt = _snapshot()["next_milestone"]
    assert nxt["requires_new_session"] is True
    joined = " | ".join(nxt["ruled_out"])
    assert "eval-v5" in joined
    assert "eval-v6 authoring session evaluating candidate 004" in joined
    for required in nxt["authority_required"]:
        assert "eval-v5" not in required


# ══════════════════════════════════════════════════════════════════════════════
#  51-54. Capacity, and the compaction that made room
# ══════════════════════════════════════════════════════════════════════════════
def test_the_projected_generation_thirteen_cleared_its_capacity_gate():
    """The transform that PROJECTED the snapshot is the one that emitted it."""
    from scripts.verify_m62_control_plane import SNAPSHOT_MAX_BYTES, canonical_bytes

    snapshot = _snapshot()
    size = len(canonical_bytes(snapshot))
    assert size <= SNAPSHOT_MAX_BYTES
    assert SNAPSHOT_MAX_BYTES - size >= CAP.REQUIRED_HEADROOM_BYTES


def test_the_snapshot_budget_was_not_raised_to_make_room():
    from scripts.verify_m62_control_plane import (
        PROGRESS_MAX_BYTES,
        PROGRESS_MAX_LINES,
        SNAPSHOT_MAX_BYTES,
    )

    assert SNAPSHOT_MAX_BYTES == 34_816
    assert PROGRESS_MAX_BYTES == 40_960
    assert PROGRESS_MAX_LINES == 760


def test_every_carried_forward_clause_survived_the_compaction():
    """Lossless is a measurement here, not a claim in a commit message."""
    assert CAP.check_carried_forward(_snapshot()) == []
    assert len(CAP.CARRIED_FORWARD) >= 30


def test_the_compaction_check_is_not_vacuous():
    """A carry-forward check that cannot fail would certify anything."""
    mutated = json.loads(json.dumps(_snapshot()))
    mutated["frozen_invariants"] = [
        i for i in mutated["frozen_invariants"] if "FRESH_V6_REQUIRED" not in i]
    assert CAP.check_carried_forward(mutated)


def test_progress_still_clears_its_byte_and_line_budgets():
    from scripts.verify_m62_control_plane import PROGRESS_MAX_BYTES, PROGRESS_MAX_LINES

    raw = (_repo_root() / "PROGRESS.md").read_bytes()
    assert len(raw) <= PROGRESS_MAX_BYTES
    assert raw.count(b"\n") <= PROGRESS_MAX_LINES
    assert PROGRESS_MAX_BYTES - len(raw) > 0
    assert PROGRESS_MAX_LINES - raw.count(b"\n") > 0

def test_the_recorded_policy_identities_still_match_production():
    snapshot = _snapshot()
    identities = snapshot["policy_identities"]
    assert identities["gate_policy_hash"] == GATE_POLICY_HASH
    assert identities["metric_policy_hash"] == METRIC_POLICY_HASH
    assert identities["generation_policy_hash"] == GENERATION_POLICY_HASH
    assert identities["max_new_tokens"] == 512
    assert identities["reasoning_policy"] == "DISABLED"


def test_no_d38_gate_was_added_while_a_corpus_was_being_frozen():
    """D38 is FIXED_OBSERVABILITY_ONLY. No gate reads it, and none may be added here."""
    snapshot = _snapshot()
    d38 = next(d for d in snapshot["defects"] if d["id"] == "D38")
    assert d38["status"] == "FIXED_OBSERVABILITY_ONLY"
    assert d38["is_gate"] is False

def test_d44_is_still_recorded_fixed_and_is_still_a_gate():
    d44 = next(d for d in _snapshot()["defects"] if d["id"] == "D44")
    assert d44["status"] == "FIXED"
    assert d44["is_gate"] is True

def test_the_body_blindness_invariant_still_names_every_disclosure_route():
    invariants = " ".join(_snapshot()["frozen_invariants"])
    assert "ORCHESTRATOR BODY-BLINDNESS IS A GATE" in invariants
    assert "repr of a bound method" in invariants
