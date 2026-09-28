"""reasoning_distillation/verifier.py — V69 M67A: does this record trace back to its source?

HOW THIS DIFFERS FROM THE CRITIC
--------------------------------
They ask opposite questions, and §12 lists them as separate stages because collapsing them
loses one of the two:

  * the CRITIC asks *"is anything here wrong?"* — it hunts for defects, and a clean critique
    means no defect was FOUND.
  * this VERIFIER asks *"is everything here attributable?"* — for each populated field, WHICH
    source turn supports it. A clean verification means every field has a named witness.

A record can pass the critic and fail here. That is the interesting case and it is the reason
this stage exists: the critic's grounding check asks whether a field's words appear ANYWHERE in
the conversation, which a fabricated field assembled from the conversation's own vocabulary can
satisfy. This stage asks which SINGLE turn supports the field, which that fabrication cannot
answer — its words come from three different turns and no one of them says it.

WHAT "ATTRIBUTABLE" MEANS HERE
------------------------------
A field is attributed to a source unit when that unit's text grounds it at or above
:data:`ATTRIBUTION_FLOOR`. The floor is higher than the critic's
:data:`~reasoning_distillation.critic.GROUNDING_OVERLAP_FLOOR` on purpose: a single turn is a
much smaller vocabulary than a whole conversation, so demanding the same fraction from it is a
strictly stronger requirement, and demanding MORE makes the attribution meaningful rather than
incidental.

WHY A PARTIAL RESULT IS NOT A PASS
----------------------------------
:class:`SourceVerification` has three verdicts and ``PARTIAL`` is not clean. §22's absent-control
list requires that *every ACCEPT item has a verified source link*, so a record with two
attributed fields and one orphan is not two-thirds accepted — it routes to review. The orphan is
the interesting field: it is the one nobody can check.

ONE THING THIS CANNOT DO
------------------------
It cannot verify that the historical reasoning was CORRECT, and nothing in M67A can. Attribution
proves the record faithfully describes what the trace said. Whether what the trace said was
right is §10's fact/procedure question, and for almost every claim in this corpus the honest
answer stays :attr:`~reasoning_distillation.models.FactualState.SOURCE_ONLY`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from training_gym.schemas import SchemaError

from .critic import _content_words
from .models import CanonicalConversation, DistilledDecision, TurnRole

#: Bump when attribution semantics change. Recorded per pass in the recursion trace.
VERIFIER_VERSION = "m67a.verifier.1"

#: Fraction of a field's content words that must appear in ONE source turn for that turn to be
#: its witness. Higher than the critic's conversation-wide floor: see the module docstring.
ATTRIBUTION_FLOOR = 0.6

#: Roles whose text may witness a field. The USER's turns witness the TASK; the assistant's
#: witness the DECISION. A tool result witnesses an observation. Deliberately explicit, because
#: crediting the assistant with a procedure the USER described is the failure this prevents.
_TASK_WITNESS_ROLES = frozenset({TurnRole.USER_REQUEST, TurnRole.SYSTEM_CONTEXT,
                                 TurnRole.FOLLOW_UP})
_DECISION_WITNESS_ROLES = frozenset({TurnRole.ASSISTANT_REASONING, TurnRole.ASSISTANT_REVISION,
                                     TurnRole.ASSISTANT_ANSWER, TurnRole.TOOL_REQUEST,
                                     TurnRole.TOOL_RESULT})


class VerificationError(SchemaError):
    """A verification could not run, so attribution is UNKNOWN rather than absent."""


class SourceVerdict(str, Enum):
    """The outcome of source verification. ``PARTIAL`` is not a pass."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"

    @property
    def is_clean(self) -> bool:
        return self in (SourceVerdict.VERIFIED, SourceVerdict.NOT_APPLICABLE)


@dataclass(frozen=True)
class Attribution:
    """One field traced to one source unit, or recorded as un-attributable."""

    field_path: str
    index: int
    witness_unit_id: str
    overlap: float
    #: The witness's role, so a reviewer can see at a glance whether a decision field was
    #: attributed to an assistant turn or (wrongly) to the user's.
    witness_role: str = ""

    @property
    def attributed(self) -> bool:
        return bool(self.witness_unit_id)

    def to_dict(self) -> dict:
        return {"field_path": self.field_path, "index": self.index,
                "witness_unit_id": self.witness_unit_id, "overlap": round(self.overlap, 6),
                "witness_role": self.witness_role, "attributed": self.attributed}


@dataclass(frozen=True)
class SourceVerification:
    """Every populated field's attribution, and the resulting verdict."""

    verdict: SourceVerdict
    attributions: tuple[Attribution, ...]
    verifier_version: str = VERIFIER_VERSION
    attribution_floor: float = ATTRIBUTION_FLOOR

    @property
    def orphans(self) -> tuple[Attribution, ...]:
        """Fields no single source turn supports. The interesting ones."""
        return tuple(a for a in self.attributions if not a.attributed)

    @property
    def attributed_count(self) -> int:
        return sum(1 for a in self.attributions if a.attributed)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "verifier_version": self.verifier_version,
            "attribution_floor": self.attribution_floor,
            "field_count": len(self.attributions),
            "attributed_count": self.attributed_count,
            "orphan_count": len(self.orphans),
            "attributions": [a.to_dict() for a in self.attributions],
        }


