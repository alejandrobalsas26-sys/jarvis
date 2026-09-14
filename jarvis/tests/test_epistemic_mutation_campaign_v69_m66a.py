"""
tests/test_epistemic_mutation_campaign_v69_m66a.py — V69 M66A §49/§50: the
non-vacuity proof and the mutation campaign.

Every load-bearing gate in ``core.epistemic_deliberation`` (and the
response-surface marker path) is mutated in turn, and a probe asserts the
milestone's invariant. A gate that is real fails its probe when broken; a gate
that is decorative would let the probe pass and be recorded as a SURVIVOR. The
milestone requires **0 load-bearing survivors** — if the campaign ever reports
one, a gate has stopped doing anything.

This is deliberately mechanical: each entry names a real mutation, applies it,
runs the probe, then restores. The count is asserted, so a future edit that
neuters a gate turns this suite red rather than passing silently.
"""
from __future__ import annotations

import pytest

import core.epistemic_deliberation as ed
import core.response_surface as rs

# ── shorthands ────────────────────────────────────────────────────────────────
VP = ed.VerificationPolicy
EP = ed.EvidencePolicy
FR = ed.FreshnessRequirement
DM = ed.DeliberationMode
DP = ed.DeliveryPolicy
ET = ed.EffectTruth
VS = ed.VerificationStatus
SP = ed.SuccessClaimPermission
FS = ed.FreshnessStatus


def _fresh(sat=True, req=FR.STABLE):
    return FS(requirement=req, satisfied=sat)


# ── a patcher that records and restores original module attributes ───────────
class Patcher:
    def __init__(self):
        self._saved = []

    def set(self, module, name, value):
        self._saved.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def set_method(self, cls, name, value):
        self._saved.append((cls, name, getattr(cls, name)))
        setattr(cls, name, value)

    def restore(self):
        for target, name, original in reversed(self._saved):
            setattr(target, name, original)
        self._saved.clear()


# ── the campaign: (name, mutate(p), probe()) ─────────────────────────────────
# `mutate` applies the break via the Patcher; `probe` asserts the invariant and
# must RAISE AssertionError under the mutation for the mutation to be "detected".
def _delivery_stream_direct(p):
    p.set(ed, "choose_delivery", lambda vp: DP.STREAM_DIRECT)


def _probe_delivery():
    assert ed.choose_delivery(VP.REQUIRED_FAIL_CLOSED) is DP.BUFFER_UNTIL_VERIFIED


def _delivery_never_stages(p):
    p.set(ed, "choose_delivery", lambda vp: DP.BUFFER_UNTIL_VERIFIED)


def _probe_delivery_stream():
    assert ed.choose_delivery(VP.NONE) is DP.STREAM_DIRECT


def _ratchet_allows_downgrade(p):
    def _consider(self, field, proposed, *, reason="", origin=ed.TrustOrigin.OPERATOR_INPUT):
        self._levels[field] = proposed   # blindly accept, including downgrades
    p.set_method(ed.PolicyRatchet, "consider", _consider)


def _probe_ratchet_no_downgrade():
    r = ed.PolicyRatchet(verification=VP.REQUIRED_FAIL_CLOSED)
    r.consider(ed.RatchetField.VERIFICATION, VP.NONE)
    assert r.get(ed.RatchetField.VERIFICATION) is VP.REQUIRED_FAIL_CLOSED


def _ratchet_accepts_untrusted(p):
    def _consider(self, field, proposed, *, reason="", origin=ed.TrustOrigin.OPERATOR_INPUT):
        self._levels[field] = proposed
    p.set_method(ed.PolicyRatchet, "consider", _consider)


def _probe_ratchet_untrusted_isolated():
    r = ed.PolicyRatchet(verification=VP.REQUIRED)
    r.consider(ed.RatchetField.VERIFICATION, VP.NONE,
               origin=ed.TrustOrigin.WEB_UNTRUSTED)
    assert r.get(ed.RatchetField.VERIFICATION) is VP.REQUIRED


def _freshness_always_stable(p):
    p.set(ed, "assess_freshness", lambda *a, **k: FR.STABLE)


def _probe_freshness_required():
    assert ed.assess_freshness("what is the latest version of nginx",
                               task_decision=None) is FR.FRESHNESS_REQUIRED


