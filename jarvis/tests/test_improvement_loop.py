"""
tests/test_improvement_loop.py — V65 M18 continuous improvement coordinator.

Proves M18 *coordinates* without duplicating: eval failures become events,
failures are classified to the cheapest adequate remedy (fine-tuning is NOT the
answer to tool/routing/timeout/scope failures), training-eligible failures with a
trustworthy target flow to M16 curation (and stop at PENDING_REVIEW — never
approved/trained), a training-warranted failure with no target routes to human
authoring instead of fabricating one, and cycles are bounded.
"""
from __future__ import annotations

import asyncio

from core.dataset_pipeline import CandidateStatus, DatasetPipeline
from core.eval_harness import EvalCase, EvalResult, EvalRun
from core.improvement_loop import (
    FailureCategory,
    ImprovementAction,
    ImprovementCoordinator,
    RemedyKind,
    classify_failure,
    ImprovementEvent,
)


def _run(coro):
    return asyncio.run(coro)


def _event(failures, domain="general", error=None, case_id="c1") -> ImprovementEvent:
    return ImprovementEvent(source="eval", case_id=case_id, domain=domain,
                            failures=tuple(failures), error=error)


# ── event extraction ──────────────────────────────────────────────────────────
def test_events_only_from_failed_results():
    cases = [EvalCase(id="c1", domain="general", prompt="p1"),
             EvalCase(id="c2", domain="general", prompt="p2")]
    run = EvalRun(run_id="r", results=[
        EvalResult(case_id="c1", domain="general", passed=False, score=0.0, failures=["correctness:missing=['x']"]),
        EvalResult(case_id="c2", domain="general", passed=True, score=1.0),
    ])
    events = ImprovementCoordinator().events_from_eval_run(run, cases)
    assert [e.case_id for e in events] == ["c1"]


# ── classification: remedy is the cheapest adequate fix ───────────────────────
def test_injection_failure_is_non_training():
    c = classify_failure(_event(["injection_resistance:got=False want=True"]))
    assert c.category is FailureCategory.PROMPT_INJECTION_FAILURE
    assert c.remedy is RemedyKind.FIREWALL_ADVERSARIAL_EVAL
    assert not c.requires_training


def test_tool_choice_failure_routes_to_tool_schema():
    c = classify_failure(_event(["tool_choice:missing_tools=['web_search']"]))
    assert c.category is FailureCategory.TOOL_SELECTION_ERROR
    assert c.remedy is RemedyKind.TOOL_SCHEMA and not c.requires_training


def test_forbidden_tool_routes_to_scope_review():
    c = classify_failure(_event(["tool_safety:forbidden_tools_used=['rm']"]))
    assert c.category is FailureCategory.SCOPE_POLICY_ERROR
    assert c.remedy is RemedyKind.SCOPE_POLICY_REVIEW and not c.requires_training


def test_timeout_routes_to_scheduling():
    c = classify_failure(_event(["timeout"], error="timeout"))
    assert c.category is FailureCategory.TIMEOUT
    assert c.remedy is RemedyKind.SCHEDULING_POLICY and not c.requires_training


def test_routing_failure_routes_to_routing_policy():
    c = classify_failure(_event(["domain_routing:got=general want=coder"]))
    assert c.category is FailureCategory.ROUTING_ERROR
    assert c.remedy is RemedyKind.ROUTING_POLICY and not c.requires_training


def test_citation_failure_routes_to_rag():
    c = classify_failure(_event(["citation_validity"], domain="research"))
    assert c.category is FailureCategory.CITATION_ERROR
    assert c.remedy is RemedyKind.TRUSTED_RAG and not c.requires_training


def test_research_correctness_is_knowledge_gap_not_training():
    c = classify_failure(_event(["correctness:missing=['fact']"], domain="research"))
    assert c.category is FailureCategory.KNOWLEDGE_GAP
    assert c.remedy is RemedyKind.TRUSTED_RAG and not c.requires_training


def test_general_reasoning_correctness_is_training_candidate():
    c = classify_failure(_event(["correctness:missing=['step']"], domain="general"))
    assert c.category is FailureCategory.REASONING_ERROR
    assert c.remedy is RemedyKind.TRAINING_CANDIDATE and c.requires_training


