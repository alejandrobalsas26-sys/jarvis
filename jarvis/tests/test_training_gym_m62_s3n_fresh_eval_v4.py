"""V69 M62 S3N — the fresh held-out holdout ``m62-defensive-eval v4``, frozen candidate-blind.

WHAT THESE TESTS ARE FOR
------------------------
``eval-v3`` is spent. S3L measured candidate 002 against it, and S3M / S3M.2 then drew a
diagnosis and an output-budget retrospective from its body-free per-task results — so
under the standing **D35** rule it is development evidence, not a fresh eligibility
instrument, for any third candidate. ``v4`` is the fourth holdout, and S3N froze it
**before candidate 003 exists**.

That creates four ways to get it wrong, and every one of them is measured here rather
than promised in a document:

  * **Pseudo-freshness.** New task ids over recycled material would look fresh and measure
    nothing new. Ids, prompts, targets, canonical task hashes, candidate hashes and target
    hashes are all asserted disjoint from ``v1``, ``v2`` AND ``v3``, and the existing
    16-check leakage analyser is run over both training corpora.
  * **Changing the exam after seeing the results.** S3M diagnosed a structured-output
    termination failure and S3L recorded a refusal regression, so the tempting move is to
    add structured tasks or rebalance safety. The per-``(split, family)`` distribution, the
    decision classes, the grader assignment, the response schemas and the tool-contract
    classes are asserted **identical to v3**, cell for cell.
  * **Moving the instrument.** D37 and D38 are frozen. The gate policy digest, the metric
    policy digest, the generation policy digest, ``max_new_tokens`` and the absence of any
    D38 gate are all pinned here.
  * **Building the student in the same session.** No candidate 003 configuration, plan or
    adapter identity, and no ``train-v3``, may exist. Asserted over the repository.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS OR GENERATES A TOKEN.

This file deliberately contains **no v4 prompt, target or task body**. Every assertion is
computed from the generator, so a future reader of the test suite cannot learn the holdout
from it.
"""
from __future__ import annotations

import getpass
import json

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.datasets.leakage import LeakageVerdict
from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
from training_gym.evaluation.policy import GatePolicy, MetricPolicy
from training_gym.schemas import sha256_text

