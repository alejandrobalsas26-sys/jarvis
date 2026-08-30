"""V69 M63 S4B — the control plane at generation 17: candidate 005, and a second ruling.

WHAT THESE TESTS ARE FOR
------------------------
Generation 17 records a fifth candidate in `DESIGNED_UNTRAINED` and a SECOND human
operator ruling that superseded one clause of one standing `ruled_out` entry so that
candidate could exist. Both are exactly the kind of claim a control plane can make about
itself and then believe, so both are re-derived here rather than read.

The failures these tests exist to prevent:

  * **A transition table that stopped running.** Generation 17 is the FIRST generation
    whose parent is a V3 container. The transition guard reads `parent["candidates"]`,
    and a V3 generation stores candidates as a content-addressed record -- so a raw read
    of a V3 parent has no `candidates` key at all and every sealed ordinal looks like a
    brand-new candidate. That defect is pinned below, in both directions.
  * **A candidate minted at a fresh ordinal.** Generation 16 carried no ordinal 5. The
    entry guard must still refuse a fresh ordinal arriving already trained or evaluated.
  * **A design the repository cannot produce.** `DESIGNED_UNTRAINED` is re-derived from
    the production generator: corpus, base revision, render policy, single-axis relation.
  * **A supersession that quietly widened.** The rewritten prospective rule must still
    bar every subject it is not superseding.
  * **An axis recorded without its two ends.** `primary_axis` must name the dial AND both
    values, re-derived from the generator.
  * **Two rulings vouching for each other.** Each is re-derived independently, and a
    ruling copied from the other is refused.
  * **A held reference quietly moved.** Candidate 004 must come out of generation 17 with
    the status, digest and receipts it went in with.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS OR MATERIALISES A TOKEN. Every
mutation happens inside a per-test copy of the control plane; the real tree is never
written. This file reads no holdout task body and contains none.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import shutil
from pathlib import Path

import pytest

import scripts.build_quality_training_config as QCFG
from scripts import verify_m62_control_plane as V

REPO = V.REPO_ROOT

CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"
CANDIDATE_005_ID = "qwen3-06b-lora-quality-live-005"

#: Written independently of the artefacts under test.
EXPECTED_GENERATION = 17
EXPECTED_PARENT_SHA = (
    "2d7ee69db1e18a419b0f87867a7a943223bb8ff79592beb0f072aca80e284639")
RULING_PATH = "state/m62/rulings/0002-s4b-candidate005-learning-rate.json"
S3U_RULING_PATH = "state/m62/rulings/0001-s3u-candidate004-learning-rate.json"
DESIGN_DOC = "jarvis/docs/V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md"
CANDIDATE_004_ADAPTER = (
    "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67")


#: The generation S4B wrote, addressed BY PATH rather than by following the live pointer.
#:
#: RESCOPED AT S4C, which trained candidate 005 and moved it to TRAINED_UNEVALUATED at
#: generation 18. The assertions below are about what S4B RECORDED -- a designed candidate
#: with no weights, no receipt and no exam. Read from the live pointer they also asserted,
#: silently, that no later generation exists, which was true by coincidence until S4C wrote
#: one. The property S4B owns is unchanged and is now addressed as such; the checks that
#: are genuinely about whatever is newest still follow the pointer.
S4B_SNAPSHOT = "0017-m63-s4b-candidate005-designed.json"


def _s4b_snapshot(root: Path = REPO) -> dict:
    stored = json.loads(
        (root / V.SNAPSHOT_DIR / S4B_SNAPSHOT).read_text(encoding="utf-8"))
    payload, problems = V.rehydrate_v3(
        stored, V.load_record_store(root / V.RECORD_DIR))
    assert not problems, problems
    return payload


def _s4b_entry(field: str = "") -> dict:
    entry = next(c for c in _s4b_snapshot()["candidates"]
                 if c["candidate_id"] == CANDIDATE_005_ID)
    return entry[field] if field else entry


@pytest.fixture()
def s4b_sandbox(sandbox):
    """The sandbox, repointed at generation 17 so S4B's own state is what is mutated."""
    current = json.loads((sandbox / V.CURRENT_PATH).read_text(encoding="utf-8"))
    path = f"{V.SNAPSHOT_DIR}/{S4B_SNAPSHOT}"
    current["latest_snapshot_path"] = path
    current["latest_snapshot_sha256"] = V.sha256_bytes((sandbox / path).read_bytes())
    current["state_generation"] = 17
    (sandbox / V.CURRENT_PATH).write_bytes(V.canonical_bytes(current))
    return sandbox


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A writable copy of the whole control plane, records included.

    The record store is copied WITH the snapshots, because generation 17 is the first
    one whose parent is content-addressed: a sandbox without the records cannot
    rehydrate that parent, and every transition assertion below would pass by asserting
    nothing.
    """
    shutil.copytree(REPO / "state" / "m62", tmp_path / "state" / "m62")
    for rel in (V.PROGRESS_PATH, V.HISTORY_INDEX_PATH, V.ARCHIVE_PATH):
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, destination)
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    return tmp_path


def _plane(root: Path) -> V.ControlPlane:
    """The sandbox's plane, loaded exactly the way the verifier loads the real one.

    Production ``load`` is used rather than a hand-built ``ControlPlane`` so the record
    store, the stored container and the rehydration problems are all populated the same
    way; a locally-assembled plane would quietly disable ``check_record_store``.
    """
    report = V.Report()
    plane = V.load(report)
    assert plane is not None, _messages(report)
    return plane


def _messages(report: V.Report) -> str:
    return " | ".join(message for _, message in report.problems)


def _mutate(root: Path, mutate) -> V.ControlPlane:
    """Apply *mutate* to the sandbox's newest generation, semantically."""
    plane = _plane(root)
    payload = copy.deepcopy(plane.snapshot)
    mutate(payload)
    return dataclasses.replace(plane, snapshot=payload)


