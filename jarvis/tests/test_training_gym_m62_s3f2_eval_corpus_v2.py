"""V69 M62 S3F.2 — ``m62-defensive-eval v2``: the corpus that states its output contract.

S3F.1 found that the instrument was qualified against an instruction the corpus does not
give: the synthetic fixture says *"Answer with JSON only."*, while all 36 ``v1`` tasks
carry an empty system prompt and the word "JSON" appears in none of the nine
``structured_report`` prompts. A model was graded against a schema nobody communicated to
it. Operator ruling **H6b** authorises stating the contract — as a NEW immutable dataset
version, because ``v1`` is a promoted, hash-verified version the S3E.2 measurement of
record was drawn from.

These tests pin both directions. ``v2`` must state the contract, and ``v2`` must differ
from ``v1`` in *nothing else*: not a split, not a family, not a decision class, not a
hidden target, and above all not in the direction of making the tasks easier by hinting at
the answer.
"""
from __future__ import annotations

import collections
import json

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.evaluation.pack_builder import build_task_pack_from_dataset

pytest.importorskip("scripts.build_evaluation_corpus")
from scripts import build_evaluation_corpus as _BUILDER  # noqa: E402

#: Recorded when this milestone built each version. A rebuild that does not reproduce
#: these exactly is a determinism failure, not a new baseline to write down.
V1_MANIFEST_HASH = "0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b"
V2_MANIFEST_HASH = "10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0"
V1_PACK_HASH = "d714d89bb1842789ec254c4d14de1c467944d0d769b5b44367bd822e1655f1f0"
V2_PACK_HASH = "b4f9d6b1f81ff13cc45d72e612a717b126bfcb64cccf326c2dc9b4b58abade11"

_SPLITS = (DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
           DatasetSplit.ADVERSARIAL)


@pytest.fixture(scope="module")
def promoted(tmp_path_factory):
    """Both versions, promoted once through the real authority chain."""
    out = {}
    for version in ("v1", "v2"):
        root = tmp_path_factory.mktemp(f"corpus-{version}")
        out[version] = (root, _BUILDER.build(root, dataset_version=version))
    return out


def _pack(root, version):
    return build_task_pack_from_dataset(
        root=root, dataset_id=_BUILDER.DATASET_ID, dataset_version=version,
        splits=_SPLITS, generation=1)


# ══════════════════════════════════════════════════════════════════════════════
#  v1 is untouched
# ══════════════════════════════════════════════════════════════════════════════
def test_v1_still_rebuilds_to_the_hash_it_always_had(promoted):
    """The S3E.2 measurement of record was drawn from this. A byte here restates history."""
    assert promoted["v1"][1]["manifest_hash"] == V1_MANIFEST_HASH


def test_the_v1_material_is_unchanged_by_the_v2_derivation():
    entries = _BUILDER.corpus()
    assert len(entries) == 36
    structured = [e for e in entries if e[1] == "STRUCTURED_REPORT"]
    assert len(structured) == 9
    assert not any(_BUILDER.STRUCTURED_OUTPUT_CONTRACT in e[3] for e in entries)
    assert not any("JSON" in e[3] for e in structured)


def test_the_v1_task_pack_hash_is_unchanged(promoted):
    root, _summary = promoted["v1"]
    assert _pack(root, "v1").pack.pack_hash() == V1_PACK_HASH


# ══════════════════════════════════════════════════════════════════════════════
#  v2 is a new identity, not an edit
# ══════════════════════════════════════════════════════════════════════════════
def test_v2_is_a_new_dataset_identity(promoted):
    v1, v2 = promoted["v1"][1], promoted["v2"][1]
    assert v2["dataset_id"] == v1["dataset_id"] == "m62-defensive-eval"
    assert v2["dataset_version"] == "v2"
    assert v2["manifest_hash"] == V2_MANIFEST_HASH
    assert v2["manifest_hash"] != v1["manifest_hash"]


def test_v2_rebuilds_deterministically(tmp_path):
    assert _BUILDER.build(tmp_path / "again",
                          dataset_version="v2")["manifest_hash"] == V2_MANIFEST_HASH


def test_an_unknown_dataset_version_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown dataset version"):
        _BUILDER.build(tmp_path / "v9", dataset_version="v9")


def test_the_generator_names_exactly_the_versions_it_can_build():
    assert sorted(_BUILDER.CORPUS_VERSIONS) == ["v1", "v2"]
    assert _BUILDER.LATEST_DATASET_VERSION == "v2"


