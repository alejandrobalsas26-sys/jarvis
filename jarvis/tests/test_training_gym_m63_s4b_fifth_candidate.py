"""V69 M63 S4B — the fifth quality candidate: the same dial, one step further.

WHAT THESE TESTS ARE FOR
------------------------
Candidate 004 was measured against `eval-v6`, came back
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`, and a human decided **HOLD**. A hold is a
decision, not a defect, and candidate 005 is not a second attempt at candidate 004: it is
a NEW identity built against a retained, held reference under a SECOND explicit operator
ruling that moves one dial, 5e-5 -> 2.5e-5, and nothing else.

The entire scientific value of that rests on one claim: **exactly one thing changed.**
These tests exist because that claim is cheap to assert and easy to break:

  * **A second dial.** The option is DERIVED from candidate 004's by expansion, so the
    other eight dials have no second place to drift to; the semantic diff is then
    required to be exactly ``{"learning_rate"}`` -- not "small", not "intended".
  * **A slaved variable.** `alpha/r` stays 32/16 = 2.0 because neither term moves. A
    "compensating" adjustment would be a second axis wearing a justification.
  * **A re-identified control.** Adding candidate 005 must not perturb what candidates
    001-004 hash to, or the comparison is against a configuration that never ran.
  * **An experiment that tests nothing.** A candidate carrying its reference's learning
    rate is a re-run under a new name, and is refused.
  * **A generator that picks its own value.** 2.5e-5 is a recorded human decision, not a
    knob this repository may turn; any other value is refused.
  * **Capability arriving by accident.** No adapter, no run record, no plan token, no
    `train-v3`, no evaluation plan and no holdout material -- asserted, not promised.
  * **A held reference quietly reopened.** Candidate 004 must come out of this milestone
    byte-for-byte the candidate it went in as.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS, CREATES AN OPTIMIZER OR MATERIALISES
A TOKEN. No test in this file loads a tokenizer or reads a byte of any evaluation
holdout, and this file contains no `eval-v4`, `eval-v5` or `eval-v6` task body.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import scripts.build_quality_training_config as QCFG

REPO = Path(__file__).resolve().parents[2]

# ── identities S4B may not move, written independently of what they check ────────────
CANDIDATE_001_ID = "qwen3-06b-lora-quality-live-001"
CANDIDATE_002_ID = "qwen3-06b-lora-quality-live-002"
CANDIDATE_003_ID = "qwen3-06b-lora-quality-live-003"
CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"
CANDIDATE_005_ID = "qwen3-06b-lora-quality-live-005"

TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")
TRAIN_V2_RECORDS = 182
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"

#: Candidate 004's TRACKED portable receipt: the sealed record of what the reference
#: actually trained on and under. Candidate 005 re-derives from disk; equality is the
#: corpus half of the single-axis claim.
REFERENCE_TRAIN_RECEIPT = "state/m62/receipts/qwen3-06b-lora-quality-live-004.train.json"
REFERENCE_EVAL_RECEIPT = "state/m62/receipts/qwen3-06b-lora-quality-live-004.eval.json"

#: The operator ruling, as two independent numbers rather than one restated pair.
REFERENCE_LEARNING_RATE = 5e-5
RULED_LEARNING_RATE = 2.5e-5

DESIGN_DOC = "jarvis/docs/V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md"
RULING = "state/m62/rulings/0002-s4b-candidate005-learning-rate.json"
S3U_RULING = "state/m62/rulings/0001-s3u-candidate004-learning-rate.json"
PINNED_REQUIREMENTS = "jarvis/requirements/training-m62-pinned.txt"

IDENTITY_FIELDS = frozenset({"run_id", "experiment_name", "notes"})
PRIMARY_AXIS = "learning_rate"


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    """A corpus root holding both training versions, plus a shared output root.

    Both configurations are built against the SAME roots on purpose: ``config_hash``
    binds ``output_root_id``, so comparing configurations built under different roots
    would show a difference that is about a filesystem path, not about the model.
    """
    root = tmp_path_factory.mktemp("m63-s4b-train")
    import scripts.build_training_corpus as QC

    QC.build(root, dataset_version="v1")
    QC.build(root, dataset_version="v2")
    return root, root / "runs"


@pytest.fixture(scope="module")
def reference(roots):
    dataset_root, output_root = roots
    return QCFG.build_config(QCFG.CANDIDATE_OPTION["004"], dataset_root=dataset_root,
                             output_root=output_root, candidate="004")


@pytest.fixture(scope="module")
def experiment(roots):
    dataset_root, output_root = roots
    return QCFG.build_config(QCFG.CANDIDATE_OPTION["005"], dataset_root=dataset_root,
                             output_root=output_root, candidate="005")


@pytest.fixture()
def throwaway(monkeypatch):
    """A per-test deep copy of the generator's mutable tables."""
    monkeypatch.setattr(QCFG, "OPTIONS", copy.deepcopy(QCFG.OPTIONS))
    monkeypatch.setattr(QCFG, "CANDIDATES", copy.deepcopy(QCFG.CANDIDATES))
    monkeypatch.setattr(QCFG, "CANDIDATE_OPTION", dict(QCFG.CANDIDATE_OPTION))
    monkeypatch.setattr(QCFG, "CANDIDATE_REASONING", dict(QCFG.CANDIDATE_REASONING))
    monkeypatch.setattr(QCFG, "RULED_LEARNING_RATE", dict(QCFG.RULED_LEARNING_RATE))
    monkeypatch.setattr(QCFG, "CANDIDATE_SINGLE_AXIS", dict(QCFG.CANDIDATE_SINGLE_AXIS))
    return QCFG


