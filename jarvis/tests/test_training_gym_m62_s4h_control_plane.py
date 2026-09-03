"""V69 M62 S4H — generation 28, re-derived, and the mutations the verifier must refuse.

WHAT THESE TESTS ARE FOR
------------------------
A control plane can make a claim about itself and then believe it. Generation 28 says a
milestone about instruments changed nothing about the science, which is exactly the kind
of claim that is cheap to write and expensive to be wrong about. So every part of it is
re-derived here from something other than the snapshot: the candidate ledger from the
record store, the instrument versions from the production modules, the scientific suite
from its own manifest.

The second half is non-vacuity. Six mutations — candidate 005's status, eval-v7's state,
a moved instrument version, an unanchored slot, a narrowed scientific suite, a historical
scorer reaching for the new instruments — each must make a verifier check FAIL. A control
plane that passes after candidate 005 is marked eligible is not a control plane.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "jarvis"
STATE = REPO_ROOT / "state" / "m62"

sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture(scope="module")
def V():
    """The verifier itself, imported the way the rest of this suite imports it."""
    from scripts import verify_m62_control_plane
    return verify_m62_control_plane


@pytest.fixture
def pointer() -> dict:
    return json.loads((STATE / "current.json").read_text(encoding="utf-8"))


#: The generation this file owns, addressed by PATH rather than through the live
#: pointer.
#:
#: V69 M65A rescoping. Reading `current.json` made every assertion here silently
#: also assert "and no later generation exists" — true by coincidence until the
#: next milestone wrote one, which M65A did (generation 29, governance-only). The
#: properties this file was written to protect belong to generation 28, so it now
#: reads generation 28; that is the precedent S3N, S3S, S3X.1, S4D and S4F each
#: set, and it is argued in V69_M65A_SPECIALIST_EXECUTION_CORE.md.
#:
#: No assertion is weakened by this. Every check below still re-derives S4H's
#: claims from the record store, the production modules and the suite manifest —
#: against the snapshot that made them.
GENERATION_28 = "state/m62/snapshots/0028-m62-s4h-instrument-hardening.json"


@pytest.fixture
def stored() -> dict:
    return json.loads((REPO_ROOT / GENERATION_28).read_text(encoding="utf-8"))


def record(stored: dict, block: str):
    digest = stored["records"][block]
    return json.loads((STATE / "records" / f"{digest}.json").read_text(
        encoding="utf-8"))["value"]


# ══════════════════════════════════════════════════════════════════════════════
#  §69 THE SUCCESSOR IS APPEND-ONLY AND TRUTHFUL
# ══════════════════════════════════════════════════════════════════════════════
def test_generation_28_is_still_present_and_the_pointer_never_regresses(pointer):
    """What S4H can honestly assert about the LIVE pointer.

    Not "the pointer still names 28" — a successor generation is a normal event
    and would make that a false alarm rather than a finding. What must stay true
    is that generation 28 still exists where it was written, that the schema did
    not change under it, and that the chain only ever moves forward.
    """
    assert (REPO_ROOT / GENERATION_28).is_file()
    assert pointer["schema_version"] == "m62.control_plane.3"
    assert pointer["state_generation"] >= 28


def test_the_pointer_digest_is_the_snapshot_s_own_bytes(pointer, V):
    path = REPO_ROOT / pointer["latest_snapshot_path"]
    assert V.sha256_bytes(path.read_bytes()) == pointer["latest_snapshot_sha256"]


def test_generation_28_hashes_to_what_its_successor_recorded(V):
    """Append-only, checked from the OTHER side: whatever generation follows 28
    must name 28's real bytes as its parent. This is what stops a rescoped file
    from quietly losing the immutability it was protecting."""
    import glob

    successors = sorted(glob.glob(str(STATE / "snapshots" / "00*.json")))
    gen28_digest = V.sha256_bytes((REPO_ROOT / GENERATION_28).read_bytes())
    for path in successors:
        snap = json.loads(Path(path).read_text(encoding="utf-8"))
        if snap["state_generation"] == 29:
            assert snap["parent_snapshot_sha256"] == gen28_digest


def test_generation_28_descends_from_generation_27(stored, V):
    parent = STATE / "snapshots" / "0027-m62-s4f-eval-v7-spent.json"
    assert stored["parent_snapshot_sha256"] == V.sha256_bytes(parent.read_bytes())
    assert stored["state_generation"] == 28
    assert stored["subject_state_milestone"] == "S4H"


def test_generation_27_is_unmodified_by_this_milestone(V):
    """Append-only: the parent must still hash to what generation 28 says it does."""
    parent = STATE / "snapshots" / "0027-m62-s4f-eval-v7-spent.json"
    assert V.sha256_bytes(parent.read_bytes()) == (
        "bcf54ae6263a5c362e874a6e5f0e2a8a0c6f527e3db7809bf75409380559e736")


def test_the_snapshot_declares_the_s4h_branch_and_leaves_master_alone(stored):
    project = stored["project"]
    assert project["branch"] == "jarvis-v69-s4h-eval-instrument-hardening"
    assert project["master_commit"] == "3705114228edef2f665be349c5c4429b7b16777a"
    assert project["merged_into_master"] is False
    assert project["tagged"] is False and project["released"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  §70 WHAT IT MAY AND MAY NOT CLAIM
# ══════════════════════════════════════════════════════════════════════════════
def test_generation_28_preserves_every_candidate_and_holdout_state(stored):
    candidates = {c["candidate_id"]: c["status"] for c in record(stored, "candidates")}
    assert candidates["qwen3-06b-lora-quality-live-004"] == (
        "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW")
    assert candidates["qwen3-06b-lora-quality-live-005"] == "EVALUATED_NOT_ELIGIBLE"
    assert len(candidates) == 5
    v7 = [d for d in record(stored, "datasets")
          if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v7"]
    assert v7[0]["status"] == "USED_IMMUTABLE"
    assert v7[0]["spent_by"].startswith("S4E LIVE")


def test_the_candidate_and_dataset_records_are_the_ones_generation_27_carried(stored):
    """Not merely equal in content: the same content-addressed records."""
    parent = json.loads((STATE / "snapshots"
                         / "0027-m62-s4f-eval-v7-spent.json").read_text())
    for block in ("candidates", "datasets", "base_model", "archive",
                  "frozen_invariants", "policy_identities"):
        assert stored["records"][block] == parent["records"][block], block


def test_only_the_limitations_and_defects_records_moved(stored):
    parent = json.loads((STATE / "snapshots"
                         / "0027-m62-s4f-eval-v7-spent.json").read_text())
    moved = {b for b in stored["records"]
             if stored["records"][b] != parent["records"][b]}
    assert moved == {"limitations", "defects"}


def test_the_note_claims_nothing_that_did_not_happen(stored):
    note = stored["control_plane_note"].lower()
    for forbidden in ("rescored", "reevaluated", "re-evaluated", "candidate 006",
                      "eval-v8", "promoted", "calibrated"):
        assert forbidden not in note, forbidden
    assert "0 model loads" in note and "0 generations" in note


def test_the_defect_ledger_records_the_four_s4h_findings(stored):
    defects = {d["id"]: d for d in record(stored, "defects")}
    for identifier in ("D45", "D46", "D47", "D48"):
        assert identifier in defects, identifier
        assert "S4H" in defects[identifier]["evidence"]
    assert defects["D48"]["status"] == "FIXED"
    for identifier in ("D45", "D46", "D47"):
        assert defects[identifier]["status"] == "FIXED_OBSERVABILITY_ONLY"
    # D28 and D29 describe the HISTORICAL path and must not be closed by a future fix.
    assert defects["D28"]["status"] == "OPEN"
    assert defects["D29"]["status"] == "ACCEPTED_KNOWN_LIMITATION"


def test_the_limitations_record_states_that_calibration_is_synthetic(stored):
    joined = " ".join(record(stored, "limitations"))
    assert "SYNTHETICALLY CALIBRATED" in joined
    assert "REAL_WORLD_CALIBRATED = NO" in joined
    assert "ADDITIVE AND INERT" in joined


def test_the_stale_unrun_claim_about_eval_v7_is_gone(stored):
    """§50: a CURRENT_STATE_STALE line, corrected. The historical documents are not."""
    joined = " ".join(record(stored, "limitations"))
    assert "eval-v7 is the first true head-to-head and is UNRUN" not in joined
    assert "eval-v7 was the first, and it is SPENT" in joined


def test_the_test_baseline_no_longer_names_the_keyword_as_the_authority(stored):
    invocation = stored["test_baseline"]["invocation"]
    assert "verify_m62_scientific_suite.py" in invocation
    assert "NO LONGER the canonical authority" in invocation


def test_no_authority_is_observed_and_none_can_be_granted(stored):
    observation = stored["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for field in ("eval", "train", "promotion"):
        assert observation[field] == "NONE_OBSERVED_IN_REPOSITORY"


def test_the_ruled_out_list_bars_reopening_candidate_005_over_a_better_ruler(stored):
    joined = " ".join(stored["next_milestone"]["ruled_out"]).lower()
    assert "better ruler does not reopen a spent exam" in joined
    assert "no gate threshold may be set from a synthetic rate" in joined


# ══════════════════════════════════════════════════════════════════════════════
#  The instrument anchor, re-derived
# ══════════════════════════════════════════════════════════════════════════════
def test_the_frozen_instrument_versions_re_derive_from_the_production_modules(V):
    from training_gym.evaluation.instruments import current_versions
    assert current_versions() == V.FROZEN_INSTRUMENT_VERSIONS


def test_the_scientific_suite_manifest_is_canonical_and_named_by_the_verifier(V):
    path = REPO_ROOT / V.SCIENTIFIC_SUITE_PATH
    raw = path.read_bytes()
    assert raw == V.canonical_bytes(json.loads(raw.decode("utf-8")))


def test_the_whole_control_plane_verifies(V):
    assert V.main([]) == 0


# ══════════════════════════════════════════════════════════════════════════════
#  §71 NON-VACUITY — the mutations that must be refused
# ══════════════════════════════════════════════════════════════════════════════
def plane(V, mutate=None):
    report = V.Report()
    cp = V.load(report)
    assert not report.problems, report.problems
    if mutate:
        mutate(cp)
    return cp, V.Report()


def test_mutation_candidate005_marked_eligible_is_refused(V):
    def mutate(cp):
        for entry in cp.snapshot["candidates"]:
            if entry["candidate_id"] == "qwen3-06b-lora-quality-live-005":
                entry["status"] = "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    cp, report = plane(V, mutate)
    V.check_evaluation_receipt(cp, report)
    assert report.problems


def test_mutation_candidate005_marked_promoted_is_refused(V):
    def mutate(cp):
        for entry in cp.snapshot["candidates"]:
            if entry["candidate_id"] == "qwen3-06b-lora-quality-live-005":
                entry["status"] = "PROMOTED"
    cp, report = plane(V, mutate)
    V.check_candidate_state(cp, report)
    assert report.problems


def test_mutation_eval_v7_marked_unspent_is_refused(V):
    def mutate(cp):
        for entry in cp.snapshot["datasets"]:
            if entry["dataset_id"] == "m62-defensive-eval" and entry["version"] == "v7":
                entry["status"] = "FROZEN_UNUSED"
                entry["spent_by"] = None
    cp, report = plane(V, mutate)
    V.check_dataset_state(cp, report)
    assert report.problems


def test_mutation_an_instrument_version_moves_is_refused(V, monkeypatch):
    """An instrument that changed under a stable version is a different instrument."""
    monkeypatch.setitem(V.FROZEN_INSTRUMENT_VERSIONS, "secret_pii",
                        "m62.secret_pii_detector.3")
    cp, report = plane(V)
    V.check_instrument_stack(cp, report)
    assert any(c == "INSTRUMENT_STACK" for c, _ in report.problems)


def test_mutation_an_unanchored_slot_appears_is_refused(V, monkeypatch):
    anchor = {k: v for k, v in V.FROZEN_INSTRUMENT_VERSIONS.items() if k != "coverage"}
    monkeypatch.setattr(V, "FROZEN_INSTRUMENT_VERSIONS", anchor)
    cp, report = plane(V)
    V.check_instrument_stack(cp, report)
    assert any("unanchored" in m for _, m in report.problems)


def test_mutation_the_scientific_suite_drops_a_keyword_invisible_module(V, monkeypatch,
                                                                       tmp_path):
    suite = json.loads((REPO_ROOT / V.SCIENTIFIC_SUITE_PATH).read_text())
    suite["groups"] = [
        {**g, "modules": [m for m in g["modules"]
                          if m not in V.KEYWORD_INVISIBLE_MODULES]}
        for g in suite["groups"]]
    narrowed = tmp_path / "state" / "m62"
    narrowed.mkdir(parents=True)
    (narrowed / "scientific-suite.json").write_bytes(V.canonical_bytes(suite))
    # The plane is loaded from the REAL root FIRST; only the manifest lookup is
    # redirected, so the mutation under test is the narrowed manifest and nothing else.
    cp, report = plane(V)
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(V, "_git", lambda *a: (0, V.SCIENTIFIC_SUITE_PATH))
    V.check_instrument_stack(cp, report)
    assert any("nothing selects it" in m for _, m in report.problems)


def test_mutation_a_historical_scorer_reaching_the_instruments_is_refused(V,
                                                                          monkeypatch,
                                                                          tmp_path):
    """The property that keeps every sealed receipt meaning what it meant."""
    fake = tmp_path / "training_gym" / "evaluation"
    fake.mkdir(parents=True)
    for name in ("scoring.py", "gates.py", "policy.py", "statistics.py", "metrics.py",
                 "comparison.py", "reports.py", "__init__.py"):
        (fake / name).write_text("from .instruments import scan_text\n")
    (tmp_path / "tests").mkdir()
    for module in V.KEYWORD_INVISIBLE_MODULES:
        (tmp_path / module).write_text("")
    monkeypatch.setattr(V, "_PACKAGE_ROOT", tmp_path)
    cp, report = plane(V)
    V.check_instrument_stack(cp, report)
    assert any("can drift onto a 'latest' meaning" in m for _, m in report.problems)


def test_the_unmutated_plane_passes_every_check_the_mutations_use(V):
    """The control: each refusal above is caused by its mutation, not by the checks."""
    cp, report = plane(V)
    for check in (V.check_candidate_state, V.check_dataset_state,
                  V.check_evaluation_receipt, V.check_instrument_stack):
        check(cp, report)
    assert not report.problems, report.problems