# ══════════════════════════════════════════════════════════════════════════════
#  Every count is preserved
# ══════════════════════════════════════════════════════════════════════════════
def test_v2_holds_thirty_six_tasks(promoted):
    summary = promoted["v2"][1]
    assert summary["candidates_built"] == 36
    assert summary["promoted"] == 36
    assert summary["promoted_records"] == 36
    assert summary["excluded"] == [] and summary["quarantined"] == []


def test_v2_split_counts_are_unchanged(promoted):
    assert promoted["v2"][1]["by_split"] == promoted["v1"][1]["by_split"] == {
        "HIDDEN_EVALUATION": 12, "SECURITY_REGRESSION": 12, "ADVERSARIAL": 12}


def test_v2_family_counts_are_unchanged(promoted):
    assert promoted["v2"][1]["by_family"] == promoted["v1"][1]["by_family"] == {
        "SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9, "EVIDENCE_REQUEST": 9,
        "TOOL_CALL_SCHEMA": 6}


def test_v2_decision_class_counts_are_unchanged(promoted):
    counts = {}
    for version in ("v1", "v2"):
        pack = _pack(promoted[version][0], version).pack
        counts[version] = dict(collections.Counter(t.kind.value for t in pack.tasks))
    assert counts["v2"] == counts["v1"] == {
        "required_refusal": 12, "required_completion": 6, "completion": 18}


def test_v2_carries_no_train_and_no_validation_material(promoted):
    assert "TRAIN" not in promoted["v2"][1]["by_split"]
    assert "VALIDATION" not in promoted["v2"][1]["by_split"]


def test_v2_remains_evaluation_only_and_dataset_ineligible():
    for entry in _BUILDER.corpus_v2():
        candidate = _BUILDER.make_candidate(entry)
        assert candidate.evaluation_only is True
        assert candidate.dataset_eligible is False


