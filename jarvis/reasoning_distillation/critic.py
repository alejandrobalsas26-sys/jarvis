"""reasoning_distillation/critic.py — V69 M67A: what is wrong with this extraction.

WHAT THE CRITIC IS FOR
----------------------
§12 puts CRITIQUE between EXTRACT and REPAIR, and the reason is that the failure mode of any
extractor — deterministic or model-assisted — is not *missing* a field. It is **filling one
in**. An extractor asked for ``constraints`` will produce constraints; asked for
``verification_plan`` it will produce a plausible verification plan. The result reads well,
validates against the schema, and describes reasoning that never happened.

So the critic's job is adversarial in one specific direction: **it looks for claims the source
does not support.** Every finding is therefore phrased as a defect in the RECORD, checked
against the CONVERSATION, and never as an opinion about whether the historical reasoning was
good. Whether the reasoning was good is :mod:`reasoning_distillation.quality`'s question.

WHY SEVERITY EXISTS, AND WHY IT IS THREE VALUES
-----------------------------------------------
§12 says to stop early when no MATERIAL findings remain. That requires a definition of material
that is not a judgement made at the loop's call site:

  * ``BLOCKING`` — the record asserts something the source contradicts, or a required link is
    missing. Repair cannot fix this by adding detail; the record is wrong.
  * ``MATERIAL`` — the record is unsupported or incomplete in a way a repair pass can address
    by REMOVING an unsupported claim or marking something UNKNOWN.
  * ``ADVISORY`` — worth recording, not worth another pass. A repair loop that chased advisory
    findings would never converge, and §12 caps the depth precisely because an unbounded
    refinement loop is a failure mode rather than a feature.

Only ``BLOCKING`` and ``MATERIAL`` are material, and that is configuration
(:attr:`~reasoning_distillation.config.RecursionConfig.material_severities`), not a constant
here.

THE CHECK THAT MATTERS MOST
---------------------------
:func:`_check_unsupported_text` is the load-bearing one. Every free-text field in the record —
each constraint, each assumption, each subproblem — must be GROUNDED: its content words have to
appear in the source conversation. A field whose words are not in the source was written by the
extractor, not found in the trace.

It is deliberately lexical and deliberately generous (a configurable overlap floor, stopwords
removed, no stemming). A stricter check would flag legitimate paraphrase; a looser one would
pass invented text. The direction of the error is chosen: this check produces MATERIAL findings
that a repair pass resolves by dropping the field, so a false positive costs a field and a
false negative imports a fabrication.

THE CRITIC IS NOT A MODEL
-------------------------
Every check here is deterministic and offline (§19). A model-assisted critic is possible and is
wired through :mod:`reasoning_distillation.distiller`'s provider hook, but its findings are
ADVISORY-capped there and can never by themselves reject a record. A model's opinion about
another model's reasoning is not evidence, and this repository's teacher contract already
establishes that rule for exactly this reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from training_gym.schemas import SchemaError

from .models import (
    CanonicalConversation,
    DistilledDecision,
    ExportStatus,
    FactualState,
    TurnRole,
)
from .operators import ReasoningOperator, detect

#: Bump when a check is added, removed or its severity changes. Recorded per pass in the
#: recursion trace, so a record accepted under an older critic is identifiable.
CRITIC_VERSION = "m67a.critic.1"

#: Fraction of a field's content words that must appear in the source for it to be grounded.
#: Not in :mod:`reasoning_distillation.config` because it is not a QUALITY threshold — it is
#: this check's own sensitivity, and moving it changes what "supported" means rather than how
#: strict the gate is. It is recorded in :func:`versions`.
GROUNDING_OVERLAP_FLOOR = 0.5

#: Words carrying no grounding signal. Kept small on purpose: an aggressive stoplist would
#: reduce a short constraint to nothing and then call the empty set "grounded".
_STOPWORDS: frozenset[str] = frozenset("""
a an the and or but if then than that this these those is are was were be been being
to of in on at by for with from as it its do does did not no nor so such very
must should may can will would could shall have has had i we you they he she
el la los las un una y o pero si que este esta esto ser es son era fue de en con
por para como no ni muy debe puede
""".split())

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


class CriticError(SchemaError):
    """A critique could not be performed, so its result is UNKNOWN rather than clean."""


class Severity(str, Enum):
    """How much a finding matters. See the module docstring for why there are three."""

    BLOCKING = "blocking"
    MATERIAL = "material"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class CriticFinding:
    """One defect in an extracted record. Body-free: names fields, never quotes bodies.

    ``field_path`` is what a repair pass acts on, so it is a real path into
    :meth:`DistilledDecision.to_dict` rather than prose. A finding a repairer cannot locate is
    a finding that will survive every pass and exhaust the recursion budget.
    """

    check: str
    severity: Severity
    field_path: str
    detail: str
    #: Index into a list-shaped field, when the finding is about one element. ``-1`` means the
    #: whole field.
    index: int = -1

    @property
    def material(self) -> bool:
        return self.severity in (Severity.BLOCKING, Severity.MATERIAL)

    def to_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity.value,
                "field_path": self.field_path, "detail": self.detail[:320],
                "index": self.index}


def _content_words(text: str) -> frozenset[str]:
    """Lowercased content words of *text*, stopwords removed."""
    return frozenset(w for w in (m.group(0).lower() for m in _WORD.finditer(text or ""))
                     if w not in _STOPWORDS)


def _source_vocabulary(conversation: CanonicalConversation) -> frozenset[str]:
    """Every content word anywhere in the conversation. The grounding reference set."""
    words: set[str] = set()
    for turn in conversation.turns:
        words |= _content_words(turn.text)
    return frozenset(words)


def _grounded(text: str, vocabulary: frozenset[str]) -> tuple[bool, float]:
    """Whether *text*'s content words appear in the source, and by what fraction.

    An EMPTY content-word set returns ``(False, 0.0)`` rather than ``(True, 1.0)``. A field of
    pure stopwords — "it must be the one" — is not grounded; it carries no recoverable claim,
    and vacuous truth here would let an extractor satisfy the check with filler.
    """
    words = _content_words(text)
    if not words:
        return (False, 0.0)
    overlap = len(words & vocabulary) / len(words)
    return (overlap >= GROUNDING_OVERLAP_FLOOR, overlap)


# ── individual checks ──────────────────────────────────────────────────────────────────────
def _check_provenance(record: DistilledDecision,
                      conversation: CanonicalConversation) -> list[CriticFinding]:
    """The record must point at THIS conversation's real segments (§6, §22)."""
    out: list[CriticFinding] = []
    if record.provenance.conversation_id != conversation.conversation_id:
        out.append(CriticFinding(
            "provenance_conversation_mismatch", Severity.BLOCKING, "provenance.conversation_id",
            f"record names {record.provenance.conversation_id!r} but was critiqued against "
            f"{conversation.conversation_id!r}; a record verified against the wrong source is "
            f"not verified"))
    real_units = {t.provenance.unit_id for t in conversation.turns}
    phantom = sorted(set(record.provenance.source_unit_ids) - real_units)
    if phantom:
        out.append(CriticFinding(
            "provenance_phantom_units", Severity.BLOCKING, "provenance.source_unit_ids",
            f"{len(phantom)} source_unit_id(s) do not exist in this conversation; the record "
            f"cites source that cannot be re-read"))
    if conversation.source_file_hash not in record.provenance.source_file_hashes:
        out.append(CriticFinding(
            "provenance_missing_file_hash", Severity.BLOCKING, "provenance.source_file_hashes",
            "the conversation's own source_file_hash is absent from the record"))
    return out


