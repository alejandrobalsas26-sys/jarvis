"""V69 M62 S3U — the control plane at generation 9: a candidate, and the ruling behind it.

WHAT THESE TESTS ARE FOR
------------------------
Generation 9 records two new things: a fourth candidate in `DESIGNED_UNTRAINED`, and a
human operator ruling that superseded one clause of one standing `ruled_out` entry so
that candidate could exist at all. Both are exactly the kind of claim a control plane can
make about itself and then believe, so both are re-derived here rather than read.

The failures these tests exist to prevent:

  * **A candidate minted at a fresh ordinal.** Generation 8 carried no ordinal 4. The
    entry guard was widened by exactly one state, and it must still refuse a fresh
    ordinal arriving already trained, evaluated or promoted.
  * **A design the repository cannot produce.** `DESIGNED_UNTRAINED` is re-derived from
    the production generator: corpus, base revision, render policy, control, and the
    single-axis relation. A snapshot agreeing with a verifier constant while the
    generator disagrees with both is a FAILURE.
  * **A supersession that quietly widened.** The rewritten prospective rule must still
    bar every subject it is not superseding, and the learning-rate permission must stay
    scoped to candidate 004 rather than opening the dial for everyone.
  * **An axis recorded without its two ends.** `primary_axis` must name the dial AND both
    values, re-derived from the generator, so the control plane cannot describe an
    experiment the repository is not building.
  * **A ruling nobody can audit.** The operator ruling must exist as a tracked, canonical
    record whose subject, reference and two values match what is actually configured —
    and it must NOT contain the authorisation phrase, only a digest of it.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS OR GENERATES A TOKEN. Every mutation
happens inside a per-test copy of the control plane; the real tree is never written.

This file reads no `eval-v4` or `eval-v5` task body and contains none.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

import scripts.build_quality_training_config as QCFG
from scripts import verify_m62_control_plane as V

REPO = V.REPO_ROOT

CANDIDATE_003_ID = "qwen3-06b-lora-quality-live-003"
GEN13_SNAPSHOT_PATH = "state/m62/snapshots/0013-m62-s3x1-fresh-eval-v6-frozen.json"
CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"

#: Written independently of the artefacts under test.
EXPECTED_GENERATION = 9
EXPECTED_PARENT_SHA = (
    "e4001dec216b4623c68f98e7ea64c0dfe6eba74347ccb898eecfd715de2a23fb")
EVAL_V5_MANIFEST = (
    "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c")
EVAL_V5_PACK = "287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6"
RULING_PATH = "state/m62/rulings/0001-s3u-candidate004-learning-rate.json"

#: The ruling authorised design at this rate and no other. Written here, not imported.
REFERENCE_LEARNING_RATE_TEXT = "1e-4"
RULED_LEARNING_RATE_TEXT = "5e-5"


#: The snapshot THIS file is about, named by path rather than followed from the live
#: pointer. RESCOPED AT S3V, which is the documented pattern here for an assertion that
#: compares a sealed milestone's property against LIVE state.
#:
#: Through generation 9 the two were the same document, so reading the pointer looked
#: equivalent. It was not: it made every assertion below silently claim "and no later
#: generation exists", so the moment S3V sealed generation 10 this file failed on
#: candidate 004 having been trained -- which is not a defect in S3U and not something
#: S3U's tests have any business asserting. A sealed generation is immutable; reading it
#: by path is what makes these assertions true forever instead of true until the next
#: milestone.
S3U_SNAPSHOT = "0009-m62-s3u-candidate004-designed.json"


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(
        (REPO / V.SNAPSHOT_DIR / S3U_SNAPSHOT).read_text(encoding="utf-8"))


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A writable copy of the control plane, so a mutation never touches the real tree."""
    for rel in (V.CURRENT_PATH, V.MIGRATION_MANIFEST_PATH, V.ARCHIVE_PATH,
                V.PROGRESS_PATH, V.HISTORY_INDEX_PATH, V.CURRENT_SCHEMA_PATH,
                V.SNAPSHOT_SCHEMA_PATH, V.OPERATOR_RULING_S3U):
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, destination)
    for source in (REPO / V.SNAPSHOT_DIR).iterdir():
        destination = tmp_path / V.SNAPSHOT_DIR / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    # PINNED AT S3Y to generation 13, BY PATH rather than by following `current.json`.
    #
    # Every claim in this file is about the S3U design of candidate 004, which was live
    # from generation 9 through 13. S3Y then measured the candidate, and generation 14
    # records the axis as MEASURED and CLOSED -- so `check_candidate_design`, which
    # deliberately re-derives only DESIGNED_UNTRAINED and TRAINED_UNEVALUATED claims,
    # correctly finds nothing to re-derive there. Left following the live pointer, every
    # non-vacuity mutation below would pass by asserting nothing at all.
    pinned = json.loads((tmp_path / V.CURRENT_PATH).read_text(encoding="utf-8"))
    pinned["latest_snapshot_path"] = GEN13_SNAPSHOT_PATH
    pinned["latest_snapshot_sha256"] = V.sha256_bytes(
        (tmp_path / GEN13_SNAPSHOT_PATH).read_bytes())
    pinned["state_generation"] = 13
    (tmp_path / V.CURRENT_PATH).write_text(
        json.dumps(pinned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    return tmp_path


def _plane_from(root: Path) -> V.ControlPlane:
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    snapshot_path = root / current["latest_snapshot_path"]
    snapshot_bytes = snapshot_path.read_bytes()
    return V.ControlPlane(
        current=current,
        current_bytes=(root / V.CURRENT_PATH).read_bytes(),
        snapshot=json.loads(snapshot_bytes.decode("utf-8")),
        snapshot_bytes=snapshot_bytes,
        snapshot_path=snapshot_path,
        migration=json.loads(
            (root / V.MIGRATION_MANIFEST_PATH).read_text(encoding="utf-8")))


def _categories(report: V.Report) -> set[str]:
    return {category for category, _ in report.problems}


def _messages(report: V.Report) -> str:
    return " | ".join(message for _, message in report.problems)


def _rewrite(root: Path, rel: str, payload: dict) -> None:
    (root / rel).write_bytes(V.canonical_bytes(payload))


def _repoint(root: Path) -> None:
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    data = (root / current["latest_snapshot_path"]).read_bytes()
    current["latest_snapshot_sha256"] = V.sha256_bytes(data)
    _rewrite(root, V.CURRENT_PATH, current)


def _mutate(root: Path, mutate) -> V.ControlPlane:
    """Apply *mutate* to the sandbox's newest snapshot and reload the plane."""
    plane = _plane_from(root)
    payload = copy.deepcopy(plane.snapshot)
    mutate(payload)
    _rewrite(root, plane.current["latest_snapshot_path"], payload)
    _repoint(root)
    return _plane_from(root)


def _entry(snapshot: dict, ordinal: int) -> dict:
    return next(c for c in snapshot["candidates"] if c["ordinal"] == ordinal)


# ══════════════════════════════════════════════════════════════════════════════
#  1. The live control plane
# ══════════════════════════════════════════════════════════════════════════════
def test_the_live_control_plane_verifies_clean():
    """The whole point. Everything below is about WHY this passes."""
    report = V.run()
    assert report.problems == [], _messages(report)


def test_the_generation_advanced_by_one_from_the_recorded_parent(snapshot):
    assert snapshot["state_generation"] == EXPECTED_GENERATION
    assert snapshot["parent_snapshot_sha256"] == EXPECTED_PARENT_SHA
    parent = json.loads(
        (REPO / V.SNAPSHOT_DIR /
         "0008-m62-s3t0-termination-observability.json").read_text(encoding="utf-8"))
    assert parent["state_generation"] == EXPECTED_GENERATION - 1


def test_the_generation_nine_snapshot_is_still_byte_exact():
    """RESCOPED AT S3V. This asserted the LIVE pointer still named generation 9, which
    stopped being true the moment a later milestone sealed one -- and "no successor
    exists" was never S3U's claim to make.

    What S3U is owed forever is that its own generation was not rewritten. A superseded
    snapshot is never revised, so its digest is checked against the bytes on disk here,
    and the live pointer is checked only for the property that must hold at EVERY
    generation: that it is internally consistent."""
    data = (REPO / V.SNAPSHOT_DIR / S3U_SNAPSHOT).read_bytes()
    assert V.sha256_bytes(data) == (
        "4b6f1c9b1d5e512ecd22a66849245709204ed69c1c5ad25dd26cca9766022c98")
    current = json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
    assert current["state_generation"] >= EXPECTED_GENERATION
    live = (REPO / current["latest_snapshot_path"]).read_bytes()
    assert current["latest_snapshot_sha256"] == V.sha256_bytes(live)


def test_the_snapshot_is_within_its_reviewed_budget(snapshot):
    """S3U recompacted rather than raising a budget, and that remains true OF S3U.

    The reviewed ceiling itself moved once, at S3X.0, from 32,768 to 34,816 bytes under
    an explicit operator ruling recorded in the generation 12 snapshot. This pin is the
    anchor that makes a SILENT raise impossible, so it names the number literally; the
    size assertion below binds to the constant so the two can never disagree.
    """
    assert V.SNAPSHOT_MAX_BYTES == 34_816
    current = json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
    assert len((REPO / current["latest_snapshot_path"]).read_bytes()) <= V.SNAPSHOT_MAX_BYTES


# ══════════════════════════════════════════════════════════════════════════════
#  2. Candidate 004 is recorded, and 001-003 are not disturbed
# ══════════════════════════════════════════════════════════════════════════════
def test_candidate_004_is_recorded_designed_and_untrained(snapshot):
    entry = _entry(snapshot, 4)
    assert entry["candidate_id"] == CANDIDATE_004_ID
    assert entry["status"] == "DESIGNED_UNTRAINED"
    assert entry["training_corpus"] == "m62-defensive-quality-train v2"
    assert entry["base_model_revision"] == QCFG.BASE_MODEL_REVISION
    assert entry["evidence"] == V.CANDIDATE_004_EVIDENCE
    assert (REPO / entry["evidence"]).is_file()


@pytest.mark.parametrize("field", [
    "adapter_sha256", "adapter_manifest_hash", "evaluation_corpus",
    "training_receipt", "evaluation_receipt",
])
def test_candidate_004_carries_nothing_that_would_claim_an_operation(snapshot, field):
    """Designed means a configuration exists and NOTHING ELSE does."""
    assert _entry(snapshot, 4)[field] is None


def test_the_three_earlier_candidates_are_unchanged(snapshot):
    parent = json.loads(
        (REPO / V.SNAPSHOT_DIR /
         "0008-m62-s3t0-termination-observability.json").read_text(encoding="utf-8"))
    before = {c["candidate_id"]: c for c in parent["candidates"]}
    after = {c["candidate_id"]: c for c in snapshot["candidates"]}
    assert set(after) - set(before) == {CANDIDATE_004_ID}
    for cid, entry in before.items():
        assert after[cid] == entry, f"{cid} moved"


def test_the_verifier_pins_candidate_004_as_trained_and_unevaluated():
    """RESCOPED AT S3V. Through generation 9 this pinned ``("DESIGNED_UNTRAINED", None)``:
    S3U designed candidate 004 and produced no weights, and ``None`` WAS the whole content
    of that state. S3V spent one plan-bound TRAIN authority, so the pin moves with it.

    The digest is asserted to be candidate 004's OWN, not merely non-null and not candidate
    003's: a fourth candidate inheriting a third's weights digest is exactly the
    substitution this pair exists to catch.
    """
    status, adapter = V.FROZEN_CANDIDATES[CANDIDATE_004_ID]
    # RE-QUOTED AT S3Y, from the milestone that sealed the transition. The adapter digest
    # is what this pair is really for and it is UNCHANGED: an evaluation measures weights
    # and does not alter them.
    assert status == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert adapter == (
        "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67")
    assert adapter != V.FROZEN_CANDIDATES[CANDIDATE_003_ID][1]


def test_candidate_004_receipts_are_the_training_one_and_the_s3y_evaluation():
    """RESCOPED AT S3Y. Both receipts now exist because both operations happened.

    What this test has always really guarded is that an evaluation receipt for this
    candidate may not mean `eval-v5` was spent. It does not: S3Y spent `eval-v6`, under
    one explicit human EVAL authority, and `eval-v5` is still FROZEN_UNUSED with
    `spent_by` null. That invariant is asserted here rather than dropped. Runtime
    artefacts stay unasserted either way -- they are gitignored, and a measured candidate
    stays measured after its run tree is deleted.
    """
    names = sorted(p.name for p in (REPO / "state/m62/receipts").iterdir()
                   if CANDIDATE_004_ID in p.name)
    assert names == [f"{CANDIDATE_004_ID}.eval.json", f"{CANDIDATE_004_ID}.train.json"]
    receipt = json.loads(
        (REPO / f"state/m62/receipts/{CANDIDATE_004_ID}.eval.json").read_text("utf-8"))
    assert receipt["holdout"]["dataset_version"] == "v6"
    assert "v5" != receipt["holdout"]["dataset_version"]


# ══════════════════════════════════════════════════════════════════════════════
#  3. The fresh-ordinal entry guard was widened by exactly one state
# ══════════════════════════════════════════════════════════════════════════════
def test_the_entry_guard_admits_only_two_states():
    assert V.FRESH_ORDINAL_ENTRY_STATES == ("NOT_CREATED", "DESIGNED_UNTRAINED")


@pytest.mark.parametrize("status", [
    "TRAINED_UNEVALUATED", "EVALUATED_NOT_ELIGIBLE",
    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW", "EVALUATED_NEEDS_MORE_EVIDENCE",
    "EVALUATED_QUARANTINED", "PROMOTED",
])
def test_a_fresh_ordinal_may_not_enter_already_trained_or_measured(sandbox, status):
    """The guard's whole purpose, checked at every state it must still refuse."""
    def mutate(payload: dict) -> None:
        payload["candidates"].append({
            "adapter_manifest_hash": None, "adapter_sha256": None,
            "base_model_revision": QCFG.BASE_MODEL_REVISION,
            "candidate_id": "qwen3-06b-lora-quality-live-005",
            "evaluation_corpus": None, "evaluation_receipt": None,
            "evidence": V.CANDIDATE_004_EVIDENCE, "ordinal": 5, "status": status,
            "training_corpus": "m62-defensive-quality-train v2",
            "training_receipt": None})

    plane = _mutate(sandbox, mutate)
    report = V.Report()
    V.check_candidate_state(plane, report)
    assert "may only enter as" in _messages(report)


def test_a_fresh_ordinal_entering_designed_is_still_re_derived(sandbox):
    """Admitting DESIGNED_UNTRAINED is safe only because the design is checked. A
    candidate at a fresh ordinal the generator cannot produce is refused."""
    def mutate(payload: dict) -> None:
        payload["candidates"].append({
            "adapter_manifest_hash": None, "adapter_sha256": None,
            "base_model_revision": QCFG.BASE_MODEL_REVISION,
            "candidate_id": "qwen3-06b-lora-quality-live-005",
            "evaluation_corpus": None, "evaluation_receipt": None,
            "evidence": V.CANDIDATE_004_EVIDENCE, "ordinal": 5,
            "status": "DESIGNED_UNTRAINED",
            "training_corpus": "m62-defensive-quality-train v2",
            "training_receipt": None})

    plane = _mutate(sandbox, mutate)
    entry_report = V.Report()
    V.check_candidate_state(plane, entry_report)
    assert "may only enter as" not in _messages(entry_report)

    design_report = V.Report()
    V.check_candidate_design(plane, design_report)
    assert "no candidate in the production generator carries that run id" in _messages(
        design_report)


# ══════════════════════════════════════════════════════════════════════════════
#  4. The design is re-derived, and a second axis is refused
# ══════════════════════════════════════════════════════════════════════════════
def test_the_design_re_derivation_accepts_the_real_state():
    report = V.Report()
    V.check_candidate_design(V.load(V.Report()), report)
    assert report.problems == [], _messages(report)


def test_a_design_claim_fails_when_a_second_dial_appears(sandbox, monkeypatch):
    """The generator refuses its own design, and the verifier reports that refusal."""
    monkeypatch.setattr(QCFG, "OPTIONS", copy.deepcopy(QCFG.OPTIONS))
    QCFG.OPTIONS["S3U"]["epochs"] = 3
    report = V.Report()
    V.check_candidate_design(_plane_from(sandbox), report)
    assert "refuses its own design" in _messages(report)
    assert "second experimental axis" in _messages(report)


def test_a_design_claim_fails_when_the_axis_is_set_back_to_the_reference(sandbox,
                                                                        monkeypatch):
    monkeypatch.setattr(QCFG, "OPTIONS", copy.deepcopy(QCFG.OPTIONS))
    QCFG.OPTIONS["S3U"]["learning_rate"] = QCFG.OPTIONS["S3J"]["learning_rate"]
    report = V.Report()
    V.check_candidate_design(_plane_from(sandbox), report)
    assert "tests nothing" in _messages(report)


def test_a_design_claim_fails_at_a_learning_rate_the_operator_did_not_rule(sandbox,
                                                                          monkeypatch):
    monkeypatch.setattr(QCFG, "OPTIONS", copy.deepcopy(QCFG.OPTIONS))
    QCFG.OPTIONS["S3U"]["learning_rate"] = 2e-4
    report = V.Report()
    V.check_candidate_design(_plane_from(sandbox), report)
    assert "operator ruling" in _messages(report)


def test_a_designed_candidate_with_no_declared_relation_is_refused(sandbox, monkeypatch):
    """'Exactly one thing changed' must be a declaration, not an absence of one."""
    monkeypatch.setattr(QCFG, "CANDIDATE_SINGLE_AXIS",
                        {k: v for k, v in QCFG.CANDIDATE_SINGLE_AXIS.items()
                         if k != "004"})
    report = V.Report()
    V.check_candidate_design(_plane_from(sandbox), report)
    assert "declares no single-axis relation" in _messages(report)


def test_candidate_003s_design_re_derivation_is_not_weakened(sandbox, monkeypatch):
    """S3U generalised the guard; candidate 003's shared-by-key form must still hold."""
    monkeypatch.setattr(QCFG, "CANDIDATE_OPTION", dict(QCFG.CANDIDATE_OPTION))
    monkeypatch.setattr(QCFG, "OPTIONS", copy.deepcopy(QCFG.OPTIONS))
    QCFG.OPTIONS["S3J_COPY"] = copy.deepcopy(QCFG.OPTIONS["S3J"])
    QCFG.CANDIDATE_OPTION["003"] = "S3J_COPY"

    def mutate(payload: dict) -> None:
        _entry(payload, 3)["status"] = "DESIGNED_UNTRAINED"
        _entry(payload, 3)["adapter_sha256"] = None
        _entry(payload, 3)["adapter_manifest_hash"] = None
        _entry(payload, 3)["evaluation_corpus"] = None
        _entry(payload, 3)["evaluation_receipt"] = None

    plane = _mutate(sandbox, mutate)
    report = V.Report()
    V.check_candidate_design(plane, report)
    assert "not dials that are the same" in _messages(report)


# ══════════════════════════════════════════════════════════════════════════════
#  5. The recorded axis is the one the repository builds
# ══════════════════════════════════════════════════════════════════════════════
def test_the_recorded_primary_axis_names_the_dial_and_both_ends(snapshot):
    recorded = snapshot["next_milestone"]["primary_axis"]
    assert "learning_rate" in recorded
    assert REFERENCE_LEARNING_RATE_TEXT in recorded
    assert RULED_LEARNING_RATE_TEXT in recorded


def test_an_axis_recorded_without_the_dial_is_refused(sandbox):
    plane = _mutate(sandbox, lambda p: p["next_milestone"].update(
        primary_axis="PREREGISTERED. Candidate 004 moves one dial from 1e-4 to 5e-5."))
    report = V.Report()
    V.check_next(plane, report)
    assert "does not name" in _messages(report)


@pytest.mark.parametrize("dropped", ["1e-4", "5e-5"])
def test_an_axis_recorded_without_one_of_its_ends_is_refused(sandbox, dropped):
    """An axis with one end is not a measurable claim."""
    plane = _mutate(sandbox, lambda p: p["next_milestone"].update(
        primary_axis=p["next_milestone"]["primary_axis"].replace(dropped, "some value")))
    report = V.Report()
    V.check_next(plane, report)
    assert "does not carry" in _messages(report)


def test_the_historical_axis_assertion_still_applies_with_no_designed_candidate(sandbox):
    """With nothing designed, the field records the last MEASURED axis. The pre-S3U
    assertion is rescoped by state, not deleted."""
    def mutate(payload: dict) -> None:
        payload["candidates"] = [c for c in payload["candidates"]
                                 if c["ordinal"] != 4]
        payload["next_milestone"]["primary_axis"] = "something else entirely"

    plane = _mutate(sandbox, mutate)
    report = V.Report()
    V.check_next(plane, report)
    assert "the preregistered primary axis is not recorded" in _messages(report)


# ══════════════════════════════════════════════════════════════════════════════
#  6. The supersession is narrow, and stayed narrow
# ══════════════════════════════════════════════════════════════════════════════
def test_the_current_rule_still_bars_every_subject_it_is_not_superseding(snapshot):
    joined = " | ".join(snapshot["next_milestone"]["ruled_out"])
    for subject in V.REQUIRED_RULED_OUT_SUBJECTS:
        assert subject in joined, f"the rule no longer bars {subject!r}"


def test_the_learning_rate_supersession_names_the_candidate_it_is_scoped_to(snapshot):
    """Exactly one entry records the supersession, and it names its scope and its value.

    Other entries mention the learning rate as a PROHIBITION -- a compensating dial
    slaved to it, for instance -- and those are not scoped claims and must not be
    required to look like one. What must never appear is an unscoped PERMISSION, which
    is the shape the verifier refuses.
    """
    entries = snapshot["next_milestone"]["ruled_out"]
    superseding = [e for e in entries if "superseded" in e]
    assert len(superseding) == 1, superseding
    assert "candidate 004 only" in superseding[0]
    assert "5e-5" in superseding[0]
    for entry in entries:
        if "learning" in entry and "candidate 004" not in entry:
            assert not any(word in entry.lower()
                           for word in ("allow", "permit", "may ")), entry


def test_a_rewrite_that_drops_a_barred_subject_is_refused(sandbox):
    plane = _mutate(sandbox, lambda p: p["next_milestone"].update(
        ruled_out=[e for e in p["next_milestone"]["ruled_out"]
                   if "max_new_tokens" not in e]))
    report = V.Report()
    V.check_next(plane, report)
    assert "no longer bars" in _messages(report)


def test_an_unscoped_learning_rate_permission_is_refused(sandbox):
    """The supersession is candidate-004-specific and may not silently generalise."""
    plane = _mutate(sandbox, lambda p: p["next_milestone"].update(
        ruled_out=[*p["next_milestone"]["ruled_out"],
                   "any future candidate may choose its own learning rate"]))
    report = V.Report()
    V.check_next(plane, report)
    assert "unscoped learning-rate permission" in _messages(report)


def test_the_historical_ruling_is_not_erased():
    """Generation 8's list stays exactly as generation 8 wrote it."""
    parent = json.loads(
        (REPO / V.SNAPSHOT_DIR /
         "0008-m62-s3t0-termination-observability.json").read_text(encoding="utf-8"))
    assert ("any learning-rate, epoch, rank, alpha or dropout change"
            in parent["next_milestone"]["ruled_out"])


# ══════════════════════════════════════════════════════════════════════════════
#  7. The operator ruling: tracked, checkable, and phrase-free
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def ruling():
    return json.loads((REPO / RULING_PATH).read_text(encoding="utf-8"))


def test_the_ruling_is_recorded_where_the_verifier_looks(ruling):
    assert V.OPERATOR_RULING_S3U == RULING_PATH
    assert (REPO / RULING_PATH).is_file()


def test_the_ruling_does_not_store_the_authorisation_phrase(ruling):
    """A digest proves which decision was given and cannot be spent."""
    assert ruling["ruling_phrase_recorded"] is False
    assert V.SHA256_RE.match(ruling["ruling_phrase_sha256"])
    text = (REPO / RULING_PATH).read_text(encoding="utf-8")
    # Assembled from parts so this file does not itself carry a fragment of the phrase
    # it is proving is absent. The same reason the vendor-attribution scan in the sibling
    # design suite builds its needles rather than spelling them.
    assert ("AUTHORIZE" + "_M62") not in text
    assert not V.TOKEN_LITERAL_RE.search(text)


def test_the_ruling_authorised_a_design_and_nothing_else(ruling):
    assert ruling["scope"] == "DESIGN_ONLY"
    assert ruling["m62_continues"] is True
    excluded = " | ".join(ruling["excluded_from_this_ruling"])
    for subject in ("training", "evaluating", "eval-v5", "eval-v4", "promotion",
                    "second experimental axis", "train-v3"):
        assert subject in excluded


def test_the_ruling_is_an_operator_decision_and_says_so(ruling):
    assert ruling["decision_kind"] == "HUMAN_OPERATOR_RULING"
    assert "RANKED" in ruling["distinct_from_analysis"]
    assert "authorises nothing" in ruling["distinct_from_analysis"]


def test_the_ruling_supersedes_one_clause_and_keeps_the_rest(ruling):
    superseded = ruling["supersedes"]
    assert superseded["at_state_generation"] == 8
    assert superseded["entry"] == (
        "any learning-rate, epoch, rank, alpha or dropout change")
    assert superseded["clause"] == "learning-rate change"
    assert superseded["historical_entry_erased"] is False
    assert sorted(superseded["clauses_untouched"]) == [
        "alpha change", "dropout change", "epoch change", "rank change"]


def test_the_ruling_names_the_experiment_the_repository_actually_builds(ruling):
    assert ruling["subject_candidate"] == CANDIDATE_004_ID
    assert ruling["reference_candidate"] == CANDIDATE_003_ID
    assert ruling["primary_axis"] == "learning_rate"
    assert ruling["reference_value"] == REFERENCE_LEARNING_RATE_TEXT
    assert ruling["ruled_value"] == RULED_LEARNING_RATE_TEXT


def test_the_ruling_check_accepts_the_real_record():
    report = V.Report()
    V.check_operator_ruling(V.load(V.Report()), report)
    assert report.problems == [], _messages(report)


def test_a_ruling_that_stored_the_phrase_is_refused(sandbox):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["ruling_phrase_recorded"] = True
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane_from(sandbox), report)
    assert "withheld" in _messages(report)


def test_a_ruling_with_no_digest_is_refused(sandbox):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["ruling_phrase_sha256"] = ""
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane_from(sandbox), report)
    assert "unauditable" in _messages(report)


