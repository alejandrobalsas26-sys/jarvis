"""V69 M62 S3O — the third quality candidate: one axis, one corpus, no capability.

WHAT THESE TESTS ARE FOR
------------------------
Candidates 001 and 002 failed in opposite directions on the same axis, and both sat at
7/9 on structured output against a perfect 9/9 baseline. S3M diagnosed that as a
TERMINATION failure and opened D37: training rendered its prompts one way and evaluation
rendered them another. S3M.1 fixed the mechanism. Nothing has yet been trained under it.

S3O designs the candidate that would exercise it, and the entire scientific value of that
candidate rests on one claim: **exactly one thing changed.** A design that quietly moved
a second dial produces a third uninterpretable run and burns a fresh holdout to do it. So
this file is mostly about that claim being MEASURED rather than asserted.

Five ways this design could be quietly worthless, each checked here:

  * **A second axis.** Every hyperparameter is compared field by field between the
    control and the experiment, and the normalized semantic diff is required to be
    exactly ``{"reasoning_policy"}`` — not "small", not "intended", exactly that.
  * **A re-identified control.** Adding candidate 003 must not perturb what candidates
    001 and 002 hash to, or the comparison is against a configuration that never ran.
    Both historical identities are re-derived and pinned.
  * **A legacy field read as the experiment.** An ABSENT ``reasoning_policy`` is the
    legacy implicit template default, and must never be read as ``DISABLED`` — that would
    silently claim the experiment had already been run.
  * **A control plane that believes itself.** A snapshot claiming
    ``DESIGNED_UNTRAINED`` is required to FAIL verification when the production generator
    cannot produce that design, *even when a constant in the verifier agrees with it*.
  * **Capability arriving by accident.** No adapter, no run record, no plan token, no
    ``train-v3``, no evaluation plan, and no eval-v4 body — asserted, not promised.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS, CREATES AN OPTIMIZER OR GENERATES A
TOKEN. The two tests that need a real tokenizer load one from the reviewed offline cache
and skip when it is absent; they load no weights.

This file reads no ``eval-v4`` task body and contains none.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.build_quality_training_config as QCFG
from scripts import verify_m62_control_plane as V
from training_gym.training.chat_render import (
    TRAIN_EVAL_PARITY_REASONING_POLICY,
    ChatRenderPolicy,
    ReasoningPolicy,
)

REPO = V.REPO_ROOT

# ── the identities S3O may not move ──────────────────────────────────────────────────
#: Written here independently of the artefacts being checked. A test that reads its
#: expected value out of the thing under test proves nothing.
CANDIDATE_001_ID = "qwen3-06b-lora-quality-live-001"
CANDIDATE_002_ID = "qwen3-06b-lora-quality-live-002"
CANDIDATE_003_ID = "qwen3-06b-lora-quality-live-003"

TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
CHAT_TEMPLATE_DIGEST = (
    "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8")

#: The reviewed offline cache. Operator-supplied and outside the repository by invariant,
#: so its absence SKIPS the two tokenizer tests rather than failing them.
#:
#: DERIVED, never written down. A literal absolute path here would bake this machine's
#: account name into a tracked file, which is the very thing the private-path scan and
#: D36 exist to prevent — and it would make the tests unrunnable anywhere else.
MODEL_CACHE = Path(os.environ.get("M62_MODEL_CACHE_ROOT",
                                  str(REPO.parent / ".m62-model-cache")))
EXPORTS = (REPO / "jarvis" / "training_gym_datasets" / "exports"
           / "m62-defensive-quality-train" / "v2")

#: Config fields that may differ between the control and the experiment because they are
#: consequences of the candidate's IDENTITY, not of its training behaviour. Naming them
#: explicitly is the point: an unlisted difference is a second axis.
IDENTITY_FIELDS = frozenset({"run_id", "experiment_name", "notes"})

#: The single preregistered axis (S3N §0).
PRIMARY_AXIS = "reasoning_policy"


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    """A corpus root holding both training versions, plus a shared output root.

    Both configurations are built against the SAME roots on purpose: ``config_hash``
    binds ``output_root_id``, so comparing configurations built under different roots
    would show a difference that is about a filesystem path and not about the model.
    """
    root = tmp_path_factory.mktemp("m62-s3o-train")
    import scripts.build_training_corpus as QC

    QC.build(root, dataset_version="v1")
    QC.build(root, dataset_version="v2")
    return root, root / "runs"


@pytest.fixture(scope="module")
def control(roots):
    dataset_root, output_root = roots
    return QCFG.build_config(QCFG.CANDIDATE_OPTION["002"], dataset_root=dataset_root,
                             output_root=output_root, candidate="002")


@pytest.fixture(scope="module")
def experiment(roots):
    dataset_root, output_root = roots
    return QCFG.build_config(QCFG.CANDIDATE_OPTION["003"], dataset_root=dataset_root,
                             output_root=output_root, candidate="003")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A writable copy of the control plane, so a mutation never touches the real tree."""
    for rel in (V.CURRENT_PATH, V.MIGRATION_MANIFEST_PATH, V.ARCHIVE_PATH,
                V.PROGRESS_PATH, V.HISTORY_INDEX_PATH, V.CURRENT_SCHEMA_PATH,
                V.SNAPSHOT_SCHEMA_PATH):
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, destination)
    for source in (REPO / V.SNAPSHOT_DIR).iterdir():
        destination = tmp_path / V.SNAPSHOT_DIR / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
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


