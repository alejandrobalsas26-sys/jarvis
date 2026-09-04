"""
core/mesh_verifier.py — V69 M64: ARGUS, the mesh verifier.

ARGUS distrusts everyone constructively. It checks that claims map to evidence,
that evidence exists at all, that commands actually succeeded, that targets were
in scope, that no authority was invented, that the task asked for was the task
done, and that known limitations are reported.

What ARGUS is NOT allowed to do is as load-bearing as what it does:

  * it **cannot grant authority** — :attr:`VerifierVerdict.grants_authority` is a
    property that always returns ``False``, and no function here constructs a
    scope, a capability or an autonomy lift;
  * it **cannot rewrite evidence** — it reads the :class:`EvidenceGraph` and may
    only promote a claim through ``mark_verified``, which itself requires
    corroborating evidence already bound to that claim;
  * it **cannot execute** — there is no executor, tool broker or action path in
    this module.

The core of the verification is DETERMINISTIC. A model pass may add nuance
elsewhere in the runtime (``core.verification.verify_answer`` already does), but
the verdict returned here is computed from typed facts: which claims are bound to
corroborating evidence, which tool outcomes are citable, which scope decisions
denied, which handoffs stayed within depth. A verifier that can be talked out of
its verdict has verified nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.cognitive_mesh import REGISTRY, AutonomyLevel, MeshBudget, SpecialistId, permits
from core.mesh_contracts import (
    ActionDisposition,
    ActionRequest,
    ClaimStatus,
    EvidenceGraph,
    ResultStatus,
    SpecialistResult,
    ToolCallStatus,
    Verdict,
    VerifierVerdict,
)
from core.security_scope import SecurityScopeDecision


@dataclass
class VerificationInput:
    """Everything ARGUS is allowed to look at. Assembled by the orchestrator so
    the verifier reads facts rather than re-running the investigation."""

    task_id: str
    objective: str
    graph: EvidenceGraph
    results: tuple[SpecialistResult, ...] = ()
    action_requests: tuple[ActionRequest, ...] = ()
    scope_decisions: tuple[SecurityScopeDecision, ...] = ()
    handoff_depths: tuple[int, ...] = ()
    budget: MeshBudget = field(default_factory=MeshBudget)
    required_evidence: tuple[str, ...] = ()
    autonomy_ceiling: AutonomyLevel = AutonomyLevel.ADVISE
    retries_used: int = 0
    #: V69 M65C §46 — effects the durable journal left INDETERMINATE: a previous
    #: attempt may or may not have taken effect and nothing local can say which.
    #: Assembled by the caller from tool receipts, so ARGUS reads a fact rather
    #: than re-deriving one. Each entry is a body-safe "tool: reason" string.
    indeterminate_effects: tuple[str, ...] = ()


def verify(inp: VerificationInput) -> VerifierVerdict:
    """Compute the verdict (§31, §32). Deterministic; fail-closed.

    Checks run in severity order and the first hard failure wins, so an operator
    is told the most serious thing that is wrong rather than the first thing
    noticed. A missing input never reads as a pass: an empty task with no results
    is FAILED, not VERIFIED.
    """
    reasons: list[str] = []
    limitations: list[str] = []
    scope_violations: list[str] = []

    # ── 0. there has to be something to verify ───────────────────────────────
    if not inp.results:
        return VerifierVerdict(
            Verdict.FAILED,
            reasons=("no specialist returned a result; there is nothing to verify",),
            retries_used=inp.retries_used)

    # ── 1. scope. A denial that was acted on anyway is the worst finding. ────
    for decision in inp.scope_decisions:
        if not decision.allowed:
            scope_violations.append(
                f"{decision.activity} against {decision.target or '(no target)'} "
                f"denied: {decision.denial.value if decision.denial else 'refused'}")
    acted_after_denial = _acted_after_denial(inp)
    if acted_after_denial:
        return VerifierVerdict(
            Verdict.SCOPE_VIOLATION,
            reasons=tuple(f"an out-of-scope step produced a result: {a}"
                          for a in acted_after_denial),
            scope_violations=tuple(scope_violations), retries_used=inp.retries_used)

    # ── 2. authority. Nothing may claim an approval it does not have. ────────
    invented = _invented_authority(inp)
    if invented:
        return VerifierVerdict(
            Verdict.AUTHORITY_MISSING, reasons=tuple(invented),
            scope_violations=tuple(scope_violations), retries_used=inp.retries_used)

    # ── 2b. effects whose outcome is unknown. ────────────────────────────────
    # V69 M65C §46. ARGUS may not turn INDETERMINATE into VERIFIED. An effect
    # the journal could not resolve is not a failure — the action may well have
    # succeeded — but it is not a demonstrated success either, and only evidence
    # from the external system can settle it. Ranked above fabricated results
    # because an unresolved irreversible action is the more urgent thing to say.
    if inp.indeterminate_effects:
        return VerifierVerdict(
            Verdict.INSUFFICIENT_EVIDENCE,
            reasons=tuple(
                f"the outcome of {entry} could not be determined; it may or may "
                f"not have taken effect and was NOT retried"
                for entry in inp.indeterminate_effects[:4]
            ) + ("an effect with an unknown outcome cannot support a claim that "
                 "the task completed; it needs reconciliation against the "
                 "external system",),
            scope_violations=tuple(scope_violations),
            limitations=tuple(limitations), retries_used=inp.retries_used)

    # ── 3. fabricated tool results. ──────────────────────────────────────────
    fabricated = _fabricated_tool_results(inp)
    if fabricated:
        return VerifierVerdict(
            Verdict.FAILED,
            reasons=tuple(fabricated) + ("a tool result that did not happen cannot "
                                         "support a conclusion",),
            scope_violations=tuple(scope_violations), retries_used=inp.retries_used)

    # ── 4. unresolved disagreement. ──────────────────────────────────────────
    disputes = inp.graph.disputed()
    if disputes:
        for a, b in disputes:
            inp.graph.mark_disputed(a.claim_id)
            inp.graph.mark_disputed(b.claim_id)
        return VerifierVerdict(
            Verdict.CONFLICT_UNRESOLVED,
            reasons=tuple(
                f"{a.author.value} asserts '{a.statement}' and {b.author.value} "
                f"asserts '{b.statement}'; the evidence does not settle it"
                for a, b in disputes[:4]),
            scope_violations=tuple(scope_violations), retries_used=inp.retries_used)

    # ── 5. evidence. A high-impact conclusion needs something under it. ──────
    unsupported = inp.graph.unsupported_high_impact()
    if unsupported:
        return VerifierVerdict(
            Verdict.INSUFFICIENT_EVIDENCE,
            reasons=("a high-impact conclusion rests on no corroborating evidence",),
            unsupported_claims=tuple(c.statement for c in unsupported[:6]),
            scope_violations=tuple(scope_violations), retries_used=inp.retries_used)

    for requirement in inp.required_evidence:
        if not _requirement_met(inp, requirement):
            limitations.append(f"required evidence not demonstrated: {requirement}")

    # ── 6. completion. Was the task actually done? ───────────────────────────
    incomplete = [r for r in inp.results
                  if r.status in (ResultStatus.PARTIAL, ResultStatus.BLOCKED,
                                  ResultStatus.FAILED, ResultStatus.REFUSED)]
    for result in incomplete:
        limitations.append(
            f"{result.specialist_id.value} returned {result.status.value}"
            + (f": {result.limitations[0]}" if result.limitations else ""))

    # ── 7. budgets. An exhausted budget is a limitation, never a silent stop. ─
    if inp.handoff_depths and max(inp.handoff_depths) > inp.budget.max_handoff_depth:
        limitations.append(
            f"handoff depth {max(inp.handoff_depths)} exceeded the budget of "
            f"{inp.budget.max_handoff_depth}")
    if inp.retries_used > inp.budget.max_verifier_retries:
        limitations.append(
            f"verifier retries {inp.retries_used} exceeded "
            f"{inp.budget.max_verifier_retries}; the last verdict stands")

    # ── 8. promote what the evidence supports. ───────────────────────────────
    promoted = 0
    for claim in inp.graph.claims():
        if claim.status in (ClaimStatus.OBSERVED, ClaimStatus.INFERRED):
            if inp.graph.mark_verified(claim.claim_id, by=SpecialistId.ARGUS):
                promoted += 1
    reasons.append(f"{promoted} claim(s) promoted to VERIFIED against "
                   f"{inp.graph.corroborated_evidence_count} corroborating reference(s)")

    if inp.graph.corroborated_evidence_count == 0 and _needs_evidence(inp):
        return VerifierVerdict(
            Verdict.INSUFFICIENT_EVIDENCE,
            reasons=("this task's specialists require evidence and none of the "
                     "references collected is corroborating",),
            scope_violations=tuple(scope_violations),
            limitations=tuple(limitations), retries_used=inp.retries_used)

    if scope_violations:
        limitations.append(f"{len(scope_violations)} scope denial(s) were respected "
                           f"and the corresponding steps did not run")

    verdict = Verdict.VERIFIED_WITH_LIMITATIONS if limitations else Verdict.VERIFIED
    return VerifierVerdict(
        verdict, reasons=tuple(reasons), scope_violations=tuple(scope_violations),
        limitations=tuple(limitations), retries_used=inp.retries_used)


def _acted_after_denial(inp: VerificationInput) -> tuple[str, ...]:
    """Steps that produced a tool outcome for an activity the scope refused.

    A DENIED outcome is fine — that is the control working. What is not fine is a
    SUCCESS or PARTIAL outcome for a target a scope decision refused.
    """
    denied_targets = {d.target for d in inp.scope_decisions
                      if not d.allowed and d.target}
    if not denied_targets:
        return ()
    offenders: list[str] = []
    for result in inp.results:
        for outcome in result.tool_outcomes:
            if outcome.status not in (ToolCallStatus.SUCCESS, ToolCallStatus.PARTIAL):
                continue
            for target in denied_targets:
                if target and target in outcome.summary:
                    offenders.append(
                        f"{result.specialist_id.value} ran {outcome.tool} and "
                        f"reported output naming denied target {target}")
    return tuple(offenders)


def _invented_authority(inp: VerificationInput) -> tuple[str, ...]:
    """Action requests that claim an approval nothing granted.

    Two shapes are refused: an effectful request marked APPROVED_FOR_EXECUTOR
    (only a human decision reaches that state for HITL-class actions), and any
    request whose required autonomy exceeds the ceiling the route actually set.
    """
    problems: list[str] = []
    for request in inp.action_requests:
        if (request.required_autonomy >= AutonomyLevel.HITL_EXECUTE
                and request.disposition is ActionDisposition.APPROVED_FOR_EXECUTOR):
            problems.append(
                f"'{request.action}' needs human approval but is marked "
                f"APPROVED_FOR_EXECUTOR; no human decision is recorded")
        if not permits(inp.autonomy_ceiling, request.required_autonomy) and \
                request.disposition not in (
                    ActionDisposition.REFUSED_ABOVE_AUTONOMY,
                    ActionDisposition.REFUSED_OUT_OF_SCOPE,
                    ActionDisposition.REFUSED_NO_EVIDENCE,
                    ActionDisposition.REQUIRES_HUMAN_APPROVAL):
            problems.append(
                f"'{request.action}' needs L{int(request.required_autonomy)} under a "
                f"ceiling of L{int(inp.autonomy_ceiling)} and was not refused")
    return tuple(problems)


def _fabricated_tool_results(inp: VerificationInput) -> tuple[str, ...]:
    """Outcomes claiming success with nothing to show, and citations of
    non-citable outcomes."""
    problems: list[str] = []
    for result in inp.results:
        if result.hallucinated_tool_results:
            problems.append(
                f"{result.specialist_id.value} reported "
                f"{result.hallucinated_tool_results} SUCCESS tool outcome(s) with no "
                f"output")
        for outcome in result.tool_outcomes:
            if outcome.status in (ToolCallStatus.DENIED, ToolCallStatus.UNAVAILABLE) \
                    and outcome.summary.strip():
                problems.append(
                    f"{result.specialist_id.value} attached output to a "
                    f"{outcome.status.value} call to {outcome.tool}; a call that did "
                    f"not run produced nothing")
    for ref in inp.graph.all_evidence():
        if ref.tool_outcome is not None and not ref.tool_outcome.citable:
            problems.append(
                f"evidence {ref.ref_id} cites a {ref.tool_outcome.status.value} call "
                f"to {ref.tool_outcome.tool}")
    return tuple(problems)


def _needs_evidence(inp: VerificationInput) -> bool:
    return any(REGISTRY.get(r.specialist_id).evidence_policy.value == "evidence_required"
               for r in inp.results)


def _requirement_met(inp: VerificationInput, requirement: str) -> bool:
    """Whether a routing-declared evidence requirement was demonstrably met.

    Conservative by construction: a requirement it cannot check is reported as a
    LIMITATION rather than silently passed, so an unverifiable requirement makes
    the answer more cautious, never less.
    """
    text = requirement.lower()
    if "world state" in text:
        return any("world" in (ref.provenance.value + ref.source).lower()
                   for ref in inp.graph.all_evidence())
    if "cites a corroborating reference" in text:
        return inp.graph.corroborated_evidence_count > 0
    if "scope decision" in text:
        return bool(inp.scope_decisions)
    if "rollback" in text:
        return any(a.rollback_plan.strip() for a in inp.action_requests)
    if "severity and confidence" in text:
        return any("confidence" in f.lower() for r in inp.results for f in r.findings)
    if "preserved before" in text:
        return any("preserv" in f.lower() or "acquir" in f.lower()
                   for r in inp.results for f in r.findings)
    return False


def adjudicate(graph: EvidenceGraph) -> tuple[str, ...]:
    """Rank disagreeing claims by the QUALITY of the evidence under them (§33).

    ARGUS does not pick a winner and delete the loser. It reports which side has
    the better-supported claim and by how much, leaving both on the board. When
    the two are equally supported it says so, which is the honest answer and the
    one an operator can act on.
    """
    notes: list[str] = []
    for a, b in graph.disputed():
        sa = sum(1 for e in a.evidence_ids
                 if (r := graph.evidence(e)) is not None and r.corroborating)
        sb = sum(1 for e in b.evidence_ids
                 if (r := graph.evidence(e)) is not None and r.corroborating)
        if sa == sb:
            notes.append(
                f"UNRESOLVED: {a.author.value} and {b.author.value} disagree and both "
                f"rest on {sa} corroborating reference(s). Discriminating evidence is "
                f"needed; neither claim is withdrawn.")
        else:
            strong, weak, ns, nw = ((a, b, sa, sb) if sa > sb else (b, a, sb, sa))
            notes.append(
                f"BETTER SUPPORTED: {strong.author.value}'s claim rests on {ns} "
                f"corroborating reference(s) against {weak.author.value}'s {nw}. "
                f"Both remain on the board; the weaker is not deleted.")
    return tuple(notes)
