"""reasoning_distillation/skills.py — V69 M67A: skills, and the evidence bar they must clear.

WHAT A SKILL IS, AND WHY IT IS NOT A DATASET ROW
------------------------------------------------
A distilled record says *"in this conversation, the assistant checked the premise before
answering"*. A skill says *"checking the premise before answering is a thing worth doing, here is
when, here is how, and here is how it fails"*. The first is an observation; the second is a claim
about what generalises.

§15 keeps them in separate layers, and adds the line that shapes this whole module:

    **Never create a skill from one anecdotal trace. Require repeated evidence across multiple
    ACCEPT examples.**

That is not a quality preference. A single trace cannot distinguish "this is a good procedure"
from "this worked once, here". This repository's own history is the argument: it has a standing
rule against reading one measurement as a pattern, and a defect recorded because a synthetic rate
was cited as calibration. A skill derived from one example would be exactly that mistake, wearing
the word "skill".

THREE INDEPENDENT SUPPORT THRESHOLDS
------------------------------------
:class:`~reasoning_distillation.config.SkillConfig` declares three, and they are independent
because they fail in different ways — any ONE of them alone is gameable by a corpus that repeats
itself:

  * ``min_supporting_examples`` — raw count. Defeated by three near-copies of one conversation.
  * ``min_supporting_families`` — distinct PROBLEM families, using the same family ids the split
    uses. This is the one that actually closes the near-copy hole: duplicates share a family by
    construction (:mod:`reasoning_distillation.dedupe`), so three copies of one conversation are
    one family and cannot clear a threshold of two.
  * ``min_supporting_source_files`` — distinct source FILES. One export is one witness: if every
    supporting example came out of a single file, the "repeated evidence" is one person's one
    session, and the skill is marked ``PROVISIONAL`` rather than ``SUPPORTED``.

A skill that misses any threshold is not silently dropped. It is emitted with
:attr:`SkillStatus.INSUFFICIENT_EVIDENCE` and its actual support counts, because "we saw this
operator twice and need one more example" is useful information and a missing skill file is not.

WHAT A SKILL FILE MAY NOT CONTAIN
---------------------------------
§15: *"no long raw traces inside skill files"*. :meth:`ReasoningSkill.to_dict` emits source
example HASHES and never text, and the whole payload passes
:func:`reasoning_distillation.privacy.assert_body_free_payload` before it is written. The reason is
concrete rather than procedural: the skill layer is the part of this corpus most likely to be
shown to someone, pasted into a document, or committed — and it derives from private
conversations.

THE SEPARATION FROM WEIGHTS
---------------------------
§15 also requires this layer stay separate from any future learned model weights. Nothing here
produces a training example, and the module has no export path into one. A skill is a stated
procedure with its evidence attached; whether it is ever used by prompting, by a check, or by
training is a decision for a later milestone with its own authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from training_gym.schemas import SchemaError, sha256_obj

from .config import SkillConfig
from .models import Disposition, DistilledDecision
from .operators import BY_OPERATOR, ReasoningOperator
from .quality import QualityVerdict

#: Bump when derivation or the skill record shape changes. Recorded in every manifest.
SKILLS_VERSION = "m67a.skills.1"


class SkillError(SchemaError):
    """A skill could not be derived. Never a skill with unstated support."""


class SkillStatus(str, Enum):
    """How much the evidence supports this skill. ``SUPPORTED`` is the only full status.

    There is no member meaning "probably generalises". A skill is supported by evidence that
    clears three declared thresholds, provisional when it clears the example and family bars from
    a single source file, or explicitly insufficient. Anything else would be a confidence claim
    with nothing behind it.
    """

    SUPPORTED = "supported"
    PROVISIONAL = "provisional"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    @property
    def is_usable(self) -> bool:
        return self is SkillStatus.SUPPORTED


@dataclass(frozen=True)
class SkillSupport:
    """The evidence behind one skill, as counts and hashes. Never as text."""

    example_count: int
    family_count: int
    source_file_count: int
    example_ids: tuple[str, ...] = ()
    #: Content addresses of the supporting records, so a reviewer can re-read each one locally.
    example_hashes: tuple[str, ...] = ()
    family_ids: tuple[str, ...] = ()
    source_file_hashes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "example_count": self.example_count,
            "family_count": self.family_count,
            "source_file_count": self.source_file_count,
            "example_ids": sorted(self.example_ids),
            "example_hashes": sorted(self.example_hashes),
            "family_ids": sorted(self.family_ids),
            "source_file_hashes": sorted(self.source_file_hashes),
        }


@dataclass(frozen=True)
class ReasoningSkill:
    """One reusable reasoning skill with its evidence (§15)."""

    skill_id: str
    operator: ReasoningOperator
    purpose: str
    trigger_conditions: tuple[str, ...]
    procedure: tuple[str, ...]
    failure_modes: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    support: SkillSupport
    status: SkillStatus
    #: The JARVIS decision field this skill informs, or "" when there is no counterpart (§3).
    jarvis_dimension: str = ""
    #: Why the status is what it is. Always populated for a non-SUPPORTED status.
    status_reason: str = ""
    skills_version: str = SKILLS_VERSION

    def validated(self) -> "ReasoningSkill":
        if self.status is not SkillStatus.SUPPORTED and not self.status_reason:
            raise SkillError(
                f"{self.skill_id}: status {self.status.value} with no status_reason; a skill that "
                f"is not usable must say why, or a reader cannot tell what would fix it")
        if not self.support.example_ids:
            raise SkillError(
                f"{self.skill_id}: no supporting example ids. A skill whose evidence cannot be "
                f"re-read is not evidence-backed, whatever its counts say")
        if self.support.example_count != len(set(self.support.example_ids)):
            raise SkillError(
                f"{self.skill_id}: example_count {self.support.example_count} does not match "
                f"{len(set(self.support.example_ids))} distinct example ids")
        return self

    def to_dict(self) -> dict:
        """The skill file's content. Hashes and stated procedure only — never a raw trace (§15)."""
        return {
            "skills_version": self.skills_version,
            "skill_id": self.skill_id,
            "operator": self.operator.value,
            "purpose": self.purpose,
            "jarvis_dimension": self.jarvis_dimension,
            "trigger_conditions": list(self.trigger_conditions),
            "procedure": list(self.procedure),
            "failure_modes": list(self.failure_modes),
            "stop_conditions": list(self.stop_conditions),
            "status": self.status.value,
            "status_reason": self.status_reason[:320],
            "support": self.support.to_dict(),
        }