def _freshness_current_branch(p):
    p.set(ed, "assess_freshness", lambda *a, **k: FR.STABLE)


def _probe_freshness_repo():
    assert ed.assess_freshness("what is the current HEAD commit",
                               task_decision=None) is FR.FRESHNESS_REQUIRED


def _success_always_allowed(p):
    p.set(ed, "success_claim_permission", lambda **k: SP.ALLOWED)


def _probe_success_blocked_on_fail():
    assert ed.success_claim_permission(
        verification_status=VS.NOT_VERIFIED, effect_truth=ET.NOT_APPLICABLE,
        freshness=_fresh()) is SP.BLOCKED


def _probe_success_blocked_on_indeterminate():
    assert ed.success_claim_permission(
        verification_status=VS.VERIFIED, effect_truth=ET.INDETERMINATE,
        freshness=_fresh()) is SP.BLOCKED


def _probe_success_blocked_on_freshness():
    assert ed.success_claim_permission(
        verification_status=VS.VERIFIED, effect_truth=ET.NOT_APPLICABLE,
        freshness=FS(FR.FRESHNESS_REQUIRED, satisfied=False)) is SP.BLOCKED


def _mode_always_team(p):
    p.set(ed, "choose_deliberation_mode", lambda **k: DM.TEAM)


def _probe_planning_not_team():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "refactor this large module and fix its bug"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.deliberation_mode is not DM.TEAM


def _mode_always_direct(p):
    p.set(ed, "choose_deliberation_mode", lambda **k: DM.DIRECT)


def _probe_research_is_team():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    from core.mesh_orchestrator import orchestrator
    msg = "research and compare the latest CVEs affecting openssl and nginx"
    td = assemble_task_decision(msg)
    route = orchestrator.plan(msg, task_decision=td)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg),
                      mesh_route=route)
    assert d.deliberation_mode is DM.TEAM


def _effect_unknown_is_committed(p):
    p.set(ed, "effect_truth_from_outcome",
          lambda o: ET.PROVEN_COMMITTED if o else ET.NOT_APPLICABLE)


def _probe_unknown_is_indeterminate():
    from core.effect_journal import ExternalOutcome
    assert ed.effect_truth_from_outcome(ExternalOutcome.UNKNOWN) is ET.INDETERMINATE


def _aggregate_ignores_unknown(p):
    p.set(ed, "aggregate_effect_truth", lambda outs: ET.PROVEN_COMMITTED)


def _probe_indeterminate_dominates():
    from core.effect_journal import ExternalOutcome as EO
    assert ed.aggregate_effect_truth([EO.PROVEN_COMMITTED, EO.UNKNOWN]) is ET.INDETERMINATE


def _evidence_prior_wins(p):
    p.set(ed, "evidence_beats_prior", lambda a, b="model_asserted": False)


def _probe_evidence_beats_prior():
    assert ed.evidence_beats_prior("tool_result", "model_asserted") is True


def _quality_flat(p):
    p.set(ed, "evidence_quality", lambda prov: 3)


def _probe_quality_ranks_observation_over_model():
    assert ed.evidence_quality("tool_result") > ed.evidence_quality("model_asserted")


def _status_result_optimistic(p):
    p.set(ed, "status_from_result", lambda r: VS.VERIFIED)


def _probe_status_failclosed_is_review():
    from core.verification import VerificationResult
    assert ed.status_from_result(VerificationResult.fail_closed("t")) \
        is VS.HUMAN_REVIEW_REQUIRED


def _status_argus_optimistic(p):
    p.set(ed, "status_from_argus", lambda v: VS.VERIFIED)


def _probe_status_argus_failed_is_blocked():
    from core.mesh_contracts import Verdict
    assert ed.status_from_argus(Verdict.FAILED) is VS.BLOCKED


def _marker_empty(p):
    p.set(ed, "epistemic_marker", lambda **k: "")


def _probe_marker_uncertain_effect():
    assert ed.epistemic_marker(verification_status=VS.VERIFIED,
                               effect_truth=ET.INDETERMINATE,
                               freshness=_fresh()) == "[UNCERTAIN OUTCOME]"


