"""V69 M62 S3Y — generation 14 has room for every truthful ending, proved BEFORE the spend.

Generation 13 closed with 1 028 bytes of headroom against a policy floor of 1 024. Four
bytes. Generation 14 has to carry strictly more truth than that — a spent single-use
holdout that names what spent it, a candidate that stops being unmeasured, and a measured
result — and the S3Y contract bars the escape hatch: no budget may be raised, no defect,
limitation or invariant deleted to make room.

So the capacity proof happens before any EVAL authority is requested and before a single
model weight is loaded. If it failed AFTER eval-v6 were spent, the repository would hold a
measurement it cannot record and a holdout it cannot re-open — and a rerun to fix the
recording is forbidden by the same rules that made the holdout single-use.

WHAT THESE TESTS ARE REALLY FOR
-------------------------------
A capacity gate is trivially passable by a projector that omits the truth. So the byte
assertions are the SMALL half of this file. The larger half is NON-VACUITY: every guard is
shown to FIRE on a mutation that should trip it. A ``check_carried_forward`` that returns
``[]`` because it looks at the wrong surface, a 320-character cap that never sees the entry
that violates it, or a parent guard that accepts any snapshot handed to it would each let a
lossy compaction through while printing PASS.

The four terminal states are projected because an evaluation has more than one truthful
ending and the gate is decided on the LARGEST, not the most convenient.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS, GENERATES A TOKEN, READS A HELD-OUT BODY,
CREATES AN AUTHORITY OR WRITES STATE.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("scripts.build_training_corpus")
from scripts import project_m62_gen14_capacity as CAP  # noqa: E402
from scripts import verify_m62_control_plane as V  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

EVAL_V5_MANIFEST = (
    "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c")
EVAL_V6_MANIFEST = (
    "413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6")
EVAL_V6_PACK = (
    "41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16")
CANDIDATE = "qwen3-06b-lora-quality-live-004"
ADAPTER_SHA256 = (
    "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67")


def _parent() -> dict:
    current = json.loads((REPO / "state/m62/current.json").read_text("utf-8"))
    return json.loads(
        (REPO / current["latest_snapshot_path"]).read_bytes().decode("utf-8"))


def _project(state: str, **kw) -> dict:
    defaults = dict(
        terminal_state=state, subject_commit="0" * 40,
        parent_sha256=CAP.EXPECTED_PARENT_SHA256,
        evaluation_id="m62-s3y-quality-heldout-live",
        plan_digest="0" * 8, report_digest="0" * 8,
        passed=0, skipped=0, failed=0)
    defaults.update(kw)
    return CAP.project_gen14(_parent(), **defaults)


def _entry(payload: dict, surface: str, key: str, value: str) -> dict:
    hits = [x for x in payload[surface] if x.get(key) == value]
    assert len(hits) == 1, f"{surface}: {key}={value}"
    return hits[0]


# ── the parent this projection was written against ───────────────────────────────────
def test_parent_is_generation_13_and_the_expected_bytes() -> None:
    """The projection is a claim about ONE parent. Anything else is a different claim."""
    current = json.loads((REPO / "state/m62/current.json").read_text("utf-8"))
    raw = (REPO / current["latest_snapshot_path"]).read_bytes()
    assert current["state_generation"] == 13
    assert V.sha256_bytes(raw) == CAP.EXPECTED_PARENT_SHA256
    assert current["latest_snapshot_sha256"] == CAP.EXPECTED_PARENT_SHA256
    # The artefact on disk is authoritative. Historical prose reported both 33 783/1 033
    # and 33 788/1 028; the file settles it, and the file is byte-identical to its own
    # canonical serialisation, so there is no third answer.
    assert len(raw) == 33_788
    assert V.canonical_bytes(json.loads(raw.decode("utf-8"))) == raw
    assert V.SNAPSHOT_MAX_BYTES - len(raw) == 1028


def test_the_budget_and_the_floor_are_the_reviewed_ones() -> None:
    """S3Y may not buy room. If either constant moved, this gate proved nothing."""
    assert V.SNAPSHOT_MAX_BYTES == 34_816
    assert CAP.REQUIRED_HEADROOM_BYTES == 1024
    assert CAP.MAX_ENTRY_CHARS == V.MAX_JSON_STRING_CHARS == 320


# ── the gate itself ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_every_terminal_state_fits_with_the_required_headroom(state: str) -> None:
    size, headroom = CAP.measure(_project(state))
    assert size <= V.SNAPSHOT_MAX_BYTES, f"{state}: {size} bytes"
    assert headroom >= CAP.REQUIRED_HEADROOM_BYTES, f"{state}: {headroom} spare"


def test_all_four_states_are_projected_not_just_the_convenient_one() -> None:
    """A gate decided on the smallest ending is not a gate."""
    assert set(CAP.TERMINAL_STATES) == {
        "ELIGIBLE", "NOT_ELIGIBLE", "ABORTED", "DURABILITY_FAILURE"}


def test_progress_still_has_headroom_and_its_budget_did_not_move() -> None:
    assert V.PROGRESS_MAX_BYTES == 40_960
    assert V.PROGRESS_MAX_LINES == 760
    _, byte_head, _, line_head = CAP.progress_headroom()
    assert byte_head > 0 and line_head > 0


# ── the holdout lifecycle, in every ending ───────────────────────────────────────────
@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_eval_v6_is_spent_in_every_ending_including_the_failures(state: str) -> None:
    """The spend boundary is the durable commit, NOT proof a forward pass finished.

    An abort after that point spends the corpus exactly as a clean run does. Recording it
    any other way would manufacture a fresh holdout out of a failure.
    """
    v6 = _entry(_project(state), "datasets", "manifest_hash", EVAL_V6_MANIFEST)
    assert v6["status"] == "USED_IMMUTABLE"
    assert v6["spent_by"]
    assert "candidate 004" in v6["spent_by"]
    assert v6["pack_hash"] == EVAL_V6_PACK
    assert v6["task_count"] == 36
    assert v6["parent_manifest_hash"] == EVAL_V5_MANIFEST
    # The verifier's own coupling: USED_IMMUTABLE must name what spent it.
    assert V.DATASET_TRANSITIONS["FROZEN_UNUSED"]["USED_IMMUTABLE"] == (
        "EVAL_AUTHORITY_CONSUMED")


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_eval_v5_is_untouched_in_every_ending(state: str) -> None:
    """Retirement is an ELIGIBILITY ruling and is never written in as a spend."""
    v5 = _entry(_project(state), "datasets", "version", "v5")
    assert v5["status"] == "FROZEN_UNUSED"
    assert v5["spent_by"] is None
    assert v5["manifest_hash"] == EVAL_V5_MANIFEST


def test_the_projector_refuses_a_parent_whose_v6_is_already_spent() -> None:
    parent = _parent()
    for entry in parent["datasets"]:
        if entry.get("manifest_hash") == EVAL_V6_MANIFEST:
            entry["status"] = "USED_IMMUTABLE"
            entry["spent_by"] = "somebody else"
    with pytest.raises(RuntimeError, match="FROZEN_UNUSED"):
        CAP.project_gen14(parent, terminal_state="ELIGIBLE", subject_commit="0" * 40,
                          parent_sha256=CAP.EXPECTED_PARENT_SHA256,
                          evaluation_id="x", plan_digest="0" * 8,
                          report_digest="0" * 8, passed=0, skipped=0, failed=0)


# ── the candidate lifecycle, in every ending ─────────────────────────────────────────
def test_measured_endings_use_the_canonical_statuses_and_bind_their_evidence() -> None:
    for state, expected in (("ELIGIBLE", "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"),
                            ("NOT_ELIGIBLE", "EVALUATED_NOT_ELIGIBLE")):
        c4 = _entry(_project(state), "candidates", "candidate_id", CANDIDATE)
        assert c4["status"] == expected
        assert expected in V.CANDIDATE_STATES
        # The transition must be one the control plane actually allows, and it must be
        # the one EVAL authority buys.
        assert V.CANDIDATE_TRANSITIONS["TRAINED_UNEVALUATED"][expected] == (
            "EVAL_AUTHORITY_CONSUMED")
        assert c4["evaluation_corpus"] == "m62-defensive-eval v6"
        assert c4["evaluation_receipt"] == CAP.EVAL_RECEIPT_PATH
        assert c4["adapter_sha256"] == ADAPTER_SHA256


@pytest.mark.parametrize("state", ("ABORTED", "DURABILITY_FAILURE"))
def test_an_unfinished_ending_may_not_claim_an_evaluated_state(state: str) -> None:
    """An ``EVALUATED_*`` state REQUIRES a portable receipt and is REDERIVED from it.

    Without one the honest record is the ugly one: a candidate still TRAINED_UNEVALUATED
    beside a holdout that is already spent. The verifier independently refuses a
    TRAINED_UNEVALUATED candidate that names an evaluation corpus, so a projection that
    split the difference would fail there too.
    """
    c4 = _entry(_project(state), "candidates", "candidate_id", CANDIDATE)
    assert c4["status"] == "TRAINED_UNEVALUATED"
    assert c4["evaluation_corpus"] is None
    assert c4["evaluation_receipt"] is None


def test_the_projector_refuses_a_parent_whose_candidate_is_already_evaluated() -> None:
    parent = _parent()
    for entry in parent["candidates"]:
        if entry.get("candidate_id") == CANDIDATE:
            entry["status"] = "EVALUATED_NOT_ELIGIBLE"
    with pytest.raises(RuntimeError, match="TRAINED_UNEVALUATED"):
        CAP.project_gen14(parent, terminal_state="ELIGIBLE", subject_commit="0" * 40,
                          parent_sha256=CAP.EXPECTED_PARENT_SHA256,
                          evaluation_id="x", plan_digest="0" * 8,
                          report_digest="0" * 8, passed=0, skipped=0, failed=0)


def test_no_ending_promotes_anything() -> None:
    for state in CAP.TERMINAL_STATES:
        payload = _project(state)
        statuses = {c["status"] for c in payload["candidates"]}
        assert "PROMOTED" not in statuses
        assert payload["authority_observation"]["promotion"] == (
            "NONE_OBSERVED_IN_REPOSITORY")
        assert payload["authority_observation"]["control_plane_can_grant_authority"] is (
            False)
        assert payload["project"]["merged_into_master"] is False
        assert payload["project"]["tagged"] is False
        assert payload["project"]["released"] is False


# ── the compaction is lossless, and the check that says so actually looks ────────────
@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_the_compaction_is_lossless(state: str) -> None:
    assert CAP.check_carried_forward(_project(state)) == []


def test_the_carried_forward_set_covers_every_topic_s3y_must_preserve() -> None:
    subjects = {substring for substring, _ in CAP.CARRIED_FORWARD}
    for required in ("ELIGIBILITY_USE: RETIRED", "FRESH_V6_REQUIRED",
                     "ORCHESTRATOR BODY-BLINDNESS IS A GATE",
                     "A HOLDOUT AUTHOR IS NEVER ITS EVALUATOR",
                     "PLAN CONSUMED != HOLDOUT SPENT", "RERUN IS FORBIDDEN",
                     "eval-v5", "eval-v6", "eval-v7", "candidate 005", "second",
                     "raising a reviewed budget", "D38 gate", "D39", "5e-5",
                     "structured rows", "response schema", "ATTENTION_ONLY",
                     "train-v3", "max_new_tokens", "grader", "threshold",
                     "refusal detector", "epoch", "rank", "alpha", "dropout"):
        assert required in subjects, required
    assert len(CAP.CARRIED_FORWARD) >= 45


@pytest.mark.parametrize("index", range(len(CAP.CARRIED_FORWARD)))
def test_dropping_any_single_carried_forward_clause_is_detected(index: int) -> None:
    """NON-VACUITY, one clause at a time.

    A coverage check that passes because it reads the wrong surface, or because the
    substring it looks for is a fragment of an unrelated word, is worse than no check: it
    prints PASS over a compaction that dropped a standing prohibition. So every clause is
    removed in turn and the checker is required to notice THAT clause specifically.
    """
    substring, surface = CAP.CARRIED_FORWARD[index]
    mutated = _project("DURABILITY_FAILURE")
    node = mutated
    parts = surface.split(".")
    for part in parts[:-1]:
        node = node[part]
    leaf = parts[-1]
    value = node[leaf]
    if isinstance(value, list):
        node[leaf] = [str(x).replace(substring, "") for x in value]
    else:
        node[leaf] = str(value).replace(substring, "")
    lost = CAP.check_carried_forward(mutated)
    assert any(repr(substring) in problem for problem in lost), (
        f"removing {substring!r} from {surface} was not detected")


# ── the 320-character firewall ───────────────────────────────────────────────────────
@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_no_entry_exceeds_the_body_firewall_cap(state: str) -> None:
    payload = _project(state)
    for surface in ("frozen_invariants", "limitations"):
        for entry in payload[surface]:
            assert len(entry) <= CAP.MAX_ENTRY_CHARS, f"{surface}: {len(entry)}"
    for entry in payload["next_milestone"]["ruled_out"]:
        assert len(entry) <= CAP.MAX_ENTRY_CHARS, f"ruled_out: {len(entry)}"


def test_the_cap_check_fires_on_an_over_long_entry() -> None:
    """NON-VACUITY. The cap is what stops a body arriving in instalments."""
    parent = _parent()
    parent["limitations"] = list(parent["limitations"]) + ["x" * 321]
    with pytest.raises(RuntimeError, match="character control-plane cap"):
        CAP.project_gen14(parent, terminal_state="ELIGIBLE", subject_commit="0" * 40,
                          parent_sha256=CAP.EXPECTED_PARENT_SHA256,
                          evaluation_id="x", plan_digest="0" * 8,
                          report_digest="0" * 8, passed=0, skipped=0, failed=0)


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_no_projection_carries_body_shaped_content(state: str) -> None:
    """The verifier's own recursive body scan, run on the projection."""
    assert V._body_shaped(_project(state), f"gen14/{state}") == []


