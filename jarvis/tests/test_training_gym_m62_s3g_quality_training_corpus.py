"""V69 M62 S3G — the first quality-oriented training corpus, and the plan it feeds.

WHAT THESE TESTS ARE FOR
------------------------
The corpus is authored material. Authored material is exactly the kind of artefact that
looks fine and trains the wrong thing, because the failure is silent: a refusal phrased
in wording the grader does not recognise still reads as a refusal to a human reviewer,
and scores as ``unsafe_acceptance`` to the instrument. These tests pin the properties a
human cannot check by reading, against the SAME production modules that will grade the
model — never a second copy of the rule.

The four properties that matter most:

  * a required-refusal target is recognised by ``scoring.looks_like_refusal`` (D29);
  * a completion target is NOT — otherwise the corpus teaches ``unnecessary_refusal``;
  * nothing in the corpus would score as a security finding, a fabricated citation or an
    unsupported claim;
  * the corpus is disjoint from BOTH held-out evaluation versions, measured by the
    existing leakage authority rather than asserted.

Plus the D30 planner regression: a plan whose weights are not known to be cached, under
a deny download policy, must not report itself executable.
"""
from __future__ import annotations

import json

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.datasets.leakage import LeakageVerdict
from training_gym.datasets.manifests import TRAIN_SIDE_SPLITS
from training_gym.task_spec import TaskFamily, unsafe_task_markers

pytest.importorskip("scripts.build_training_corpus")
from scripts import build_training_corpus as QC  # noqa: E402

ROWS = QC.curriculum()


# ══════════════════════════════════════════════════════════════════════════════
#  Shape
# ══════════════════════════════════════════════════════════════════════════════
def test_the_authored_material_satisfies_every_declared_invariant():
    """The generator's own gate. A non-empty problem list must fail the build."""
    assert QC.invariant_problems(ROWS) == []


def test_the_invariant_check_is_not_vacuous():
    """A deliberately broken row must be caught, or the test above proves nothing."""
    broken = list(ROWS)
    split, category, family, task_id, prompt, _target = broken[0]
    assert category in QC.REFUSAL_CATEGORIES, "expected the first row to be a refusal"
    broken[0] = (split, category, family, task_id, prompt,
                 "Sure, here is the complete procedure you asked for, step by step.")
    problems = QC.invariant_problems(broken)
    assert any("looks_like_refusal" in p for p in problems), problems


def test_task_ids_and_texts_are_unique():
    assert len({r[3] for r in ROWS}) == len(ROWS)
    assert len({r[4] for r in ROWS}) == len(ROWS)
    assert len({r[5] for r in ROWS}) == len(ROWS)


def test_every_category_has_a_recorded_training_rationale():
    """A category with no rationale is a category that exists to inflate a count.

    Measured over EVERY version the generator can build, not over ``v1`` alone: S3J adds
    categories that ``v1`` does not use, and asserting against one version would either
    forbid the addition or let a stale rationale survive in the other direction.
    """
    used = {row[1] for version in QC.CURRICULUM_VERSIONS
            for row in QC.curriculum_for(version)}
    assert {r[1] for r in ROWS} <= set(QC.CATEGORY_RATIONALE)
    assert set(QC.CATEGORY_RATIONALE) == used, "an unused rationale is stale"


def test_the_tool_call_family_is_absent_because_d28_is_open():
    """D28: the production backend never populates ``proposed_tool_calls``.

    Training a capability the instrument has no transport for would buy an
    unfalsifiable claim, so tool calls are out of scope for the first quality
    candidate. This test is what stops that scope decision being quietly reversed.
    """
    assert TaskFamily.TOOL_CALL_SCHEMA.name not in QC.FAMILY_GRADERS
    assert all(r[2] != TaskFamily.TOOL_CALL_SCHEMA.name for r in ROWS)


# ══════════════════════════════════════════════════════════════════════════════
#  The refusal contract — D29
# ══════════════════════════════════════════════════════════════════════════════
def test_every_required_refusal_target_is_recognised_by_the_production_detector():
    from training_gym.evaluation.scoring import looks_like_refusal

    for _split, category, _family, task_id, _prompt, target in ROWS:
        if category in QC.REFUSAL_CATEGORIES:
            assert looks_like_refusal(target), (
                f"{task_id}: the instrument would score this as unsafe_acceptance")


def test_no_completion_target_reads_as_a_refusal():
    from training_gym.evaluation.scoring import looks_like_refusal

    for _split, category, _family, task_id, _prompt, target in ROWS:
        if category in QC.COMPLETION_CATEGORIES:
            assert not looks_like_refusal(target), (
                f"{task_id}: the instrument would score this as unnecessary_refusal")


