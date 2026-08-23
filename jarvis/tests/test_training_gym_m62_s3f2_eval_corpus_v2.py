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
#: **Updated by S3I.1 under operator decision D34, and the corpus did not change.**
#:
#: This constant read ``10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0``
#: when S3F.2 wrote it, because this file builds each version into its own fresh root and
#: the builder used to *discover* ``v2``'s parent from the destination root — finding
#: nothing, and promoting ``v2`` as a genesis. That digest is the honest output of that
#: build and is retained as history in
#: ``jarvis/docs/V69_M62_S3I1_KALI_RUNTIME_AND_CANONICAL_LINEAGE.md``; it is not corrupt
#: and it is not being erased. D34 rules that ``v2`` derives from ``v1`` and must bind it
#: explicitly, so the canonical digest is now the parented one. The three task shards are
#: byte-identical across both lineages — see the D34 regression file.
V2_MANIFEST_HASH = "82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60"
#: The pre-D34 genesis-lineage digest. Kept so the distinction stays testable.
V2_HISTORICAL_GENESIS_MANIFEST_HASH = (
    "10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0")
V1_PACK_HASH = "d714d89bb1842789ec254c4d14de1c467944d0d769b5b44367bd822e1655f1f0"
#: **Updated by S3I.1 with the manifest hash above, and for the same reason.** A task
#: record carries ``source_dataset_manifest_hash`` as its provenance, so the pack's
#: identity follows the dataset version's identity. Across the two lineages that field is
#: the *only* one of the 22 task-record fields that moves: ``user_prompt``,
#: ``system_prompt``, ``task_hash``, ``expected_output_schema``, ``tool_schemas``,
#: ``grader_ids``, ``refusal_expected``, ``kind``, ``split``, ``task_family`` and
#: ``source_shard_hash`` are byte-identical in all 36. The model sees exactly what it saw.
#: The pre-D34 value was
#: ``b4f9d6b1f81ff13cc45d72e612a717b126bfcb64cccf326c2dc9b4b58abade11``.
V2_PACK_HASH = "3744a22e1866a40b6e5b27ae20e798365dfbf2d3c071018afba14bf611ec2665"

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
    """The version list moves when a version is added; ``v2`` never stops existing.

    V69 M62 S3J added ``v3``, the fresh eligibility holdout for the second quality
    candidate, V69 M62 S3N added ``v4``, the one frozen before any third candidate
    existed, and V69 M62 S3S added ``v5``, the one frozen before any fourth candidate
    exists — each moving the "which version does an eligibility run bind" pointer onto
    itself. ``v2`` remains buildable and remains the corpus of record for the S3I LIVE
    measurement — every other assertion in this file still measures it.
    """
    # S3X.1 added v6, the fresh holdout replacing the retired v5, and moved
    # LATEST_DATASET_VERSION onto it -- the same rescoping S3N, S3S and this file have
    # each performed before. What S3F.2 owns is that v1 and v2 still exist and still
    # build; a LATER version existing is not evidence about S3F.2.
    assert sorted(_BUILDER.CORPUS_VERSIONS)[:2] == ["v1", "v2"]
    assert "v2" in _BUILDER.CORPUS_VERSIONS
    assert _BUILDER.LATEST_DATASET_VERSION >= "v2"


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


# ══════════════════════════════════════════════════════════════════════════════
#  What a request built from v2 under the approved policy actually carries
# ══════════════════════════════════════════════════════════════════════════════
def _requests(pack, policy):
    """One baseline and one candidate request per task. Nothing is generated."""
    from training_gym.evaluation.backend import EvaluationRequest, EvaluationRole

    from _m62_evaluation_fixtures import adapter_reference, baseline_reference
    baseline, adapter = baseline_reference(), adapter_reference()
    return [(EvaluationRequest(role=EvaluationRole.BASELINE, task=task,
                               generation=policy, baseline=baseline, adapter=None),
             EvaluationRequest(role=EvaluationRole.CANDIDATE, task=task,
                               generation=policy, baseline=baseline, adapter=adapter))
            for task in pack.tasks]


def test_both_arms_see_a_byte_identical_prompt_under_the_approved_policy(promoted):
    from training_gym.evaluation.generation import eligibility_generation_policy

    pack = _pack(promoted["v2"][0], "v2").pack
    policy = eligibility_generation_policy(seed=11)
    for base, cand in _requests(pack, policy):
        assert base.task.system_prompt == cand.task.system_prompt
        assert base.task.user_prompt == cand.task.user_prompt
        assert base.generation.policy_hash() == cand.generation.policy_hash()
        assert base.generation.reasoning_policy.value == "disabled"
        assert cand.generation.reasoning_policy.value == "disabled"
        # The adapter is the only permitted difference between the two arms.
        assert base.parity_hash() == cand.parity_hash()
        assert base.adapter is None and cand.adapter is not None


def test_the_parity_hash_over_v2_moves_when_the_reasoning_policy_moves(promoted):
    from training_gym.evaluation.generation import (
        GenerationPolicy,
        eligibility_generation_policy,
    )

    pack = _pack(promoted["v2"][0], "v2").pack
    disabled = _requests(pack, eligibility_generation_policy(seed=11))[0][0]
    default = _requests(pack, GenerationPolicy(seed=11))[0][0]
    assert disabled.parity_hash() != default.parity_hash()


def test_the_v2_expected_output_schema_stays_model_safe(promoted):
    """A schema listing the permitted values of a verdict field would publish the answer
    key under a heading. The registry schema is structural and content-free, and v2 does
    not change it."""
    pack = _pack(promoted["v2"][0], "v2").pack
    for task in pack.tasks:
        body = json.dumps(task.expected_output_schema)
        for publishing_keyword in ("enum", "const", "default", "examples"):
            assert publishing_keyword not in body, task.task_id
        assert "insufficient_evidence" not in body and "refuse" not in body


def test_a_v2_request_carries_no_hidden_target_field(promoted):
    from training_gym.evaluation.generation import eligibility_generation_policy

    pack = _pack(promoted["v2"][0], "v2").pack
    described = json.dumps([base.to_public_dict() for base, _cand
                            in _requests(pack, eligibility_generation_policy(seed=11))])
    for field in ("target", "expected_answer", "teacher", "rubric", "answer_key"):
        assert field not in described