def test_unknown_failure_routes_to_human_review():
    c = classify_failure(_event(["some_novel_dimension:weird"]))
    assert c.category is FailureCategory.UNKNOWN and c.remedy is RemedyKind.HUMAN_REVIEW


# ── plan cycle: fine-tuning is not the answer to every failure ────────────────
def _mixed_run():
    cases = [
        EvalCase(id="inj", domain="research", prompt="p", ground_truth="gt"),
        EvalCase(id="tool", domain="general", prompt="p"),
        EvalCase(id="reason", domain="general", prompt="explain the tradeoff", ground_truth="the ideal reasoned answer"),
    ]
    run = EvalRun(run_id="r", results=[
        EvalResult(case_id="inj", domain="research", passed=False, score=0.0, failures=["injection_resistance:x"]),
        EvalResult(case_id="tool", domain="general", passed=False, score=0.0, failures=["tool_choice:missing"]),
        EvalResult(case_id="reason", domain="general", passed=False, score=0.0, failures=["correctness:missing=['y']"]),
    ])
    return run, cases


def test_plan_cycle_routes_most_failures_away_from_training():
    run, cases = _mixed_run()
    cycle = ImprovementCoordinator().plan_cycle(run, cases, cycle_id="c1")
    assert len(cycle.candidates) == 3
    # Only the reasoning failure (with a ground-truth target) is a training candidate.
    assert len(cycle.training_candidates()) == 1
    remedies = cycle.remedy_breakdown()
    assert remedies.get("training_candidate") == 1
    assert remedies.get("firewall_adversarial_eval") == 1
    assert remedies.get("tool_schema") == 1


def test_training_candidate_built_from_ground_truth():
    run, cases = _mixed_run()
    cycle = ImprovementCoordinator().plan_cycle(run, cases)
    tc = cycle.training_candidates()[0]
    assert tc.failure_ref == "reason" and tc.ideal_output == "the ideal reasoned answer"


def test_training_warranted_without_target_routes_to_human():
    # Reasoning failure but NO ground truth ⇒ no trustworthy target ⇒ human review,
    # never a fabricated training example.
    cases = [EvalCase(id="r", domain="general", prompt="explain")]
    run = EvalRun(run_id="run", results=[
        EvalResult(case_id="r", domain="general", passed=False, score=0.0, failures=["correctness:missing=['z']"]),
    ])
    cycle = ImprovementCoordinator().plan_cycle(run, cases)
    cand = cycle.candidates[0]
    assert cand.classification.requires_training
    assert cand.action is ImprovementAction.HUMAN_REVIEW
    assert cand.training_candidate is None and "human-authored" in cand.notes
    assert cycle.training_candidates() == []


# ── bounded cycle ─────────────────────────────────────────────────────────────
def test_cycle_is_bounded_and_reports_dropped():
    cases = [EvalCase(id=f"c{i}", domain="general", prompt="p") for i in range(5)]
    run = EvalRun(run_id="r", results=[
        EvalResult(case_id=f"c{i}", domain="general", passed=False, score=0.0, failures=["correctness:x"])
        for i in range(5)
    ])
    cycle = ImprovementCoordinator(max_events_per_cycle=2).plan_cycle(run, cases)
    assert len(cycle.candidates) == 2 and cycle.dropped_events == 3


# ── training candidates flow to M16 and STOP at PENDING_REVIEW ─────────────────
def test_curated_training_candidates_never_auto_approved():
    run, cases = _mixed_run()
    coord = ImprovementCoordinator()
    cycle = coord.plan_cycle(run, cases)
    report = _run(coord.curate_training_candidates(cycle, DatasetPipeline(), version="imp-v1"))
    assert report is not None
    # Every survivor is PENDING_REVIEW — the coordinator never approves or trains.
    assert all(e.status is not CandidateStatus.APPROVED for e in report.examples)
    assert len(report.pending_review) >= 1


def test_curate_returns_none_when_no_training_candidates():
    # A run whose only failure is a tool error ⇒ no training candidates at all.
    cases = [EvalCase(id="t", domain="general", prompt="p")]
    run = EvalRun(run_id="r", results=[
        EvalResult(case_id="t", domain="general", passed=False, score=0.0, failures=["tool_choice:missing"]),
    ])
    coord = ImprovementCoordinator()
    cycle = coord.plan_cycle(run, cases)
    report = _run(coord.curate_training_candidates(cycle, DatasetPipeline()))
    assert report is None
