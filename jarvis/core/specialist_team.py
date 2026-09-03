"""
core/specialist_team.py — V69 M65B: the specialist TEAM execution fabric.

M65A made one specialist real. ``SpecialistExecutor.run`` takes a typed request,
recomputes authority from the registry, reasons on a chosen model role, sends any
tool intent through ``ToolBroker`` -> ``ToolExecutor.aexecute``, and returns a
typed result. It is deliberately not a scheduler: it has no concept of a second
specialist, no queue, no DAG and no delegation.

This module is that scheduler, and only that. It is a controlled execution
fabric, not an agent swarm:

    SpecialistTeamPlan -> validate -> DAG -> TeamOrchestrator -> SpecialistTeamResult

WHAT IT REUSES (everything that decides anything)
=================================================
    who a specialist is         core.cognitive_mesh.REGISTRY
    how far one may go          core.cognitive_mesh.AutonomyLevel / permits
    one specialist's work       core.specialist_execution.SpecialistExecutor
    which model it reasons on   core.model_role_router.ModelRoleRouter
    which tools it may reach    core.specialist_runtime.ToolBroker
    the ONE effect path         tools.executor.ToolExecutor.aexecute
    exactly-once                that executor's own effect ledger
    human approval              core.specialist_execution.HitlApprovalRegistry
    whether the work holds up   core.mesh_verifier.verify (ARGUS)

There is no second registry, no second broker, no second ledger, no second
verifier, no second executor and no subprocess. Every authority question is
answered by ``SpecialistExecutor`` exactly as it was in M65A; this module decides
only WHEN a task may start and WHETHER two tasks may run at the same time.

THE FIVE THINGS A SCHEDULER MUST NOT BECOME
===========================================
    parallelism is not authority   two tasks running concurrently are each
                                   bounded by their own registry ceiling
    delegation is not authority    a child task is capped by its parent, and the
                                   proposal grammar has no field for a ceiling
    consensus is not authority     nothing counts agreeing specialists
    verification is not authority  TeamVerification.grants_authority is False
    a retry is not permission      a retry re-enters the SAME effect identity, so
                                   the ledger holds the effect count at one

BODY-SAFE BY CONSTRUCTION
=========================
Everything this module records is an id, a status, a counter, a duration or a
bounded human-readable summary assembled here. No chain-of-thought, no system
prompt, no raw tool payload, no secret.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from core.cognitive_mesh import (
    REGISTRY,
    AutonomyLevel,
    MeshBudget,
    SpecialistId,
    permits,
)
from core.mesh_contracts import EvidenceGraph, ToolCallStatus, Verdict
from core.model_role_router import router as _default_role_router
from core.security_scope import ActivityClass
from core.specialist_execution import (
    APPROVALS,
    ExecutionStatus,
    SpecialistExecutionRequest,
    SpecialistExecutionResult,
    _receipt_id,
)

logger = logging.getLogger("jarvis.specialist_team")


# ══════════════════════════════════════════════════════════════════════════════
#  Bounds (§12). Every one of these is a named constant, never a property of a
#  loop, so "how big can a team get" is answerable by reading one block.
# ══════════════════════════════════════════════════════════════════════════════
#: Tasks in one plan. Small on purpose: a plan that needs more than this is a
#: plan that has stopped being reviewable by the human who authorised it.
MAX_PLAN_TASKS = 8

#: Specialists reasoning at the same time. Three, matching the CPU-bound target
#: host's existing ``specialist_runtime._MAX_TOTAL_AGENTS`` convention of 4 minus
#: the primary that is streaming the answer.
MAX_PARALLEL_SPECIALISTS = 3

#: Effectful tasks running at the same time. Strictly tighter than the read-only
#: limit (§12): concurrency on observation costs latency, concurrency on effects
#: costs the world.
MAX_PARALLEL_EFFECTFUL = 1

#: Orchestrator-approved delegation depth. 0 is a task JARVIS planned; 1 is one
#: specialist-proposed child. There is no 2, so there is no agent tree.
MAX_DELEGATION_DEPTH = 1

#: Delegation proposals considered per plan, before any of them is validated.
MAX_DELEGATION_PROPOSALS = 2

#: Teams admitted concurrently, and plans allowed to wait for admission.
MAX_ACTIVE_TEAMS = 2
MAX_QUEUED_TEAMS = 2

#: Concurrent specialist executions per resolved model backend. Several roles
#: legitimately share one backend on this host (deep and verifier both resolve to
#: qwen3:8b on some hosts), so bounding roles would not bound the machine.
MAX_BACKEND_CONCURRENCY = 2

#: Retries across a whole plan, and per task.
MAX_TEAM_RETRIES = 3
MAX_TASK_RETRIES = 1

#: Wall clock for one plan and for one task, whatever the plan asks for.
MAX_TEAM_TIMEOUT_S = 180.0
MAX_TASK_TIMEOUT_S = 60.0
DEFAULT_TASK_TIMEOUT_S = 30.0

#: Resource claims one task may declare.
MAX_CLAIMS_PER_TASK = 8

MAX_OBJECTIVE = 800
MAX_REASON = 300
MAX_TRACE = 64
MAX_TRACE_CHARS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ══════════════════════════════════════════════════════════════════════════════
#  Task and team vocabulary (§9, §10)
# ══════════════════════════════════════════════════════════════════════════════
class TaskState(str, Enum):
    """Where one task in a plan actually got to.

    There is no member meaning "probably fine" and no member meaning "finished,
    unclear how". Every terminal state names a different thing a human would do
    about it: FAILED is a bug or a bad backend, DENIED is policy working,
    TIMED_OUT is a budget, CANCELLED is the operator, SKIPPED is a dependency
    that did not hold.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def succeeded(self) -> bool:
        return self is TaskState.SUCCESS


_TERMINAL_STATES = frozenset({
    TaskState.SUCCESS, TaskState.FAILED, TaskState.DENIED,
    TaskState.TIMED_OUT, TaskState.CANCELLED, TaskState.SKIPPED,
})

#: How one specialist execution status lands as a task state. TIMED_OUT and
#: CANCELLED keep their own identity rather than collapsing into FAILED: an
#: operator who cancelled a team must not read it as a malfunction.
_EXECUTION_STATE: "dict[ExecutionStatus, TaskState]" = {
    ExecutionStatus.SUCCESS: TaskState.SUCCESS,
    ExecutionStatus.PARTIAL: TaskState.FAILED,
    ExecutionStatus.FAILED: TaskState.FAILED,
    ExecutionStatus.DENIED: TaskState.DENIED,
    ExecutionStatus.TIMED_OUT: TaskState.TIMED_OUT,
    ExecutionStatus.CANCELLED: TaskState.CANCELLED,
}


class TeamStatus(str, Enum):
    """What the whole plan amounts to (§10).

    PARTIAL_SUCCESS exists because a single branch failing must not destroy an
    independent useful result. A team that answered two of three questions has
    not failed, and reporting it as SUCCESS would be worse than either.
    """

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALID = "invalid"


class DependencyPolicy(str, Enum):
    """What a task requires of the tasks it depends on (§8).

    ALL_SUCCESS is the minimum and the default: a task that reasons over a
    dependency's findings cannot run when there are none. ALL_TERMINAL exists
    for the genuinely different case — a summariser whose job is to report what
    happened, including that something failed — and is the only reason it is
    here at all.
    """

    ALL_SUCCESS = "all_success"
    ALL_TERMINAL = "all_terminal"


class EffectClass(str, Enum):
    """Whether a task is allowed to change anything.

    Declared by the planner and used for SCHEDULING only. It grants nothing: an
    EFFECTFUL task whose specialist sits at L1 still cannot execute an effect,
    because the ceiling is recomputed from the registry inside
    ``SpecialistExecutor``.
    """

    READ_ONLY = "read_only"
    EFFECTFUL = "effectful"


class ClaimMode(str, Enum):
    READ = "read"
    WRITE = "write"


class ConflictPolicy(str, Enum):
    """What the scheduler does about two tasks wanting the same resource.

    SERIALIZE, not DENY. A write/write pair is a scheduling problem, not a
    policy violation: both tasks were separately authorised, and refusing the
    second would make a legal plan fail for a reason the planner cannot see.
    Denial stays where denial belongs — capability, scope and autonomy.
    """

    PARALLEL = "parallel"
    SERIALIZE = "serialize"


# ══════════════════════════════════════════════════════════════════════════════
#  Resource claims (§13) and their canonical identity (§16)
# ══════════════════════════════════════════════════════════════════════════════
#: Claim kinds the scheduler understands. A claim of an unknown kind is a plan
#: validation error rather than an unscheduled free-for-all.
CLAIM_KINDS = frozenset({
    "file", "service", "process", "container", "host",
    "network-target", "world-state", "memory",
})

_MULTI_SLASH = re.compile(r"/{2,}")


def canonical_resource(kind: str, identity: str, *, base: str = "") -> str:
    """The scheduling identity of one resource.

    ``./foo``, ``foo`` and ``/base/foo`` must be the SAME write lock, or two
    tasks mutating one file would be scheduled in parallel because they spelled
    it differently. Normalisation is therefore lexical and total.

    It is also deliberately lexical ONLY. There is no ``realpath``, no
    ``os.stat`` and no symlink resolution: normalising a name must not touch the
    filesystem, both because the scheduler runs before any authority check and
    because resolving a path is exactly the operation an attacker would want a
    pre-authorisation component to perform on their behalf.

    A claim is scheduling metadata. It grants no access (§13); the tool it would
    be used by still passes capability, scope, autonomy and the executor's own
    gates.
    """
    kind = (kind or "").strip().lower()
    identity = (identity or "").strip()
    if kind == "file":
        text = identity.replace("\\", "/")
        if base and not text.startswith("/"):
            text = f"{base.rstrip('/')}/{text}"
        text = _MULTI_SLASH.sub("/", text)
        absolute = text.startswith("/")
        normalised = posixpath.normpath(text)
        if absolute and not normalised.startswith("/"):
            normalised = "/" + normalised.lstrip("/")
        identity = normalised
    elif kind == "network-target":
        identity = identity.lower().rstrip(".")
    else:
        identity = identity.lower()
    return f"{kind}:{identity}"


@dataclass(frozen=True)
class ResourceClaim:
    """What a task intends to touch, and how.

    Constructing one performs nothing and permits nothing. The scheduler reads
    it to decide ordering; every gate downstream is unaware it exists.
    """

    kind: str
    identity: str
    mode: ClaimMode = ClaimMode.READ
    base: str = ""

    @property
    def canonical(self) -> str:
        return canonical_resource(self.kind, self.identity, base=self.base)

    @property
    def writes(self) -> bool:
        return self.mode is ClaimMode.WRITE

    def conflicts_with(self, other: "ResourceClaim") -> bool:
        """READ+READ runs in parallel; anything with a WRITE serialises (§14)."""
        if self.canonical != other.canonical:
            return False
        return self.writes or other.writes

    def to_dict(self) -> dict:
        return {"kind": self.kind, "canonical": self.canonical,
                "mode": self.mode.value}


