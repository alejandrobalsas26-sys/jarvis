"""V69 M62 S3U — the fourth quality candidate: one dial, and a human decision behind it.

WHAT THESE TESTS ARE FOR
------------------------
Candidate 003 was measured and is `EVALUATED_NOT_ELIGIBLE`. Against its own
simultaneously-measured baseline it improved task success and reward and fixed three
security findings, and it produced six output ceilings where the baseline produced two.
S3S.1 then established BODY-FREE that both observed ceiling phenotypes already occur in
the base model at rank 0: the adapter cured the base model's two and produced six of its
own. It did not invent the failure family. It redistributed which inputs trigger it.

That is what makes "how far the update moves the base model" the ranked hypothesis, and
the learning rate is the one dial never varied across three candidates. A human operator
ruled that candidate 004 may move it, from 1e-4 to 5e-5, and nothing else.

The entire scientific value of that candidate rests on one claim: **exactly one thing
changed.** These tests are mostly about that claim being MEASURED rather than asserted,
and about the ways it could be quietly false:

  * **A second dial.** The option is DERIVED from candidate 003's by expansion, so the
    other eight dials have no second place to drift to; the semantic diff is then
    required to be exactly ``{"learning_rate"}`` — not "small", not "intended".
  * **A slaved variable.** A rank change would have needed alpha moved with it. A
    learning-rate change needs nothing moved with it, so ``alpha/r`` is pinned unchanged
    and a "compensating" adjustment is refused as the second axis it would be.
  * **A re-identified control.** Adding candidate 004 must not perturb what candidates
    001, 002 and 003 hash to, or the comparison is against a configuration that never ran.
  * **An experiment that tests nothing.** A candidate carrying its reference's learning
    rate is a re-run under a new name, and is refused.
  * **A generator that picks its own value.** 5e-5 is a recorded human decision, not a
    knob this repository may turn; any other value is refused.
  * **Capability arriving by accident.** No adapter, no run record, no plan token, no
    ``train-v3``, no evaluation plan, and no eval-v5 material — asserted, not promised.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS, CREATES AN OPTIMIZER OR GENERATES A
TOKEN. No test in this file loads a tokenizer or reads a byte of any evaluation holdout.

This file reads no ``eval-v4`` or ``eval-v5`` task body and contains none.
"""
from __future__ import annotations

import ast
import copy
import json
import subprocess
from pathlib import Path

import pytest

import scripts.build_quality_training_config as QCFG

REPO = Path(__file__).resolve().parents[2]

# ── the identities S3U may not move ──────────────────────────────────────────────────
#: Written here independently of the artefacts being checked. A test that reads its
#: expected value out of the thing under test proves nothing.
CANDIDATE_001_ID = "qwen3-06b-lora-quality-live-001"
CANDIDATE_002_ID = "qwen3-06b-lora-quality-live-002"
CANDIDATE_003_ID = "qwen3-06b-lora-quality-live-003"
CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"

TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"

#: Quoted from candidate 003's TRACKED portable training receipt, which is the sealed
#: record of the material the reference adapter actually trained on. Candidate 004
#: re-derives these from disk; equality is the corpus half of the single-axis claim.
REFERENCE_TRAIN_RECEIPT = "state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json"

#: The operator ruling, as two independent numbers rather than one restated pair.
REFERENCE_LEARNING_RATE = 1e-4
RULED_LEARNING_RATE = 5e-5

#: The deep evidence the design is recorded in.
DESIGN_DOC = "jarvis/docs/V69_M62_S3U_CANDIDATE004_SINGLE_AXIS_DESIGN.md"

#: Config fields that may differ between the reference and the experiment because they
#: are consequences of the candidate's IDENTITY, not of its training behaviour. Naming
#: them explicitly is the point: an unlisted difference is a second axis.
IDENTITY_FIELDS = frozenset({"run_id", "experiment_name", "notes"})

#: The single axis the operator ruled.
PRIMARY_AXIS = "learning_rate"


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    """A corpus root holding both training versions, plus a shared output root.

    Both configurations are built against the SAME roots on purpose: ``config_hash``
    binds ``output_root_id``, so comparing configurations built under different roots
    would show a difference that is about a filesystem path and not about the model.
    """
    root = tmp_path_factory.mktemp("m62-s3u-train")
    import scripts.build_training_corpus as QC

    QC.build(root, dataset_version="v1")
    QC.build(root, dataset_version="v2")
    return root, root / "runs"