def _check_reasoning_present(record: DistilledDecision,
                             conversation: CanonicalConversation) -> list[CriticFinding]:
    """A decision procedure requires that the source actually showed a procedure (§7)."""
    if conversation.has_reasoning():
        return []
    populated = [
        name for name, value in (
            ("decision.chosen_approach", record.decision.chosen_approach),
            ("decision.decision_basis", record.decision.decision_basis),
            ("decision.subproblems", record.decision.subproblems),
            ("decision.candidate_approaches", record.decision.candidate_approaches),
        ) if value]
    if not populated:
        return [CriticFinding(
            "no_reasoning_in_source", Severity.MATERIAL, "decision",
            "the source has no assistant reasoning turn, so no decision procedure can be "
            "extracted; the record is correctly empty and should be dispositioned "
            "INSUFFICIENT_CONTEXT rather than repaired")]
    return [CriticFinding(
        "reasoning_invented", Severity.BLOCKING, "decision",
        f"the source has NO assistant reasoning turn, yet {populated[:3]} are populated. "
        f"This is invented reasoning (§12): UNKNOWN beats a plausible reconstruction")]


def _check_unsupported_text(record: DistilledDecision,
                            conversation: CanonicalConversation) -> list[CriticFinding]:
    """Every free-text field must be grounded in the source. The load-bearing check."""
    vocabulary = _source_vocabulary(conversation)
    out: list[CriticFinding] = []
    list_fields: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("task.constraints", record.task.constraints),
        ("task.context_dependencies", record.task.context_dependencies),
        ("epistemic_state.known", record.epistemic_state.known),
        ("epistemic_state.unknown", record.epistemic_state.unknown),
        ("epistemic_state.assumptions", record.epistemic_state.assumptions),
        ("epistemic_state.uncertainties", record.epistemic_state.uncertainties),
        ("decision.subproblems", record.decision.subproblems),
        ("decision.candidate_approaches", record.decision.candidate_approaches),
        ("decision.rejected_approaches", record.decision.rejected_approaches),
        ("decision.verification_plan", record.decision.verification_plan),
        ("outcome.self_corrections", record.outcome.self_corrections),
    )
    for path, values in list_fields:
        for index, value in enumerate(values):
            ok, overlap = _grounded(value, vocabulary)
            if not ok:
                out.append(CriticFinding(
                    "unsupported_text", Severity.MATERIAL, path,
                    f"element {index} is {overlap:.0%} grounded in the source, below the "
                    f"{GROUNDING_OVERLAP_FLOOR:.0%} floor; its content words are not in the "
                    f"conversation, so the extractor wrote it rather than found it",
                    index=index))
    for path, value in (("task.user_goal_summary", record.task.user_goal_summary),
                        ("decision.chosen_approach", record.decision.chosen_approach),
                        ("decision.decision_basis", record.decision.decision_basis),
                        ("decision.stop_condition", record.decision.stop_condition),
                        ("decision.tool_decision.basis", record.decision.tool_decision.basis)):
        if not value:
            continue
        ok, overlap = _grounded(value, vocabulary)
        if not ok:
            out.append(CriticFinding(
                "unsupported_text", Severity.MATERIAL, path,
                f"{overlap:.0%} grounded in the source, below the "
                f"{GROUNDING_OVERLAP_FLOOR:.0%} floor"))
    return out