def test_the_held_out_corpus_refusal_targets_are_not_recognised_which_is_why_d29_exists():
    """The observation that shaped every refusal target here, pinned as evidence.

    ``m62-defensive-eval``'s twelve required-refusal answers are JSON objects of the
    form ``{"decision": "refuse", ...}``. None of them contains a phrasing
    ``looks_like_refusal`` knows, so a model emitting the corpus's OWN expected answer
    verbatim would score ``unsafe_acceptance``. That is recorded as D29 and deliberately
    NOT fixed in S3G: changing the detector in the same milestone as the first
    reasoning-disabled measurement would put two variables into one comparison.

    If this test ever fails, D29 has been addressed somewhere and this corpus's refusal
    phrasing should be revisited rather than left as-is.
    """
    from scripts import build_evaluation_corpus as BC
    from training_gym.evaluation.scoring import looks_like_refusal

    refusal_targets = [target for _s, family, _t, _p, target in
                       ((e[0], e[1], e[2], e[3], e[4]) for e in BC.corpus())
                       if family == "SAFETY_REFUSAL"]
    assert refusal_targets, "expected the held-out corpus to carry refusal targets"
    assert not any(looks_like_refusal(t) for t in refusal_targets)


def test_refusal_and_completion_rows_are_balanced_against_over_refusal():
    """A corpus that mostly says no teaches that security questions get refused."""
    refusals = [r for r in ROWS if r[1] in QC.REFUSAL_CATEGORIES]
    share = len(refusals) / len(ROWS)
    assert 0.2 <= share <= 0.4, f"refusal share {share:.3f} is outside the design band"
    counterexamples = [r for r in ROWS if r[1] == "over_refusal_counterexample"]
    assert len(counterexamples) >= 10, (
        "the over-refusal counterweight must not be trimmed below the point where it "
        "stops balancing the refusal rows")


# ══════════════════════════════════════════════════════════════════════════════
#  What the security scan and the graders would make of the material
# ══════════════════════════════════════════════════════════════════════════════
def test_no_prompt_or_target_would_trip_the_security_scan():
    from core.redaction_policy import scan_for_leaks
    from training_gym.evaluation.scoring import private_paths

    for _split, _category, _family, task_id, prompt, target in ROWS:
        for text in (prompt, target):
            assert scan_for_leaks(text) == [], task_id
            assert private_paths(text) == (), task_id


def test_no_target_would_score_as_a_fabricated_citation_or_an_unsupported_claim():
    from training_gym.evaluation.scoring import _UNSUPPORTED_CLAIM_PATTERNS

    for _split, _category, _family, task_id, _prompt, target in ROWS:
        assert not QC._INVENTED_CITATION_RE.search(target), task_id
        for pattern, _finding in _UNSUPPORTED_CLAIM_PATTERNS:
            assert not pattern.search(target), task_id


def test_every_prompt_clears_the_gyms_own_safe_boundary_screen():
    for _split, _category, _family, task_id, prompt, _target in ROWS:
        assert unsafe_task_markers(prompt) == (), task_id


def test_no_target_carries_reasoning_markup():
    """The eligibility evaluation runs under ``reasoning_policy = DISABLED``.

    Training a hidden-reasoning target would teach the model to emit a block the
    evaluation asks the template to suppress, and the structural check strips.
    """
    for _split, _category, _family, task_id, _prompt, target in ROWS:
        assert "<think" not in target.lower(), task_id


def test_structured_targets_are_exactly_one_json_object_with_a_stated_contract():
    for _split, category, family, task_id, prompt, target in ROWS:
        if category in QC.JSON_ONLY_CATEGORIES:
            assert family == TaskFamily.STRUCTURED_REPORT.name, task_id
            parsed = json.loads(target)
            assert isinstance(parsed, dict), task_id
            assert any(c.strip() in prompt for c in QC.JSON_CONTRACTS), task_id
        else:
            assert not target.lstrip().startswith(("{", "[")), task_id


def test_the_json_contract_is_stated_in_more_than_one_wording():
    """A single fixed sentence would be memorised instead of the contract behind it."""
    used = {c for c in QC.JSON_CONTRACTS
            if any(c.strip() in r[4] for r in ROWS)}
    assert len(used) >= 4


def test_targets_stay_concise_and_are_final_answers():
    lengths = [len(r[5]) for r in ROWS]
    assert max(lengths) <= QC.MAX_TARGET_CHARS
    assert min(lengths) >= QC.MIN_TARGET_CHARS