#: Per-operator skill content. Deliberately AUTHORED here rather than generated from the traces,
#: and the distinction matters: the traces supply the EVIDENCE that an operator recurs, while the
#: procedure, failure modes and stop conditions are a stated claim this repository is making and
#: can be held to. Generating them from historical prose would produce a skill file whose wording
#: came from a model and whose authority came from nowhere — and §1 is explicit that the goal is
#: the reusable procedure, not the source's phrasing.
_SKILL_CONTENT: dict[ReasoningOperator, dict] = {
    ReasoningOperator.PREMISE_CHECK: {
        "trigger_conditions": (
            "the request asserts a fact as the basis for its question",
            "the asserted fact is checkable from the repository, a tool or the request itself",
        ),
        "procedure": (
            "isolate the assertion the question rests on, separately from the question",
            "decide whether the assertion is checkable here, and with what",
            "check it before answering the question that assumes it",
            "when it is false, answer the corrected question and say what was corrected",
            "when it cannot be checked, say the answer is conditional on it",
        ),
        "failure_modes": (
            "answering the question as asked and inheriting its false premise",
            "correcting the premise and then not answering the underlying need",
            "treating an unverifiable premise as false rather than as unknown",
        ),
        "stop_conditions": (
            "the premise is confirmed, corrected, or explicitly recorded as unverifiable",
        ),
    },
    ReasoningOperator.FRESHNESS_CHECK: {
        "trigger_conditions": (
            "the answer's truth depends on current state: a version, a price, a branch, a running process",
            "the request uses latest / current / now, or names a fast-moving artifact",
        ),
        "procedure": (
            "decide whether the claim is time-sensitive before deciding how to answer it",
            "identify what observation would establish the current state",
            "make that observation, or state plainly that the claim is as-of an unknown date",
            "never present a remembered value as a current one",
        ),
        "failure_modes": (
            "asserting a remembered version as current",
            "treating freshness as a web-only concern when a repository or host read would settle it",
            "hedging every claim instead of the time-sensitive one",
        ),
        "stop_conditions": (
            "current state was observed, or the answer is explicitly marked as not current",
        ),
    },
    ReasoningOperator.TOOL_NOT_NEEDED: {
        "trigger_conditions": (
            "a tool is available and the task is answerable from what is already known",
            "the cost of the tool call exceeds what its result would change",
        ),
        "procedure": (
            "separate tool AVAILABILITY from tool NEED",
            "ask what the tool's result would change about the answer",
            "when the answer is nothing, answer directly and say why no tool was needed",
        ),
        "failure_modes": (
            "calling a tool because it is registered rather than because it is needed",
            "declining a tool the answer actually depends on, and asserting the gap instead",
        ),
        "stop_conditions": (
            "the answer is complete without the tool, or the need is established and the tool runs",
        ),
    },
    ReasoningOperator.VERIFY_BEFORE_CLAIM: {
        "trigger_conditions": (
            "the answer will assert that something works, passes, or happened",
            "an action with an external effect was attempted",
        ),
        "procedure": (
            "name the check that would make the claim true before making it",
            "run the check",
            "report the check's actual outcome, including when it failed",
            "when no check ran, state the claim as unverified rather than omitting the caveat",
        ),
        "failure_modes": (
            "inferring success from the absence of an error",
            "reporting the intended outcome instead of the observed one",
            "verifying something adjacent to the claim and reporting it as the claim",
        ),
        "stop_conditions": (
            "the check ran and its outcome is reported, or the claim is marked unverified",
        ),
    },
    ReasoningOperator.UNCERTAINTY_IDENTIFICATION: {
        "trigger_conditions": (
            "the request is under-specified in a way that changes the answer",
            "two readings of the request lead to materially different work",
        ),
        "procedure": (
            "name what is not known, specifically, rather than hedging generally",
            "decide whether the unknown changes the work or only its presentation",
            "when it changes the work, ask; when it does not, state the assumption and proceed",
        ),
        "failure_modes": (
            "asking about an ambiguity that does not change the work",
            "proceeding silently on a reading that a reader would not have chosen",
            "replacing a specific unknown with a general disclaimer",
        ),
        "stop_conditions": (
            "every unknown is either resolved, stated as an assumption, or asked about",
        ),
    },
    ReasoningOperator.DECOMPOSITION: {
        "trigger_conditions": (
            "the task has parts that can be solved and checked independently",
            "a single answer would have to be right about several things at once",
        ),
        "procedure": (
            "split on what can be VERIFIED separately, not on what reads as separate topics",
            "order the parts so an earlier answer constrains a later one",
            "solve and check each part before composing",
        ),
        "failure_modes": (
            "decomposing into parts that cannot be checked independently",
            "decomposition as presentation: headings over a single undivided argument",
            "losing a cross-part constraint that only the whole satisfies",
        ),
        "stop_conditions": (
            "every part is answered and the composition is checked against the original request",
        ),
    },
    ReasoningOperator.SELF_CORRECTION: {
        "trigger_conditions": (
            "new information contradicts something already asserted in this episode",
            "a check fails on a claim already made",
        ),
        "procedure": (
            "state the correction plainly and once",
            "correct the downstream conclusions the error reached, not only the sentence",
            "do not re-derive the whole episode around the correction",
        ),
        "failure_modes": (
            "correcting the wording while leaving the wrong conclusion standing",
            "an apology in place of a correction",
            "cascading self-correction that never converges",
        ),
        "stop_conditions": (
            "the corrected claim and everything that depended on it are consistent",
        ),
    },
    ReasoningOperator.CONSTRAINT_EXTRACTION: {
        "trigger_conditions": (
            "the request states limits the answer must respect",
            "the domain implies limits the request does not state",
        ),
        "procedure": (
            "collect stated constraints verbatim before designing anything",
            "name implied constraints explicitly, as implied",
            "check the final answer against each constraint individually",
        ),
        "failure_modes": (
            "satisfying the request while violating a constraint stated in it",
            "inventing a constraint and then optimising for it",
            "dropping a constraint during a later revision",
        ),
        "stop_conditions": (
            "every collected constraint is individually checked against the answer",
        ),
    },
    ReasoningOperator.SCOPE_CONTROL: {
        "trigger_conditions": (
            "an adjacent improvement is visible while doing the requested work",
            "the requested change touches code with other problems in it",
        ),
        "procedure": (
            "deliver what was asked, completely",
            "name the adjacent problem without fixing it",
            "let the decision to widen be the requester's",
        ),
        "failure_modes": (
            "widening the change and reporting it as the requested one",
            "narrowing the change because part of it was harder than expected",
            "withholding a finding because acting on it was out of scope",
        ),
        "stop_conditions": (
            "the requested scope is complete and anything left out is named",
        ),
    },
    ReasoningOperator.STOP_CONDITION: {
        "trigger_conditions": (
            "the task has no natural end, such as research, review or optimisation",
            "further work would produce diminishing or unverifiable improvement",
        ),
        "procedure": (
            "state what will count as done before starting",
            "check against that statement rather than against how much work remains possible",
            "stop when it is met, and say so",
        ),
        "failure_modes": (
            "continuing because more is findable rather than because more is needed",
            "stopping at the first plausible answer and calling that the condition",
            "a stop condition that cannot be checked",
        ),
        "stop_conditions": (
            "the stated condition is met and the check that it is met is reported",
        ),
    },
}