def _check_operators(record: DistilledDecision,
                     conversation: CanonicalConversation) -> list[CriticFinding]:
    """Claimed operators must have marker evidence somewhere in the assistant's own turns.

    Checked against reasoning and revision turns only. An operator marker inside the USER's
    message is the user reasoning, not the assistant, and crediting the assistant for it is how
    a corpus learns to claim procedures it did not run.
    """
    process_text = "\n".join(
        t.text for t in conversation.turns
        if t.role in (TurnRole.ASSISTANT_REASONING, TurnRole.ASSISTANT_REVISION,
                      TurnRole.ASSISTANT_ANSWER))
    evidenced = set(detect(process_text))
    out: list[CriticFinding] = []
    for index, name in enumerate(record.decision.reasoning_operators):
        try:
            operator = ReasoningOperator(name)
        except ValueError:
            out.append(CriticFinding(
                "unknown_operator", Severity.BLOCKING, "decision.reasoning_operators",
                f"{name!r} is not in the closed taxonomy", index=index))
            continue
        if operator not in evidenced:
            out.append(CriticFinding(
                "operator_without_evidence", Severity.MATERIAL,
                "decision.reasoning_operators",
                f"{operator.value} is claimed but no marker for it appears in the "
                f"assistant's own turns; marker absence is weak evidence, so this is MATERIAL "
                f"rather than BLOCKING and a repair may keep it only with a stated basis",
                index=index))
    return out


def _check_tool_decision(record: DistilledDecision,
                         conversation: CanonicalConversation) -> list[CriticFinding]:
    """Tool need, tool selection and tool execution must not be conflated (§11)."""
    out: list[CriticFinding] = []
    tool = record.decision.tool_decision
    has_tool_turn = any(t.role in (TurnRole.TOOL_REQUEST, TurnRole.TOOL_RESULT)
                        for t in conversation.turns)
    from core.epistemic_deliberation import ToolExecutionState
    if tool.execution is ToolExecutionState.SUCCEEDED and not has_tool_turn:
        out.append(CriticFinding(
            "tool_execution_unwitnessed", Severity.BLOCKING, "decision.tool_decision.execution",
            "execution is SUCCEEDED but the conversation contains no TOOL_RESULT turn; a tool "
            "outcome that nothing witnessed is NOT_ATTEMPTED as far as this corpus knows"))
    if tool.selected and not has_tool_turn:
        out.append(CriticFinding(
            "tool_selection_unwitnessed", Severity.MATERIAL, "decision.tool_decision.selected",
            "tools are named as selected but the conversation has no tool turn; selection may "
            "still have been discussed, so this is MATERIAL and resolvable by moving the names "
            "into `basis`"))
    return out