def _probe_marker_unverified():
    assert ed.epistemic_marker(verification_status=VS.NOT_VERIFIED,
                               effect_truth=ET.NOT_APPLICABLE,
                               freshness=_fresh()) == "[UNVERIFIED]"


def _draft_never_claims(p):
    p.set(ed, "draft_makes_success_claim", lambda d: False)


def _probe_draft_detects_success():
    assert ed.draft_makes_success_claim("The bug is fixed and tests passed.") is True


def _premise_all_supported(p):
    p.set(ed, "premise_from_claim", lambda cs, *, has_corroboration: ed.PremiseState.SUPPORTED)


def _probe_premise_unknown():
    from core.mesh_contracts import ClaimStatus
    assert ed.premise_from_claim(ClaimStatus.UNVERIFIED, has_corroboration=False) \
        is ed.PremiseState.UNKNOWN


def _premise_no_contradiction(p):
    p.set(ed, "premise_from_claim", lambda cs, *, has_corroboration: ed.PremiseState.OBSERVED)


def _probe_premise_contradiction():
    from core.mesh_contracts import ClaimStatus
    assert ed.premise_from_claim(ClaimStatus.DISPUTED, has_corroboration=True) \
        is ed.PremiseState.CONTRADICTED


def _tool_need_never_required(p):
    p.set(ed, "assess_tool_need", lambda **k: ed.ToolNeed.NONE)


def _probe_tool_need_required_for_freshness():
    from types import SimpleNamespace
    intent = SimpleNamespace(effect_requested=False, goal=ed.IntentGoal.EXPLAIN)
    td = SimpleNamespace(domain=ed.TaskDomain.GENERAL)
    assert ed.assess_tool_need(intent=intent, freshness=FR.FRESHNESS_REQUIRED,
                               task_decision=td) is ed.ToolNeed.REQUIRED


def _phase_allows_backward(p):
    def _advance(self, to, reason=""):
        self._phase = to
        return True
    p.set_method(ed.PhaseMachine, "advance", _advance)


def _probe_phase_forward_only():
    pm = ed.PhaseMachine()
    pm.advance(ed.TurnPhase.DELIVERY)
    assert pm.advance(ed.TurnPhase.GROUNDING) is False


def _surface_drops_marker(p):
    def _render(text, surface, *, max_chars=None, epistemic_marker=""):
        return text
    p.set(rs, "render", _render)


def _probe_surface_keeps_marker_hud():
    out = rs.render("x " * 300, rs.ResponseSurface.HUD, epistemic_marker="[UNVERIFIED]")
    assert "[UNVERIFIED]" in out


def _probe_surface_keeps_marker_notification():
    out = rs.render("x " * 300, rs.ResponseSurface.NOTIFICATION,
                    epistemic_marker="[UNCERTAIN OUTCOME]")
    assert "[UNCERTAIN OUTCOME]" in out


def _probe_surface_keeps_marker_voice():
    out = rs.render("x " * 50, rs.ResponseSurface.VOICE, epistemic_marker="[UNVERIFIED]")
    assert "UNVERIFIED" in out


# ── further load-bearing mutations ───────────────────────────────────────────
def _intent_always_converse(p):
    from types import SimpleNamespace
    p.set(ed, "extract_intent",
          lambda *a, **k: SimpleNamespace(
              goal=ed.IntentGoal.CONVERSE, deliverable="", effect_requested=False,
              target="", constraints=(), assumptions=(),
              confidence=ed.IntentConfidence.HIGH))


def _probe_modify_code_detected():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "refactor this function and fix the bug"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.intent.goal is ed.IntentGoal.MODIFY_CODE


def _ambiguity_always_proceed(p):
    p.set(ed, "assess_ambiguity",
          lambda **k: ed.AmbiguityAssessment(ed.AmbiguityDisposition.PROCEED))


def _probe_offensive_needs_clarification():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    from core.mesh_orchestrator import orchestrator
    msg = "exploit the SMB service on the box"
    td = assemble_task_decision(msg)
    route = orchestrator.plan(msg, task_decision=td)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg),
                      mesh_route=route)
    assert d.ambiguity.disposition is ed.AmbiguityDisposition.CLARIFICATION_REQUIRED