# ── the surfaces the verifier independently re-checks ────────────────────────────────
@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_every_required_ruled_out_subject_survives(state: str) -> None:
    joined = " | ".join(_project(state)["next_milestone"]["ruled_out"])
    missing = [s for s in V.REQUIRED_RULED_OUT_SUBJECTS if s not in joined]
    assert missing == []


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_no_ruled_out_entry_reads_as_an_unscoped_learning_rate_permission(
        state: str) -> None:
    for entry in _project(state)["next_milestone"]["ruled_out"]:
        if "learning" in entry.lower() and "candidate 004" not in entry:
            assert not any(word in entry.lower()
                           for word in ("allow", "permit", "may "))


def test_a_measured_ending_records_the_axis_the_verifier_will_ask_for() -> None:
    """With no unmeasured candidate left, ``_check_primary_axis`` takes its other branch.

    It then requires the recorded axis to name the render transition, so a generation 14
    that recorded only the learning-rate dial would fail the verifier it just passed.
    """
    for state in ("ELIGIBLE", "NOT_ELIGIBLE"):
        payload = _project(state)
        assert not [c for c in payload["candidates"]
                    if c["status"] in ("DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED")]
        axis = payload["next_milestone"]["primary_axis"]
        assert "MODEL_DEFAULT" in axis and "DISABLED" in axis