@pytest.mark.parametrize("field,value", [
    ("ruled_value", "1e-5"), ("reference_value", "2e-4"),
    ("primary_axis", "lora_rank"), ("subject_candidate", CANDIDATE_003_ID),
])
def test_a_ruling_that_drifted_from_the_repository_is_refused(sandbox, field, value):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload[field] = value
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane_from(sandbox), report)
    assert "the repository builds" in _messages(report)


def test_a_ruling_claiming_a_wider_scope_is_refused(sandbox):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["scope"] = "DESIGN_AND_TRAINING"
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane_from(sandbox), report)
    assert "authorised a DESIGN and nothing else" in _messages(report)


def test_a_missing_ruling_is_refused_for_a_recorded_candidate_004(sandbox):
    (sandbox / RULING_PATH).unlink()
    report = V.Report()
    V.check_operator_ruling(_plane_from(sandbox), report)
    assert "is not a file in this tree" in _messages(report)


def test_a_ruling_that_erased_the_historical_entry_is_refused(sandbox):
    payload = json.loads((sandbox / RULING_PATH).read_text(encoding="utf-8"))
    payload["supersedes"]["historical_entry_erased"] = True
    (sandbox / RULING_PATH).write_bytes(V.canonical_bytes(payload))
    report = V.Report()
    V.check_operator_ruling(_plane_from(sandbox), report)
    assert "remains factual" in _messages(report)