@pytest.fixture(scope="module")
def reference(roots):
    dataset_root, output_root = roots
    return QCFG.build_config(QCFG.CANDIDATE_OPTION["003"], dataset_root=dataset_root,
                             output_root=output_root, candidate="003")


@pytest.fixture(scope="module")
def experiment(roots):
    dataset_root, output_root = roots
    return QCFG.build_config(QCFG.CANDIDATE_OPTION["004"], dataset_root=dataset_root,
                             output_root=output_root, candidate="004")


@pytest.fixture()
def throwaway(monkeypatch):
    """A per-test deep copy of the generator's mutable tables.

    Every non-vacuity test below mutates a dial and requires a refusal. They must not be
    able to mutate the module the rest of the session imports, and they must not depend
    on each other's clean-up, so the tables are replaced wholesale and restored by
    ``monkeypatch`` regardless of how the test ends.
    """
    monkeypatch.setattr(QCFG, "OPTIONS", copy.deepcopy(QCFG.OPTIONS))
    monkeypatch.setattr(QCFG, "CANDIDATES", copy.deepcopy(QCFG.CANDIDATES))
    monkeypatch.setattr(QCFG, "CANDIDATE_OPTION", dict(QCFG.CANDIDATE_OPTION))
    monkeypatch.setattr(QCFG, "CANDIDATE_REASONING", dict(QCFG.CANDIDATE_REASONING))
    monkeypatch.setattr(QCFG, "RULED_LEARNING_RATE", dict(QCFG.RULED_LEARNING_RATE))
    monkeypatch.setattr(QCFG, "CANDIDATE_SINGLE_AXIS",
                        dict(QCFG.CANDIDATE_SINGLE_AXIS))
    return QCFG


def _tracked(rel: str) -> bool:
    return subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                          cwd=REPO, capture_output=True).returncode == 0


# ══════════════════════════════════════════════════════════════════════════════
#  1. Candidate identity: derived, non-colliding, consistent with convention
# ══════════════════════════════════════════════════════════════════════════════
def test_the_fourth_candidate_has_the_next_identity_in_the_quality_lineage():
    """The ordinal is per-LINEAGE, and the convention is read off the lineage itself."""
    assert QCFG.CANDIDATES["004"]["run_id"] == CANDIDATE_004_ID
    assert QCFG.RUN_ID_004 == CANDIDATE_004_ID
    stem = "qwen3-06b-lora-quality-live-"
    assert [QCFG.CANDIDATES[key]["run_id"] for key in ("001", "002", "003", "004")] == [
        f"{stem}001", f"{stem}002", f"{stem}003", f"{stem}004"]


