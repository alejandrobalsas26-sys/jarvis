"""
core/improvement_loop.py — V65 Milestone 18: continuous improvement coordinator.

M18 is a *coordinator*, not a new engine. It sits above the systems already
built and sequences them:

    runtime result → M14 eval → failure? → classify → remedy
        ├─ non-training remedy (RAG / tools / routing / firewall / scheduling / scope)
        └─ training remedy → M16 curation (human approval) → M17 experiment →
           offline M14 eval → Model Registry promotion decision → activate/reject/rollback

It **duplicates nothing** (no second evaluator, curator, trainer, or router) and
it enforces the central discipline: **fine-tuning is not the answer to every
failure.** Most failures route to a cheaper, faster fix (retrieval, tool schema,
routing policy, adversarial-eval, scheduling). Only genuine reasoning/hallucination
failures *with a trustworthy target* become training candidates — and even then
they go through M16's human-approval gate; M18 never auto-approves, auto-trains,
or auto-promotes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from core.dataset_pipeline import TrainingCandidate, candidates_from_eval_run

COORDINATOR_VERSION = "v65.m18"
_MAX_EVENTS_PER_CYCLE = 200


# ── failure taxonomy ──────────────────────────────────────────────────────────
class FailureCategory(str, Enum):
    KNOWLEDGE_GAP = "knowledge_gap"
    REASONING_ERROR = "reasoning_error"
    TOOL_SELECTION_ERROR = "tool_selection_error"
    TOOL_ARGUMENT_ERROR = "tool_argument_error"
    HALLUCINATION = "hallucination"
    CITATION_ERROR = "citation_error"
    SOURCE_TRUST_ERROR = "source_trust_error"
    PROMPT_INJECTION_FAILURE = "prompt_injection_failure"
    MEMORY_RETRIEVAL_ERROR = "memory_retrieval_error"
    MEMORY_CONTAMINATION = "memory_contamination"
    ROUTING_ERROR = "routing_error"
    PLANNING_ERROR = "planning_error"
    AGENT_SELECTION_ERROR = "agent_selection_error"
    TIMEOUT = "timeout"
    RESOURCE_PRESSURE = "resource_pressure"
    SCOPE_POLICY_ERROR = "scope_policy_error"
    UNKNOWN = "unknown"


class RemedyKind(str, Enum):
    TRUSTED_RAG = "trusted_rag"                     # improve retrieval, not weights
    TOOL_SCHEMA = "tool_schema"                     # tool schema / examples
    ROUTING_POLICY = "routing_policy"               # routing policy / eval data
    FIREWALL_ADVERSARIAL_EVAL = "firewall_adversarial_eval"
    PLANNING_POLICY = "planning_policy"
    MEMORY_POLICY = "memory_policy"
    SCHEDULING_POLICY = "scheduling_policy"         # resource/timeout — not training
    SCOPE_POLICY_REVIEW = "scope_policy_review"     # human policy review
    TRAINING_CANDIDATE = "training_candidate"       # genuine reasoning/hallucination gap
    STRONGER_MODEL = "stronger_model"               # escalate model, not fine-tune
    HUMAN_REVIEW = "human_review"
    NONE = "none"

    @property
    def is_training(self) -> bool:
        return self is RemedyKind.TRAINING_CANDIDATE


class ImprovementAction(str, Enum):
    CURATE_DATASET = "curate_dataset"               # → M16 (human approval still required)
    APPLY_NON_TRAINING_REMEDY = "apply_non_training_remedy"
    ESCALATE_MODEL = "escalate_model"
    HUMAN_REVIEW = "human_review"
    DEFER = "defer"


# ── event / classification / candidate / decision ─────────────────────────────
@dataclass(frozen=True)
class ImprovementEvent:
    """One observed failure worth reasoning about. Built from an M14 result."""

    source: str                     # "eval" | "runtime"
    case_id: str
    domain: str
    failures: tuple[str, ...] = ()
    error: str | None = None
    metrics: dict = field(default_factory=dict)
    created_ts: float = 0.0

    def to_dict(self) -> dict:
        return {"source": self.source, "case_id": self.case_id, "domain": self.domain,
                "failures": list(self.failures), "error": self.error,
                "metrics": self.metrics, "created_ts": self.created_ts}


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    remedy: RemedyKind
    confidence: float
    rationale: str

    @property
    def requires_training(self) -> bool:
        return self.remedy.is_training

    def to_dict(self) -> dict:
        return {"category": self.category.value, "remedy": self.remedy.value,
                "confidence": self.confidence, "rationale": self.rationale,
                "requires_training": self.requires_training}


@dataclass(frozen=True)
class ImprovementCandidate:
    event: ImprovementEvent
    classification: FailureClassification
    action: ImprovementAction
    training_candidate: TrainingCandidate | None = None
    notes: str = ""

    @property
    def training_eligible(self) -> bool:
        return self.action is ImprovementAction.CURATE_DATASET and self.training_candidate is not None

    def to_dict(self) -> dict:
        return {"event": self.event.to_dict(), "classification": self.classification.to_dict(),
                "action": self.action.value,
                "training_candidate": self.training_candidate.to_dict() if self.training_candidate else None,
                "notes": self.notes}


# ── deterministic classifier ──────────────────────────────────────────────────
# Ordered (dimension-substring → (category, remedy)). Safety/routing/tool causes
# are matched BEFORE generic correctness so a failure is attributed to its most
# actionable cause. Every non-training remedy is intentionally cheaper than
# fine-tuning.
_FAILURE_RULES: tuple[tuple[str, FailureCategory, RemedyKind], ...] = (
    ("injection_resistance", FailureCategory.PROMPT_INJECTION_FAILURE, RemedyKind.FIREWALL_ADVERSARIAL_EVAL),
    ("injection_detection", FailureCategory.PROMPT_INJECTION_FAILURE, RemedyKind.FIREWALL_ADVERSARIAL_EVAL),
    ("tool_safety", FailureCategory.SCOPE_POLICY_ERROR, RemedyKind.SCOPE_POLICY_REVIEW),
    ("forbidden_tools", FailureCategory.SCOPE_POLICY_ERROR, RemedyKind.SCOPE_POLICY_REVIEW),
    ("tool_choice", FailureCategory.TOOL_SELECTION_ERROR, RemedyKind.TOOL_SCHEMA),
    ("missing_tools", FailureCategory.TOOL_SELECTION_ERROR, RemedyKind.TOOL_SCHEMA),
    ("citation_validity", FailureCategory.CITATION_ERROR, RemedyKind.TRUSTED_RAG),
    ("citation", FailureCategory.CITATION_ERROR, RemedyKind.TRUSTED_RAG),
    ("domain_routing", FailureCategory.ROUTING_ERROR, RemedyKind.ROUTING_POLICY),
    ("forbidden_output", FailureCategory.HALLUCINATION, RemedyKind.TRAINING_CANDIDATE),
    ("verification", FailureCategory.HALLUCINATION, RemedyKind.TRAINING_CANDIDATE),
)


def classify_failure(event: ImprovementEvent) -> FailureClassification:
    """Deterministically classify a failure and recommend the *cheapest adequate*
    remedy. Fine-tuning is recommended only for genuine reasoning/hallucination
    gaps — never for tool/routing/resource/scope causes."""
    blob = " ".join(event.failures).lower()
    err = (event.error or "").lower()

    if "timeout" in blob or "timeout" in err:
        return FailureClassification(FailureCategory.TIMEOUT, RemedyKind.SCHEDULING_POLICY,
                                     0.9, "timeout → scheduling/latency policy, not training")
    if "resource" in err or "resource_pressure" in blob or "memoryerror" in err:
        return FailureClassification(FailureCategory.RESOURCE_PRESSURE, RemedyKind.SCHEDULING_POLICY,
                                     0.85, "resource pressure → scheduling policy, not training")

    for needle, category, remedy in _FAILURE_RULES:
        if needle in blob:
            return FailureClassification(
                category, remedy, 0.8,
                f"matched failure signal {needle!r} → {remedy.value}")

    # Correctness/ground-truth misses: knowledge gap (retrieval) for evidence
    # domains, otherwise a reasoning gap that *may* warrant a training candidate.
    if "correctness" in blob or "ground_truth" in blob:
        if event.domain in ("research", "grc", "dfir"):
            return FailureClassification(FailureCategory.KNOWLEDGE_GAP, RemedyKind.TRUSTED_RAG,
                                         0.7, "evidence-domain correctness miss → improve retrieval")
        return FailureClassification(FailureCategory.REASONING_ERROR, RemedyKind.TRAINING_CANDIDATE,
                                     0.65, "reasoning correctness gap → training candidate (if target exists)")
    if "confidence" in blob:
        return FailureClassification(FailureCategory.REASONING_ERROR, RemedyKind.STRONGER_MODEL,
                                     0.5, "low calibrated confidence → consider a stronger model")
    return FailureClassification(FailureCategory.UNKNOWN, RemedyKind.HUMAN_REVIEW,
                                 0.3, "unclassified failure → human review")


def _action_for(remedy: RemedyKind) -> ImprovementAction:
    if remedy is RemedyKind.TRAINING_CANDIDATE:
        return ImprovementAction.CURATE_DATASET
    if remedy is RemedyKind.STRONGER_MODEL:
        return ImprovementAction.ESCALATE_MODEL
    if remedy in (RemedyKind.SCOPE_POLICY_REVIEW, RemedyKind.HUMAN_REVIEW):
        return ImprovementAction.HUMAN_REVIEW
    if remedy is RemedyKind.NONE:
        return ImprovementAction.DEFER
    return ImprovementAction.APPLY_NON_TRAINING_REMEDY


# ── improvement cycle ─────────────────────────────────────────────────────────
@dataclass
class ImprovementCycle:
    cycle_id: str
    candidates: list[ImprovementCandidate] = field(default_factory=list)
    dropped_events: int = 0
    created_ts: float = 0.0

    def training_candidates(self) -> list[TrainingCandidate]:
        return [c.training_candidate for c in self.candidates if c.training_eligible]

    def remedy_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.candidates:
            out[c.classification.remedy.value] = out.get(c.classification.remedy.value, 0) + 1
        return dict(sorted(out.items()))

    def action_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.candidates:
            out[c.action.value] = out.get(c.action.value, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "events": len(self.candidates),
            "dropped_events": self.dropped_events,
            "training_candidates": len(self.training_candidates()),
            "remedies": self.remedy_breakdown(), "actions": self.action_breakdown(),
        }


# ── coordinator ───────────────────────────────────────────────────────────────
class ImprovementCoordinator:
    """Bounded, non-duplicating coordinator. It classifies failures, builds a
    plan of remedies, and (only when asked and only for training-eligible
    failures with a trustworthy target) hands candidates to M16 curation. It
    never approves, trains, or promotes — those stay explicit and human-gated."""

    def __init__(self, *, max_events_per_cycle: int = _MAX_EVENTS_PER_CYCLE) -> None:
        self.max_events_per_cycle = max_events_per_cycle

    # ── ingest failures from an M14 run ──────────────────────────────────────
    def events_from_eval_run(self, run, cases, *, now_ts: float = 0.0) -> list[ImprovementEvent]:
        by_id = {getattr(c, "id", ""): c for c in cases}
        events: list[ImprovementEvent] = []
        for result in getattr(run, "results", []):
            if getattr(result, "passed", True):
                continue
            case = by_id.get(getattr(result, "case_id", ""))
            events.append(ImprovementEvent(
                source="eval", case_id=getattr(result, "case_id", ""),
                domain=getattr(case, "domain", getattr(result, "domain", "general")),
                failures=tuple(getattr(result, "failures", []) or ()),
                error=getattr(result, "error", None),
                metrics=dict(getattr(result, "metrics", {}) or {}), created_ts=now_ts,
            ))
        return events

    # ── plan a cycle (classification + remedy routing) ───────────────────────
    def plan_cycle(self, run, cases, *, cycle_id: str = "cycle", now_ts: float = 0.0) -> ImprovementCycle:
        events = self.events_from_eval_run(run, cases, now_ts=now_ts)
        dropped = max(0, len(events) - self.max_events_per_cycle)
        if dropped:
            logger.warning(f"M18: cycle {cycle_id} bounded — {dropped} event(s) deferred to next cycle")
        events = events[: self.max_events_per_cycle]

        # Build trustworthy training targets ONCE (reuses M16; never fabricates).
        train_by_case = {c.failure_ref: c for c in candidates_from_eval_run(run, cases)}

        cycle = ImprovementCycle(cycle_id=cycle_id, dropped_events=dropped, created_ts=now_ts)
        for ev in events:
            cls = classify_failure(ev)
            action = _action_for(cls.remedy)
            tc = None
            notes = ""
            if cls.requires_training:
                tc = train_by_case.get(ev.case_id)
                if tc is None:
                    # Training warranted but no trustworthy target exists → do NOT
                    # fabricate one; route to human authoring instead.
                    action = ImprovementAction.HUMAN_REVIEW
                    notes = "training warranted but no trustworthy target — needs human-authored ideal"
            cycle.candidates.append(ImprovementCandidate(
                event=ev, classification=cls, action=action, training_candidate=tc, notes=notes,
            ))
        logger.info(f"M18: planned {cycle_id}: {cycle.summary()}")
        return cycle

    # ── optional: hand training candidates to M16 curation (no approval) ──────
    async def curate_training_candidates(
        self, cycle: ImprovementCycle, dataset_pipeline, *,
        existing: tuple[str, ...] = (), version: str = "improvement-v0", now_ts: float = 0.0,
    ):
        """Run the cycle's training candidates through M16 curation. Returns the
        M16 ``CurationReport`` — every survivor is at most ``PENDING_REVIEW``;
        this method NEVER approves, writes, or trains anything."""
        candidates = cycle.training_candidates()
        if not candidates:
            return None
        report = await dataset_pipeline.curate(candidates, existing=existing, version=version, now_ts=now_ts)
        logger.info(f"M18: curated {len(candidates)} training candidate(s) → {report.summary()} "
                    "(all PENDING_REVIEW — human approval required before any training)")
        return report