# ══════════════════════════════════════════════════════════════════════════════
#  8. Nothing else moved
# ══════════════════════════════════════════════════════════════════════════════
def test_the_holdout_is_still_frozen_and_unspent(snapshot):
    v5 = next(d for d in snapshot["datasets"]
              if d.get("dataset_id") == "m62-defensive-eval" and d.get("version") == "v5")
    assert v5["status"] == "FROZEN_UNUSED"
    assert v5["spent_by"] is None
    assert v5["manifest_hash"] == EVAL_V5_MANIFEST
    assert v5["pack_hash"] == EVAL_V5_PACK
    assert v5["task_count"] == 36


def test_the_training_corpus_is_unchanged(snapshot):
    parent = json.loads(
        (REPO / V.SNAPSHOT_DIR /
         "0008-m62-s3t0-termination-observability.json").read_text(encoding="utf-8"))
    assert snapshot["datasets"] == parent["datasets"]


def test_the_defect_ledger_is_unchanged_and_d43_is_pinned(snapshot):
    parent = json.loads(
        (REPO / V.SNAPSHOT_DIR /
         "0008-m62-s3t0-termination-observability.json").read_text(encoding="utf-8"))
    assert snapshot["defects"] == parent["defects"]
    assert V.FROZEN_DEFECT_STATUSES["D43"] == "FIXED_OBSERVABILITY_ONLY"
    assert V.FROZEN_DEFECT_STATUSES["D37"] == "FIXED"
    d43 = next(d for d in snapshot["defects"] if d["id"] == "D43")
    assert d43["is_gate"] is False