def test_the_candidate_identity_is_deterministic():
    """Nothing about the identity is a function of the host, the clock or a path."""
    source = (REPO / "jarvis/scripts/build_quality_training_config.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "RUN_ID_004" for t in node.targets):
            assert isinstance(node.value, ast.Constant), "the identity is a literal"
            break
    else:  # pragma: no cover - the assignment exists or the test above already failed
        pytest.fail("RUN_ID_004 is not a module-level assignment")


def test_no_candidate_identity_or_ordinal_collides():
    ids = [spec["run_id"] for spec in QCFG.CANDIDATES.values()]
    assert len(ids) == len(set(ids))
    assert CANDIDATE_004_ID not in {CANDIDATE_001_ID, CANDIDATE_002_ID, CANDIDATE_003_ID}
    assert sorted(QCFG.CANDIDATES) == ["001", "002", "003", "004"]


def test_the_fourth_candidate_has_its_own_experiment_name():
    """A shared experiment name would let two runs write into one another's history."""
    names = [spec["experiment_name"] for spec in QCFG.CANDIDATES.values()]
    assert len(names) == len(set(names))
    assert QCFG.EXPERIMENT_NAME_004 == "m62-s3u-defensive-quality-004"


def test_an_unknown_candidate_is_still_a_refusal():
    """Fail-closed survived the addition: a typo is a refusal, never a run."""
    with pytest.raises(ValueError, match="unknown candidate"):
        QCFG.candidate_spec("005")


# ══════════════════════════════════════════════════════════════════════════════
#  2. The reference is re-derived, not quoted
# ══════════════════════════════════════════════════════════════════════════════
def test_the_reference_candidate_is_candidate_003():
    assert QCFG.CANDIDATE_004_REFERENCE_KEY == "003"
    assert QCFG.CANDIDATES[QCFG.CANDIDATE_004_REFERENCE_KEY]["run_id"] == CANDIDATE_003_ID


def test_the_reference_learning_rate_is_re_derived_from_the_qualified_configuration():
    """1e-4 is read out of the option candidate 003 was actually configured under."""
    option = QCFG.CANDIDATE_OPTION[QCFG.CANDIDATE_004_REFERENCE_KEY]
    assert QCFG.OPTIONS[option]["learning_rate"] == REFERENCE_LEARNING_RATE


def test_the_ruled_learning_rate_is_the_operators_and_is_written_once():
    assert QCFG.CANDIDATE_004_LEARNING_RATE == RULED_LEARNING_RATE
    assert QCFG.RULED_LEARNING_RATE["004"] == RULED_LEARNING_RATE
    assert QCFG.OPTIONS["S3U"]["learning_rate"] == RULED_LEARNING_RATE


def test_the_learning_rate_notation_round_trips():
    """A rate printed into prose is derived from the number, never typed beside it."""
    assert QCFG.format_learning_rate(REFERENCE_LEARNING_RATE) == "1e-4"
    assert QCFG.format_learning_rate(RULED_LEARNING_RATE) == "5e-5"
    assert QCFG.format_learning_rate(2e-4) == "2e-4"
    for value in (REFERENCE_LEARNING_RATE, RULED_LEARNING_RATE, 2e-4):
        assert float(QCFG.format_learning_rate(value)) == value


def test_a_rate_the_notation_cannot_represent_is_refused():
    """Refusing beats rounding: a printed rate that is not the rate is a false claim."""
    with pytest.raises(ValueError, match="does not round-trip"):
        QCFG.format_learning_rate(1.5e-4)


# ══════════════════════════════════════════════════════════════════════════════
#  3. ONE AXIS — the assertion this whole candidate rests on
# ══════════════════════════════════════════════════════════════════════════════
def test_the_semantic_diff_is_exactly_one_key(reference, experiment):
    """THE assertion. Not 'small', not 'intended' — exactly ``{"learning_rate"}``."""
    before, after = reference.to_dict(), experiment.to_dict()
    differing = {key for key in set(before) | set(after)
                 if before.get(key) != after.get(key)}
    assert differing - IDENTITY_FIELDS == {PRIMARY_AXIS}


def test_every_raw_difference_is_either_the_axis_or_an_identity(reference, experiment):
    """The complement of the test above: nothing differs that is not accounted for."""
    before, after = reference.to_dict(), experiment.to_dict()
    differing = {key for key in set(before) | set(after)
                 if before.get(key) != after.get(key)}
    assert differing == IDENTITY_FIELDS | {PRIMARY_AXIS}


def test_the_option_dial_diff_is_exactly_the_axis():
    assert QCFG.single_axis_diff("004") == frozenset({PRIMARY_AXIS})
    assert len(QCFG.single_axis_diff("004")) == 1


def test_the_dials_are_derived_from_the_reference_not_copied():
    """A copied option is a thing that can drift; a derived one cannot.

    Checked structurally rather than by comparing numbers: every dial except the axis
    must be the SAME OBJECT the reference option holds, which a re-typed literal would
    not be for the container and could not be for a mutable value.
    """
    reference_option = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["003"]]
    experiment_option = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["004"]]
    assert QCFG.S3U_REFERENCE_OPTION == QCFG.CANDIDATE_OPTION["003"] == "S3J"
    for dial in QCFG.OPTION_DIALS:
        if dial == PRIMARY_AXIS:
            continue
        assert experiment_option[dial] == reference_option[dial]
    assert experiment_option[PRIMARY_AXIS] != reference_option[PRIMARY_AXIS]


def test_the_declared_relation_matches_the_measured_one():
    """The generator's declaration and its own arithmetic are required to agree."""
    reference_key, declared = QCFG.CANDIDATE_SINGLE_AXIS["004"]
    assert reference_key == "003"
    assert declared == frozenset({PRIMARY_AXIS})
    assert QCFG.single_axis_diff("004") == declared