def _best_witness(text: str, conversation: CanonicalConversation,
                  roles: frozenset) -> tuple[str, float, str]:
    """The single turn that best supports *text*. Returns ``("", 0.0, "")`` when none does.

    Ties break on the turn's ``unit_id`` so the result is deterministic. Without that, two
    equally-good witnesses would be chosen by iteration order and two runs could attribute the
    same field to different turns — which would make the attribution unreproducible and
    therefore useless as evidence.
    """
    words = _content_words(text)
    if not words:
        return ("", 0.0, "")
    best: tuple[str, float, str] = ("", 0.0, "")
    for turn in conversation.turns:
        if turn.role not in roles:
            continue
        overlap = len(words & _content_words(turn.text)) / len(words)
        if overlap > best[1] or (overlap == best[1] and overlap > 0.0
                                 and turn.provenance.unit_id < best[0]):
            best = (turn.provenance.unit_id, overlap, turn.role.value)
    if best[1] < ATTRIBUTION_FLOOR:
        return ("", best[1], "")
    return best


def verify(record: DistilledDecision,
           conversation: CanonicalConversation) -> SourceVerification:
    """Attribute every populated free-text field of *record* to a source turn (§12).

    Fields are checked against the witness roles appropriate to their meaning: task fields
    against the operator's own turns, decision fields against the assistant's. A decision field
    that can only be attributed to a USER turn is left as an orphan rather than credited — the
    user describing an approach is not the assistant choosing one.
    """
    if record.provenance.conversation_id != conversation.conversation_id:
        raise VerificationError(
            f"{record.example_id}: refusing to verify against conversation "
            f"{conversation.conversation_id!r}; the record names "
            f"{record.provenance.conversation_id!r}. Verifying against the wrong source would "
            f"produce a verdict that means nothing")

    checks: list[tuple[str, int, str, frozenset]] = []
    for index, value in enumerate(record.task.constraints):
        checks.append(("task.constraints", index, value, _TASK_WITNESS_ROLES))
    for index, value in enumerate(record.task.context_dependencies):
        checks.append(("task.context_dependencies", index, value, _TASK_WITNESS_ROLES))
    if record.task.user_goal_summary:
        checks.append(("task.user_goal_summary", -1, record.task.user_goal_summary,
                       _TASK_WITNESS_ROLES))
    for path, values in (("epistemic_state.known", record.epistemic_state.known),
                         ("epistemic_state.unknown", record.epistemic_state.unknown),
                         ("epistemic_state.assumptions", record.epistemic_state.assumptions),
                         ("epistemic_state.uncertainties", record.epistemic_state.uncertainties)):
        for index, value in enumerate(values):
            checks.append((path, index, value, _DECISION_WITNESS_ROLES))
    for path, values in (("decision.subproblems", record.decision.subproblems),
                         ("decision.candidate_approaches", record.decision.candidate_approaches),
                         ("decision.rejected_approaches", record.decision.rejected_approaches),
                         ("decision.verification_plan", record.decision.verification_plan),
                         ("outcome.self_corrections", record.outcome.self_corrections)):
        for index, value in enumerate(values):
            checks.append((path, index, value, _DECISION_WITNESS_ROLES))
    for path, value in (("decision.chosen_approach", record.decision.chosen_approach),
                        ("decision.decision_basis", record.decision.decision_basis),
                        ("decision.stop_condition", record.decision.stop_condition),
                        ("decision.tool_decision.basis", record.decision.tool_decision.basis)):
        if value:
            checks.append((path, -1, value, _DECISION_WITNESS_ROLES))

    attributions: list[Attribution] = []
    for path, index, value, roles in checks:
        unit, overlap, role = _best_witness(value, conversation, roles)
        attributions.append(Attribution(field_path=path, index=index, witness_unit_id=unit,
                                        overlap=overlap, witness_role=role))

    if not attributions:
        # Nothing populated. NOT_APPLICABLE rather than VERIFIED: an empty record has not been
        # verified, it has nothing to verify, and the two must not report the same verdict.
        verdict = SourceVerdict.NOT_APPLICABLE
    elif all(a.attributed for a in attributions):
        verdict = SourceVerdict.VERIFIED
    elif any(a.attributed for a in attributions):
        verdict = SourceVerdict.PARTIAL
    else:
        verdict = SourceVerdict.FAILED

    return SourceVerification(verdict=verdict, attributions=tuple(attributions))


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {"verifier_version": VERIFIER_VERSION, "attribution_floor": ATTRIBUTION_FLOOR,
            "verdicts": [v.value for v in SourceVerdict],
            "partial_is_clean": False,
            "note": "attribution proves the record describes what the trace SAID; it cannot "
                    "and does not prove the trace was correct (§10)"}


__all__ = [
    "ATTRIBUTION_FLOOR", "VERIFIER_VERSION", "Attribution", "SourceVerdict",
    "SourceVerification", "VerificationError", "verify", "versions",
]
