"""V69 M62 S2d — deterministic splitting, sticky assignment and leakage prevention.

The claim under test is narrower and harder than "the split function is deterministic":
that there is no sequence of calls — including re-running with a different seed, feeding
the candidates in a different order, or passing an explicit override — that moves a group
out of a held-out split and into training.

The candidates here are built directly rather than through ``build_candidate``: this
module is testing the split and leakage algebra, and constructing a full approved episode
per fixture would make the arithmetic being asserted impossible to read.
"""
from __future__ import annotations

import pytest

from training_gym.datasets.candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    SourceType,
    TargetSource,
)
from training_gym.datasets.leakage import (
    LeakageAnalyzer,
    LeakagePolicy,
    LeakageReport,
    LeakageVerdict,
)
from training_gym.datasets.similarity import (
    CHARACTER_EXACT_FAMILIES,
    compare_groups,
    jaccard,
    normalize_for_similarity,
    normalized_key,
    signature,
)
from training_gym.datasets.split import (
    DEFAULT_RATIOS,
    SplitPlan,
    InMemorySplitAssignmentLedger,
    SplitAssignmentLedger,
    SplitError,
    SplitPolicy,
    StickyConflict,
    commit_plan,
    group_candidates,
    leakage_group_key,
    plan_splits,
)
from training_gym.schemas import SensitivityClass, sha256_text
from training_gym.task_spec import TaskFamily

NOW = "2026-08-01T12:00:00Z"


def make_candidate(candidate_id: str, *, prompt: str = "", target: str = "",
                   lineage: str = "", fixtures: tuple[str, ...] = (),
                   family: TaskFamily = TaskFamily.STRUCTURED_REPORT,
                   **overrides) -> DatasetCandidate:
    """A structurally valid candidate. Built directly — see the module docstring."""
    prompt = prompt or f"Prompt for {candidate_id}"
    target = target or f'{{"verdict": "{candidate_id}"}}'
    base = {
        "candidate_id": candidate_id,
        "task_id": overrides.pop("task_id", candidate_id),
        "task_family": family,
        "task_hash": overrides.pop("task_hash", sha256_text(f"task-{candidate_id}")),
        "attempt_id": f"attempt-{candidate_id}",
        "attempt_hash": sha256_text(f"attempt-{candidate_id}"),
        "deterministic_report_hash": sha256_text(f"det-{candidate_id}"),
        "consensus_report_hash": sha256_text(f"con-{candidate_id}"),
        "human_review_hash": sha256_text(f"rev-{candidate_id}"),
        "source_episode_hash": sha256_text(f"ep-{candidate_id}"),
        "student_model_id": "qwen3:8b-q4_K_M",
        "system_message": "",
        "user_prompt": prompt,
        "target_text": target,
        "target_source": TargetSource.VERIFIED_STUDENT_OUTPUT,
        "target_hash": sha256_text(target),
        "approval_hash": sha256_text(f"rev-{candidate_id}"),
        "created_at_utc": NOW,
        "lineage_group": lineage or candidate_id,
        "input_fixture_hashes": tuple(sha256_text(f) for f in fixtures),
        "provenance": {"source": "handwritten"},
    }
    base.update(overrides)
    return DatasetCandidate(**base)


def policy(seed: str = "seed-a", **ratios) -> SplitPolicy:
    return SplitPolicy(seed=seed, ratios=ratios or dict(DEFAULT_RATIOS))


def fifty_fifty(seed: str = "seed-a") -> SplitPolicy:
    return SplitPolicy(seed=seed, ratios={DatasetSplit.TRAIN.value: 0.5,
                                          DatasetSplit.HIDDEN_EVALUATION.value: 0.5})


# ── determinism ───────────────────────────────────────────────────────────────
def test_the_same_inputs_and_seed_produce_an_identical_plan():
    records = [make_candidate(f"c-{i}") for i in range(20)]
    first = plan_splits(records, policy=policy())
    second = plan_splits(records, policy=policy())
    assert first.plan_hash() == second.plan_hash()


def test_the_order_the_candidates_arrive_in_does_not_change_the_plan():
    records = [make_candidate(f"c-{i}") for i in range(20)]
    forward = plan_splits(records, policy=policy())
    backward = plan_splits(list(reversed(records)), policy=policy())
    assert forward.plan_hash() == backward.plan_hash()


