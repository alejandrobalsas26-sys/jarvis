"""reasoning_distillation/distiller.py — V69 M67A: extraction, and a recursion that must end.

THE PIPELINE §12 SPECIFIES
--------------------------
``SOURCE -> EXTRACT -> CRITIQUE -> REPAIR -> VERIFY AGAINST SOURCE -> ACCEPT / REVIEW / REJECT``

This module owns EXTRACT, REPAIR and the loop. CRITIQUE is
:mod:`reasoning_distillation.critic`, VERIFY is :mod:`reasoning_distillation.verifier`, and the
final disposition is :mod:`reasoning_distillation.quality`. They are separate modules because
they are separately versioned and separately falsifiable.

WHY EXTRACTION IS SUBTRACTIVE
-----------------------------
The deterministic extractor never composes prose. Every populated field is a SPAN LIFTED from a
source turn — a sentence, a bullet, a clause — chosen by a marker, and stored as it appeared.

That is a deliberate restriction and it costs recall: a constraint expressed across two
sentences will not be captured. The alternative costs correctness, and §12 is unambiguous about
which way to fail: *"never fill missing reasoning merely to make the schema look complete.
UNKNOWN > hallucination."* A generative extractor writes a field that reads better than the
source and cannot be attributed to any single turn — exactly what
:mod:`reasoning_distillation.verifier` would then flag as an orphan. Lifting spans means the
verifier's attribution is almost always available, because the field IS a turn's own words.

WHY THE RECURSION IS BOUNDED, AND HOW IT TERMINATES
---------------------------------------------------
An unbounded critique/repair loop does not converge on truth; it converges on whatever the
critic cannot see. §12 caps the depth at 3 by default and this module enforces termination
through three independent conditions, any one of which ends the loop:

  1. **no material findings remain** — the intended exit;
  2. **the repair produced a byte-identical artifact** — the repairer has nothing left to do, so
     another pass cannot help. Detected by comparing ``artifact_hash``, which is why that is a
     content address and not a counter;
  3. **the depth cap is reached** — and the record is then dispositioned
     ``NEEDS_HUMAN_REVIEW`` with ``RECURSION_EXHAUSTED``, never ACCEPT. A record that still has
     material findings after the last permitted repair has not been fixed, and letting the cap
     act as an acceptance would make the bound a rubber stamp.

Condition 2 matters more than it looks. Without it, a finding the repairer cannot act on — an
``operator_without_evidence`` the repairer chooses to keep — would burn every pass and every
record with one would exhaust the budget, making the depth cap the normal exit rather than the
exceptional one.

THE AUDIT TRAIL IS THE POINT
----------------------------
§12 requires each pass to persist artifact hash, parent hash, stage, findings, repair actions and
verification result, and that *the recursion itself must be auditable*. :class:`RecursionTrace`
is that record. It is body-free, it is a chain (each pass names its parent), and
:meth:`RecursionTrace.chain_intact` proves the chain has no gap — because a trace with a broken
parent link cannot show what a record was repaired FROM, which is the only thing that makes a
repair reviewable.

MODEL-ASSISTED EXTRACTION (§19)
-------------------------------
:class:`DistillationProvider` is the narrow hook. Two properties are enforced structurally
rather than requested:

  * **it cannot see the holdout.** :func:`distil_corpus` requires a
    :class:`~reasoning_distillation.holdout.HoldoutGuard` and calls
    ``assert_no_holdout_content`` before any provider is reachable.
  * **its output has no authority.** A provider returns SUGGESTIONS, which are applied only
    where the deterministic critic and verifier subsequently confirm them; its own critique
    findings are capped at ADVISORY. This is the same ceiling
    :mod:`training_gym.teachers.base` already places on a teacher's opinion, for the same
    reason: a persuasive, well-formed answer from a model is not evidence.

That module's :class:`~training_gym.teachers.base.TeacherProvider` is NOT subclassed, and the
reason is fit rather than preference: its ``_produce`` takes a teacher PACKET about a task
attempt and its records bind to four subjects from the training gym's episode model, none of
which exists here. Its standalone value types — ``TeacherKind``, ``TeacherAvailability``,
``ReviewMode`` — ARE reused, so availability and kind are declared in the vocabulary the
repository already audits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from training_gym.schemas import SchemaError, sha256_text
from training_gym.teachers.base import ReviewMode, TeacherAvailability, TeacherKind

from core.epistemic_deliberation import (
    DeliberationMode,
    FreshnessRequirement,
    IntentGoal,
    PremiseState,
    ToolExecutionState,
    ToolNeed,
    VerificationStatus,
)
from core.mesh_contracts import ClaimStatus, Provenance

from .config import DistillationConfig, default_config
from .critic import Critique, CriticFinding, critique
from .holdout import HoldoutGuard
from .models import (
    ASSISTANT_PROCESS_ROLES,
    CanonicalConversation,
    DecisionFrame,
    DistilledDecision,
    DistilledProvenance,
    EpistemicStateFrame,
    ExportStatus,
    FactProcedureLink,
    FactualClaim,
    FactualState,
    OutcomeFrame,
    PrivacyAssessment,
    TaskFrame,
    ToolDecisionFrame,
    TurnRole,
)
from .operators import KNOWN_OPERATOR_NAMES, ReasoningOperator, detect
from .verifier import SourceVerdict, SourceVerification, verify

#: Bump when extraction or repair behaviour changes for identical input.
DISTILLER_VERSION = "m67a.distiller.1"

#: Sentence-ish splitter. Deliberately crude: it must not merge two clauses across a full stop,
#: and it must not split a decimal or an ellipsis into fragments that then fail attribution.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[(])|\n+")

#: The longest span the extractor will lift into a single field.
_MAX_SPAN = 320


class DistillationRefused(SchemaError):
    """Extraction was refused. Never a partially-extracted record."""


# ══════════════════════════════════════════════════════════════════════════════════════════
#  §19 — the provider hook
# ══════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ProviderSuggestion:
    """One field a provider proposes. A SUGGESTION, never an assignment.

    Applied only when the deterministic critic and verifier subsequently accept the resulting
    record. ``basis`` is the provider's own stated reason and is stored for audit; it carries no
    weight in any decision, which is the whole point of the type being separate from
    :class:`~reasoning_distillation.models.DistilledDecision`.
    """

    field_path: str
    value: str
    basis: str = ""

    def to_dict(self) -> dict:
        return {"field_path": self.field_path, "value_digest": sha256_text(self.value),
                "value_chars": len(self.value), "basis": self.basis[:320]}


@runtime_checkable
class DistillationProvider(Protocol):
    """The narrowest model-assisted interface M67A needs (§19).

    A provider is asked for suggestions about ONE conversation and returns them. It is never
    handed a corpus, a split, a guard, a filesystem path, a credential or a holdout id — its
    whole input is the turns of one permitted conversation and its whole output is a list of
    suggestions plus advisory notes.
    """

    #: Stable identifier, recorded with every non-deterministic artifact.
    provider_id: str
    provider_kind: TeacherKind
    #: The resolved model identifier, never a role alias. Recorded in the manifest, because §19
    #: requires model/provider metadata for every non-deterministic stage.
    model: str

    def availability(self) -> TeacherAvailability:
        """Whether this provider can run HERE. ``available=False`` is a first-class answer."""
        ...

    def suggest(self, *, turns: "tuple[tuple[str, str], ...]", mode: ReviewMode
                ) -> "tuple[ProviderSuggestion, ...]":
        """Propose field values from ``(role, text)`` pairs. Must not raise on empty input."""
        ...


# ══════════════════════════════════════════════════════════════════════════════════════════
#  EXTRACT — subtractive, deterministic, offline
# ══════════════════════════════════════════════════════════════════════════════════════════
def _spans(text: str) -> tuple[str, ...]:
    """Source spans, trimmed and bounded. Never composed, only cut."""
    out: list[str] = []
    for piece in _SENTENCE.split(text or ""):
        cleaned = " ".join(piece.split()).strip(" -*•\t")
        if cleaned:
            out.append(cleaned[:_MAX_SPAN])
    return tuple(out)


def _lift(text: str, markers: tuple[str, ...], *, limit: int = 8) -> tuple[str, ...]:
    """Every span containing one of *markers*, deduplicated, source order preserved."""
    lowered = tuple(m.lower() for m in markers)
    seen: set[str] = set()
    out: list[str] = []
    for span in _spans(text):
        low = span.lower()
        if any(m in low for m in lowered) and span not in seen:
            seen.add(span)
            out.append(span)
            if len(out) >= limit:
                break
    return tuple(out)


_CONSTRAINT_MARKERS = ("must ", "must not", "cannot", "can't", "without ", "only ", "never ",
                       "required", "requirement", "has to", "constraint", "preserve",
                       "no more than", "at most", "at least", "debe ", "sin ", "solo ")
_ASSUMPTION_MARKERS = ("assum", "suppose", "if we take", "presum", "supongo", "asumo")
_UNCERTAIN_MARKERS = ("not sure", "unsure", "unclear", "i don't know", "i do not know",
                      "uncertain", "ambiguous", "cannot tell", "might be", "no estoy seguro",
                      "no sé", "ambiguo")
_UNKNOWN_MARKERS = ("i don't know", "i do not know", "unknown", "not stated", "not shown",
                    "did not say", "no information", "no se indica", "desconocido")
_ALTERNATIVE_MARKERS = ("option", "alternatively", "another approach", "could also", "one way",
                        "other way", "opción", "alternativa", "otra forma")
_REJECTED_MARKERS = ("will not work", "won't work", "rejected", "ruled out", "too slow",
                     "not suitable", "bad idea", "no funcionará", "descartado")
_STOP_MARKERS = ("done when", "stop when", "sufficient", "enough to", "that completes",
                 "no further", "terminado cuando", "suficiente")
_VERIFY_MARKERS = ("verify", "confirm", "test that", "check that", "run the test", "make sure",
                   "verificar", "confirmar", "comprobar")
_SUBPROBLEM_MARKERS = ("first", "second", "third", "step ", "part ", "then ", "next ",
                       "primero", "segundo", "paso ", "luego")
_CORRECTION_MARKERS = ("i was wrong", "correction", "actually", "mistake", "let me correct",
                       "that was incorrect", "me equivoqué", "corrección")
_STALE_MARKERS = ("may have changed", "might have changed", "as of my", "knowledge cutoff",
                  "out of date", "outdated", "puede haber cambiado", "desactualizado")
_FRESHNESS_REQUIRED = ("latest", "current", "currently", "right now", "today", "newest",
                       "most recent", "actual", "ahora", "hoy")
_PREMISE_FALSE = ("that is not correct", "that's not correct", "false premise", "incorrect",
                  "is not true", "no es cierto", "premise")
_EFFECT_MARKERS = ("delete", "remove", "deploy", "push", "install", "write to", "overwrite",
                   "borrar", "eliminar", "desplegar")


def _goal_of(conversation: CanonicalConversation) -> IntentGoal:
    """Infer the intent goal from the operator's OWN words, conservatively.

    Defaults to :attr:`IntentGoal.EXPLAIN` because that is the least committal member that still
    describes a request for information. It is NOT ``CONVERSE``: a historical export exists
    because somebody asked something, and classifying that as chat would exclude it from every
    downstream comparison.
    """
    user = " ".join(t.text for t in conversation.turns
                    if t.role is TurnRole.USER_REQUEST).lower()
    if any(m in user for m in _EFFECT_MARKERS):
        return IntentGoal.EXECUTE_EFFECT
    if any(m in user for m in ("fix", "refactor", "implement", "bug", "patch", "arregla",
                               "implementa")):
        return IntentGoal.MODIFY_CODE
    if any(m in user for m in ("why does", "why is", "failing", "error", "traceback",
                               "diagnose", "por qué", "falla")):
        return IntentGoal.DIAGNOSE
    if any(m in user for m in ("design", "architecture", "should we use", "diseña",
                              "arquitectura")):
        return IntentGoal.DESIGN
    if any(m in user for m in ("summarize", "summary", "tl;dr", "resume", "resumen")):
        return IntentGoal.SUMMARIZE
    if any(m in user for m in ("research", "compare", "find out", "investiga", "compara")):
        return IntentGoal.RESEARCH
    return IntentGoal.EXPLAIN


def extract(conversation: CanonicalConversation, *,
            privacy: PrivacyAssessment | None = None) -> DistilledDecision:
    """Build a distilled record by LIFTING spans from *conversation*. Composes nothing.

    Every populated field is a span from a source turn. A field with no matching span stays
    empty, which is the honest representation of "the trace did not do this visibly" — and is
    what makes the record falsifiable, because an empty field cannot be wrong about the source.
    """
    if not conversation.family_id:
        raise DistillationRefused(
            f"{conversation.conversation_id}: refusing to distil an ungrouped conversation; "
            f"without a family it cannot be placed on the correct side of the firewall (§9)")

    # A turn carrying secret-like material is EXCLUDED from span lifting.
    #
    # This is not belt-and-braces; it closes a measured leak. Extraction is subtractive — every
    # populated field is a span lifted verbatim from a turn — so a conversation containing an API
    # key produces a record containing that API key, in `task.user_goal_summary` and anywhere else
    # the span was chosen. Measured on a synthetic fixture: `persist` refused to write the record
    # because its payload carried `['home_path', 'secret']`.
    #
    # The refusal was correct and insufficient. A credential must not be COPIED into a second file
    # on disk at all, however well that file is gitignored: the corpus root's protection is that
    # raw material lives in one place, and a derived record that duplicates a key defeats it. So
    # the exclusion happens here, at the point of lifting, rather than at the point of writing.
    #
    # The turn itself is not destroyed (§13) and its privacy finding still travels into the
    # record's own assessment, so the record remains attributable to a source that a human can
    # lawfully read locally — it simply does not restate the secret.
    def _liftable(turn: object) -> bool:
        return not getattr(turn, "privacy").contains_secret_like_data

    excluded = tuple(t.provenance.unit_id for t in conversation.turns if not _liftable(t))

    user_text = "\n".join(t.text for t in conversation.turns
                          if t.role is TurnRole.USER_REQUEST and _liftable(t))
    process_text = "\n".join(t.text for t in conversation.turns
                             if t.role in ASSISTANT_PROCESS_ROLES and not t.is_meta_noise
                             and _liftable(t))
    answer_text = "\n".join(t.text for t in conversation.turns
                            if t.role is TurnRole.ASSISTANT_ANSWER and _liftable(t))
    decision_text = "\n".join(p for p in (process_text, answer_text) if p)
    has_tool_turn = any(t.role in (TurnRole.TOOL_REQUEST, TurnRole.TOOL_RESULT)
                        for t in conversation.turns)

    operators = detect(decision_text)
    goal = _goal_of(conversation)

    # Freshness: from the operator's request (what the answer's truth depends on) OR from the
    # assistant noticing staleness. Either is evidence the turn was time-sensitive.
    lowered_user = user_text.lower()
    lowered_decision = decision_text.lower()
    if any(m in lowered_user for m in _FRESHNESS_REQUIRED):
        freshness = FreshnessRequirement.FRESHNESS_REQUIRED
    elif any(m in lowered_decision for m in _STALE_MARKERS):
        freshness = FreshnessRequirement.FRESHNESS_PREFERRED
    else:
        freshness = FreshnessRequirement.STABLE

    premise = (PremiseState.CONTRADICTED
               if any(m in lowered_decision for m in _PREMISE_FALSE)
               else PremiseState.UNKNOWN)

    # Tool need: from what the trace CONCLUDED, not from what was available (§11).
    if ReasoningOperator.TOOL_NEEDED in operators:
        need = ToolNeed.REQUIRED
    elif ReasoningOperator.TOOL_NOT_NEEDED in operators:
        need = ToolNeed.NONE
    elif ReasoningOperator.RETRIEVAL_DECISION in operators:
        need = ToolNeed.OPTIONAL
    else:
        need = ToolNeed.NONE
    # Execution is NEVER inferred from need. A tool turn witnesses execution; nothing else does.
    execution = (ToolExecutionState.SUCCEEDED if has_tool_turn
                 else ToolExecutionState.NOT_ATTEMPTED)

    verification_plan = _lift(decision_text, _VERIFY_MARKERS)
    # A stated plan is NOT a verified verdict. The strongest status extraction may assign is
    # NOT_VERIFIED-with-a-plan; VERIFIED requires evidence this corpus does not have.
    verification_status = (VerificationStatus.NOT_VERIFIED if verification_plan
                          else VerificationStatus.NOT_REQUIRED)

    deliberation = (DeliberationMode.PLAN_SINGLE
                    if ReasoningOperator.DECOMPOSITION in operators
                    or ReasoningOperator.PLAN_FORMATION in operators
                    else DeliberationMode.SINGLE_ANALYSIS if process_text
                    else DeliberationMode.DIRECT)

    # Factual claims: a claim is recorded only where the trace itself flagged staleness. M67A
    # does not enumerate every assertion — it cannot verify them (§10) — so it records the ones
    # the trace marked and leaves the rest untouched rather than mass-labelling them SOURCE_ONLY
    # and implying a review that never happened.
    claims: list[FactualClaim] = []
    links: list[FactProcedureLink] = []
    for span in _lift(decision_text, _STALE_MARKERS, limit=4):
        digest = sha256_text(span)
        claims.append(FactualClaim(
            statement_digest=digest, state=FactualState.STALE,
            claim_provenance=Provenance.MODEL_ASSERTED, claim_status=ClaimStatus.UNVERIFIED,
            topic="self-flagged staleness", freshness_sensitive=True,
            note="the trace itself marked this as possibly out of date"))
        links.append(FactProcedureLink(
            claim_digest=digest, procedure_depends_on_claim=None,
            basis="undecided by the deterministic extractor: whether the procedure depends on "
                  "this claim is a reading of the source, and guessing it would import a "
                  "fact-dependent plan as a fact-independent one (§10)"))

    # observed_success stays UNKNOWN unless a later USER turn says so. A follow-up that says
    # "that worked" is the only success evidence a historical export ever carries, and inventing
    # one would manufacture exactly the labels §26 forbids claiming.
    later_user = " ".join(t.text for t in conversation.turns[1:]
                          if t.role in (TurnRole.USER_REQUEST, TurnRole.FOLLOW_UP)).lower()
    if any(m in later_user for m in ("that worked", "works now", "fixed it", "thanks, that",
                                     "funcionó", "ya funciona")):
        observed_success: bool | None = True
    elif any(m in later_user for m in ("still fails", "did not work", "didn't work",
                                       "still broken", "sigue fallando")):
        observed_success = False
    else:
        observed_success = None

    record = DistilledDecision(
        example_id=f"m67a-{conversation.digest[:24]}",
        provenance=DistilledProvenance(
            conversation_id=conversation.conversation_id,
            family_id=conversation.family_id,
            source_file_hashes=(conversation.source_file_hash,),
            source_unit_ids=tuple(t.provenance.unit_id for t in conversation.turns),
            source_model=conversation.source_model,
            source_date=conversation.source_date,
            source_type=conversation.source_type),
        task=TaskFrame(
            domain="",  # not inferable from a historical export without guessing
            goal=goal,
            user_goal_summary=(_spans(user_text)[0] if _spans(user_text) else ""),
            constraints=_lift(user_text, _CONSTRAINT_MARKERS),
            context_dependencies=()),
        epistemic_state=EpistemicStateFrame(
            known=(),
            unknown=_lift(decision_text, _UNKNOWN_MARKERS, limit=6),
            assumptions=_lift(decision_text, _ASSUMPTION_MARKERS, limit=6),
            uncertainties=_lift(decision_text, _UNCERTAIN_MARKERS, limit=6),
            freshness=freshness,
            premise_state=premise),
        decision=DecisionFrame(
            reasoning_operators=tuple(op.value for op in operators),
            subproblems=_lift(process_text, _SUBPROBLEM_MARKERS, limit=8),
            candidate_approaches=_lift(decision_text, _ALTERNATIVE_MARKERS, limit=6),
            rejected_approaches=_lift(decision_text, _REJECTED_MARKERS, limit=6),
            chosen_approach=(_spans(answer_text)[0] if _spans(answer_text) else ""),
            decision_basis=(_spans(process_text)[-1] if _spans(process_text) else ""),
            tool_decision=ToolDecisionFrame(
                need=need, execution=execution,
                basis=(_lift(decision_text, ("need to check", "no need to", "look up",
                                             "necesito comprobar"), limit=1) or ("",))[0]),
            verification_plan=verification_plan,
            verification_status=verification_status,
            stop_condition=(_lift(decision_text, _STOP_MARKERS, limit=1) or ("",))[0],
            deliberation_mode=deliberation),
        outcome=OutcomeFrame(
            final_answer_ref=sha256_text(answer_text) if answer_text else "",
            self_corrections=_lift(decision_text, _CORRECTION_MARKERS, limit=4),
            observed_success=observed_success,
            # A label, not a body: names how many turns were withheld from lifting so the record's
            # thinness is attributable to the exclusion rather than to a weak source.
            quality_labels=((f"secret_bearing_turns_excluded_from_lifting:{len(excluded)}",)
                            if excluded else ())),
        privacy=privacy or _derive_privacy(conversation),
        factual_claims=tuple(claims),
        fact_procedure_links=tuple(links),
        stage="extract",
        depth=0,
    )
    return record.validated(known_operators=KNOWN_OPERATOR_NAMES)


def _derive_privacy(conversation: CanonicalConversation) -> PrivacyAssessment:
    """The record's privacy status, derived from its source and never weaker than it (§4).

    Aggregates the turns' own assessments: any secret-like or personal finding anywhere in the
    conversation carries into the record, and the export status is the conversation's weakest.
    A derived record cannot be safer than what it derives from, and the critic enforces that
    independently — this is where the correct value comes from, not the only place it is checked.
    """
    personal = any(t.privacy.contains_personal_data for t in conversation.turns)
    secret = any(t.privacy.contains_secret_like_data for t in conversation.turns)
    scanner_ok = all(t.privacy.scanner_available for t in conversation.turns)
    categories = tuple(sorted({c for t in conversation.turns for c in t.privacy.categories}
                              - {"none"}))
    sensitivity = min((t.privacy.sensitivity for t in conversation.turns),
                      key=lambda s: (s.dataset_eligible, s.exportable_to_teacher),
                      default=None)
    status = conversation.export_status
    if status is ExportStatus.EXPORT_SAFE and (personal or secret or not scanner_ok):
        status = ExportStatus.EXPORT_BLOCKED
    first = conversation.turns[0].privacy if conversation.turns else None
    return PrivacyAssessment(
        contains_personal_data=personal,
        contains_secret_like_data=secret,
        export_status=status,
        categories=categories or ("none",),
        scanner_available=scanner_ok,
        sensitivity=sensitivity if sensitivity is not None
        else (first.sensitivity if first else PrivacyAssessment(
            False, False, ExportStatus.EXPORT_UNKNOWN).sensitivity),
        note="derived from the source conversation's turn assessments",
    ).validated()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  REPAIR — removal and marking only
# ══════════════════════════════════════════════════════════════════════════════════════════
#: What a repair pass is permitted to do. A closed list, because "repair" is the step most
#: tempted to improve the record: an ADD action would let the repairer write the field the critic
#: said was unsupported, which is how a critique loop launders a fabrication into an accepted
#: record. Every action here either DELETES an unsupported value or WEAKENS a claim.
REPAIR_ACTIONS: tuple[str, ...] = (
    "drop_unsupported_element",
    "clear_unsupported_field",
    "downgrade_verification_status",
    "reset_tool_execution",
    "drop_unevidenced_operator",
    "clear_invented_decision",
)


@dataclass(frozen=True)
class RepairAction:
    """One thing a repair pass did. Body-free: names the field, never the removed value."""

    action: str
    field_path: str
    index: int = -1
    reason: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "field_path": self.field_path, "index": self.index,
                "reason": self.reason[:320]}


def _drop_index(values: tuple[str, ...], index: int) -> tuple[str, ...]:
    return tuple(v for i, v in enumerate(values) if i != index)


def repair(record: DistilledDecision, findings: tuple[CriticFinding, ...]
           ) -> tuple[DistilledDecision, tuple[RepairAction, ...]]:
    """Apply every repairable finding. Subtractive only — see :data:`REPAIR_ACTIONS`.

    Returns the new record and what was done. Findings are applied in DESCENDING index order per
    field so that dropping element 2 does not renumber element 5 out from under a later action —
    an off-by-one here would delete the wrong element and the audit trail would say otherwise.
    """
    task, epi, dec, out = record.task, record.epistemic_state, record.decision, record.outcome
    actions: list[RepairAction] = []

    by_field: dict[str, list[CriticFinding]] = {}
    for finding in findings:
        by_field.setdefault(finding.field_path, []).append(finding)
    for values in by_field.values():
        values.sort(key=lambda f: -f.index)

    list_targets: dict[str, tuple[object, str]] = {
        "task.constraints": (task, "constraints"),
        "task.context_dependencies": (task, "context_dependencies"),
        "epistemic_state.known": (epi, "known"),
        "epistemic_state.unknown": (epi, "unknown"),
        "epistemic_state.assumptions": (epi, "assumptions"),
        "epistemic_state.uncertainties": (epi, "uncertainties"),
        "decision.subproblems": (dec, "subproblems"),
        "decision.candidate_approaches": (dec, "candidate_approaches"),
        "decision.rejected_approaches": (dec, "rejected_approaches"),
        "decision.verification_plan": (dec, "verification_plan"),
        "outcome.self_corrections": (out, "self_corrections"),
    }
    scalar_targets: dict[str, tuple[object, str]] = {
        "task.user_goal_summary": (task, "user_goal_summary"),
        "decision.chosen_approach": (dec, "chosen_approach"),
        "decision.decision_basis": (dec, "decision_basis"),
        "decision.stop_condition": (dec, "stop_condition"),
    }
    frames = {"task": task, "epistemic_state": epi, "decision": dec, "outcome": out}

    for path, group in sorted(by_field.items()):
        for finding in group:
            if finding.check == "unsupported_text" and path in list_targets:
                holder, attr = list_targets[path]
                current = getattr(frames[path.split(".")[0]], attr)
                if 0 <= finding.index < len(current):
                    frames[path.split(".")[0]] = replace(
                        frames[path.split(".")[0]],
                        **{attr: _drop_index(current, finding.index)})
                    actions.append(RepairAction(
                        "drop_unsupported_element", path, finding.index,
                        "element was not grounded in the source"))
                del holder
            elif finding.check == "unsupported_text" and path in scalar_targets:
                _, attr = scalar_targets[path]
                root = path.split(".")[0]
                if getattr(frames[root], attr):
                    frames[root] = replace(frames[root], **{attr: ""})
                    actions.append(RepairAction(
                        "clear_unsupported_field", path, -1,
                        "field was not grounded in the source; UNKNOWN beats unsupported text"))
            elif finding.check == "unsupported_text" and path == "decision.tool_decision.basis":
                frames["decision"] = replace(
                    frames["decision"],
                    tool_decision=replace(frames["decision"].tool_decision, basis=""))
                actions.append(RepairAction("clear_unsupported_field", path, -1,
                                            "tool basis was not grounded in the source"))
            elif finding.check == "operator_without_evidence":
                current = frames["decision"].reasoning_operators
                if 0 <= finding.index < len(current):
                    frames["decision"] = replace(
                        frames["decision"],
                        reasoning_operators=_drop_index(current, finding.index))
                    actions.append(RepairAction(
                        "drop_unevidenced_operator", path, finding.index,
                        "no marker for this operator in the assistant's own turns"))
            elif finding.check == "verified_without_plan":
                frames["decision"] = replace(
                    frames["decision"], verification_status=VerificationStatus.NOT_VERIFIED)
                actions.append(RepairAction(
                    "downgrade_verification_status", path, -1,
                    "a verdict with no stated check behind it is NOT_VERIFIED"))
            elif finding.check == "tool_execution_unwitnessed":
                frames["decision"] = replace(
                    frames["decision"],
                    tool_decision=replace(frames["decision"].tool_decision,
                                          execution=ToolExecutionState.NOT_ATTEMPTED))
                actions.append(RepairAction(
                    "reset_tool_execution", path, -1,
                    "no TOOL_RESULT turn witnesses the execution"))
            elif finding.check == "tool_selection_unwitnessed":
                frames["decision"] = replace(
                    frames["decision"],
                    tool_decision=replace(frames["decision"].tool_decision, selected=()))
                actions.append(RepairAction(
                    "clear_unsupported_field", path, -1,
                    "no tool turn witnesses the selection"))
            elif finding.check == "reasoning_invented":
                frames["decision"] = replace(
                    frames["decision"], chosen_approach="", decision_basis="", subproblems=(),
                    candidate_approaches=(), rejected_approaches=())
                actions.append(RepairAction(
                    "clear_invented_decision", "decision", -1,
                    "the source has no assistant reasoning turn, so every decision field is "
                    "invented (§12): UNKNOWN beats a plausible reconstruction"))

    repaired = replace(record, task=frames["task"], epistemic_state=frames["epistemic_state"],
                       decision=frames["decision"], outcome=frames["outcome"])
    return (repaired, tuple(actions))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the bounded loop
# ══════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RecursionPass:
    """One EXTRACT/REPAIR pass, persisted exactly as §12 requires."""

    stage: str
    depth: int
    artifact_hash: str
    parent_hash: str
    critique: Critique
    repair_actions: tuple[RepairAction, ...]
    verification: SourceVerification | None
    #: Why the loop stopped at this pass, or "" when it continued.
    termination: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": self.stage, "depth": self.depth, "artifact_hash": self.artifact_hash,
            "parent_hash": self.parent_hash, "critique": self.critique.to_dict(),
            "repair_actions": [a.to_dict() for a in self.repair_actions],
            "verification": self.verification.to_dict() if self.verification else None,
            "termination": self.termination,
        }


@dataclass(frozen=True)
class RecursionTrace:
    """The whole auditable chain for one record (§12)."""

    passes: tuple[RecursionPass, ...]
    max_depth: int
    distiller_version: str = DISTILLER_VERSION
    provider_metadata: dict = field(default_factory=dict)

    @property
    def final(self) -> RecursionPass:
        if not self.passes:
            raise DistillationRefused("recursion trace is empty: no pass was recorded")
        return self.passes[-1]

    @property
    def depth_reached(self) -> int:
        return self.final.depth

    @property
    def exhausted(self) -> bool:
        """True when the loop stopped at the cap with material findings still present."""
        return self.final.termination == "depth_cap_reached"

    @property
    def chain_intact(self) -> bool:
        """Every pass after the first names its predecessor's artifact hash (§22).

        A broken link means the trace cannot show what a record was repaired FROM, which is the
        only thing that makes a repair reviewable. Checked as a property rather than assumed,
        because the chain is built by a loop and a loop can drop a link.
        """
        if not self.passes:
            return False
        if self.passes[0].parent_hash:
            return False
        for previous, current in zip(self.passes, self.passes[1:]):
            if current.parent_hash != previous.artifact_hash:
                return False
            if current.depth != previous.depth + 1:
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "distiller_version": self.distiller_version,
            "max_depth": self.max_depth,
            "depth_reached": self.depth_reached,
            "pass_count": len(self.passes),
            "exhausted": self.exhausted,
            "chain_intact": self.chain_intact,
            "provider_metadata": dict(sorted(self.provider_metadata.items())),
            "passes": [p.to_dict() for p in self.passes],
        }


@dataclass(frozen=True)
class DistillationResult:
    """One conversation's distilled record plus its whole audit trail."""

    record: DistilledDecision
    trace: RecursionTrace
    verification: SourceVerification

    def to_dict(self) -> dict:
        return {"record": self.record.to_dict(), "trace": self.trace.to_dict(),
                "verification": self.verification.to_dict()}