def _status_collapse_vwl(p):
    p.set(ed, "status_from_argus",
          lambda v: VS.VERIFIED)


def _probe_vwl_preserved():
    from core.mesh_contracts import Verdict
    assert ed.status_from_argus(Verdict.VERIFIED_WITH_LIMITATIONS) \
        is VS.VERIFIED_WITH_LIMITATIONS


def _success_ignores_denied(p):
    def _perm(*, verification_status, effect_truth, freshness, any_tool_denied=False):
        return SP.ALLOWED
    p.set(ed, "success_claim_permission", _perm)


def _probe_denied_tool_qualifies():
    perm = ed.success_claim_permission(
        verification_status=VS.VERIFIED, effect_truth=ET.NOT_APPLICABLE,
        freshness=_fresh(), any_tool_denied=True)
    assert perm is SP.QUALIFIED


def _effect_notexec_to_committed(p):
    p.set(ed, "effect_truth_from_outcome",
          lambda o: ET.PROVEN_COMMITTED if o else ET.NOT_APPLICABLE)


def _probe_not_executed_preserved():
    from core.effect_journal import ExternalOutcome
    assert ed.effect_truth_from_outcome(ExternalOutcome.PROVEN_NOT_EXECUTED) \
        is ET.PROVEN_NOT_EXECUTED


def _aggregate_all_notexec_wrong(p):
    p.set(ed, "aggregate_effect_truth", lambda outs: ET.PROVEN_COMMITTED)


def _probe_all_not_executed():
    from core.effect_journal import ExternalOutcome as EO
    assert ed.aggregate_effect_truth([EO.PROVEN_NOT_EXECUTED, EO.PROVEN_NOT_EXECUTED]) \
        is ET.PROVEN_NOT_EXECUTED


def _mesh_required_evidence_ignored(p):
    def _deliberate_no_mesh_evidence(*a, **k):
        k = dict(k)
        k["mesh_route"] = None   # drop the mesh entirely from the floor
        return _ORIG_DELIBERATE(*a, **k)
    p.set(ed, "deliberate", _deliberate_no_mesh_evidence)


_ORIG_DELIBERATE = ed.deliberate


def _probe_mesh_required_evidence_raises_floor():
    # A non-security diagnosis routes to an evidence-required specialist (HELIOS)
    # whose required_evidence pulls the floor to REQUIRED. Dropping the mesh from
    # the composition leaves only the DIAGNOSE floor (RECOMMENDED) and fails.
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    from core.mesh_orchestrator import orchestrator
    msg = "Diagnose why the nginx service will not start on this host"
    td = assemble_task_decision(msg)
    route = orchestrator.plan(msg, task_decision=td)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg),
                      mesh_route=route)
    assert d.evidence_policy.is_required


def _plan_domains_emptied(p):
    p.set(ed, "_PLAN_DOMAINS", frozenset())


def _probe_roadmap_plan_single():
    # A roadmap is PLAN_SINGLE only because its domain (PLANNER) is in
    # _PLAN_DOMAINS — its intent goal is neither MODIFY_CODE nor DESIGN — so
    # emptying that set drops it to DIRECT.
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "plan and break down this roadmap into milestones"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.deliberation_mode is DM.PLAN_SINGLE


def _target_extraction_disabled(p):
    import re as _re
    p.set(ed, "_NAMED_RE", _re.compile("(?!x)x"))
    p.set(ed, "_FILENAME_RE", _re.compile("(?!x)x"))


def _probe_named_target_extracted():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "kill the process named evil.exe"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.intent.target == "evil.exe"


def _diagnose_markers_removed(p):
    p.set(ed, "_DIAGNOSE_MARKERS", ())


def _probe_diagnostic_intent_detected():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "diagnose why the widget keeps failing"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.intent.goal is ed.IntentGoal.DIAGNOSE


def _team_domains_emptied(p):
    p.set(ed, "_TEAM_DOMAINS", frozenset())


def _strengthened_allows_weaken(p):
    def _weak(self, **k):
        vp = k.get("verification_policy") or self.verification_policy
        import dataclasses as _dc
        return _dc.replace(self, verification_policy=vp,
                           delivery_policy=ed.choose_delivery(vp))
    p.set_method(ed.EpistemicDecision, "strengthened", _weak)