def _rewrite(root: Path, rel: str, payload: dict) -> None:
    (root / rel).write_bytes(V.canonical_bytes(payload))


def _repoint(root: Path) -> None:
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    data = (root / current["latest_snapshot_path"]).read_bytes()
    current["latest_snapshot_sha256"] = V.sha256_bytes(data)
    _rewrite(root, V.CURRENT_PATH, current)


def _entry(snapshot: dict, ordinal: int) -> dict:
    return next(c for c in snapshot["candidates"] if c["ordinal"] == ordinal)


def _designed(root: Path) -> V.ControlPlane:
    """Put ordinal 3 into the S3O design state inside the sandbox and reload.

    Written here rather than read from the live snapshot on purpose: these tests are
    about the MECHANISM that checks a design claim, and they must mean the same thing
    whether or not the control plane has yet recorded one.
    """
    plane = _plane_from(root)
    mutated = copy.deepcopy(plane.snapshot)
    entry = _entry(mutated, 3)
    entry.update(candidate_id=CANDIDATE_003_ID, status="DESIGNED_UNTRAINED",
                 training_corpus="m62-defensive-quality-train v2",
                 base_model_revision=BASE_MODEL_REVISION,
                 evidence=V.CANDIDATE_003_EVIDENCE)
    _rewrite(root, plane.current["latest_snapshot_path"], mutated)
    _repoint(root)
    return _plane_from(root)


def _tokenizer():
    """The reviewed tokenizer, or a skip. NEVER a model, never a download."""
    if not MODEL_CACHE.is_dir():
        pytest.skip("the reviewed model cache is not present on this host")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        from transformers import AutoTokenizer
    except Exception:  # pragma: no cover - the training profile is not installed here
        pytest.skip("transformers is not installed on this interpreter")
    return AutoTokenizer.from_pretrained(
        BASE_MODEL_ID, revision=BASE_MODEL_REVISION, cache_dir=str(MODEL_CACHE),
        local_files_only=True, trust_remote_code=False)


# ══════════════════════════════════════════════════════════════════════════════
#  1. Candidate identity: derived, non-colliding, and consistent with convention
# ══════════════════════════════════════════════════════════════════════════════
def test_the_third_candidate_has_the_next_identity_in_the_quality_lineage():
    """The ordinal is per-LINEAGE. ``smoke-live-003`` exists and is a different series."""
    assert QCFG.CANDIDATES["003"]["run_id"] == CANDIDATE_003_ID
    assert QCFG.RUN_ID_003 == CANDIDATE_003_ID
    stem = "qwen3-06b-lora-quality-live-"
    assert [QCFG.CANDIDATES[key]["run_id"] for key in ("001", "002", "003")] == [
        f"{stem}001", f"{stem}002", f"{stem}003"]


