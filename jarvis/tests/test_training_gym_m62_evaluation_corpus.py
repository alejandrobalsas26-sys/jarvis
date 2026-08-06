"""V69 M62 S3E.1 — held-out evaluation material, built through the training chain.

WHY THE AUTHORITIES HAD TO CHANGE AT ALL
----------------------------------------
Three separate places assumed "approved evidence" and "training material" were the same
thing, so evaluation-only material could not be produced by the pipeline that produces
everything else:

  * ``Episode.approval_blockers`` refused to approve an evaluation-only task, even
    though ``Episode.outcome()`` already derives dataset eligibility separately;
  * ``build_candidate`` hard-coded ``evaluation_only=False`` and ``dataset_eligible=True``
    into every record it made;
  * ``plan_splits`` dropped evaluation-only candidates before assigning anything, so no
    such record could reach a split, a manifest or a dataset version.

The result was that the only way to obtain held-out evidence was to hand-write a
candidate and skip the chain — which is the thing the chain exists to prevent.

These tests pin the separation in both directions. Evaluation-only material must be
buildable, and it must remain unusable for fitting: not in TRAIN, not in VALIDATION, not
through an override, and not by a caller passing the wrong flag.
"""
from __future__ import annotations

import pytest

from training_gym.datasets.candidate import CandidateState, DatasetSplit
from training_gym.datasets.manifests import TRAIN_SIDE_SPLITS
from training_gym.datasets.split import (
    SplitError,
    SplitPolicy,
    leakage_group_key,
    plan_splits,
)
from training_gym.schemas import SchemaError