# ══════════════════════════════════════════════════════════════════════════════
#  Promotion, splits and eligibility
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def promoted(tmp_path_factory):
    root = tmp_path_factory.mktemp("m62-s3g-corpus")
    return root, QC.build(root)


def test_the_corpus_promotes_with_a_clean_leakage_verdict(promoted):
    _root, summary = promoted
    assert summary["leakage_verdict"] == LeakageVerdict.CLEAN.value
    assert summary["leakage_findings"] == 0
    assert summary["rejected"] == 0
    assert summary["promoted"] == summary["candidates_built"] == len(ROWS)


def test_both_train_side_splits_are_populated(promoted):
    _root, summary = promoted
    assert summary["train_rows"] > 0
    assert summary["validation_rows"] > 0
    assert summary["hidden_evaluation_rows"] > 0
    assert summary["security_regression_rows"] > 0
    assert (summary["train_rows"] + summary["validation_rows"]
            + summary["hidden_evaluation_rows"]
            + summary["security_regression_rows"]) == len(ROWS)


def test_the_sft_export_covers_exactly_the_train_split(promoted):
    _root, summary = promoted
    assert summary["sft_export_rows"] == summary["train_rows"]
    assert summary["sft_export_excluded"] == {}


def test_no_held_out_record_of_this_corpus_reaches_a_train_side_split(promoted):
    """The corpus carries its own internal held-out material; it must stay held out."""
    from training_gym.datasets.manifests import load_manifest

    root, _summary = promoted
    manifest = load_manifest(root=root, dataset_id=QC.DATASET_ID,
                             dataset_version=QC.DATASET_VERSION)
    by_id = {row.candidate_id: row for row in manifest.candidates}
    for split in TRAIN_SIDE_SPLITS:
        for candidate_id in manifest.candidate_ids_in(DatasetSplit(split)):
            row = by_id[candidate_id]
            assert not row.evaluation_only, candidate_id
            assert row.dataset_eligible, candidate_id


def test_the_author_cannot_hand_pick_what_the_model_is_steered_on():
    """``plan_splits`` refuses a forced train-side destination, for any record."""
    from training_gym.datasets.split import SplitError, plan_splits

    candidates, forced = QC.prepared_candidates(ROWS)
    poisoned = dict(forced)
    from training_gym.datasets.split import leakage_group_key
    poisoned[leakage_group_key(candidates[0])] = DatasetSplit.TRAIN
    with pytest.raises(SplitError, match="may never place one into training"):
        plan_splits(candidates, policy=QC.split_policy(), forced=poisoned)


def test_the_build_is_deterministic_across_roots(tmp_path):
    """Two independent builds must produce the same dataset identity.

    The PROMOTION PLAN hash is deliberately excluded: it binds ``output_root_id``, so
    it is expected to differ between roots. The dataset's identity is not.
    """
    first = QC.build(tmp_path / "one")
    second = QC.build(tmp_path / "two")
    for key in ("manifest_hash", "split_plan_hash", "leakage_report_hash",
                "sft_export_hash", "sft_export_file_sha256", "train_rows",
                "validation_rows"):
        assert first[key] == second[key], key
    assert first["promotion_plan_hash"] != second["promotion_plan_hash"]


# ══════════════════════════════════════════════════════════════════════════════
#  Cross-corpus leakage against the held-out material
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("version", QC.HELD_OUT_VERSIONS)
def test_the_training_corpus_does_not_leak_the_held_out_corpus(version):
    """Fifteen deterministic checks over both corpora at once. No new dependency.

    The semantic check is reported unavailable rather than clean, which is the property
    that keeps ``INSUFFICIENT_EVIDENCE`` meaningful. Near-duplicate detection here is
    therefore LEXICAL, and that limitation is recorded rather than papered over.
    """
    report = QC.leakage_against_held_out(version)
    assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
    assert report["blocks_finalization"] is False
    assert report["finding_count"] == 0
    assert report["ceiling_reached"] is False
    assert "char_ngram_similarity" in report["checks_run"]
    assert "token_shingle_similarity" in report["checks_run"]
    assert "evaluation_only_contamination" in report["checks_run"]
    assert report["comparisons"] > 0
    assert report["checks_unavailable"] == ["semantic_similarity"]


def test_no_training_prompt_or_target_appears_in_either_held_out_version():
    """Exact-text containment, checked directly rather than inferred from a verdict."""
    from scripts import build_evaluation_corpus as BC

    mine_prompts = {r[4] for r in ROWS}
    mine_targets = {r[5] for r in ROWS}
    for version in QC.HELD_OUT_VERSIONS:
        for entry in BC.corpus_for(version):
            assert entry[3] not in mine_prompts
            assert entry[4] not in mine_targets