def test_the_candidate_identity_is_deterministic():
    """Nothing about the identity is a function of the host, the clock or a path."""
    assert QCFG.CANDIDATES["003"]["run_id"] == QCFG.CANDIDATES["003"]["run_id"]
    source = (REPO / "jarvis/scripts/build_quality_training_config.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "RUN_ID_003" for t in node.targets):
            assert isinstance(node.value, ast.Constant), "the identity is a literal"


def test_no_candidate_identity_collides():
    ids = [spec["run_id"] for spec in QCFG.CANDIDATES.values()]
    assert len(ids) == len(set(ids))
    assert CANDIDATE_003_ID not in {CANDIDATE_001_ID, CANDIDATE_002_ID}


def test_an_unknown_candidate_is_still_a_refusal():
    """Fail-closed survived the addition: a typo is a refusal, never a run."""
    # S3U added candidate 004, so the probe moved on again -- exactly as it moved
    # from 003 to 004 when S3O added candidate 003. What this line owns is the
    # FAIL-CLOSED property, never a particular number.
    with pytest.raises(ValueError, match="unknown candidate"):
        QCFG.candidate_spec("005")


#: The TWO tracked non-document artefacts permitted to name candidate 003: the S3P
#: portable training receipt and, from S3Q.0.2, the portable EVALUATION receipt. Both are
#: evidence, not artefacts of the model -- no weights, no configuration document, no
#: plan, no token. Naming them explicitly is the point: an unlisted tracked path carrying
#: this identity is still a failure.
#:
#: WIDENED at S3Q.0.2 by exactly one path, and not a weakening: the control plane refuses
#: an ``EVALUATED_*`` state that shows no tracked portable receipt, so this file is the
#: invariant being satisfied. The per-path assertions below are unchanged and still
#: refuse anything shaped like an adapter, a configuration or a plan.
CANDIDATE_003_TRACKED_ALLOWLIST = frozenset({
    "state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json",
    "state/m62/receipts/qwen3-06b-lora-quality-live-003.eval.json",
})


def test_no_tracked_artefact_is_named_for_the_third_candidate():
    """No adapter, no run, no sealed config, no plan document -- in Git.

    S3P added exactly one tracked path naming this candidate, and it is deliberately not
    an artefact OF the model: the portable receipt exists so the training history
    survives a clone that has no weights. Everything the invariant actually protects --
    adapters, configuration documents, plans and tokens stay out of Git -- is unchanged
    and is still asserted for every other path.
    """
    try:
        tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                                 capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("git is not available")
    if tracked.returncode != 0:  # pragma: no cover
        pytest.skip("not a git repository")
    names = [p for p in tracked.stdout.split()
             if not p.endswith(".md") and "docs/" not in p]
    named = {p for p in names if CANDIDATE_003_ID in p}
    assert named == CANDIDATE_003_TRACKED_ALLOWLIST
    for path in named:
        assert path.endswith(".json") and "/receipts/" in path
        assert "adapter" not in path and "config" not in path and "plan" not in path


# ══════════════════════════════════════════════════════════════════════════════
#  2. The control is untouched — adding 003 must not re-identify 001 or 002
# ══════════════════════════════════════════════════════════════════════════════
def test_the_first_candidate_is_untouched(roots):
    dataset_root, output_root = roots
    first = QCFG.build_config("B", dataset_root=dataset_root, output_root=output_root)
    assert first.run_id == CANDIDATE_001_ID
    assert first.dataset_reference.dataset_version == "v1"
    assert first.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert first.notes == (
        "M62 S3G first quality candidate, option B (recommended). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")


def test_the_second_candidate_is_untouched(control):
    assert control.run_id == CANDIDATE_002_ID
    assert control.dataset_reference.dataset_version == "v2"
    assert control.dataset_reference.dataset_manifest_hash == TRAIN_V2_MANIFEST
    assert control.dataset_reference.record_count == 182
    assert control.notes == (
        "M62 S3J second quality candidate, option S3J (gentler, wider curriculum). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")


def test_the_control_keeps_the_legacy_render_policy(control):
    """MODEL_DEFAULT is what 002 ACTUALLY trained under. Restating it as DISABLED would
    rewrite history and delete the experiment in the same edit."""
    assert control.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert QCFG.candidate_reasoning_policy("001") is ReasoningPolicy.MODEL_DEFAULT
    assert QCFG.candidate_reasoning_policy("002") is ReasoningPolicy.MODEL_DEFAULT


def test_the_legacy_policy_is_value_gated_out_of_the_canonical_body(control):
    """Naming the legacy default explicitly must not move the historical digest."""
    assert PRIMARY_AXIS not in control.to_dict()
    assert not ReasoningPolicy.MODEL_DEFAULT.is_explicit


def test_an_absent_reasoning_field_reloads_as_the_legacy_default(control):
    """Absent is LEGACY_IMPLICIT_TEMPLATE_DEFAULT, never DISABLED."""
    from training_gym.training.config import TrainingConfig

    body = control.to_dict()
    assert PRIMARY_AXIS not in body
    reloaded = TrainingConfig.from_dict(body)
    assert reloaded.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert reloaded.config_hash() == control.config_hash()


def test_both_historical_config_identities_are_stable(roots, control):
    """Re-derived through the CURRENT parser, after the generator gained a candidate."""
    dataset_root, output_root = roots
    first = QCFG.build_config("B", dataset_root=dataset_root, output_root=output_root)
    again = QCFG.build_config(QCFG.CANDIDATE_OPTION["002"], dataset_root=dataset_root,
                              output_root=output_root, candidate="002")
    assert control.config_hash() == again.config_hash()
    assert first.config_hash() != control.config_hash()


# ══════════════════════════════════════════════════════════════════════════════
#  3. THE ONE AXIS — the claim the whole milestone rests on
# ══════════════════════════════════════════════════════════════════════════════
def test_the_experiment_binds_the_disabled_render_policy(experiment):
    assert experiment.reasoning_policy is ReasoningPolicy.DISABLED
    assert experiment.to_dict()[PRIMARY_AXIS] == "disabled"


def test_the_experiment_binds_the_same_object_evaluation_generates_under():
    """Not a second enum member that spells the same: the SAME object. If these ever
    diverge, train/eval parity becomes a coincidence rather than a guarantee."""
    from training_gym.evaluation.generation import ELIGIBILITY_REASONING_POLICY

    assert QCFG.candidate_reasoning_policy("003") is TRAIN_EVAL_PARITY_REASONING_POLICY
    assert TRAIN_EVAL_PARITY_REASONING_POLICY is ELIGIBILITY_REASONING_POLICY
    assert TRAIN_EVAL_PARITY_REASONING_POLICY is ReasoningPolicy.DISABLED


def test_the_semantic_diff_is_exactly_one_key(control, experiment):
    """THE assertion. Not 'small', not 'intended' — exactly ``{"reasoning_policy"}``."""
    before, after = control.to_dict(), experiment.to_dict()
    differing = {key for key in set(before) | set(after)
                 if before.get(key) != after.get(key)}
    assert differing - IDENTITY_FIELDS == {PRIMARY_AXIS}


def test_every_raw_difference_is_either_the_axis_or_an_identity(control, experiment):
    """The complement of the test above: nothing differs that is not accounted for."""
    before, after = control.to_dict(), experiment.to_dict()
    differing = {key for key in set(before) | set(after)
                 if before.get(key) != after.get(key)}
    assert differing == IDENTITY_FIELDS | {PRIMARY_AXIS}


def test_the_dials_are_shared_by_reference_not_by_copy():
    """A copied option is a thing that can drift; a shared key cannot."""
    assert QCFG.CANDIDATE_OPTION["003"] == QCFG.CANDIDATE_OPTION["002"] == "S3J"


@pytest.mark.parametrize("field", [
    "method", "base_model_id", "base_model_revision", "base_model_parameters_b",
    "base_model_family", "tokenizer_id", "tokenizer_revision", "trust_remote_code",
    "dataset_reference", "training_split", "validation_split",
    "hidden_evaluation_reference", "security_regression_reference", "output_root_id",
    "seed", "epochs", "max_steps", "batch_size", "gradient_accumulation_steps",
    "learning_rate", "weight_decay", "warmup_ratio", "max_sequence_length",
    "gradient_checkpointing", "precision_policy", "device_policy", "lora",
    "dataloader_workers", "checkpoint_strategy", "checkpoint_interval_steps",
    "max_checkpoints", "logging_target", "logging_interval_steps",
    "model_download_policy", "dependency_profile", "resource_policy",
    "created_at_utc", "validation_strategy", "schema_version",
])
def test_no_other_field_moved(control, experiment, field):
    """Field by field, every canonical key that is not the axis or an identity."""
    assert control.to_dict()[field] == experiment.to_dict()[field]


@pytest.mark.parametrize("attribute,expected", [
    ("seed", 42), ("epochs", 2), ("max_steps", 40), ("batch_size", 1),
    ("gradient_accumulation_steps", 8), ("learning_rate", 1e-4),
    ("weight_decay", 0.0), ("warmup_ratio", 0.1), ("max_sequence_length", 512),
    ("gradient_checkpointing", False), ("dataloader_workers", 0),
    ("max_checkpoints", 1), ("logging_interval_steps", 5), ("trust_remote_code", False),
])
def test_the_experiment_keeps_the_controls_hyperparameters(experiment, attribute,
                                                           expected):
    assert getattr(experiment, attribute) == expected


def test_the_lora_scope_and_shape_are_the_controls(experiment):
    """ATTENTION_AND_MLP, not ATTENTION_ONLY — combining scope with the render axis
    would move two variables and produce a third uninterpretable run."""
    assert experiment.lora.target_policy.value == "attention_and_mlp"
    assert list(experiment.lora.target_modules) == [
        "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    assert (experiment.lora.rank, experiment.lora.alpha, experiment.lora.dropout) == (
        16, 32, 0.05)
    assert experiment.lora.bias.value == "none"
    assert experiment.lora.task_type == "CAUSAL_LM"


def test_the_device_and_precision_are_declared_inputs(experiment):
    assert experiment.device_policy.value == "cpu"
    assert experiment.precision_policy.value == "fp32"


def test_the_validation_and_checkpoint_behaviour_is_the_controls(experiment):
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


def test_the_experiment_config_is_deterministic(roots):
    dataset_root, output_root = roots
    built = [QCFG.build_config(QCFG.CANDIDATE_OPTION["003"], dataset_root=dataset_root,
                               output_root=output_root, candidate="003").config_hash()
             for _ in range(3)]
    assert len(set(built)) == 1


def test_the_experiment_config_round_trips(experiment):
    from training_gym.training.config import TrainingConfig

    reloaded = TrainingConfig.from_dict(experiment.to_dict())
    assert reloaded.reasoning_policy is ReasoningPolicy.DISABLED
    assert reloaded.config_hash() == experiment.config_hash()


# ══════════════════════════════════════════════════════════════════════════════
#  4. The corpus did not move — train-v2 unchanged, no train-v3
# ══════════════════════════════════════════════════════════════════════════════
def test_the_experiment_trains_on_train_v2_unchanged(experiment, control):
    assert experiment.dataset_reference.dataset_id == "m62-defensive-quality-train"
    assert experiment.dataset_reference.dataset_version == "v2"
    assert experiment.dataset_reference.dataset_manifest_hash == TRAIN_V2_MANIFEST
    assert experiment.dataset_reference.record_count == 182
    assert experiment.dataset_reference.to_dict() == control.dataset_reference.to_dict()


def test_there_is_no_third_training_corpus_version():
    import scripts.build_training_corpus as QC

    assert sorted(QC.CURRICULUM_VERSIONS) == ["v1", "v2"]
    assert QC.LATEST_DATASET_VERSION == "v2"
    assert QCFG.TRAINING_DATASET_VERSION_003 == QCFG.TRAINING_DATASET_VERSION_002 == "v2"


def test_the_experiment_never_references_the_holdout(experiment):
    """A training configuration that can name eval-v4 is one edit from training on it."""
    body = json.dumps(experiment.to_dict())
    assert "m62-defensive-eval" not in body
    assert "v4" not in experiment.dataset_reference.dataset_version


# ══════════════════════════════════════════════════════════════════════════════
#  5. D37 — the render identity that IS the axis
# ══════════════════════════════════════════════════════════════════════════════
def _render(policy, *, add_generation_prompt=True):
    return ChatRenderPolicy(
        tokenizer_id=BASE_MODEL_ID, tokenizer_revision=BASE_MODEL_REVISION,
        chat_template_hash=CHAT_TEMPLATE_DIGEST, reasoning_policy=policy,
        add_generation_prompt=add_generation_prompt, tokenize=True).render_policy_hash()


def test_the_render_identity_is_deterministic():
    assert _render(ReasoningPolicy.DISABLED) == _render(ReasoningPolicy.DISABLED)


def test_the_experiment_render_identity_differs_from_the_controls():
    """If these were equal the axis would have moved nothing at all."""
    assert _render(ReasoningPolicy.DISABLED) != _render(ReasoningPolicy.MODEL_DEFAULT)


def test_the_render_identity_binds_the_library_level_call():
    """``None`` (do not pass the keyword) and ``False`` (pass it) are DIFFERENT calls."""
    assert ReasoningPolicy.MODEL_DEFAULT.template_kwarg is None
    assert ReasoningPolicy.DISABLED.template_kwarg is False
    body = ChatRenderPolicy(
        tokenizer_id=BASE_MODEL_ID, tokenizer_revision=BASE_MODEL_REVISION,
        chat_template_hash=CHAT_TEMPLATE_DIGEST,
        reasoning_policy=ReasoningPolicy.DISABLED).to_dict()
    assert body["reasoning_policy"] == "disabled"
    assert body["enable_thinking"] is False


def test_the_render_identity_binds_the_template_digest():
    """A different template is a different render, even under the same policy."""
    other = ChatRenderPolicy(
        tokenizer_id=BASE_MODEL_ID, tokenizer_revision=BASE_MODEL_REVISION,
        chat_template_hash="0" * 64, reasoning_policy=ReasoningPolicy.DISABLED,
        add_generation_prompt=True, tokenize=True).render_policy_hash()
    assert other != _render(ReasoningPolicy.DISABLED)


def test_the_prompt_and_full_sequence_renders_are_distinct_identities():
    assert _render(ReasoningPolicy.DISABLED) != _render(
        ReasoningPolicy.DISABLED, add_generation_prompt=False)


def test_d37_and_d38_sources_are_untouched_by_this_milestone():
    """S3O designs a candidate. It does not touch the instruments."""
    from training_gym.evaluation import gates

    gate_source = Path(gates.__file__).read_text(encoding="utf-8")
    assert "output_budget_exhaust" not in gate_source
    assert "finish_reason" not in gate_source


# ══════════════════════════════════════════════════════════════════════════════
#  6. Tokenizer-only qualification — real rows, real template, NO weights
# ══════════════════════════════════════════════════════════════════════════════
def test_the_reviewed_template_still_hashes_to_the_pinned_digest():
    from training_gym.training.dataset_conversion import chat_template_hash

    tokenizer = _tokenizer()
    assert chat_template_hash(tokenizer.chat_template) == CHAT_TEMPLATE_DIGEST


def test_the_axis_moves_the_mask_boundary_and_nothing_else():
    """The measured heart of D37, over the REAL corpus rather than a fixture.

    Under the legacy default every training row supervised an empty ``<think></think>``
    block as though it were the opening of the assistant's answer. Under ``DISABLED``
    none does. The full token sequence is IDENTICAL either way — only the mask boundary
    moves — so the assistant's answer text is untouched by the axis.
    """
    if not EXPORTS.is_dir():
        pytest.skip("the promoted train-v2 exports are not materialised on this host")
    tokenizer = _tokenizer()
    from training_gym.training.backends.transformers_peft import TransformersPeftBackend
    from training_gym.training.dataset_conversion import (
        check_masking,
        convert_sft_export,
    )

    dataset = convert_sft_export(EXPORTS / "sft_train.jsonl", max_sequence_length=512,
                                 expected_record_count=154)
    backend = TransformersPeftBackend()
    legacy, legacy_truncated = backend._encode(
        dataset, tokenizer=tokenizer, max_length=512,
        reasoning_policy=ReasoningPolicy.MODEL_DEFAULT)
    disabled, disabled_truncated = backend._encode(
        dataset, tokenizer=tokenizer, max_length=512,
        reasoning_policy=ReasoningPolicy.DISABLED)

    assert legacy_truncated == disabled_truncated == 0
    assert [row["input_ids"] for row in legacy] == [row["input_ids"] for row in disabled]
    assert {d["prompt_length"] - m["prompt_length"]
            for m, d in zip(legacy, disabled)} == {4}

    supervised = lambda row: tokenizer.decode(  # noqa: E731
        [t for t in row["labels"] if t != -100])
    assert all("<think>" in supervised(row) for row in legacy)
    assert not any("<think>" in supervised(row) for row in disabled)
    assert all(supervised(m).endswith(supervised(d)) for m, d in zip(legacy, disabled))

    for rows in (legacy, disabled):
        assert backend._masking_self_test(rows).verified
        assert [p for row in rows
                for p in check_masking(row["labels"],
                                       prompt_length=row["prompt_length"])] == []
        assert all(row["labels"][-2] == tokenizer.convert_tokens_to_ids("<|im_end|>")
                   for row in rows)


# ══════════════════════════════════════════════════════════════════════════════
#  7. Control plane — the state is re-derived, never believed
# ══════════════════════════════════════════════════════════════════════════════
def test_the_design_state_verifies_against_the_real_control_plane():
    report = V.Report()
    loaded = V.load(report)
    assert loaded is not None
    V.check_candidate_design(loaded, report)
    assert not report.problems, report.problems


def test_the_transition_from_not_created_to_designed_is_permitted():
    assert V.transition_problems("NOT_CREATED", "DESIGNED_UNTRAINED",
                                 V.CANDIDATE_TRANSITIONS, "candidate") == []


@pytest.mark.parametrize("target", [
    "TRAINED_UNEVALUATED", "EVALUATED_NOT_ELIGIBLE",
    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW", "PROMOTED",
])
def test_a_design_milestone_may_not_skip_to_a_later_state(target):
    assert V.transition_problems("NOT_CREATED", target, V.CANDIDATE_TRANSITIONS,
                                 "candidate") != []


def test_a_designed_candidate_may_not_carry_an_adapter(sandbox):
    designed = _designed(sandbox)
    mutated = copy.deepcopy(designed.snapshot)
    _entry(mutated, 3)["adapter_sha256"] = "a" * 64
    _rewrite(sandbox, designed.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert "CANDIDATE_STATE" in _categories(report)
    assert any("has a configuration and no weights" in m for _, m in report.problems)


@pytest.mark.parametrize("field", ["training_corpus", "base_model_revision", "evidence"])
def test_a_designed_candidate_must_name_its_design(sandbox, field):
    designed = _designed(sandbox)
    mutated = copy.deepcopy(designed.snapshot)
    _entry(mutated, 3)[field] = None
    _rewrite(sandbox, designed.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert "CANDIDATE_STATE" in _categories(report)


def test_a_renamed_candidate_cannot_walk_past_the_transition_table():
    """The hole this milestone closed. Generation 1 recorded ordinal 3 under a
    PLACEHOLDER id, so keying the transition check on ``candidate_id`` meant a rename
    made ``before`` resolve to ``None`` and every illegal jump pass unchecked."""
    parent = {"candidates": [{"candidate_id": "candidate-003", "ordinal": 3,
                              "status": "NOT_CREATED"}]}
    child = {"candidates": [{"candidate_id": "something-entirely-new", "ordinal": 3,
                             "status": "TRAINED_UNEVALUATED",
                             "adapter_sha256": None, "adapter_manifest_hash": None,
                             "base_model_revision": None, "evaluation_corpus": None,
                             "evidence": None, "training_corpus": None}]}
    previous = {c["ordinal"]: c for c in parent["candidates"]}
    entry = child["candidates"][0]
    before = previous[entry["ordinal"]]["status"]
    assert V.transition_problems(before, entry["status"], V.CANDIDATE_TRANSITIONS,
                                 "candidate") != []
    assert V.CANDIDATE_IDENTITY_RESOLUTIONS["candidate-003"] == CANDIDATE_003_ID


def test_the_snapshot_alone_cannot_create_a_design(sandbox, monkeypatch):
    """ZERO-TRUST. The snapshot says DESIGNED_UNTRAINED and the verifier's own constant
    agrees with it — and it must STILL fail, because the production generator cannot
    produce that design. Two agreeing writable surfaces are not evidence."""
    designed = _designed(sandbox)
    mutated = copy.deepcopy(designed.snapshot)
    _entry(mutated, 3)["candidate_id"] = "qwen3-06b-lora-quality-live-999"
    _rewrite(sandbox, designed.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    monkeypatch.setitem(V.FROZEN_CANDIDATES, "qwen3-06b-lora-quality-live-999",
                        ("DESIGNED_UNTRAINED", None))
    report = V.Report()
    V.check_candidate_design(_plane_from(sandbox), report)
    assert "CANDIDATE_STATE" in _categories(report)
    assert any("no candidate in the production generator" in m
               for _, m in report.problems)


def test_a_design_claim_fails_when_the_generator_moves_the_axis_back(sandbox,
                                                                     monkeypatch):
    """The mutation that matters most: the experiment quietly reverts to the legacy
    representation while the control plane still calls it the S3O candidate."""
    monkeypatch.setitem(QCFG.CANDIDATE_REASONING, "003", "LEGACY_MODEL_DEFAULT")
    report = V.Report()
    V.check_candidate_design(_designed(sandbox), report)
    assert any("not DISABLED" in m for _, m in report.problems)


def test_a_design_claim_fails_when_a_second_axis_appears(sandbox, monkeypatch):
    """Changing the option key is how a second dial would arrive."""
    monkeypatch.setitem(QCFG.CANDIDATE_OPTION, "003", "C")
    report = V.Report()
    V.check_candidate_design(_designed(sandbox), report)
    assert any("second experimental axis" in m for _, m in report.problems)


def test_a_design_claim_fails_when_the_control_moves(sandbox, monkeypatch):
    """If the control is retconned to DISABLED there is no experiment left to run."""
    monkeypatch.setitem(QCFG.CANDIDATE_REASONING, "002", "TRAIN_EVAL_PARITY")
    report = V.Report()
    V.check_candidate_design(_designed(sandbox), report)
    assert any("no longer controlled" in m for _, m in report.problems)


def test_a_design_claim_fails_when_the_corpus_moves(sandbox, monkeypatch):
    monkeypatch.setitem(QCFG.CANDIDATES["003"], "dataset_version", "v4")
    report = V.Report()
    V.check_candidate_design(_designed(sandbox), report)
    assert any("trains it on" in m for _, m in report.problems)


def test_a_design_claim_fails_without_tracked_deep_evidence(sandbox):
    designed = _designed(sandbox)
    mutated = copy.deepcopy(designed.snapshot)
    _entry(mutated, 3)["evidence"] = "jarvis/docs/V69_M62_S3O_DOES_NOT_EXIST.md"
    _rewrite(sandbox, designed.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_design(_plane_from(sandbox), report)
    assert any("is not a file in this tree" in m for _, m in report.problems)


def test_the_unknown_reasoning_symbol_is_a_refusal_not_a_default(monkeypatch):
    """Falling back to MODEL_DEFAULT would train the legacy representation under a
    configuration claiming otherwise — the worst possible silent failure."""
    monkeypatch.setitem(QCFG.CANDIDATE_REASONING, "003", "SOMETHING_ELSE")
    with pytest.raises(ValueError, match="cannot resolve"):
        QCFG.candidate_reasoning_policy("003")


# ══════════════════════════════════════════════════════════════════════════════
#  8. No capability was created
# ══════════════════════════════════════════════════════════════════════════════
def test_the_generator_never_prints_a_plan_token():
    """``train_experiment.py --print-plan`` legitimately prints one for an operator who
    already holds the plan. This generator deliberately does not, and S3O used only this
    one."""
    source = (REPO / "jarvis/scripts/build_quality_training_config.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    # AST, not a substring scan: the generator's own docstring explains that it does NOT
    # print the token, and a text search finds the word inside the sentence forbidding it
    # -- the operator-ruling-H4 shape S3N recorded for ``<think``.
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    assert "confirmation_token" not in names
    assert "to_record" not in names
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    executable = [s for s in literals if "TRAIN:" in s and "DERIVED" not in s
                  and "derivable" not in s]
    assert executable == [], executable


def test_no_authority_literal_is_tracked():
    result = subprocess.run(
        ["git", "-C", str(REPO), "grep", "-I", "-n", "-E", V.TOKEN_LITERAL_PATTERN,
         "--", "."], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 1, result.stdout[:400]


def test_the_snapshot_records_no_training_or_evaluation_authority():
    snapshot = json.loads(
        (REPO / json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
         ["latest_snapshot_path"]).read_text(encoding="utf-8"))
    observation = snapshot["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    assert observation["train"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"


def test_designing_the_candidate_did_not_spend_the_holdout():
    """RESCOPED at S3Q.0.2 to the generation S3O wrote, and not a weakening.

    This read the LIVE snapshot, so it also asserted -- by coincidence rather than by
    design -- that no later generation had spent v4. S3Q spent it, which is exactly what
    S3O said a future authorised evaluation would do.

    The property S3O owns is that DESIGNING a candidate spends nothing, and that property
    is immutable: it is checked at generation 2, where S3O recorded it, and stays
    checkable forever. The manifest digest is asserted here too, because "unspent" about
    the wrong corpus is not the claim.
    """
    generation_2 = next(
        path for path in sorted((REPO / V.SNAPSHOT_DIR).iterdir())
        if json.loads(path.read_text(encoding="utf-8"))["state_generation"] == 2)
    snapshot = json.loads(generation_2.read_text(encoding="utf-8"))
    assert snapshot["subject_state_milestone"] == "S3O"
    v4 = next(d for d in snapshot["datasets"]
              if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    assert v4["status"] == "FROZEN_UNUSED"
    assert v4["spent_by"] is None
    assert v4["manifest_hash"] == (
        "8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5")


def test_this_file_names_no_holdout_task():
    """The suite that protects the firewall must not itself carry the material."""
    text = Path(__file__).read_text(encoding="utf-8")
    for task_id in V.EVAL_V4_TASK_IDS:
        assert task_id not in text
    for symbol in V.FORBIDDEN_BODY_SYMBOLS:
        assert symbol not in text


def test_this_milestone_created_no_evaluation_plan():
    """Candidate 003 has no adapter, so no honest live evaluation plan can exist yet."""
    evaluations = REPO / "jarvis" / "evaluation" / "evaluations"
    if not evaluations.is_dir():
        return
    assert [p for p in evaluations.iterdir() if CANDIDATE_003_ID in p.name] == []