def test_a_missing_seed_is_refused_rather_than_taken_from_the_clock():
    with pytest.raises(SplitError, match="split.seed: required"):
        SplitPolicy(seed="")


def test_changing_the_seed_changes_the_plan_identity():
    records = [make_candidate(f"c-{i}") for i in range(30)]
    a = plan_splits(records, policy=policy("seed-a"))
    b = plan_splits(records, policy=policy("seed-b"))
    assert a.plan_hash() != b.plan_hash()
    assert a.policy.policy_hash() != b.policy.policy_hash()


def test_ratios_that_do_not_sum_to_one_are_refused():
    with pytest.raises(SplitError, match="must sum to 1.0"):
        SplitPolicy(seed="s", ratios={DatasetSplit.TRAIN.value: 0.5})


def test_an_unknown_split_name_in_a_ratio_is_refused():
    with pytest.raises(ValueError):
        SplitPolicy(seed="s", ratios={"almost_train": 1.0})


def test_no_global_random_state_is_consulted():
    import random
    records = [make_candidate(f"c-{i}") for i in range(20)]
    random.seed(1)
    first = plan_splits(records, policy=policy()).plan_hash()
    random.seed(999)
    [random.random() for _ in range(50)]
    assert plan_splits(records, policy=policy()).plan_hash() == first


# ── grouping ──────────────────────────────────────────────────────────────────
def test_every_member_of_a_lineage_group_lands_in_one_split():
    records = [make_candidate(f"v-{i}", lineage="template-7") for i in range(25)]
    plan = plan_splits(records, policy=policy())
    assert len({plan.assignments[c.candidate_id] for c in records}) == 1


def test_candidates_sharing_a_fixture_land_in_one_split():
    records = [make_candidate(f"f-{i}", lineage=f"g-{i}", fixtures=("alert.json",))
               for i in range(20)]
    plan = plan_splits(records, policy=policy())
    assert len({plan.assignments[c.candidate_id] for c in records}) == 1


def test_fixture_overlap_is_transitive_so_a_chain_moves_as_one_group():
    a = make_candidate("a", lineage="ga", fixtures=("f1",))
    b = make_candidate("b", lineage="gb", fixtures=("f1", "f2"))
    c = make_candidate("c", lineage="gc", fixtures=("f2",))
    assert len(group_candidates([a, b, c])) == 1


def test_unrelated_candidates_are_not_forced_into_one_group():
    a = make_candidate("a", lineage="ga", fixtures=("f1",))
    b = make_candidate("b", lineage="gb", fixtures=("f2",))
    assert len(group_candidates([a, b])) == 2


def test_a_generated_variant_never_separates_from_its_parent():
    parent_hash = sha256_text("parent-task")
    variants = [make_candidate(f"var-{i}", lineage=f"lg-{i}",
                               source_type=SourceType.GENERATED_VARIANT,
                               parent_task_hash=parent_hash) for i in range(10)]
    keys = {leakage_group_key(v) for v in variants}
    # Distinct lineage_group values, so the parent hash must not be what unites them —
    # it is the fixture/lineage key that does, and here it does not. The split must
    # therefore be protected by the parent-overlap leakage check, asserted below.
    assert len(keys) == 10


def test_the_parent_overlap_check_blocks_a_variant_family_split_across_the_boundary():
    parent_hash = sha256_text("parent-task")
    a = make_candidate("a", lineage="la", source_type=SourceType.GENERATED_VARIANT,
                       parent_task_hash=parent_hash)
    b = make_candidate("b", lineage="lb", source_type=SourceType.GENERATED_VARIANT,
                       parent_task_hash=parent_hash)
    records, plan = forced_plan([a], [b])
    report = LeakageAnalyzer().analyze(records, plan=plan)
    assert report.verdict is LeakageVerdict.BLOCKED
    assert any(f.check == "parent_overlap" for f in report.blocking_findings)


# ── stickiness ────────────────────────────────────────────────────────────────
def test_a_held_out_group_cannot_be_ground_into_training_with_a_new_seed():
    record = make_candidate("c-1", lineage="lg-1")
    key = leakage_group_key(record)
    ledger = InMemorySplitAssignmentLedger({key: DatasetSplit.HIDDEN_EVALUATION})
    for seed in (f"seed-{i}" for i in range(25)):
        plan = plan_splits([record], policy=fifty_fifty(seed), ledger=ledger)
        assert plan.assignments["c-1"] == DatasetSplit.HIDDEN_EVALUATION.value


