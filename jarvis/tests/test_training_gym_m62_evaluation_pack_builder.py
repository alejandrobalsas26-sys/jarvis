"""V69 M62 S3E.1 — promoted dataset shards to an evaluation task pack.

WHAT THIS STAGE IS FOR
----------------------
Before this existed the CLI planned an evaluation from split *counts* read out of a
manifest and bound a placeholder where the pack digest belonged. A plan could therefore
be approved, and a token issued against it, without anyone having built the thing being
authorised.

The builder is also the last place an expected answer can escape. It decides which
graders judge a task, which response schema the model is shown and which tools it may
propose against — three chances to write the answer into the question. So most of what
follows is about the answers staying on the other side of the wall.
"""
from __future__ import annotations

import json

import pytest

from training_gym.datasets.candidate import CandidateState, DatasetSplit
from training_gym.evaluation import pack_builder as PB
from training_gym.evaluation.task_pack import (
    EvaluationTaskKind,
    HiddenTargetError,
    TaskPackError,
)
from training_gym.task_spec import TaskFamily

pytest.importorskip("scripts.build_evaluation_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402

HELD_OUT = [DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
            DatasetSplit.ADVERSARIAL]


@pytest.fixture(scope="module")
def corpus_root(tmp_path_factory):
    """One promoted version, built through the real chain, shared by every test."""
    root = tmp_path_factory.mktemp("packbuilder")
    BC.build(root)
    return root


@pytest.fixture(scope="module")
def built(corpus_root):
    return PB.build_task_pack_from_dataset(
        root=corpus_root, dataset_id=BC.DATASET_ID,
        dataset_version=BC.DATASET_VERSION, splits=HELD_OUT)


def build(corpus_root, **overrides):
    kwargs = {"root": corpus_root, "dataset_id": BC.DATASET_ID,
              "dataset_version": BC.DATASET_VERSION, "splits": HELD_OUT}
    kwargs.update(overrides)
    return PB.build_task_pack_from_dataset(**kwargs)


# ── the shards are read and verified ──────────────────────────────────────────
def test_every_selected_shard_is_read_and_its_digest_recorded(built):
    assert set(built.shard_hashes) == {s.value for s in HELD_OUT}
    assert all(len(h) == 64 for h in built.shard_hashes.values())


def test_the_shard_digests_match_the_manifest(built, corpus_root):
    from training_gym.datasets.manifests import load_manifest
    manifest = load_manifest(root=corpus_root, dataset_id=BC.DATASET_ID,
                             dataset_version=BC.DATASET_VERSION)
    assert built.dataset_manifest_hash == manifest.manifest_hash()
    for split in HELD_OUT:
        assert built.shard_hashes[split.value] == manifest.shard_for(split).sha256_file


def test_a_tampered_shard_is_refused(corpus_root, tmp_path):
    """The loader re-hashes from disk; a rewritten record cannot pass as promoted."""
    import shutil
    from training_gym.datasets.manifests import shard_filename, version_dir
    root = tmp_path / "tampered"
    shutil.copytree(corpus_root, root)
    path = version_dir(root, BC.DATASET_ID, BC.DATASET_VERSION) / shard_filename(
        DatasetSplit.ADVERSARIAL)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["user_prompt"] = "a different question entirely"
    lines[0] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(Exception):
        build(root)


# ── which splits may be drawn from ────────────────────────────────────────────
def test_train_is_excluded_unconditionally(corpus_root):
    with pytest.raises(PB.PackBuilderError, match="expected outcome"):
        build(corpus_root, splits=[DatasetSplit.TRAIN])


def test_train_cannot_be_smuggled_in_alongside_held_out_splits(corpus_root):
    with pytest.raises(PB.PackBuilderError):
        build(corpus_root, splits=[DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.TRAIN])


def test_quarantine_is_never_model_input(corpus_root):
    with pytest.raises(PB.PackBuilderError, match="set aside"):
        build(corpus_root, splits=[DatasetSplit.QUARANTINE])


@pytest.mark.parametrize("split", HELD_OUT)
def test_each_held_out_split_loads_on_its_own(corpus_root, split):
    result = build(corpus_root, splits=[split])
    assert set(result.pack.counts_by_split()) == {split.value}
    assert len(result.pack) >= 8


def test_an_unknown_split_is_refused(corpus_root):
    with pytest.raises(PB.PackBuilderError, match="not a split"):
        build(corpus_root, splits=["nonexistent_split"])


def test_no_split_at_all_is_refused(corpus_root):
    with pytest.raises(PB.PackBuilderError, match="no denominator"):
        build(corpus_root, splits=[])


def test_a_split_named_twice_is_refused(corpus_root):
    with pytest.raises(PB.PackBuilderError, match="twice"):
        build(corpus_root, splits=[DatasetSplit.ADVERSARIAL, DatasetSplit.ADVERSARIAL])


def test_split_order_does_not_change_the_pack(corpus_root):
    forward = build(corpus_root, splits=HELD_OUT)
    backward = build(corpus_root, splits=list(reversed(HELD_OUT)))
    assert forward.pack.pack_hash() == backward.pack.pack_hash()


# ── the answers stay hidden ───────────────────────────────────────────────────
def test_no_task_carries_a_field_that_could_hold_an_answer(built):
    for task in built.pack.tasks:
        record = task.to_dict()
        for forbidden in ("target", "target_text", "answer", "expected", "solution",
                          "gold", "ground_truth", "rubric"):
            assert forbidden not in record, f"{task.task_id}: {forbidden}"


def test_no_expected_answer_appears_anywhere_in_the_model_facing_json(built,
                                                                     corpus_root):
    """The strongest form: serialise the whole pack and search it for every answer."""
    serialised = json.dumps(built.pack.to_record(), sort_keys=True).casefold()
    for task_id in built.targets.task_ids():
        target = built.targets.lookup(
            task_id, task_hash=built.pack.by_id(task_id).task_hash)
        # Compare on a distinctive slice; a short shared word proves nothing.
        fragment = " ".join(target.target_text.split())[:96].casefold()
        assert len(fragment) > 32
        assert fragment not in serialised, task_id


def test_every_task_has_exactly_one_hidden_target(built):
    assert len(built.targets) == len(built.pack)
    assert set(built.targets.task_ids()) == {t.task_id for t in built.pack.tasks}


def test_the_store_is_frozen_before_it_is_returned(built):
    assert built.targets.frozen


def test_the_store_exports_digests_and_never_text(built):
    exported = json.dumps(built.targets.to_dict(), sort_keys=True).casefold()
    for task_id in built.targets.task_ids():
        target = built.targets.lookup(
            task_id, task_hash=built.pack.by_id(task_id).task_hash)
        assert " ".join(target.target_text.split())[:64].casefold() not in exported


def test_a_target_lookup_requires_the_matching_task_hash(built):
    task_id = built.pack.tasks[0].task_id
    with pytest.raises(HiddenTargetError):
        built.targets.lookup(task_id, task_hash="0" * 64)


def test_a_duplicate_hidden_target_is_refused(built):
    from training_gym.evaluation.task_pack import HiddenTargetStore
    store = HiddenTargetStore(generation=1)
    task_id = built.pack.tasks[0].task_id
    target = built.targets.lookup(task_id,
                                  task_hash=built.pack.by_id(task_id).task_hash)
    store.add(target)
    with pytest.raises(HiddenTargetError):
        store.add(target)


# ── the schemas the builder attaches ──────────────────────────────────────────
@pytest.mark.parametrize("leak_key", ["const", "default", "example", "examples",
                                      "expected", "answer", "target", "solution",
                                      "gold", "ground_truth", "rubric"])
def test_a_response_schema_naming_the_answer_is_refused(corpus_root, leak_key):
    """Each of these publishes an expected value whatever it contains."""
    from training_gym.evaluation.task_pack import answer_exposure
    assert answer_exposure({"type": "object", leak_key: "anything"})


def test_a_single_member_enum_is_treated_as_a_const():
    from training_gym.evaluation.task_pack import answer_exposure
    assert answer_exposure({"enum": ["only_possible_answer"]})
    assert not answer_exposure({"enum": ["one", "two"]})


def test_a_schema_description_that_spells_the_answer_out_is_refused(corpus_root):
    from training_gym.evaluation.task_pack import schema_target_leak
    target = ("the analyst must record severity medium with supporting signals "
              "burst of failures and a single source host and a terminal success")
    leaking = {"type": "object", "description": target}
    assert schema_target_leak(leaking, target, path="expected_schema")


def test_every_attached_schema_is_free_of_exposure(built):
    from training_gym.evaluation.task_pack import answer_exposure
    for task in built.pack.tasks:
        assert not answer_exposure(task.expected_output_schema)
        for schema in task.tool_schemas:
            assert not answer_exposure(schema)


def test_a_family_with_no_grader_mapping_is_refused():
    with pytest.raises(PB.PackBuilderError, match="no grader mapping"):
        PB.graders_for(TaskFamily.YARA_RULE)


def test_a_family_with_no_response_schema_is_refused():
    with pytest.raises(PB.PackBuilderError, match="no response schema"):
        PB.response_schema_for(TaskFamily.SIGMA_RULE)


def test_every_declared_grader_is_one_the_repository_implements():
    from training_gym.graders import DEFAULT_GRADERS
    for graders, mandatory in PB.GRADER_REGISTRY.values():
        for grader_id in set(graders) | set(mandatory):
            assert grader_id in DEFAULT_GRADERS, grader_id


def test_secrets_are_graded_in_every_family():
    """A response that leaks a secret has failed whatever else it got right."""
    for _graders, mandatory in PB.GRADER_REGISTRY.values():
        assert "secret_pii" in mandatory


def test_tool_schemas_are_attached_only_where_a_tool_call_is_asked_for(built):
    for task in built.pack.tasks:
        if task.task_family is TaskFamily.TOOL_CALL_SCHEMA:
            assert task.tool_schemas
        else:
            assert task.tool_schemas == ()


def test_no_tool_schema_exposes_an_expected_target(built):
    serialised = json.dumps([s for t in built.pack.tasks for s in t.tool_schemas],
                            sort_keys=True).casefold()
    for task_id in built.targets.task_ids():
        target = built.targets.lookup(
            task_id, task_hash=built.pack.by_id(task_id).task_hash)
        assert " ".join(target.target_text.split())[:96].casefold() not in serialised


# ── determinism ───────────────────────────────────────────────────────────────
def test_the_pack_hash_is_deterministic(corpus_root):
    assert build(corpus_root).pack.pack_hash() == build(corpus_root).pack.pack_hash()


def test_the_task_ordering_is_deterministic_and_not_insertion_order(corpus_root):
    first = [t.task_id for t in build(corpus_root).pack.tasks]
    second = [t.task_id for t in build(corpus_root, splits=list(reversed(HELD_OUT)))
              .pack.tasks]
    assert first == second
    assert first == sorted(
        first, key=lambda i: (build(corpus_root).pack.by_id(i).task_hash, i))


def test_the_manifest_reloads_and_verifies(built, corpus_root):
    manifest = built.manifest()
    assert manifest["pack_hash"] == built.pack.pack_hash()
    assert manifest["task_count"] == len(built.pack)
    rebuilt = build(corpus_root)
    assert rebuilt.manifest_hash() == built.manifest_hash()


def test_the_manifest_carries_no_prompt_and_no_answer(built):
    serialised = json.dumps(built.manifest(), sort_keys=True).casefold()
    for task in built.pack.tasks:
        assert " ".join(task.user_prompt.split())[:96].casefold() not in serialised


# ── the counts a gate will read ───────────────────────────────────────────────
def test_the_family_counts_are_correct(built):
    expected: dict[str, int] = {}
    for _split, family, *_ in BC.corpus():
        value = getattr(TaskFamily, family).value
        expected[value] = expected.get(value, 0) + 1
    assert built.pack.counts_by_family() == expected


def test_the_split_counts_are_correct(built):
    expected: dict[str, int] = {}
    for split, *_ in BC.corpus():
        expected[split.lower()] = expected.get(split.lower(), 0) + 1
    assert built.pack.counts_by_split() == expected


def test_the_decision_class_counts_are_correct(built):
    kinds = built.counts_by_kind()
    assert kinds[EvaluationTaskKind.REQUIRED_REFUSAL.value] > 0
    assert kinds[EvaluationTaskKind.REQUIRED_COMPLETION.value] > 0
    assert sum(kinds.values()) == len(built.pack)


def test_a_refusal_task_is_marked_as_one(built):
    for task in built.pack.tasks:
        if task.task_family is TaskFamily.SAFETY_REFUSAL:
            assert task.kind is EvaluationTaskKind.REQUIRED_REFUSAL
            assert task.refusal_expected
            assert task.security_required


# ── the claim gate ────────────────────────────────────────────────────────────
def test_the_full_corpus_supports_a_claim(built):
    assert PB.pack_blockers(built, min_tasks=30,
                            mandatory_families=("safety_refusal",
                                                "tool_call_schema")) == ()


def test_too_few_pairs_remains_insufficient(built):
    problems = PB.pack_blockers(built, min_tasks=500)
    assert any("below the policy minimum" in p for p in problems)


def test_a_missing_mandatory_family_is_a_blocker(built):
    problems = PB.pack_blockers(built, min_tasks=1,
                                mandatory_families=("sigma_rule",))
    assert any("sigma_rule" in p for p in problems)


def test_a_pack_with_no_required_refusal_is_a_blocker(corpus_root):
    """Without one, a model that refuses everything looks safe."""
    result = build(corpus_root, splits=[DatasetSplit.HIDDEN_EVALUATION])
    only_completions = [t for t in result.pack.tasks
                        if t.kind is not EvaluationTaskKind.REQUIRED_REFUSAL]
    from training_gym.evaluation.task_pack import EvaluationTaskPack
    trimmed = PB.BuiltPack(
        pack=EvaluationTaskPack(tasks=tuple(only_completions),
                                dataset_id=BC.DATASET_ID,
                                dataset_version=BC.DATASET_VERSION),
        targets=result.targets, dataset_id=BC.DATASET_ID,
        dataset_version=BC.DATASET_VERSION,
        dataset_manifest_hash=result.dataset_manifest_hash)
    assert any("refus" in p for p in PB.pack_blockers(trimmed, min_tasks=1))


def test_a_missing_mandatory_split_is_a_blocker(corpus_root):
    result = build(corpus_root, splits=[DatasetSplit.ADVERSARIAL])
    problems = PB.pack_blockers(result, min_tasks=1)
    assert any("security_regression" in p for p in problems)
    assert any("hidden_evaluation" in p for p in problems)


# ── records that must not become tasks ────────────────────────────────────────
def test_a_non_promoted_record_is_refused(corpus_root, tmp_path):
    import shutil
    from training_gym.datasets.manifests import shard_filename, version_dir
    root = tmp_path / "unpromoted"
    shutil.copytree(corpus_root, root)
    path = version_dir(root, BC.DATASET_ID, BC.DATASET_VERSION) / shard_filename(
        DatasetSplit.ADVERSARIAL)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["state"] = CandidateState.QUARANTINED.value
    lines[0] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(Exception):
        build(root)


def test_a_prompt_containing_its_own_answer_is_refused(corpus_root):
    """`build_task` refuses before the task object exists; asserted directly."""
    from training_gym.evaluation.task_pack import build_task
    from training_gym.datasets.manifests import read_shard, shard_filename, version_dir
    from dataclasses import replace
    path = version_dir(corpus_root, BC.DATASET_ID, BC.DATASET_VERSION) / shard_filename(
        DatasetSplit.HIDDEN_EVALUATION)
    candidate = read_shard(path)[0]
    leaking = replace(candidate, user_prompt=candidate.target_text * 3)
    with pytest.raises(TaskPackError, match="already contains the expected answer"):
        build_task(leaking, split=DatasetSplit.HIDDEN_EVALUATION,
                   dataset_manifest_hash="a" * 64, shard_hash="b" * 64,
                   grader_ids=("secret_pii",), mandatory_grader_ids=("secret_pii",))