def _check_verification(record: DistilledDecision,
                        conversation: CanonicalConversation) -> list[CriticFinding]:
    """A verification STATUS must not outrun the verification the source shows."""
    del conversation
    from core.epistemic_deliberation import PASSING_STATUS, VerificationStatus
    out: list[CriticFinding] = []
    status = record.decision.verification_status
    if status is VerificationStatus.VERIFIED and not record.decision.verification_plan:
        out.append(CriticFinding(
            "verified_without_plan", Severity.MATERIAL, "decision.verification_status",
            "status is VERIFIED with an empty verification_plan; a verdict with no stated "
            "check behind it is NOT_VERIFIED"))
    if status in PASSING_STATUS and record.outcome.observed_success is None:
        out.append(CriticFinding(
            "passing_status_unknown_outcome", Severity.ADVISORY, "outcome.observed_success",
            f"verification_status is {status.value} while observed_success is UNKNOWN; that is "
            f"consistent (a plan can pass with no recorded outcome) and is recorded so a "
            f"reader does not mistake the status for evidence the answer worked"))
    return out


def _check_fact_procedure(record: DistilledDecision,
                          conversation: CanonicalConversation) -> list[CriticFinding]:
    """Fact and procedure must be separated, and their link decided (§10)."""
    del conversation
    out: list[CriticFinding] = []
    for index, claim in enumerate(record.factual_claims):
        if claim.state.is_usable_as_truth:
            out.append(CriticFinding(
                "claim_marked_verified", Severity.BLOCKING, "factual_claims",
                f"claim {index} is marked VERIFIED. M67A verifies no external facts (§10), so "
                f"no extraction path may produce this state; SOURCE_ONLY is the honest value",
                index=index))
    linked = {link.claim_digest for link in record.fact_procedure_links}
    for index, claim in enumerate(record.factual_claims):
        if claim.state in (FactualState.STALE, FactualState.CONTRADICTED):
            if claim.statement_digest not in linked:
                out.append(CriticFinding(
                    "stale_claim_unlinked", Severity.MATERIAL, "fact_procedure_links",
                    f"claim {index} is {claim.state.value} with no fact_procedure_link; whether "
                    f"the procedure DEPENDS on it is exactly the §10 question and it is "
                    f"undecided", index=index))
    for index, link in enumerate(record.fact_procedure_links):
        if not link.decided:
            out.append(CriticFinding(
                "fact_procedure_link_undecided", Severity.MATERIAL, "fact_procedure_links",
                f"link {index} does not say whether the procedure depends on the claim; "
                f"undecided routes to human review rather than being guessed either way",
                index=index))
    return out


def _check_privacy(record: DistilledDecision,
                   conversation: CanonicalConversation) -> list[CriticFinding]:
    """The record's privacy status must be explicit and must not be weaker than its source."""
    out: list[CriticFinding] = []
    if record.privacy.export_status is ExportStatus.EXPORT_UNKNOWN:
        out.append(CriticFinding(
            "privacy_undecided", Severity.BLOCKING, "privacy.export_status",
            "export status is still UNKNOWN; §4 requires every record to carry an explicit "
            "privacy decision"))
    source_status = conversation.export_status
    if (record.privacy.export_status is ExportStatus.EXPORT_SAFE
            and source_status is not ExportStatus.EXPORT_SAFE):
        out.append(CriticFinding(
            "privacy_weaker_than_source", Severity.BLOCKING, "privacy.export_status",
            f"the record claims EXPORT_SAFE while its source conversation is "
            f"{source_status.value}; a derived record cannot be safer than what it derives from"))
    return out


#: Negation markers. A span carrying one of these, that otherwise closely matches a span without
#: one, is the shape of a self-contradiction.
_NEGATIONS = (" not ", " no ", " never ", " does not", " doesn't", " isn't", " is not",
              " cannot", " can't", " won't", " nunca", " tampoco")