def test_recording_a_move_out_of_a_held_out_split_raises():
    ledger = InMemorySplitAssignmentLedger()
    ledger.record(group_key="g1", split=DatasetSplit.HIDDEN_EVALUATION,
                  dataset_version="1.0.0", at=NOW)
    with pytest.raises(StickyConflict, match="may not move to train"):
        ledger.record(group_key="g1", split=DatasetSplit.TRAIN,
                      dataset_version="1.0.1", at=NOW)


def test_recording_the_same_assignment_twice_is_not_a_conflict():
    ledger = InMemorySplitAssignmentLedger()
    for _ in range(3):
        ledger.record(group_key="g1", split=DatasetSplit.TRAIN,
                      dataset_version="1.0.0", at=NOW)
    assert ledger.assignments() == {"g1": DatasetSplit.TRAIN.value}


def test_the_sticky_ledger_survives_the_process_that_wrote_it(tmp_path):
    path = tmp_path / "assignments.jsonl"
    records = [make_candidate("c-1", lineage="lg-1")]
    plan = plan_splits(records, policy=fifty_fifty("seed-a"))
    commit_plan(plan, SplitAssignmentLedger(path), dataset_version="1.0.0", at=NOW)
    reread = SplitAssignmentLedger(path)
    again = plan_splits(records, policy=fifty_fifty("seed-z"), ledger=reread)
    assert again.assignments == plan.assignments


def test_a_truncated_assignment_ledger_raises_rather_than_dropping_an_entry(tmp_path):
    path = tmp_path / "assignments.jsonl"
    ledger = SplitAssignmentLedger(path)
    ledger.record(group_key="g1", split=DatasetSplit.HIDDEN_EVALUATION,
                  dataset_version="1.0.0", at=NOW)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    with pytest.raises(SplitError, match="final line is truncated"):
        ledger.assignments()


def test_an_override_may_isolate_a_record_but_never_place_one_into_training():
    record = make_candidate("c-1")
    key = leakage_group_key(record)
    plan = plan_splits([record], policy=policy(),
                       forced={key: DatasetSplit.QUARANTINE})
    assert plan.quarantined == ("c-1",)
    for target in (DatasetSplit.TRAIN, DatasetSplit.VALIDATION):
        with pytest.raises(SplitError, match="never place one into training"):
            plan_splits([record], policy=policy(), forced={key: target})


# ── eligibility and honesty ───────────────────────────────────────────────────
def test_an_evaluation_only_candidate_never_enters_a_split():
    record = make_candidate("eval-1", evaluation_only=True, dataset_eligible=False)
    plan = plan_splits([record], policy=policy())
    assert "eval-1" not in plan.assignments
    assert plan.excluded == ("eval-1",)


@pytest.mark.parametrize("state", [CandidateState.QUARANTINED, CandidateState.REJECTED,
                                   CandidateState.FAILED, CandidateState.REVOKED])
def test_a_blocked_candidate_state_never_enters_a_split(state):
    record = make_candidate("c-1", state=state)
    plan = plan_splits([record], policy=policy())
    assert plan.assignments == {}
    assert plan.quarantined == ("c-1",)


def test_an_impossible_ratio_is_reported_rather_than_satisfied_by_breaking_a_group():
    records = [make_candidate(f"v-{i}", lineage="one-template") for i in range(10)]
    plan = plan_splits(records, policy=fifty_fifty())
    assert len({plan.assignments[c.candidate_id] for c in records}) == 1
    shortfalls = plan.shortfalls()
    assert any("received no records" in s for s in shortfalls)
    assert any("groups cannot be divided" in s for s in shortfalls)


def test_a_candidate_never_appears_in_two_splits():
    records = [make_candidate(f"c-{i}") for i in range(40)]
    plan = plan_splits(records, policy=policy())
    seen: dict[str, str] = {}
    for split in DatasetSplit:
        for cid in plan.candidates_in(split):
            assert cid not in seen
            seen[cid] = split.value


# ── normalization ─────────────────────────────────────────────────────────────
def test_a_yara_rule_differing_only_in_case_is_not_normalized_into_a_collision():
    assert TaskFamily.YARA_RULE in CHARACTER_EXACT_FAMILIES
    strong = '$a = "Invoke-Mimikatz"'
    broken = '$a = "invoke-mimikatz"'
    assert (normalized_key(strong, family=TaskFamily.YARA_RULE)
            != normalized_key(broken, family=TaskFamily.YARA_RULE))