def distil(conversation: CanonicalConversation, *,
           config: DistillationConfig | None = None,
           provider: DistillationProvider | None = None) -> DistillationResult:
    """Run the bounded EXTRACT -> CRITIQUE -> REPAIR -> VERIFY loop for one conversation (§12).

    The loop's three termination conditions are in the module docstring. Every pass is recorded
    whether or not it changed anything: a pass that found nothing is evidence the record was
    already clean, and omitting it would make the trace shorter than the work done.
    """
    cfg = (config or default_config()).validated()
    provider_metadata: dict = {}
    if provider is not None:
        availability = provider.availability()
        provider_metadata = {
            "provider_id": provider.provider_id,
            "provider_kind": provider.provider_kind.value,
            "model": provider.model,
            "available": availability.available,
            "availability_reason": availability.reason,
            "deterministic": False,
            "note": "a provider's output is a SUGGESTION and its critique findings are capped "
                    "at ADVISORY; it can never accept or reject a record (§19)",
        }

    record = extract(conversation)
    passes: list[RecursionPass] = []
    parent_hash = ""
    depth = 0
    verification = verify(record, conversation)

    while True:
        found = critique(record, conversation)
        material = found.material(cfg.recursion.material_severities)
        current_hash = record.artifact_hash

        if not material:
            passes.append(RecursionPass(
                stage=record.stage, depth=depth, artifact_hash=current_hash,
                parent_hash=parent_hash, critique=found, repair_actions=(),
                verification=verification, termination="no_material_findings"))
            break

        if depth >= cfg.recursion.max_repair_depth:
            passes.append(RecursionPass(
                stage=record.stage, depth=depth, artifact_hash=current_hash,
                parent_hash=parent_hash, critique=found, repair_actions=(),
                verification=verification, termination="depth_cap_reached"))
            break

        repaired, actions = repair(record, material)
        identical = repaired.artifact_hash == current_hash
        if identical and cfg.recursion.stop_on_identical_artifact:
            passes.append(RecursionPass(
                stage=record.stage, depth=depth, artifact_hash=current_hash,
                parent_hash=parent_hash, critique=found, repair_actions=actions,
                verification=verification, termination="repair_converged_no_change"))
            break

        passes.append(RecursionPass(
            stage=record.stage, depth=depth, artifact_hash=current_hash,
            parent_hash=parent_hash, critique=found, repair_actions=actions,
            verification=verification, termination=""))
        parent_hash = current_hash
        depth += 1
        record = replace(repaired, stage="repair", depth=depth,
                         parent_artifact_hash=parent_hash).validated(
            known_operators=KNOWN_OPERATOR_NAMES)
        verification = verify(record, conversation)

    trace = RecursionTrace(passes=tuple(passes), max_depth=cfg.recursion.max_repair_depth,
                           provider_metadata=provider_metadata)
    return DistillationResult(record=record, trace=trace, verification=verification)