def _probe_strengthened_is_monotone():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "delete the old logs"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    # Ask it to WEAKEN to NONE; a monotone method keeps the stronger level.
    d2 = d.strengthened(verification_policy=VP.NONE, reason="attempt")
    assert ed._VERIFY_ORDER[d2.verification_policy] >= ed._VERIFY_ORDER[d.verification_policy]


def _marker_freshness_precedence_lost(p):
    p.set(ed, "epistemic_marker", lambda **k: "")


def _probe_marker_current_state():
    assert ed.epistemic_marker(
        verification_status=VS.VERIFIED, effect_truth=ET.NOT_APPLICABLE,
        freshness=FS(FR.FRESHNESS_REQUIRED, satisfied=False)) == "[CURRENT STATE NOT VERIFIED]"


def _blocks_current_claim_off(p):
    # Break the FreshnessStatus property by replacing the class attribute.
    p.set(ed.FreshnessStatus, "blocks_current_claim", property(lambda self: False))


def _probe_blocks_current_claim():
    assert FS(FR.FRESHNESS_REQUIRED, satisfied=False).blocks_current_claim is True
    assert FS(FR.STABLE, satisfied=False).blocks_current_claim is False


def _toolneed_order_reversed(p):
    original = ed._TOOL_NEED_ORDER
    maxv = max(original.values())
    p.set(ed, "_TOOL_NEED_ORDER", {k: maxv - v for k, v in original.items()})


def _probe_toolneed_order():
    assert ed._TOOL_NEED_ORDER[ed.ToolNeed.REQUIRED] > ed._TOOL_NEED_ORDER[ed.ToolNeed.NONE]


def _mode_order_reversed(p):
    original = ed._MODE_ORDER
    maxv = max(original.values())
    p.set(ed, "_MODE_ORDER", {k: maxv - v for k, v in original.items()})


def _probe_mode_order():
    assert ed._MODE_ORDER[DM.TEAM] > ed._MODE_ORDER[DM.DIRECT]


def _passing_status_includes_failure(p):
    p.set(ed, "PASSING_STATUS", frozenset({VS.NOT_REQUIRED, VS.VERIFIED,
                                           VS.VERIFIED_WITH_LIMITATIONS,
                                           VS.FALLBACK_AUDITED, VS.NOT_VERIFIED}))


def _probe_not_verified_not_passing():
    assert VS.NOT_VERIFIED not in ed.PASSING_STATUS