@pytest.mark.parametrize("state", ("ABORTED", "DURABILITY_FAILURE"))
def test_an_open_axis_still_names_its_dial_and_both_ends(state: str) -> None:
    axis = _project(state)["next_milestone"]["primary_axis"]
    assert "learning_rate" in axis
    assert "1e-4" in axis and "5e-5" in axis


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_the_retirement_markers_stay_on_the_invariant_surface(state: str) -> None:
    joined = " | ".join(_project(state)["frozen_invariants"])
    assert V.RETIREMENT_MARKER in joined
    assert V.FRESH_HOLDOUT_MARKER in joined


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_the_next_milestone_never_requests_authority_naming_a_retired_holdout(
        state: str) -> None:
    nxt = _project(state)["next_milestone"]
    for entry in nxt["authority_required"]:
        assert "eval-v5" not in entry
    assert "eval-v5" not in nxt["evaluation_holdout"] or "RETIRED" in (
        nxt["evaluation_holdout"])


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_nothing_is_deleted_to_make_room(state: str) -> None:
    """S3Y bars buying room by forgetting. Counts may only hold or grow."""
    parent, payload = _parent(), _project(state)
    assert len(payload["frozen_invariants"]) >= len(parent["frozen_invariants"])
    assert len(payload["defects"]) >= len(parent["defects"])
    assert {d["id"] for d in parent["defects"]} <= {d["id"] for d in payload["defects"]}
    d44 = _entry(payload, "defects", "id", "D44")
    assert d44["status"] == "FIXED" and d44["is_gate"] is True
    assert len(payload["datasets"]) == len(parent["datasets"])
    assert len(payload["candidates"]) == len(parent["candidates"])


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_the_projection_validates_against_the_tracked_snapshot_schema(
        state: str) -> None:
    problems: list[str] = []
    V._validate_node(V.snapshot_schema(), _project(state), "$", problems)
    assert problems == []