def _tracked(rel: str) -> bool:
    return subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                          cwd=REPO, capture_output=True).returncode == 0


def _json(rel: str) -> dict:
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  1. Identity
# ══════════════════════════════════════════════════════════════════════════════
def test_the_fifth_candidate_has_the_next_identity_in_the_quality_lineage():
    assert QCFG.RUN_ID_005 == CANDIDATE_005_ID
    assert QCFG.CANDIDATES["005"]["run_id"] == CANDIDATE_005_ID
    assert QCFG.CANDIDATES["005"]["milestone"] == "S4B"


def test_the_candidate_appears_exactly_once():
    """Once in the generator, once in the option map, once in the axis declaration."""
    ids = [spec["run_id"] for spec in QCFG.CANDIDATES.values()]
    assert ids.count(CANDIDATE_005_ID) == 1
    assert len(ids) == len(set(ids))
    assert sorted(QCFG.CANDIDATES) == ["001", "002", "003", "004", "005"]
    assert [k for k, v in QCFG.CANDIDATE_OPTION.items() if v == "S4B"] == ["005"]


def test_the_fifth_candidate_has_its_own_experiment_name():
    names = [spec["experiment_name"] for spec in QCFG.CANDIDATES.values()]
    assert len(names) == len(set(names))
    assert QCFG.EXPERIMENT_NAME_005 == "m62-s4b-defensive-quality-005"


def test_no_candidate_006_exists():
    """The ruling is candidate-005-only. A sixth identity would be an unruled one."""
    assert "006" not in QCFG.CANDIDATES
    assert not [i for i in (s["run_id"] for s in QCFG.CANDIDATES.values())
                if i.endswith("-006")]
    with pytest.raises(ValueError, match="unknown candidate"):
        QCFG.candidate_spec("006")


# ══════════════════════════════════════════════════════════════════════════════
#  2. The parent, and the ruled value
# ══════════════════════════════════════════════════════════════════════════════
def test_the_parent_is_candidate_004():
    assert QCFG.CANDIDATE_005_REFERENCE_KEY == "004"
    assert QCFG.CANDIDATES["004"]["run_id"] == CANDIDATE_004_ID
    reference_key, _declared = QCFG.CANDIDATE_SINGLE_AXIS["005"]
    assert QCFG.CANDIDATES[reference_key]["run_id"] == CANDIDATE_004_ID


def test_the_reference_rate_is_re_derived_from_the_configuration_004_was_measured_under():
    option = QCFG.CANDIDATE_OPTION[QCFG.CANDIDATE_005_REFERENCE_KEY]
    assert QCFG.OPTIONS[option][PRIMARY_AXIS] == REFERENCE_LEARNING_RATE


def test_the_ruled_rate_is_the_operators_and_is_written_once():
    assert QCFG.CANDIDATE_005_LEARNING_RATE == RULED_LEARNING_RATE
    assert QCFG.RULED_LEARNING_RATE["005"] == RULED_LEARNING_RATE
    assert QCFG.OPTIONS["S4B"][PRIMARY_AXIS] == RULED_LEARNING_RATE


def test_candidate_004_keeps_its_own_ruled_rate():
    """The new ruling is prospective and candidate-005-only; 004's is not rewritten."""
    assert QCFG.RULED_LEARNING_RATE["004"] == REFERENCE_LEARNING_RATE
    assert QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["004"]][PRIMARY_AXIS] == 5e-5


def test_the_ruled_rate_renders_in_the_repositorys_notation():
    assert QCFG.format_learning_rate(RULED_LEARNING_RATE) == "2.5e-5"
    assert QCFG.format_learning_rate(REFERENCE_LEARNING_RATE) == "5e-5"
    assert float(QCFG.format_learning_rate(RULED_LEARNING_RATE)) == RULED_LEARNING_RATE


def test_the_widened_notation_did_not_move_any_existing_rendering():
    """The widening may not re-identify a rate any sealed surface already quotes."""
    for value, text in ((2e-4, "2e-4"), (1e-4, "1e-4"), (5e-5, "5e-5")):
        assert QCFG.format_learning_rate(value) == text
    assert QCFG.MAX_LEARNING_RATE_MANTISSA_DIGITS == 1
    with pytest.raises(ValueError, match="does not round-trip"):
        QCFG.format_learning_rate(1.25e-4)


