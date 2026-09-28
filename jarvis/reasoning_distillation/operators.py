"""reasoning_distillation/operators.py — V69 M67A: the closed reasoning-operator taxonomy.

WHAT AN OPERATOR IS
-------------------
A reusable COGNITIVE MOVE — "check whether the premise holds before answering it", "decide
whether this needs a tool" — as distinct from three things it is often confused with:

  * a *topic*. ``SECURITY_ANALYSIS`` is a goal, not an operator; it says what the task was
    about, not what thinking happened.
  * a *style*. "explained clearly with bullet points" is not an operator. §1 is explicit that
    the goal is not to make JARVIS talk like Claude.
  * an *outcome*. "got the right answer" is not an operator either. An operator can be applied
    well in a trace that reaches a wrong conclusion, and that trace is still useful — which is
    the whole reason §10 separates fact from procedure.

WHY THE SET IS CLOSED
---------------------
§1 supplies the list, and this module implements it as a closed enum rather than free text for
a reason that shows up immediately in §15: a skill may only be derived when the same operator
appears across several independent examples. If operators were strings, then ``"premise
check"``, ``"PREMISE_CHECK"`` and ``"checked the premise"`` would be three operators with one
example each, no skill would ever reach its support threshold, and the failure would look like
"the corpus is too small" rather than "the taxonomy leaked".

:meth:`~reasoning_distillation.models.DistilledDecision.validated` therefore refuses an
unknown operator outright.

EACH OPERATOR NAMES ITS JARVIS DIMENSION
----------------------------------------
:attr:`OperatorSpec.jarvis_dimension` points at the field in JARVIS's existing decision
architecture that the operator informs — ``epistemic.freshness_requirement``,
``epistemic.tool_need``, ``decision.verification_status`` and so on. That mapping is §3's
requirement made concrete, and it is what a future M67B experiment would actually compare: for
a given historical example, did the distilled procedure reach the same conclusion on that
dimension as :func:`core.epistemic_deliberation.deliberate` would have?

An operator whose dimension is ``""`` has no JARVIS counterpart yet. That is recorded honestly
rather than mapped to the nearest field: a forced mapping would make the comparison meaningless
in exactly the places it is most interesting.

ABOUT DETECTION
---------------
:func:`detect` is a deterministic, marker-based classifier and it is **evidence, not ground
truth**. It exists so that the offline pipeline can work with no model at all (§19) and so that
a model-assisted extraction has something to be checked against. Its precision is deliberately
biased toward recall on the *presence* side and it never asserts ABSENCE: :func:`detect` finding
no ``VERIFY_BEFORE_CLAIM`` marker means "no marker matched", which the quality gate reports as
unverified rather than as "the trace failed to verify". The distinction matters because the
alternative would score a trace that verified in unusual wording as having skipped verification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: Bump when a member is added or removed, or when a marker set changes materially. Recorded in
#: every manifest (§20): two corpora built under different taxonomies are not comparable, and
#: the version is how a reader knows.
OPERATOR_TAXONOMY_VERSION = "m67a.operators.1"


class ReasoningOperator(str, Enum):
    """The 26 operators §1 names. Closed."""

    TASK_CLASSIFICATION = "TASK_CLASSIFICATION"
    GOAL_EXTRACTION = "GOAL_EXTRACTION"
    CONSTRAINT_EXTRACTION = "CONSTRAINT_EXTRACTION"
    PREMISE_CHECK = "PREMISE_CHECK"
    ASSUMPTION_CHECK = "ASSUMPTION_CHECK"
    FRESHNESS_CHECK = "FRESHNESS_CHECK"
    UNCERTAINTY_IDENTIFICATION = "UNCERTAINTY_IDENTIFICATION"
    CONTEXT_PRIORITY = "CONTEXT_PRIORITY"
    TOOL_NEEDED = "TOOL_NEEDED"
    TOOL_NOT_NEEDED = "TOOL_NOT_NEEDED"
    TOOL_SELECTION = "TOOL_SELECTION"
    RETRIEVAL_DECISION = "RETRIEVAL_DECISION"
    DECOMPOSITION = "DECOMPOSITION"
    ALTERNATIVE_GENERATION = "ALTERNATIVE_GENERATION"
    TRADEOFF_ANALYSIS = "TRADEOFF_ANALYSIS"
    PLAN_FORMATION = "PLAN_FORMATION"
    PLAN_REVISION = "PLAN_REVISION"
    EVIDENCE_CHECK = "EVIDENCE_CHECK"
    VERIFY_BEFORE_CLAIM = "VERIFY_BEFORE_CLAIM"
    TEST_PLAN = "TEST_PLAN"
    FAILURE_ANALYSIS = "FAILURE_ANALYSIS"
    SELF_CORRECTION = "SELF_CORRECTION"
    SCOPE_CONTROL = "SCOPE_CONTROL"
    RISK_CHECK = "RISK_CHECK"
    STOP_CONDITION = "STOP_CONDITION"
    ANSWER_STRUCTURE = "ANSWER_STRUCTURE"


@dataclass(frozen=True)
class OperatorSpec:
    """One operator's definition, its JARVIS mapping and its detection markers."""

    operator: ReasoningOperator
    purpose: str
    #: The JARVIS decision field this operator informs, or "" when there is no counterpart.
    jarvis_dimension: str
    #: Deterministic English/Spanish markers. Evidence of PRESENCE only; never of absence.
    markers: tuple[str, ...]
    #: True when a marker alone is weak evidence and the extractor should require a second
    #: signal. Recorded so the threshold is visible rather than encoded in the marker list.
    weak_markers: bool = False

    def to_dict(self) -> dict:
        return {"operator": self.operator.value, "purpose": self.purpose,
                "jarvis_dimension": self.jarvis_dimension,
                "marker_count": len(self.markers), "weak_markers": self.weak_markers}