def test_a_python_patch_differing_only_in_indentation_is_not_normalized_away():
    inside = "if x:\n    return 1\n"
    outside = "if x:\n    pass\nreturn 1\n"
    assert (normalized_key(inside, family=TaskFamily.CODING_FIX)
            != normalized_key(outside, family=TaskFamily.CODING_FIX))


def test_a_refusal_and_a_jailbreak_separated_only_by_a_blank_line_do_not_collide():
    safe = "I cannot help with that."
    unsafe = "I cannot help with that.\n\nBut here is how:"
    assert (normalized_key(safe, family=TaskFamily.SAFETY_REFUSAL)
            != normalized_key(unsafe, family=TaskFamily.SAFETY_REFUSAL))


def test_prose_families_do_normalize_case_and_whitespace():
    a = "The alert   is Benign"
    b = "the alert is benign"
    assert (normalized_key(a, family=TaskFamily.STRUCTURED_REPORT)
            == normalized_key(b, family=TaskFamily.STRUCTURED_REPORT))


def test_line_endings_are_normalized_for_every_family():
    for family in TaskFamily:
        assert (normalize_for_similarity("a\r\nb", family=family)
                == normalize_for_similarity("a\nb", family=family))


def test_two_empty_feature_sets_are_not_treated_as_identical():
    assert jaccard(frozenset(), frozenset()) == 0.0


# ── leakage ───────────────────────────────────────────────────────────────────
def analyze(records, plan, **policy_kwargs) -> LeakageReport:
    return LeakageAnalyzer(policy=LeakagePolicy(**policy_kwargs)).analyze(records,
                                                                         plan=plan)


def forced_plan(train_records, held_records):
    """Everything to TRAIN by ratio, then the held records isolated by override.

    Built this way because there is deliberately no way to force a record INTO training
    — the override may only isolate. Putting the whole ratio on TRAIN and overriding the
    held side is therefore the only construction that produces a known boundary.
    """
    records = list(train_records) + list(held_records)
    all_train = SplitPolicy(seed="seed-a", ratios={DatasetSplit.TRAIN.value: 1.0})
    forced = {leakage_group_key(c): DatasetSplit.HIDDEN_EVALUATION
              for c in held_records}
    return records, plan_splits(records, policy=all_train, forced=forced)