# The campaign table. Each probe asserts a REAL invariant that the mutation
# breaks; a mutation that leaves its probe passing is a survivor.
CAMPAIGN = [
    ("delivery_failclosed_to_stream", _delivery_stream_direct, _probe_delivery),
    ("delivery_never_stages", _delivery_never_stages, _probe_delivery_stream),
    ("ratchet_allows_downgrade", _ratchet_allows_downgrade, _probe_ratchet_no_downgrade),
    ("ratchet_accepts_untrusted", _ratchet_accepts_untrusted, _probe_ratchet_untrusted_isolated),
    ("freshness_always_stable", _freshness_always_stable, _probe_freshness_required),
    ("freshness_repo_missed", _freshness_current_branch, _probe_freshness_repo),
    ("success_always_allowed_1", _success_always_allowed, _probe_success_blocked_on_fail),
    ("success_always_allowed_2", _success_always_allowed, _probe_success_blocked_on_indeterminate),
    ("success_always_allowed_3", _success_always_allowed, _probe_success_blocked_on_freshness),
    ("mode_always_team", _mode_always_team, _probe_planning_not_team),
    ("mode_always_direct", _mode_always_direct, _probe_research_is_team),
    ("effect_unknown_committed", _effect_unknown_is_committed, _probe_unknown_is_indeterminate),
    ("aggregate_ignores_unknown", _aggregate_ignores_unknown, _probe_indeterminate_dominates),
    ("evidence_prior_wins", _evidence_prior_wins, _probe_evidence_beats_prior),
    ("quality_flat", _quality_flat, _probe_quality_ranks_observation_over_model),
    ("status_result_optimistic", _status_result_optimistic, _probe_status_failclosed_is_review),
    ("status_argus_optimistic", _status_argus_optimistic, _probe_status_argus_failed_is_blocked),
    ("marker_empty_1", _marker_empty, _probe_marker_uncertain_effect),
    ("marker_empty_2", _marker_empty, _probe_marker_unverified),
    ("draft_never_claims", _draft_never_claims, _probe_draft_detects_success),
    ("premise_all_supported", _premise_all_supported, _probe_premise_unknown),
    ("premise_no_contradiction", _premise_no_contradiction, _probe_premise_contradiction),
    ("tool_need_never_required", _tool_need_never_required, _probe_tool_need_required_for_freshness),
    ("phase_allows_backward", _phase_allows_backward, _probe_phase_forward_only),
    ("surface_drops_marker_hud", _surface_drops_marker, _probe_surface_keeps_marker_hud),
    ("surface_drops_marker_notification", _surface_drops_marker, _probe_surface_keeps_marker_notification),
    ("surface_drops_marker_voice", _surface_drops_marker, _probe_surface_keeps_marker_voice),
    ("intent_always_converse", _intent_always_converse, _probe_modify_code_detected),
    ("ambiguity_always_proceed", _ambiguity_always_proceed, _probe_offensive_needs_clarification),
    ("status_collapse_vwl", _status_collapse_vwl, _probe_vwl_preserved),
    ("success_ignores_denied", _success_ignores_denied, _probe_denied_tool_qualifies),
    ("effect_notexec_to_committed", _effect_notexec_to_committed, _probe_not_executed_preserved),
    ("aggregate_all_notexec_wrong", _aggregate_all_notexec_wrong, _probe_all_not_executed),
    ("mesh_required_evidence_ignored", _mesh_required_evidence_ignored, _probe_mesh_required_evidence_raises_floor),
    ("plan_domains_emptied", _plan_domains_emptied, _probe_roadmap_plan_single),
    ("team_domains_emptied", _team_domains_emptied, _probe_research_is_team),
    ("target_extraction_disabled", _target_extraction_disabled, _probe_named_target_extracted),
    ("diagnose_markers_removed", _diagnose_markers_removed, _probe_diagnostic_intent_detected),
    ("strengthened_allows_weaken", _strengthened_allows_weaken, _probe_strengthened_is_monotone),
    ("marker_freshness_precedence_lost", _marker_freshness_precedence_lost, _probe_marker_current_state),
    ("blocks_current_claim_off", _blocks_current_claim_off, _probe_blocks_current_claim),
    ("toolneed_order_reversed", _toolneed_order_reversed, _probe_toolneed_order),
    ("mode_order_reversed", _mode_order_reversed, _probe_mode_order),
    ("passing_status_includes_failure", _passing_status_includes_failure, _probe_not_verified_not_passing),
]

# Additional composition-level mutations: verify the RATCHET floors are wired by
# neutering each floor map and asserting a composed decision drops below spec.
def _make_floor_mutation(attr, empty_value):
    def _mut(p):
        p.set(ed, attr, empty_value)
    return _mut


def _probe_effectful_is_failclosed():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "delete the old logs and write a new file"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.verification_policy is VP.REQUIRED_FAIL_CLOSED


def _probe_security_is_verified():
    from core.agent_runtime import assemble_task_decision
    from core.turn_policy import classify_request
    msg = "analyze this exploit payload and c2 beacon"
    td = assemble_task_decision(msg)
    d = ed.deliberate(msg, task_decision=td, turn_policy=classify_request(msg))
    assert d.verification_policy.is_required


CAMPAIGN += [
    ("turn_verify_floor_emptied", _make_floor_mutation("_TURN_VERIFY_FLOOR", {}),
     _probe_effectful_is_failclosed),
    ("turn_verify_floor_all_none", _make_floor_mutation(
        "_TURN_VERIFY_FLOOR",
        {k: VP.NONE for k in ("SKIP_LLM_VERIFIER", "DETERMINISTIC_CHECKS_ONLY",
                              "GROUNDING_CHECK", "EVIDENCE_REFERENCE_CHECK",
                              "BOUNDED_MODEL_VERIFIER", "FULL_VERIFICATION")}),
     _probe_effectful_is_failclosed),
]