@pytest.mark.parametrize("state", CAP.TERMINAL_STATES)
def test_the_generation_and_its_parent_pointer_are_the_next_link_in_the_chain(
        state: str) -> None:
    payload = _project(state)
    assert payload["state_generation"] == 14
    assert payload["parent_snapshot_sha256"] == CAP.EXPECTED_PARENT_SHA256
    assert payload["subject_state_milestone"] == "S3Y"
    assert payload["generation_label"] == CAP.GEN14_LABELS[state]
    assert payload["schema_version"] == "m62.control_plane.1"


# ── the emitter fails closed ─────────────────────────────────────────────────────────
def test_emit_refuses_a_stand_in_digest(tmp_path: Path) -> None:
    target = tmp_path / "gen14.json"
    code = CAP.main(["--terminal-state", "ELIGIBLE", "--subject-commit", "a" * 40,
                     "--emit", str(target)])
    assert code == 1
    assert not target.exists()


def test_emit_refuses_a_stand_in_subject_commit(tmp_path: Path) -> None:
    target = tmp_path / "gen14.json"
    code = CAP.main(["--terminal-state", "ELIGIBLE", "--plan-digest", "abcdef12",
                     "--report-digest", "34567890", "--emit", str(target)])
    assert code == 1
    assert not target.exists()