#: The taxonomy. One entry per member; :func:`_assert_complete` proves that at import time.
TAXONOMY: tuple[OperatorSpec, ...] = (
    OperatorSpec(
        ReasoningOperator.TASK_CLASSIFICATION,
        "Decide what KIND of task this is before deciding how to approach it.",
        "task_decision.domain", ("this is a", "this looks like a", "the task is", "se trata de",
                                 "esto es un", "kind of problem", "type of question")),
    OperatorSpec(
        ReasoningOperator.GOAL_EXTRACTION,
        "Name the outcome the operator actually wants, distinct from what they literally asked.",
        "epistemic.intent.goal", ("what they want", "what the user wants", "the goal is",
                                  "they are asking for", "lo que quiere", "el objetivo es",
                                  "actually asking", "really asking")),
    OperatorSpec(
        ReasoningOperator.CONSTRAINT_EXTRACTION,
        "Collect the limits the answer must respect, including implicit ones.",
        "epistemic.intent.constraints", ("constraint", "must not", "cannot use", "without using",
                                         "requirement is", "has to", "restricción", "sin usar",
                                         "limited to", "only using")),
    OperatorSpec(
        ReasoningOperator.PREMISE_CHECK,
        "Test whether the question's own assertion is true before answering it.",
        "epistemic.premise_state", ("premise", "assumes that", "is that actually",
                                    "but that is not", "that is incorrect", "presupone",
                                    "no es cierto", "the question assumes", "false premise")),
    OperatorSpec(
        ReasoningOperator.ASSUMPTION_CHECK,
        "Surface the assumptions the ANSWER will rest on, and mark them as assumptions.",
        "epistemic.intent.assumptions", ("i am assuming", "i'm assuming", "assuming that",
                                         "if we assume", "supongo que", "asumiendo",
                                         "my assumption")),
    OperatorSpec(
        ReasoningOperator.FRESHNESS_CHECK,
        "Notice that the answer's truth is time-sensitive and that current state is needed.",
        "epistemic.freshness_requirement", ("may have changed", "as of", "current version",
                                            "latest version", "might be out of date",
                                            "my knowledge cutoff", "puede haber cambiado",
                                            "versión actual", "need to check the current")),
    OperatorSpec(
        ReasoningOperator.UNCERTAINTY_IDENTIFICATION,
        "State what is not known, rather than answering past the gap.",
        "epistemic.intent.confidence", ("i am not sure", "i'm not sure", "unclear",
                                        "i do not know", "i don't know", "uncertain",
                                        "no estoy seguro", "no sé", "it is ambiguous",
                                        "cannot tell from")),
    OperatorSpec(
        ReasoningOperator.CONTEXT_PRIORITY,
        "Decide which of several competing context sources governs.",
        "", ("takes precedence", "overrides", "more authoritative", "the user said",
             "tiene prioridad", "prevalece", "later instruction")),
    OperatorSpec(
        ReasoningOperator.TOOL_NEEDED,
        "Conclude that the turn cannot be answered without an external observation.",
        "epistemic.tool_need", ("i need to check", "i should look up", "need to search",
                                "have to read the file", "necesito comprobar", "debo buscar",
                                "let me check", "requires reading")),
    OperatorSpec(
        ReasoningOperator.TOOL_NOT_NEEDED,
        "Conclude that a tool is available but unnecessary — the harder of the two calls.",
        "epistemic.tool_need", ("no need to search", "i can answer directly",
                                "without looking", "do not need a tool", "no necesito buscar",
                                "puedo responder directamente", "from first principles")),
    OperatorSpec(
        ReasoningOperator.TOOL_SELECTION,
        "Choose among available tools, and say why that one.",
        "", ("better tool for", "use grep instead", "rather than using", "i will use the",
             "mejor herramienta", "en lugar de usar")),
    OperatorSpec(
        ReasoningOperator.RETRIEVAL_DECISION,
        "Decide whether and what to retrieve before reasoning.",
        "epistemic.evidence_policy", ("retrieve", "look up the docs", "read the source",
                                      "search for", "buscar en", "consultar la documentación",
                                      "need the documentation")),
    OperatorSpec(
        ReasoningOperator.DECOMPOSITION,
        "Break the problem into parts that can be solved and checked separately.",
        "epistemic.deliberation_mode", ("break this into", "first,", "step 1", "sub-problem",
                                        "subproblem", "two parts", "dividir en", "primero,",
                                        "paso 1", "there are three")),
    OperatorSpec(
        ReasoningOperator.ALTERNATIVE_GENERATION,
        "Produce more than one candidate approach before choosing.",
        "", ("another option", "alternatively", "option a", "option 1", "two approaches",
             "otra opción", "alternativamente", "could also", "one approach")),
    OperatorSpec(
        ReasoningOperator.TRADEOFF_ANALYSIS,
        "Compare candidates on named axes rather than asserting a winner.",
        "", ("trade-off", "tradeoff", "faster but", "simpler but", "at the cost of",
             "compromiso", "más rápido pero", "downside", "advantage is")),
    OperatorSpec(
        ReasoningOperator.PLAN_FORMATION,
        "Commit to an ordered approach before acting.",
        "task_decision.requires_planning", ("the plan is", "i will first", "my approach",
                                            "el plan es", "mi enfoque", "here is the plan")),
    OperatorSpec(
        ReasoningOperator.PLAN_REVISION,
        "Change the plan on new information, and say what changed it.",
        "", ("that will not work", "changing approach", "instead of", "revise the plan",
             "eso no funcionará", "cambiar de enfoque", "let me reconsider", "on reflection")),
    OperatorSpec(
        ReasoningOperator.EVIDENCE_CHECK,
        "Ask what supports a claim before relying on it.",
        "epistemic.evidence_policy", ("what evidence", "how do i know", "is there support",
                                      "qué evidencia", "cómo sé", "based on what",
                                      "no evidence for")),
    OperatorSpec(
        ReasoningOperator.VERIFY_BEFORE_CLAIM,
        "Confirm the thing before asserting it happened or is true.",
        "decision.verification_status", ("let me verify", "i should confirm", "before claiming",
                                         "run the test first", "verificar", "confirmar antes",
                                         "check that it actually", "make sure it")),
    OperatorSpec(
        ReasoningOperator.TEST_PLAN,
        "Say how the result will be checked, concretely.",
        "", ("test that", "write a test", "the test should", "probar que", "escribir un test",
             "verify by running", "check by")),
    OperatorSpec(
        ReasoningOperator.FAILURE_ANALYSIS,
        "Reason from a symptom to a cause rather than to a fix.",
        "", ("root cause", "why it fails", "the error says", "traceback shows", "causa raíz",
             "por qué falla", "this fails because", "the reason it")),
    OperatorSpec(
        ReasoningOperator.SELF_CORRECTION,
        "Detect and repair one's own earlier error inside the same episode.",
        "", ("i was wrong", "correction:", "actually, that", "i made a mistake",
             "me equivoqué", "corrección:", "let me correct", "that was incorrect")),
    OperatorSpec(
        ReasoningOperator.SCOPE_CONTROL,
        "Refuse to widen the task beyond what was asked.",
        "", ("out of scope", "beyond what was asked", "not going to change",
             "fuera del alcance", "más allá de lo pedido", "sticking to", "only what")),
    OperatorSpec(
        ReasoningOperator.RISK_CHECK,
        "Notice that an action is destructive, irreversible or security-relevant.",
        "task_decision.security_sensitive", ("this is destructive", "irreversible",
                                             "cannot be undone", "dangerous", "security risk",
                                             "destructivo", "no se puede deshacer",
                                             "back up first", "confirm before")),
    OperatorSpec(
        ReasoningOperator.STOP_CONDITION,
        "Name in advance what will count as done.",
        "", ("done when", "stop when", "that is sufficient", "enough to answer",
             "terminado cuando", "suficiente", "no further", "this completes")),
    OperatorSpec(
        ReasoningOperator.ANSWER_STRUCTURE,
        "Shape the answer to the question's actual shape.",
        "epistemic.delivery_policy", ("i will answer in", "structure the answer",
                                      "lead with", "estructura de la respuesta",
                                      "answer the question first"),
        weak_markers=True),
)