def distil_corpus(conversations: "list[CanonicalConversation]", *, guard: HoldoutGuard,
                  config: DistillationConfig | None = None,
                  provider: DistillationProvider | None = None,
                  limit: int = 0) -> tuple[DistillationResult, ...]:
    """Distil every PERMITTED conversation. The guard is required, not optional (§17).

    ``guard`` is a positional-by-keyword REQUIRED argument. That is the enforcement: there is no
    call to this function that omits the firewall, so "the distiller read the holdout" is a
    signature error rather than an oversight. The assertion runs before any provider is reachable.
    """
    allowed, _refused = guard.filter(conversations, operation="distil_corpus")
    guard.assert_no_holdout_content(list(allowed), operation="distil_corpus")
    selected = allowed[:limit] if limit and limit > 0 else allowed
    return tuple(distil(c, config=config, provider=provider) for c in selected)


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {
        "distiller_version": DISTILLER_VERSION,
        "extraction": "subtractive: every populated field is a span lifted from a source turn; "
                      "nothing is composed",
        "repair_actions": list(REPAIR_ACTIONS),
        "termination_conditions": ["no_material_findings", "repair_converged_no_change",
                                   "depth_cap_reached"],
        "depth_cap_is_not_acceptance": True,
    }


__all__ = [
    "DISTILLER_VERSION", "REPAIR_ACTIONS", "DistillationProvider", "DistillationRefused",
    "DistillationResult", "ProviderSuggestion", "RecursionPass", "RecursionTrace",
    "RepairAction", "SourceVerdict", "distil", "distil_corpus", "extract", "repair",
    "versions",
]