# ══════════════════════════════════════════════════════════════════════════════
#  3. ONE AXIS — the assertion this whole candidate rests on
# ══════════════════════════════════════════════════════════════════════════════
def test_the_semantic_diff_is_exactly_one_key(reference, experiment):
    """THE assertion. Not 'small', not 'intended' -- exactly ``{"learning_rate"}``."""
    before, after = reference.to_dict(), experiment.to_dict()
    differing = {key for key in set(before) | set(after)
                 if before.get(key) != after.get(key)}
    assert differing - IDENTITY_FIELDS == {PRIMARY_AXIS}


def test_every_raw_difference_is_either_the_axis_or_an_identity(reference, experiment):
    before, after = reference.to_dict(), experiment.to_dict()
    differing = {key for key in set(before) | set(after)
                 if before.get(key) != after.get(key)}
    assert differing == IDENTITY_FIELDS | {PRIMARY_AXIS}


def test_the_option_dial_diff_is_exactly_the_axis():
    assert QCFG.single_axis_diff("005") == frozenset({PRIMARY_AXIS})
    assert len(QCFG.single_axis_diff("005")) == 1


def test_the_dials_are_derived_from_the_reference_not_copied():
    """A copied option can drift; a derived one cannot. Checked structurally."""
    source = Path(QCFG.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Subscript)
                           and getattr(t.value, "id", "") == "OPTIONS"
                           and getattr(t.slice, "value", "") == "S4B"
                           for t in n.targets)]
    assert len(assignments) == 1, "OPTIONS['S4B'] is assigned exactly once"
    built = assignments[0].value
    assert isinstance(built, ast.Dict)
    assert any(key is None for key in built.keys), (
        "OPTIONS['S4B'] does not expand its reference; the dials were retyped")
    literal_dials = {k.value for k in built.keys
                     if isinstance(k, ast.Constant) and k.value in QCFG.OPTION_DIALS}
    assert literal_dials == set(), (
        f"OPTIONS['S4B'] re-types dial(s) {sorted(literal_dials)} as literals")


@pytest.mark.parametrize("dial", [d for d in QCFG.OPTION_DIALS if d != PRIMARY_AXIS])
def test_no_other_dial_moved(dial):
    reference_option = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["004"]]
    experiment_option = QCFG.OPTIONS["S4B"]
    assert experiment_option[dial] == reference_option[dial]


@pytest.mark.parametrize("attribute", [
    "seed", "epochs", "max_steps", "batch_size", "gradient_accumulation_steps",
    "weight_decay", "warmup_ratio", "max_sequence_length", "gradient_checkpointing",
    "dataloader_workers", "logging_interval_steps", "base_model_id",
    "base_model_revision", "tokenizer_id", "tokenizer_revision", "trust_remote_code",
    "base_model_family",
])
def test_the_experiment_keeps_the_references_configuration(reference, experiment,
                                                           attribute):
    assert getattr(experiment, attribute) == getattr(reference, attribute)


def test_the_lora_shape_and_scope_are_the_references(reference, experiment):
    assert experiment.lora.rank == reference.lora.rank == 16
    assert experiment.lora.alpha == reference.lora.alpha == 32
    assert experiment.lora.dropout == reference.lora.dropout == 0.05
    assert experiment.lora.bias == reference.lora.bias
    assert experiment.lora.target_policy == reference.lora.target_policy
    assert experiment.lora.task_type == reference.lora.task_type


def test_alpha_is_not_slaved_because_nothing_asked_it_to_be(reference, experiment):
    """A learning-rate change needs no compensating dial; alpha/r is pinned unchanged."""
    assert experiment.lora.alpha / experiment.lora.rank == 2.0
    assert (experiment.lora.alpha / experiment.lora.rank
            == reference.lora.alpha / reference.lora.rank)


def test_the_policy_surfaces_are_the_references(reference, experiment):
    for attribute in ("precision_policy", "device_policy", "checkpoint_strategy",
                      "validation_strategy", "validation_split", "logging_target",
                      "model_download_policy", "dependency_profile", "method",
                      "reasoning_policy"):
        assert getattr(experiment, attribute) == getattr(reference, attribute), attribute


def test_the_render_policy_is_inherited_by_assignment_not_by_a_second_literal():
    source = Path(QCFG.__file__).read_text(encoding="utf-8")
    assert 'CANDIDATE_REASONING["005"] = CANDIDATE_REASONING[' in source
    assert QCFG.CANDIDATE_REASONING["005"] is QCFG.CANDIDATE_REASONING["004"]


def test_the_experiment_trains_under_the_policy_evaluation_generates_under():
    from training_gym.evaluation.generation import ELIGIBILITY_REASONING_POLICY

    assert QCFG.candidate_reasoning_policy("005") is ELIGIBILITY_REASONING_POLICY


def test_train_time_validation_is_unchanged_and_steers_nothing(reference, experiment):
    assert experiment.train_time_validation_enabled
    assert experiment.train_time_validation_enabled == \
        reference.train_time_validation_enabled
    for absent in ("early_stopping", "load_best_model_at_end"):
        assert not hasattr(experiment, absent)