def test_the_policy_identities_and_invariants_are_unchanged(snapshot):
    parent = json.loads(
        (REPO / V.SNAPSHOT_DIR /
         "0008-m62-s3t0-termination-observability.json").read_text(encoding="utf-8"))
    assert snapshot["policy_identities"] == parent["policy_identities"]
    assert snapshot["frozen_invariants"] == parent["frozen_invariants"]
    assert snapshot["base_model"] == parent["base_model"]
    assert snapshot["archive"] == parent["archive"]


def test_no_authority_was_created(snapshot):
    observation = snapshot["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for key in ("train", "eval", "promotion"):
        assert observation[key] == "NONE_OBSERVED_IN_REPOSITORY"


def test_master_is_still_untouched(snapshot):
    project = snapshot["project"]
    assert project["master_commit"] == (
        "3705114228edef2f665be349c5c4429b7b16777a")
    assert project["merged_into_master"] is False
    assert project["tagged"] is False
    assert project["released"] is False


def test_the_recompacted_limitations_kept_every_fact_they_merged(snapshot):
    """Three groups of near-duplicate limitations were merged to stay inside the
    snapshot budget. Merged, never dropped: the facts must all still be there."""
    joined = " | ".join(snapshot["limitations"])
    for fact in ("6f9f470f", "414ce9e3", "output_root_id",
                 "STALE_STATE_DETECTION is PARTIAL",
                 "deterministic_reproduction_claimed is false",
                 "no dtype control arm", "single observation"):
        assert fact in joined, f"the merge lost {fact!r}"