def _entry(snapshot: dict, ordinal: int) -> dict:
    return next(c for c in snapshot["candidates"] if c["ordinal"] == ordinal)


# ══════════════════════════════════════════════════════════════════════════════
#  1. The generation itself
# ══════════════════════════════════════════════════════════════════════════════
def test_the_s4b_generation_is_seventeen_and_chains_to_sixteen():
    stored = json.loads(
        (REPO / V.SNAPSHOT_DIR / S4B_SNAPSHOT).read_text(encoding="utf-8"))
    assert stored["state_generation"] == EXPECTED_GENERATION
    assert stored["parent_snapshot_sha256"] == EXPECTED_PARENT_SHA
    assert stored["subject_state_milestone"] == "S4B"
    assert stored["schema_version"] == V.CONTROL_PLANE_V3_SCHEMA_VERSION
    assert set(stored["records"]) == set(V.V3_RECORD_BLOCKS)


def test_the_live_generation_descends_from_the_s4b_one():
    plane = V.load(V.Report())
    assert plane is not None
    assert plane.snapshot["state_generation"] >= EXPECTED_GENERATION


def test_the_whole_verifier_passes_with_no_problems():
    """The one assertion that cannot be satisfied by agreeing with itself."""
    report = V.run()
    assert report.problems == [], _messages(report)


def test_the_snapshot_stays_far_inside_its_budget():
    plane = V.load(V.Report())
    size = len(plane.snapshot_bytes)
    assert size <= V.SNAPSHOT_MAX_BYTES
    assert V.SNAPSHOT_MAX_BYTES - size >= 1024
    assert V.SNAPSHOT_MAX_BYTES == 34_816, "the reviewed budget did not move"


# ══════════════════════════════════════════════════════════════════════════════
#  2. THE V3-PARENT DEFECT — the transition table must actually run
# ══════════════════════════════════════════════════════════════════════════════
def test_a_v3_parent_is_rehydrated_before_the_transition_table_reads_it(sandbox):
    """Generation 16 is content-addressed; its candidates live in a record.

    Read raw, it has no ``candidates`` key, and every sealed ordinal would look absent
    from the parent. This asserts the parent comes back with its candidates.
    """
    plane = _plane(sandbox)
    parent = V._parent_snapshot(plane)
    assert parent is not None, "the V3 parent did not resolve at all"
    assert parent["state_generation"] == plane.snapshot["state_generation"] - 1
    ordinals = sorted(c["ordinal"] for c in parent["candidates"])
    assert ordinals, "the parent came back with no candidates; it was read RAW"
    assert ordinals == sorted(range(1, len(ordinals) + 1))


def test_the_sealed_ordinals_are_not_reported_as_fresh_entries(sandbox):
    """The defect's symptom, pinned: four sealed candidates 'entering' at EVALUATED_*."""
    report = V.Report()
    V.check_candidate_state(_plane(sandbox), report)
    assert "may only enter as" not in _messages(report)
    assert report.problems == [], _messages(report)