pytest.importorskip("scripts.build_training_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402
from scripts import build_training_corpus as QC  # noqa: E402

# ── The identities S3N may not move ───────────────────────────────────────────
EVAL_V1_MANIFEST = (
    "0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b")
EVAL_V2_MANIFEST = (
    "82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60")
EVAL_V3_MANIFEST = (
    "7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0")
TRAIN_V1_MANIFEST = (
    "9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a")
TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")

# ── The identities S3N freezes. Recorded here so the freeze is enforced, not filed ──
EVAL_V4_MANIFEST = (
    "8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5")
EVAL_V4_PACK = (
    "95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7")
#: A digest over the sorted canonical task hashes. This is the body-free set identity a
#: future candidate-design session binds ``v4`` by, WITHOUT reading a single task body.
EVAL_V4_TASK_HASH_SET = (
    "959f28f5b37d1bcc53934a0b5be3055c3b2ce1a4192cd5ae5ec2dc05491f9c68")
EVAL_V4_PROMPT_HASH_SET = (
    "26493db629d20973acb6333455d3a3af5f268d98f96de0f2ed2a571cbdbfb11e")
EVAL_V4_TARGET_HASH_SET = (
    "916e1ad9a6f41ff3cd4a1719b536036687ce2fc0a94acf2d2900430ecc53c696")

# ── Frozen instrument identities (S3G / S3M.1 / S3M.2) ────────────────────────
GATE_POLICY_HASH = (
    "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
METRIC_POLICY_HASH = (
    "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")
GENERATION_POLICY_HASH = (
    "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")

PRIOR_VERSIONS = ("v1", "v2", "v3")
ALL_SPLITS = (DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
              DatasetSplit.ADVERSARIAL)


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def eval_root(tmp_path_factory):
    """A root holding all four held-out versions, built by their own generator."""
    root = tmp_path_factory.mktemp("m62-s3n-eval")
    BC.build(root, dataset_version="v4")   # materialises v1 -> v2 -> v3 -> v4
    return root


@pytest.fixture(scope="module")
def v4_rows():
    return BC.corpus_v4()


def _candidates(rows):
    return [BC.make_candidate(row) for row in rows]


def _pack(root, version="v4"):
    return build_task_pack_from_dataset(root=root, dataset_id=BC.DATASET_ID,
                                        dataset_version=version, splits=ALL_SPLITS)


# ══════════════════════════════════════════════════════════════════════════════
#  1-4. v4 exists as a NEW version, with a declared lineage
# ══════════════════════════════════════════════════════════════════════════════
def test_v4_is_a_new_version_and_the_old_ones_still_exist():
    assert sorted(BC.CORPUS_VERSIONS) == ["v1", "v2", "v3", "v4"]
    assert BC.LATEST_DATASET_VERSION == "v4"


def test_v4_declares_an_explicit_lineage_onto_v3_rather_than_discovering_one():
    """D34's rule: a parent decided by what is on disk is not an identity."""
    assert BC.canonical_parent_for("v4") == ("v3", EVAL_V3_MANIFEST)
    assert BC.CANONICAL_V3_MANIFEST == EVAL_V3_MANIFEST


def test_an_undeclared_version_is_refused_rather_than_promoted_as_a_genesis():
    with pytest.raises(ValueError, match="canonical lineage"):
        BC.canonical_parent_for("v9")


def test_v4_is_not_an_alias_for_v3():
    """A new label on old material would be the one failure a manifest cannot catch."""
    assert BC.corpus_v4() != BC.corpus_v3()
    assert BC.CORPUS_VERSIONS["v4"] is not BC.CORPUS_VERSIONS["v3"]


# ══════════════════════════════════════════════════════════════════════════════
#  5-8. The frozen evaluation contract, preserved exactly
# ══════════════════════════════════════════════════════════════════════════════
def test_v4_holds_thirty_six_tasks_in_the_frozen_splits(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v4")
    assert manifest.total_records == 36
    assert manifest.parent_manifest_hash == EVAL_V3_MANIFEST
    counts = manifest.counts()
    assert counts.get(DatasetSplit.HIDDEN_EVALUATION.value) == 12
    assert counts.get(DatasetSplit.SECURITY_REGRESSION.value) == 12
    assert counts.get(DatasetSplit.ADVERSARIAL.value) == 12
    assert counts.get(DatasetSplit.TRAIN.value, 0) == 0
    assert counts.get(DatasetSplit.VALIDATION.value, 0) == 0


def test_v4_family_distribution_is_the_frozen_one(v4_rows):
    families: dict[str, int] = {}
    for _split, family, _tid, _p, _t in v4_rows:
        families[family] = families.get(family, 0) + 1
    assert families == {"SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9,
                        "EVIDENCE_REQUEST": 9, "TOOL_CALL_SCHEMA": 6}


def test_v4_keeps_every_gate_denominator_cell_for_cell(v4_rows):
    """The gates are counts over named denominators. Move one cell and QG/FG change meaning.

    This is the assertion that stops the exam being rewritten after the results are known:
    S3M diagnosed a structured-output failure, so widening ``structured_report`` would be
    the natural and wrong move.
    """
    def shape(rows):
        cells: dict[tuple[str, str], int] = {}
        for split, family, _tid, _p, _t in rows:
            cells[(split, family)] = cells.get((split, family), 0) + 1
        return cells

    assert shape(v4_rows) == shape(BC.corpus_v3())
    assert shape(v4_rows) == shape(BC.corpus_v2())


def test_v4_decision_classes_are_twelve_six_eighteen(eval_root):
    kinds: dict[str, int] = {}
    for task in _pack(eval_root).pack.tasks:
        kinds[task.kind.value] = kinds.get(task.kind.value, 0) + 1
    assert kinds == {"required_refusal": 12, "required_completion": 6, "completion": 18}


# ══════════════════════════════════════════════════════════════════════════════
#  9-12. Freshness — more than new task ids
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v4_task_id_is_unique(v4_rows):
    ids = [row[2] for row in v4_rows]
    assert len(set(ids)) == len(ids) == 36


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v4_task_id_prompt_or_target_appears_in_a_prior_holdout(version, v4_rows):
    prior = BC.corpus_for(version)
    assert {r[2] for r in v4_rows} & {r[2] for r in prior} == set()
    assert {r[3] for r in v4_rows} & {r[3] for r in prior} == set()
    assert {r[4] for r in v4_rows} & {r[4] for r in prior} == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v4_canonical_task_hash_appears_in_a_prior_holdout(version, v4_rows):
    """The strongest identity the pipeline has: a spec hash over the whole task."""
    mine = {c.task_hash for c in _candidates(v4_rows)}
    assert len(mine) == 36
    assert mine & {c.task_hash for c in _candidates(BC.corpus_for(version))} == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v4_prompt_hash_candidate_hash_or_target_hash_collides(version, v4_rows):
    mine = _candidates(v4_rows)
    theirs = _candidates(BC.corpus_for(version))
    assert ({sha256_text(c.user_prompt) for c in mine}
            & {sha256_text(c.user_prompt) for c in theirs}) == set()
    assert {c.target_hash for c in mine} & {c.target_hash for c in theirs} == set()
    assert {c.candidate_hash() for c in mine} & {c.candidate_hash() for c in theirs} == set()


def test_the_body_free_set_identities_are_the_frozen_ones(v4_rows):
    """What a future candidate-design session binds v4 by, without reading a body."""
    cands = _candidates(v4_rows)
    assert sha256_text("\n".join(sorted(c.task_hash for c in cands))) \
        == EVAL_V4_TASK_HASH_SET
    assert sha256_text("\n".join(sorted(sha256_text(c.user_prompt) for c in cands))) \
        == EVAL_V4_PROMPT_HASH_SET
    assert sha256_text("\n".join(sorted(c.target_hash for c in cands))) \
        == EVAL_V4_TARGET_HASH_SET


# ══════════════════════════════════════════════════════════════════════════════
#  13-16. Training leakage
# ══════════════════════════════════════════════════════════════════════════════
def test_v4_is_one_of_the_held_out_versions_the_training_corpus_is_checked_against():
    """A version absent from this tuple is a version nothing is ever checked against."""
    assert "v4" in QC.HELD_OUT_VERSIONS
    assert sorted(QC.HELD_OUT_VERSIONS) == ["v1", "v2", "v3", "v4"]


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_neither_training_corpus_leaks_into_v4(train_version):
    """The existing analyser, unchanged, over every train-side split it already reads."""
    report = QC.leakage_against_held_out("v4", train_version=train_version)
    assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
    assert report["finding_count"] == 0
    assert report["blocking_finding_count"] == 0
    assert report["blocks_finalization"] is False
    assert report["comparisons"] > 0
    assert report["ceiling_reached"] is False


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_no_exact_training_text_appears_anywhere_in_v4(train_version, v4_rows):
    """Exact containment, measured rather than inferred from a verdict."""
    rows = QC.curriculum_for(train_version)
    ids, prompts, targets = {r[3] for r in rows}, {r[4] for r in rows}, {r[5] for r in rows}
    for _split, _family, task_id, prompt, target in v4_rows:
        assert task_id not in ids
        assert prompt not in prompts
        assert target not in targets


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_semantic_leakage_is_reported_unavailable_and_never_clean(train_version):
    """Exact cleanliness is not proof of semantic independence, and must not be sold as it.

    No embedding backend is bundled and none was added for S3N. The analyser reports the
    check as unavailable, and this test exists so a future reader cannot mistake
    ``verdict: clean`` for a semantic result.
    """
    report = QC.leakage_against_held_out("v4", train_version=train_version)
    assert report["checks_unavailable"] == ["semantic_similarity"]
    assert "semantic_similarity" not in report["checks_run"]


def test_v4_leaks_no_answer_into_its_own_model_facing_material(eval_root):
    """A prompt that already carries its answer scores copying, not reasoning."""
    from training_gym.evaluation.task_pack import target_leaked_into

    built = _pack(eval_root)
    for task, (_split, _family, task_id, _p, target) in zip(
            sorted(built.pack.tasks, key=lambda t: t.task_id),
            sorted(BC.corpus_v4(), key=lambda r: r[2]), strict=True):
        assert task.task_id == task_id
        assert not target_leaked_into(task.user_prompt, target)
        assert not target_leaked_into(task.system_prompt, target)


# ══════════════════════════════════════════════════════════════════════════════
#  17-20. Sanitization, host identity, secrets, private paths
# ══════════════════════════════════════════════════════════════════════════════
def test_no_v4_row_would_be_rewritten_by_the_promotion_sanitizer(v4_rows):
    """D36's fail-closed control, over the authored material, before a byte is written."""
    assert BC.sanitization_stability_problems(
        (task_id, field, text)
        for _s, _f, task_id, prompt, target in v4_rows
        for field, text in (("prompt", prompt), ("target", target))) == []


def test_the_stability_control_is_not_vacuous_on_this_host():
    """A control that cannot fire proves nothing about the corpus it clears."""
    user = getpass.getuser()
    if len(user) < 4:
        pytest.skip("this host's account name is too short to be substituted at all")
    problems = BC.sanitization_stability_problems(
        [("probe", "target", f"a note written by {user} about the incident")])
    assert len(problems) == 1 and "D36" in problems[0]


def test_a_host_identity_buried_in_a_v4_word_cannot_perturb_the_corpus(monkeypatch,
                                                                      v4_rows):
    """The D36 rule, exercised against THIS corpus rather than a synthetic string.

    An identity that occurs only as the interior of a longer word must leave every row
    byte-identical — otherwise the promoted bytes, and every digest above them, would be a
    function of the building account name.
    """
    import re

    import training_gym.teachers.sanitization as S

    words = {w for _s, _f, _t, p, t in v4_rows
             for w in re.findall(r"[A-Za-z]{8,}", p + " " + t)}
    interiors = sorted({w[2:6].lower() for w in words if len(w) >= 8})
    assert interiors, "expected the corpus to contain long words to probe with"
    monkeypatch.setattr(S, "_local_hostname", lambda: "")
    for interior in interiors[:12]:
        monkeypatch.setattr(S, "_local_username", lambda i=interior: i)
        assert BC.sanitization_stability_problems(
            (task_id, field, text)
            for _s, _f, task_id, prompt, target in v4_rows
            for field, text in (("prompt", prompt), ("target", target))
        ) == [], interior


def test_v4_carries_no_secret_no_private_path_and_no_authority_token(v4_rows):
    from core.redaction_policy import scan_for_leaks

    for _split, _family, task_id, prompt, target in v4_rows:
        for name, text in (("prompt", prompt), ("target", target)):
            assert list(scan_for_leaks(text)) == [], f"{task_id} {name}"
            assert "/home/" not in text and "/Users/" not in text
            assert "TRAIN:" not in text and "EVAL:" not in text


# ══════════════════════════════════════════════════════════════════════════════
#  21-25. Schema and task-family contract validation
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v4_target_is_exactly_one_json_object_on_one_line(v4_rows):
    for _split, _family, task_id, _prompt, target in v4_rows:
        assert "\n" not in target, task_id
        assert target.strip() == target, task_id
        assert not target.startswith("```"), task_id
        assert "<think" not in target, task_id
        parsed = json.loads(target)
        assert isinstance(parsed, dict), task_id


def test_every_v4_target_satisfies_the_declared_response_schema(eval_root):
    jsonschema = pytest.importorskip("jsonschema")

    built = _pack(eval_root)
    by_id = {t.task_id: t for t in built.pack.tasks}
    for _split, _family, task_id, _prompt, target in BC.corpus_v4():
        schema = by_id[task_id].expected_output_schema
        jsonschema.validate(json.loads(target), schema)


def test_v4_states_the_same_format_only_output_contract_v3_does(v4_rows):
    """Same behavioural contract, different task material — the whole point of v4."""
    assert BC.CONTRACT_FAMILIES == frozenset({"STRUCTURED_REPORT"})
    for _split, family, task_id, prompt, _target in v4_rows:
        if family in BC.CONTRACT_FAMILIES:
            assert prompt.endswith(BC.STRUCTURED_OUTPUT_CONTRACT), task_id
        else:
            assert BC.STRUCTURED_OUTPUT_CONTRACT not in prompt, task_id


def test_the_tool_call_family_is_still_uninstructed_under_d28(v4_rows):
    """D28 is NOT solved here. Instructing a format the backend cannot read is not a fix."""
    tool_prompts = [p for _s, family, _t, p, _tg in v4_rows
                    if family == "TOOL_CALL_SCHEMA"]
    assert len(tool_prompts) == 6
    assert all(BC.STRUCTURED_OUTPUT_CONTRACT not in p for p in tool_prompts)


def test_v4_refusal_targets_stay_unrecognised_by_the_detector_because_d29_is_open(v4_rows):
    """The D29 limitation travels into v4 unchanged, and that is deliberate.

    ``m62-defensive-eval``'s required-refusal answers are ``{"decision": "refuse", ...}``
    objects, and ``looks_like_refusal`` recognises none of them. Rewriting v4's refusal
    phrasing so that it *is* recognised would address D29 as a rider on a corpus milestone
    AND change what QG-1 and SV-5 measure between candidate 002 and candidate 003. If this
    test ever fails, D29 has been addressed somewhere and this corpus should be revisited
    deliberately rather than silently.
    """
    from training_gym.evaluation.scoring import looks_like_refusal

    refusals = [t for _s, family, _tid, _p, t in v4_rows if family == "SAFETY_REFUSAL"]
    assert len(refusals) == 12
    assert not any(looks_like_refusal(t) for t in refusals)


def test_no_safe_v4_target_reads_as_a_refusal(v4_rows):
    """A required-completion answer that reads as a refusal would score its own failure."""
    from training_gym.evaluation.scoring import looks_like_refusal

    safe = [(tid, t) for _s, family, tid, _p, t in v4_rows
            if family != "SAFETY_REFUSAL"]
    assert len(safe) == 24
    assert [tid for tid, t in safe if looks_like_refusal(t)] == []


# ══════════════════════════════════════════════════════════════════════════════
#  26-29. Evaluation-only, and the train-side refusal
# ══════════════════════════════════════════════════════════════════════════════
def test_v4_is_evaluation_only_and_never_dataset_eligible(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v4")
    for record in manifest.candidates:
        assert record.evaluation_only is True, record.candidate_id
        assert record.dataset_eligible is False, record.candidate_id


@pytest.mark.parametrize("destination", (DatasetSplit.TRAIN, DatasetSplit.VALIDATION))
def test_no_training_export_can_ever_include_v4(destination):
    """``plan_splits`` refuses a forced train-side destination before a hash is computed."""
    from training_gym.datasets.candidate import CandidateState
    from training_gym.datasets.split import (
        SplitError,
        SplitPolicy,
        leakage_group_key,
        plan_splits,
    )

    candidates = []
    for entry in BC.corpus_v4()[:3]:
        candidate = BC.make_candidate(entry)
        for state in (CandidateState.VALIDATED, CandidateState.PRIVACY_CHECKED,
                      CandidateState.PROVENANCE_CHECKED,
                      CandidateState.LEAKAGE_CHECKED,
                      CandidateState.READY_FOR_PROMOTION):
            candidate = candidate.with_state(state)
        candidates.append(candidate)
    forced = {leakage_group_key(candidates[0]): destination}
    with pytest.raises(SplitError, match="may never place one into training"):
        plan_splits(candidates,
                    policy=SplitPolicy(seed=f"{BC.DATASET_ID}-v4"), forced=forced)


# ══════════════════════════════════════════════════════════════════════════════
#  30-34. Deterministic identity
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v4_manifest_reproduces_across_roots_and_build_orders(tmp_path):
    """Identity must not depend on which versions happened to exist first — D34."""
    direct = BC.build(tmp_path / "direct", dataset_version="v4")
    staged_root = tmp_path / "staged"
    for version in PRIOR_VERSIONS:
        BC.build(staged_root, dataset_version=version)
    staged = BC.build(staged_root, dataset_version="v4")
    for key in ("manifest_hash", "parent_manifest_hash", "leakage_report_hash",
                "split_policy_hash"):
        assert direct[key] == staged[key], key
    assert direct["manifest_hash"] == EVAL_V4_MANIFEST
    assert direct["parent_manifest_hash"] == EVAL_V3_MANIFEST
    assert direct["promoted_records"] == 36
    assert direct["leakage_verdict"] == LeakageVerdict.CLEAN.value
    assert direct["leakage_findings"] == 0


def test_the_v4_manifest_is_the_frozen_digest(eval_root):
    from training_gym.datasets.manifests import verify_version

    result = verify_version(root=eval_root, dataset_id=BC.DATASET_ID,
                            dataset_version="v4")
    assert result.ok, list(result.problems)
    assert result.manifest is not None
    assert result.manifest.manifest_hash() == EVAL_V4_MANIFEST


def test_the_v4_task_pack_is_the_frozen_digest_and_carries_no_blockers(eval_root,
                                                                      tmp_path):
    built = _pack(eval_root)
    assert len(built.pack.tasks) == 36
    assert built.pack.pack_hash() == EVAL_V4_PACK
    assert list(built.pack.eligibility_blockers()) == []
    # And in an independent root, so the pack hash is not a property of this directory.
    other = tmp_path / "independent"
    BC.build(other, dataset_version="v4")
    assert _pack(other).pack.pack_hash() == EVAL_V4_PACK


def test_the_v4_task_order_is_deterministic_and_not_a_filesystem_artefact(eval_root,
                                                                         tmp_path):
    other = tmp_path / "order"
    BC.build(other, dataset_version="v4")
    assert [t.task_id for t in _pack(eval_root).pack.tasks] \
        == [t.task_id for t in _pack(other).pack.tasks]


def test_no_timestamp_enters_the_v4_identity():
    """``NOW`` is a frozen literal, so a rebuild tomorrow is a rebuild, not a new corpus."""
    assert BC.NOW == "2026-08-06T00:00:00Z"


# ══════════════════════════════════════════════════════════════════════════════
#  35-38. Historical immutability
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("version,expected", (
    ("v1", EVAL_V1_MANIFEST), ("v2", EVAL_V2_MANIFEST), ("v3", EVAL_V3_MANIFEST)))
def test_the_prior_holdouts_are_unchanged_by_the_existence_of_v4(version, expected,
                                                                 eval_root):
    from training_gym.datasets.manifests import load_manifest

    assert load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                         dataset_version=version).manifest_hash() == expected


@pytest.mark.parametrize("version,expected", (
    ("v1", TRAIN_V1_MANIFEST), ("v2", TRAIN_V2_MANIFEST)))
def test_the_training_corpora_are_unchanged_by_the_existence_of_v4(version, expected,
                                                                   tmp_path):
    root = tmp_path / "train-control"
    QC.build(root, dataset_version="v1")
    if version == "v1":
        from training_gym.datasets.manifests import load_manifest
        assert load_manifest(root=root, dataset_id=QC.DATASET_ID,
                             dataset_version="v1").manifest_hash() == expected
        return
    assert QC.build(root, dataset_version="v2")["manifest_hash"] == expected


def test_the_prior_holdout_material_is_byte_identical():
    """v4 is additive. A generator that edits v1-v3 while adding v4 would restate history."""
    assert len(BC.corpus()) == len(BC.corpus_v2()) == len(BC.corpus_v3()) == 36
    for split, family, task_id, prompt, target in BC.corpus_v2():
        assert (split, family, task_id) != ("", "", "")
        assert prompt and target
    # v2 is still exactly v1 plus the contract sentence on the contract families.
    for (s1, f1, t1, p1, g1), (s2, f2, t2, p2, g2) in zip(BC.corpus(), BC.corpus_v2(),
                                                          strict=True):
        assert (s1, f1, t1, g1) == (s2, f2, t2, g2)
        assert p2 == (p1 + BC.STRUCTURED_OUTPUT_CONTRACT
                      if f1 in BC.CONTRACT_FAMILIES else p1)


# ══════════════════════════════════════════════════════════════════════════════
#  39-44. The instrument is frozen: D37, D38, the gates and the budget
# ══════════════════════════════════════════════════════════════════════════════
def test_the_acceptance_gates_did_not_move_for_the_fourth_holdout():
    """Freezing a new exam is not an occasion to reprice the pass mark."""
    assert GatePolicy().policy_hash() == GATE_POLICY_HASH


def test_the_metric_policy_is_the_one_s3m2_froze():
    assert MetricPolicy().policy_hash() == METRIC_POLICY_HASH


def test_the_future_evaluation_keeps_reasoning_disabled_and_the_same_token_budget():
    """D37 and D38 are frozen. S3N changes neither, and the budget stays at 512."""
    from training_gym.evaluation.generation import (
        ELIGIBILITY_REASONING_POLICY,
        ReasoningPolicy,
    )

    assert ELIGIBILITY_REASONING_POLICY is ReasoningPolicy.DISABLED
    assert eval_config_generation_policy_hash() == GENERATION_POLICY_HASH


def eval_config_generation_policy_hash() -> str:
    """The sealed evaluation configuration's generation policy, re-derived from its bytes.

    ``eligibility_generation_policy()`` alone carries library defaults (``timeout_s`` 120,
    ``seed`` 0, ``auto_safe`` device/precision); the digest of record ``c6b0b682…`` is the
    policy the S3I and S3L configs actually declared. Re-deriving from the sealed document
    is what makes this a check on the instrument rather than on a default.
    """
    from pathlib import Path

    from training_gym.evaluation.generation import GenerationPolicy

    root = Path(__file__).resolve().parents[1]
    document = root / "evaluation" / "configs" / "m62-s3l-quality-heldout-live.json"
    if not document.is_file():  # pragma: no cover - the sealed config is runtime state
        pytest.skip("the sealed S3L evaluation config is not present in this checkout")
    policy = GenerationPolicy.from_dict(json.loads(document.read_text())["generation"])
    assert policy.max_new_tokens == 512
    assert policy.reasoning_policy.value == "disabled"
    return policy.policy_hash()


def test_d38_remains_diagnostic_and_no_gate_reads_it():
    """S3M.2 designed no gate, and freezing a fresh holdout does not create one."""
    from pathlib import Path

    gates = (Path(__file__).resolve().parents[1] / "training_gym" / "evaluation"
             / "gates.py").read_text()
    for name in ("output_budget_exhausted", "output_budget_exhaustion_rate",
                 "output_budget_exhaustion_count", "finish_reason"):
        assert name not in gates, name


#: S3N's own commit range: the commit it started from, and the commit it closed at.
#:
#: V69 M62 S3Q.0 pinned the second endpoint. It was open — the diff ran against the
#: WORKING TREE — which meant this test silently asserted that no LATER milestone had
#: touched ``training_gym`` either. That happened to hold through S3N.1, S3O and S3P
#: because none of them changed production evaluation source, and it stopped holding
#: when S3Q.0 hardened the plan binding and the ledger under an explicit authority to do
#: so. Reading that as an S3N regression would have been wrong twice over: S3N is sealed
#: and cannot regress, and a later milestone's authorised change is not evidence about
#: an earlier one's discipline.
#:
#: The property this test owns is unchanged and still measured exactly: *S3N* touched no
#: evaluation policy, grader or gate source. What a LATER milestone may touch is that
#: milestone's own scope question, guarded by its own suite.
S3N_STARTING_COMMIT = "4c669fad8a4f576a87b30c919296e316518800fb"
S3N_CLOSING_COMMIT = "ec446e3"


def test_s3n_changed_no_evaluation_policy_grader_or_gate_source():
    """The only production files S3N may touch are the two corpus generators."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", S3N_STARTING_COMMIT, S3N_CLOSING_COMMIT],
            cwd=root, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available to compare against the S3N starting commit")
    if changed.returncode != 0:  # pragma: no cover - shallow clone or missing commit
        pytest.skip("the S3N commit range is not reachable in this checkout")
    forbidden = [p for p in changed.stdout.split()
                 if p.startswith("jarvis/training_gym/")]
    assert forbidden == [], forbidden


# ══════════════════════════════════════════════════════════════════════════════
#  45-49. No candidate 003, no train-v3, no authority
# ══════════════════════════════════════════════════════════════════════════════
#: S3P added exactly one tracked path naming candidate 003: its portable training
#: receipt. That is evidence a run happened, not a configuration, a plan, an adapter or
#: a token — none of which may ever be tracked.
CANDIDATE_003_TRACKED_ALLOWLIST = frozenset({
    "state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json",
})


def test_no_candidate_003_configuration_plan_or_adapter_identity_is_tracked():
    """S3N froze the exam and S3O built the student; S3P trained it.

    What this test owns is unchanged: no configuration document, no plan, no adapter and
    no token for candidate 003 lives in Git. The single allowlisted receipt is the
    deliberate exception S3P introduced so the training history outlives the gitignored
    runtime tree, and it is required to be exactly that one path.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                                 text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available to enumerate tracked files")
    if tracked.returncode != 0:  # pragma: no cover
        pytest.skip("this checkout is not a git repository")
    names = [p for p in tracked.stdout.split()
             if not p.endswith(".md") and "docs/" not in p]
    assert {p for p in names
            if "quality-live-003" in p} == CANDIDATE_003_TRACKED_ALLOWLIST
    assert [p for p in names if "candidate_003" in p or "candidate-003" in p] == []


def test_the_training_corpus_has_no_third_version():
    """Candidate 003's preregistered axis is the render policy, not the curriculum."""
    assert sorted(QC.CURRICULUM_VERSIONS) == ["v1", "v2"]
    assert QC.LATEST_DATASET_VERSION == "v2"


def test_v4_is_not_reachable_from_any_training_export():
    """The pack builder refuses TRAIN unconditionally for evaluation-only material."""
    from training_gym.datasets.export import EXPORTABLE_SPLITS

    for source_split, _filename in (
            (s, f) for s, f in _exportable_pairs(EXPORTABLE_SPLITS)):
        assert source_split in {DatasetSplit.TRAIN.value, DatasetSplit.VALIDATION.value,
                                DatasetSplit.TRAIN, DatasetSplit.VALIDATION}


def _exportable_pairs(table):
    for key, value in dict(table).items():
        yield (key, value)


def test_this_test_file_contains_no_held_out_task_body():
    """A test that quotes the holdout publishes it to every future reader of the suite."""
    from pathlib import Path

    source = Path(__file__).read_text()
    for _split, _family, _tid, prompt, target in BC.corpus_v4():
        assert prompt not in source
        assert target not in source
    for _split, _family, _tid, prompt, target in BC.corpus_v3():
        assert prompt not in source
        assert target not in source
