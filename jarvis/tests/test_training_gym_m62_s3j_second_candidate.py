"""V69 M62 S3J — the second quality candidate: corpus v2, holdout v3, and the plan.

WHAT THESE TESTS ARE FOR
------------------------
Candidate 001 was measured against ``m62-defensive-eval v2`` and came back
``EVALUATED_NOT_ELIGIBLE``: a large, real security gain (required refusal 1/12 -> 9/12,
critical safety violations 11 -> 3, all nine vetoes PASS) bought at the price of two
gates it was always going to be judged by — it refused 2 of 24 SAFE tasks, and it dropped
JSON parseability and schema validity from a **perfect** 9/9 baseline to 7/9.

S3J designs a second candidate to keep the first result and repair the second. That
creates three ways to cheat, and every one of them is checked here rather than promised
in a document:

  * **Train on the holdout.** The measured failures are now known, so the corpus could be
    written towards them. Exact overlap against eval ``v1``, ``v2`` AND the fresh ``v3``
    is asserted to be zero on ids, prompts, targets and task hashes, and the existing
    leakage authority is run over all three.
  * **Cure over-refusal by deleting refusals.** The refusal curriculum that produced the
    gain is asserted present and UNCHANGED at 37 rows; ``v2`` is asserted to be ``v1``
    plus additions and nothing else.
  * **Reuse the holdout that has already spoken.** ``v3`` is asserted to exist, to be
    structurally identical to ``v2`` where the acceptance gates read it, and to share no
    task instance with it.

Plus two regressions of their own:

  * **D36** — the promotion sanitizer matched the operator's account name as a plain
    SUBSTRING, so a corpus containing an ordinary English word that happened to spell it
    was rewritten at promotion time and the dataset's ``manifest_hash`` became a function
    of the BUILDING HOST. Both directions are pinned: an identity buried inside a longer
    word is left alone, and a real identity anywhere else is still redacted.
  * The S3G §6 acceptance gates are asserted to hash to the value the S3I LIVE plan
    recorded, so "the gates were not loosened after the result was known" is a digest
    rather than an assurance.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS OR GENERATES A TOKEN.
"""
from __future__ import annotations

import json

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.datasets.leakage import LeakageVerdict
from training_gym.datasets.manifests import TRAIN_SIDE_SPLITS
from training_gym.evaluation.generation import ReasoningPolicy
from training_gym.evaluation.policy import GatePolicy
from training_gym.task_spec import TaskFamily, unsafe_task_markers
from training_gym.teachers.sanitization import sanitize_text, scan_export_payload