def test_candidate_003_still_shares_its_option_by_key():
    """S3U widened the mechanism; it did not weaken candidate 003's stronger form."""
    reference_key, declared = QCFG.CANDIDATE_SINGLE_AXIS["003"]
    assert (reference_key, declared) == ("002", frozenset())
    assert QCFG.CANDIDATE_OPTION["003"] == QCFG.CANDIDATE_OPTION["002"] == "S3J"


@pytest.mark.parametrize("field", [
    "method", "base_model_id", "base_model_revision", "base_model_parameters_b",
    "base_model_family", "tokenizer_id", "tokenizer_revision", "trust_remote_code",
    "dataset_reference", "training_split", "validation_split",
    "hidden_evaluation_reference", "security_regression_reference", "output_root_id",
    "seed", "epochs", "max_steps", "batch_size", "gradient_accumulation_steps",
    "weight_decay", "warmup_ratio", "max_sequence_length",
    "gradient_checkpointing", "precision_policy", "device_policy", "lora",
    "dataloader_workers", "checkpoint_strategy", "checkpoint_interval_steps",
    "max_checkpoints", "logging_target", "logging_interval_steps",
    "model_download_policy", "dependency_profile", "resource_policy",
    "created_at_utc", "validation_strategy", "schema_version", "reasoning_policy",
])
def test_no_other_field_moved(reference, experiment, field):
    """Field by field, every canonical key that is not the axis or an identity."""
    assert reference.to_dict()[field] == experiment.to_dict()[field]


@pytest.mark.parametrize("attribute,expected", [
    ("seed", 42), ("epochs", 2), ("max_steps", 40), ("batch_size", 1),
    ("gradient_accumulation_steps", 8), ("learning_rate", RULED_LEARNING_RATE),
    ("weight_decay", 0.0), ("warmup_ratio", 0.1), ("max_sequence_length", 512),
    ("gradient_checkpointing", False), ("dataloader_workers", 0),
    ("max_checkpoints", 1), ("logging_interval_steps", 5), ("trust_remote_code", False),
])
def test_the_experiment_keeps_the_references_hyperparameters(experiment, attribute,
                                                             expected):
    assert getattr(experiment, attribute) == expected


# ══════════════════════════════════════════════════════════════════════════════
#  4. No slaved variable — the difference from the rank hypothesis
# ══════════════════════════════════════════════════════════════════════════════
def test_the_lora_shape_and_scope_are_the_references(experiment, reference):
    """ATTENTION_AND_MLP, rank 16, alpha 32, dropout 0.05 — none of them the axis."""
    assert experiment.lora.target_policy.value == "attention_and_mlp"
    assert list(experiment.lora.target_modules) == [
        "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    assert (experiment.lora.rank, experiment.lora.alpha, experiment.lora.dropout) == (
        16, 32, 0.05)
    assert experiment.lora.bias.value == "none"
    assert experiment.lora.task_type == "CAUSAL_LM"
    assert experiment.lora.to_dict() == reference.lora.to_dict()


def test_alpha_is_not_slaved_because_nothing_asked_it_to_be():
    """A rank change needs alpha moved with it. A learning-rate change needs nothing.

    ``alpha/r`` is therefore identical, and it is identical because neither term moved —
    not because a compensating adjustment happened to land on the same ratio.
    """
    before = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["003"]]
    after = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["004"]]
    assert after["lora_rank"] == before["lora_rank"] == 16
    assert after["lora_alpha"] == before["lora_alpha"] == 32
    assert after["lora_alpha"] / after["lora_rank"] == before["lora_alpha"] / before[
        "lora_rank"] == 2.0
    assert after["lora_dropout"] == before["lora_dropout"] == 0.05
    assert after["epochs"] == before["epochs"] == 2


def test_the_device_precision_validation_and_checkpoint_behaviour_are_the_references(
        experiment, reference):
    assert experiment.device_policy.value == reference.device_policy.value == "cpu"
    assert experiment.precision_policy.value == reference.precision_policy.value == "fp32"
    assert experiment.validation_strategy.value == "epoch"
    assert experiment.checkpoint_strategy.value == "no"
    assert experiment.checkpoint_interval_steps == 0