def test_a_broken_record_store_does_not_silently_skip_the_transition_table(sandbox):
    """A parent that cannot be rehydrated must not be read as 'there is no parent'.

    It returns None, which reports nothing here -- and the RECORD_STORE check fails
    independently on the same store, so the generation as a whole is still refused.
    """
    plane = _plane(sandbox)          # captured while the store is still intact
    for record in (sandbox / V.RECORD_DIR).glob("*.json"):
        record.unlink()
    assert V._parent_snapshot(plane) is None
    report = V.Report()
    V.check_record_store(plane, report)
    assert report.problems, "a record store with no records was accepted"


@pytest.mark.parametrize("status", [
    "TRAINED_UNEVALUATED", "EVALUATED_NOT_ELIGIBLE",
    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
])
def test_a_fresh_ordinal_may_still_not_enter_already_trained(sandbox, status):
    """Non-vacuity: the guard the test above proves is running must still REFUSE."""
    def mutate(payload: dict) -> None:
        payload["candidates"].append({
            **copy.deepcopy(_entry(payload, 5)),
            "candidate_id": "qwen3-06b-lora-quality-live-006",
            "ordinal": 6, "status": status,
            "adapter_sha256": "e" * 64, "adapter_manifest_hash": "e" * 64,
            "training_receipt": "state/m62/receipts/x.train.json"})

    report = V.Report()
    V.check_candidate_state(_mutate(sandbox, mutate), report)
    assert "may only enter as" in _messages(report)


def test_an_illegal_transition_out_of_the_held_reference_is_refused(sandbox):
    def mutate(payload: dict) -> None:
        _entry(payload, 4)["status"] = "PROMOTED"

    report = V.Report()
    V.check_candidate_state(_mutate(sandbox, mutate), report)
    assert "PROMOTED" in _messages(report)


# ══════════════════════════════════════════════════════════════════════════════
#  3. Candidate 005 in the control plane
# ══════════════════════════════════════════════════════════════════════════════
def test_candidate_005_appears_exactly_once_and_is_designed():
    candidates = _s4b_snapshot()["candidates"]
    mine = [c for c in candidates if c["candidate_id"] == CANDIDATE_005_ID]
    assert len(mine) == 1
    entry = mine[0]
    assert entry["status"] == "DESIGNED_UNTRAINED"
    assert entry["ordinal"] == 5
    assert entry["training_corpus"] == "m62-defensive-quality-train v2"
    assert entry["base_model_revision"] == QCFG.BASE_MODEL_REVISION
    assert entry["evidence"] == DESIGN_DOC
    assert [c["candidate_id"] for c in candidates].count(CANDIDATE_005_ID) == 1


@pytest.mark.parametrize("field", [
    "adapter_sha256", "adapter_manifest_hash", "training_receipt",
    "evaluation_corpus", "evaluation_receipt",
])
def test_candidate_005_carried_no_artefact_and_no_measurement_when_designed(field):
    assert _s4b_entry(field) is None


def test_candidate_005_was_designed_with_no_weights_at_generation_17():
    """The sealed pair moved forward at S4C; what S4B RECORDED did not."""
    assert _s4b_entry("status") == "DESIGNED_UNTRAINED"
    assert _s4b_entry("adapter_sha256") is None
    assert V.FROZEN_CANDIDATES[CANDIDATE_005_ID][0] in (
        "DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED")


def test_the_design_is_re_derived_from_the_production_generator():
    report = V.Report()
    V.check_candidate_design(V.load(V.Report()), report)
    assert report.problems == [], _messages(report)


def test_a_designed_candidate_carrying_an_adapter_is_refused(s4b_sandbox):
    def mutate(payload: dict) -> None:
        _entry(payload, 5)["adapter_sha256"] = "a" * 64

    report = V.Report()
    V.check_candidate_state(_mutate(s4b_sandbox, mutate), report)
    assert "designed candidate has a configuration and no weights" in _messages(report)


def test_a_design_the_generator_cannot_produce_is_refused(sandbox):
    def mutate(payload: dict) -> None:
        _entry(payload, 5)["candidate_id"] = "qwen3-06b-lora-quality-live-006"

    report = V.Report()
    V.check_candidate_design(_mutate(sandbox, mutate), report)
    assert "no candidate in the production generator carries that run id" in \
        _messages(report)