def derive(results: "list[tuple[DistilledDecision, QualityVerdict]]", *,
           family_of: "dict[str, str] | None" = None,
           config: SkillConfig | None = None) -> tuple[ReasoningSkill, ...]:
    """Derive skills from ACCEPT records only, with their evidence attached (§15).

    Only ``ACCEPT`` records contribute support. A record routed to review has not been shown to be
    faithful to its source, and a skill built on unreviewed evidence would inherit that
    uncertainty while presenting itself as a stated procedure.

    ``family_of`` maps example id to family id; when absent, each record's own
    :attr:`~reasoning_distillation.models.DistilledProvenance.family_id` is used. The parameter
    exists so a caller holding the dedupe report can supply the authoritative mapping rather than
    trusting a field that could have been set before grouping settled.
    """
    cfg = config or SkillConfig()
    accepted = [(record, verdict) for record, verdict in results
                if verdict.disposition is Disposition.ACCEPT]

    by_operator: dict[ReasoningOperator, list[DistilledDecision]] = {}
    for record, _ in accepted:
        for name in record.decision.reasoning_operators:
            try:
                operator = ReasoningOperator(name)
            except ValueError:  # pragma: no cover - validated() refuses unknown operators
                continue
            by_operator.setdefault(operator, []).append(record)

    skills: list[ReasoningSkill] = []
    for operator in sorted(by_operator, key=lambda op: op.value):
        content = _SKILL_CONTENT.get(operator)
        if content is None:
            # An operator that recurs but has no authored skill content. Deliberately skipped
            # rather than auto-generated: a skill file whose procedure was synthesised from
            # historical prose would have a model's wording and nobody's authority. The gap is
            # reported by `coverage()` so it is visible instead of silent.
            continue
        records = sorted(by_operator[operator], key=lambda r: r.example_id)
        example_ids = tuple(r.example_id for r in records)
        families = tuple(sorted({(family_of or {}).get(r.example_id, r.provenance.family_id)
                                 for r in records}))
        source_files = tuple(sorted({h for r in records
                                     for h in r.provenance.source_file_hashes}))
        support = SkillSupport(
            example_count=len(set(example_ids)),
            family_count=len(families),
            source_file_count=len(source_files),
            example_ids=example_ids,
            example_hashes=tuple(sorted(r.artifact_hash for r in records)),
            family_ids=families,
            source_file_hashes=source_files,
        )

        shortfalls: list[str] = []
        if support.example_count < cfg.min_supporting_examples:
            shortfalls.append(f"{support.example_count} example(s), needs "
                              f"{cfg.min_supporting_examples}")
        if support.family_count < cfg.min_supporting_families:
            shortfalls.append(f"{support.family_count} problem famil(y/ies), needs "
                              f"{cfg.min_supporting_families} — duplicates share a family, so "
                              f"repeated copies of one conversation cannot clear this")
        if shortfalls:
            status = SkillStatus.INSUFFICIENT_EVIDENCE
            reason = "; ".join(shortfalls)
        elif support.source_file_count < cfg.min_supporting_source_files:
            status = SkillStatus.PROVISIONAL
            reason = (f"support comes from {support.source_file_count} source file(s), needs "
                      f"{cfg.min_supporting_source_files}; one export is one witness")
        else:
            status = SkillStatus.SUPPORTED
            reason = ""

        spec = BY_OPERATOR[operator]
        skills.append(ReasoningSkill(
            skill_id=f"m67a-skill-{operator.value.lower()}",
            operator=operator,
            purpose=spec.purpose,
            trigger_conditions=content["trigger_conditions"],
            procedure=content["procedure"],
            failure_modes=content["failure_modes"],
            stop_conditions=content["stop_conditions"],
            support=support,
            status=status,
            jarvis_dimension=spec.jarvis_dimension,
            status_reason=reason,
        ).validated())
    return tuple(skills)