def test_early_stopping_and_load_best_are_not_config_surface():
    """Both are backend behaviour, hard-coded off. They cannot drift into an axis."""
    source = (REPO / "jarvis/training_gym/training/backends/transformers_peft.py"
              ).read_text(encoding="utf-8")
    assert "load_best_model_at_end=False" in source.replace(" ", "")
    from training_gym.training.config import TrainingConfig

    assert not hasattr(TrainingConfig, "early_stopping")
    assert not hasattr(TrainingConfig, "load_best_model_at_end")


# ══════════════════════════════════════════════════════════════════════════════
#  5. Non-vacuity — the refusals, exercised on throwaway copies only
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("dial,value", [
    ("epochs", 3), ("lora_rank", 8), ("lora_alpha", 16), ("lora_dropout", 0.1),
    ("max_steps", 27), ("gradient_accumulation_steps", 4), ("weight_decay", 0.01),
    ("warmup_ratio", 0.2),
])
def test_moving_a_second_dial_is_refused(throwaway, roots, dial, value):
    """Every dial the operator ruling did NOT supersede, one at a time."""
    dataset_root, output_root = roots
    throwaway.OPTIONS["S3U"][dial] = value
    with pytest.raises(ValueError, match="declared single axis"):
        throwaway.verify_single_axis("004")
    with pytest.raises(ValueError, match="declared single axis"):
        throwaway.build_config(throwaway.CANDIDATE_OPTION["004"],
                               dataset_root=dataset_root, output_root=output_root,
                               candidate="004")


def test_deleting_a_dial_is_refused_rather_than_skipped(throwaway):
    """A missing key is a difference, not an absence of one."""
    del throwaway.OPTIONS["S3U"]["weight_decay"]
    with pytest.raises(ValueError, match="declared single axis"):
        throwaway.verify_single_axis("004")


def test_the_experiment_with_the_references_learning_rate_is_refused(throwaway, roots):
    """A candidate that moves no dial is a re-run under a new identity.

    The refusal must name the RIGHT defect. Setting the axis back to its reference's
    value also empties the dial diff, so a diff-first check would report "no axis moved"
    — true, and the less useful of the two things wrong here.
    """
    dataset_root, output_root = roots
    throwaway.OPTIONS["S3U"]["learning_rate"] = REFERENCE_LEARNING_RATE
    with pytest.raises(ValueError, match="tests nothing"):
        throwaway.verify_single_axis("004")
    with pytest.raises(ValueError, match="tests nothing"):
        throwaway.build_config(throwaway.CANDIDATE_OPTION["004"],
                               dataset_root=dataset_root, output_root=output_root,
                               candidate="004")


@pytest.mark.parametrize("rate", [2e-4, 1e-5, 2.5e-5, 5e-4])
def test_a_learning_rate_the_operator_did_not_rule_is_refused(throwaway, roots, rate):
    """5e-5 is a recorded human decision, not a value this repository may choose."""
    dataset_root, output_root = roots
    throwaway.OPTIONS["S3U"]["learning_rate"] = rate
    with pytest.raises(ValueError, match="operator ruling"):
        throwaway.build_config(throwaway.CANDIDATE_OPTION["004"],
                               dataset_root=dataset_root, output_root=output_root,
                               candidate="004")


def test_an_option_that_merely_agrees_is_not_an_option_that_is_shared(throwaway):
    """Candidate 003's stronger form: dials that agree today are not the same dials."""
    throwaway.OPTIONS["S3J_COPY"] = copy.deepcopy(throwaway.OPTIONS["S3J"])
    throwaway.CANDIDATE_OPTION["003"] = "S3J_COPY"
    with pytest.raises(ValueError, match="not dials that are the same"):
        throwaway.verify_single_axis("003")


def test_the_sealed_design_is_accepted(throwaway, roots):
    """The complement of every refusal above: 5e-5 with all else equal PASSES."""
    dataset_root, output_root = roots
    throwaway.verify_single_axis("004")
    config = throwaway.build_config(throwaway.CANDIDATE_OPTION["004"],
                                    dataset_root=dataset_root, output_root=output_root,
                                    candidate="004")
    assert config.learning_rate == RULED_LEARNING_RATE
    assert config.run_id == CANDIDATE_004_ID