# ══════════════════════════════════════════════════════════════════════════════
#  4. Non-vacuity — the refusals actually fire
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("dial,value", [
    ("lora_rank", 32), ("lora_alpha", 64), ("lora_dropout", 0.1), ("epochs", 3),
    ("max_steps", 80), ("gradient_accumulation_steps", 4), ("weight_decay", 0.01),
    ("warmup_ratio", 0.2),
])
def test_moving_a_second_dial_is_refused(throwaway, roots, dial, value):
    dataset_root, output_root = roots
    throwaway.OPTIONS["S4B"] = {**throwaway.OPTIONS["S4B"], dial: value}
    with pytest.raises(ValueError, match="second experimental axis"):
        throwaway.build_config("S4B", dataset_root=dataset_root,
                               output_root=output_root, candidate="005")


def test_deleting_a_dial_is_refused_rather_than_skipped(throwaway):
    option = dict(throwaway.OPTIONS["S4B"])
    option.pop("warmup_ratio")
    throwaway.OPTIONS["S4B"] = option
    with pytest.raises(ValueError, match="second experimental axis"):
        throwaway.verify_single_axis("005")


def test_the_experiment_with_the_references_rate_is_refused(throwaway, roots):
    """A candidate that moves no dial is a re-run under a new identity."""
    dataset_root, output_root = roots
    throwaway.OPTIONS["S4B"] = {**throwaway.OPTIONS["S4B"],
                                PRIMARY_AXIS: REFERENCE_LEARNING_RATE}
    with pytest.raises(ValueError, match="tests nothing"):
        throwaway.build_config("S4B", dataset_root=dataset_root,
                               output_root=output_root, candidate="005")


@pytest.mark.parametrize("rate", [1e-5, 2e-5, 3e-5, 4e-5, 1e-4, 2.4e-5])
def test_a_rate_the_operator_did_not_rule_is_refused(throwaway, roots, rate):
    dataset_root, output_root = roots
    throwaway.OPTIONS["S4B"] = {**throwaway.OPTIONS["S4B"], PRIMARY_AXIS: rate}
    with pytest.raises(ValueError, match="operator ruling"):
        throwaway.build_config("S4B", dataset_root=dataset_root,
                               output_root=output_root, candidate="005")


def test_the_sealed_design_is_accepted(throwaway, roots):
    """Non-vacuity in the other direction: the real design must BUILD."""
    dataset_root, output_root = roots
    config = throwaway.build_config("S4B", dataset_root=dataset_root,
                                    output_root=output_root, candidate="005")
    assert config.run_id == CANDIDATE_005_ID
    assert config.learning_rate == RULED_LEARNING_RATE


# ══════════════════════════════════════════════════════════════════════════════
#  5. The corpus — train-v2, unchanged
# ══════════════════════════════════════════════════════════════════════════════
def test_the_experiment_trains_on_train_v2_unchanged(experiment, reference):
    assert QCFG.TRAINING_DATASET_VERSION_005 == "v2"
    assert QCFG.TRAINING_DATASET_VERSION_005 is QCFG.TRAINING_DATASET_VERSION_004
    assert experiment.dataset_reference.dataset_version == "v2"
    assert experiment.dataset_reference.dataset_manifest_hash == \
        reference.dataset_reference.dataset_manifest_hash


def test_the_corpus_identity_matches_the_references_sealed_training_receipt(experiment):
    receipt = _json(REFERENCE_TRAIN_RECEIPT)["training_dataset"]
    mine = experiment.dataset_reference
    assert mine.dataset_manifest_hash == receipt["manifest_hash"] == TRAIN_V2_MANIFEST
    assert mine.train_shard_hash == receipt["train_shard_hash"]
    assert mine.validation_shard_hash == receipt["validation_shard_hash"]
    assert mine.export_manifest_hash == receipt["export_manifest_hash"]
    assert mine.reference_hash() == receipt["reference_hash"]
    assert mine.record_count == TRAIN_V2_RECORDS


def test_there_is_no_third_training_corpus_version():
    source = Path(QCFG.__file__).read_text(encoding="utf-8")
    assert "TRAINING_DATASET_VERSION_005 = TRAINING_DATASET_VERSION_004" in source
    assert '"v3"' not in source
    versions = {spec["dataset_version"] for spec in QCFG.CANDIDATES.values()}
    assert versions == {"v1", "v2"}


def test_the_experiment_never_references_any_holdout(experiment):
    payload = json.dumps(experiment.to_dict())
    for forbidden in ("m62-defensive-eval", "eval-v4", "eval-v5", "eval-v6", "eval-v7",
                      "hidden_evaluation_rows", "holdout"):
        assert forbidden not in payload


# ══════════════════════════════════════════════════════════════════════════════
#  6. The reference is not perturbed
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("candidate,option,run_id", [
    ("001", "B", CANDIDATE_001_ID), ("002", "S3J", CANDIDATE_002_ID),
    ("003", "S3J", CANDIDATE_003_ID), ("004", "S3U", CANDIDATE_004_ID),
])
def test_the_earlier_candidates_keep_their_measured_identities(candidate, option,
                                                               run_id):
    assert QCFG.CANDIDATE_OPTION[candidate] == option
    assert QCFG.CANDIDATES[candidate]["run_id"] == run_id