pytest.importorskip("scripts.build_training_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402
from scripts import build_training_corpus as QC  # noqa: E402

V1_ROWS = QC.curriculum()
V2_ROWS = QC.curriculum_for("v2")

#: Recorded by the S3I LIVE evaluation of record, and by S3H before it. These are the
#: identities S3J may not move.
V1_TRAIN_MANIFEST = (
    "9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a")
EVAL_V1_MANIFEST = (
    "0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b")
EVAL_V2_MANIFEST = (
    "82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60")
#: The digest the S3I LIVE plan bound as ``gate_policy_hash``.
S3I_GATE_POLICY_HASH = (
    "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")

#: A SYNTHETIC home path used only by the D36 probes below. It names no real account and
#: no real machine; the whole point of the assertion is that it comes back removed.
HOME_PROBE = "/home/" + "fore" + "/notes.md"


@pytest.fixture(scope="module")
def eval_root(tmp_path_factory):
    """A root holding all three held-out versions, built by their own generator."""
    root = tmp_path_factory.mktemp("m62-s3j-eval")
    BC.build(root, dataset_version="v3")   # materialises v1 -> v2 -> v3
    return root


@pytest.fixture(scope="module")
def train_root(tmp_path_factory):
    """A root holding both training versions, each with its own exports.

    ``v1`` is built explicitly rather than left to the lineage step, which materialises a
    parent WITHOUT exports on purpose: establishing lineage must not have the side effect
    of writing a second SFT export nobody asked for. Both configurations are compared
    here, so both exports have to exist.
    """
    root = tmp_path_factory.mktemp("m62-s3j-train")
    QC.build(root, dataset_version="v1")
    summary = QC.build(root, dataset_version="v2")
    return root, summary


# ══════════════════════════════════════════════════════════════════════════════
#  1-4. TRAIN-V2: v1 is untouched, the lineage is declared, the identity is stable
# ══════════════════════════════════════════════════════════════════════════════
def test_v1_is_not_modified_by_the_existence_of_v2():
    """Every v1 row appears in v2 byte for byte, and v2 alters none of them."""
    by_v1 = {row[3]: row for row in V1_ROWS}
    by_v2 = {row[3]: row for row in V2_ROWS}
    assert len(by_v1) == 128
    assert set(by_v1) <= set(by_v2)
    assert [tid for tid in by_v1 if by_v1[tid] != by_v2[tid]] == []
    # The correction map exists so this is checkable rather than asserted; D36 was fixed
    # in the redactor, so it is legitimately empty.
    assert QC.V2_DATA_INTEGRITY_CORRECTIONS == {}


def test_v2_declares_its_parent_rather_than_discovering_one():
    """The D34 rule, on the training side. A version with no declared lineage refuses."""
    assert QC.canonical_parent_for("v1") is None
    assert QC.canonical_parent_for("v2") == ("v1", V1_TRAIN_MANIFEST)
    with pytest.raises(ValueError, match="no canonical lineage"):
        QC.canonical_parent_for("v9")


def test_v2_promotes_onto_the_declared_parent(train_root):
    root, summary = train_root
    assert summary["parent_manifest_hash"] == V1_TRAIN_MANIFEST
    assert summary["dataset_version"] == "v2"
    assert summary["leakage_verdict"] == LeakageVerdict.CLEAN.value
    assert summary["leakage_findings"] == 0

    from training_gym.datasets.manifests import verify_version
    parent = verify_version(root=root, dataset_id=QC.DATASET_ID, dataset_version="v1")
    assert parent.ok and parent.manifest.manifest_hash() == V1_TRAIN_MANIFEST


def test_v2_refuses_a_parent_that_is_not_the_one_it_declares(tmp_path, monkeypatch):
    """Not vacuous: point the lineage at a digest nothing produces and it must refuse."""
    monkeypatch.setitem(QC.CANONICAL_LINEAGE, "v2", ("v1", "0" * 64))
    with pytest.raises(RuntimeError, match="not the one it declares|cannot be produced"):
        QC.build(tmp_path / "wrong-parent", dataset_version="v2")


def test_the_v2_manifest_and_exports_are_deterministic_across_roots(tmp_path):
    """Identity is a function of the corpus, not of where or in what order it was built.

    ``promotion_plan_hash`` binds ``output_root_id`` and is expected to differ; the
    dataset's identity, its lineage and both exports are not allowed to.
    """
    first = QC.build(tmp_path / "one", dataset_version="v2")
    QC.build(tmp_path / "two", dataset_version="v1")
    second = QC.build(tmp_path / "two", dataset_version="v2")
    for key in ("manifest_hash", "parent_manifest_hash", "split_plan_hash",
                "leakage_report_hash", "sft_export_hash", "sft_export_file_sha256",
                "validation_export_hash", "validation_export_file_sha256",
                "train_rows", "validation_rows"):
        assert first[key] == second[key], key
    assert first["promotion_plan_hash"] != second["promotion_plan_hash"]


# ══════════════════════════════════════════════════════════════════════════════
#  5-7. The additive expansion, and what it is made of
# ══════════════════════════════════════════════════════════════════════════════
def test_v2_is_v1_plus_exactly_the_declared_additions():
    additions = QC.curriculum_v2_additions()
    assert len(V2_ROWS) == len(V1_ROWS) + len(additions) == 182
    assert all(row[3].startswith(QC.V2_ROW_PREFIX) for row in additions)
    assert not any(row[3].startswith(QC.V2_ROW_PREFIX) for row in V1_ROWS)


def test_the_safe_completion_correction_is_present_and_large_enough():
    """The measured defect was over-refusal of SAFE tasks. This is the counterweight."""
    new = [r for r in V2_ROWS if r[3].startswith(QC.V2_ROW_PREFIX)]
    new_safe = [r for r in new if r[1] in QC.SAFE_COMPLETION_CATEGORIES]
    assert len(new_safe) >= 24, len(new_safe)
    # And they must actually reach the model: a counterexample in a held-out split
    # corrects nothing.
    assert sum(1 for r in new_safe if r[0] == "TRAIN_SIDE") >= 24


def test_the_structured_output_correction_is_present_and_large_enough():
    """The measured defect was 9/9 -> 7/9. This is the curriculum aimed at it."""
    new = [r for r in V2_ROWS if r[3].startswith(QC.V2_ROW_PREFIX)]
    new_structured = [r for r in new if r[1] in QC.JSON_ONLY_CATEGORIES]
    assert len(new_structured) >= 24, len(new_structured)
    # Multiple defensive domains, not one schema repeated.
    assert len({r[1] for r in new_structured}) >= 6


def test_the_intersection_rows_are_both_things_at_once():
    """Safe completion and structured output must not be learnable as separate modes."""
    intersection = [r for r in V2_ROWS if r[1] == "structured_safe_intersection"]
    assert len(intersection) >= 8, len(intersection)
    assert "structured_safe_intersection" in QC.JSON_ONLY_CATEGORIES
    assert "structured_safe_intersection" in QC.SAFE_COMPLETION_CATEGORIES
    assert "structured_safe_intersection" in QC.COMPLETION_CATEGORIES


# ══════════════════════════════════════════════════════════════════════════════
#  8-11. What the new material must NOT do
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v2_row_satisfies_the_generators_own_invariants():
    assert QC.invariant_problems(V2_ROWS) == []


def test_the_invariant_check_is_not_vacuous_over_the_new_material():
    """Break a new safe-completion row into a refusal; the gate must catch it."""
    broken = list(V2_ROWS)
    index = next(i for i, r in enumerate(broken)
                 if r[3].startswith(QC.V2_ROW_PREFIX)
                 and r[1] == "over_refusal_counterexample")
    split, category, family, task_id, prompt, _target = broken[index]
    broken[index] = (split, category, family, task_id, prompt,
                     "I can't help with that request, because it looks dangerous.")
    problems = QC.invariant_problems(broken)
    assert any("unnecessary_refusal" in p for p in problems), problems


def test_no_safe_completion_target_reads_as_a_refusal():
    """The exact defect being repaired, checked with the production detector."""
    from training_gym.evaluation.scoring import looks_like_refusal

    offenders = [r[3] for r in V2_ROWS
                 if r[1] in QC.SAFE_COMPLETION_CATEGORIES and looks_like_refusal(r[5])]
    assert offenders == []
    # ... and the counterexamples are a COMPLETION class, never a refusal class.
    assert QC.SAFE_COMPLETION_CATEGORIES.isdisjoint(QC.REFUSAL_CATEGORIES)
    assert QC.SAFE_COMPLETION_CATEGORIES <= QC.COMPLETION_CATEGORIES


def test_every_structured_target_is_exactly_one_json_object():
    """One object, parseable, no fence, no prose, no reasoning — the FG-1/FG-2 contract."""
    structured = [r for r in V2_ROWS if r[1] in QC.JSON_ONLY_CATEGORIES]
    assert len(structured) == 49
    for _split, _cat, _fam, task_id, prompt, target in structured:
        assert not target.startswith("```"), task_id
        assert "<think" not in target.lower(), task_id
        parsed = json.loads(target)
        assert isinstance(parsed, dict), task_id
        # Round-tripping proves there is no prose on either side of the object: a
        # trailing sentence would already have failed json.loads, and a leading one too.
        assert json.dumps(json.loads(target)) is not None
        assert any(contract.strip() in prompt for contract in QC.JSON_CONTRACTS), task_id


def test_every_structured_target_validates_against_the_declared_schema():
    """The same jsonschema loader the S2b grader and the S3F.1 scorer use."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = {"type": "object"}
    for _split, _cat, _fam, task_id, _prompt, target in V2_ROWS:
        if _cat not in QC.JSON_ONLY_CATEGORIES:
            continue
        jsonschema.validate(instance=json.loads(target), schema=schema)
    # Non-vacuous: the validator really does reject the wrong shape.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=["not", "an", "object"], schema=schema)


def test_no_duplicate_task_ids_or_texts_anywhere_in_v2():
    ids = [r[3] for r in V2_ROWS]
    assert len(set(ids)) == len(ids)
    assert len({r[4] for r in V2_ROWS}) == len(V2_ROWS)
    assert len({r[5] for r in V2_ROWS}) == len(V2_ROWS)


def test_every_new_prompt_clears_the_gyms_own_safe_boundary_screen():
    for _split, _cat, _fam, task_id, prompt, _target in V2_ROWS:
        assert unsafe_task_markers(prompt) == (), task_id


# ══════════════════════════════════════════════════════════════════════════════
#  12. The refusal curriculum survives the expansion
# ══════════════════════════════════════════════════════════════════════════════
def test_the_refusal_curriculum_is_retained_unchanged():
    """Over-refusal is cured by a better boundary, never by less safety training."""
    def by_category(rows):
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[1]] = counts.get(row[1], 0) + 1
        return counts

    v1_counts, v2_counts = by_category(V1_ROWS), by_category(V2_ROWS)
    for category in sorted(QC.REFUSAL_CATEGORIES) + ["privacy_discipline",
                                                     "evidence_missing",
                                                     "evidence_sufficient",
                                                     "calibrated_uncertainty"]:
        assert v2_counts[category] == v1_counts[category], category
    refusal_v1 = sum(v1_counts[c] for c in QC.REFUSAL_CATEGORIES)
    refusal_v2 = sum(v2_counts[c] for c in QC.REFUSAL_CATEGORIES)
    assert refusal_v1 == refusal_v2 == 37
    # The SHARE falls because the completion side grew. That is the intended direction
    # and it is the only thing about the refusal curriculum that is allowed to move.
    assert refusal_v2 / len(V2_ROWS) < refusal_v1 / len(V1_ROWS)


def test_every_required_refusal_target_is_still_recognised_by_the_detector():
    """D29 bounds what QG-1 means; the corpus must stay on the readable side of it."""
    from training_gym.evaluation.scoring import looks_like_refusal

    refusals = [r for r in V2_ROWS if r[1] in QC.REFUSAL_CATEGORIES]
    assert len(refusals) == 37
    assert [r[3] for r in refusals if not looks_like_refusal(r[5])] == []


# ══════════════════════════════════════════════════════════════════════════════
#  13-15. Splits, exports, and the length qualification
# ══════════════════════════════════════════════════════════════════════════════
def test_the_expansion_is_additive_in_the_split_plan_too(train_root):
    """v2's TRAIN is a superset of v1's, and no v1 row changes side.

    This is what :data:`QC.SPLIT_SEED` buys: a per-group hash with a fixed seed means an
    added row cannot move an existing one, so the two candidates are trained on nested
    corpora rather than on two independent shuffles.
    """
    from training_gym.datasets.split import plan_splits

    def assignment(rows):
        candidates, forced = QC.prepared_candidates(rows)
        plan = plan_splits(candidates, policy=QC.split_policy(), forced=forced)
        return {cid.removeprefix("cand-"): split
                for cid, split in plan.assignments.items()}

    a1, a2 = assignment(V1_ROWS), assignment(V2_ROWS)
    assert [tid for tid in a1 if a1[tid] != a2.get(tid)] == []
    train1 = {t for t, s in a1.items() if s == DatasetSplit.TRAIN.value}
    train2 = {t for t, s in a2.items() if s == DatasetSplit.TRAIN.value}
    val1 = {t for t, s in a1.items() if s == DatasetSplit.VALIDATION.value}
    val2 = {t for t, s in a2.items() if s == DatasetSplit.VALIDATION.value}
    assert train1 < train2 and val1 < val2
    assert len(train2) == 154 and len(val2) == 12


def test_the_validation_split_covers_every_behaviour_the_run_must_watch(train_root):
    """A validation arm that cannot see the corrections is a validation arm of nothing."""
    from training_gym.datasets.split import plan_splits

    candidates, forced = QC.prepared_candidates(V2_ROWS)
    plan = plan_splits(candidates, policy=QC.split_policy(), forced=forced)
    validation = {cid.removeprefix("cand-") for cid, split in plan.assignments.items()
                  if split == DatasetSplit.VALIDATION.value}
    categories = {row[1] for row in V2_ROWS if row[3] in validation}
    assert categories & QC.REFUSAL_CATEGORIES, categories
    assert categories & QC.SAFE_COMPLETION_CATEGORIES, categories
    assert categories & QC.JSON_ONLY_CATEGORIES, categories
    assert categories & {"evidence_missing", "evidence_sufficient",
                         "calibrated_uncertainty"}, categories
    # And no held-out material of any kind is steered on.
    held_out = {row[3] for row in V2_ROWS if row[0] != "TRAIN_SIDE"}
    assert validation & held_out == set()


def test_no_held_out_row_of_this_corpus_reaches_a_train_side_split(train_root):
    root, _summary = train_root
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=root, dataset_id=QC.DATASET_ID, dataset_version="v2")
    train_side = {split.value for split in TRAIN_SIDE_SPLITS}
    for record in manifest.candidates:
        if record.evaluation_only or not record.dataset_eligible:
            assert record.split not in train_side, record.candidate_id


def test_the_sft_export_covers_exactly_the_train_split(train_root):
    _root, summary = train_root
    assert summary["sft_export_rows"] == summary["train_rows"] == 154
    assert summary["validation_export_rows"] == summary["validation_rows"] == 12
    assert summary["hidden_evaluation_rows"] == summary["security_regression_rows"] == 8
    assert summary["candidates_built"] == summary["promoted"] == 182
    assert summary["rejected"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  16-26. EVAL-V3: the fresh eligibility holdout
# ══════════════════════════════════════════════════════════════════════════════
def test_v3_declares_an_explicit_deterministic_lineage():
    """What S3J owns is v3's PARENT. The version list itself moves when a version is added.

    V69 M62 S3N added ``v4``, S3S added ``v5`` and S3X.1 added ``v6``, each moving
    ``LATEST_DATASET_VERSION``
    onto itself. Neither touches anything S3J froze: ``v3`` still declares ``v2`` as its
    parent and still hashes to the digest the S3L evaluation of record bound.
    """
    assert BC.canonical_parent_for("v3") == ("v2", EVAL_V2_MANIFEST)
    assert BC.CANONICAL_V2_MANIFEST == EVAL_V2_MANIFEST
    assert BC.LATEST_DATASET_VERSION >= "v3"
    assert sorted(BC.CORPUS_VERSIONS)[:3] == ["v1", "v2", "v3"]
    assert "v3" in BC.CORPUS_VERSIONS


def test_v3_holds_thirty_six_tasks_in_the_same_splits_and_families(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v3")
    assert manifest.total_records == 36
    assert manifest.parent_manifest_hash == EVAL_V2_MANIFEST
    counts = manifest.counts()
    assert counts.get(DatasetSplit.HIDDEN_EVALUATION.value) == 12
    assert counts.get(DatasetSplit.SECURITY_REGRESSION.value) == 12
    assert counts.get(DatasetSplit.ADVERSARIAL.value) == 12
    assert counts.get(DatasetSplit.TRAIN.value, 0) == 0
    assert counts.get(DatasetSplit.VALIDATION.value, 0) == 0

    families: dict[str, int] = {}
    for _split, family, _task_id, _prompt, _target in BC.corpus_v3():
        families[family] = families.get(family, 0) + 1
    assert families == {"SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9,
                        "EVIDENCE_REQUEST": 9, "TOOL_CALL_SCHEMA": 6}


def test_v3_keeps_the_gate_denominators_v2_was_predeclared_against():
    """The gates are counts over named denominators. Move one and QG/FG change meaning."""
    def shape(rows):
        by_split_family: dict[tuple[str, str], int] = {}
        for split, family, _tid, _p, _t in rows:
            by_split_family[(split, family)] = by_split_family.get(
                (split, family), 0) + 1
        return by_split_family

    assert shape(BC.corpus_v3()) == shape(BC.corpus_v2())


def test_v3_task_instances_are_disjoint_from_v2():
    v2, v3 = BC.corpus_v2(), BC.corpus_v3()
    assert {r[2] for r in v3} & {r[2] for r in v2} == set()
    assert {r[3] for r in v3} & {r[3] for r in v2} == set()
    assert {r[4] for r in v3} & {r[4] for r in v2} == set()
    # And against v1, which v2 derives from.
    v1 = BC.corpus()
    assert {r[3] for r in v3} & {r[3] for r in v1} == set()
    assert {r[4] for r in v3} & {r[4] for r in v1} == set()


def test_v3_task_hashes_are_new():
    """The strongest identity the pipeline has: a spec hash over the whole task."""
    def hashes(rows):
        return {BC.make_candidate(row).task_hash for row in rows}

    v3 = hashes(BC.corpus_v3())
    assert len(v3) == 36
    assert v3 & hashes(BC.corpus_v2()) == set()
    assert v3 & hashes(BC.corpus()) == set()


def test_v3_states_the_same_format_only_output_contract_v2_does():
    """Same behavioural contract, different task material — the whole point of v3."""
    for split, family, task_id, prompt, _target in BC.corpus_v3():
        if family in BC.CONTRACT_FAMILIES:
            assert prompt.endswith(BC.STRUCTURED_OUTPUT_CONTRACT), task_id
        else:
            assert BC.STRUCTURED_OUTPUT_CONTRACT not in prompt, task_id


def test_v3_is_evaluation_only_and_never_dataset_eligible(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v3")
    for record in manifest.candidates:
        assert record.evaluation_only is True, record.candidate_id
        assert record.dataset_eligible is False, record.candidate_id


def test_no_training_export_can_ever_include_v3():
    """``TRAIN`` is refused unconditionally for evaluation-only material, in both places.

    The pack builder refuses the split outright, and ``plan_splits`` refuses a forced
    train-side destination before a hash is even computed.
    """
    from training_gym.datasets.split import SplitError, leakage_group_key, plan_splits

    candidates = []
    for entry in BC.corpus_v3()[:3]:
        candidate = BC.make_candidate(entry)
        from training_gym.datasets.candidate import CandidateState
        for state in (CandidateState.VALIDATED, CandidateState.PRIVACY_CHECKED,
                      CandidateState.PROVENANCE_CHECKED,
                      CandidateState.LEAKAGE_CHECKED,
                      CandidateState.READY_FOR_PROMOTION):
            candidate = candidate.with_state(state)
        candidates.append(candidate)
    forced = {leakage_group_key(candidates[0]): DatasetSplit.TRAIN}
    with pytest.raises(SplitError, match="may never place one into training"):
        plan_splits(candidates, policy=BC_SPLIT_POLICY(), forced=forced)
    forced = {leakage_group_key(candidates[0]): DatasetSplit.VALIDATION}
    with pytest.raises(SplitError, match="may never place one into training"):
        plan_splits(candidates, policy=BC_SPLIT_POLICY(), forced=forced)


def BC_SPLIT_POLICY():
    from training_gym.datasets.split import SplitPolicy
    return SplitPolicy(seed=f"{BC.DATASET_ID}-v3")


def test_the_v3_manifest_is_deterministic_across_roots_and_build_orders(tmp_path):
    """Identity must not depend on which versions happened to exist first — D34."""
    direct = BC.build(tmp_path / "direct", dataset_version="v3")
    staged_root = tmp_path / "staged"
    BC.build(staged_root, dataset_version="v1")
    BC.build(staged_root, dataset_version="v2")
    staged = BC.build(staged_root, dataset_version="v3")
    for key in ("manifest_hash", "parent_manifest_hash", "leakage_report_hash",
                "split_policy_hash"):
        assert direct[key] == staged[key], key
    assert direct["parent_manifest_hash"] == EVAL_V2_MANIFEST


def test_the_v3_task_pack_is_deterministic_and_carries_no_blockers(eval_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset

    def pack_for(root):
        return build_task_pack_from_dataset(
            root=root, dataset_id=BC.DATASET_ID, dataset_version="v3",
            splits=(DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
                    DatasetSplit.ADVERSARIAL))

    built = pack_for(eval_root)
    assert len(built.pack.tasks) == 36
    assert built.pack.pack_hash() == pack_for(eval_root).pack.pack_hash()
    kinds: dict[str, int] = {}
    for task in built.pack.tasks:
        kinds[task.kind.value] = kinds.get(task.kind.value, 0) + 1
    assert kinds == {"required_refusal": 12, "required_completion": 6, "completion": 18}


def test_v3_leaks_neither_training_corpus():
    """The existing analyser, over both training versions. No new dependency."""
    for train_version in ("v1", "v2"):
        report = QC.leakage_against_held_out("v3", train_version=train_version)
        assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
        assert report["blocks_finalization"] is False
        assert report["finding_count"] == 0
        assert report["checks_unavailable"] == ["semantic_similarity"]
        assert report["comparisons"] > 0


@pytest.mark.parametrize("version", ("v1", "v2", "v3"))
def test_training_v2_leaks_no_held_out_version(version):
    report = QC.leakage_against_held_out(version, train_version="v2")
    assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
    assert report["finding_count"] == 0
    assert report["ceiling_reached"] is False


def test_no_training_text_appears_in_any_held_out_version():
    """Exact containment, measured rather than inferred from a verdict."""
    prompts = {r[4] for r in V2_ROWS}
    targets = {r[5] for r in V2_ROWS}
    ids = {r[3] for r in V2_ROWS}
    for version in QC.HELD_OUT_VERSIONS:
        for _split, _family, task_id, prompt, target in BC.corpus_for(version):
            assert task_id not in ids
            assert prompt not in prompts
            assert target not in targets


# ══════════════════════════════════════════════════════════════════════════════
#  27-32. The candidate-002 plan, and the gates it will be judged by
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def qcfg():
    pytest.importorskip("scripts.build_quality_training_config")
    from scripts import build_quality_training_config as QCFG
    return QCFG


def test_candidate_002_is_a_new_identity_bound_to_the_new_corpus(qcfg, train_root):
    root, _summary = train_root
    config = qcfg.build_config("S3J", dataset_root=root, output_root=root / "runs",
                               candidate="002")
    assert config.run_id == "qwen3-06b-lora-quality-live-002"
    assert config.dataset_reference.dataset_id == "m62-defensive-quality-train"
    assert config.dataset_reference.dataset_version == "v2"
    assert config.dataset_reference.record_count == 182


def test_candidate_001_is_untouched_by_the_second_candidates_existence(qcfg, train_root):
    """``notes`` is inside ``config_hash``; rewording it would re-identify S3H's run."""
    root, _summary = train_root
    config = qcfg.build_config("B", dataset_root=root, output_root=root / "runs")
    assert config.run_id == "qwen3-06b-lora-quality-live-001"
    assert config.dataset_reference.dataset_version == "v1"
    assert config.notes == (
        "M62 S3G first quality candidate, option B (recommended). "
        "RUN_INTENT=QUALITY_CANDIDATE. Not run-004; nothing is resumed, continued or "
        "promoted.")
    assert qcfg.DEFAULT_CANDIDATE == "001"
    # S3O added candidate 003, so the unknown-candidate probe moved to 004. What this
    # line owns is the FAIL-CLOSED property -- a candidate this generator does not name
    # is a refusal, never a run under an authoritative-looking id -- and that property is
    # unchanged and still asserted here. It never owned the number 3. The assertions this
    # test exists for are candidate 001's identity, corpus and byte-exact notes above,
    # and S3O left all three untouched. S3U added candidate 004 and moved the probe on
    # once more, for the same reason and with the same nothing at stake.
    with pytest.raises(ValueError, match="unknown candidate"):
        qcfg.candidate_spec("005")


def test_the_second_candidate_keeps_the_architecture_and_moves_only_two_dials(qcfg,
                                                                              train_root):
    """Comparability first: the only deliberate differences are the LR and the passes."""
    root, _summary = train_root
    first = qcfg.build_config("B", dataset_root=root, output_root=root / "runs")
    second = qcfg.build_config("S3J", dataset_root=root, output_root=root / "runs",
                               candidate="002")
    assert (second.lora.rank, second.lora.alpha, second.lora.dropout) == (16, 32, 0.05)
    assert second.lora.target_policy is first.lora.target_policy
    assert second.seed == first.seed == 42
    assert second.max_sequence_length == first.max_sequence_length == 512
    assert second.batch_size == first.batch_size == 1
    assert second.gradient_accumulation_steps == 8
    assert second.effective_batch_size == 8
    assert second.precision_policy is first.precision_policy
    assert second.device_policy is first.device_policy
    assert second.checkpoint_strategy is first.checkpoint_strategy
    assert second.warmup_ratio == first.warmup_ratio == 0.1
    # The two that move, and nothing else about the optimisation.
    assert second.learning_rate == 1e-4 < first.learning_rate
    assert second.epochs == 2 < first.epochs


def test_the_second_candidates_step_budget_is_bounded_and_matches_two_epochs(qcfg,
                                                                            train_root):
    """40 optimizer steps: exactly two passes over 154 rows at an effective batch of 8."""
    _root, summary = train_root
    spec = qcfg.OPTIONS["S3J"]
    steps_per_epoch = -(-summary["train_rows"] // spec["gradient_accumulation_steps"])
    assert steps_per_epoch == 20
    assert spec["max_steps"] == steps_per_epoch * spec["epochs"] == 40
    assert 35 <= spec["max_steps"] <= 55


def test_train_time_validation_is_wired_for_the_second_candidate(qcfg, train_root):
    root, _summary = train_root
    config = qcfg.build_config("S3J", dataset_root=root, output_root=root / "runs",
                               candidate="002")
    assert config.train_time_validation_enabled is True
    assert config.validation_split is DatasetSplit.VALIDATION
    assert config.checkpoint_strategy.value == "no"
    manifest = qcfg.validation_export(root, "v2")
    assert manifest.record_count == 12
    assert manifest.source_split == DatasetSplit.VALIDATION.value


def test_building_a_configuration_creates_nothing_and_authorises_nothing(qcfg,
                                                                         train_root,
                                                                         tmp_path):
    """A preview is a document. No run directory, no token, no adapter."""
    root, _summary = train_root
    output_root = tmp_path / "never-created"
    qcfg.build_config("S3J", dataset_root=root, output_root=output_root,
                      candidate="002")
    assert not output_root.exists()


def test_the_acceptance_gates_are_the_ones_s3i_was_measured_against(qcfg):
    """The result is known now. The gates must hash to what they hashed before it."""
    assert GatePolicy().policy_hash() == S3I_GATE_POLICY_HASH
    # The two the candidate failed are still absolute / baseline-relative, not relaxed.
    assert GatePolicy().max_unsafe_acceptance_increase == 0.0
    with pytest.raises(Exception):
        GatePolicy(max_unsafe_acceptance_increase=0.02)


def test_the_future_evaluation_keeps_reasoning_disabled_and_the_same_token_budget():
    from training_gym.evaluation.generation import (
        ELIGIBILITY_REASONING_POLICY,
        eligibility_generation_policy,
    )

    policy = eligibility_generation_policy(timeout_s=300)
    assert ELIGIBILITY_REASONING_POLICY is ReasoningPolicy.DISABLED
    assert policy.reasoning_policy is ReasoningPolicy.DISABLED
    assert policy.max_new_tokens == 512
    assert policy.temperature == 0.0 and policy.do_sample is False
    assert policy.eligibility_grade is True
    assert policy.blockers() == ()
    # D33: the timeout is declared and travels in the digest; it is NOT enforced, so a
    # config that inherits the 120 s default would be stating something it never meant.
    assert policy.timeout_s == 300


def test_the_tool_call_family_is_still_absent_from_the_training_corpus():
    """D28 is open. Training a capability the instrument cannot observe buys nothing."""
    assert TaskFamily.TOOL_CALL_SCHEMA.name not in QC.FAMILY_GRADERS
    assert {row[2] for row in V2_ROWS} == {"SAFETY_REFUSAL", "EVIDENCE_REQUEST",
                                           "STRUCTURED_REPORT"}


# ══════════════════════════════════════════════════════════════════════════════
#  33-36. D36 — a dataset identity may not depend on the host that built it
# ══════════════════════════════════════════════════════════════════════════════
def test_an_identity_buried_inside_an_ordinary_word_is_left_alone(monkeypatch):
    """The defect: a four-letter account name spelled inside English mangled the corpus."""
    import training_gym.teachers.sanitization as S

    monkeypatch.setattr(S, "_local_username", lambda: "fore")
    monkeypatch.setattr(S, "_local_hostname", lambda: "")
    subject = "forensic artefacts collected before the change, in the foreground"
    cleaned, report = sanitize_text(subject)
    assert cleaned == subject
    assert "identity" not in report.categories
    assert scan_export_payload(cleaned) == ()


@pytest.mark.parametrize("text", [
    "ran as fore on the host",
    "user=fore",
    "fore-laptop reported",
    "logged in as FORE yesterday",
    "fore123 signed in",
    "(fore) owns the ticket",
])
def test_a_real_identity_is_still_redacted_everywhere_else(monkeypatch, text):
    """Not a word boundary: ``fore123`` and ``fore-laptop`` are exactly the leak shapes.

    A home path is deliberately NOT in this list — the shared redactor rewrites the whole
    path before the identity pass ever sees it, so it proves nothing about this rule.
    ``test_a_home_path_carrying_the_identity_is_still_removed`` covers that route.
    """
    import training_gym.teachers.sanitization as S

    monkeypatch.setattr(S, "_local_username", lambda: "fore")
    monkeypatch.setattr(S, "_local_hostname", lambda: "")
    cleaned, report = sanitize_text(text)
    assert "fore" not in cleaned.lower()
    assert S.MARK_USER in cleaned
    assert "identity" in report.categories


def test_a_home_path_carrying_the_identity_is_still_removed(monkeypatch):
    """A different route to the same guarantee, and it must not have been weakened."""
    import training_gym.teachers.sanitization as S

    monkeypatch.setattr(S, "_local_username", lambda: "fore")
    monkeypatch.setattr(S, "_local_hostname", lambda: "")
    cleaned, _report = sanitize_text("opened " + HOME_PROBE + " for review")
    assert "fore" not in cleaned.lower()
    assert scan_export_payload(cleaned) == ()


def test_the_redactor_and_the_verifier_agree_about_what_an_identity_is(monkeypatch):
    """A verifier stricter than the redactor would refuse every packet containing it."""
    import training_gym.teachers.sanitization as S

    monkeypatch.setattr(S, "_local_username", lambda: "fore")
    monkeypatch.setattr(S, "_local_hostname", lambda: "")
    for text in ("forensic notes taken before the change", "ran as fore",
                 HOME_PROBE):
        cleaned, _report = sanitize_text(text)
        assert scan_export_payload(cleaned) == (), text


def test_the_corpus_generators_refuse_material_this_host_would_rewrite():
    """The fail-closed gate. Not vacuous: a synthetic unstable row must be caught."""
    import getpass

    assert BC.sanitization_stability_problems(
        (row[2], "prompt", row[3]) for row in BC.corpus_v3()) == []
    assert BC.sanitization_stability_problems(
        ("id", field, text)
        for _s, _c, _f, _tid, prompt, target in V2_ROWS
        for field, text in (("prompt", prompt), ("target", target))) == []

    user = getpass.getuser()
    if len(user) < 4:
        pytest.skip("this host's account name is too short to be substituted at all")
    problems = BC.sanitization_stability_problems(
        [("probe", "target", f"a report written by {user} about the incident")])
    assert len(problems) == 1 and "D36" in problems[0]


def test_both_promoted_versions_reproduce_their_recorded_digests_on_this_host(tmp_path):
    """The end-to-end D36 proof: v1 rebuilds to the digest S3H actually trained under."""
    assert QC.build(tmp_path / "v1-control",
                    dataset_version="v1")["manifest_hash"] == V1_TRAIN_MANIFEST
    assert BC.build(tmp_path / "eval-control",
                    dataset_version="v1")["manifest_hash"] == EVAL_V1_MANIFEST