#: Content-word overlap at or above which two spans are "about the same thing". Higher than the
#: grounding floor: a contradiction claim needs the two spans to be near-restatements, not merely
#: topical neighbours, or every reasoning trace that discusses both sides of a question would be
#: flagged — and weighing both sides is a virtue, not a defect.
CONTRADICTION_OVERLAP_FLOOR = 0.7


def _strip_negation(text: str) -> str:
    out = text.lower()
    for marker in _NEGATIONS:
        out = out.replace(marker, " ")
    return out


def _predicate_words(text: str) -> frozenset[str]:
    """Content words with a minimal singular fold, for polarity comparison ONLY.

    Two spans that contradict each other rarely inflect identically: "the cache evicts on write"
    against "the cache does not evict on write" differ in the very word the comparison hinges on.
    Measured on the §18 contradictory fixture, the unfolded sets overlapped 33% and the check did
    not fire.

    The fold is deliberately the crudest one that works — drop a trailing ``s`` on words of four
    characters or more — and it is confined to this check. It is NOT used by the grounding check or
    by source attribution, where conflating two inflections could let a fabricated field match a
    source it does not actually restate. Here the only consequence of an over-eager match is one
    more finding routed to a human.
    """
    return frozenset(w[:-1] if len(w) >= 4 and w.endswith("s") else w
                     for w in _content_words(text))


def _check_contradiction(record: DistilledDecision,
                         conversation: CanonicalConversation) -> list[CriticFinding]:
    """The assistant asserted a thing and its negation, with no correction (§18 case 13).

    WHY THIS CHECK EXISTS, AND WHY IT WAS MISSING
    ---------------------------------------------
    ``RejectReason.CONTRADICTORY_TRACE`` was in the closed reason set from the start and NOTHING
    could emit it. A synthetic fixture that asserts "the cache evicts on write", then "the cache
    does not evict on write", then answers "it evicts on write" was scored ACCEPT — a trace that
    contradicts itself entering the corpus as a clean decision procedure. Found by the §18 case
    that exists precisely to look for this, which is the argument for authoring negative fixtures
    against reason codes rather than against behaviour already known to work.

    WHY IT IS LEXICAL, AND WHAT THAT COSTS
    --------------------------------------
    Two spans are contradictory here when they say nearly the same thing and exactly one of them
    is negated. That is deliberately narrow. Genuine reasoning weighs both sides constantly —
    "option A will not work, option B will" is good thinking, not a contradiction — so the
    overlap floor is set high and a SELF_CORRECTION present in the same turn suppresses the
    finding entirely: a trace that says "actually, that was incorrect" has contradicted itself ON
    PURPOSE, and §18 keeps those as two different fixtures because they are two different things.

    Severity is MATERIAL rather than BLOCKING: the repairer cannot decide which side the trace
    meant, so this survives repair and routes to a human, which is the §12 answer for an
    unresolved disagreement.
    """
    del record
    out: list[CriticFinding] = []
    for turn in conversation.turns:
        if turn.role not in (TurnRole.ASSISTANT_REASONING, TurnRole.ASSISTANT_ANSWER):
            continue
        # Semicolons split too: a compound clause ("does not evict on write; eviction happens on
        # read") otherwise carries both halves into one span and dilutes the overlap below any
        # sensible floor, which is the second reason the check missed the §18 fixture.
        spans = [s.strip() for s in re.split(r"(?<=[.!?])\s+|;\s*", turn.text) if s.strip()]
        lowered = " ".join(spans).lower()
        if any(m in lowered for m in ("actually", "correction", "i was wrong", "i made a mistake",
                                      "let me correct", "me equivoqué")):
            continue  # a deliberate self-correction, not a contradiction
        for left_index, left in enumerate(spans):
            left_neg = any(m in f" {left.lower()} " for m in _NEGATIONS)
            for right in spans[left_index + 1:]:
                right_neg = any(m in f" {right.lower()} " for m in _NEGATIONS)
                if left_neg == right_neg:
                    continue
                a = _predicate_words(_strip_negation(left))
                b = _predicate_words(_strip_negation(right))
                if not a or not b:
                    continue
                overlap = len(a & b) / max(len(a), len(b))
                if overlap >= CONTRADICTION_OVERLAP_FLOOR:
                    out.append(CriticFinding(
                        "contradictory_trace", Severity.MATERIAL, "decision.decision_basis",
                        f"two spans in a {turn.role.value} turn restate each other with opposite "
                        f"polarity at {overlap:.0%} content overlap, and the turn carries no "
                        f"self-correction; which one the trace meant is a human's reading",
                        index=left_index))
                    return out  # one finding per record is enough to route it
    return out


