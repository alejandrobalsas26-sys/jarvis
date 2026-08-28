"""V69 M62 S3X.0 Phase-B — the recovery state, and the rules that hold it in place.

S3X.0 recorded three things a later session could quietly undo, so each one is an
executable constraint here rather than a sentence in a document:

  1. the reviewed snapshot budget moved ``32_768 -> 34_816`` by operator ruling, and the
     duplicated literals that name it must not drift apart;
  2. ``eval-v5`` is RETIRED FROM ELIGIBILITY USE while remaining ``FROZEN_UNUSED`` with
     ``spent_by`` null, because no model ever saw it -- two facts that a future session
     could collapse in either direction;
  3. candidate 004 is ``TRAINED_UNEVALUATED`` and stays that way.

Every assertion is BODY-FREE. Nothing here reads, names or reconstructs a held-out task
body; the retirement is tested through the control-plane record and the verifier alone.
The D44 representation fix has its own suite in
``test_training_gym_m62_s3x0_repr_body_blindness.py`` and is not duplicated here.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from scripts import verify_m62_control_plane as V

REPO = Path(__file__).resolve().parents[2]
CANDIDATE = "qwen3-06b-lora-quality-live-004"
#: eval-v5: retired, never model-spent. A candidate-004 receipt may never name it.
EVAL_V5_MANIFEST = "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c"
RETIRED = ("m62-defensive-eval", "v5")
RETIRED_LABEL = "m62-defensive-eval v5"

#: The generation the S3X.0 ruling took effect at. Everything older is history and stays
#: byte-exact; the retirement is prospective from here.
RETIREMENT_GENERATION = 12


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def current() -> dict:
    return json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshot(current) -> dict:
    return json.loads((REPO / current["latest_snapshot_path"]).read_text(encoding="utf-8"))


#: Generation 12 BY PATH, not by following ``current.json``.
#:
#: What S3X.0 sealed is a fact about ONE snapshot: that generation 12 is the milestone
#: which retired eval-v5. Reading that through the current pointer also asserts, silently,
#: that no later generation exists -- true by coincidence until S3X.1 wrote generation 13.
#: The sealed half is therefore read from the sealed artefact, and the live pointer is
#: checked separately for the property it actually owns: that it has not regressed behind
#: the retirement.
RECOVERY_SNAPSHOT_PATH = (
    "state/m62/snapshots/0012-m62-s3x0-holdout-firewall-recovery.json")


@pytest.fixture(scope="module")
def recovery_snapshot() -> dict:
    return json.loads((REPO / RECOVERY_SNAPSHOT_PATH).read_text(encoding="utf-8"))


def _plane(snapshot: dict) -> V.ControlPlane:
    """A ControlPlane carrying *snapshot*. Only the snapshot is read by the check."""
    raw = V.canonical_bytes(snapshot)
    return V.ControlPlane(
        current={}, current_bytes=b"", snapshot=snapshot, snapshot_bytes=raw,
        snapshot_path=REPO / "state/m62/snapshots/unused.json", migration={})


def _retirement_problems(snapshot: dict) -> list[str]:
    report = V.Report()
    V.check_holdout_retirement(_plane(snapshot), report)
    return [message for category, message in report.problems]


def _mutated(snapshot: dict) -> dict:
    return copy.deepcopy(snapshot)


# ══════════════════════════════════════════════════════════════════════════════
#  1. The reviewed snapshot budget moved, coherently, and only it
# ══════════════════════════════════════════════════════════════════════════════
def test_the_reviewed_snapshot_budget_is_the_migrated_one():
    assert V.SNAPSHOT_MAX_BYTES == 34_816


def test_the_history_index_budget_did_not_move_with_it():
    """It shared 32 KiB with the snapshot by coincidence, never by derivation."""
    assert V.HISTORY_INDEX_MAX_BYTES == 32_768


def test_the_progress_budget_did_not_move():
    assert V.PROGRESS_MAX_BYTES == 40_960
    assert V.PROGRESS_MAX_LINES == 760
    assert V.CURRENT_MAX_BYTES == 2_048


def test_no_m62_surface_still_names_the_superseded_snapshot_budget():
    """The failure this catches is a *partial* migration.

    A budget that lives in one constant and three test literals is migrated correctly or
    it is migrated in four places that disagree, and the disagreement is silent: the
    emitter would accept a snapshot the pinning test rejects. Rather than trusting that
    every site was found by hand, the tree is asked.
    """
    out = subprocess.run(
        ["git", "grep", "-n", "-E", r"32_768|32768"],
        cwd=REPO, capture_output=True, text=True, check=False).stdout
    offenders = []
    for line in out.splitlines():
        path, _, text = line.partition(":")
        if not (path.startswith("jarvis/scripts/") or path.startswith("jarvis/tests/")):
            continue
        # The ONE legitimate survivor, and it says so in a comment above itself.
        if "HISTORY_INDEX_MAX_BYTES" in text:
            continue
        # This file is the CHECKER. Naming the superseded number inside the check that
        # forbids it is hygiene, not a partial migration -- the same shape as the
        # verifier's H4 ruling about naming a forbidden token inside its own scanner.
        if path == "jarvis/tests/" + Path(__file__).name:
            continue
        offenders.append(line)
    assert offenders == [], (
        "these surfaces still name 32,768 after the S3X.0 snapshot-budget migration; a "
        f"duplicated budget that drifts is worse than no budget: {offenders}")


def test_every_reviewed_value_pin_names_the_same_number_as_the_constant():
    """The explicit pins are deliberate duplicates, so they are proved equal here.

    They exist to make a SILENT raise impossible -- binding them to the constant they
    guard would defeat their whole purpose -- so the coherence they cannot get from
    architecture is asserted instead.
    """
    pins = {
        "jarvis/tests/test_training_gym_m62_s3u_control_plane.py":
            "assert V.SNAPSHOT_MAX_BYTES == 34_816",
        "jarvis/tests/test_training_gym_m62_s3w0_eval_qualification.py":
            "assert V.SNAPSHOT_MAX_BYTES == 34_816",
    }
    for rel, expected in pins.items():
        text = (REPO / rel).read_text(encoding="utf-8")
        assert expected in text, f"{rel} no longer pins the reviewed budget as {expected!r}"


def test_the_live_snapshot_fits_the_budget_with_the_required_headroom(current):
    raw = (REPO / current["latest_snapshot_path"]).read_bytes()
    headroom = V.SNAPSHOT_MAX_BYTES - len(raw)
    assert len(raw) <= V.SNAPSHOT_MAX_BYTES
    assert headroom >= 1_024, (
        f"{len(raw)} bytes leaves {headroom}; the reviewed policy requires 1,024 spare so "
        f"the next truthful generation does not need another migration")


def test_progress_keeps_both_of_its_budgets(current):
    text = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= V.PROGRESS_MAX_BYTES
    assert V.PROGRESS_MAX_LINES >= text.count("\n") + 150


# ══════════════════════════════════════════════════════════════════════════════
#  2. The recovery generation says what it must
# ══════════════════════════════════════════════════════════════════════════════
def test_the_recovery_generation_is_sealed_and_still_binds(recovery_snapshot, snapshot):
    """RESCOPED at S3X.1, which wrote generation 13.

    The sealed claim -- generation 12 is the S3X.0 recovery -- is asserted against
    generation 12 itself, pinned by path, so it stays true no matter how far the control
    plane advances. The live pointer is asserted only for what it owns: it may move
    forward, and it may never regress behind the generation the retirement took effect at.
    """
    assert recovery_snapshot["state_generation"] == RETIREMENT_GENERATION
    assert recovery_snapshot["subject_state_milestone"] == "S3X.0"
    assert snapshot["state_generation"] >= RETIREMENT_GENERATION


def test_candidate_004_was_trained_and_unevaluated_before_the_spend():
    """PINNED AT S3Y to generation 13, BY PATH -- the same reasoning the generation-12 pin
    above records. The claim is permanently true of the last pre-spend generation."""
    snapshot = json.loads(
        (REPO / "state/m62/snapshots/0013-m62-s3x1-fresh-eval-v6-frozen.json").read_text(encoding="utf-8"))
    entry = next(c for c in snapshot["candidates"] if c["candidate_id"] == CANDIDATE)
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["evaluation_corpus"] is None
    assert entry["evaluation_receipt"] is None
    assert entry["training_receipt"] == f"state/m62/receipts/{CANDIDATE}.train.json"


def test_the_candidate_004_evaluation_receipt_names_v6_and_never_v5():
    """RESCOPED AT S3Y. This asserted no evaluation receipt existed, which was true while
    none did. What it was protecting is that a receipt must never mean `eval-v5` was
    spent. S3Y spent `eval-v6` under one human EVAL authority, so the receipt exists and
    the protection is asserted directly instead of by absence."""
    import json as _json
    path = REPO / f"state/m62/receipts/{CANDIDATE}.eval.json"
    assert path.exists(), "S3Y sealed this receipt; it may not be removed"
    receipt = _json.loads(path.read_text("utf-8"))
    assert receipt["holdout"]["dataset_version"] == "v6"
    assert receipt["holdout"]["dataset_manifest_hash"] != EVAL_V5_MANIFEST


def test_eval_v5_keeps_its_lifecycle_truth(snapshot):
    """Never model-spent. The eligibility ruling may not be written in as a spend."""
    entry = next(d for d in snapshot["datasets"]
                 if (d["dataset_id"], d["version"]) == RETIRED)
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None


def test_the_snapshot_carries_the_retirement_and_the_replacement_requirement(snapshot):
    invariants = " ".join(snapshot["frozen_invariants"])
    assert V.RETIREMENT_MARKER in invariants
    assert V.FRESH_HOLDOUT_MARKER in invariants


def test_d44_is_a_fixed_gate_defect(snapshot):
    d44 = next(d for d in snapshot["defects"] if d["id"] == "D44")
    assert d44["status"] == "FIXED"
    assert d44["is_gate"] is True
    assert d44["evidence"].endswith(".md")
    # The distinction the defect exists to record: one firewall held, the other was absent.
    assert "PERSISTENCE" in d44["summary"]
    assert "ORCHESTRATION-DISPLAY" in d44["summary"]


def test_the_snapshot_grants_no_authority(snapshot):
    observation = snapshot["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for role in ("train", "eval", "promotion"):
        assert observation[role] in V.AUTHORITY_OBSERVATIONS


def test_the_recovery_generation_is_clean_against_the_retirement_rule(snapshot):
    assert _retirement_problems(snapshot) == []


# ══════════════════════════════════════════════════════════════════════════════
#  3. Non-vacuity: each way of un-retiring eval-v5 is refused
# ══════════════════════════════════════════════════════════════════════════════
def _dataset(snapshot: dict) -> dict:
    return next(d for d in snapshot["datasets"]
                if (d["dataset_id"], d["version"]) == RETIRED)


def test_claiming_v5_was_model_spent_is_refused(snapshot):
    mutant = _mutated(snapshot)
    _dataset(mutant)["status"] = "USED_IMMUTABLE"
    _dataset(mutant)["spent_by"] = "S3W.1, candidate 004"
    assert _retirement_problems(mutant), "a fabricated holdout spend was accepted"


def test_setting_spent_by_without_evidence_is_refused(snapshot):
    mutant = _mutated(snapshot)
    _dataset(mutant)["spent_by"] = "S3X.0 orchestrator exposure"
    problems = _retirement_problems(mutant)
    assert problems, "spent_by was moved off null with nothing to support it"
    assert any("NEVER model-spent" in p for p in problems)


def test_dropping_the_retirement_invariant_is_refused(snapshot):
    mutant = _mutated(snapshot)
    mutant["frozen_invariants"] = [s for s in mutant["frozen_invariants"]
                                   if V.RETIREMENT_MARKER not in s]
    assert _retirement_problems(mutant), "the retirement was dropped rather than superseded"


def test_dropping_the_fresh_holdout_requirement_is_refused(snapshot):
    mutant = _mutated(snapshot)
    mutant["frozen_invariants"] = [s for s in mutant["frozen_invariants"]
                                   if V.FRESH_HOLDOUT_MARKER not in s]
    assert _retirement_problems(mutant), "FRESH_V6_REQUIRED was dropped"


def test_nominating_v5_as_candidate_004s_holdout_is_refused(snapshot):
    mutant = _mutated(snapshot)
    entry = next(c for c in mutant["candidates"] if c["candidate_id"] == CANDIDATE)
    entry["evaluation_corpus"] = RETIRED_LABEL
    problems = _retirement_problems(mutant)
    assert any(CANDIDATE in p for p in problems)


def test_presenting_v5_as_fresh_eligibility_evidence_is_refused(snapshot):
    """The next milestone may name it, but only while saying it is retired."""
    mutant = _mutated(snapshot)
    mutant["next_milestone"]["evaluation_holdout"] = (
        "m62-defensive-eval v5, FROZEN_UNUSED and fresh, to be spent once on candidate 004")
    problems = _retirement_problems(mutant)
    assert any("without saying it is RETIRED" in p for p in problems)


def test_requesting_eval_authority_for_v5_is_refused(snapshot):
    mutant = _mutated(snapshot)
    mutant["next_milestone"]["authority_required"] = [
        "a fresh single-use human EVAL authority bound to a plan spending eval-v5"]
    problems = _retirement_problems(mutant)
    assert any("authority_required" in p for p in problems)


def test_dropping_the_prohibition_from_the_next_session_is_refused(snapshot):
    mutant = _mutated(snapshot)
    mutant["next_milestone"]["ruled_out"] = [
        rule for rule in mutant["next_milestone"]["ruled_out"]
        if "eval-v5" not in rule and "m62-defensive-eval v5" not in rule]
    problems = _retirement_problems(mutant)
    assert any("ruled_out" in p for p in problems)


def test_the_mutation_probe_is_not_vacuous(snapshot):
    """The eight mutations above matter only if the unmutated state passes."""
    assert _retirement_problems(_mutated(snapshot)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  4. The retirement is PROSPECTIVE: history is not rewritten
# ══════════════════════════════════════════════════════════════════════════════
HISTORICAL = (
    "0007-m62-s3s-eval-v5-frozen.json",
    "0008-m62-s3t0-termination-observability.json",
    "0009-m62-s3u-candidate004-designed.json",
    "0010-m62-s3v-candidate004-trained.json",
    "0011-m62-s3w0-candidate004-eval-ready.json",
)


@pytest.mark.parametrize("name", HISTORICAL)
def test_generations_7_to_11_are_byte_exact_against_git(name):
    rel = f"state/m62/snapshots/{name}"
    committed = subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=REPO, capture_output=True, check=True).stdout
    assert (REPO / rel).read_bytes() == committed, (
        f"{name} was edited; a superseded snapshot is never revised, and the S3X.0 "
        f"retirement is prospective rather than a rewrite of history")


@pytest.mark.parametrize("name", HISTORICAL)
def test_historical_generations_may_still_call_v5_frozen_unused(name):
    """They were true when written, and the check must not reach back and fail them."""
    snapshot = json.loads((REPO / f"state/m62/snapshots/{name}").read_text("utf-8"))
    assert snapshot["state_generation"] < RETIREMENT_GENERATION
    entry = next(d for d in snapshot["datasets"]
                 if (d["dataset_id"], d["version"]) == RETIRED)
    assert entry["status"] == "FROZEN_UNUSED" and entry["spent_by"] is None
    assert _retirement_problems(snapshot) == []


def test_a_pre_ruling_generation_is_not_required_to_carry_the_invariant():
    """Non-vacuity for the prospectivity itself.

    Strip the retirement invariant from generation 11 and the check must STILL pass: if it
    failed, the rule would be retroactive and every sealed generation would become invalid.
    """
    snapshot = json.loads(
        (REPO / "state/m62/snapshots/0011-m62-s3w0-candidate004-eval-ready.json")
        .read_text("utf-8"))
    snapshot["frozen_invariants"] = []
    snapshot["next_milestone"]["ruled_out"] = ["nothing"]
    assert _retirement_problems(snapshot) == []


# ══════════════════════════════════════════════════════════════════════════════
#  5. This suite reads no held-out material
# ══════════════════════════════════════════════════════════════════════════════
def test_this_suite_names_no_holdout_body_source():
    """Assembled at runtime, never written literally.

    A scan that spells out what it forbids matches itself -- the same shape as the
    verifier's own H4 hygiene ruling. Building the names from fragments keeps the check
    honest instead of exempting the line that performs it.
    """
    text = Path(__file__).read_text(encoding="utf-8")
    ext = "." + "jsonl"
    for stem in ("hidden_" + "evaluation", "adver" + "sarial",
                 "security_" + "regression", "task-" + "pack"):
        assert stem + ext not in text