def conflict_policy(a: ResourceClaim, b: ResourceClaim) -> ConflictPolicy:
    """The documented policy, as a function so a test can assert the table."""
    return ConflictPolicy.SERIALIZE if a.conflicts_with(b) else ConflictPolicy.PARALLEL


# ══════════════════════════════════════════════════════════════════════════════
#  The plan (§6)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SpecialistTeamTask:
    """One node of a plan.

    Immutable, and — exactly like ``SpecialistExecutionRequest`` — NOT the source
    of truth for authority. ``autonomy`` and ``allowed_tools`` say what the PLAN
    permits; the executor intersects them with the registry and takes the
    minimum. A task can narrow an execution and can never widen one.
    """

    task_id: str
    specialist_id: SpecialistId
    objective: str
    capability: str = ""
    dependencies: "tuple[str, ...]" = ()
    dependency_policy: DependencyPolicy = DependencyPolicy.ALL_SUCCESS
    autonomy: AutonomyLevel = AutonomyLevel.ADVISE
    #: The targets THIS task may name, which must be a subset of the plan's.
    #: A task carries its own rather than reading the plan's because a delegated
    #: child is bounded by its PARENT, and a parent is frequently narrower than
    #: the team; without this field "child scope subset" could only be checked
    #: against the team ceiling, which is not the property §20 asks for.
    scope: "tuple[str, ...]" = ()
    model_role: "str | None" = None
    allowed_tools: "frozenset[str]" = frozenset()
    resource_claims: "tuple[ResourceClaim, ...]" = ()
    effect_class: EffectClass = EffectClass.READ_ONLY
    timeout_s: float = DEFAULT_TASK_TIMEOUT_S
    retry_limit: int = 0
    evidence_requirements: "tuple[str, ...]" = ()
    activity: "ActivityClass | None" = None
    context: str = ""
    #: 0 for a task JARVIS planned, 1 for an orchestrator-approved delegation.
    depth: int = 0
    parent_task_id: "str | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", _clip(self.objective, MAX_OBJECTIVE))
        object.__setattr__(self, "dependencies", tuple(dict.fromkeys(self.dependencies)))
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "scope", tuple(dict.fromkeys(self.scope)))
        object.__setattr__(self, "resource_claims",
                           tuple(self.resource_claims[:MAX_CLAIMS_PER_TASK]))
        timeout = float(self.timeout_s or 0.0)
        if timeout <= 0.0:
            timeout = DEFAULT_TASK_TIMEOUT_S
        object.__setattr__(self, "timeout_s", min(timeout, MAX_TASK_TIMEOUT_S))
        object.__setattr__(self, "retry_limit",
                           max(0, min(int(self.retry_limit), MAX_TASK_RETRIES)))

    @property
    def effectful(self) -> bool:
        return self.effect_class is EffectClass.EFFECTFUL

    @property
    def write_claims(self) -> "tuple[ResourceClaim, ...]":
        return tuple(c for c in self.resource_claims if c.writes)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "specialist_id": self.specialist_id.value,
            "objective": self.objective, "capability": self.capability,
            "dependencies": list(self.dependencies),
            "dependency_policy": self.dependency_policy.value,
            "autonomy": int(self.autonomy), "scope": list(self.scope),
            "model_role": self.model_role,
            "allowed_tools": sorted(self.allowed_tools),
            "resource_claims": [c.to_dict() for c in self.resource_claims],
            "effect_class": self.effect_class.value,
            "timeout_s": self.timeout_s, "retry_limit": self.retry_limit,
            "evidence_requirements": list(self.evidence_requirements),
            "activity": self.activity.value if self.activity else None,
            "depth": self.depth, "parent_task_id": self.parent_task_id,
        }

    def executable_identity(self) -> dict:
        """The part of this task that changes what actually runs (§48).

        Timestamps, prose context and human-facing text are excluded: two plans
        that differ only in when they were written are the same plan to execute,
        and an approval bound to one must bind to the other.
        """
        return {
            "task_id": self.task_id, "specialist_id": self.specialist_id.value,
            "capability": self.capability,
            "dependencies": sorted(self.dependencies),
            "dependency_policy": self.dependency_policy.value,
            "autonomy": int(self.autonomy), "scope": sorted(self.scope),
            "model_role": self.model_role,
            "allowed_tools": sorted(self.allowed_tools),
            "resource_claims": sorted(
                f"{c.canonical}|{c.mode.value}" for c in self.resource_claims),
            "effect_class": self.effect_class.value,
            "activity": self.activity.value if self.activity else None,
            "depth": self.depth,
        }


