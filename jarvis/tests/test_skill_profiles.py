"""
tests/test_skill_profiles.py — V65 M15 Agent Skill Profiles.

Proves that skill profiles are a real, validated operating+evaluation contract:
complete role coverage, unique domain ownership, budgets that never exceed the
spec, required verification/evidence for high-risk domains, correct eval-dataset
exposure, a measured evaluation summary from a real M14 EvalRun, and — critically
— that a profile can never weaken ToolExecutor / authority controls and that the
registry actually influences team selection (a production caller, not docs).
"""
from __future__ import annotations

from core.agent_runtime import assemble_task_decision
from core.eval_harness import EvalResult, EvalRun
from core.model_router import ModelRole
from core.skill_profiles import (
    EvidencePolicy,
    SkillEvaluationSummary,
    SkillProfile,
    SkillProfileRegistry,
    get_skill_registry,
)
from core.specialist_runtime import (
    AgentTeamSelector,
    SpecialistRole,
    ToolCategory,
    spec_for,
)
from core.task_domain import TaskDomain


def _reg() -> SkillProfileRegistry:
    return get_skill_registry()


# ── completeness & validity ───────────────────────────────────────────────────
def test_registry_covers_every_specialist_role():
    reg = _reg()
    assert set(reg.roles()) == set(SpecialistRole)
    assert len(reg.all()) == 15


def test_registry_validates_clean():
    assert _reg().validate() == ()


def test_no_domain_owned_by_two_profiles():
    reg = _reg()
    owners: dict[TaskDomain, list[str]] = {}
    for prof in reg.all():
        for dom in prof.supported_domains:
            owners.setdefault(dom, []).append(prof.role.value)
    dupes = {d.value: r for d, r in owners.items() if len(r) > 1}
    assert dupes == {}


def test_every_task_domain_maps_to_a_profile():
    reg = _reg()
    for domain in TaskDomain:
        assert reg.for_domain(domain) is not None, f"{domain} has no profile"


# ── domain → profile mapping ──────────────────────────────────────────────────
def test_domain_maps_to_expected_profile():
    reg = _reg()
    assert reg.for_domain(TaskDomain.CODER).role is SpecialistRole.CODE
    assert reg.for_domain(TaskDomain.RESEARCH).role is SpecialistRole.RESEARCH
    assert reg.for_domain(TaskDomain.DFIR).role is SpecialistRole.DFIR
    assert reg.for_domain(TaskDomain.MATHEMATICS).role is SpecialistRole.MATH


# ── profile influences model role & tier ──────────────────────────────────────
def test_profile_influences_model_role():
    reg = _reg()
    assert reg.for_domain(TaskDomain.CODER).preferred_model_role is ModelRole.CODER
    assert reg.for_domain(TaskDomain.VISION).preferred_model_role is ModelRole.VISION
    assert reg.for_domain(TaskDomain.GENERAL).preferred_model_role is ModelRole.FAST
    assert reg.for_domain(TaskDomain.RESEARCH).preferred_model_role is ModelRole.DEEP


def test_profile_tier_matches_spec_tier():
    reg = _reg()
    for prof in reg.all():
        assert prof.model_tier is spec_for(prof.role).tier


# ── budgets never exceed the spec ─────────────────────────────────────────────
def test_profile_context_budget_never_exceeds_spec():
    reg = _reg()
    for prof in reg.all():
        spec = spec_for(prof.role)
        assert prof.context_budget <= spec.context_budget
        assert prof.resource_budget.max_context <= spec.context_budget


def test_profile_latency_budget_is_positive():
    for prof in _reg().all():
        assert prof.latency_budget_s > 0


# ── required verification / evidence for high-risk domains ────────────────────
def test_high_risk_domains_require_verification():
    reg = _reg()
    for domain in (TaskDomain.RESEARCH, TaskDomain.DFIR, TaskDomain.CYBER_PURPLE,
                   TaskDomain.GRC, TaskDomain.CYBER_BLUE):
        assert reg.for_domain(domain).requires_verification, domain


def test_general_and_language_do_not_force_verification():
    reg = _reg()
    assert not reg.for_domain(TaskDomain.GENERAL).requires_verification
    assert not reg.for_domain(TaskDomain.LANGUAGE).requires_verification


def test_dfir_and_purple_are_fail_closed_verification():
    reg = _reg()
    assert reg.for_domain(TaskDomain.DFIR).fail_closed_verification
    assert reg.for_domain(TaskDomain.CYBER_PURPLE).fail_closed_verification


def test_research_requires_authoritative_evidence():
    reg = _reg()
    assert reg.for_domain(TaskDomain.RESEARCH).evidence_policy is EvidencePolicy.REQUIRED_AUTHORITATIVE
    assert reg.for_domain(TaskDomain.DFIR).requires_evidence
    assert not reg.for_domain(TaskDomain.GENERAL).requires_evidence


# ── eval datasets exposed correctly ───────────────────────────────────────────
def test_profiles_expose_materialized_benchmarks():
    reg = _reg()
    names = {b.name: b for b in reg.benchmark_sets()}
    assert "injection_resistance" in names and names["injection_resistance"].exists()
    assert "sqli" in names and names["sqli"].exists()


def test_code_profile_uses_sqli_benchmark():
    reg = _reg()
    code = reg.for_domain(TaskDomain.CODER)
    assert any(b.name == "sqli" for b in code.benchmark_sets)