def test_a_corpus_the_generator_does_not_train_on_is_refused(sandbox):
    def mutate(payload: dict) -> None:
        _entry(payload, 5)["training_corpus"] = "m62-defensive-quality-train v1"

    report = V.Report()
    V.check_candidate_design(_mutate(sandbox, mutate), report)
    assert "the generator trains it on v2" in _messages(report)


# ══════════════════════════════════════════════════════════════════════════════
#  4. The held reference is not moved
# ══════════════════════════════════════════════════════════════════════════════
def test_candidate_004_comes_out_exactly_as_it_went_in():
    entry = next(c for c in V.load(V.Report()).snapshot["candidates"]
                 if c["candidate_id"] == CANDIDATE_004_ID)
    assert entry["status"] == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert entry["adapter_sha256"] == CANDIDATE_004_ADAPTER
    assert entry["evaluation_corpus"] == "m62-defensive-eval v6"
    assert entry["evaluation_receipt"] == \
        "state/m62/receipts/qwen3-06b-lora-quality-live-004.eval.json"
    assert entry["training_receipt"] == \
        "state/m62/receipts/qwen3-06b-lora-quality-live-004.train.json"


def test_the_candidates_block_changed_by_exactly_one_appended_entry():
    """Generation 17 appends; it does not rewrite what four sealed generations recorded."""
    parent = json.loads(
        (REPO / "state/m62/snapshots/0016-m63-control-plane-v3-and-branch.json"
         ).read_text(encoding="utf-8"))
    before, problems = V.rehydrate_v3(parent, V.load_record_store(REPO / V.RECORD_DIR))
    assert not problems
    after = V.load(V.Report()).snapshot["candidates"]
    assert after[:len(before["candidates"])] == before["candidates"]
    assert len(after) == len(before["candidates"]) + 1


def test_the_holdouts_are_untouched():
    datasets = {f"{d['dataset_id']} {d['version']}": d
                for d in V.load(V.Report()).snapshot["datasets"]}
    v5 = datasets["m62-defensive-eval v5"]
    assert v5["status"] == "FROZEN_UNUSED"
    assert v5["spent_by"] is None
    assert datasets["m62-defensive-eval v6"]["status"] == "USED_IMMUTABLE"
    assert datasets["m62-defensive-eval v4"]["status"] == "USED_IMMUTABLE"
    assert "v7" not in " ".join(datasets)


def test_the_training_corpus_is_not_re_versioned():
    datasets = V.load(V.Report()).snapshot["datasets"]
    training = [d for d in datasets if d["role"] == "TRAINING_CORPUS"]
    assert sorted(d["version"] for d in training) == ["v1", "v2"]
    v2 = next(d for d in training if d["version"] == "v2")
    assert v2["manifest_hash"] == \
        "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f"
    assert v2["task_count"] == 182


# ══════════════════════════════════════════════════════════════════════════════
#  5. The second ruling
# ══════════════════════════════════════════════════════════════════════════════
def test_both_rulings_are_checked_not_only_the_first():
    assert set(V.OPERATOR_RULINGS) == {CANDIDATE_004_ID, CANDIDATE_005_ID}
    assert V.OPERATOR_RULINGS[CANDIDATE_005_ID] == ("005", RULING_PATH)
    report = V.Report()
    V.check_operator_ruling(V.load(V.Report()), report)
    assert report.problems == [], _messages(report)


@pytest.mark.parametrize("field,value", [
    ("subject_candidate", "qwen3-06b-lora-quality-live-004"),
    ("reference_candidate", "qwen3-06b-lora-quality-live-003"),
    ("primary_axis", "lora_rank"),
    ("reference_value", "1e-4"),
    ("ruled_value", "5e-5"),
])
def test_a_ruling_that_drifted_from_what_is_built_is_refused(sandbox, field, value):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload[field] = value
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane(sandbox), report)
    assert f"records {field}=" in _messages(report)


def test_a_ruling_that_stored_its_phrase_is_refused(sandbox):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["ruling_phrase_recorded"] = True
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane(sandbox), report)
    assert "authorisation phrase is withheld" in _messages(report)


def test_a_ruling_claiming_a_wider_scope_is_refused(sandbox):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["scope"] = "DESIGN_AND_TRAINING"
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane(sandbox), report)
    assert "authorised a DESIGN and nothing else" in _messages(report)