def test_a_candidate_with_no_declared_relation_is_not_silently_checked():
    """001 and 002 declare none and must not acquire one by accident."""
    assert "001" not in QCFG.CANDIDATE_SINGLE_AXIS
    assert "002" not in QCFG.CANDIDATE_SINGLE_AXIS
    QCFG.verify_single_axis("001")  # a no-op, not a pass and not a failure
    with pytest.raises(ValueError, match="declares no single-axis relation"):
        QCFG.single_axis_diff("001")


# ══════════════════════════════════════════════════════════════════════════════
#  6. The control was not re-identified
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("candidate,expected_option,expected_rate", [
    ("001", "B", 2e-4), ("002", "S3J", 1e-4), ("003", "S3J", 1e-4),
])
def test_the_earlier_candidates_keep_their_measured_dials(candidate, expected_option,
                                                          expected_rate):
    assert QCFG.CANDIDATE_OPTION[candidate] == expected_option
    assert QCFG.OPTIONS[expected_option]["learning_rate"] == expected_rate


def test_the_earlier_candidates_keep_their_measured_identities(roots, reference):
    """Adding candidate 004 must not perturb what 001, 002 and 003 hash to.

    Pinned through ``notes``, which ``config_hash`` covers, plus the corpus identity and
    the render policy — root-independent facts, unlike ``config_hash`` itself, which
    binds ``output_root_id`` and would be a claim about one filesystem.
    """
    from training_gym.training.chat_render import ReasoningPolicy

    dataset_root, output_root = roots
    first = QCFG.build_config("B", dataset_root=dataset_root, output_root=output_root)
    second = QCFG.build_config(QCFG.CANDIDATE_OPTION["002"], dataset_root=dataset_root,
                               output_root=output_root, candidate="002")
    assert first.run_id == CANDIDATE_001_ID
    assert first.dataset_reference.dataset_version == "v1"
    assert first.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert first.notes == (
        "M62 S3G first quality candidate, option B (recommended). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")
    assert second.run_id == CANDIDATE_002_ID
    assert second.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert second.notes == (
        "M62 S3J second quality candidate, option S3J (gentler, wider curriculum). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")
    assert reference.run_id == CANDIDATE_003_ID
    assert reference.notes == (
        "M62 S3O third quality candidate, option S3J (gentler, wider curriculum). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")
    assert len({first.config_hash(), second.config_hash(),
                reference.config_hash()}) == 3


def test_the_fourth_candidate_states_its_own_identity_in_its_notes(experiment):
    """``notes`` is covered by ``config_hash``, so this string is part of the identity
    and not decoration."""
    assert experiment.notes == (
        "M62 S3U fourth quality candidate, option S3U (reduced update magnitude). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")


def test_the_new_option_is_used_by_exactly_one_candidate():
    users = [key for key, option in QCFG.CANDIDATE_OPTION.items() if option == "S3U"]
    assert users == ["004"]


# ══════════════════════════════════════════════════════════════════════════════
#  7. The corpus did not move — train-v2 unchanged, no train-v3
# ══════════════════════════════════════════════════════════════════════════════
def test_the_experiment_trains_on_train_v2_unchanged(experiment, reference):
    assert experiment.dataset_reference.dataset_id == "m62-defensive-quality-train"
    assert experiment.dataset_reference.dataset_version == "v2"
    assert experiment.dataset_reference.dataset_manifest_hash == TRAIN_V2_MANIFEST
    assert experiment.dataset_reference.record_count == 182
    assert experiment.dataset_reference.to_dict() == reference.dataset_reference.to_dict()


def test_the_corpus_identity_matches_the_references_sealed_training_receipt(experiment):
    """Re-derived from disk here, sealed there. Equality is the corpus half of the
    single-axis claim, and it is checked against a TRACKED receipt rather than against
    another computation in the same process."""
    receipt = json.loads((REPO / REFERENCE_TRAIN_RECEIPT).read_text(encoding="utf-8"))
    sealed = receipt["training_dataset"]
    derived = experiment.dataset_reference
    assert sealed["dataset_id"] == derived.dataset_id
    assert sealed["version"] == derived.dataset_version
    assert sealed["manifest_hash"] == derived.dataset_manifest_hash
    assert sealed["export_manifest_hash"] == derived.export_manifest_hash
    assert sealed["train_shard_hash"] == derived.train_shard_hash
    assert sealed["validation_shard_hash"] == derived.validation_shard_hash
    assert sealed["reference_hash"] == derived.reference_hash()