def test_v2_leakage_is_clean(promoted):
    summary = promoted["v2"][1]
    assert summary["leakage_verdict"] == "clean"
    assert summary["leakage_findings"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  Exactly one field changed, on exactly nine records
# ══════════════════════════════════════════════════════════════════════════════
def test_only_the_prompt_of_the_structured_report_tasks_changed():
    v1 = {e[2]: e for e in _BUILDER.corpus()}
    v2 = {e[2]: e for e in _BUILDER.corpus_v2()}
    assert set(v1) == set(v2)
    changed = [task_id for task_id in v1 if v1[task_id] != v2[task_id]]
    assert len(changed) == 9
    for task_id in v1:
        before, after = v1[task_id], v2[task_id]
        # split, family, task_id and target are identical for every record.
        assert before[0] == after[0] and before[1] == after[1]
        assert before[2] == after[2] and before[4] == after[4]
        if task_id in changed:
            assert after[1] == "STRUCTURED_REPORT"
            assert after[3] == before[3] + _BUILDER.STRUCTURED_OUTPUT_CONTRACT
        else:
            assert after[3] == before[3]


def test_the_hidden_targets_are_unchanged_and_still_hidden(promoted):
    v1 = {e[2]: e[4] for e in _BUILDER.corpus()}
    v2 = {e[2]: e[4] for e in _BUILDER.corpus_v2()}
    assert v1 == v2
    built = _pack(promoted["v2"][0], "v2")
    model_facing = json.dumps(built.pack.task_records())
    # A tool-call target names a tool from the catalogue the model is legitimately shown,
    # so its ``tool`` value is published by design and is not the answer key. Every other
    # value in every target is the answer, and none of them may appear.
    for _split, family, task_id, _prompt, target in _BUILDER.corpus_v2():
        for key, value in json.loads(target).items():
            if family == "TOOL_CALL_SCHEMA" and key == "tool":
                continue
            if isinstance(value, str) and len(value) > 8:
                assert value not in model_facing, (task_id, key)


def test_no_v2_prompt_reveals_an_expected_answer():
    """The contract correction is FORMAT CLARITY, not target leakage."""
    forbidden = ("refuse", "severity", "informational", "true_positive",
                 "insufficient_evidence", "credential_access", "expected_change",
                 "correct_refusal", "unsafe", "grader", "rubric", "score")
    for _split, family, task_id, prompt, _target in _BUILDER.corpus_v2():
        if family not in _BUILDER.CONTRACT_FAMILIES:
            continue
        added = prompt[len(prompt) - len(_BUILDER.STRUCTURED_OUTPUT_CONTRACT):]
        assert added == _BUILDER.STRUCTURED_OUTPUT_CONTRACT, task_id
        lowered = added.lower()
        for word in forbidden:
            assert word not in lowered, (task_id, word)


def test_the_contract_sentence_is_identical_on_every_task_it_applies_to():
    added = {p[len(p) - len(_BUILDER.STRUCTURED_OUTPUT_CONTRACT):]
             for _s, f, _t, p, _g in _BUILDER.corpus_v2()
             if f in _BUILDER.CONTRACT_FAMILIES}
    assert added == {_BUILDER.STRUCTURED_OUTPUT_CONTRACT}


# ══════════════════════════════════════════════════════════════════════════════
#  What the model actually sees
# ══════════════════════════════════════════════════════════════════════════════
def test_every_structured_report_prompt_explicitly_requires_json(promoted):
    pack = _pack(promoted["v2"][0], "v2").pack
    structured = [t for t in pack.tasks
                  if t.task_family.value == "structured_report"]
    assert len(structured) == 9
    for task in structured:
        assert "JSON" in task.user_prompt, task.task_id
        assert "single JSON object" in task.user_prompt


def test_no_other_family_gained_a_json_instruction(promoted):
    pack = _pack(promoted["v2"][0], "v2").pack
    for task in pack.tasks:
        if task.task_family.value == "structured_report":
            continue
        assert _BUILDER.STRUCTURED_OUTPUT_CONTRACT not in task.user_prompt, task.task_id


def test_the_tool_call_family_is_deliberately_untouched(promoted):
    """The production backend never populates ``proposed_tool_calls``, so it has no
    transport by which a tool call could be observed however the model formats one.
    Instructing a format the instrument cannot read would change the prompt without
    changing what is measured."""
    assert _BUILDER.CONTRACT_FAMILIES == frozenset({"STRUCTURED_REPORT"})
    v1 = {e[2]: e[3] for e in _BUILDER.corpus() if e[1] == "TOOL_CALL_SCHEMA"}
    v2 = {e[2]: e[3] for e in _BUILDER.corpus_v2() if e[1] == "TOOL_CALL_SCHEMA"}
    assert v1 == v2 and len(v2) == 6


def test_the_response_and_tool_schemas_are_preserved(promoted):
    v1 = _pack(promoted["v1"][0], "v1").pack
    v2 = _pack(promoted["v2"][0], "v2").pack
    for label in ("expected_output_schema", "tool_schemas"):
        by_family_v1 = {t.task_family.value: json.dumps(getattr(t, label),
                                                        sort_keys=True, default=str)
                        for t in v1.tasks}
        by_family_v2 = {t.task_family.value: json.dumps(getattr(t, label),
                                                        sort_keys=True, default=str)
                        for t in v2.tasks}
        assert by_family_v1 == by_family_v2, label


def test_the_grader_mapping_is_preserved(promoted):
    v1 = {t.task_family.value: tuple(sorted(t.grader_ids))
          for t in _pack(promoted["v1"][0], "v1").pack.tasks}
    v2 = {t.task_family.value: tuple(sorted(t.grader_ids))
          for t in _pack(promoted["v2"][0], "v2").pack.tasks}
    assert v1 == v2


# ══════════════════════════════════════════════════════════════════════════════
#  The pack
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v2_task_pack_has_a_new_deterministic_hash(promoted):
    built = _pack(promoted["v2"][0], "v2")
    assert len(built.pack) == 36
    assert len({t.task_id for t in built.pack.tasks}) == 36
    assert built.pack.pack_hash() == V2_PACK_HASH
    assert built.pack.pack_hash() != V1_PACK_HASH
    assert _pack(promoted["v2"][0], "v2").pack.pack_hash() == V2_PACK_HASH


def test_the_v2_hidden_target_store_is_frozen(promoted):
    built = _pack(promoted["v2"][0], "v2")
    assert built.targets.frozen is True
    assert len(built.targets.store_hash()) == 64


def test_the_v2_pack_is_ordered_deterministically(promoted):
    tasks = _pack(promoted["v2"][0], "v2").pack.tasks
    assert list(tasks) == sorted(tasks, key=lambda t: (t.task_hash, t.task_id))


def test_the_v2_pack_hash_is_not_the_plan_time_commitment_digest(promoted):
    """Two different digests by design: the pack hash covers the built tasks, the
    commitment digest covers the dataset manifest and its split counts."""
    built = _pack(promoted["v2"][0], "v2")
    assert built.pack.pack_hash() != promoted["v2"][1]["manifest_hash"]
