"""training_gym — V69 M62: a bounded practice environment for JARVIS.

WHAT THIS IS
------------
A factory for supervised training data, built so that every step from "the model
attempted something" to "this became training data" is explicit, typed, recorded and
testable. A task is specified and bounded; an attempt runs in a disposable sandbox; a
trajectory records exactly what happened; deterministic graders produce evidence;
teacher models offer opinions that are never evidence; a human approves or refuses;
only then may an example enter a versioned dataset.

WHAT THIS IS NOT
----------------
Not autonomous self-training. Not reinforcement learning — nothing here feeds a
signal back into a policy. Not continuous or automatic: no training starts because a
conversation ended. Not a hardened multi-tenant isolation boundary; the sandbox is a
locked-down container, and :doc:`TRAINING_GYM_SECURITY` states plainly what that does
and does not defend against.

THE INVARIANTS, AND WHERE THEY LIVE
-----------------------------------
  * *A check that did not run is not a pass.* :class:`~training_gym.schemas.
    ResultStatus` — ``SKIPPED``, ``ERROR`` and ``INSUFFICIENT_EVIDENCE`` block a
    mandatory grader exactly as hard as ``FAIL``.
  * *A policy that exists is already within bounds.* :mod:`training_gym.policies` —
    ceilings are enforced in ``__post_init__``, so no consumer can widen one.
  * *Unsafe work is unrepresentable.* :class:`~training_gym.task_spec.ActionKind` has
    no member that reaches a network, a real target or the operator's machine.
  * *No model output silently becomes training data.*
    :data:`~training_gym.episode.ALLOWED_TRANSITIONS` — ``APPROVED`` has exactly one
    predecessor, ``NEEDS_HUMAN_REVIEW``.
  * *A blocking failure dominates every positive score.*
    :func:`~training_gym.rewards.score_attempt` returns ``0.0``, not a weighted
    average that dilutes it.
  * *What leaves the host is scanned, not assumed clean.*
    :meth:`~training_gym.trajectory.Trajectory.to_export_dict` redacts and then
    refuses to emit if the scanner still finds something.

Importing this package performs no I/O, reads no environment, contacts no network and
downloads no model. Submodules that need Docker, cloud credentials or training
dependencies import them lazily and report their absence honestly.
"""
from __future__ import annotations

from .episode import (
    ALLOWED_TRANSITIONS,
    Episode,
    EpisodeAttempt,
    EpisodeOutcome,
    EpisodeState,
    TransitionError,
)
from .policies import (
    DEFAULT_ENV_ALLOWLIST,
    DETERMINISTIC_ENV,
    ArtifactPolicy,
    MountSpec,
    NetworkPolicy,
    NetworkSpec,
    ResourceBudget,
    SandboxPolicy,
    ScoringPolicy,
    credential_shaped,
    forbidden_mount,
)
from .rewards import (
    REWARD_VERSION,
    RewardBreakdown,
    RewardDimension,
    score_attempt,
)
from .schemas import (
    APPROVABLE_STATUSES,
    GYM_VERSION,
    SCHEMA_VERSION,
    ResultStatus,
    SchemaError,
    SensitivityClass,
    Severity,
    canonical_json,
    sha256_obj,
    sha256_text,
)
from .task_spec import (
    GRADER_IDS,
    ActionKind,
    FixtureRef,
    Provenance,
    TaskFamily,
    TaskSpec,
    unsafe_task_markers,
)
from .trajectory import (
    ActionRecord,
    ArtifactRecord,
    GraderResult,
    HumanDecision,
    HumanReview,
    MessageRole,
    ModelIdentity,
    ModelRole,
    Recommendation,
    TeacherReview,
    ToolCallRecord,
    Trajectory,
)
from .workspace import (
    EpisodeWorkspace,
    WorkspaceReport,
    canonicalize_within,
    safe_relative_path,
)

__version__ = GYM_VERSION

__all__ = [
    "ALLOWED_TRANSITIONS", "APPROVABLE_STATUSES", "DEFAULT_ENV_ALLOWLIST",
    "DETERMINISTIC_ENV", "GRADER_IDS", "GYM_VERSION", "REWARD_VERSION",
    "SCHEMA_VERSION", "ActionKind", "ActionRecord", "ArtifactPolicy",
    "ArtifactRecord", "Episode", "EpisodeAttempt", "EpisodeOutcome",
    "EpisodeState", "EpisodeWorkspace", "FixtureRef", "GraderResult",
    "HumanDecision", "HumanReview", "MessageRole", "ModelIdentity", "ModelRole",
    "MountSpec", "NetworkPolicy", "NetworkSpec", "Provenance", "Recommendation",
    "ResourceBudget", "ResultStatus", "RewardBreakdown", "RewardDimension",
    "SandboxPolicy", "SchemaError", "ScoringPolicy", "SensitivityClass",
    "Severity", "TaskFamily", "TaskSpec", "TeacherReview", "ToolCallRecord",
    "Trajectory", "TransitionError", "WorkspaceReport", "__version__",
    "canonical_json", "canonicalize_within", "credential_shaped",
    "forbidden_mount", "safe_relative_path", "score_attempt", "sha256_obj",
    "sha256_text", "unsafe_task_markers",
]