def test_research_profile_uses_injection_benchmark():
    reg = _reg()
    research = reg.for_domain(TaskDomain.RESEARCH)
    assert any(b.name == "injection_resistance" for b in research.benchmark_sets)


# ── cannot weaken ToolExecutor / authority ────────────────────────────────────
def test_profile_never_grants_tools_beyond_spec():
    reg = _reg()
    for prof in reg.all():
        spec = spec_for(prof.role)
        assert prof.tool_categories <= spec.allowed_tools


def test_manually_widened_profile_is_rejected_by_validation():
    reg = _reg()
    base = reg.get(SpecialistRole.GENERAL)
    # Attempt to grant RECON (an active-network category GENERAL must never have).
    widened = SkillProfile(
        role=base.role, supported_domains=base.supported_domains,
        preferred_model_role=base.preferred_model_role, model_tier=base.model_tier,
        context_budget=base.context_budget,
        tool_categories=frozenset(base.tool_categories | {ToolCategory.RECON}),
        memory_scopes=base.memory_scopes, evidence_policy=base.evidence_policy,
        verification_policy=base.verification_policy, objectives=base.objectives,
        benchmark_sets=base.benchmark_sets, quality_metrics=base.quality_metrics,
        latency_budget_s=base.latency_budget_s, resource_budget=base.resource_budget,
    )
    issues = widened.validate_against_spec(spec_for(SpecialistRole.GENERAL))
    assert issues and any("beyond spec" in i for i in issues)


def test_widened_profile_makes_registry_construction_fail_closed():
    reg = _reg()
    good = {p.role: p for p in reg.all()}
    base = good[SpecialistRole.GENERAL]
    good[SpecialistRole.GENERAL] = SkillProfile(
        role=base.role, supported_domains=base.supported_domains,
        preferred_model_role=base.preferred_model_role, model_tier=base.model_tier,
        context_budget=base.context_budget + 999_999,  # blow past the spec budget
        tool_categories=base.tool_categories, memory_scopes=base.memory_scopes,
        evidence_policy=base.evidence_policy, verification_policy=base.verification_policy,
        objectives=base.objectives, benchmark_sets=base.benchmark_sets,
        quality_metrics=base.quality_metrics, latency_budget_s=base.latency_budget_s,
        resource_budget=base.resource_budget,
    )
    bad = SkillProfileRegistry(good)
    assert any("context budget" in i for i in bad.validate())


# ── evaluation summary from a real EvalRun ────────────────────────────────────
def _run_with_metrics(run_id: str, metric_ok: bool) -> EvalRun:
    """Build an EvalRun whose metric pass-rates are all-pass or all-fail for the
    dimensions a RESEARCH profile gates on."""
    score = 1.0 if metric_ok else 0.0
    passed = metric_ok
    metrics = {
        "correctness": {"passed": passed}, "citation_validity": {"passed": passed},
        "injection_resistance": {"passed": passed}, "confidence": {"passed": passed},
    }
    return EvalRun(run_id=run_id, results=[
        EvalResult(case_id="c1", domain="research", passed=passed, score=score, metrics=metrics),
    ])


def test_evaluation_summary_promotable_when_thresholds_met():
    reg = _reg()
    summary = reg.evaluate(SpecialistRole.RESEARCH, _run_with_metrics("good", True), now_ts=1.0)
    assert isinstance(summary, SkillEvaluationSummary)
    assert summary.meets_thresholds and summary.promotable
    assert summary.failing_metrics == ()


def test_evaluation_summary_not_promotable_on_regression():
    reg = _reg()
    summary = reg.evaluate(SpecialistRole.RESEARCH, _run_with_metrics("bad", False), now_ts=1.0)
    assert not summary.promotable
    assert summary.failing_metrics  # names which gating metrics failed


def test_evaluation_summary_absent_gating_metric_fails_closed():
    reg = _reg()
    # A run that never exercised the gated dimensions ⇒ absent ⇒ fail-closed.
    empty = EvalRun(run_id="empty", results=[
        EvalResult(case_id="c", domain="research", passed=True, score=1.0, metrics={}),
    ])
    summary = reg.evaluate(SpecialistRole.RESEARCH, empty)
    assert not summary.promotable
    assert any("absent" in f for f in summary.failing_metrics)


# ── production caller: selector consults the profile ──────────────────────────
def test_selector_adds_verifier_for_profile_required_domain():
    # DFIR profile requires (fail-closed) verification — even a non-security,
    # non-explicit-verification turn must pull the VERIFIER into the team.
    td = assemble_task_decision("do a DFIR forensic root-cause investigation of this incident")
    roles = AgentTeamSelector().select(td)
    assert SpecialistRole.DFIR in roles
    assert SpecialistRole.VERIFIER in roles


def test_selector_no_verifier_for_plain_general_chat():
    td = assemble_task_decision("hello there, how are you today")
    assert AgentTeamSelector().select(td) == []  # fast path, no team at all


def test_selector_injected_registry_is_used():
    # Inject a registry stub that forces verification for GENERAL to prove the
    # selector actually consults the registry (not a hardcoded domain list).
    class _ForceVerify:
        def requires_verification_for_domain(self, domain):
            return True

    td = assemble_task_decision("plan and break down this multi-step roadmap into milestones")
    roles = AgentTeamSelector(registry=_ForceVerify()).select(td)
    assert SpecialistRole.VERIFIER in roles