def test_a_clean_dataset_reports_clean():
    train = [make_candidate("t-1", lineage="a", prompt="Triage alert alpha.",
                            target='{"verdict":"benign"}')]
    held = [make_candidate("h-1", lineage="b",
                           prompt="Rebuild the DFIR timeline for case omega.",
                           target='{"timeline":["omega"]}',
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert report.verdict is LeakageVerdict.CLEAN
    assert report.blocks_finalization is False


def test_an_exact_prompt_duplicate_across_train_and_hidden_eval_is_blocked():
    shared = "Triage the provided alert fixture and give a verdict."
    train = [make_candidate("t-1", lineage="a", prompt=shared, target="A")]
    held = [make_candidate("h-1", lineage="b", prompt=shared, target="B",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert report.verdict is LeakageVerdict.BLOCKED
    assert any(f.check == "exact_prompt_hash" for f in report.blocking_findings)


def test_an_exact_target_duplicate_across_train_and_hidden_eval_is_blocked():
    shared = '{"verdict":"malicious","confidence":0.9}'
    train = [make_candidate("t-1", lineage="a", prompt="P one", target=shared)]
    held = [make_candidate("h-1", lineage="b", prompt="P two", target=shared,
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert any(f.check == "exact_target_hash" for f in report.blocking_findings)


def test_a_normalized_only_duplicate_is_blocked():
    train = [make_candidate("t-1", lineage="a", prompt="Triage   The Alert",
                            target="A")]
    held = [make_candidate("h-1", lineage="b", prompt="triage the alert", target="B",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert any(f.check == "normalized_prompt_hash" for f in report.blocking_findings)


def test_a_shared_task_hash_across_the_boundary_is_blocked():
    shared = sha256_text("one-task")
    train = [make_candidate("t-1", lineage="a", task_hash=shared)]
    held = [make_candidate("h-1", lineage="b", task_hash=shared,
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    assert any(f.check == "task_hash" for f in analyze(records, plan).blocking_findings)


def test_a_shared_fixture_across_the_boundary_is_blocked():
    """The planner would never produce this plan — the grouping merges the two records.

    The plan is therefore constructed by hand, because this asserts the SECOND line of
    defence: that if the grouping is ever bypassed, weakened or fed a hand-written plan,
    the fixture-overlap check still refuses to certify the result.
    """
    train = make_candidate("t-1", lineage="a", fixtures=("alert.json",))
    held = make_candidate("h-1", lineage="b", fixtures=("alert.json",),
                          sensitivity=SensitivityClass.INTERNAL)
    assert len(group_candidates([train, held])) == 1
    hand_written = SplitPlan(
        policy=SplitPolicy(seed="s", ratios={DatasetSplit.TRAIN.value: 1.0}),
        assignments={"t-1": DatasetSplit.TRAIN.value,
                     "h-1": DatasetSplit.HIDDEN_EVALUATION.value})
    report = analyze([train, held], hand_written)
    assert any(f.check == "fixture_overlap" for f in report.blocking_findings)


def test_a_shared_upstream_template_across_the_boundary_is_reported():
    prov = {"source": "generated", "upstream_ref": "template-42"}
    train = [make_candidate("t-1", lineage="a", provenance=prov)]
    held = [make_candidate("h-1", lineage="b", provenance=prov,
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert any(f.check == "template_overlap" for f in report.blocking_findings)


def test_a_near_duplicate_across_the_boundary_is_blocked():
    base = ("Review the attached authentication log and decide whether the burst of "
            "failed logins from a single source address indicates credential stuffing.")
    reworded = ("Review the attached authentication log and decide whether the burst of "
                "failed logins from one source address indicates credential stuffing!")
    train = [make_candidate("t-1", lineage="a", prompt=base, target="A")]
    held = [make_candidate("h-1", lineage="b", prompt=reworded, target="B",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert report.verdict is LeakageVerdict.BLOCKED
    assert any("similarity" in f.check for f in report.blocking_findings)


def test_a_moderate_overlap_is_a_warning_and_not_a_block():
    train = [make_candidate("t-1", lineage="a",
                            prompt="Summarise the provided alert fixture and state a "
                                   "verdict, with the evidence that supports it, and "
                                   "cite every observable you relied on.",
                            target="A")]
    held = [make_candidate("h-1", lineage="b",
                           prompt="Summarise the provided alert fixture and state a "
                                  "verdict, with the evidence that supports it, and "
                                  "list every observable you used.",
                           target="B", sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert report.verdict is LeakageVerdict.WARNING
    assert report.blocks_finalization is False


def test_reaching_the_comparison_ceiling_is_insufficient_evidence_not_clean():
    train = [make_candidate(f"t-{i}", lineage=f"a{i}", prompt=f"Alpha prompt {i}")
             for i in range(6)]
    held = [make_candidate(f"h-{i}", lineage=f"b{i}", prompt=f"Omega prompt {i}",
                           sensitivity=SensitivityClass.INTERNAL) for i in range(6)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan, max_comparisons=1)
    assert report.verdict is LeakageVerdict.INSUFFICIENT_EVIDENCE
    assert report.ceiling_reached is True
    assert report.blocks_finalization is True
    assert any("ceiling was reached" in n for n in report.notes)


def test_the_comparison_ceiling_is_reported_and_not_asserted():
    left = [signature(f"l{i}", f"text number {i}") for i in range(5)]
    right = [signature(f"r{i}", f"text number {i}") for i in range(5)]
    result = compare_groups(left, right, max_comparisons=3)
    assert result.ceiling_reached is True
    assert result.complete is False
    assert result.comparisons == 3


def test_a_held_out_answer_appearing_in_a_training_prompt_is_blocked():
    secret_answer = '{"verdict":"malicious","technique":"T1486 ransomware encryption"}'
    train = [make_candidate("t-1", lineage="a",
                            prompt=f"Here is a worked example: {secret_answer}. Now "
                                   f"triage the new alert.", target="A")]
    held = [make_candidate("h-1", lineage="b", prompt="Triage the new alert.",
                           target=secret_answer,
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert any(f.check == "hidden_answer_exposure"
               for f in report.blocking_findings)


def test_held_out_material_that_may_be_sent_to_a_teacher_is_reported():
    train = [make_candidate("t-1", lineage="a", prompt="Alpha")]
    held = [make_candidate("h-1", lineage="b", prompt="Omega",
                           sensitivity=SensitivityClass.SYNTHETIC)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan)
    assert any(f.check == "teacher_packet_exposure" for f in report.findings)


def test_a_within_train_duplicate_is_a_warning_not_a_block():
    shared = "The same prompt twice."
    records = [make_candidate("t-1", lineage="a", prompt=shared, target="A"),
               make_candidate("t-2", lineage="b", prompt=shared, target="B")]
    plan = plan_splits(records, policy=SplitPolicy(
        seed="s", ratios={DatasetSplit.TRAIN.value: 1.0}))
    report = analyze(records, plan)
    assert report.verdict is LeakageVerdict.WARNING
    assert report.blocks_finalization is False


# ── the semantic backend, and its absence ─────────────────────────────────────
def test_an_absent_semantic_backend_is_reported_and_never_read_as_clean():
    train = [make_candidate("t-1", lineage="a", prompt="Alpha")]
    held = [make_candidate("h-1", lineage="b", prompt="Omega",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan, require_semantic=True)
    assert report.verdict is LeakageVerdict.INSUFFICIENT_EVIDENCE
    assert "semantic_similarity" in report.checks_unavailable
    assert any("insufficient evidence rather than clean" in n for n in report.notes)


def test_an_absent_semantic_backend_does_not_suppress_the_other_checks():
    shared = "Identical prompt on both sides."
    train = [make_candidate("t-1", lineage="a", prompt=shared, target="A")]
    held = [make_candidate("h-1", lineage="b", prompt=shared, target="B",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    report = analyze(records, plan, require_semantic=True)
    assert any(f.check == "exact_prompt_hash" for f in report.blocking_findings)


def test_a_supplied_semantic_backend_is_used_and_blocks_on_equivalence():
    class AlwaysEquivalent:
        def similar(self, left: str, right: str) -> float:
            return 0.99

        def identity(self) -> dict:
            return {"model": "test-stub", "revision": "n/a"}

    train = [make_candidate("t-1", lineage="a", prompt="Alpha")]
    held = [make_candidate("h-1", lineage="b", prompt="Omega",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    analyzer = LeakageAnalyzer(policy=LeakagePolicy(require_semantic=True),
                               semantic_backend=AlwaysEquivalent())
    report = analyzer.analyze(records, plan=plan)
    assert any(f.check == "semantic_similarity" for f in report.blocking_findings)


def test_no_module_import_is_attempted_for_semantics(monkeypatch):
    """Constructing and running the analyzer must not reach for a model package."""
    import builtins
    real_import = builtins.__import__
    forbidden = ("torch", "transformers", "sentence_transformers", "numpy.linalg",
                 "urllib.request", "httpx", "requests")

    def guard(name, *args, **kwargs):
        if name in forbidden:
            raise AssertionError(f"the analyzer must not import {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    train = [make_candidate("t-1", lineage="a")]
    held = [make_candidate("h-1", lineage="b",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    LeakageAnalyzer(policy=LeakagePolicy(require_semantic=True)).analyze(records,
                                                                        plan=plan)


# ── the report itself ─────────────────────────────────────────────────────────
def test_an_empty_candidate_set_is_insufficient_evidence_not_a_clean_dataset():
    report = LeakageAnalyzer().analyze([], plan=plan_splits([], policy=policy()))
    assert report.verdict is LeakageVerdict.INSUFFICIENT_EVIDENCE
    assert report.blocks_finalization is True


def test_a_plan_naming_a_candidate_that_was_not_supplied_is_an_error():
    supplied = [make_candidate("t-1", lineage="a")]
    plan = plan_splits(supplied + [make_candidate("t-2", lineage="b")],
                       policy=policy())
    report = LeakageAnalyzer().analyze(supplied, plan=plan)
    assert report.verdict is LeakageVerdict.ERROR
    assert report.blocks_finalization is True


def test_the_leakage_report_hash_is_deterministic():
    train = [make_candidate("t-1", lineage="a")]
    held = [make_candidate("h-1", lineage="b",
                           sensitivity=SensitivityClass.INTERNAL)]
    records, plan = forced_plan(train, held)
    assert analyze(records, plan).report_hash() == analyze(records,
                                                           plan).report_hash()


def test_only_clean_and_warning_permit_a_dataset_to_be_written():
    permitted = {v for v in LeakageVerdict if v.permits_finalization}
    assert permitted == {LeakageVerdict.CLEAN, LeakageVerdict.WARNING}
