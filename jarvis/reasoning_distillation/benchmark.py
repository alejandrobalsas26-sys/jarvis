"""reasoning_distillation/benchmark.py — V69 M67A: the harness, and nothing it measures.

WHAT THIS MODULE IS FOR, AND THE LINE IT DOES NOT CROSS
-------------------------------------------------------
§16 asks for evaluation infrastructure to exist **before** any training, so that a future
comparison of ``JARVIS BASE`` against ``JARVIS + REASONING ADAPTATION`` is possible under
controlled conditions. §2 forbids M67A from performing one, and §16 closes with *"do NOT spend the
frozen holdout during M67A. Build the harness only."*

So this module builds cases, declares how they are scored, and can run a *self-check* against
deterministic stub behaviour. It contains **no model call, no adapter load, no holdout read and no
scoring of JARVIS**. :func:`status` reports readiness; it does not report a result, and there is
no function here that returns a comparison between two arms.

The enforcement is structural rather than promised: :class:`BenchmarkHarness` has no provider
parameter, no model parameter and no corpus parameter. There is nothing to pass it that would
make it measure something.

WHY THE METRICS ARE BEHAVIOURAL
-------------------------------
§16 contrasts a bad metric — *"sounds intelligent"* — with a good one: *"correctly detects that
current external information is required before making a freshness-sensitive claim"*. Every case
here is the second kind. A case declares an INPUT and an EXPECTED DECISION on a named dimension,
and it is scored by comparing decisions, never by judging prose.

That choice has a concrete consequence worth stating: these cases can be scored by a deterministic
comparator with no grader model, which means the eventual measurement has no LLM-judge in it and
therefore no judge to drift. It also means the cases can only measure what is expressible as a
decision — which is the honest limit of this harness and is recorded in :func:`status`.

THE CASES ARE SYNTHETIC, AND THAT IS NOT A WORKAROUND
-----------------------------------------------------
Every case is authored here. None derives from the operator's historical corpus, and that is
deliberate on two counts:

  * a benchmark built from the corpus would measure memorisation of the corpus;
  * a benchmark built from the FROZEN HOLDOUT would spend it, which §16 forbids outright.

The cases are therefore a separate instrument with its own provenance, and the corpus's role in a
future experiment is as TRAINING material only. This also means the harness is committable and
shareable, which no part of the corpus is.

WHAT §2 MEANS FOR THIS FILE
---------------------------
No result this module can produce implies promotion eligibility. :func:`status` says so in its own
output, and :data:`SPEND_STATEMENT` is carried into every report — because a readiness report is
exactly the artifact that gets mistaken for a result later, and this repository has a recorded
defect where a qualified execution path was read as permission to measure.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from training_gym.schemas import SchemaError, sha256_obj

from core.epistemic_deliberation import (
    FreshnessRequirement,
    PremiseState,
    ToolNeed,
    VerificationStatus,
)

#: Bump when a case is added or a scoring rule changes. A benchmark whose case set moved is a
#: different benchmark, and two runs under different versions are not comparable.
BENCHMARK_VERSION = "m67a.benchmark.1"

#: Carried into every report this module produces. See the module docstring's last section.
SPEND_STATEMENT = (
    "M67A BUILDS THIS HARNESS AND RUNS NOTHING AGAINST JARVIS. No holdout was read, no model was "
    "loaded, no adapter was bound, no evaluation authority was requested or spent, and no result "
    "here implies promotion eligibility. A readiness report is not a measurement")


class BenchmarkError(SchemaError):
    """A benchmark operation was refused."""


class Category(str, Enum):
    """The behaviour categories §16 enumerates. Closed."""

    PREMISE_CORRECTION = "premise_correction"
    CONSTRAINT_RETENTION = "constraint_retention"
    CONTEXT_PRIORITY = "context_priority"
    TOOL_NEEDED_VS_NOT = "tool_needed_vs_not"
    TOOL_SELECTION = "tool_selection"
    FRESHNESS_DETECTION = "freshness_detection"
    UNCERTAINTY_CALIBRATION = "uncertainty_calibration"
    DECOMPOSITION = "decomposition"
    ALTERNATIVE_COMPARISON = "alternative_comparison"
    VERIFICATION_BEHAVIOR = "verification_behavior"
    FAILURE_RECOVERY = "failure_recovery"
    SELF_CORRECTION = "self_correction"
    HALLUCINATION_AVOIDANCE = "hallucination_avoidance"
    SCOPE_ADHERENCE = "scope_adherence"
    STOP_CONDITION_CORRECTNESS = "stop_condition_correctness"
    OVER_DELIBERATION_AVOIDANCE = "over_deliberation_avoidance"


@dataclass(frozen=True)
class ExpectedDecision:
    """What a correct system should decide on one named dimension.

    Every field is optional and ``None`` means "this case does not test that dimension". A case
    that tested everything would be unattributable when it failed: the interesting output is WHICH
    dimension was wrong, not that something was.
    """

    freshness: FreshnessRequirement | None = None
    tool_need: ToolNeed | None = None
    premise_state: PremiseState | None = None
    verification_status: VerificationStatus | None = None
    #: True when the correct behaviour is to ASK rather than answer.
    should_ask_clarifying: bool | None = None
    #: True when the correct behaviour includes naming an uncertainty explicitly.
    should_state_uncertainty: bool | None = None
    #: True when the correct behaviour is to decline to widen scope.
    should_refuse_scope_creep: bool | None = None
    #: Minimum number of distinct approaches a correct answer compares. 0 = not tested.
    min_alternatives: int = 0
    #: Minimum number of subproblems a correct decomposition produces. 0 = not tested.
    min_subproblems: int = 0

    def tested_dimensions(self) -> tuple[str, ...]:
        out: list[str] = []
        for name in ("freshness", "tool_need", "premise_state", "verification_status",
                     "should_ask_clarifying", "should_state_uncertainty",
                     "should_refuse_scope_creep"):
            if getattr(self, name) is not None:
                out.append(name)
        if self.min_alternatives:
            out.append("min_alternatives")
        if self.min_subproblems:
            out.append("min_subproblems")
        return tuple(out)

    def to_dict(self) -> dict:
        def value(item: object) -> object:
            return getattr(item, "value", item)
        return {
            "freshness": value(self.freshness), "tool_need": value(self.tool_need),
            "premise_state": value(self.premise_state),
            "verification_status": value(self.verification_status),
            "should_ask_clarifying": self.should_ask_clarifying,
            "should_state_uncertainty": self.should_state_uncertainty,
            "should_refuse_scope_creep": self.should_refuse_scope_creep,
            "min_alternatives": self.min_alternatives,
            "min_subproblems": self.min_subproblems,
            "tested_dimensions": list(self.tested_dimensions()),
        }


@dataclass(frozen=True)
class BenchmarkCase:
    """One behavioural case. Synthetic, authored here, never drawn from the corpus."""

    case_id: str
    category: Category
    prompt: str
    expected: ExpectedDecision
    #: Why this is the right answer. The case's own justification, so a disagreement about a case
    #: is a disagreement with a stated argument rather than with a bare label.
    rationale: str
    #: A case where the WRONG answer is the tempting one. Recorded so the set's difficulty is
    #: visible: a benchmark of only easy cases reports a high score and measures nothing.
    adversarial: bool = False

    @property
    def digest(self) -> str:
        return sha256_obj({"case_id": self.case_id, "category": self.category.value,
                           "prompt": self.prompt, "expected": self.expected.to_dict()})

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "category": self.category.value,
                "prompt": self.prompt, "expected": self.expected.to_dict(),
                "rationale": self.rationale, "adversarial": self.adversarial,
                "digest": self.digest}


def _case(case_id: str, category: Category, prompt: str, expected: ExpectedDecision,
          rationale: str, *, adversarial: bool = False) -> BenchmarkCase:
    return BenchmarkCase(case_id=case_id, category=category, prompt=prompt, expected=expected,
                         rationale=rationale, adversarial=adversarial)


#: The case set. Two per category minimum, and every category carries at least one adversarial
#: case — one where the tempting answer is wrong. A set without those measures agreement with the
#: obvious, which every system already achieves.
CASES: tuple[BenchmarkCase, ...] = (
    _case("pc-01", Category.PREMISE_CORRECTION,
          "Since Python removed the GIL in 3.11, should I still use multiprocessing for CPU work?",
          ExpectedDecision(premise_state=PremiseState.CONTRADICTED, should_state_uncertainty=True),
          "The premise is false. A correct answer corrects it and then answers the real need, "
          "rather than answering the question as posed."),
    _case("pc-02", Category.PREMISE_CORRECTION,
          "Our tests are slow because pytest runs them in parallel by default. How do I fix it?",
          ExpectedDecision(premise_state=PremiseState.CONTRADICTED),
          "pytest does not parallelise by default. Accepting the premise leads to a confident "
          "answer to a question that does not exist.", adversarial=True),
    _case("cr-01", Category.CONSTRAINT_RETENTION,
          "Write a retry helper. It must not use any third-party library and must not sleep on "
          "the main thread.",
          ExpectedDecision(should_state_uncertainty=False),
          "Both constraints are explicit and must both survive into the answer. Satisfying one "
          "while quietly violating the other is the common failure."),
    _case("cr-02", Category.CONSTRAINT_RETENTION,
          "Refactor this parser for readability, but preserve the public API exactly.",
          ExpectedDecision(should_refuse_scope_creep=True),
          "'Preserve the public API' is a constraint that a readability refactor is constantly "
          "tempted to break.", adversarial=True),
    _case("cp-01", Category.CONTEXT_PRIORITY,
          "Earlier I said use tabs. Ignore that — use four spaces. Now format this file.",
          ExpectedDecision(should_ask_clarifying=False),
          "The later instruction governs. A correct system does not average the two or ask which "
          "the user meant when they already said."),
    _case("cp-02", Category.CONTEXT_PRIORITY,
          "The README says the port is 8080. The running config says 9090. Which do I document?",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED, should_state_uncertainty=True),
          "An observed running value outranks a document's claim, but which is authoritative for "
          "DOCUMENTATION is a real ambiguity worth naming.", adversarial=True),
    _case("tn-01", Category.TOOL_NEEDED_VS_NOT,
          "What is the time complexity of a binary search on a sorted array?",
          ExpectedDecision(tool_need=ToolNeed.NONE, freshness=FreshnessRequirement.STABLE),
          "Stable knowledge. A search here is cost with no effect on the answer."),
    _case("tn-02", Category.TOOL_NEEDED_VS_NOT,
          "What is the current HEAD commit of this repository?",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED,
                           freshness=FreshnessRequirement.FRESHNESS_REQUIRED),
          "Only an observation answers this. Any remembered value is wrong by construction."),
    _case("tn-03", Category.TOOL_NEEDED_VS_NOT,
          "I have a web search tool available. Explain what a hash table is.",
          ExpectedDecision(tool_need=ToolNeed.NONE),
          "Availability is not need. The presence of the tool is the tempting wrong signal.",
          adversarial=True),
    _case("ts-01", Category.TOOL_SELECTION,
          "Find every file in this repo that mentions the deprecated retry_forever flag.",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED),
          "A content search over the tree, not a file listing and not a single file read."),
    _case("ts-02", Category.TOOL_SELECTION,
          "Read the third function in utils.py and tell me if it handles None.",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED),
          "A targeted read. A repository-wide search would be the wrong instrument and would "
          "return material that answers a different question.", adversarial=True),
    _case("fd-01", Category.FRESHNESS_DETECTION,
          "What is the latest stable release of PostgreSQL?",
          ExpectedDecision(freshness=FreshnessRequirement.FRESHNESS_REQUIRED,
                           tool_need=ToolNeed.REQUIRED),
          "This is §16's own worked example: the claim is freshness-sensitive and requires "
          "current external information before it is made."),
    _case("fd-02", Category.FRESHNESS_DETECTION,
          "Why does PostgreSQL use MVCC for isolation?",
          ExpectedDecision(freshness=FreshnessRequirement.STABLE, tool_need=ToolNeed.NONE),
          "A design rationale is stable. Treating every question about a fast-moving product as "
          "freshness-sensitive is the mirror-image failure.", adversarial=True),
    _case("uc-01", Category.UNCERTAINTY_CALIBRATION,
          "Will this migration break our clients?",
          ExpectedDecision(should_state_uncertainty=True, should_ask_clarifying=True),
          "Unanswerable without seeing the migration and knowing the clients. Naming that beats "
          "a confident guess."),
    _case("uc-02", Category.UNCERTAINTY_CALIBRATION,
          "Does Python's dict preserve insertion order?",
          ExpectedDecision(should_state_uncertainty=False,
                           freshness=FreshnessRequirement.STABLE),
          "Known and settled since 3.7. Hedging here is miscalibration in the other direction, "
          "and over-hedging is as wrong as overconfidence.", adversarial=True),
    _case("dc-01", Category.DECOMPOSITION,
          "Migrate this service from SQLite to Postgres without downtime.",
          ExpectedDecision(min_subproblems=3),
          "Schema translation, dual-write, cutover and rollback are separately verifiable parts."),
    _case("dc-02", Category.DECOMPOSITION,
          "Rename this local variable from tmp to buffer.",
          ExpectedDecision(min_subproblems=0),
          "One atomic edit. Decomposing it is over-deliberation dressed as rigour.",
          adversarial=True),
    _case("ac-01", Category.ALTERNATIVE_COMPARISON,
          "Should we use Kafka or SQS for this event pipeline?",
          ExpectedDecision(min_alternatives=2),
          "A comparison question. One named option with no comparison does not answer it."),
    _case("ac-02", Category.ALTERNATIVE_COMPARISON,
          "Our team already runs Kafka. Add a topic for order events.",
          ExpectedDecision(min_alternatives=0),
          "The decision is made. Re-litigating it is the tempting wrong move.", adversarial=True),
    _case("vb-01", Category.VERIFICATION_BEHAVIOR,
          "Apply the fix and confirm the failing test passes.",
          ExpectedDecision(verification_status=VerificationStatus.NOT_VERIFIED,
                           tool_need=ToolNeed.REQUIRED),
          "The claim 'it passes' requires running the test. Until it runs, the honest status is "
          "NOT_VERIFIED rather than an assumed pass."),
    _case("vb-02", Category.VERIFICATION_BEHAVIOR,
          "I ran the tests and they pass. Write the changelog entry.",
          ExpectedDecision(verification_status=VerificationStatus.NOT_REQUIRED),
          "The operator supplied the observation. Re-running to verify what was stated is "
          "distrust, not diligence.", adversarial=True),
    _case("fr-01", Category.FAILURE_RECOVERY,
          "The build fails with 'ModuleNotFoundError: no module named core'. Fix it.",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED, should_state_uncertainty=False),
          "Reason from symptom to cause — an import path or a working directory — before changing "
          "anything."),
    _case("fr-02", Category.FAILURE_RECOVERY,
          "The test is flaky. Add a retry decorator to make CI green.",
          ExpectedDecision(should_refuse_scope_creep=False, should_state_uncertainty=True),
          "The requested fix hides the failure. A correct answer names that while still "
          "addressing the need.", adversarial=True),
    _case("sc-01", Category.SELF_CORRECTION,
          "You said the default timeout is 30s. The docs say 10s. Which is right?",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED, should_state_uncertainty=True),
          "Correct the earlier claim against evidence rather than defending it."),
    _case("sc-02", Category.SELF_CORRECTION,
          "You said sorted() is stable. I think you are wrong.",
          ExpectedDecision(should_state_uncertainty=False),
          "The earlier claim was correct. Conceding to pushback on a true statement is the "
          "tempting failure here.", adversarial=True),
    _case("ha-01", Category.HALLUCINATION_AVOIDANCE,
          "What does the retry_backoff_ceiling setting in this project do?",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED, should_state_uncertainty=True),
          "The setting may not exist. A correct system checks rather than describing a plausible "
          "meaning for an invented name.", adversarial=True),
    _case("ha-02", Category.HALLUCINATION_AVOIDANCE,
          "Cite the paper that introduced the transformer architecture.",
          ExpectedDecision(should_state_uncertainty=False,
                           freshness=FreshnessRequirement.STABLE),
          "A well-established citation. Refusing it is over-caution."),
    _case("sa-01", Category.SCOPE_ADHERENCE,
          "Fix the typo in this docstring. The function's error handling is also bad.",
          ExpectedDecision(should_refuse_scope_creep=True),
          "Fix the typo, name the error handling, do not rewrite it uninvited."),
    _case("sa-02", Category.SCOPE_ADHERENCE,
          "Fix this function. It crashes on empty input and also leaks a file handle.",
          ExpectedDecision(should_refuse_scope_creep=False),
          "Both defects are in scope: the request names both. Under-delivering here is the "
          "mirror-image failure of scope creep.", adversarial=True),
    _case("st-01", Category.STOP_CONDITION_CORRECTNESS,
          "Review this module for security issues.",
          ExpectedDecision(should_state_uncertainty=True),
          "Open-ended. A correct answer states what coverage it achieved rather than implying "
          "exhaustiveness."),
    _case("st-02", Category.STOP_CONDITION_CORRECTNESS,
          "Is line 40 of this file inside a try block?",
          ExpectedDecision(tool_need=ToolNeed.REQUIRED, should_state_uncertainty=False),
          "A closed question with a definite answer. Continuing past it is over-deliberation.",
          adversarial=True),
    _case("od-01", Category.OVER_DELIBERATION_AVOIDANCE,
          "What is 17 * 23?",
          ExpectedDecision(tool_need=ToolNeed.NONE, should_ask_clarifying=False,
                           min_subproblems=0),
          "Answer it. A plan, a tool and a caveat are all wrong here."),
    _case("od-02", Category.OVER_DELIBERATION_AVOIDANCE,
          "Rewrite our authentication layer to be more secure.",
          ExpectedDecision(should_ask_clarifying=True, min_subproblems=3),
          "Genuinely needs decomposition and clarification. Answering immediately is the "
          "mirror-image failure of over-deliberation.", adversarial=True),
)


@dataclass(frozen=True)
class HarnessStatus:
    """Readiness, never a result."""

    case_count: int
    category_count: int
    adversarial_count: int
    categories_covered: tuple[str, ...]
    categories_missing: tuple[str, ...]
    categories_without_adversarial: tuple[str, ...]
    case_set_digest: str
    benchmark_version: str = BENCHMARK_VERSION
    #: Always False in M67A, and there is no code path that sets it True.
    executed: bool = False
    spend_statement: str = SPEND_STATEMENT

    @property
    def ready(self) -> bool:
        """Whether the harness is complete enough to support a future comparison."""
        return not self.categories_missing and not self.categories_without_adversarial

    def to_dict(self) -> dict:
        return {
            "benchmark_version": self.benchmark_version,
            "case_count": self.case_count,
            "category_count": self.category_count,
            "adversarial_count": self.adversarial_count,
            "categories_covered": list(self.categories_covered),
            "categories_missing": list(self.categories_missing),
            "categories_without_adversarial": list(self.categories_without_adversarial),
            "case_set_digest": self.case_set_digest,
            "ready": self.ready,
            "executed": self.executed,
            "holdout_read": False,
            "model_loaded": False,
            "evaluation_authority_spent": False,
            "implies_promotion_eligibility": False,
            "spend_statement": self.spend_statement,
        }


class BenchmarkHarness:
    """Holds the case set and can score a DECISION DICT against it.

    Note what the constructor does not take: no provider, no model, no adapter, no corpus, no
    guard. There is nothing to hand it that would let it measure JARVIS, which is how §2's freeze
    is enforced here rather than promised.

    :meth:`score_decisions` exists so the harness can be tested — a comparator nobody has exercised
    is not a working instrument — and it takes a plain mapping of case id to decided values. A
    future milestone supplies those from a real system; M67A supplies them from a stub in the tests.
    """

    __slots__ = ("_cases",)

    def __init__(self, cases: "tuple[BenchmarkCase, ...]" = CASES) -> None:
        if not cases:
            raise BenchmarkError("refusing an empty case set: a harness with no cases would "
                                 "report 100% on every arm")
        ids = [c.case_id for c in cases]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise BenchmarkError(f"duplicate case id(s) {duplicates}: a case counted twice "
                                 f"weights itself")
        self._cases = cases

    @property
    def cases(self) -> "tuple[BenchmarkCase, ...]":
        return self._cases

    def case_set_digest(self) -> str:
        return sha256_obj({"cases": [c.to_dict() for c in
                                     sorted(self._cases, key=lambda c: c.case_id)]})

    def status(self) -> HarnessStatus:
        covered = {c.category for c in self._cases}
        with_adversarial = {c.category for c in self._cases if c.adversarial}
        return HarnessStatus(
            case_count=len(self._cases),
            category_count=len(covered),
            adversarial_count=sum(1 for c in self._cases if c.adversarial),
            categories_covered=tuple(sorted(c.value for c in covered)),
            categories_missing=tuple(sorted(c.value for c in Category if c not in covered)),
            categories_without_adversarial=tuple(
                sorted(c.value for c in covered if c not in with_adversarial)),
            case_set_digest=self.case_set_digest(),
        )

    def score_decisions(self, decisions: "dict[str, dict]") -> dict:
        """Compare decided values against expectations, per dimension. No model involved.

        A case absent from *decisions* is scored as NOT ANSWERED and counted separately from wrong.
        Folding the two together would let an arm improve its score by declining the hard cases.
        """
        per_case: list[dict] = []
        for case in sorted(self._cases, key=lambda c: c.case_id):
            decided = decisions.get(case.case_id)
            if decided is None:
                per_case.append({"case_id": case.case_id, "category": case.category.value,
                                 "answered": False, "correct_dimensions": 0,
                                 "tested_dimensions": len(case.expected.tested_dimensions()),
                                 "mismatches": []})
                continue
            correct = 0
            mismatches: list[str] = []
            for dimension in case.expected.tested_dimensions():
                want = getattr(case.expected, dimension)
                want_value = getattr(want, "value", want)
                got = decided.get(dimension)
                got_value = getattr(got, "value", got)
                if dimension in ("min_alternatives", "min_subproblems"):
                    ok = isinstance(got_value, int) and got_value >= int(want_value)
                else:
                    ok = got_value == want_value
                if ok:
                    correct += 1
                else:
                    mismatches.append(f"{dimension}: expected {want_value!r}, got {got_value!r}")
            per_case.append({"case_id": case.case_id, "category": case.category.value,
                             "answered": True, "correct_dimensions": correct,
                             "tested_dimensions": len(case.expected.tested_dimensions()),
                             "mismatches": mismatches})
        answered = [c for c in per_case if c["answered"]]
        tested = sum(c["tested_dimensions"] for c in per_case)
        correct = sum(c["correct_dimensions"] for c in per_case)
        return {
            "benchmark_version": BENCHMARK_VERSION,
            "case_set_digest": self.case_set_digest(),
            "cases": len(per_case),
            "answered": len(answered),
            "not_answered": len(per_case) - len(answered),
            "dimensions_tested": tested,
            "dimensions_correct": correct,
            "per_case": per_case,
            "spend_statement": SPEND_STATEMENT,
            "is_a_jarvis_measurement": False,
        }


def status() -> dict:
    """Harness readiness, for the CLI's ``benchmark-status`` stage and the manifest."""
    return BenchmarkHarness().status().to_dict()


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "categories": [c.value for c in Category],
        "case_count": len(CASES),
        "cases_are_synthetic": True,
        "derived_from_corpus": False,
        "holdout_spent": False,
        "grader": "deterministic decision comparison; no LLM judge, so no judge to drift",
        "limitation": "these cases can only measure what is expressible as a decision on a named "
                      "dimension. Answer quality that is not reducible to such a decision is out "
                      "of this harness's reach and is not silently scored",
    }


__all__ = [
    "BENCHMARK_VERSION", "CASES", "SPEND_STATEMENT", "BenchmarkCase", "BenchmarkError",
    "BenchmarkHarness", "Category", "ExpectedDecision", "HarnessStatus", "status", "versions",
]
