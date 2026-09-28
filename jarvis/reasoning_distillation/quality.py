"""reasoning_distillation/quality.py — V69 M67A: fifteen dimensions, and no magic scalar.

WHY THERE IS NO SINGLE SCORE
----------------------------
§14 opens with *"do NOT reduce quality to one magic scalar"*, and this repository has already
measured why. At S4G a milestone reported a mean quality gain of +0.1714 that turned out to be
six safety flips — a single aggregate number moved in the right direction while the thing it was
supposed to summarise moved in the wrong one. An average lets a strong score on one dimension pay
for a missing score on another, and the dimensions here are not interchangeable: ``efficiency``
and ``provenance_quality`` are not two views of the same virtue, and no amount of the former
substitutes for the latter.

So :class:`QualityScore` carries fifteen named dimensions, each with its own floor, and the
disposition is decided by WHICH floors were missed rather than by any combination of the values.
:attr:`QualityScore.mean` exists — reporting it is useful — but **nothing reads it**, and
:func:`disposition` does not receive it. That is deliberate: a scalar that no decision depends on
cannot quietly become the decision.

THE THREE-WAY DISPOSITION
-------------------------
  * ``REJECT`` — a REQUIRED dimension is below its floor, or a blocking defect stands. The record
    is wrong, not merely weak.
  * ``NEEDS_HUMAN_REVIEW`` — the record may be fine and a machine cannot tell. Every undecided
    fact/procedure link, every exhausted recursion, every critic disagreement lands here. §12 and
    §14 both route here rather than guessing, and §29's ``UNKNOWN beats invented completeness``
    is the same instruction.
  * ``ACCEPT`` — every required floor met, at most
    :attr:`~reasoning_distillation.config.QualityConfig.max_soft_floor_misses` soft misses, source
    verification clean, privacy decided.

Every non-ACCEPT outcome carries :class:`~reasoning_distillation.models.RejectReason` codes (§14).
A disposition with no reason code is not permitted by :meth:`QualityVerdict.validated`, because a
rejection nobody can explain is a rejection nobody can appeal.

WHAT THE SCORES ARE AND ARE NOT
-------------------------------
Each dimension is scored from OBSERVABLE STRUCTURE — how many fields survived verification, whether
a freshness-sensitive task noticed its freshness, whether claimed operators had evidence. None of
them measures whether the historical reasoning was *good*, and none of them is calibrated against
human judgement. This repository recorded at S4H that a synthetic detector rate is not calibration;
the same caution applies here and is stated in :func:`versions` so it travels with the numbers.

These scores are therefore a trustworthiness filter for corpus construction, not a quality
measurement of Claude's reasoning. §26 forbids the latter claim and nothing here supports it.

WHY ``provenance_quality`` HAS A FLOOR OF 1.0
--------------------------------------------
Because it is binary in substance: either every field traces to source or one does not. A floor of
0.9 would mean "nine of ten fields are attributable, accept it", and the tenth field — the one
nobody can check — is precisely the one that would end up in a training corpus unexamined. §22
lists *every ACCEPT item has a verified source link* as a required control, and a fractional floor
would not implement it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from training_gym.schemas import SchemaError

from .config import QualityConfig
from .critic import Critique, Severity
from .distiller import RecursionTrace
from .models import (
    CanonicalConversation,
    Disposition,
    DistilledDecision,
    ExportStatus,
    FreshnessRequirement,
    RejectReason,
)
from .verifier import SourceVerdict, SourceVerification

#: Bump when a dimension is added or its scoring changes. Two corpora scored under different
#: versions are not comparable, and the version is how a reader knows.
QUALITY_VERSION = "m67a.quality.1"

#: The fifteen dimensions §14 names, in its order. Named here so a missing dimension is a failure
#: rather than a silently absent score.
DIMENSIONS: tuple[str, ...] = (
    "task_understanding", "constraint_retention", "reasoning_relevance", "premise_handling",
    "uncertainty_handling", "tool_decision_quality", "alternative_quality",
    "verification_quality", "self_correction_quality", "factual_reliability",
    "scope_adherence", "answer_alignment", "efficiency", "meta_noise", "provenance_quality",
)


class QualityError(SchemaError):
    """A quality decision could not be made, so it is UNKNOWN rather than favourable."""


@dataclass(frozen=True)
class QualityScore:
    """Fifteen named scores in ``[0, 1]``. No aggregate drives any decision."""

    scores: dict[str, float] = field(default_factory=dict)
    quality_version: str = QUALITY_VERSION

    @property
    def mean(self) -> float:
        """Reported for humans. Read by nothing — see the module docstring."""
        if not self.scores:
            return 0.0
        return round(sum(self.scores.values()) / len(self.scores), 6)

    def below(self, floors: dict[str, float]) -> tuple[str, ...]:
        """Dimensions scoring below their floor, sorted. A dimension with no floor never fails."""
        return tuple(sorted(name for name, floor in floors.items()
                            if self.scores.get(name, 0.0) < floor))

    def validated(self) -> "QualityScore":
        missing = sorted(set(DIMENSIONS) - set(self.scores))
        if missing:
            raise QualityError(
                f"quality score is missing dimension(s) {missing}; an absent score is not a "
                f"zero and not a pass, so it is refused rather than defaulted")
        out_of_range = sorted(n for n, v in self.scores.items() if not 0.0 <= v <= 1.0)
        if out_of_range:
            raise QualityError(f"quality score(s) outside [0, 1]: {out_of_range}")
        unknown = sorted(set(self.scores) - set(DIMENSIONS))
        if unknown:
            raise QualityError(
                f"quality score names unknown dimension(s) {unknown}; the dimension set is "
                f"closed so a typo cannot create a score nothing has a floor for")
        return self

    def to_dict(self) -> dict:
        return {"quality_version": self.quality_version,
                "scores": {name: round(self.scores.get(name, 0.0), 6) for name in DIMENSIONS},
                "mean_reported_not_used": self.mean}


@dataclass(frozen=True)
class QualityVerdict:
    """The terminal decision for one record, with its reasons (§14)."""

    disposition: Disposition
    score: QualityScore
    reasons: tuple[RejectReason, ...] = ()
    #: Human-readable detail per reason. Body-free.
    notes: tuple[str, ...] = ()
    floors_missed: tuple[str, ...] = ()
    required_missed: tuple[str, ...] = ()

    def validated(self) -> "QualityVerdict":
        if self.disposition is not Disposition.ACCEPT and not self.reasons:
            raise QualityError(
                f"disposition {self.disposition.value} carries no reason code; §14 requires one "
                f"for every non-ACCEPT outcome, because a rejection nobody can explain is a "
                f"rejection nobody can appeal")
        if self.disposition is Disposition.ACCEPT and self.required_missed:
            raise QualityError(
                f"ACCEPT with required dimension(s) below floor {list(self.required_missed)}; a "
                f"required floor is not a preference")
        return self

    def to_dict(self) -> dict:
        return {
            "disposition": self.disposition.value,
            "reasons": [r.value for r in self.reasons],
            "notes": [n[:320] for n in self.notes],
            "floors_missed": list(self.floors_missed),
            "required_missed": list(self.required_missed),
            "score": self.score.to_dict(),
        }


def _ratio(numerator: int, denominator: int) -> float:
    """A bounded ratio where an empty denominator is 0.0, never 1.0.

    "There was nothing to get right" is not the same as "everything was right", and returning 1.0
    for an empty denominator would give a record with no constraints a perfect
    ``constraint_retention`` — rewarding the absence of the thing being measured.
    """
    if denominator <= 0:
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


def score(record: DistilledDecision, conversation: CanonicalConversation, *,
          critique_result: Critique, verification: SourceVerification,
          trace: RecursionTrace) -> QualityScore:
    """Score all fifteen dimensions from observable structure. Deterministic and offline."""
    from .operators import ReasoningOperator

    operators = {name for name in record.decision.reasoning_operators}
    findings = critique_result.findings
    unsupported = sum(1 for f in findings if f.check == "unsupported_text")
    blocking = sum(1 for f in findings if f.severity is Severity.BLOCKING)

    process_turns = [t for t in conversation.turns
                     if t.role.value in ("assistant_reasoning", "assistant_revision")]
    noisy = sum(1 for t in process_turns if t.is_meta_noise)

    # task_understanding — did extraction recover a goal and a stated objective at all?
    task_understanding = 0.0
    if record.task.user_goal_summary:
        task_understanding += 0.6
    if record.task.goal.value != "converse":
        task_understanding += 0.4

    # constraint_retention — did extraction and repair LOSE a constraint the request contained?
    #
    # The denominator is the number of constraint-bearing SPANS in the request, obtained from the
    # extractor's own `_lift`, not a count of matched markers. Counting markers was measured
    # wrong: the marker list contains overlapping entries ("must " is a substring of "must not"),
    # and one sentence can match several while being a single constraint. On the request "Our
    # client must not retry on 4xx. Why does it retry on 429?" that gave a denominator of 2 for
    # one real constraint, scored 0.5, and REJECTED an otherwise strong trace on a required
    # floor. Sharing `_lift` with the extractor also makes the two definitions incapable of
    # drifting apart.
    from .distiller import _CONSTRAINT_MARKERS, _lift
    user_text = "\n".join(t.text for t in conversation.turns
                          if t.role.value == "user_request")
    available = _lift(user_text, _CONSTRAINT_MARKERS, limit=64)
    if not available:
        # No constraint language in the request: nothing to retain, and retaining nothing is
        # correct. Scored 1.0 here — unlike _ratio's empty case — because the measurement is
        # "did it lose a constraint", and there was none to lose.
        constraint_retention = 1.0
    else:
        constraint_retention = _ratio(len(record.task.constraints), len(available))

    # reasoning_relevance — how much of the record survived the grounding check.
    populated = sum(len(v) for v in (
        record.task.constraints, record.epistemic_state.assumptions,
        record.epistemic_state.uncertainties, record.decision.subproblems,
        record.decision.candidate_approaches, record.decision.verification_plan))
    reasoning_relevance = 1.0 if populated == 0 else _ratio(populated - unsupported, populated)

    premise_handling = 1.0 if ReasoningOperator.PREMISE_CHECK.value in operators else 0.5
    if record.epistemic_state.premise_state.value == "contradicted":
        premise_handling = 1.0

    # uncertainty_handling — a freshness-sensitive task that named no uncertainty is penalised;
    # a stable task that named none is not. This is the asymmetry §14 asks for by distinguishing
    # uncertainty_handling from over-deliberation.
    named_uncertainty = bool(record.epistemic_state.uncertainties
                             or record.epistemic_state.unknown)
    if record.epistemic_state.freshness is FreshnessRequirement.STABLE:
        uncertainty_handling = 1.0 if named_uncertainty else 0.7
    else:
        uncertainty_handling = 1.0 if named_uncertainty else 0.2

    # tool_decision_quality — deciding EITHER way explicitly beats not deciding.
    decided_tool = (ReasoningOperator.TOOL_NEEDED.value in operators
                    or ReasoningOperator.TOOL_NOT_NEEDED.value in operators)
    tool_decision_quality = 1.0 if decided_tool else 0.4
    if any(f.check.startswith("tool_") for f in findings):
        tool_decision_quality = min(tool_decision_quality, 0.3)

    alternative_quality = 0.3
    if record.decision.candidate_approaches:
        alternative_quality = 0.7
    if record.decision.rejected_approaches:
        alternative_quality = 1.0

    verification_quality = 0.2
    if record.decision.verification_plan:
        verification_quality = 0.8
    if ReasoningOperator.VERIFY_BEFORE_CLAIM.value in operators:
        verification_quality = 1.0

    # self_correction_quality — floor is 0.0 in config: most traces have nothing to correct, and
    # penalising that would reward traces that made mistakes.
    self_correction_quality = 1.0 if record.outcome.self_corrections else 0.5

    # factual_reliability — the honest measure is whether claims are properly STATUSED, not
    # whether they are true. M67A verifies no external facts (§10).
    if not record.factual_claims:
        factual_reliability = 0.6
    else:
        undecided = sum(1 for link in record.fact_procedure_links if not link.decided)
        factual_reliability = _ratio(len(record.fact_procedure_links) - undecided,
                                     max(len(record.factual_claims), 1))

    scope_adherence = 1.0
    if ReasoningOperator.SCOPE_CONTROL.value in operators:
        scope_adherence = 1.0
    elif blocking:
        scope_adherence = 0.3

    answer_alignment = 1.0 if record.decision.chosen_approach else 0.4

    # efficiency / meta_noise — over-deliberation and self-talk, as §16 asks to measure them.
    total_process_chars = sum(len(t.text) for t in process_turns)
    if total_process_chars == 0:
        efficiency = 0.5
    elif total_process_chars > 8000:
        efficiency = 0.4
    else:
        efficiency = 1.0
    meta_noise = 1.0 if not process_turns else _ratio(len(process_turns) - noisy,
                                                      len(process_turns))

    # provenance_quality — binary in substance. See the module docstring.
    provenance_quality = 1.0 if (verification.verdict is SourceVerdict.VERIFIED
                                 and trace.chain_intact
                                 and record.provenance.source_unit_ids) else 0.0
    if verification.verdict is SourceVerdict.NOT_APPLICABLE:
        # Nothing populated: there is no attribution to have. Not a provenance failure, and not
        # a pass either — the record will fail LOW_INFORMATION instead, which is the accurate
        # finding. Scored 1.0 so the rejection reason names the real problem.
        provenance_quality = 1.0 if trace.chain_intact else 0.0

    return QualityScore(scores={
        "task_understanding": task_understanding,
        "constraint_retention": constraint_retention,
        "reasoning_relevance": reasoning_relevance,
        "premise_handling": premise_handling,
        "uncertainty_handling": uncertainty_handling,
        "tool_decision_quality": tool_decision_quality,
        "alternative_quality": alternative_quality,
        "verification_quality": verification_quality,
        "self_correction_quality": self_correction_quality,
        "factual_reliability": factual_reliability,
        "scope_adherence": scope_adherence,
        "answer_alignment": answer_alignment,
        "efficiency": efficiency,
        "meta_noise": meta_noise,
        "provenance_quality": provenance_quality,
    }).validated()


def disposition(record: DistilledDecision, conversation: CanonicalConversation, *,
                critique_result: Critique, verification: SourceVerification,
                trace: RecursionTrace, config: QualityConfig | None = None,
                duplicate_of: str = "") -> QualityVerdict:
    """The terminal ACCEPT / NEEDS_HUMAN_REVIEW / REJECT decision (§14).

    Note the signature: it receives the critique, the verification and the trace, and **not** a
    scalar. It computes the scores itself and reads individual floors. There is no parameter
    through which an aggregate could arrive and decide anything.
    """
    cfg = config or QualityConfig()
    quality = score(record, conversation, critique_result=critique_result,
                    verification=verification, trace=trace)
    floors_missed = quality.below(cfg.floors)
    required_missed = tuple(d for d in floors_missed if d in cfg.required_dimensions)
    soft_missed = tuple(d for d in floors_missed if d not in cfg.required_dimensions)

    reasons: list[RejectReason] = []
    notes: list[str] = []

    if duplicate_of:
        reasons.append(RejectReason.DUPLICATE)
        notes.append(f"canonical member of this family is {duplicate_of[:16]}")

    if record.privacy.export_status is ExportStatus.EXPORT_UNKNOWN:
        reasons.append(RejectReason.PRIVACY_RISK)
        notes.append("export status is UNKNOWN: no privacy decision exists for this record (§4)")
    if record.privacy.contains_secret_like_data:
        reasons.append(RejectReason.PRIVACY_RISK)
        notes.append("secret-like content in the source; the record is not exportable")

    blocking = critique_result.blocking
    if blocking:
        checks = sorted({f.check for f in blocking})
        if "reasoning_invented" in checks:
            reasons.append(RejectReason.UNSUPPORTED_DECISION)
            notes.append("decision fields were populated from a source with no reasoning turn")
        if "claim_marked_verified" in checks:
            reasons.append(RejectReason.FACT_REASONING_ENTANGLED)
            notes.append("a factual claim was marked VERIFIED, which M67A cannot establish (§10)")
        if any(c.startswith("provenance_") for c in checks):
            reasons.append(RejectReason.UNKNOWN_SOURCE)
            notes.append("the record cites source that cannot be re-read")
        if "privacy_weaker_than_source" in checks or "privacy_undecided" in checks:
            if RejectReason.PRIVACY_RISK not in reasons:
                reasons.append(RejectReason.PRIVACY_RISK)
            notes.append("the record's privacy status is weaker than its source's")
        if not reasons:
            reasons.append(RejectReason.UNSUPPORTED_DECISION)
            notes.append(f"blocking critique finding(s): {checks[:3]}")

    if conversation.review_status.value == "malformed":
        reasons.append(RejectReason.MALFORMED_EXPORT)
        notes.append("the source export could not be read coherently")

    if not conversation.has_reasoning():
        reasons.append(RejectReason.INSUFFICIENT_CONTEXT)
        notes.append("no assistant reasoning turn: there is no procedure in the source to distil")

    if any(f.check == "all_reasoning_is_meta_noise" for f in critique_result.findings):
        reasons.append(RejectReason.META_POLICY_NOISE)
        notes.append("every assistant process turn is marked meta-noise (§13)")

    if verification.verdict is SourceVerdict.NOT_APPLICABLE:
        reasons.append(RejectReason.LOW_INFORMATION)
        notes.append("no field was populated: the record carries no recoverable decision")
    elif verification.verdict is SourceVerdict.FAILED:
        reasons.append(RejectReason.UNSUPPORTED_DECISION)
        notes.append("no field could be attributed to any single source turn")
    elif verification.verdict is SourceVerdict.PARTIAL:
        reasons.append(RejectReason.VERIFICATION_MISSING)
        notes.append(f"{len(verification.orphans)} field(s) have no single source witness; "
                     f"PARTIAL is not a pass (§22)")

    if trace.exhausted:
        reasons.append(RejectReason.RECURSION_EXHAUSTED)
        notes.append(f"material findings remained after {trace.max_depth} repair pass(es); the "
                     f"depth cap is not an acceptance (§12)")
    if not trace.chain_intact:
        reasons.append(RejectReason.UNKNOWN_SOURCE)
        notes.append("the recursion chain has a gap, so what the record was repaired from is "
                     "not recoverable (§22)")

    if any(f.check == "contradictory_trace" for f in critique_result.findings):
        reasons.append(RejectReason.CONTRADICTORY_TRACE)
        notes.append("the trace asserts a claim and its negation with no self-correction; which "
                     "one it meant is a human's reading (§18)")

    residual = critique_result.material()
    undecided_links = [f for f in residual if f.check in ("fact_procedure_link_undecided",
                                                          "stale_claim_unlinked")]
    if undecided_links:
        reasons.append(RejectReason.FACT_REASONING_ENTANGLED)
        notes.append(f"{len(undecided_links)} fact/procedure link(s) undecided: whether the "
                     f"procedure depends on a stale claim is a reading of the source (§10)")

    unresolved_other = [f for f in residual
                        if f not in undecided_links and f.severity is not Severity.BLOCKING
                        and f.check != "contradictory_trace"]
    if unresolved_other:
        reasons.append(RejectReason.CRITIC_DISAGREEMENT)
        notes.append(f"{len(unresolved_other)} material finding(s) survived repair: "
                     f"{sorted({f.check for f in unresolved_other})[:3]}")

    if required_missed:
        reasons.append(RejectReason.QUALITY_FLOOR)
        notes.append(f"required dimension(s) below floor: {list(required_missed)}")
    elif len(soft_missed) > cfg.max_soft_floor_misses:
        reasons.append(RejectReason.QUALITY_FLOOR)
        notes.append(f"{len(soft_missed)} soft dimension(s) below floor, above the "
                     f"{cfg.max_soft_floor_misses} permitted: {list(soft_missed)}")

    # Deduplicate reasons while preserving first-seen order: a reason listed twice reads as two
    # findings, and the note list already carries the detail for each occurrence.
    seen: set[RejectReason] = set()
    ordered_reasons = tuple(r for r in reasons if not (r in seen or seen.add(r)))

    # REJECT is reserved for records that are WRONG. Everything that merely cannot be decided by
    # a machine — an undecided link, a surviving disagreement, an exhausted recursion — routes to
    # review, because §12 and §14 both say so and because discarding those records would throw
    # away the ones most worth a human's attention.
    reject_reasons = frozenset({
        RejectReason.DUPLICATE, RejectReason.PRIVACY_RISK, RejectReason.MALFORMED_EXPORT,
        RejectReason.UNKNOWN_SOURCE, RejectReason.UNSUPPORTED_DECISION,
        RejectReason.INSUFFICIENT_CONTEXT, RejectReason.META_POLICY_NOISE,
        RejectReason.LOW_INFORMATION, RejectReason.QUALITY_FLOOR,
    })
    if any(r in reject_reasons for r in ordered_reasons):
        final = Disposition.REJECT
    elif ordered_reasons:
        final = Disposition.NEEDS_HUMAN_REVIEW
    else:
        final = Disposition.ACCEPT

    return QualityVerdict(
        disposition=final, score=quality, reasons=ordered_reasons, notes=tuple(notes),
        floors_missed=floors_missed, required_missed=required_missed).validated()


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {
        "quality_version": QUALITY_VERSION,
        "dimensions": list(DIMENSIONS),
        "aggregate_used_in_decision": False,
        "reason_codes": [r.value for r in RejectReason],
        "calibration": "NOT_CALIBRATED. These scores are derived from observable record "
                       "structure and are NOT validated against human judgement. They filter "
                       "for trustworthiness during corpus construction; they do not measure "
                       "whether the historical reasoning was good, and no claim in §26 rests "
                       "on them",
    }


__all__ = [
    "DIMENSIONS", "QUALITY_VERSION", "QualityError", "QualityScore", "QualityVerdict",
    "disposition", "score", "versions",
]