def test_candidate_003_still_shares_its_option_by_key():
    """S3O's structural guarantee survives two later candidates being added."""
    assert QCFG.CANDIDATE_OPTION["003"] is QCFG.CANDIDATE_OPTION["002"]
    assert QCFG.CANDIDATE_SINGLE_AXIS["003"] == ("002", frozenset())


def test_candidate_004_still_declares_exactly_its_own_axis():
    assert QCFG.CANDIDATE_SINGLE_AXIS["004"] == ("003", frozenset({PRIMARY_AXIS}))
    assert QCFG.single_axis_diff("004") == frozenset({PRIMARY_AXIS})


def test_the_two_configurations_are_distinct_identities(reference, experiment):
    assert reference.config_hash() != experiment.config_hash()
    assert reference.run_id != experiment.run_id


def test_the_experiment_config_is_deterministic(roots):
    dataset_root, output_root = roots
    first = QCFG.build_config("S4B", dataset_root=dataset_root,
                              output_root=output_root, candidate="005")
    second = QCFG.build_config("S4B", dataset_root=dataset_root,
                               output_root=output_root, candidate="005")
    assert first.config_hash() == second.config_hash()
    assert first.to_dict() == second.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
#  7. The operator ruling
# ══════════════════════════════════════════════════════════════════════════════
def test_the_ruling_is_tracked_and_canonical():
    assert (REPO / RULING).is_file()
    assert _tracked(RULING)
    from scripts.verify_m62_control_plane import canonical_bytes

    raw = (REPO / RULING).read_bytes()
    assert raw == canonical_bytes(json.loads(raw.decode("utf-8")))


def test_the_ruling_binds_the_experiment_the_repository_builds():
    payload = _json(RULING)
    assert payload["subject_candidate"] == CANDIDATE_005_ID
    assert payload["reference_candidate"] == CANDIDATE_004_ID
    assert payload["primary_axis"] == PRIMARY_AXIS
    assert payload["reference_value"] == QCFG.format_learning_rate(
        REFERENCE_LEARNING_RATE)
    assert payload["ruled_value"] == QCFG.format_learning_rate(RULED_LEARNING_RATE)
    assert payload["scope"] == "DESIGN_ONLY"
    assert payload["decision_kind"] == "HUMAN_OPERATOR_RULING"


def test_the_ruling_withholds_the_phrase_and_carries_a_re_derivable_digest():
    payload = _json(RULING)
    assert payload["ruling_phrase_recorded"] is False
    digest = payload["ruling_phrase_sha256"]
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    phrase = "\n".join([
        f"candidate = {payload['subject_candidate']}",
        f"parent = {payload['reference_candidate']}",
        f"primary_axis = {payload['primary_axis']}",
        f"reference_value = {payload['reference_value']}",
        f"ruled_value = {payload['ruled_value']}",
    ])
    assert hashlib.sha256(phrase.encode("utf-8")).hexdigest() == digest
    assert "normalisation" in " ".join(payload).lower() or \
        "ruling_phrase_normalisation" in payload


def test_the_ruling_supersedes_narrowly_and_erases_nothing():
    superseded = _json(RULING)["supersedes"]
    assert superseded["historical_entry_erased"] is False
    assert "candidate 005" in superseded["scope"]
    assert "2.5e-5" in superseded["scope"]
    untouched = " | ".join(superseded["clauses_untouched"])
    for clause in ("eval-v7", "eval-v6", "eval-v5", "promotion"):
        assert clause in untouched


def test_the_ruling_authorises_no_execution():
    payload = _json(RULING)
    excluded = " | ".join(payload["excluded_from_this_ruling"]).lower()
    for barred in ("evaluating", "eval-v7", "promotion", "candidate 006", "sweep",
                   "train-v3", "second experimental axis"):
        assert barred in excluded
    from scripts.verify_m62_control_plane import TOKEN_LITERAL_RE

    assert not TOKEN_LITERAL_RE.search((REPO / RULING).read_text(encoding="utf-8"))


def test_the_two_rulings_are_two_decisions():
    """A copied ruling record is the cheapest fake second human decision."""
    new, old = _json(RULING), _json(S3U_RULING)
    for field in ("ruling_id", "ruling_phrase_sha256", "subject_candidate",
                  "reference_candidate", "ruled_value", "milestone"):
        assert new[field] != old[field], field
    assert old["subject_candidate"] == CANDIDATE_004_ID
    assert old["ruled_value"] == "5e-5"


# ══════════════════════════════════════════════════════════════════════════════
#  8. No capability, no artefact, no exam
# ══════════════════════════════════════════════════════════════════════════════
#: The generation S4B wrote. RESCOPED AT S4C, which trained candidate 005 under one
#: authorised TRAIN token and moved it to TRAINED_UNEVALUATED at generation 18.
#:
#: The two assertions below read the live FILESYSTEM for absent artefacts. That was the
#: right reading while the candidate was designed and untrained, and it also asserted,
#: silently, that no authorised run would ever happen -- which is not a property a design
#: suite owns. What S4B owns is that DESIGNING created nothing, and that is a property of
#: generation 17, which is where it is now checked. The design suite must not become a
#: suite that fails the moment the experiment it preregistered is actually run.
S4B_SNAPSHOT = "state/m62/snapshots/0017-m63-s4b-candidate005-designed.json"