def test_emit_refuses_without_a_single_terminal_state(tmp_path: Path) -> None:
    target = tmp_path / "gen14.json"
    code = CAP.main(["--subject-commit", "a" * 40, "--plan-digest", "abcdef12",
                     "--report-digest", "34567890", "--emit", str(target)])
    assert code == 1
    assert not target.exists()


def test_a_wrong_parent_is_refused_before_anything_is_measured(tmp_path: Path) -> None:
    decoy = tmp_path / "decoy.json"
    decoy.write_bytes(V.canonical_bytes({"state_generation": 13}))
    assert CAP.main(["--parent", str(decoy)]) == 2


def test_the_cli_reports_pass_without_writing_anything(tmp_path: Path) -> None:
    before = sorted(p.name for p in (REPO / "state/m62/snapshots").iterdir())
    assert CAP.main([]) == 0
    after = sorted(p.name for p in (REPO / "state/m62/snapshots").iterdir())
    assert before == after
    current = json.loads((REPO / "state/m62/current.json").read_text("utf-8"))
    assert current["state_generation"] == 13


# ── what the preflight has NOT done ──────────────────────────────────────────────────
def test_the_capacity_proof_spent_nothing() -> None:
    """The whole point of proving capacity FIRST: at this moment nothing has happened."""
    live = _parent()
    v6 = _entry(live, "datasets", "manifest_hash", EVAL_V6_MANIFEST)
    assert v6["status"] == "FROZEN_UNUSED" and v6["spent_by"] is None
    c4 = _entry(live, "candidates", "candidate_id", CANDIDATE)
    assert c4["status"] == "TRAINED_UNEVALUATED"
    assert c4["evaluation_corpus"] is None and c4["evaluation_receipt"] is None
    assert live["authority_observation"]["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert not (REPO / CAP.EVAL_RECEIPT_PATH).exists()