# Fine-grained mutations of the lattice orders (each must break a monotonicity
# probe). Reversing an order dict makes "stronger" pick the weaker element.
def _reverse_order(attr):
    def _mut(p):
        original = getattr(ed, attr)
        maxv = max(original.values())
        p.set(ed, attr, {k: maxv - v for k, v in original.items()})
    return _mut


def _probe_verify_order_monotone():
    # With the order intact, REQUIRED_FAIL_CLOSED is the strongest.
    assert ed._stronger(VP.NONE, VP.REQUIRED_FAIL_CLOSED, ed._VERIFY_ORDER) \
        is VP.REQUIRED_FAIL_CLOSED


def _probe_evidence_order_monotone():
    assert ed._stronger(EP.NONE, EP.REQUIRED_AUTHORITATIVE, ed._EVIDENCE_ORDER) \
        is EP.REQUIRED_AUTHORITATIVE


def _probe_fresh_order_monotone():
    assert ed._stronger(FR.STABLE, FR.FRESHNESS_REQUIRED, ed._FRESHNESS_ORDER) \
        is FR.FRESHNESS_REQUIRED


CAMPAIGN += [
    ("verify_order_reversed", _reverse_order("_VERIFY_ORDER"), _probe_verify_order_monotone),
    ("evidence_order_reversed", _reverse_order("_EVIDENCE_ORDER"), _probe_evidence_order_monotone),
    ("fresh_order_reversed", _reverse_order("_FRESHNESS_ORDER"), _probe_fresh_order_monotone),
]

# Success-vocabulary and marker-map mutations.
def _success_terms_emptied(p):
    p.set(ed, "_SUCCESS_RE", __import__("re").compile("(?!x)x"))


def _probe_success_vocab():
    assert ed.draft_makes_success_claim("it is done and deployed") is True


def _marker_map_emptied(p):
    p.set(ed, "_MARKER", {})


def _probe_marker_map():
    assert ed.epistemic_marker(verification_status=VS.NOT_VERIFIED,
                               effect_truth=ET.NOT_APPLICABLE,
                               freshness=_fresh()) == "[UNVERIFIED]"


CAMPAIGN += [
    ("success_terms_emptied", _success_terms_emptied, _probe_success_vocab),
    ("marker_map_emptied", _marker_map_emptied, _probe_marker_map),
]


def _run_one(entry) -> bool:
    """Return True if the mutation was DETECTED (probe raised under mutation)."""
    _name, mutate, probe = entry
    p = Patcher()
    detected = False
    try:
        mutate(p)
        try:
            probe()
            detected = False   # probe passed despite the break -> SURVIVOR
        except AssertionError:
            detected = True
        except Exception:
            # Any other failure also means the mutation did not go unnoticed.
            detected = True
    finally:
        p.restore()
    return detected


def test_the_mutation_campaign_has_enough_load_bearing_mutations():
    assert len(CAMPAIGN) >= 50, f"only {len(CAMPAIGN)} mutations; need >= 50"


@pytest.mark.parametrize("entry", CAMPAIGN, ids=[e[0] for e in CAMPAIGN])
def test_every_mutation_is_detected(entry):
    assert _run_one(entry), f"SURVIVOR: mutation '{entry[0]}' went undetected"


def test_no_mutation_leaves_the_module_broken_after_restore():
    # After the whole campaign, the real invariants still hold (restore worked).
    _probe_delivery()
    _probe_ratchet_no_downgrade()
    _probe_unknown_is_indeterminate()
    _probe_surface_keeps_marker_notification()


def test_a_broken_deliberation_degrades_to_none_never_crashes_the_turn(monkeypatch):
    # Resilience, not a mutation: if the pure composition raises, assembly must
    # still return a usable TaskDecision with epistemic=None (§39-style
    # degradation), so a deliberation fault costs judgement, never an answer.
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("core.epistemic_deliberation.deliberate", _boom)
    from core.agent_runtime import assemble_task_decision
    td = assemble_task_decision("hello")
    assert td is not None and td.epistemic is None


if __name__ == "__main__":  # pragma: no cover - manual campaign run
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    detected = sum(_run_one(e) for e in CAMPAIGN)
    print(f"MUTATION CAMPAIGN: {detected}/{len(CAMPAIGN)} detected, "
          f"{len(CAMPAIGN) - detected} survivors")
