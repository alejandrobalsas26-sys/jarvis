"""training_gym/episode.py — V69 M62: the state machine an attempt must walk.

WHY THIS EXISTS
---------------
The milestone's central claim is that no model output can silently become training
data. A comment cannot enforce that. A state machine can.

:data:`ALLOWED_TRANSITIONS` is the enforcement. ``APPROVED`` has exactly ONE
predecessor — ``NEEDS_HUMAN_REVIEW`` — so there is no sequence of calls, in any order,
that reaches approval without a human decision being recorded first. ``RUNNING`` does
not reach ``APPROVED``. ``FAILED`` does not reach ``APPROVED``. ``QUARANTINED`` and
``REJECTED`` are terminal except for cleanup, and neither is a dataset source. This
is not a policy the caller is asked to respect; it is the only path the type permits.

On top of the graph sit the guards in :meth:`Episode.approve`, which re-check the
evidence at the moment of approval rather than trusting that whoever moved the episode
into review had already checked: every mandatory grader present and affirmative, the
reward unblocked, the human decision bound by hash to THIS attempt, and — because a
cloud model's opinion is not evidence — a teacher recommendation that cannot override
a deterministic failure.

The history is append-only. A transition is never rewritten and never removed, so the
record of how an episode reached its state survives disagreement about whether it
should have.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .rewards import RewardBreakdown, score_attempt
from .schemas import (
    GYM_VERSION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    ResultStatus,
    SchemaError,
    require_enum,
    require_id,
    sha256_obj,
)
from .task_spec import TaskSpec
from .trajectory import HumanDecision, Recommendation, Trajectory

MAX_ATTEMPTS = 8
MAX_HISTORY = 256


class EpisodeState(str, Enum):
    """Where an episode is. Thirteen states, one legal path to approval."""

    CREATED = "created"
    VALIDATED = "validated"
    SANDBOX_PREPARED = "sandbox_prepared"
    RUNNING = "running"
    GRADING = "grading"
    TEACHER_REVIEW = "teacher_review"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    ABORTED = "aborted"
    CLEANED = "cleaned"

    @property
    def is_terminal_execution(self) -> bool:
        """States after which no further execution happens and cleanup is due."""
        return self in _TERMINAL_EXECUTION

    @property
    def is_dataset_source(self) -> bool:
        """Only an approved episode may contribute a positive training example.

        ``QUARANTINED`` is excluded permanently — quarantine exists for material that
        must never be trained on — and ``REJECTED`` is excluded as a positive target,
        though it remains available as preference/negative data downstream."""
        return self is EpisodeState.APPROVED


_TERMINAL_EXECUTION: frozenset[EpisodeState] = frozenset({
    EpisodeState.APPROVED, EpisodeState.REJECTED, EpisodeState.QUARANTINED,
    EpisodeState.FAILED, EpisodeState.ABORTED,
})

#: The complete legal graph. Anything not listed here is refused.
ALLOWED_TRANSITIONS: dict[EpisodeState, frozenset[EpisodeState]] = {
    EpisodeState.CREATED: frozenset({
        EpisodeState.VALIDATED, EpisodeState.FAILED, EpisodeState.ABORTED}),
    EpisodeState.VALIDATED: frozenset({
        EpisodeState.SANDBOX_PREPARED, EpisodeState.FAILED, EpisodeState.ABORTED}),
    EpisodeState.SANDBOX_PREPARED: frozenset({
        EpisodeState.RUNNING, EpisodeState.FAILED, EpisodeState.ABORTED}),
    EpisodeState.RUNNING: frozenset({
        EpisodeState.GRADING, EpisodeState.FAILED, EpisodeState.ABORTED}),
    EpisodeState.GRADING: frozenset({
        EpisodeState.TEACHER_REVIEW, EpisodeState.NEEDS_HUMAN_REVIEW,
        EpisodeState.REJECTED, EpisodeState.QUARANTINED, EpisodeState.FAILED,
        EpisodeState.ABORTED}),
    EpisodeState.TEACHER_REVIEW: frozenset({
        EpisodeState.NEEDS_HUMAN_REVIEW, EpisodeState.REJECTED,
        EpisodeState.QUARANTINED, EpisodeState.FAILED, EpisodeState.ABORTED}),
    # The single door to approval.
    EpisodeState.NEEDS_HUMAN_REVIEW: frozenset({
        EpisodeState.APPROVED, EpisodeState.REJECTED, EpisodeState.QUARANTINED,
        EpisodeState.ABORTED}),
    EpisodeState.APPROVED: frozenset({EpisodeState.CLEANED}),
    EpisodeState.REJECTED: frozenset({EpisodeState.CLEANED}),
    EpisodeState.QUARANTINED: frozenset({EpisodeState.CLEANED}),
    EpisodeState.FAILED: frozenset({EpisodeState.CLEANED}),
    EpisodeState.ABORTED: frozenset({EpisodeState.CLEANED}),
    EpisodeState.CLEANED: frozenset(),
}


class TransitionError(SchemaError):
    """An illegal state change was attempted. Never downgraded to a warning."""


@dataclass(frozen=True)
class TransitionRecord:
    """One entry in the append-only history."""

    from_state: EpisodeState
    to_state: EpisodeState
    actor: str
    at: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {"from": self.from_state.value, "to": self.to_state.value,
                "actor": self.actor, "at": self.at, "reason": self.reason}


@dataclass
class EpisodeAttempt:
    """One trajectory plus the score computed from it."""

    trajectory: Trajectory
    reward: RewardBreakdown | None = None

    @property
    def attempt_hash(self) -> str:
        return self.trajectory.attempt_hash()

    def score(self, spec: TaskSpec) -> RewardBreakdown:
        self.reward = score_attempt(spec, self.trajectory)
        return self.reward

    def to_dict(self) -> dict:
        return {"trajectory": self.trajectory.to_dict(),
                "reward": self.reward.to_dict() if self.reward else None}


@dataclass(frozen=True)
class EpisodeOutcome:
    """The sealed result. What a dataset bridge is allowed to read."""

    episode_id: str
    task_id: str
    task_hash: str
    state: EpisodeState
    attempt_hash: str
    dataset_eligible: bool
    reward: dict = field(default_factory=dict)
    rejection_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "gym_version": GYM_VERSION,
                "episode_id": self.episode_id, "task_id": self.task_id,
                "task_hash": self.task_hash, "state": self.state.value,
                "attempt_hash": self.attempt_hash,
                "dataset_eligible": self.dataset_eligible,
                "reward": dict(self.reward),
                "rejection_reasons": list(self.rejection_reasons)}

    def outcome_hash(self) -> str:
        return sha256_obj(self.to_dict())


@dataclass
class Episode:
    """One task, one or more attempts, one auditable path to a decision."""

    episode_id: str
    spec: TaskSpec
    state: EpisodeState = EpisodeState.CREATED
    attempts: list[EpisodeAttempt] = field(default_factory=list)
    history: list[TransitionRecord] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        require_id(self.episode_id, "episode.episode_id")
        object.__setattr__(self, "state", require_enum(self.state, EpisodeState,
                                                       "episode.state"))

    # -- transitions ------------------------------------------------------------
    def transition(self, to_state: EpisodeState | str, *, actor: str,
                   at: str = "", reason: str = "") -> EpisodeState:
        """Move to *to_state* or raise. The only way the state ever changes."""
        target = require_enum(to_state, EpisodeState, "episode.transition")
        allowed = ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if target not in allowed:
            raise TransitionError(
                f"episode {self.episode_id}: {self.state.value} -> {target.value} is "
                f"not a legal transition (legal: "
                f"{sorted(s.value for s in allowed) or 'none — terminal'})")
        if len(self.history) >= MAX_HISTORY:
            raise TransitionError(f"episode {self.episode_id}: history ceiling reached")
        self.history.append(TransitionRecord(from_state=self.state, to_state=target,
                                             actor=str(actor), at=at, reason=reason))
        self.state = target
        return target

    def add_attempt(self, trajectory: Trajectory) -> EpisodeAttempt:
        if len(self.attempts) >= MAX_ATTEMPTS:
            raise SchemaError(f"episode {self.episode_id}: attempt ceiling "
                              f"({MAX_ATTEMPTS}) reached")
        if trajectory.task_hash != self.spec.spec_hash():
            raise SchemaError(
                f"episode {self.episode_id}: trajectory is bound to task hash "
                f"{trajectory.task_hash[:12]} but this episode runs "
                f"{self.spec.spec_hash()[:12]}")
        attempt = EpisodeAttempt(trajectory=trajectory)
        self.attempts.append(attempt)
        return attempt

    @property
    def latest(self) -> EpisodeAttempt | None:
        return self.attempts[-1] if self.attempts else None

    # -- the approval guards ----------------------------------------------------
    def approval_blockers(self) -> tuple[str, ...]:
        """Every reason this episode may not be approved. Empty tuple = approvable.

        Re-derived from the evidence at call time. Nothing here trusts that an earlier
        stage already checked, because "an earlier stage already checked" is how a
        gate becomes decorative.
        """
        problems: list[str] = []
        attempt = self.latest
        if attempt is None:
            return ("no attempt recorded",)

        trajectory = attempt.trajectory

        # Deterministic evidence, re-verified.
        mandatory = tuple(self.spec.scoring.mandatory_graders)
        absent = trajectory.missing_graders(mandatory)
        if absent:
            problems.append(f"mandatory grader(s) never ran: {list(absent)}")
        for grader_id in mandatory:
            result = trajectory.grader(grader_id)
            if result is None:
                continue
            if result.status.is_blocking_when_mandatory:
                problems.append(f"mandatory grader {grader_id} returned "
                                f"{result.status.value}")
            elif result.status is ResultStatus.NOT_APPLICABLE:
                problems.append(f"mandatory grader {grader_id} did not apply; an "
                                f"attempt nothing measured is not approvable")

        # The score, re-computed rather than read back.
        reward = attempt.reward or attempt.score(self.spec)
        if reward.blocked:
            problems.append(f"reward blocked: {list(reward.block_reasons)}")
        elif not reward.meets(self.spec.scoring.min_total_score):
            problems.append(f"reward {reward.total:.3f} below the task minimum "
                            f"{self.spec.scoring.min_total_score:.3f}")

        # A cloud model's opinion is not evidence, and never overrides the above.
        for review in trajectory.teacher_reviews:
            if review.flags_unsafe:
                problems.append(f"teacher {review.provider} flagged unsafe behaviour: "
                                f"{list(review.unsafe_behavior)}")
            if review.recommendation is Recommendation.REJECT:
                problems.append(f"teacher {review.provider} recommends REJECT")

        # The human decision, bound by hash to this exact attempt.
        human = trajectory.human_review
        if human is None:
            problems.append("no human review recorded; a model's output never becomes "
                            "training data without an operator decision")
        else:
            if human.attempt_hash != trajectory.attempt_hash():
                problems.append("human review is bound to a different attempt")
            if not human.approves:
                problems.append(f"human decision is {human.decision.value}")

        # Task-level eligibility.
        if self.spec.evaluation_only:
            problems.append("task is evaluation_only and may never be trained on")
        if not self.spec.dataset_eligible:
            problems.append("task is marked not dataset eligible")
        if not self.spec.sensitivity.dataset_eligible:
            problems.append(f"task sensitivity {self.spec.sensitivity.value} is never "
                            f"trainable")
        return tuple(problems)

    def approve(self, *, actor: str, at: str = "", reason: str = "") -> EpisodeState:
        """Approve, or raise with every blocker. Legal only from NEEDS_HUMAN_REVIEW."""
        blockers = self.approval_blockers()
        if blockers:
            self.rejection_reasons.extend(blockers)
            raise TransitionError(
                f"episode {self.episode_id}: refusing to approve — "
                + "; ".join(blockers))
        return self.transition(EpisodeState.APPROVED, actor=actor, at=at, reason=reason)

    def reject(self, *, actor: str, reason: str, at: str = "") -> EpisodeState:
        if not str(reason or "").strip():
            raise SchemaError("episode: a rejection must state a reason")
        self.rejection_reasons.append(reason)
        return self.transition(EpisodeState.REJECTED, actor=actor, at=at, reason=reason)

    def quarantine(self, *, actor: str, reason: str, at: str = "") -> EpisodeState:
        """Isolate material that must never reach a dataset, in any role."""
        if not str(reason or "").strip():
            raise SchemaError("episode: a quarantine must state a reason")
        self.rejection_reasons.append(f"quarantined: {reason}")
        return self.transition(EpisodeState.QUARANTINED, actor=actor, at=at,
                               reason=reason)

    # -- sealing ----------------------------------------------------------------
    def outcome(self) -> EpisodeOutcome:
        """The sealed, hashable result. Dataset eligibility is derived, never set."""
        attempt = self.latest
        reward = attempt.reward if attempt else None
        eligible = (self.state.is_dataset_source
                    and self.spec.dataset_eligible
                    and not self.spec.evaluation_only
                    and self.spec.sensitivity.dataset_eligible
                    and bool(reward) and not reward.blocked)
        return EpisodeOutcome(
            episode_id=self.episode_id,
            task_id=self.spec.task_id,
            task_hash=self.spec.spec_hash(),
            state=self.state,
            attempt_hash=attempt.attempt_hash if attempt else "",
            dataset_eligible=bool(eligible),
            reward=reward.to_dict() if reward else {},
            rejection_reasons=tuple(self.rejection_reasons),
        )

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "gym_version": GYM_VERSION,
                "episode_id": self.episode_id, "task_id": self.spec.task_id,
                "task_hash": self.spec.spec_hash(), "state": self.state.value,
                "attempts": [a.to_dict() for a in self.attempts],
                "history": [h.to_dict() for h in self.history],
                "rejection_reasons": list(self.rejection_reasons),
                "outcome": self.outcome().to_dict()}


def human_decision_to_state(decision: HumanDecision) -> EpisodeState:
    """Map an operator decision onto the state it authorises."""
    return {
        HumanDecision.APPROVED: EpisodeState.APPROVED,
        HumanDecision.REJECTED: EpisodeState.REJECTED,
        HumanDecision.QUARANTINED: EpisodeState.QUARANTINED,
        HumanDecision.DEFERRED: EpisodeState.NEEDS_HUMAN_REVIEW,
    }[require_enum(decision, HumanDecision, "human decision")]


__all__ = [
    "ALLOWED_TRANSITIONS", "MAX_ATTEMPTS", "Episode", "EpisodeAttempt",
    "EpisodeOutcome", "EpisodeState", "TransitionError", "TransitionRecord",
    "human_decision_to_state",
]