@dataclass(frozen=True)
class SpecialistTeamPlan:
    """One bounded, immutable, validated-before-execution team plan (§6)."""

    plan_id: str
    turn_id: str
    objective: str
    tasks: "tuple[SpecialistTeamTask, ...]" = ()
    scope: "tuple[str, ...]" = ()
    authority_ceiling: AutonomyLevel = AutonomyLevel.ADVISE
    execution_budget: MeshBudget = field(default_factory=MeshBudget)
    timeout_budget_s: float = MAX_TEAM_TIMEOUT_S
    retry_budget: int = MAX_TEAM_RETRIES
    max_parallelism: int = MAX_PARALLEL_SPECIALISTS
    delegation_depth: int = MAX_DELEGATION_DEPTH
    completion_policy: DependencyPolicy = DependencyPolicy.ALL_SUCCESS
    verification_policy: str = "team_argus"
    effect_epoch: str = ""
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", _clip(self.objective, MAX_OBJECTIVE))
        object.__setattr__(self, "max_parallelism",
                           max(1, min(int(self.max_parallelism),
                                      MAX_PARALLEL_SPECIALISTS)))
        object.__setattr__(self, "delegation_depth",
                           max(0, min(int(self.delegation_depth),
                                      MAX_DELEGATION_DEPTH)))
        object.__setattr__(self, "retry_budget",
                           max(0, min(int(self.retry_budget), MAX_TEAM_RETRIES)))
        timeout = float(self.timeout_budget_s or 0.0)
        if timeout <= 0.0:
            timeout = MAX_TEAM_TIMEOUT_S
        object.__setattr__(self, "timeout_budget_s", min(timeout, MAX_TEAM_TIMEOUT_S))

    @property
    def dependency_graph(self) -> "dict[str, tuple[str, ...]]":
        return {t.task_id: t.dependencies for t in self.tasks}

    @property
    def task_ids(self) -> "tuple[str, ...]":
        return tuple(t.task_id for t in self.tasks)

    def task(self, task_id: str) -> "SpecialistTeamTask | None":
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def dependents_of(self, task_id: str) -> "tuple[str, ...]":
        return tuple(t.task_id for t in self.tasks if task_id in t.dependencies)

    def with_task(self, task: SpecialistTeamTask) -> "SpecialistTeamPlan":
        """A new plan carrying one more task. The plan itself stays frozen, so a
        delegation produces a successor rather than mutating what was validated."""
        return replace(self, tasks=(*self.tasks, task))

    def canonical_identity(self) -> str:
        """A stable identity for what this plan EXECUTES (§48).

        Excludes ``created_at``, ``plan_id``, ``turn_id`` and every prose field,
        so replanning the same work yields the same identity and a change to what
        actually runs yields a different one. Useful for binding an approval, for
        replay protection and for audit.
        """
        payload = {
            "objective_digest": hashlib.sha256(
                self.objective.encode()).hexdigest()[:16],
            "scope": sorted(self.scope),
            "authority_ceiling": int(self.authority_ceiling),
            "completion_policy": self.completion_policy.value,
            "verification_policy": self.verification_policy,
            "max_parallelism": self.max_parallelism,
            "delegation_depth": self.delegation_depth,
            "tasks": sorted((t.executable_identity() for t in self.tasks),
                            key=lambda d: d["task_id"]),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return "plan:" + hashlib.sha256(blob.encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id, "turn_id": self.turn_id,
            "objective": self.objective, "tasks": [t.to_dict() for t in self.tasks],
            "dependency_graph": {k: list(v)
                                 for k, v in self.dependency_graph.items()},
            "scope": list(self.scope),
            "authority_ceiling": int(self.authority_ceiling),
            "execution_budget": self.execution_budget.to_dict(),
            "timeout_budget_s": self.timeout_budget_s,
            "retry_budget": self.retry_budget,
            "max_parallelism": self.max_parallelism,
            "delegation_depth": self.delegation_depth,
            "completion_policy": self.completion_policy.value,
            "verification_policy": self.verification_policy,
            "canonical_identity": self.canonical_identity(),
            "created_at": self.created_at,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Plan validation (§7) — before ANY task starts
# ══════════════════════════════════════════════════════════════════════════════
class PlanDefect(str, Enum):
    """Every way a plan can be refused. A closed set, so "the plan was rejected"
    is always answerable with which rule."""

    EMPTY = "empty_plan"
    TOO_MANY_TASKS = "too_many_tasks"
    DUPLICATE_TASK_ID = "duplicate_task_id"
    MISSING_DEPENDENCY = "missing_dependency"
    SELF_DEPENDENCY = "self_dependency"
    CYCLE = "cycle"
    UNKNOWN_SPECIALIST = "unknown_specialist"
    UNKNOWN_MODEL_ROLE = "unknown_model_role"
    ILLEGAL_AUTONOMY = "illegal_autonomy"
    AUTONOMY_ABOVE_CEILING = "autonomy_above_ceiling"
    CAPABILITY_MISMATCH = "capability_mismatch"
    SCOPE_EXPANSION = "scope_expansion"
    UNREGISTERED_TOOL = "unregistered_tool"
    BUDGET_OVERFLOW = "budget_overflow"
    DELEGATION_DEPTH = "delegation_depth_overflow"
    UNKNOWN_CLAIM_KIND = "unknown_claim_kind"
    EFFECT_CLASS_MISMATCH = "effect_class_mismatch"


@dataclass(frozen=True)
class PlanValidation:
    """The verdict on a plan. ``valid`` is derived from ``defects`` and is not a
    field, so a validator cannot report a clean plan by setting a flag."""

    defects: "tuple[tuple[PlanDefect, str], ...]" = ()

    @property
    def valid(self) -> bool:
        return not self.defects

    @property
    def codes(self) -> "tuple[PlanDefect, ...]":
        return tuple(d for d, _ in self.defects)

    def to_dict(self) -> dict:
        return {"valid": self.valid,
                "defects": [{"code": c.value, "detail": _clip(d, MAX_REASON)}
                            for c, d in self.defects]}


def _known_tool(tool: str) -> bool:
    """Whether the ONE executor implements this tool.

    Read from ``ToolBroker``'s category map and the executor's own handler
    surface rather than from a list kept here: a second list of tool names is a
    second thing to keep true.
    """
    from core.specialist_runtime import ToolBroker
    if tool in ToolBroker._TOOL_CATEGORY:
        return True
    from tools.executor import ToolExecutor
    return hasattr(ToolExecutor, f"_tool_{tool}")


def detect_cycle(graph: "dict[str, tuple[str, ...]]") -> "tuple[str, ...]":
    """A dependency cycle, as the node ids that form it, or ``()``.

    Iterative depth-first search with an explicit stack. Recursive would be
    shorter and would also mean a malformed plan could exhaust the interpreter's
    stack before the validator got to reject it.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in graph}
    for root in graph:
        if colour[root] != WHITE:
            continue
        stack: "list[tuple[str, int]]" = [(root, 0)]
        path: "list[str]" = []
        colour[root] = GREY
        path.append(root)
        while stack:
            node, index = stack[-1]
            deps = graph.get(node, ())
            if index >= len(deps):
                stack.pop()
                colour[node] = BLACK
                if path and path[-1] == node:
                    path.pop()
                continue
            stack[-1] = (node, index + 1)
            nxt = deps[index]
            if nxt not in colour:
                continue                      # missing dependency: a separate defect
            if colour[nxt] is GREY:
                start = path.index(nxt) if nxt in path else 0
                return tuple(path[start:]) + (nxt,)
            if colour[nxt] is WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, 0))
    return ()


def validate_plan(plan: SpecialistTeamPlan, *,
                  registry=REGISTRY) -> PlanValidation:
    """Everything §7 requires, before a single task becomes READY.

    Collects EVERY defect rather than returning the first: a planner told about
    one problem at a time will fix them one at a time, and a partially invalid
    DAG must never begin execution, so there is no benefit in stopping early.
    """
    defects: "list[tuple[PlanDefect, str]]" = []

    def bad(code: PlanDefect, detail: str) -> None:
        defects.append((code, _clip(detail, MAX_REASON)))

    if not plan.tasks:
        bad(PlanDefect.EMPTY, "a plan with no tasks executes nothing")
        return PlanValidation(tuple(defects))

    if len(plan.tasks) > MAX_PLAN_TASKS:
        bad(PlanDefect.TOO_MANY_TASKS,
            f"{len(plan.tasks)} tasks exceeds the {MAX_PLAN_TASKS}-task ceiling")

    seen: "set[str]" = set()
    for task in plan.tasks:
        if task.task_id in seen:
            bad(PlanDefect.DUPLICATE_TASK_ID,
                f"task id '{task.task_id}' appears more than once")
        seen.add(task.task_id)

    known = set(plan.task_ids)
    for task in plan.tasks:
        # ── graph shape ──────────────────────────────────────────────────────
        if task.task_id in task.dependencies:
            bad(PlanDefect.SELF_DEPENDENCY,
                f"task '{task.task_id}' depends on itself")
        for dep in task.dependencies:
            if dep not in known:
                bad(PlanDefect.MISSING_DEPENDENCY,
                    f"task '{task.task_id}' depends on unknown task '{dep}'")

        # ── identity ─────────────────────────────────────────────────────────
        if task.specialist_id not in registry:
            bad(PlanDefect.UNKNOWN_SPECIALIST,
                f"'{task.specialist_id}' is not a registered specialist")
            continue
        record = registry.get(task.specialist_id)

        # ── model role ───────────────────────────────────────────────────────
        if task.model_role is not None:
            from core.model_router import ModelRole
            try:
                ModelRole(str(task.model_role))
            except ValueError:
                bad(PlanDefect.UNKNOWN_MODEL_ROLE,
                    f"task '{task.task_id}' names model role "
                    f"'{task.model_role}', which does not exist")

        # ── autonomy ─────────────────────────────────────────────────────────
        if task.autonomy is AutonomyLevel.PROHIBITED:
            bad(PlanDefect.ILLEGAL_AUTONOMY,
                f"task '{task.task_id}' asks for L4 PROHIBITED, which is never "
                f"executable")
        if int(task.autonomy) > int(plan.authority_ceiling):
            bad(PlanDefect.AUTONOMY_ABOVE_CEILING,
                f"task '{task.task_id}' asks for L{int(task.autonomy)} above the "
                f"plan's L{int(plan.authority_ceiling)} ceiling")

        # ── scope ────────────────────────────────────────────────────────────
        if not scope_subset(task.scope, plan.scope):
            bad(PlanDefect.SCOPE_EXPANSION,
                f"task '{task.task_id}' names target(s) outside the team's "
                f"scope: {','.join(sorted(set(task.scope) - set(plan.scope)))}")

        # ── capability ───────────────────────────────────────────────────────
        # A declared capability must be one the RECORD actually holds. This is
        # not the authority check — that is the executor's — it is the earlier
        # question of whether the plan is coherent at all.
        if task.capability:
            from core.specialist_runtime import ToolCategory
            try:
                category = ToolCategory(task.capability)
            except ValueError:
                bad(PlanDefect.CAPABILITY_MISMATCH,
                    f"'{task.capability}' is not a tool category")
            else:
                if not record.capability_allowed(category):
                    bad(PlanDefect.CAPABILITY_MISMATCH,
                        f"{record.codename} may not use capability "
                        f"'{task.capability}'")

        # ── tools ────────────────────────────────────────────────────────────
        for tool in sorted(task.allowed_tools):
            if not _known_tool(tool):
                bad(PlanDefect.UNREGISTERED_TOOL,
                    f"task '{task.task_id}' allows '{tool}', which no executor "
                    f"implements")

        # ── claims ───────────────────────────────────────────────────────────
        for claim in task.resource_claims:
            if claim.kind.strip().lower() not in CLAIM_KINDS:
                bad(PlanDefect.UNKNOWN_CLAIM_KIND,
                    f"task '{task.task_id}' claims unknown resource kind "
                    f"'{claim.kind}'")
        if task.write_claims and not task.effectful:
            bad(PlanDefect.EFFECT_CLASS_MISMATCH,
                f"task '{task.task_id}' declares a WRITE claim but calls itself "
                f"read-only; the scheduler would under-serialise it")

        # ── delegation depth ─────────────────────────────────────────────────
        if task.depth > plan.delegation_depth:
            bad(PlanDefect.DELEGATION_DEPTH,
                f"task '{task.task_id}' sits at delegation depth {task.depth}, "
                f"above the plan's {plan.delegation_depth}")
        if task.depth > MAX_DELEGATION_DEPTH:
            bad(PlanDefect.DELEGATION_DEPTH,
                f"task '{task.task_id}' sits at delegation depth {task.depth}, "
                f"above the module ceiling {MAX_DELEGATION_DEPTH}")

    # ── cycles, once the ids are known to exist ──────────────────────────────
    cycle = detect_cycle(plan.dependency_graph)
    if cycle:
        bad(PlanDefect.CYCLE, "dependency cycle: " + " -> ".join(cycle))

    # ── budget ───────────────────────────────────────────────────────────────
    if len(plan.tasks) > plan.execution_budget.max_specialists:
        bad(PlanDefect.BUDGET_OVERFLOW,
            f"{len(plan.tasks)} tasks exceeds the budget's "
            f"{plan.execution_budget.max_specialists} specialists")
    if plan.timeout_budget_s > MAX_TEAM_TIMEOUT_S:
        bad(PlanDefect.BUDGET_OVERFLOW,
            f"team timeout {plan.timeout_budget_s}s exceeds "
            f"{MAX_TEAM_TIMEOUT_S}s")

    return PlanValidation(tuple(defects))


def scope_subset(child: "tuple[str, ...]", parent: "tuple[str, ...]") -> bool:
    """Whether *child* is a subset of *parent*.

    An empty parent means "no scope was granted", so an empty child is the only
    subset of it. Treating an empty parent as "everything" is precisely the
    fail-open reading this repository refuses everywhere else.
    """
    return set(child).issubset(set(parent))


# ══════════════════════════════════════════════════════════════════════════════
#  Delegation (§19, §20, §21)
# ══════════════════════════════════════════════════════════════════════════════
#: A specialist proposes a delegation in ONE strict shape, and the grammar has no
#: key for autonomy, scope, capability, approval, depth or verification — exactly
#: as ``ToolIntent`` has no field for them. A specialist therefore has nowhere to
#: put a request for more authority, which is what makes "delegation cannot
#: escalate" structural rather than policed.
_DELEGATION_MARKER = re.compile(r"DELEGATE\s*:\s*(?=\{)", re.IGNORECASE)

_ALLOWED_DELEGATION_KEYS = frozenset({"specialist", "objective", "why", "tools"})


@dataclass(frozen=True)
class DelegationProposal:
    """What one specialist ASKS the orchestrator to do. Constructing one
    performs nothing and creates no task."""

    proposed_by: SpecialistId
    parent_task_id: str
    specialist: str
    objective: str
    why: str = ""
    tools: "tuple[str, ...]" = ()

    def to_dict(self) -> dict:
        return {"proposed_by": self.proposed_by.value,
                "parent_task_id": self.parent_task_id,
                "specialist": self.specialist,
                "objective": _clip(self.objective, MAX_REASON),
                "why": _clip(self.why, MAX_REASON), "tools": list(self.tools)}


class DelegationDenial(str, Enum):
    DEPTH = "depth_exceeded"
    UNKNOWN_SPECIALIST = "unknown_specialist"
    NOT_A_PERMITTED_HANDOFF = "handoff_not_permitted"
    TOOL_ESCALATION = "tool_escalation"
    SCOPE_ESCALATION = "scope_escalation"
    AUTHORITY_ESCALATION = "authority_escalation"
    BUDGET = "budget_exhausted"
    PLAN_FULL = "plan_full"
    CANCELLED = "team_cancelled"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class DelegationDecision:
    allowed: bool
    task: "SpecialistTeamTask | None" = None
    denial: "DelegationDenial | None" = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"allowed": self.allowed,
                "task_id": self.task.task_id if self.task else None,
                "denial": self.denial.value if self.denial else None,
                "reason": _clip(self.reason, MAX_REASON)}


def parse_delegation_proposals(text: str, *, proposed_by: SpecialistId,
                               parent_task_id: str,
                               limit: int = MAX_DELEGATION_PROPOSALS
                               ) -> "tuple[tuple[DelegationProposal, ...], tuple[str, ...]]":
    """Extract delegation proposals from a specialist's output.

    Brace-balanced and string-aware for the same measured reason the tool-intent
    scanner is: a non-greedy ``\\{.*?\\}`` closes on the inner brace of any object
    carrying a nested value, so every real proposal would parse as malformed and
    be dropped, and the specialist would appear to have asked for nothing.

    Unknown keys are DROPPED and reported. A model that writes
    ``"autonomy": 3`` alongside a legitimate proposal gets the proposal and
    nothing else.
    """
    proposals: "list[DelegationProposal]" = []
    warnings: "list[str]" = []
    for raw in _balanced_blocks(text or "", _DELEGATION_MARKER, limit):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            warnings.append("a malformed DELEGATE block was discarded")
            continue
        if not isinstance(payload, dict):
            warnings.append("a non-object DELEGATE was discarded")
            continue
        rejected = sorted(set(payload) - _ALLOWED_DELEGATION_KEYS)
        if rejected:
            warnings.append(
                "ignored delegation field(s) a specialist may not set: "
                + ",".join(rejected))
        specialist = str(payload.get("specialist", "")).strip().lower()
        objective = str(payload.get("objective", "")).strip()
        if not specialist or not objective:
            warnings.append("a DELEGATE naming no specialist or objective was "
                            "discarded")
            continue
        tools = payload.get("tools")
        tool_names = tuple(str(t).strip() for t in tools[:8]) \
            if isinstance(tools, list) else ()
        proposals.append(DelegationProposal(
            proposed_by=proposed_by, parent_task_id=parent_task_id,
            specialist=specialist, objective=objective[:MAX_OBJECTIVE],
            why=str(payload.get("why", ""))[:MAX_REASON],
            tools=tuple(t for t in tool_names if t)))
    return tuple(proposals), tuple(warnings)


def _balanced_blocks(text: str, marker: re.Pattern, limit: int) -> "tuple[str, ...]":
    """Every ``{...}`` object following *marker*, balanced and string-aware."""
    out: "list[str]" = []
    for found in marker.finditer(text or ""):
        start = found.end()
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start:index + 1])
                    break
        if len(out) >= limit:
            break
    return tuple(out)


def authorize_delegation(proposal: DelegationProposal, *,
                         parent: SpecialistTeamTask,
                         plan: SpecialistTeamPlan,
                         effective_parent_autonomy: AutonomyLevel,
                         remaining_budget: int,
                         registry=REGISTRY) -> DelegationDecision:
    """Turn one proposal into a child task, or refuse it (§20).

    Every check here is a SEPARATE refusal with its own code, because "the
    delegation was denied" is not an operable answer — an operator needs to know
    whether their team hit a depth limit or tried to escalate.

    The child's authority is COMPUTED, never taken from the proposal:

        autonomy   = min(parent's effective ceiling, the plan's ceiling)
        scope      = the parent's scope, unchanged
        tools      = a subset of the parent's, and naming anything outside it is
                     a refusal rather than a silent intersection

    A silent intersection would be safe and would also be untestable: a mutation
    that removed the check would produce the same behaviour, so the control
    would look present while proving nothing.
    """
    depth = parent.depth + 1
    if depth > plan.delegation_depth or depth > MAX_DELEGATION_DEPTH:
        return DelegationDecision(
            False, denial=DelegationDenial.DEPTH,
            reason=(f"delegation to depth {depth} exceeds the maximum "
                    f"{min(plan.delegation_depth, MAX_DELEGATION_DEPTH)}; a "
                    f"specialist may not build an agent tree"))

    try:
        child_id = SpecialistId(proposal.specialist)
    except ValueError:
        return DelegationDecision(
            False, denial=DelegationDenial.UNKNOWN_SPECIALIST,
            reason=f"'{proposal.specialist}' is not a registered specialist")
    if child_id not in registry:
        return DelegationDecision(
            False, denial=DelegationDenial.UNKNOWN_SPECIALIST,
            reason=f"'{proposal.specialist}' is not a registered specialist")

    if not registry.handoff_allowed(parent.specialist_id, child_id):
        return DelegationDecision(
            False, denial=DelegationDenial.NOT_A_PERMITTED_HANDOFF,
            reason=(f"{registry.get(parent.specialist_id).codename} may not hand "
                    f"off to {registry.get(child_id).codename}"))

    extra = sorted(set(proposal.tools) - set(parent.allowed_tools))
    if extra:
        return DelegationDecision(
            False, denial=DelegationDenial.TOOL_ESCALATION,
            reason=(f"the delegation asks for tool(s) the parent was not given: "
                    f"{','.join(extra)}"))

    # The child INHERITS the parent's scope, and the check is against the PARENT
    # rather than the team: a parent narrowed to one host must not be able to
    # produce a child that reaches every host the team was authorised for.
    child_scope = parent.scope
    if not scope_subset(child_scope, parent.scope) or \
            not scope_subset(child_scope, plan.scope):
        return DelegationDecision(
            False, denial=DelegationDenial.SCOPE_ESCALATION,
            reason=("a delegated task may not name a target outside its "
                    "parent's scope"))

    child_autonomy = min(effective_parent_autonomy, plan.authority_ceiling, key=int)
    if int(child_autonomy) > int(effective_parent_autonomy) or \
            int(child_autonomy) > int(plan.authority_ceiling):
        return DelegationDecision(
            False, denial=DelegationDenial.AUTHORITY_ESCALATION,
            reason=(f"a delegated task may not exceed its parent's authority "
                    f"(L{int(effective_parent_autonomy)}) or the team's "
                    f"(L{int(plan.authority_ceiling)})"))

    if remaining_budget <= 0:
        return DelegationDecision(
            False, denial=DelegationDenial.BUDGET,
            reason="the team's execution budget has no room for another task")
    if len(plan.tasks) >= MAX_PLAN_TASKS:
        return DelegationDecision(
            False, denial=DelegationDenial.PLAN_FULL,
            reason=f"the plan already holds the maximum {MAX_PLAN_TASKS} tasks")

    child = SpecialistTeamTask(
        task_id=f"{parent.task_id}.d{len(plan.tasks)}",
        specialist_id=child_id,
        objective=proposal.objective,
        dependencies=(),
        dependency_policy=DependencyPolicy.ALL_SUCCESS,
        autonomy=child_autonomy,
        scope=child_scope,
        allowed_tools=frozenset(proposal.tools),
        resource_claims=(),
        effect_class=EffectClass.READ_ONLY,
        timeout_s=parent.timeout_s,
        retry_limit=0,
        activity=parent.activity,
        depth=depth,
        parent_task_id=parent.task_id,
        context=parent.context,
    )
    return DelegationDecision(True, task=child,
                              reason=f"delegation approved at depth {depth}")


# ══════════════════════════════════════════════════════════════════════════════
#  Cancellation (§25) and backpressure (§28)
# ══════════════════════════════════════════════════════════════════════════════
class CancellationToken:
    """Cooperative, one-way cancellation for one team.

    One way on purpose: a token that could be un-cancelled would let a race
    resurrect a team the operator stopped. ``reason`` is bounded text a human
    wrote or the orchestrator assembled, never a payload.
    """

    __slots__ = ("_cancelled", "_reason", "_at")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._at = 0.0

    def cancel(self, reason: str = "operator cancelled the team") -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = _clip(reason, MAX_REASON)
        self._at = time.monotonic()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def __call__(self) -> bool:
        """So a token can be handed straight to ``SpecialistExecutor.run``."""
        return self._cancelled


class Admission(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    REJECTED = "rejected"


@dataclass
class TeamAdmissionController:
    """Bounded admission for team plans (§28, §29).

    Below the limit a plan is ACCEPTED. At the limit it is QUEUED, and only while
    the queue itself has room. Above both it is REJECTED — explicitly, with a
    reason, and without allocating anything. There is no branch that grows a
    list to whatever arrives, which is the only property that actually bounds
    memory.
    """

    max_active: int = MAX_ACTIVE_TEAMS
    max_queued: int = MAX_QUEUED_TEAMS
    active: int = 0
    queued: int = 0
    rejected: int = 0

    def admit(self) -> "tuple[Admission, str]":
        if self.active < self.max_active:
            self.active += 1
            return Admission.ACCEPTED, "capacity available"
        if self.queued < self.max_queued:
            self.queued += 1
            return (Admission.QUEUED,
                    f"{self.active} team(s) already running; queued behind them")
        self.rejected += 1
        return (Admission.REJECTED,
                f"team fabric is at capacity ({self.max_active} running, "
                f"{self.max_queued} queued); this plan was not started")

    def _slot_free(self) -> "asyncio.Event":
        """Signalled whenever a team releases its slot. Created lazily so this
        controller can be a module singleton built before any event loop."""
        event = getattr(self, "_free_event", None)
        if event is None:
            event = asyncio.Event()
            self._free_event = event
        return event

    def promote(self) -> bool:
        """Move one queued plan into a free slot, if there is one."""
        if self.queued > 0 and self.active < self.max_active:
            self.queued -= 1
            self.active += 1
            return True
        return False

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        try:
            self._slot_free().set()
        except RuntimeError:  # pragma: no cover — released outside a loop
            pass

    def to_dict(self) -> dict:
        return {"max_active": self.max_active, "max_queued": self.max_queued,
                "active": self.active, "queued": self.queued,
                "rejected": self.rejected}


class BackendLimiter:
    """Concurrency ceiling per resolved model backend (§30).

    Several specialists legitimately route to one backend — on this host both
    ``deep`` and ``verifier`` can resolve to the same model — so bounding roles
    would not bound the machine. Bounding the backend does.

    Queueing for a backend changes WHEN a specialist reasons and nothing else.
    It cannot change what the specialist may do, because authority is recomputed
    from the registry inside the executor, after this limiter has already let go.
    """

    def __init__(self, limit: int = MAX_BACKEND_CONCURRENCY) -> None:
        self.limit = max(1, int(limit))
        self._semaphores: "dict[str, asyncio.Semaphore]" = {}
        self.waits = 0
        self.peak: "dict[str, int]" = {}
        self._held: "dict[str, int]" = {}

    def _semaphore(self, backend: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(backend)
        if sem is None:
            sem = asyncio.Semaphore(self.limit)
            self._semaphores[backend] = sem
        return sem

    async def acquire(self, backend: str) -> None:
        sem = self._semaphore(backend)
        if sem.locked():
            self.waits += 1
        await sem.acquire()
        held = self._held.get(backend, 0) + 1
        self._held[backend] = held
        self.peak[backend] = max(self.peak.get(backend, 0), held)

    def release(self, backend: str) -> None:
        sem = self._semaphores.get(backend)
        if sem is not None:
            sem.release()
        self._held[backend] = max(0, self._held.get(backend, 0) - 1)

    def to_dict(self) -> dict:
        return {"limit": self.limit, "backends": sorted(self._semaphores),
                "waits": self.waits, "peak": dict(sorted(self.peak.items()))}


# ══════════════════════════════════════════════════════════════════════════════
#  The conflict scheduler (§14, §15)
# ══════════════════════════════════════════════════════════════════════════════
class ResourceArbiter:
    """Holds the resource reservations of every RUNNING task.

    §15 — no lock, no effect. A task's claims are reserved BEFORE its execution
    is started and released only after it is terminal, so there is no window in
    which a policy check has passed, the world has changed underneath it and the
    effect then runs. The reservation is not an authority: it decides ordering,
    and every gate still runs inside the execution it ordered.
    """

    def __init__(self) -> None:
        self._held: "dict[str, list[ResourceClaim]]" = {}
        self.serializations = 0
        self.conflicts: "list[str]" = []

    def blocking(self, task: SpecialistTeamTask) -> "tuple[str, ...]":
        """Which running task ids block *task*, and on which resource."""
        blockers: "list[str]" = []
        for task_id, claims in self._held.items():
            for held in claims:
                if any(held.conflicts_with(c) for c in task.resource_claims):
                    blockers.append(f"{task_id}:{held.canonical}")
                    break
        return tuple(blockers)

    def reserve(self, task: SpecialistTeamTask) -> bool:
        """Take every claim of *task*, or take none.

        All-or-nothing because a partial reservation is a deadlock generator:
        two tasks each holding half of what the other needs would both wait
        forever, and the scheduler would report neither as running.
        """
        blockers = self.blocking(task)
        if blockers:
            self.serializations += 1
            self.conflicts.append(
                _clip(f"{task.task_id} waits on {','.join(blockers[:3])}",
                      MAX_TRACE_CHARS))
            return False
        if task.resource_claims:
            self._held[task.task_id] = list(task.resource_claims)
        return True

    def release(self, task: SpecialistTeamTask) -> None:
        self._held.pop(task.task_id, None)

    @property
    def held_count(self) -> int:
        return len(self._held)

    def to_dict(self) -> dict:
        return {"held": self.held_count, "serializations": self.serializations,
                "conflicts": self.conflicts[:8]}


# ══════════════════════════════════════════════════════════════════════════════
#  Results (§6)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TeamTaskResult:
    """What happened to one node.

    ``execution`` is the M65A result, unchanged and unedited, so every receipt,
    every model selection and every ARGUS verdict a single specialist produced is
    still available exactly as it was. The team layer adds ordering facts around
    it and rewrites nothing inside it.
    """

    task_id: str
    specialist_id: SpecialistId
    state: TaskState
    execution: "SpecialistExecutionResult | None" = None
    reason: str = ""
    attempts: int = 0
    depth: int = 0
    parent_task_id: "str | None" = None
    #: Monotonic ordering marks. Used to PROVE overlap and ordering rather than
    #: to infer them from wall-clock sleeps, which is the difference between a
    #: parallelism claim and a parallelism proof (§11, §35).
    started_seq: int = -1
    finished_seq: int = -1
    started_at: float = 0.0
    finished_at: float = 0.0
    reserved: "tuple[str, ...]" = ()

    @property
    def receipts(self) -> tuple:
        return self.execution.tool_receipts if self.execution else ()

    @property
    def executed_effects(self) -> int:
        return self.execution.executed_effects if self.execution else 0

    @property
    def evidence_ids(self) -> "tuple[str, ...]":
        return self.execution.evidence_ids if self.execution else ()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "specialist_id": self.specialist_id.value,
            "state": self.state.value, "reason": _clip(self.reason, MAX_REASON),
            "attempts": self.attempts, "depth": self.depth,
            "parent_task_id": self.parent_task_id,
            "started_seq": self.started_seq, "finished_seq": self.finished_seq,
            "reserved": list(self.reserved),
            "execution": self.execution.to_dict() if self.execution else None,
        }


@dataclass(frozen=True)
class TeamVerification:
    """ARGUS's verdict on a whole plan (§44).

    ``grants_authority`` is a property returning ``False`` with no constructor
    argument behind it, exactly as ``VerifierVerdict.grants_authority`` and
    ``RoleSelection.grants_authority`` are. §38 is therefore a property of the
    type rather than a promise made about it: there is no way to build a
    verification that permits anything.
    """

    verdict: Verdict
    reasons: "tuple[str, ...]" = ()
    task_verdicts: "tuple[tuple[str, str], ...]" = ()
    checked: "tuple[str, ...]" = ()

    @property
    def passing(self) -> bool:
        """The repository's own reading, imported rather than re-decided.

        ``PASSING_VERDICTS`` already answers "may the answer state a conclusion
        as established", and a team verdict that used a stricter rule would make
        the same run pass at task level and fail at team level for no reason an
        operator could act on.
        """
        from core.mesh_contracts import PASSING_VERDICTS
        return self.verdict in PASSING_VERDICTS

    @property
    def grants_authority(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "passing": self.passing,
                "grants_authority": self.grants_authority,
                "reasons": [_clip(r, MAX_REASON) for r in self.reasons[:8]],
                "task_verdicts": [{"task_id": t, "verdict": v}
                                  for t, v in self.task_verdicts],
                "checked": list(self.checked)}


@dataclass(frozen=True)
class SpecialistTeamResult:
    """What a whole plan amounts to (§6).

    Every count is DERIVED from ``task_results``. None is stored, so a team
    cannot report zero failures by saying so, and ``status`` is checked against
    the derived counts by team ARGUS rather than trusted.
    """

    plan_id: str
    plan_identity: str
    status: TeamStatus
    task_results: "tuple[TeamTaskResult, ...]" = ()
    validation: PlanValidation = field(default_factory=PlanValidation)
    verification: "TeamVerification | None" = None
    delegations: "tuple[DelegationDecision, ...]" = ()
    budget_usage: "dict" = field(default_factory=dict)
    body_safe_trace: "tuple[str, ...]" = ()
    cancelled_reason: str = ""
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "body_safe_trace", tuple(
            _clip(t, MAX_TRACE_CHARS) for t in self.body_safe_trace[:MAX_TRACE] if t))

    # ── derived facts, never stored ──────────────────────────────────────────
    def _in(self, state: TaskState) -> "tuple[str, ...]":
        return tuple(r.task_id for r in self.task_results if r.state is state)

    @property
    def completed(self) -> "tuple[str, ...]":
        return self._in(TaskState.SUCCESS)

    @property
    def failed(self) -> "tuple[str, ...]":
        return self._in(TaskState.FAILED) + self._in(TaskState.TIMED_OUT)

    @property
    def denied(self) -> "tuple[str, ...]":
        return self._in(TaskState.DENIED)

    @property
    def skipped(self) -> "tuple[str, ...]":
        return self._in(TaskState.SKIPPED)

    @property
    def cancelled(self) -> "tuple[str, ...]":
        return self._in(TaskState.CANCELLED)

    @property
    def specialists_executed(self) -> "tuple[str, ...]":
        """Specialists that actually REASONED — not merely appeared in the plan.

        A SKIPPED or CANCELLED task ran no specialist, and counting it would be
        exactly how a team could claim more participants than it had.
        """
        return tuple(sorted({r.specialist_id.value for r in self.task_results
                             if r.execution is not None}))

    @property
    def evidence(self) -> "tuple[str, ...]":
        out: "list[str]" = []
        for result in self.task_results:
            out.extend(result.evidence_ids)
        return tuple(dict.fromkeys(out))

    @property
    def receipts(self) -> tuple:
        out: list = []
        for result in self.task_results:
            out.extend(result.receipts)
        return tuple(out)

    @property
    def executed_effects(self) -> int:
        return sum(r.executed_effects for r in self.task_results)

    @property
    def deduplicated_effects(self) -> int:
        return sum(r.execution.deduplicated_effects for r in self.task_results
                   if r.execution is not None)

    @property
    def parallel_overlaps(self) -> int:
        """How many task pairs were genuinely running at the same time.

        Computed from the recorded start/finish sequence numbers, so it is a
        measurement of what the scheduler did rather than a claim about what it
        was asked to do.
        """
        spans = [(r.started_seq, r.finished_seq) for r in self.task_results
                 if r.started_seq >= 0 and r.finished_seq >= 0]
        overlaps = 0
        for i, (a_start, a_end) in enumerate(spans):
            for b_start, b_end in spans[i + 1:]:
                if a_start < b_end and b_start < a_end:
                    overlaps += 1
        return overlaps

    @property
    def verified(self) -> bool:
        return self.verification is not None and self.verification.passing

    def result_for(self, task_id: str) -> "TeamTaskResult | None":
        for r in self.task_results:
            if r.task_id == task_id:
                return r
        return None

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id, "plan_identity": self.plan_identity,
            "status": self.status.value,
            "task_results": [r.to_dict() for r in self.task_results],
            "validation": self.validation.to_dict(),
            "verification": (self.verification.to_dict()
                             if self.verification else None),
            "verified": self.verified,
            "delegations": [d.to_dict() for d in self.delegations],
            "completed": list(self.completed), "failed": list(self.failed),
            "denied": list(self.denied), "skipped": list(self.skipped),
            "cancelled": list(self.cancelled),
            "specialists_executed": list(self.specialists_executed),
            "evidence": list(self.evidence),
            "receipts": [r.to_dict() for r in self.receipts],
            "executed_effects": self.executed_effects,
            "deduplicated_effects": self.deduplicated_effects,
            "parallel_overlaps": self.parallel_overlaps,
            "budget_usage": dict(self.budget_usage),
            "cancelled_reason": _clip(self.cancelled_reason, MAX_REASON),
            "duration_ms": round(self.duration_ms, 1),
            "body_safe_trace": list(self.body_safe_trace),
            "timestamp": self.timestamp,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Observability (§46) — counters and ids only, never a payload
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class TeamCounters:
    direct_routes: int = 0
    one_specialist_routes: int = 0
    team_routes: int = 0
    plans_validated: int = 0
    plans_rejected: int = 0
    teams_run: int = 0
    team_tasks: int = 0
    tasks_parallelised: int = 0
    dependency_waits: int = 0
    conflict_serializations: int = 0
    delegation_proposals: int = 0
    delegations_approved: int = 0
    delegations_denied: int = 0
    cancellations: int = 0
    queue_rejections: int = 0
    queue_deferrals: int = 0
    backend_waits: int = 0
    retries: int = 0
    partial_successes: int = 0
    argus_rejections: int = 0
    team_verdicts: "dict[str, int]" = field(default_factory=dict)

    def record(self, result: SpecialistTeamResult) -> None:
        self.teams_run += 1
        self.team_tasks += len(result.task_results)
        self.tasks_parallelised += result.parallel_overlaps
        self.retries += sum(max(0, r.attempts - 1) for r in result.task_results)
        if result.status is TeamStatus.PARTIAL_SUCCESS:
            self.partial_successes += 1
        if result.status is TeamStatus.CANCELLED:
            self.cancellations += 1
        for decision in result.delegations:
            self.delegation_proposals += 1
            if decision.allowed:
                self.delegations_approved += 1
            else:
                self.delegations_denied += 1
        if result.verification is not None:
            key = result.verification.verdict.value
            self.team_verdicts[key] = self.team_verdicts.get(key, 0) + 1
            if not result.verification.passing:
                self.argus_rejections += 1

    def to_dict(self) -> dict:
        return {k: (dict(sorted(v.items())) if isinstance(v, dict) else v)
                for k, v in sorted(vars(self).items())}

    def reset(self) -> None:
        for name, value in list(vars(self).items()):
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                setattr(self, name, 0)
            elif isinstance(value, dict):
                value.clear()


COUNTERS = TeamCounters()
ADMISSION = TeamAdmissionController()


# ══════════════════════════════════════════════════════════════════════════════
#  The orchestrator (§8) — the ONLY thing that starts a specialist
# ══════════════════════════════════════════════════════════════════════════════
class TeamOrchestrator:
    """Executes a validated plan as a DAG, under bounds, and returns one result.

    It answers exactly two questions: WHEN may a task start, and MAY two tasks
    run at the same time. It answers no question about permission — every one of
    those is still answered inside ``SpecialistExecutor`` by reading the
    registry, which is why a scheduling decision can never become an authority
    decision here.

    Dependency-injected end to end (``executor``, ``role_router``, ``admission``,
    ``backend_limiter``, ``counters``) so every control is testable without a
    model, a socket or an Ollama host.
    """

    def __init__(self, *, executor=None, role_router=None,
                 counters: "TeamCounters | None" = None,
                 admission: "TeamAdmissionController | None" = None,
                 backend_limiter: "BackendLimiter | None" = None) -> None:
        self._injected_executor = executor
        self._router = role_router if role_router is not None else _default_role_router
        self._counters = counters if counters is not None else COUNTERS
        self._admission = admission if admission is not None else ADMISSION
        self._backends = backend_limiter

    @property
    def _executor(self):
        """The M65A executor this orchestrator runs tasks through.

        Resolved LAZILY rather than snapshotted in ``__init__``. Binding it at
        construction made the module singleton's executor depend on WHEN this
        module was first imported: import before boot and it captured an
        unwired executor, import after and it captured whatever was current.
        Production survived that by accident — ``attach_live_runtime`` mutates
        the singleton in place, so the identity never changed — and it was a
        genuine fault waiting for the first caller that rebound the name.
        """
        if self._injected_executor is not None:
            return self._injected_executor
        from core.specialist_execution import executor as live_executor
        return live_executor

    @property
    def available(self) -> bool:
        return bool(getattr(self._executor, "available", False))

    # ── the run ─────────────────────────────────────────────────────────────
    async def run(self, plan: SpecialistTeamPlan, *,
                  graph: "EvidenceGraph | None" = None,
                  token: "CancellationToken | None" = None,
                  verify_with_argus: bool = True,
                  admit: bool = True) -> SpecialistTeamResult:
        """Run one plan. Never raises.

        Order is load-bearing and there is no branch that reorders it:

            validate  ->  admit  ->  schedule  ->  verify

        Validation comes first because §7 forbids a partially invalid DAG from
        beginning execution, and "first" here means before admission, before a
        semaphore and before a single specialist is constructed.
        """
        started = time.monotonic()
        graph = graph if graph is not None else EvidenceGraph()
        token = token if token is not None else CancellationToken()
        trace: "list[str]" = []

        validation = validate_plan(plan)
        if not validation.valid:
            self._counters.plans_rejected += 1
            trace.append(f"plan rejected: {len(validation.defects)} defect(s)")
            for code, detail in validation.defects[:6]:
                trace.append(f"{code.value}: {detail}")
            logger.warning("M65B: plan %s rejected (%s)", plan.plan_id,
                           ",".join(c.value for c, _ in validation.defects[:4]))
            return SpecialistTeamResult(
                plan_id=plan.plan_id, plan_identity=plan.canonical_identity(),
                status=TeamStatus.INVALID, validation=validation,
                body_safe_trace=tuple(trace),
                duration_ms=(time.monotonic() - started) * 1000.0)
        self._counters.plans_validated += 1

        released = False
        if admit:
            decision, reason = self._admission.admit()
            trace.append(f"admission: {decision.value} — {reason}")
            if decision is Admission.REJECTED:
                self._counters.queue_rejections += 1
                logger.warning("M65B: plan %s refused admission (%s)",
                               plan.plan_id, reason)
                return SpecialistTeamResult(
                    plan_id=plan.plan_id,
                    plan_identity=plan.canonical_identity(),
                    status=TeamStatus.FAILED, validation=validation,
                    body_safe_trace=tuple(trace), cancelled_reason=reason,
                    duration_ms=(time.monotonic() - started) * 1000.0)
            if decision is Admission.QUEUED:
                self._counters.queue_deferrals += 1
                promoted = await self._await_slot(plan, token)
                if not promoted:
                    self._admission.queued = max(0, self._admission.queued - 1)
                    trace.append("queued plan was never promoted before its "
                                 "timeout; nothing was started")
                    return SpecialistTeamResult(
                        plan_id=plan.plan_id,
                        plan_identity=plan.canonical_identity(),
                        status=TeamStatus.FAILED, validation=validation,
                        body_safe_trace=tuple(trace),
                        cancelled_reason="team fabric stayed at capacity",
                        duration_ms=(time.monotonic() - started) * 1000.0)
        try:
            result = await self._schedule(plan, graph, token, trace, validation,
                                          started, verify_with_argus)
        finally:
            if admit and not released:
                self._admission.release()
        self._counters.record(result)
        logger.info(
            "M65B: team %s status=%s tasks=%d ran=%d overlaps=%d effects=%d "
            "deduped=%d ms=%.0f",
            plan.plan_id, result.status.value, len(result.task_results),
            len(result.specialists_executed), result.parallel_overlaps,
            result.executed_effects, result.deduplicated_effects,
            result.duration_ms)
        return result

    async def _await_slot(self, plan: SpecialistTeamPlan,
                          token: CancellationToken) -> bool:
        """Wait for a free team slot, bounded by the plan's own timeout.

        Bounded because an unbounded wait is how a queue stops being
        backpressure and starts being a memory leak with better manners.
        """
        deadline = time.monotonic() + plan.timeout_budget_s
        event = self._admission._slot_free()
        while time.monotonic() < deadline:
            if token.cancelled:
                return False
            if self._admission.promote():
                return True
            event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue
        return False

    # ── the DAG loop ────────────────────────────────────────────────────────
    async def _schedule(self, plan: SpecialistTeamPlan, graph: EvidenceGraph,
                        token: CancellationToken, trace: "list[str]",
                        validation: PlanValidation, started: float,
                        verify_with_argus: bool) -> SpecialistTeamResult:
        arbiter = ResourceArbiter()
        backends = self._backends if self._backends is not None else BackendLimiter()
        deadline = started + plan.timeout_budget_s
        seq = 0

        tasks: "dict[str, SpecialistTeamTask]" = {t.task_id: t for t in plan.tasks}
        states: "dict[str, TaskState]" = {t: TaskState.PENDING for t in tasks}
        results: "dict[str, TeamTaskResult]" = {}
        attempts: "dict[str, int]" = {t: 0 for t in tasks}
        running: "dict[asyncio.Task, str]" = {}
        active_effectful = 0
        delegations: "list[DelegationDecision]" = []
        retries_left = plan.retry_budget
        current_plan = plan

        def _next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        def _finalise(task_id: str, state: TaskState, *, reason: str = "",
                      execution=None, started_seq: int = -1,
                      finished_seq: int = -1, started_at: float = 0.0,
                      reserved: "tuple[str, ...]" = ()) -> None:
            task = tasks[task_id]
            states[task_id] = state
            results[task_id] = TeamTaskResult(
                task_id=task_id, specialist_id=task.specialist_id, state=state,
                execution=execution, reason=reason, attempts=attempts[task_id],
                depth=task.depth, parent_task_id=task.parent_task_id,
                started_seq=started_seq, finished_seq=finished_seq,
                started_at=started_at, finished_at=time.monotonic(),
                reserved=reserved)

        def _dependencies_satisfied(task: SpecialistTeamTask) -> "bool | None":
            """True when *task* may run, False when it can never run, None when
            it is still waiting. Three answers because a two-valued version
            cannot tell "not yet" from "never", and that is exactly the
            distinction between a dependency wait and a SKIP."""
            waiting = False
            for dep in task.dependencies:
                state = states.get(dep)
                if state is None:                      # pragma: no cover
                    return False
                if not state.terminal:
                    waiting = True
                    continue
                if task.dependency_policy is DependencyPolicy.ALL_SUCCESS \
                        and not state.succeeded:
                    return False
            return None if waiting else True

        async def _worker(task: SpecialistTeamTask, attempt: int
                          ) -> "tuple[str, SpecialistExecutionResult | None, int, float, str]":
            """Run ONE task through the M65A executor, bounded by a backend slot.

            Returns the ordering marks the parallelism and dependency proofs read.
            The specialist itself is run by ``SpecialistExecutor``; nothing here
            re-decides anything it decides.
            """
            backend = self._backend_for(task)
            await backends.acquire(backend)
            try:
                if token.cancelled:
                    return task.task_id, None, -1, 0.0, backend
                request = self._request_for(current_plan, task, attempt)
                start_seq = _next_seq()
                start_at = time.monotonic()
                execution = await self._executor.run(
                    request, graph=graph,
                    verify_with_argus=_task_needs_argus(current_plan, task),
                    cancelled=token)
                return task.task_id, execution, start_seq, start_at, backend
            finally:
                backends.release(backend)

        # ── the loop ────────────────────────────────────────────────────────
        while True:
            # 1. Propagate unsatisfiable dependencies. A failed branch skips its
            #    dependents and leaves every independent branch alone (§10).
            progressed = True
            while progressed:
                progressed = False
                for task_id, task in tasks.items():
                    if states[task_id] is not TaskState.PENDING:
                        continue
                    verdict = _dependencies_satisfied(task)
                    if verdict is False:
                        attempts[task_id] = 0
                        _finalise(task_id, TaskState.SKIPPED,
                                  reason=("a dependency did not satisfy this "
                                          "task's ALL_SUCCESS policy"))
                        trace.append(f"{task_id}: skipped on a failed dependency")
                        progressed = True

            # 2. Cancellation stops NEW work immediately (§25).
            if token.cancelled:
                for task_id in tasks:
                    if states[task_id] in (TaskState.PENDING, TaskState.READY):
                        _finalise(task_id, TaskState.CANCELLED,
                                  reason=token.reason or "team cancelled")
                if not running:
                    break

            # 3. Start whatever may start, in plan order so the schedule is
            #    deterministic rather than dependent on dict iteration luck.
            if not token.cancelled:
                for task in current_plan.tasks:
                    task_id = task.task_id
                    if states[task_id] is not TaskState.PENDING:
                        continue
                    if _dependencies_satisfied(task) is not True:
                        self._counters.dependency_waits += 1
                        continue
                    if len(running) >= current_plan.max_parallelism:
                        break
                    if task.effectful and active_effectful >= MAX_PARALLEL_EFFECTFUL:
                        continue
                    # §15 — the reservation happens BEFORE the execution starts
                    # and is held until it is terminal, so there is no window in
                    # which a check has passed and the resource has moved.
                    if not arbiter.reserve(task):
                        self._counters.conflict_serializations += 1
                        trace.append(
                            f"{task_id}: serialised behind "
                            f"{','.join(arbiter.blocking(task)[:2])}")
                        continue
                    states[task_id] = TaskState.RUNNING
                    attempts[task_id] += 1
                    if task.effectful:
                        active_effectful += 1
                    handle = asyncio.ensure_future(
                        _worker(task, attempts[task_id]))
                    running[handle] = task_id

            if not running:
                if token.cancelled:
                    break
                if all(states[t].terminal for t in tasks):
                    break
                # Nothing running and nothing startable: every remaining task is
                # blocked by a conflict that can never clear, which validation
                # already proved impossible for dependencies. Refuse to spin.
                for task_id in tasks:
                    if not states[task_id].terminal:
                        _finalise(task_id, TaskState.FAILED,
                                  reason="no runnable task remained")
                break

            # 4. Wait for the first completion, bounded by the team deadline.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                token.cancel("the team exceeded its timeout budget")
                trace.append("team timeout: no further task may start")
                handles = list(running.items())
                for handle, task_id in handles:
                    handle.cancel()
                    task = tasks[task_id]
                    arbiter.release(task)
                    if task.effectful:
                        active_effectful -= 1
                    _finalise(task_id, TaskState.TIMED_OUT,
                              reason="the team's timeout budget expired",
                              finished_seq=_next_seq())
                running.clear()
                # Await the cancellations rather than detaching them. A handle
                # that is cancelled and never awaited keeps running in the
                # background, so the team would report itself finished while its
                # specialists were still reasoning — the one thing a timeout is
                # supposed to prevent.
                await asyncio.gather(*(h for h, _ in handles),
                                     return_exceptions=True)
                continue

            done, _pending = await asyncio.wait(
                list(running), return_when=asyncio.FIRST_COMPLETED,
                timeout=remaining)
            if not done:
                continue

            for handle in done:
                task_id = running.pop(handle)
                task = tasks[task_id]
                arbiter.release(task)
                if task.effectful:
                    active_effectful -= 1
                finish_seq = _next_seq()
                reserved = tuple(c.canonical for c in task.resource_claims)
                try:
                    _tid, execution, start_seq, start_at, _backend = handle.result()
                except asyncio.CancelledError:
                    _finalise(task_id, TaskState.CANCELLED,
                              reason=token.reason or "task cancelled",
                              finished_seq=finish_seq, reserved=reserved)
                    continue
                except Exception as exc:  # noqa: BLE001 — a broken worker fails
                    logger.warning("M65B: task %s raised (%s)", task_id,
                                   type(exc).__name__)
                    _finalise(task_id, TaskState.FAILED,
                              reason=f"the task raised {type(exc).__name__}",
                              finished_seq=finish_seq, reserved=reserved)
                    continue

                if execution is None:
                    _finalise(task_id, TaskState.CANCELLED,
                              reason=token.reason or "cancelled before execution",
                              finished_seq=finish_seq, reserved=reserved)
                    continue

                state = _EXECUTION_STATE.get(execution.status, TaskState.FAILED)

                # 5. Retry, finitely. A retry re-enters the SAME objective and
                #    therefore the same effect identity, so the ledger — not this
                #    branch — is what keeps the effect count at one (§18).
                if (state is TaskState.FAILED and retries_left > 0
                        and attempts[task_id] <= task.retry_limit
                        and not token.cancelled):
                    retries_left -= 1
                    states[task_id] = TaskState.PENDING
                    trace.append(f"{task_id}: retry {attempts[task_id]} of "
                                 f"{task.retry_limit}")
                    continue

                _finalise(task_id, state, execution=execution,
                          reason=execution.summary[:MAX_REASON],
                          started_seq=start_seq, finished_seq=finish_seq,
                          started_at=start_at, reserved=reserved)
                trace.append(f"{task_id}: {state.value} "
                             f"({execution.specialist_id.value})")

                # 6. Delegation. The specialist ASKED; the orchestrator decides.
                if state is TaskState.SUCCESS and not token.cancelled:
                    current_plan, decisions = self._consider_delegations(
                        current_plan, task, execution, tasks, states, attempts,
                        trace)
                    delegations.extend(decisions)

        # ── status, derived from what happened ──────────────────────────────
        ordered = tuple(results[t] for t in current_plan.task_ids if t in results)
        status = derive_team_status(ordered, cancelled=token.cancelled)
        result = SpecialistTeamResult(
            plan_id=plan.plan_id, plan_identity=current_plan.canonical_identity(),
            status=status, task_results=ordered, validation=validation,
            delegations=tuple(delegations),
            budget_usage={
                "tasks": len(ordered),
                "retries_used": plan.retry_budget - retries_left,
                "retries_left": retries_left,
                "max_parallelism": current_plan.max_parallelism,
                "conflict_serializations": arbiter.serializations,
                "backends": backends.to_dict(),
                "elapsed_s": round(time.monotonic() - started, 3),
                "timeout_budget_s": plan.timeout_budget_s,
            },
            body_safe_trace=tuple(trace),
            cancelled_reason=token.reason,
            duration_ms=(time.monotonic() - started) * 1000.0)

        if verify_with_argus:
            verification = verify_team(current_plan, result)
            result = replace(result, verification=verification)
            trace.append(f"team argus: {verification.verdict.value}")
            result = replace(result, body_safe_trace=tuple(trace))
        return result

    # ── helpers ─────────────────────────────────────────────────────────────
    def _backend_for(self, task: SpecialistTeamTask) -> str:
        """Which backend this task will reason on, resolved through the SAME
        router the executor will use.

        Resolving it twice is safe because ``ModelRoleRouter.select`` is pure and
        deterministic: it reads the registry and the model router's precedence
        ladder and mutates nothing. Bounding the backend requires knowing it
        before the execution starts, and asking the one router is the only way to
        know it without inventing a second answer.
        """
        try:
            selection = self._router.select(
                task.specialist_id, preferred=task.model_role)
            return selection.backend or "unavailable"
        except Exception:  # noqa: BLE001 — an unroutable task still gets a slot
            return "unavailable"

    @staticmethod
    def _request_for(plan: SpecialistTeamPlan, task: SpecialistTeamTask,
                     attempt: int) -> SpecialistExecutionRequest:
        """The M65A request for one node.

        ``autonomy_level`` is the MINIMUM of the task's and the plan's, and the
        executor then takes the minimum of that and the registry's. Three
        narrowings and no widening anywhere on the path.
        """
        return SpecialistExecutionRequest(
            execution_id=f"{plan.plan_id}:{task.task_id}:{attempt}",
            plan_id=plan.plan_id,
            specialist_id=task.specialist_id,
            objective=task.objective,
            capability=task.capability,
            model_role=task.model_role,
            autonomy_level=min(task.autonomy, plan.authority_ceiling, key=int),
            authorized_scope=task.scope or plan.scope,
            allowed_tools=task.allowed_tools,
            activity=task.activity,
            budget=plan.execution_budget,
            deadline_s=task.timeout_s,
            evidence_requirements=task.evidence_requirements,
            effect_epoch=plan.effect_epoch or f"plan:{plan.plan_id}",
            task_class="team",
            context=task.context,
        )

    def _consider_delegations(self, plan: SpecialistTeamPlan,
                              parent: SpecialistTeamTask,
                              execution: SpecialistExecutionResult,
                              tasks: dict, states: dict, attempts: dict,
                              trace: "list[str]"
                              ) -> "tuple[SpecialistTeamPlan, tuple[DelegationDecision, ...]]":
        """Validate a finished task's delegation proposals and admit the legal ones.

        This is the whole of §19: a specialist emits a proposal, the orchestrator
        validates it, and only the orchestrator constructs a task. There is no
        ``specialist.spawn`` because there is no method on any specialist that
        returns a task, and this is the only call site that builds one.
        """
        proposals, warnings = parse_delegation_proposals(
            execution.summary, proposed_by=parent.specialist_id,
            parent_task_id=parent.task_id)
        for warning in warnings:
            trace.append(f"{parent.task_id}: {warning}")
        if not proposals:
            return plan, ()
        decisions: "list[DelegationDecision]" = []
        for proposal in proposals:
            decision = authorize_delegation(
                proposal, parent=parent, plan=plan,
                effective_parent_autonomy=execution.effective_autonomy,
                remaining_budget=plan.execution_budget.max_specialists
                - len(plan.tasks))
            decisions.append(decision)
            if not decision.allowed:
                trace.append(f"{parent.task_id}: delegation denied — "
                             f"{decision.denial.value if decision.denial else '?'}")
                logger.info("M65B: delegation denied (%s) from %s",
                            decision.denial.value if decision.denial else "?",
                            parent.task_id)
                continue
            child = decision.task
            plan = plan.with_task(child)
            # The successor plan is re-validated in full. A child that would make
            # the plan illegal is refused HERE rather than trusted because the
            # guard above liked it: two independent checks, and the cheaper one
            # is not allowed to be the only one.
            check = validate_plan(plan)
            if not check.valid:
                plan = replace(plan, tasks=plan.tasks[:-1])
                decisions[-1] = DelegationDecision(
                    False, denial=DelegationDenial.MALFORMED,
                    reason=("the delegated task would make the plan invalid: "
                            + ",".join(c.value for c, _ in check.defects[:3])))
                trace.append(f"{parent.task_id}: delegation would invalidate "
                             f"the plan")
                continue
            tasks[child.task_id] = child
            states[child.task_id] = TaskState.PENDING
            attempts[child.task_id] = 0
            trace.append(f"{parent.task_id}: delegated {child.task_id} "
                         f"({child.specialist_id.value}) at depth {child.depth}")
            logger.info("M65B: delegation approved %s -> %s depth=%d",
                        parent.task_id, child.task_id, child.depth)
        return plan, tuple(decisions)

    def status(self) -> dict:
        """Body-safe runtime status (§47). Answerable without an LLM."""
        return {
            "team_fabric_enabled": True,
            "executor_available": self.available,
            "max_plan_tasks": MAX_PLAN_TASKS,
            "max_parallel_specialists": MAX_PARALLEL_SPECIALISTS,
            "max_parallel_effectful": MAX_PARALLEL_EFFECTFUL,
            "max_delegation_depth": MAX_DELEGATION_DEPTH,
            "max_team_retries": MAX_TEAM_RETRIES,
            "max_team_timeout_s": MAX_TEAM_TIMEOUT_S,
            "admission": self._admission.to_dict(),
            "backend_concurrency_limit": MAX_BACKEND_CONCURRENCY,
            "counters": self._counters.to_dict(),
        }


#: Verification policies a plan may declare. TEAM verifies the plan's own
#: structure; PER_TASK_AND_TEAM also asks M65A's ARGUS about every node.
VERIFICATION_TEAM = "team_argus"
VERIFICATION_PER_TASK_AND_TEAM = "per_task_and_team"


def _task_needs_argus(plan: SpecialistTeamPlan,
                      task: SpecialistTeamTask) -> bool:
    """Whether M65A's per-task ARGUS runs for this node.

    Team verification does NOT imply it. The two answer different questions —
    per-task ARGUS asks whether one specialist's claims are bound to evidence,
    team ARGUS asks whether the PLAN holds together — and running the expensive
    one on every node by default would make VERIFIED so rare that an operator
    would learn to ignore it.

    It runs where the plan actually asked for evidence, and wherever the plan
    declares the stricter policy.
    """
    if plan.verification_policy == VERIFICATION_PER_TASK_AND_TEAM:
        return True
    return bool(task.evidence_requirements)


def derive_team_status(results: "tuple[TeamTaskResult, ...]", *,
                       cancelled: bool = False) -> TeamStatus:
    """The plan's status, computed from its nodes (§10).

    Deliberately a free function so team ARGUS can recompute it independently and
    compare, rather than reading the status the orchestrator claimed.
    """
    if not results:
        return TeamStatus.CANCELLED if cancelled else TeamStatus.FAILED
    states = [r.state for r in results]
    successes = sum(1 for s in states if s is TaskState.SUCCESS)
    if cancelled or any(s is TaskState.CANCELLED for s in states):
        return TeamStatus.CANCELLED if successes == 0 else TeamStatus.PARTIAL_SUCCESS
    if successes == len(states):
        return TeamStatus.SUCCESS
    if successes == 0:
        return TeamStatus.FAILED
    return TeamStatus.PARTIAL_SUCCESS


# ══════════════════════════════════════════════════════════════════════════════
#  Team ARGUS (§44) — verifies a plan's result. Authorizes nothing (§38).
# ══════════════════════════════════════════════════════════════════════════════
#: Every check team ARGUS performs, named so the verdict says what was looked at
#: rather than only what was wrong. A check absent from a verification is a check
#: that did not run, which is itself reportable.
TEAM_CHECKS = (
    "node_status_consistency",
    "dependency_satisfaction",
    "required_evidence",
    "receipt_validity",
    "authority_compliance",
    "scope_compliance",
    "effect_identity",
    "conflict_policy",
    "claimed_status",
)


def verify_team(plan: SpecialistTeamPlan,
                result: SpecialistTeamResult) -> TeamVerification:
    """Verify a team result against the plan that produced it.

    ARGUS reads FACTS: node states, dependency edges, receipts, effect
    identities and the recorded overlap trace. It does not read a specialist's
    prose as authority and it is not asked whether an action was permitted —
    that was decided before it ran, by the executor, and no verdict here can
    retroactively permit or refuse one.

    Every check is written so that WEAKENING it changes an answer. A check that
    can only ever pass tells an operator nothing and hides the day it should
    have failed.
    """
    reasons: "list[str]" = []
    task_verdicts: "list[tuple[str, str]]" = []
    by_id = {r.task_id: r for r in result.task_results}
    epoch = plan.effect_epoch or f"plan:{plan.plan_id}"

    # ── 1. node status consistency ──────────────────────────────────────────
    for node in result.task_results:
        if not node.state.terminal:
            reasons.append(
                f"task '{node.task_id}' finished the plan in non-terminal state "
                f"'{node.state.value}'")
        if node.state is TaskState.SUCCESS and node.execution is None:
            reasons.append(
                f"task '{node.task_id}' claims SUCCESS with no execution behind "
                f"it; a task that never ran cannot have succeeded")
        if node.execution is not None and node.state is TaskState.SUCCESS \
                and node.execution.status is not ExecutionStatus.SUCCESS:
            reasons.append(
                f"task '{node.task_id}' claims SUCCESS while its execution "
                f"reported '{node.execution.status.value}'")
        if node.state in (TaskState.SKIPPED, TaskState.CANCELLED) \
                and node.execution is not None:
            reasons.append(
                f"task '{node.task_id}' is {node.state.value} yet carries an "
                f"execution result; a task that did not run produced nothing")

    # ── 2. dependency satisfaction ──────────────────────────────────────────
    for task in plan.tasks:
        node = by_id.get(task.task_id)
        if node is None or node.state is not TaskState.SUCCESS:
            continue
        for dep in task.dependencies:
            parent = by_id.get(dep)
            if parent is None:
                reasons.append(
                    f"task '{task.task_id}' succeeded but its dependency '{dep}' "
                    f"has no result at all")
                continue
            if task.dependency_policy is DependencyPolicy.ALL_SUCCESS \
                    and parent.state is not TaskState.SUCCESS:
                reasons.append(
                    f"task '{task.task_id}' requires ALL_SUCCESS but ran on "
                    f"dependency '{dep}' in state '{parent.state.value}'")
            elif not parent.state.terminal:
                reasons.append(
                    f"task '{task.task_id}' ran before dependency '{dep}' reached "
                    f"a terminal state")
            # Ordering, from the recorded marks rather than from wall clock.
            if (node.started_seq >= 0 and parent.finished_seq >= 0
                    and node.started_seq < parent.finished_seq):
                reasons.append(
                    f"task '{task.task_id}' started before its dependency "
                    f"'{dep}' finished")

    # ── 3. required evidence ────────────────────────────────────────────────
    for task in plan.tasks:
        node = by_id.get(task.task_id)
        if node is None or node.state is not TaskState.SUCCESS:
            continue
        if task.evidence_requirements and not node.evidence_ids:
            reasons.append(
                f"task '{task.task_id}' declares evidence requirements and "
                f"produced no evidence, yet reports success")

    # ── 4. receipt validity ─────────────────────────────────────────────────
    # A receipt id is recomputed with the SAME function that mints one, so a
    # forged or edited receipt does not match. Copying the formula here instead
    # would let the two drift and the check would quietly stop meaning anything.
    for node in result.task_results:
        for receipt in node.receipts:
            expected = _receipt_id(receipt.specialist_id, receipt.effect_identity)
            if receipt.receipt_id != expected:
                reasons.append(
                    f"task '{node.task_id}' carries a receipt whose id does not "
                    f"match its own specialist and effect identity")
            if receipt.specialist_id is not node.specialist_id:
                reasons.append(
                    f"task '{node.task_id}' carries a receipt attributed to "
                    f"{receipt.specialist_id.value}, which did not run it")
            if receipt.executed and receipt.status is ToolCallStatus.DENIED:
                reasons.append(
                    f"task '{node.task_id}' has a receipt that is both DENIED and "
                    f"executed; a refused call changed nothing")

    # ── 5. authority and 6. scope ───────────────────────────────────────────
    for task in plan.tasks:
        node = by_id.get(task.task_id)
        if node is None or node.execution is None:
            continue
        if int(node.execution.effective_autonomy) > int(plan.authority_ceiling):
            reasons.append(
                f"task '{task.task_id}' executed at L"
                f"{int(node.execution.effective_autonomy)} above the team's "
                f"L{int(plan.authority_ceiling)} ceiling")
        if not scope_subset(task.scope, plan.scope):
            reasons.append(
                f"task '{task.task_id}' named a target outside the team's scope")

    # ── 7. effect identity ──────────────────────────────────────────────────
    for node in result.task_results:
        for receipt in node.receipts:
            if not receipt.effect_identity.startswith(epoch + "|"):
                reasons.append(
                    f"task '{node.task_id}' carries a receipt keyed to an effect "
                    f"epoch this plan did not open")

    # ── 8. conflict policy ──────────────────────────────────────────────────
    # Two tasks whose spans overlapped must not have held conflicting claims.
    # Read from what was recorded, so this catches a scheduler that ignored the
    # arbiter as well as one that never consulted it.
    spans = [(n, plan.task(n.task_id)) for n in result.task_results
             if n.started_seq >= 0 and n.finished_seq >= 0]
    for i, (node_a, task_a) in enumerate(spans):
        for node_b, task_b in spans[i + 1:]:
            if task_a is None or task_b is None:
                continue
            if not (node_a.started_seq < node_b.finished_seq
                    and node_b.started_seq < node_a.finished_seq):
                continue
            for claim_a in task_a.resource_claims:
                for claim_b in task_b.resource_claims:
                    if claim_a.conflicts_with(claim_b):
                        reasons.append(
                            f"tasks '{task_a.task_id}' and '{task_b.task_id}' ran "
                            f"concurrently while both claiming "
                            f"{claim_a.canonical}, one of them for WRITE")

    # ── 9. the claimed status ───────────────────────────────────────────────
    derived = derive_team_status(
        result.task_results,
        cancelled=bool(result.cancelled) or bool(result.cancelled_reason))
    if derived is not result.status and result.status is not TeamStatus.INVALID:
        reasons.append(
            f"the team reports '{result.status.value}' but its own nodes derive "
            f"'{derived.value}'")

    # ── per-task ARGUS verdicts, carried through unchanged ──────────────────
    for node in result.task_results:
        if node.execution is not None and node.execution.verification is not None:
            task_verdicts.append(
                (node.task_id, node.execution.verification.verdict.value))
            if not node.execution.verification.passing:
                reasons.append(
                    f"task '{node.task_id}' did not pass its own ARGUS check: "
                    f"{node.execution.verification.verdict.value}")

    if reasons:
        verdict = Verdict.SCOPE_VIOLATION if any("scope" in r for r in reasons) \
            else Verdict.FAILED
        return TeamVerification(verdict, reasons=tuple(reasons[:12]),
                                task_verdicts=tuple(task_verdicts),
                                checked=TEAM_CHECKS)

    limitations = bool(result.failed or result.denied or result.skipped
                       or result.cancelled)
    return TeamVerification(
        Verdict.VERIFIED_WITH_LIMITATIONS if limitations else Verdict.VERIFIED,
        reasons=(("some tasks did not complete; the team result is partial",)
                 if limitations else ()),
        task_verdicts=tuple(task_verdicts), checked=TEAM_CHECKS)


#: The module singleton, mirroring ``specialist_execution.executor``.
orchestrator = TeamOrchestrator()


def team_fabric_status() -> dict:
    """Body-safe status of the team fabric (§47), without an LLM or a socket."""
    return orchestrator.status()


__all__ = [
    "ADMISSION", "COUNTERS", "CLAIM_KINDS", "MAX_ACTIVE_TEAMS",
    "MAX_BACKEND_CONCURRENCY", "MAX_DELEGATION_DEPTH", "MAX_PARALLEL_EFFECTFUL",
    "MAX_PARALLEL_SPECIALISTS", "MAX_PLAN_TASKS", "MAX_QUEUED_TEAMS",
    "MAX_TASK_RETRIES", "MAX_TEAM_RETRIES", "MAX_TEAM_TIMEOUT_S", "TEAM_CHECKS",
    "Admission", "BackendLimiter", "CancellationToken", "ClaimMode",
    "ConflictPolicy", "DelegationDecision", "DelegationDenial",
    "DelegationProposal", "DependencyPolicy", "EffectClass", "PlanDefect",
    "PlanValidation", "ResourceArbiter", "ResourceClaim",
    "SpecialistTeamPlan", "SpecialistTeamResult", "SpecialistTeamTask",
    "TaskState", "TeamAdmissionController", "TeamCounters", "TeamOrchestrator",
    "VERIFICATION_PER_TASK_AND_TEAM", "VERIFICATION_TEAM",
    "TeamStatus", "TeamTaskResult", "TeamVerification",
    "authorize_delegation", "canonical_resource", "conflict_policy",
    "derive_team_status", "detect_cycle", "orchestrator",
    "parse_delegation_proposals", "scope_subset", "team_fabric_status",
    "validate_plan", "verify_team",
]