def test_there_is_no_third_training_corpus_version():
    import scripts.build_training_corpus as QC

    assert sorted(QC.CURRICULUM_VERSIONS) == ["v1", "v2"]
    assert QC.LATEST_DATASET_VERSION == "v2"
    assert QCFG.TRAINING_DATASET_VERSION_004 == QCFG.TRAINING_DATASET_VERSION_003 == "v2"


def test_the_experiment_never_references_any_holdout(experiment):
    """A training configuration that can name an eval corpus is one edit from
    training on it."""
    body = json.dumps(experiment.to_dict())
    assert "m62-defensive-eval" not in body
    assert experiment.dataset_reference.dataset_version == "v2"


# ══════════════════════════════════════════════════════════════════════════════
#  8. D37 stays fixed, and is inherited rather than re-decided
# ══════════════════════════════════════════════════════════════════════════════
def test_the_render_policy_is_inherited_from_the_reference_by_assignment():
    """Not a second literal that spells the same thing — the reference's own value."""
    assert QCFG.CANDIDATE_REASONING["004"] == QCFG.CANDIDATE_REASONING["003"]
    assert QCFG.CANDIDATE_REASONING["004"] == "TRAIN_EVAL_PARITY"


def test_the_experiment_trains_under_the_policy_evaluation_generates_under():
    from training_gym.evaluation.generation import ELIGIBILITY_REASONING_POLICY
    from training_gym.training.chat_render import (
        TRAIN_EVAL_PARITY_REASONING_POLICY,
        ReasoningPolicy,
    )

    policy = QCFG.candidate_reasoning_policy("004")
    assert policy is QCFG.candidate_reasoning_policy("003")
    assert policy is TRAIN_EVAL_PARITY_REASONING_POLICY
    assert policy is ELIGIBILITY_REASONING_POLICY
    assert policy is ReasoningPolicy.DISABLED


def test_the_legacy_default_is_still_not_read_as_the_fix():
    """An ABSENT reasoning policy is the legacy implicit template default and must never
    be read as DISABLED — that would claim an experiment had already been run."""
    from training_gym.training.chat_render import ReasoningPolicy

    assert QCFG.candidate_reasoning_policy("002") is ReasoningPolicy.MODEL_DEFAULT
    assert ReasoningPolicy.MODEL_DEFAULT.template_kwarg is None
    assert ReasoningPolicy.DISABLED.template_kwarg is False


def test_d37_is_not_reopened_as_an_axis(reference, experiment):
    assert reference.to_dict()["reasoning_policy"] == experiment.to_dict()[
        "reasoning_policy"] == "disabled"


# ══════════════════════════════════════════════════════════════════════════════
#  9. Determinism and round-tripping
# ══════════════════════════════════════════════════════════════════════════════
def test_the_experiment_config_is_deterministic(roots):
    dataset_root, output_root = roots
    built = [QCFG.build_config(QCFG.CANDIDATE_OPTION["004"], dataset_root=dataset_root,
                               output_root=output_root, candidate="004").config_hash()
             for _ in range(3)]
    assert len(set(built)) == 1


def test_the_experiment_config_round_trips(experiment):
    from training_gym.training.config import TrainingConfig
    from training_gym.training.chat_render import ReasoningPolicy

    reloaded = TrainingConfig.from_dict(experiment.to_dict())
    assert reloaded.reasoning_policy is ReasoningPolicy.DISABLED
    assert reloaded.learning_rate == RULED_LEARNING_RATE
    assert reloaded.config_hash() == experiment.config_hash()


def test_the_two_configurations_are_distinct_identities(reference, experiment):
    """Halving one dial must produce a different configuration, or the dial is not
    bound into the identity at all."""
    assert reference.config_hash() != experiment.config_hash()