def _s4b_entry() -> dict:
    from scripts.verify_m62_control_plane import load_record_store, rehydrate_v3, RECORD_DIR

    stored = json.loads((REPO / S4B_SNAPSHOT).read_text(encoding="utf-8"))
    payload, problems = rehydrate_v3(stored, load_record_store(REPO / RECORD_DIR))
    assert not problems, problems
    return next(c for c in payload["candidates"]
                if c["candidate_id"] == CANDIDATE_005_ID)


def test_designing_the_fifth_candidate_created_no_adapter_and_no_run():
    entry = _s4b_entry()
    assert entry["status"] == "DESIGNED_UNTRAINED"
    assert entry["adapter_sha256"] is None
    assert entry["adapter_manifest_hash"] is None
    assert entry["training_receipt"] is None


def test_designing_the_fifth_candidate_created_no_exam():
    """The one absence that is NOT a moment: it must still hold, and it does.

    RESCOPED AT S4F. What S4B recorded -- that DESIGNING a candidate creates no exam and
    no measurement -- is read from S4B and is unchanged. The receipt clause asserted the
    absence in the LIVE tree, which also asserted that nothing would ever authorise one;
    S4E then spent one human EVAL authority and S4F sealed the result. So the clause now
    says what it always meant: a receipt may exist only if a milestone earned it, and the
    one that does is bound to the candidate and to the corpus that measured it.
    """
    entry = _s4b_entry()
    assert entry["evaluation_corpus"] is None
    assert entry["evaluation_receipt"] is None
    receipts = REPO / "state" / "m62" / "receipts"
    existing = list(receipts.glob(f"{CANDIDATE_005_ID}.eval.json"))
    if existing:
        receipt = json.loads(existing[0].read_text(encoding="utf-8"))
        assert receipt["candidate"]["candidate_id"] == CANDIDATE_005_ID
        assert receipt["holdout"]["dataset_version"] == "v7"
        assert receipt["outcome"]["promotes_model"] is False


def test_no_eval_v7_exists():
    """The session that designs a candidate must not author its own exam."""
    datasets = REPO / "jarvis" / "training_gym_datasets" / "datasets"
    for path in datasets.rglob("*"):
        assert "v7" not in path.name, f"{path} looks like a seventh holdout version"


def test_the_generator_carries_no_spendable_token_literal():
    from scripts.verify_m62_control_plane import TOKEN_LITERAL_RE

    for rel in (str(Path(QCFG.__file__).relative_to(REPO)), DESIGN_DOC, RULING,
                str(Path(__file__).relative_to(REPO))):
        assert not TOKEN_LITERAL_RE.search((REPO / rel).read_text(encoding="utf-8")), rel