def _assert_complete() -> None:
    """Every enum member has exactly one spec. Checked at import, not in a test.

    An operator without a spec would be accepted by
    :meth:`~reasoning_distillation.models.DistilledDecision.validated` (it is a valid member)
    and would then be invisible to :func:`detect` and to the skill deriver — a member that can
    be stored and never found. Catching it at import makes the taxonomy's completeness a
    property of the module rather than of whether a test happened to cover it.
    """
    specified = [spec.operator for spec in TAXONOMY]
    duplicates = sorted({op.value for op in specified if specified.count(op) > 1})
    if duplicates:
        raise AssertionError(f"operator taxonomy has duplicate spec(s) for {duplicates}")
    missing = sorted(op.value for op in ReasoningOperator if op not in set(specified))
    if missing:
        raise AssertionError(
            f"operator taxonomy is incomplete: {missing} have no OperatorSpec. An operator "
            f"that can be stored but never detected is worse than an absent one")


_assert_complete()

#: Operator name -> spec. Built once; every lookup goes through it.
BY_OPERATOR: dict[ReasoningOperator, OperatorSpec] = {s.operator: s for s in TAXONOMY}

#: The set :meth:`DistilledDecision.validated` checks against.
KNOWN_OPERATOR_NAMES: frozenset[str] = frozenset(op.value for op in ReasoningOperator)