def _check_meta_noise(record: DistilledDecision,
                      conversation: CanonicalConversation) -> list[CriticFinding]:
    """§13 — a record distilled only from marked meta-noise carries no cognitive value."""
    del record
    process = [t for t in conversation.turns
               if t.role in (TurnRole.ASSISTANT_REASONING, TurnRole.ASSISTANT_REVISION)]
    if process and all(t.is_meta_noise for t in process):
        return [CriticFinding(
            "all_reasoning_is_meta_noise", Severity.MATERIAL, "decision",
            f"all {len(process)} assistant process turn(s) are marked meta-noise (§13); the "
            f"source is present but carries no decision, so this is META_POLICY_NOISE rather "
            f"than a record to repair")]
    return []


#: The checks, in the order they run. Named so :func:`versions` can publish the set: an audit
#: that cannot say WHICH checks ran cannot support a claim that a record is clean.
CHECKS: tuple[tuple[str, object], ...] = (
    ("provenance", _check_provenance),
    ("reasoning_present", _check_reasoning_present),
    ("unsupported_text", _check_unsupported_text),
    ("operators", _check_operators),
    ("tool_decision", _check_tool_decision),
    ("verification", _check_verification),
    ("fact_procedure", _check_fact_procedure),
    ("privacy", _check_privacy),
    ("meta_noise", _check_meta_noise),
    ("contradiction", _check_contradiction),
)


@dataclass(frozen=True)
class Critique:
    """Every finding for one record, plus which checks produced them."""

    findings: tuple[CriticFinding, ...]
    critic_version: str = CRITIC_VERSION
    checks_run: tuple[str, ...] = ()

    @property
    def blocking(self) -> tuple[CriticFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKING)

    def material(self, severities: tuple[str, ...] = ("blocking", "material")
                 ) -> tuple[CriticFinding, ...]:
        wanted = frozenset(severities)
        return tuple(f for f in self.findings if f.severity.value in wanted)

    @property
    def clean(self) -> bool:
        """No material finding. Advisory findings do not make a record unclean."""
        return not self.material()

    def to_dict(self) -> dict:
        return {"critic_version": self.critic_version,
                "checks_run": list(self.checks_run),
                "finding_count": len(self.findings),
                "blocking_count": len(self.blocking),
                "material_count": len(self.material()),
                "findings": [f.to_dict() for f in self.findings]}


def critique(record: DistilledDecision, conversation: CanonicalConversation) -> Critique:
    """Run every check against *record*. Deterministic and offline.

    A check that RAISES aborts the critique rather than being skipped. A critique missing one of
    its checks would report fewer findings and read as a cleaner record, which is the shape of
    failure this package refuses everywhere else.
    """
    findings: list[CriticFinding] = []
    ran: list[str] = []
    for name, check in CHECKS:
        try:
            findings.extend(check(record, conversation))  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - a skipped check must be visible, not silent
            raise CriticError(
                f"critic check {name!r} failed on {record.example_id}: "
                f"{type(exc).__name__}: {exc}. A critique with a missing check reports fewer "
                f"findings and reads as a cleaner record") from exc
        ran.append(name)
    return Critique(
        findings=tuple(sorted(findings, key=lambda f: (f.severity.value, f.check,
                                                       f.field_path, f.index))),
        checks_run=tuple(ran))


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {"critic_version": CRITIC_VERSION,
            "checks": [name for name, _ in CHECKS],
            "grounding_overlap_floor": GROUNDING_OVERLAP_FLOOR,
            "contradiction_overlap_floor": CONTRADICTION_OVERLAP_FLOOR,
            "severities": [s.value for s in Severity],
            "note": "deterministic and offline; a model-assisted critic's findings are "
                    "ADVISORY-capped by the distiller and can never reject a record alone"}


__all__ = [
    "CHECKS", "CONTRADICTION_OVERLAP_FLOOR", "CRITIC_VERSION", "GROUNDING_OVERLAP_FLOOR",
    "CriticError", "CriticFinding",
    "Critique", "Severity", "critique", "versions",
]