def test_a_ruling_copied_from_the_other_is_refused(sandbox):
    """Two rulings sharing an id, a digest or a subject are not two human decisions."""
    original = json.loads((sandbox / S3U_RULING_PATH).read_text(encoding="utf-8"))
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["ruling_phrase_sha256"] = original["ruling_phrase_sha256"]
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane(sandbox), report)
    assert "is not a second human decision" in _messages(report)


def test_a_missing_ruling_refuses_the_candidate_that_rests_on_it(sandbox):
    (sandbox / RULING_PATH).unlink()
    report = V.Report()
    V.check_operator_ruling(_plane(sandbox), report)
    assert "is not a file in this tree" in _messages(report)


# ══════════════════════════════════════════════════════════════════════════════
#  6. NEXT: the axis, the prospective rule, and the authority observation
# ══════════════════════════════════════════════════════════════════════════════
def test_the_recorded_axis_names_the_dial_and_both_ends():
    axis = V.load(V.Report()).snapshot["next_milestone"]["primary_axis"]
    assert "learning_rate" in axis
    assert QCFG.format_learning_rate(5e-5) in axis
    assert QCFG.format_learning_rate(2.5e-5) in axis


@pytest.mark.parametrize("subject", V.REQUIRED_RULED_OUT_SUBJECTS)
def test_the_prospective_rule_still_bars_everything_it_is_not_superseding(subject):
    ruled_out = " | ".join(V.load(V.Report()).snapshot["next_milestone"]["ruled_out"])
    assert subject in ruled_out


@pytest.mark.parametrize("subject", [
    "eval-v7", "candidate 006", "second seed", "sweep", "promotion", "eval-v6",
    "standing permission to retrain",
])
def test_the_prospective_rule_bars_this_milestones_own_temptations(subject):
    ruled_out = " | ".join(V.load(V.Report()).snapshot["next_milestone"]["ruled_out"])
    assert subject in ruled_out


def test_neither_learning_rate_ruling_is_recorded_as_general():
    ruled_out = V.load(V.Report()).snapshot["next_milestone"]["ruled_out"]
    joined = " | ".join(ruled_out)
    assert "candidate 004 only" in joined
    assert "candidate 005 only" in joined
    report = V.Report()
    V._check_ruled_out(V.load(V.Report()).snapshot["next_milestone"], report)
    assert report.problems == [], _messages(report)


def test_no_authority_is_observed_or_created():
    observation = V.load(V.Report()).snapshot["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for kind in ("train", "eval", "promotion"):
        assert observation[kind] in V.AUTHORITY_OBSERVATIONS
        assert observation[kind] == "NONE_OBSERVED_IN_REPOSITORY"


def test_no_tracked_file_carries_a_spendable_token():
    report = V.Report()
    V.check_holdout_firewall(V.load(V.Report()), report)
    assert report.problems == [], _messages(report)


def test_the_project_block_did_not_move_master_or_merge():
    project = V.load(V.Report()).snapshot["project"]
    assert project["branch"] == "jarvis-v69-m63-world-state"
    assert project["master_commit"] == \
        "3705114228edef2f665be349c5c4429b7b16777a"
    assert project["merged_into_master"] is False
    assert project["released"] is False
    assert project["tagged"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  7. Capacity for the state a successful run would need
# ══════════════════════════════════════════════════════════════════════════════
def test_the_trained_terminal_state_fits_before_it_is_needed():
    """Capacity is proved BEFORE authority is asked for, not discovered after training."""
    plane = V.load(V.Report())
    payload = copy.deepcopy(plane.snapshot)
    entry = _entry(payload, 5)
    entry.update({
        "status": "TRAINED_UNEVALUATED",
        "adapter_sha256": "f" * 64, "adapter_manifest_hash": "f" * 64,
        "training_receipt":
            "state/m62/receipts/qwen3-06b-lora-quality-live-005.train.json",
    })
    stored = json.loads(plane.snapshot_bytes.decode("utf-8"))
    projected = dict(stored)
    projected["records"] = dict(stored["records"])
    projected["records"]["candidates"] = V.sha256_bytes(
        V.canonical_bytes({"block": "candidates", "value": payload["candidates"]}))
    projected["state_generation"] = 18
    size = len(V.canonical_bytes(projected))
    assert size <= V.SNAPSHOT_MAX_BYTES
    assert V.SNAPSHOT_MAX_BYTES - size >= 1024