# ══════════════════════════════════════════════════════════════════════════════
#  10. No capability arrived with the design
# ══════════════════════════════════════════════════════════════════════════════
def test_the_fourth_candidate_was_trained_exactly_once():
    """RESCOPED AT S3V, for the reason this file rescopes rather than deletes.

    Through generation 9 this asserted that candidate 004 had NO run, NO adapter and NO
    ledger entry, because S3U designed it and trained nothing. S3V then spent one
    plan-bound single-use TRAIN authority on it, so the old assertion is now false about
    LIVE state while remaining true about S3U -- and a test that merely deleted itself at
    the moment the thing it guarded began to exist would have stopped guarding anything.

    What survives the transition is the property that actually matters and is permanent:
    the authority was single-use, so there is EXACTLY ONE run and EXACTLY ONE terminal
    event. A second start would be a retry nothing authorised.

    The ledger is a gitignored runtime tree, so its absence is not a failure: a fresh
    clone legitimately has none of it, and the portable receipt carries the history.
    """
    ledger = REPO / "jarvis/training_runs/training_runs.jsonl"
    if not ledger.is_file():
        pytest.skip("the gitignored training ledger is not present on this host")
    events = [json.loads(line) for line in
              ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    mine = [e for e in events if e.get("run_id") == CANDIDATE_004_ID]
    assert [e.get("event") for e in mine] == ["started", "completed"]
    assert len({e.get("plan_hash") for e in mine}) == 1


def test_exactly_one_receipt_exists_for_the_fourth_candidate():
    """RESCOPED AT S3V. A receipt is evidence of an operation, and now one has happened.

    It must be a TRAINING receipt and nothing more: an evaluation receipt for this
    candidate would mean `eval-v5` had been spent, which no authority permits.
    """
    receipts = REPO / "state/m62/receipts"
    mine = sorted(p.name for p in receipts.iterdir() if CANDIDATE_004_ID in p.name)
    assert mine == [f"{CANDIDATE_004_ID}.train.json"]


def test_the_generator_carries_no_spendable_token_literal():
    """The generator DESCRIBES the ``TRAIN:<plan-hash>`` form and holds no instance of
    it. The pattern is the control plane's own, so the two cannot drift apart."""
    from scripts.verify_m62_control_plane import TOKEN_LITERAL_RE

    for rel in ("jarvis/scripts/build_quality_training_config.py", DESIGN_DOC,
                "jarvis/tests/test_training_gym_m62_s3u_fourth_candidate.py"):
        assert not TOKEN_LITERAL_RE.search((REPO / rel).read_text(encoding="utf-8")), rel


def test_the_generator_imports_no_training_framework_at_module_scope():
    """The dry run is structural: importing torch to build a configuration would make a
    plan a thing that cannot be computed without the machinery it plans for."""
    source = (REPO / "jarvis/scripts/build_quality_training_config.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    top_level = {name.name.split(".")[0]
                 for node in tree.body if isinstance(node, ast.Import)
                 for name in node.names}
    top_level |= {(node.module or "").split(".")[0]
                  for node in tree.body if isinstance(node, ast.ImportFrom)}
    assert not top_level & {"torch", "transformers", "peft", "trl", "datasets",
                            "accelerate", "training_gym"}


def test_the_design_document_is_tracked_and_body_free():
    """The deep evidence must exist, be tracked, and carry no holdout material.

    The firewall is applied with the control plane's OWN task-id tables and its own
    private-path and token patterns rather than a weaker second opinion written here.
    """
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


def test_the_design_document_binds_eval_v5_body_free_only():
    """It may name the holdout by identity and by nothing else."""
    text = (REPO / DESIGN_DOC).read_text(encoding="utf-8")
    assert "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c" in text
    assert "287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6" in text
    assert "FROZEN_UNUSED" in text


def test_no_new_development_surface_carries_assistant_attribution():
    """S3U's brand-neutrality rule, checked on the surfaces S3U creates.

    Scoped to this milestone's own files on purpose. Historical provenance is not
    rewritten to make a number zero, and a technically-true runtime reference such as an
    API key variable is not attribution and is not touched.
    """
    vendor = "Cla" + "ude"
    forbidden = (f"Co-Authored-By: {vendor}", f"{vendor}-Session:",
                 f"Generated by {vendor}", "Generated by " + "AI",
                 "AI-" + "assisted", "assistant-" + "generated")
    for rel in (DESIGN_DOC, "jarvis/tests/test_training_gym_m62_s3u_fourth_candidate.py",
                "jarvis/scripts/build_quality_training_config.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{rel} carries {needle!r}"