pytest.importorskip("scripts.build_evaluation_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402

ENTRIES = BC.corpus()
HELD_OUT = (DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
            DatasetSplit.ADVERSARIAL)


@pytest.fixture(scope="module")
def candidates():
    """Every record, built once through the real chain."""
    return [BC.make_candidate(entry) for entry in ENTRIES]


@pytest.fixture(scope="module")
def ready(candidates):
    advanced = []
    for candidate in candidates:
        for state in (CandidateState.VALIDATED, CandidateState.PRIVACY_CHECKED,
                      CandidateState.PROVENANCE_CHECKED,
                      CandidateState.LEAKAGE_CHECKED,
                      CandidateState.READY_FOR_PROMOTION):
            candidate = candidate.with_state(state)
        advanced.append(candidate)
    return advanced


# ── the corpus is actually balanced ───────────────────────────────────────────
def test_the_corpus_clears_the_minimum_pair_policy():
    from training_gym.evaluation.policy import StatisticalPolicy
    assert len(ENTRIES) >= StatisticalPolicy().min_pairs_for_claim


def test_every_split_carries_more_than_a_token_number_of_tasks():
    counts: dict[str, int] = {}
    for split, *_ in ENTRIES:
        counts[split] = counts.get(split, 0) + 1
    assert set(counts) == {s.name for s in HELD_OUT}
    assert all(n >= 8 for n in counts.values()), counts


def test_no_validation_material_is_produced():
    """VALIDATION is train-side here; a model is steered on it.

    Held-out evidence a model was steered on measures nothing, so the corpus stays out
    of it rather than weakening the control that says so.
    """
    assert DatasetSplit.VALIDATION in TRAIN_SIDE_SPLITS
    assert not any(split == "VALIDATION" for split, *_ in ENTRIES)


def test_every_mandatory_family_appears_more_than_once():
    counts: dict[str, int] = {}
    for _split, family, *_ in ENTRIES:
        counts[family] = counts.get(family, 0) + 1
    for family in ("STRUCTURED_REPORT", "EVIDENCE_REQUEST", "SAFETY_REFUSAL",
                   "TOOL_CALL_SCHEMA"):
        assert counts.get(family, 0) > 1, f"{family}: {counts}"


def test_the_policy_mandated_families_are_present():
    from training_gym.evaluation.policy import TaskFamilyPolicy
    present = {BC.corpus.__globals__["FAMILY_GRADERS"] and family
               for _s, family, *_ in ENTRIES}
    from training_gym.task_spec import TaskFamily
    values = {getattr(TaskFamily, name).value for name in present}
    for mandatory in TaskFamilyPolicy().mandatory_families:
        assert mandatory in values, f"{mandatory} missing from {sorted(values)}"


def test_both_decision_classes_are_represented():
    """A corpus of only refusals cannot distinguish safe from uselessly cautious."""
    refusals = [e for e in ENTRIES if e[1] == "SAFETY_REFUSAL"]
    completions = [e for e in ENTRIES if e[1] != "SAFETY_REFUSAL"]
    assert refusals and completions
    # A safe completion inside the security split is what catches over-refusal.
    assert any(e[0] == "SECURITY_REGRESSION" and e[1] != "SAFETY_REFUSAL"
               for e in ENTRIES)


def test_every_task_id_and_prompt_is_unique():
    ids = [e[2] for e in ENTRIES]
    assert len(set(ids)) == len(ids)
    prompts = [e[3] for e in ENTRIES]
    assert len(set(prompts)) == len(prompts)


def test_no_prompt_contains_its_own_answer():
    """The leak check `build_task` runs, asserted on the source material directly."""
    from training_gym.evaluation.task_pack import target_leaked_into
    for _split, _family, task_id, prompt, target in ENTRIES:
        assert not target_leaked_into(prompt, target), task_id


# ── what the records are ──────────────────────────────────────────────────────
def test_every_record_is_evaluation_only_and_never_trainable(candidates):
    for candidate in candidates:
        assert candidate.evaluation_only is True
        assert candidate.dataset_eligible is False


def test_no_record_is_exportable_to_a_teacher(candidates):
    """A held-out expected answer must never travel to a reviewing model."""
    for candidate in candidates:
        assert not candidate.sensitivity.exportable_to_teacher


def test_every_record_carries_its_full_evidence_chain(candidates):
    for candidate in candidates:
        for digest in (candidate.deterministic_report_hash,
                       candidate.consensus_report_hash,
                       candidate.human_review_hash,
                       candidate.source_episode_hash,
                       candidate.approval_hash):
            assert len(digest) == 64, candidate.candidate_id


def test_a_caller_cannot_mislabel_training_material_as_held_out():
    """The task authority decides, not the flag the builder was called with."""
    entry = ENTRIES[0]
    with pytest.raises(SchemaError):
        BC.make_candidate.__globals__  # keep the import graph honest
        _build_with_flag(entry, evaluation_only=False)


def _build_with_flag(entry, *, evaluation_only: bool):
    """Rebuild one record while flipping only the builder's intent flag."""
    import training_gym.datasets.promotion as P
    original = P.build_candidate

    def intercept(**kwargs):
        kwargs["evaluation_only"] = evaluation_only
        return original(**kwargs)

    P.build_candidate = intercept
    try:
        return BC.make_candidate(entry)
    finally:
        P.build_candidate = original


# ── where they may go ─────────────────────────────────────────────────────────
def test_the_planner_places_every_record_in_the_split_it_was_written_for(ready):
    forced = {leakage_group_key(c): DatasetSplit(e[0].lower())
              for c, e in zip(ready, ENTRIES)}
    plan = plan_splits(ready, policy=SplitPolicy(seed="corpus-test"), forced=forced)
    assert plan.excluded == ()
    for candidate, entry in zip(ready, ENTRIES):
        assert plan.assignments[candidate.candidate_id] == entry[0].lower()


def test_without_an_explicit_destination_the_records_are_excluded_not_hashed(ready):
    """The hash can land on TRAIN, so held-out-only records never reach it."""
    plan = plan_splits(ready, policy=SplitPolicy(seed="corpus-test"))
    assert set(plan.excluded) == {c.candidate_id for c in ready}
    assert plan.assignments == {}


@pytest.mark.parametrize("train_side", sorted(TRAIN_SIDE_SPLITS, key=lambda s: s.value))
def test_no_override_can_force_held_out_material_into_a_trainable_split(ready,
                                                                       train_side):
    forced = {leakage_group_key(ready[0]): train_side}
    with pytest.raises(SplitError):
        plan_splits(ready, policy=SplitPolicy(seed="corpus-test"), forced=forced)


def test_a_merged_group_that_would_land_in_training_is_refused(ready):
    """The backstop, for when a group's key is not the one an override named.

    Groups merge transitively through shared fixtures, so a forced key can stop
    matching. Rather than reason about when, the outcome itself is refused.
    """
    from dataclasses import replace
    merged = [replace(c, lineage_group="shared-lineage",
                      input_fixture_hashes=("f" * 64,)) for c in ready[:2]]
    assert leakage_group_key(merged[0]) == leakage_group_key(merged[1]), \
        "these must land in one group for this test to mean anything"
    # An override naming the ORIGINAL key no longer matches the merged group, so the
    # records fall through to exclusion rather than to the hash.
    stale = {leakage_group_key(ready[0]): DatasetSplit.ADVERSARIAL}
    plan = plan_splits(merged, policy=SplitPolicy(seed="corpus-test"), forced=stale)
    assert set(plan.excluded) == {c.candidate_id for c in merged}
    assert plan.assignments == {}


def test_the_leakage_analyser_reports_a_clean_corpus(ready):
    from training_gym.datasets.leakage import LeakageAnalyzer, LeakageVerdict
    forced = {leakage_group_key(c): DatasetSplit(e[0].lower())
              for c, e in zip(ready, ENTRIES)}
    plan = plan_splits(ready, policy=SplitPolicy(seed="corpus-test"), forced=forced)
    report = LeakageAnalyzer().analyze(ready, plan=plan)
    assert report.verdict is LeakageVerdict.CLEAN, [f.detail for f in report.findings[:3]]
    assert not report.blocks_finalization


# ── the promotion itself ──────────────────────────────────────────────────────
def test_the_corpus_promotes_into_a_verifiable_version(tmp_path):
    """End to end, into a throwaway root: no hand-written manifest, no invented hash."""
    from training_gym.datasets.manifests import load_manifest

    summary = BC.build(tmp_path)
    assert summary["promoted"] == len(ENTRIES)
    assert summary["leakage_verdict"] == "clean"

    manifest = load_manifest(root=tmp_path, dataset_id=BC.DATASET_ID,
                             dataset_version=BC.DATASET_VERSION)
    assert manifest.manifest_hash() == summary["manifest_hash"]
    assert manifest.total_records == len(ENTRIES)
    for split in HELD_OUT:
        assert len(manifest.candidate_ids_in(split)) >= 8
    for split in TRAIN_SIDE_SPLITS:
        assert manifest.candidate_ids_in(split) == ()


def test_the_promotion_is_deterministic(tmp_path):
    """Same material, same version, same digest — twice, in different directories."""
    first = BC.build(tmp_path / "a")
    second = BC.build(tmp_path / "b")
    assert first["manifest_hash"] == second["manifest_hash"]
    assert first["leakage_report_hash"] == second["leakage_report_hash"]


def test_a_second_promotion_into_the_same_root_is_refused(tmp_path):
    BC.build(tmp_path)
    with pytest.raises(Exception):
        BC.build(tmp_path)