#: Compiled marker patterns, word-boundary anchored where the marker is word-shaped so that
#: "actually," inside "factually," does not match. Built once at import.
_COMPILED: tuple[tuple[ReasoningOperator, "re.Pattern[str]", bool], ...] = tuple(
    (spec.operator,
     re.compile("|".join(re.escape(m) for m in spec.markers), re.IGNORECASE),
     spec.weak_markers)
    for spec in TAXONOMY if spec.markers)


def detect(text: str, *, require_two_for_weak: bool = True
           ) -> tuple[ReasoningOperator, ...]:
    """Operators with marker evidence in *text*. Deterministic, offline, sorted.

    Evidence of PRESENCE only. A returned operator means "a marker for this matched", never
    "this operator was applied well" — quality is :mod:`reasoning_distillation.quality`'s job.
    An operator NOT returned means "no marker matched", never "the trace did not do this".

    ``require_two_for_weak`` enforces the second signal for specs flagged
    :attr:`OperatorSpec.weak_markers`, which exist because some moves have no distinctive
    phrasing. ``ANSWER_STRUCTURE`` is the current example: almost any answer could be said to
    have structure, so a single marker would attach it to every record and it would then carry
    no information at all.
    """
    if not isinstance(text, str) or not text:
        return ()
    found: list[ReasoningOperator] = []
    for operator, pattern, weak in _COMPILED:
        matches = pattern.findall(text)
        if not matches:
            continue
        if weak and require_two_for_weak and len(matches) < 2:
            continue
        found.append(operator)
    return tuple(sorted(found, key=lambda op: op.value))


def jarvis_dimension(operator: ReasoningOperator) -> str:
    """The JARVIS decision field this operator informs, or "" when there is none."""
    return BY_OPERATOR[operator].jarvis_dimension


def mapped_operators() -> tuple[ReasoningOperator, ...]:
    """Operators that DO map onto a JARVIS decision field.

    These are the ones a future M67B experiment can compare directly against
    :func:`core.epistemic_deliberation.deliberate`. The complement is recorded too, by
    :func:`taxonomy_description`, because "we have no counterpart for this move" is a finding
    about JARVIS's architecture and not a gap to paper over.
    """
    return tuple(sorted((s.operator for s in TAXONOMY if s.jarvis_dimension),
                        key=lambda op: op.value))


def taxonomy_description() -> dict:
    """The full taxonomy, for the docs and the manifest (§20)."""
    return {
        "operator_taxonomy_version": OPERATOR_TAXONOMY_VERSION,
        "operator_count": len(TAXONOMY),
        "operators": [spec.to_dict() for spec in TAXONOMY],
        "mapped_to_jarvis": [op.value for op in mapped_operators()],
        "unmapped": sorted(op.value for op in ReasoningOperator
                           if not BY_OPERATOR[op].jarvis_dimension),
        "detection": "marker-based, deterministic, evidence of PRESENCE only; never asserts "
                     "that an operator was absent or that it was applied well",
    }


__all__ = [
    "BY_OPERATOR", "KNOWN_OPERATOR_NAMES", "OPERATOR_TAXONOMY_VERSION", "TAXONOMY",
    "OperatorSpec", "ReasoningOperator", "detect", "jarvis_dimension", "mapped_operators",
    "taxonomy_description",
]