def coverage() -> dict:
    """Which operators have authored skill content, and which do not.

    The gap is published rather than hidden: an operator that recurs across many ACCEPT examples
    and has no skill content is a piece of work this milestone did not do, and naming it is more
    useful than a silently shorter skill library.
    """
    authored = sorted(op.value for op in _SKILL_CONTENT)
    return {
        "authored_count": len(authored),
        "authored": authored,
        "unauthored": sorted(op.value for op in ReasoningOperator if op not in _SKILL_CONTENT),
        "note": "skill CONTENT is authored in this repository and held to; the corpus supplies "
                "only the EVIDENCE that an operator recurs. Content is never synthesised from "
                "historical prose",
    }


def library_digest(skills: "tuple[ReasoningSkill, ...]") -> str:
    """Content address of a whole skill library, for the manifest."""
    return sha256_obj({"skills": [s.to_dict() for s in
                                  sorted(skills, key=lambda s: s.skill_id)]})


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {"skills_version": SKILLS_VERSION, "statuses": [s.value for s in SkillStatus],
            "support_thresholds": ["min_supporting_examples", "min_supporting_families",
                                   "min_supporting_source_files"],
            "accept_only": True, "separate_from_weights": True, "coverage": coverage()}


__all__ = [
    "SKILLS_VERSION", "ReasoningSkill", "SkillError", "SkillStatus", "SkillSupport",
    "coverage", "derive", "library_digest", "versions",
]