def test_the_generator_imports_no_training_framework_at_module_scope():
    tree = ast.parse(Path(QCFG.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & {"torch", "transformers", "peft", "trl", "datasets",
                        "accelerate", "training_gym"}


# ══════════════════════════════════════════════════════════════════════════════
#  8b. THE PREAUTH SURFACE IS TOKEN-SILENT
# ══════════════════════════════════════════════════════════════════════════════
#  A plan hash must be derivable BEFORE a human is asked for authority, and deriving it
#  must not put the single-use authorisation string into console scrollback. The
#  repository's own executor is explicit that `--dry-run` and `--print-plan` DO
#  materialise it, via `TrainingPlan.to_record()` -> `confirmation_token()`. The
#  generator's `--plan` path deliberately reads `plan_hash()` and the blocker list
#  instead, and never calls either.
#
#  Asserted by making the token surfaces EXPLODE. A comment claiming "we do not call it"
#  is not evidence; a run that succeeds while calling it is impossible is.
def test_the_generator_plan_path_never_materialises_a_token(roots, monkeypatch, capsys):
    from training_gym.training.plan import TrainingPlan

    def detonate(self, *args, **kwargs):  # pragma: no cover - it must not be reached
        raise AssertionError("the preauth surface materialised a TRAIN token")

    monkeypatch.setattr(TrainingPlan, "confirmation_token", detonate)
    monkeypatch.setattr(TrainingPlan, "to_record", detonate)
    monkeypatch.setattr(TrainingPlan, "expected_effects", detonate)

    dataset_root, output_root = roots
    code = QCFG.main([
        "--dataset-root", str(dataset_root), "--output-root", str(output_root),
        "--candidate", "005", "--plan"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["status"] == "ok"
    assert len(payload["plan_hash"]) == 64
    assert payload["train_token_created"] is False
    assert payload["train_token_consumed"] is False
    assert payload["training_executed"] is False
    assert payload["trained_anything"] is False
    assert payload["wrote_adapter"] is False


def test_the_plan_output_contains_no_token_shaped_string(roots, capsys):
    from scripts.verify_m62_control_plane import TOKEN_LITERAL_RE

    dataset_root, output_root = roots
    QCFG.main(["--dataset-root", str(dataset_root), "--output-root", str(output_root),
               "--candidate", "005", "--plan"])
    out = capsys.readouterr().out
    assert not TOKEN_LITERAL_RE.search(out)
    assert "TRAIN:" not in out


def test_the_plan_hash_is_reproducible_across_calls(roots, capsys):
    """A plan hash a human is asked to authorise must not move between two readings."""
    dataset_root, output_root = roots
    hashes = []
    for _ in range(2):
        QCFG.main(["--dataset-root", str(dataset_root), "--output-root",
                   str(output_root), "--candidate", "005", "--plan"])
        hashes.append(json.loads(capsys.readouterr().out)["plan_hash"])
    assert hashes[0] == hashes[1]


def test_the_plan_binds_the_ruled_learning_rate(roots, capsys):
    """A plan whose config is not the ruled one is not the plan a token should bind."""
    dataset_root, output_root = roots
    QCFG.main(["--dataset-root", str(dataset_root), "--output-root", str(output_root),
               "--candidate", "005", "--plan"])
    payload = json.loads(capsys.readouterr().out)
    config = QCFG.build_config("S4B", dataset_root=dataset_root,
                               output_root=output_root, candidate="005")
    assert payload["config_hash"] == config.config_hash()
    assert config.learning_rate == RULED_LEARNING_RATE


# ══════════════════════════════════════════════════════════════════════════════
#  8c. THE RUNTIME QUALIFIER AND THE TRAIN PLAN
# ══════════════════════════════════════════════════════════════════════════════
#  `qualify_m62_train_runtime.py` is used as authoritative evidence in the preauth
#  block, so it is tested before it is believed: token-silent, deterministic, honest
#  about what it did not do, and carrying no private path into a digest.
def test_the_runtime_report_is_deterministic_and_body_safe(roots, capsys):
    from scripts import qualify_m62_train_runtime as Q
    from scripts.verify_m62_control_plane import PRIVATE_PATH_RE, TOKEN_LITERAL_RE

    dataset_root, output_root = roots
    seen = []
    for _ in range(2):
        assert Q.main(["--dataset-root", str(dataset_root), "--output-root",
                       str(output_root), "--candidate", "005",
                       "--runtime-report"]) == 0
        out = capsys.readouterr().out
        seen.append(json.loads(out))
        assert not PRIVATE_PATH_RE.findall(out), "the report carries a private path"
        assert not TOKEN_LITERAL_RE.search(out)
    assert seen[0] == seen[1], "the runtime report is not deterministic"
    assert len(seen[0]["runtime_report_sha256"]) == 64
    assert seen[0]["candidate"] == CANDIDATE_005_ID


def test_the_runtime_report_states_what_it_did_not_do():
    from scripts import qualify_m62_train_runtime as Q

    assert Q.CANONICAL_PACKAGES[:3] == ("torch", "transformers", "peft")
    tree = ast.parse(Path(Q.__file__).read_text(encoding="utf-8"))
    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("confirmation_token", "to_record", "expected_effects"):
        assert not [c for c in called if c.endswith(forbidden)], (
            f"the qualifier calls {forbidden}; the preauth surface must stay "
            f"token-silent")
    source = Path(Q.__file__).read_text(encoding="utf-8")
    assert "CONFIRMATION_PREFIX" not in source
    assert "TRAIN:<plan-hash>" in source, "the FORM is named; the token is not built"


def test_the_train_plan_binds_the_experiment_and_stays_token_silent(roots, capsys,
                                                                    monkeypatch):
    from scripts import qualify_m62_train_runtime as Q
    from training_gym.training.plan import TrainingPlan

    def detonate(self, *args, **kwargs):  # pragma: no cover - it must not be reached
        raise AssertionError("the plan surface materialised a TRAIN token")

    monkeypatch.setattr(TrainingPlan, "confirmation_token", detonate)
    monkeypatch.setattr(TrainingPlan, "to_record", detonate)
    monkeypatch.setattr(TrainingPlan, "expected_effects", detonate)

    dataset_root, output_root = roots
    assert Q.main(["--dataset-root", str(dataset_root), "--output-root",
                   str(output_root), "--candidate", "005", "--plan",
                   "--source-head", "0" * 40]) == 0
    plan = json.loads(capsys.readouterr().out)

    assert plan["candidate"] == CANDIDATE_005_ID
    assert plan["parent"] == CANDIDATE_004_ID
    assert plan["train_plan_source_head"] == "0" * 40
    assert plan["human_ruling"]["ruling_id"] == "S4B-001"
    assert plan["human_ruling"]["ruling_phrase_sha256"] == _json(
        RULING)["ruling_phrase_sha256"]
    assert plan["science"]["primary_axis"] == PRIMARY_AXIS
    assert plan["science"]["reference_value"] == "5e-5"
    assert plan["science"]["ruled_value"] == "2.5e-5"
    assert plan["science"]["scientific_diff_count"] == 1
    assert plan["science"]["seed"] == 42
    assert plan["material"]["dataset_manifest_hash"] == TRAIN_V2_MANIFEST
    assert plan["material"]["dataset_record_count"] == TRAIN_V2_RECORDS
    assert plan["material"]["base_model_revision"] == BASE_MODEL_REVISION
    assert plan["expected_artifacts"]["expected_receipt"] == \
        f"state/m62/receipts/{CANDIDATE_005_ID}.train.json"
    assert plan["authority"] == {
        "form": "TRAIN:<plan-hash>", "single_use": True, "created_here": False,
        "consumed_here": False, "token_materialised": False}
    assert len(plan["execution"]["plan_hash"]) == 64
    assert len(plan["plan_document_sha256"]) == 64


def test_the_plan_records_every_dial_that_may_not_move(roots, capsys):
    from scripts import qualify_m62_train_runtime as Q

    dataset_root, output_root = roots
    Q.main(["--dataset-root", str(dataset_root), "--output-root", str(output_root),
            "--candidate", "005", "--plan"])
    unchanged = json.loads(capsys.readouterr().out)["science"]["unchanged_dials"]
    assert set(unchanged) == set(QCFG.OPTION_DIALS) - {PRIMARY_AXIS}
    reference = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["004"]]
    for dial, value in unchanged.items():
        assert reference[dial] == value


def test_the_plan_refuses_to_describe_a_second_axis(roots, throwaway, capsys):
    """Non-vacuity: the plan builder is not a passive transcriber of whatever it finds."""
    from scripts import qualify_m62_train_runtime as Q

    dataset_root, output_root = roots
    throwaway.OPTIONS["S4B"] = {**throwaway.OPTIONS["S4B"], "lora_rank": 32}
    assert Q.main(["--dataset-root", str(dataset_root), "--output-root",
                   str(output_root), "--candidate", "005", "--plan"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


# ══════════════════════════════════════════════════════════════════════════════
#  9. The design document and the pinned runtime
# ══════════════════════════════════════════════════════════════════════════════
def test_the_design_document_is_tracked_and_body_free():
    from scripts.verify_m62_control_plane import (
        HELD_OUT_TASK_IDS,
        PRIVATE_PATH_RE,
        TOKEN_LITERAL_RE,
    )

    assert (REPO / DESIGN_DOC).is_file()
    assert _tracked(DESIGN_DOC)
    text = (REPO / DESIGN_DOC).read_text(encoding="utf-8")
    for version, task_ids in HELD_OUT_TASK_IDS.items():
        named = sorted({tid for tid in task_ids if tid in text})
        assert not named, f"the design document names eval-{version} task(s) {named[:4]}"
    assert not TOKEN_LITERAL_RE.search(text)
    assert not PRIVATE_PATH_RE.findall(text)


def test_the_design_document_preserves_both_standing_conclusions():
    """A permitted experiment is not an indicated one, and the document must say so."""
    text = (REPO / DESIGN_DOC).read_text(encoding="utf-8")
    assert "RECOMMENDED_REMEDY" in text and "TOOLING" in text
    assert "TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY" in text
    assert "HOLD" in text


def test_the_pinned_runtime_is_tracked_and_pins_every_canonical_package():
    assert _tracked(PINNED_REQUIREMENTS)
    lines = [ln.strip() for ln in
             (REPO / PINNED_REQUIREMENTS).read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    pins = dict(ln.split("==", 1) for ln in lines)
    assert pins == {
        "torch": "2.13.0+cpu", "transformers": "5.14.1", "peft": "0.20.0",
        "datasets": "5.0.1", "trl": "1.9.2", "accelerate": "1.14.0",
        "safetensors": "0.8.0", "tokenizers": "0.22.2", "sentencepiece": "0.2.2",
        "numpy": "2.5.2", "jsonschema": "4.26.0", "huggingface-hub": "1.27.0",
    }
    assert all("==" in ln for ln in lines), "a floor is not a pin"
    # No index directive in the file. An `--extra-index-url` applies to EVERY name in it
    # and resolves by highest version across indexes, which is the dependency-confusion
    # shape; the `+cpu` torch build gets its own command against its own single index.
    assert not [ln for ln in lines if ln.startswith("-")], (
        "the pin file carries an index directive; give torch its own command instead")


def test_the_pinned_runtime_agrees_with_the_references_sealed_receipt():
    """The three versions candidate 004's receipt records are the three pinned here."""
    recorded = _json(REFERENCE_TRAIN_RECEIPT)["runtime"]["package_versions"]
    lines = [ln.strip() for ln in
             (REPO / PINNED_REQUIREMENTS).read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    pins = dict(ln.split("==", 1) for ln in lines)
    for package, version in recorded.items():
        assert pins[package] == version, package


def test_no_new_development_surface_carries_assistant_attribution():
    vendor = "Cla" + "ude"
    forbidden = (f"Co-Authored-By: {vendor}", f"{vendor}-Session:",
                 f"Generated by {vendor}", "Generated by " + "AI",
                 "AI-" + "assisted", "assistant-" + "generated")
    for rel in (DESIGN_DOC, RULING, PINNED_REQUIREMENTS,
                str(Path(__file__).relative_to(REPO)),
                "jarvis/scripts/build_quality_training_config.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{rel} carries {needle!r}"
