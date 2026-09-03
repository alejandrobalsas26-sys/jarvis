"""
tests/test_specialist_team_v69_m65b.py — V69 M65B TEAM EXECUTION GAUNTLET.

Every scenario here drives the REAL ``TeamOrchestrator`` over the REAL
``SpecialistExecutor``, the REAL registry, the REAL ``mesh_orchestrator.preflight``,
the REAL ``ToolBroker``, the REAL ``ToolExecutor`` (handlers replaced by counting
stubs, every gate untouched) and the REAL ARGUS. Nothing reimplements a policy in
order to assert it.

HOW PARALLELISM IS PROVEN, NOT CLAIMED
--------------------------------------
Two tasks appearing in one ``asyncio.gather`` proves nothing. Every parallelism
test here uses a DEADLOCK FIXTURE: task A's model call blocks on an
``asyncio.Event`` that only task B's model call can set. If the scheduler runs
them one after the other, A waits until its own deadline and the test FAILS. The
assertion is therefore that the team succeeded at all, which is only possible if
both specialists were genuinely in flight at the same time.

The inverse is proven the same way. Two tasks claiming one resource for WRITE
record their entry and exit; the fixture asserts the observed concurrency never
exceeded one, so a scheduler that ignored the arbiter fails rather than merely
looking untidy.

WHAT IS FAKED, AND WHY THAT IS HONEST
--------------------------------------
  * **The model.** ``infer`` is a scripted async callable. Every control asserted
    here is decided BELOW the model: a scripted specialist cannot raise its own
    ceiling, cannot make a denied tool run, cannot make a second effect happen
    and cannot delegate to itself, because none of those is decided by what it
    says.

  * **Tool handlers.** ``_tool_<name>`` on the real executor becomes a counting
    stub, so "how many effects executed" is MEASURED from the executor's own
    ledger rather than asserted. Preflight, guardrails, ``authorize_action``, the
    risk classification, the LAB_ONLY check, the NATO challenge and the effect
    ledger all still run for real.

  * **L2/L3 records.** Where a rung the shipped registry does not occupy has to
    be exercised, ONE record is raised for the duration of ONE test through the
    frozen record's own ``dataclasses.replace``. No shipped specialist is
    promoted; §23 forbids it and two M65A regression guards pin it.

Nothing here opens a socket, names a public target, or touches a holdout. Every
address is loopback or RFC-1918.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from core.authority import AuthorityMode, ScopePolicy
from core.cognitive_mesh import REGISTRY, AutonomyLevel, MeshBudget, SpecialistId
from core.mesh_contracts import ToolCallStatus, Verdict
from core.model_role_router import ModelRoleRouter
from core.security_effects import SCOPES
from core.security_scope import (
    ActivityClass,
    AuthorizedSecurityScope,
    EnvironmentType,
)
from core.specialist_execution import (
    ExecutionStatus,
    HitlApproval,
    HitlApprovalRegistry,
    SpecialistExecutor,
    ToolIntent,
)
from core.specialist_team import (
    ADMISSION,
    MAX_DELEGATION_DEPTH,
    MAX_PARALLEL_EFFECTFUL,
    MAX_PARALLEL_SPECIALISTS,
    MAX_PLAN_TASKS,
    TEAM_CHECKS,
    Admission,
    BackendLimiter,
    CancellationToken,
    ClaimMode,
    ConflictPolicy,
    DelegationDenial,
    DelegationProposal,
    DependencyPolicy,
    EffectClass,
    PlanDefect,
    ResourceArbiter,
    ResourceClaim,
    SpecialistTeamPlan,
    SpecialistTeamTask,
    TaskState,
    TeamAdmissionController,
    TeamCounters,
    TeamOrchestrator,
    TeamStatus,
    authorize_delegation,
    canonical_resource,
    conflict_policy,
    derive_team_status,
    detect_cycle,
    parse_delegation_proposals,
    scope_subset,
    validate_plan,
    verify_team,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Harness
# ══════════════════════════════════════════════════════════════════════════════
def intent_text(tool: str, tool_input: dict | None = None, *,
                why: str = "the objective needs this observation",
                prose: str = "Analysis: one observation settles this.") -> str:
    payload = {"tool": tool, "tool_input": tool_input or {}, "why": why,
               "hypothesis": "the value is what the operator expects"}
    return prose + "\nTOOL_INTENT: " + json.dumps(payload)


def delegate_text(specialist: str, objective: str, *, tools=None,
                  extra: dict | None = None,
                  prose: str = "Analysis: one more view would settle this.") -> str:
    payload = {"specialist": specialist, "objective": objective,
               "why": "a second domain owns the remaining question"}
    if tools is not None:
        payload["tools"] = list(tools)
    payload.update(extra or {})
    return prose + "\nDELEGATE: " + json.dumps(payload)


class Harness:
    """The real orchestrator over the real executor, with a scripted model."""

    def __init__(self):
        from tools.executor import ToolExecutor

        self.executor = ToolExecutor()
        self.effects: "dict[str, int]" = {}
        self.hitl_grants = True
        self.script: "dict[str, object]" = {}
        self.default_answer = "Observed: the objective is bounded and settled."
        self.calls: "list[str]" = []
        self.approvals = HitlApprovalRegistry()
        self.counters = TeamCounters()
        self.admission = TeamAdmissionController()
        self.router = ModelRoleRouter(availability=ModelRoleRouter.probe())

        async def _challenge(tool_name, preview):
            return (True, "test:granted") if self.hitl_grants \
                else (False, "test:denied")

        self.executor._challenge = _challenge

        async def _infer(system, user, *, tier, timeout_s, num_ctx, temperature):
            self.calls.append(user)
            for key, action in self.script.items():
                if key in user or key in system:
                    if callable(action):
                        return await action()
                    return action
            return self.default_answer

        self.infer = _infer
        self.engine = SpecialistExecutor(
            infer=_infer, tool_executor=self.executor, scopes=SCOPES,
            approvals=self.approvals, role_router=self.router)
        self.orch = TeamOrchestrator(
            executor=self.engine, role_router=self.router,
            counters=self.counters, admission=self.admission)

    # ── tools ───────────────────────────────────────────────────────────────
    def add_tool(self, name: str, result: dict | None = None, *,
                 hook=None):
        """A counting handler on the REAL executor, reached only after every gate."""
        payload = result if result is not None else {"ok": True}

        def _handler(**kwargs):
            self.effects[name] = self.effects.get(name, 0) + 1
            if hook is not None:
                hook(kwargs)
            return dict(payload)

        setattr(self.executor, f"_tool_{name}", _handler)
        return self

    def count(self, name: str) -> int:
        return self.effects.get(name, 0)

    # ── plans ───────────────────────────────────────────────────────────────
    @staticmethod
    def task(task_id: str, specialist: SpecialistId, **kw) -> SpecialistTeamTask:
        kw.setdefault("objective", f"[task={task_id}] determine the current state")
        kw.setdefault("autonomy", AutonomyLevel.OBSERVE)
        return SpecialistTeamTask(task_id=task_id, specialist_id=specialist, **kw)

    @staticmethod
    def plan(*tasks: SpecialistTeamTask, **kw) -> SpecialistTeamPlan:
        kw.setdefault("plan_id", "plan:test")
        kw.setdefault("turn_id", "turn:test")
        kw.setdefault("objective", "a bounded multi-domain objective")
        kw.setdefault("authority_ceiling", AutonomyLevel.OBSERVE)
        kw.setdefault("effect_epoch", "turn:test")
        return SpecialistTeamPlan(tasks=tuple(tasks), **kw)

    def run(self, plan, **kw):
        kw.setdefault("admit", False)
        return asyncio.run(self.orch.run(plan, **kw))

    def run_async(self, coro_factory):
        return asyncio.run(coro_factory())


@pytest.fixture
def h(monkeypatch):
    SCOPES.scopes = []

    async def _no_broadcast(_payload):
        return None
    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    return Harness()


def elevate(monkeypatch, specialist: SpecialistId, level: AutonomyLevel,
            *, capabilities=None):
    """Raise ONE record for the duration of ONE test.

    Identical to the M65A helper, and for the identical reason: the registry is
    frozen and module-level, so this replaces the object the executor reads,
    which is what changing the file and landing a commit would do. It is not a
    back door and it is undone when the test ends.
    """
    record = REGISTRY.get(specialist)
    changes = {"default_autonomy": level}
    if capabilities is not None:
        changes["allowed_capabilities"] = frozenset(capabilities)
    raised = dataclasses.replace(record, **changes)
    monkeypatch.setitem(REGISTRY._by_id, specialist, raised)
    return raised


def lab_scope(targets=("127.0.0.1",),
              activities=(ActivityClass.PASSIVE_RECON,
                          ActivityClass.READ_ONLY_ENUMERATION,
                          ActivityClass.ACTIVE_SERVICE_VALIDATION),
              scope_id="lab") -> AuthorizedSecurityScope:
    from datetime import datetime, timedelta, timezone
    expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    return AuthorizedSecurityScope(
        scope_id=scope_id, environment_type=EnvironmentType.LAB,
        policy=ScopePolicy(scope_id=scope_id, mode=AuthorityMode.TRUSTED_LAB,
                           targets=frozenset(targets), expires_at=expires),
        permitted_activity_classes=frozenset(activities),
        maximum_risk="high_impact")


# ══════════════════════════════════════════════════════════════════════════════
#  TEAM_PLAN — the contract (§6)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_plan_is_immutable_and_carries_its_graph(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS),
                  h.task("b", SpecialistId.TRACE, dependencies=("a",)))
    assert plan.dependency_graph == {"a": (), "b": ("a",)}
    assert plan.task_ids == ("a", "b")
    assert plan.dependents_of("a") == ("b",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.authority_ceiling = AutonomyLevel.HITL_EXECUTE


def test_a_task_cannot_buy_itself_more_wall_clock_than_the_module_allows(h):
    from core.specialist_team import MAX_TASK_TIMEOUT_S
    task = h.task("a", SpecialistId.ATLAS, timeout_s=10_000.0)
    assert task.timeout_s == MAX_TASK_TIMEOUT_S
    assert h.task("b", SpecialistId.ATLAS, timeout_s=0.0).timeout_s > 0


def test_a_plan_cannot_buy_itself_more_parallelism_than_the_module_allows(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS), max_parallelism=64,
                  delegation_depth=9, retry_budget=99,
                  timeout_budget_s=100_000.0)
    assert plan.max_parallelism == MAX_PARALLEL_SPECIALISTS
    assert plan.delegation_depth == MAX_DELEGATION_DEPTH
    from core.specialist_team import MAX_TEAM_RETRIES, MAX_TEAM_TIMEOUT_S
    assert plan.retry_budget == MAX_TEAM_RETRIES
    assert plan.timeout_budget_s == MAX_TEAM_TIMEOUT_S


def test_the_plan_identity_is_stable_across_replans_and_moves_with_execution(h):
    """§48 — identity covers what EXECUTES, and excludes when it was written."""
    first = h.plan(h.task("a", SpecialistId.ATLAS))
    import time as _t
    _t.sleep(0.01)
    second = h.plan(h.task("a", SpecialistId.ATLAS), plan_id="plan:other",
                    turn_id="turn:other")
    assert first.canonical_identity() == second.canonical_identity()

    louder = h.plan(h.task("a", SpecialistId.ATLAS,
                           autonomy=AutonomyLevel.ADVISE))
    assert louder.canonical_identity() != first.canonical_identity()

    wider = h.plan(h.task("a", SpecialistId.ATLAS,
                          allowed_tools=frozenset({"read_file"})))
    assert wider.canonical_identity() != first.canonical_identity()


def test_every_team_count_is_derived_from_the_nodes(h):
    """A team cannot report zero failures by saying so."""
    from core.specialist_team import SpecialistTeamResult
    result = SpecialistTeamResult(plan_id="p", plan_identity="x",
                                  status=TeamStatus.SUCCESS)
    for name in ("completed", "failed", "denied", "skipped", "cancelled"):
        assert isinstance(getattr(type(result), name), property)


# ══════════════════════════════════════════════════════════════════════════════
#  PLAN_VALIDATOR (§7) — nothing starts until the whole DAG is legal
# ══════════════════════════════════════════════════════════════════════════════
def test_a_cycle_is_rejected_and_nothing_executes(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, dependencies=("b",)),
                  h.task("b", SpecialistId.TRACE, dependencies=("a",)))
    validation = validate_plan(plan)
    assert not validation.valid
    assert PlanDefect.CYCLE in validation.codes

    result = h.run(plan)
    assert result.status is TeamStatus.INVALID
    assert result.task_results == ()
    assert h.calls == [], "a rejected plan must not consult a single model"


def test_a_longer_cycle_is_still_found(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, dependencies=("c",)),
                  h.task("b", SpecialistId.TRACE, dependencies=("a",)),
                  h.task("c", SpecialistId.FORGE, dependencies=("b",)))
    assert PlanDefect.CYCLE in validate_plan(plan).codes


def test_an_acyclic_diamond_is_not_mistaken_for_a_cycle(h):
    """The inverse control: the detector must not refuse a legal shape."""
    plan = h.plan(h.task("a", SpecialistId.ATLAS),
                  h.task("b", SpecialistId.TRACE, dependencies=("a",)),
                  h.task("c", SpecialistId.FORGE, dependencies=("a",)),
                  h.task("d", SpecialistId.GUARDIAN, dependencies=("b", "c")))
    assert validate_plan(plan).valid
    assert detect_cycle(plan.dependency_graph) == ()


def test_self_dependency_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, dependencies=("a",)))
    codes = validate_plan(plan).codes
    assert PlanDefect.SELF_DEPENDENCY in codes


def test_a_missing_dependency_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, dependencies=("ghost",)))
    assert PlanDefect.MISSING_DEPENDENCY in validate_plan(plan).codes


def test_duplicate_task_ids_are_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS),
                  h.task("a", SpecialistId.TRACE))
    assert PlanDefect.DUPLICATE_TASK_ID in validate_plan(plan).codes


def test_an_unknown_model_role_is_rejected_rather_than_coerced(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, model_role="omniscient"))
    assert PlanDefect.UNKNOWN_MODEL_ROLE in validate_plan(plan).codes


def test_l4_prohibited_is_rejected_before_execution(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS,
                         autonomy=AutonomyLevel.PROHIBITED),
                  authority_ceiling=AutonomyLevel.PROHIBITED)
    assert PlanDefect.ILLEGAL_AUTONOMY in validate_plan(plan).codes


def test_a_task_above_the_team_ceiling_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS,
                         autonomy=AutonomyLevel.HITL_EXECUTE),
                  authority_ceiling=AutonomyLevel.OBSERVE)
    assert PlanDefect.AUTONOMY_ABOVE_CEILING in validate_plan(plan).codes


def test_a_capability_the_record_does_not_hold_is_rejected(h):
    """ARGUS is read-only; a plan asking it to run code is incoherent."""
    plan = h.plan(h.task("a", SpecialistId.ARGUS, capability="code"))
    assert PlanDefect.CAPABILITY_MISMATCH in validate_plan(plan).codes


def test_an_unregistered_tool_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS,
                         allowed_tools=frozenset({"exfiltrate_everything"})))
    assert PlanDefect.UNREGISTERED_TOOL in validate_plan(plan).codes


def test_scope_expansion_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, scope=("10.0.0.9",)),
                  scope=("127.0.0.1",))
    assert PlanDefect.SCOPE_EXPANSION in validate_plan(plan).codes


def test_budget_overflow_is_rejected(h):
    tasks = [h.task(f"t{i}", SpecialistId.ATLAS) for i in range(5)]
    plan = h.plan(*tasks, execution_budget=MeshBudget(max_specialists=3))
    assert PlanDefect.BUDGET_OVERFLOW in validate_plan(plan).codes


def test_too_many_tasks_is_rejected(h):
    tasks = [h.task(f"t{i}", SpecialistId.ATLAS) for i in range(MAX_PLAN_TASKS + 1)]
    plan = h.plan(*tasks, execution_budget=MeshBudget(max_specialists=99))
    assert PlanDefect.TOO_MANY_TASKS in validate_plan(plan).codes


def test_a_delegated_task_above_the_depth_ceiling_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS),
                  h.task("a.d1", SpecialistId.TRACE, depth=2,
                         parent_task_id="a"))
    assert PlanDefect.DELEGATION_DEPTH in validate_plan(plan).codes


def test_an_unknown_resource_kind_is_rejected(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS,
                         resource_claims=(ResourceClaim("soul", "mine"),)))
    assert PlanDefect.UNKNOWN_CLAIM_KIND in validate_plan(plan).codes


def test_a_write_claim_on_a_read_only_task_is_rejected(h):
    """Otherwise the scheduler would under-serialise a task that mutates."""
    plan = h.plan(h.task("a", SpecialistId.ATLAS,
                         resource_claims=(ResourceClaim("file", "/tmp/x",
                                                        ClaimMode.WRITE),)))
    assert PlanDefect.EFFECT_CLASS_MISMATCH in validate_plan(plan).codes


def test_the_validator_reports_every_defect_not_just_the_first(h):
    plan = h.plan(h.task("a", SpecialistId.ATLAS, dependencies=("a", "ghost"),
                         model_role="omniscient"))
    codes = set(validate_plan(plan).codes)
    assert {PlanDefect.SELF_DEPENDENCY, PlanDefect.MISSING_DEPENDENCY,
            PlanDefect.UNKNOWN_MODEL_ROLE} <= codes


def test_an_empty_plan_executes_nothing(h):
    assert PlanDefect.EMPTY in validate_plan(h.plan()).codes


# ══════════════════════════════════════════════════════════════════════════════
#  DAG + DEPENDENCIES (§8, §10, §35)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_dependent_task_cannot_begin_before_its_dependency_succeeds(h):
    """§35 — proven by an ORDER TRACE, not by sleeping and hoping."""
    order: "list[str]" = []

    async def _a():
        order.append("a:enter")
        await asyncio.sleep(0.02)
        order.append("a:leave")
        return "A observed the host."

    async def _b():
        order.append("b:enter")
        return "B reasoned over A's finding."

    h.script["task=a"] = _a
    h.script["task=b"] = _b
    result = h.run(h.plan(h.task("a", SpecialistId.ATLAS),
                          h.task("b", SpecialistId.TRACE, dependencies=("a",))))

    assert result.status is TeamStatus.SUCCESS
    assert order == ["a:enter", "a:leave", "b:enter"]
    a, b = result.result_for("a"), result.result_for("b")
    assert b.started_seq > a.finished_seq, "B started before A was terminal"


def test_an_independent_branch_survives_a_failed_one(h):
    """§10 — A -> B, C independent. A fails, B skips, C still runs."""
    async def _fail():
        raise RuntimeError("the backend fell over")

    h.script["task=a"] = _fail
    h.script["task=c"] = "C observed the network path."
    result = h.run(h.plan(
        h.task("a", SpecialistId.ATLAS),
        h.task("b", SpecialistId.TRACE, dependencies=("a",)),
        h.task("c", SpecialistId.MESH)))

    assert result.status is TeamStatus.PARTIAL_SUCCESS
    assert result.result_for("a").state is TaskState.FAILED
    assert result.result_for("b").state is TaskState.SKIPPED
    assert result.result_for("c").state is TaskState.SUCCESS
    assert "c" in result.completed and "b" in result.skipped


def test_a_skipped_task_ran_no_specialist_at_all(h):
    async def _fail():
        raise RuntimeError("down")
    h.script["task=a"] = _fail
    result = h.run(h.plan(h.task("a", SpecialistId.ATLAS),
                          h.task("b", SpecialistId.TRACE, dependencies=("a",))))
    skipped = result.result_for("b")
    assert skipped.execution is None
    assert "trace" not in result.specialists_executed


def test_all_terminal_lets_a_summariser_run_over_a_failure(h):
    """The only reason ALL_TERMINAL exists: reporting that something failed."""
    async def _fail():
        raise RuntimeError("down")
    h.script["task=a"] = _fail
    result = h.run(h.plan(
        h.task("a", SpecialistId.ATLAS),
        h.task("b", SpecialistId.TRACE, dependencies=("a",),
               dependency_policy=DependencyPolicy.ALL_TERMINAL)))
    assert result.result_for("a").state is TaskState.FAILED
    assert result.result_for("b").state is TaskState.SUCCESS


def test_a_diamond_runs_its_two_middles_and_then_its_join(h):
    result = h.run(h.plan(
        h.task("a", SpecialistId.ATLAS),
        h.task("b", SpecialistId.TRACE, dependencies=("a",)),
        h.task("c", SpecialistId.MESH, dependencies=("a",)),
        h.task("d", SpecialistId.GUARDIAN, dependencies=("b", "c"))))
    assert result.status is TeamStatus.SUCCESS
    d = result.result_for("d")
    for parent in ("b", "c"):
        assert d.started_seq > result.result_for(parent).finished_seq


def test_the_team_status_vocabulary_distinguishes_four_outcomes(h):
    from core.specialist_team import TeamTaskResult

    def node(state):
        return TeamTaskResult(task_id="t", specialist_id=SpecialistId.ATLAS,
                              state=state)
    assert derive_team_status((node(TaskState.SUCCESS),)) is TeamStatus.SUCCESS
    assert derive_team_status((node(TaskState.FAILED),)) is TeamStatus.FAILED
    assert derive_team_status(
        (node(TaskState.SUCCESS), node(TaskState.FAILED))) \
        is TeamStatus.PARTIAL_SUCCESS
    assert derive_team_status((node(TaskState.CANCELLED),)) is TeamStatus.CANCELLED
    assert derive_team_status((), cancelled=True) is TeamStatus.CANCELLED


# ══════════════════════════════════════════════════════════════════════════════
#  PARALLEL (§11, §12) — the deadlock fixture
# ══════════════════════════════════════════════════════════════════════════════
def test_two_independent_read_only_specialists_genuinely_overlap(h):
    """§11 — A cannot finish until B has STARTED. Serial execution deadlocks."""
    async def scenario():
        gate = asyncio.Event()
        entered: "list[str]" = []

        async def _a():
            entered.append("a")
            await asyncio.wait_for(gate.wait(), timeout=5.0)
            return "A observed the host inventory."

        async def _b():
            entered.append("b")
            gate.set()
            return "B observed the network path."

        h.script["task=a"] = _a
        h.script["task=b"] = _b
        result = await h.orch.run(
            h.plan(h.task("a", SpecialistId.ATLAS),
                   h.task("b", SpecialistId.MESH)), admit=False)
        return result, entered

    result, entered = asyncio.run(scenario())
    assert result.status is TeamStatus.SUCCESS, (
        "A waited on an event only B could set; if this failed the two "
        "specialists did not run at the same time")
    assert set(entered) == {"a", "b"}
    assert result.parallel_overlaps >= 1


def test_three_specialists_overlap_and_a_fourth_waits_for_a_slot(h):
    """§12 — parallelism is bounded. The 4th task cannot be in flight with 3."""
    async def scenario():
        inflight = 0
        peak = 0
        release = asyncio.Event()

        async def _hold():
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            if inflight >= MAX_PARALLEL_SPECIALISTS:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=5.0)
            await asyncio.sleep(0)
            inflight -= 1
            return "observed"

        for tid in ("a", "b", "c", "d"):
            h.script[f"task={tid}"] = _hold
        plan = h.plan(h.task("a", SpecialistId.ATLAS),
                      h.task("b", SpecialistId.MESH),
                      h.task("c", SpecialistId.TRACE),
                      h.task("d", SpecialistId.FORGE))
        result = await h.orch.run(plan, admit=False)
        return result, peak

    result, peak = asyncio.run(scenario())
    assert result.status is TeamStatus.SUCCESS
    assert peak == MAX_PARALLEL_SPECIALISTS, (
        f"observed {peak} concurrent specialists against a ceiling of "
        f"{MAX_PARALLEL_SPECIALISTS}")


def test_the_backend_limiter_bounds_concurrency_per_backend(h):
    """§30 — several specialists share one backend; the backend is what binds."""
    async def scenario():
        limiter = BackendLimiter(limit=1)
        inflight = 0
        peak = 0

        async def _hold(_backend="m"):
            nonlocal inflight, peak
            await limiter.acquire("qwen-test")
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.01)
            inflight -= 1
            limiter.release("qwen-test")

        await asyncio.gather(*(_hold() for _ in range(4)))
        return peak, limiter

    peak, limiter = asyncio.run(scenario())
    assert peak == 1
    assert limiter.waits >= 1
    assert limiter.to_dict()["limit"] == 1


def test_queueing_for_a_backend_cannot_raise_authority(h):
    """§30 — model queueing changes WHEN, never WHAT."""
    async def scenario():
        limiter = BackendLimiter(limit=1)
        h.orch._backends = limiter
        return await h.orch.run(
            h.plan(h.task("a", SpecialistId.ATLAS),
                   h.task("b", SpecialistId.MESH)), admit=False)

    result = asyncio.run(scenario())
    for node in result.task_results:
        assert node.execution.effective_autonomy <= AutonomyLevel.OBSERVE


# ══════════════════════════════════════════════════════════════════════════════
#  RESOURCE_CONFLICT (§13, §14, §15, §16)
# ══════════════════════════════════════════════════════════════════════════════
def test_the_conflict_table_is_exactly_what_is_documented():
    """READ+READ parallel; READ+WRITE and WRITE+WRITE serialise."""
    read_a = ResourceClaim("file", "/tmp/one", ClaimMode.READ)
    read_b = ResourceClaim("file", "/tmp/one", ClaimMode.READ)
    write_a = ResourceClaim("file", "/tmp/one", ClaimMode.WRITE)
    write_b = ResourceClaim("file", "/tmp/one", ClaimMode.WRITE)
    other = ResourceClaim("file", "/tmp/two", ClaimMode.WRITE)

    assert conflict_policy(read_a, read_b) is ConflictPolicy.PARALLEL
    assert conflict_policy(read_a, write_a) is ConflictPolicy.SERIALIZE
    assert conflict_policy(write_a, read_a) is ConflictPolicy.SERIALIZE
    assert conflict_policy(write_a, write_b) is ConflictPolicy.SERIALIZE
    assert conflict_policy(write_a, other) is ConflictPolicy.PARALLEL


def test_equivalent_paths_are_one_lock():
    """§16 — ./foo, foo and /base/foo must not become three write locks."""
    base = "/srv/jarvis"
    a = canonical_resource("file", "./notes/x.txt", base=base)
    b = canonical_resource("file", "notes/x.txt", base=base)
    c = canonical_resource("file", "/srv/jarvis/notes/x.txt")
    d = canonical_resource("file", "/srv/jarvis//notes//x.txt")
    e = canonical_resource("file", "/srv/jarvis/other/../notes/x.txt")
    assert a == b == c == d == e == "file:/srv/jarvis/notes/x.txt"
    assert canonical_resource("file", "/srv/jarvis/notes/y.txt") != a


def test_normalisation_never_touches_the_filesystem(tmp_path):
    """§16 — lexical only. A symlink is not followed to build a lock name."""
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    assert canonical_resource("file", str(link)) != canonical_resource(
        "file", str(real))
    assert canonical_resource("file", str(link)).endswith("link.txt")


def test_network_targets_and_other_kinds_canonicalise_case_insensitively():
    assert canonical_resource("network-target", "Host.Local.") == \
        "network-target:host.local"
    assert canonical_resource("service", "NginX") == "service:nginx"


def test_two_write_claims_on_one_resource_never_overlap(h, monkeypatch):
    """§14 — the CONFLICT SCHEDULER serialises them. Observed, not assumed.

    The effectful-concurrency cap is lifted for the duration of this test, and
    that is the whole point: with it in place both tasks would be serialised by
    a ceiling that has nothing to do with the resource, the arbiter would never
    be consulted, and a scheduler that ignored conflicts entirely would pass.
    Measured — the first version of this test did exactly that.
    """
    monkeypatch.setattr("core.specialist_team.MAX_PARALLEL_EFFECTFUL", 3)

    async def scenario():
        inflight = 0
        peak = 0

        async def _touch():
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.02)
            inflight -= 1
            return "wrote the shared artefact"

        h.script["task=a"] = _touch
        h.script["task=b"] = _touch
        claim = (ResourceClaim("file", "./shared.json", ClaimMode.WRITE),)
        plan = h.plan(
            h.task("a", SpecialistId.ATLAS, resource_claims=claim,
                   effect_class=EffectClass.EFFECTFUL),
            h.task("b", SpecialistId.MESH, resource_claims=claim,
                   effect_class=EffectClass.EFFECTFUL))
        result = await h.orch.run(plan, admit=False)
        return result, peak

    result, peak = asyncio.run(scenario())
    assert result.status is TeamStatus.SUCCESS
    assert peak == 1, "two WRITE claims on one resource ran at the same time"
    assert result.parallel_overlaps == 0
    assert h.counters.conflict_serializations >= 1


def test_two_read_claims_on_one_resource_do_overlap(h):
    """The inverse control: serialising everything is not the answer."""
    async def scenario():
        gate = asyncio.Event()

        async def _a():
            await asyncio.wait_for(gate.wait(), timeout=5.0)
            return "A read the shared artefact"

        async def _b():
            gate.set()
            return "B read the shared artefact"

        h.script["task=a"] = _a
        h.script["task=b"] = _b
        claim = (ResourceClaim("file", "./shared.json", ClaimMode.READ),)
        plan = h.plan(h.task("a", SpecialistId.ATLAS, resource_claims=claim),
                      h.task("b", SpecialistId.MESH, resource_claims=claim))
        return await h.orch.run(plan, admit=False)

    result = asyncio.run(scenario())
    assert result.status is TeamStatus.SUCCESS
    assert result.parallel_overlaps >= 1


def test_a_write_and_a_read_on_one_resource_serialise(h):
    async def scenario():
        inflight = 0
        peak = 0

        async def _touch():
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.02)
            inflight -= 1
            return "touched the shared artefact"

        h.script["task=a"] = _touch
        h.script["task=b"] = _touch
        plan = h.plan(
            h.task("a", SpecialistId.ATLAS,
                   resource_claims=(ResourceClaim("file", "s.json",
                                                  ClaimMode.WRITE),),
                   effect_class=EffectClass.EFFECTFUL),
            h.task("b", SpecialistId.MESH,
                   resource_claims=(ResourceClaim("file", "./s.json",
                                                  ClaimMode.READ),)))
        result = await h.orch.run(plan, admit=False)
        return result, peak

    result, peak = asyncio.run(scenario())
    assert peak == 1
    assert result.parallel_overlaps == 0


def test_a_reservation_is_all_or_nothing(h):
    """Half a reservation is a deadlock generator, so there is no such thing."""
    arbiter = ResourceArbiter()
    first = h.task("a", SpecialistId.ATLAS, effect_class=EffectClass.EFFECTFUL,
                   resource_claims=(ResourceClaim("file", "x", ClaimMode.WRITE),
                                    ResourceClaim("file", "y", ClaimMode.WRITE)))
    second = h.task("b", SpecialistId.MESH, effect_class=EffectClass.EFFECTFUL,
                    resource_claims=(ResourceClaim("file", "y", ClaimMode.WRITE),
                                     ResourceClaim("file", "z", ClaimMode.WRITE)))
    assert arbiter.reserve(first)
    assert not arbiter.reserve(second)
    assert arbiter.held_count == 1, "a refused reservation took nothing"
    arbiter.release(first)
    assert arbiter.reserve(second)


def test_effectful_tasks_are_bounded_harder_than_read_only_ones():
    assert MAX_PARALLEL_EFFECTFUL < MAX_PARALLEL_SPECIALISTS


def test_the_effectful_ceiling_binds_when_it_is_not_lifted(h, monkeypatch):
    """The control for the test above: the cap is real when left alone."""
    async def scenario():
        inflight = 0
        peak = 0

        async def _touch():
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.02)
            inflight -= 1
            return "touched an unrelated artefact"

        h.script["task=a"] = _touch
        h.script["task=b"] = _touch
        plan = h.plan(
            h.task("a", SpecialistId.ATLAS, effect_class=EffectClass.EFFECTFUL,
                   resource_claims=(ResourceClaim("file", "one", ClaimMode.WRITE),)),
            h.task("b", SpecialistId.MESH, effect_class=EffectClass.EFFECTFUL,
                   resource_claims=(ResourceClaim("file", "two", ClaimMode.WRITE),)))
        result = await h.orch.run(plan, admit=False)
        return result, peak

    result, peak = asyncio.run(scenario())
    assert result.status is TeamStatus.SUCCESS
    assert peak == MAX_PARALLEL_EFFECTFUL == 1, (
        "two effectful tasks on DIFFERENT resources still ran together")


# ══════════════════════════════════════════════════════════════════════════════
#  TEAM_EXACTLY_ONCE (§17, §18)
# ══════════════════════════════════════════════════════════════════════════════
def approve(h, specialist: SpecialistId, tool: str, tool_input: dict, *,
            epoch: str = "turn:test", single_use: bool = False):
    """Bind a human approval to exactly one effect, exactly as an operator would."""
    from datetime import datetime, timedelta, timezone
    identity = ToolIntent(tool=tool, tool_input=tool_input).effect_identity(epoch)
    return h.approvals.grant(HitlApproval(
        approval_id=f"appr:{specialist.value}:{tool}",
        specialist_id=specialist, effect_identity=identity,
        single_use=single_use,
        expires_at=(datetime.now(timezone.utc)
                    + timedelta(hours=2)).isoformat(),
        reason="test-only bound approval"))


def test_two_different_specialists_proposing_one_effect_produce_one_effect(
        h, monkeypatch):
    """§17 — cross-specialist exactly-once, through the team.

    Both specialists are raised to L3 for this test only; neither shipped record
    reaches it. The approval is REUSABLE, which removes the single-use gate and
    leaves the effect ledger as the only thing between two identical intents and
    two effects.
    """
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    elevate(monkeypatch, SpecialistId.CIRCUIT, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "print(1)"}
    h.add_tool("code_execute", {"stdout": "1"})
    approve(h, SpecialistId.FORGE, "code_execute", args)
    approve(h, SpecialistId.CIRCUIT, "code_execute", args)
    h.script["task=a"] = intent_text("code_execute", args)
    h.script["task=b"] = intent_text("code_execute", args)

    result = h.run(h.plan(
        h.task("a", SpecialistId.FORGE, autonomy=AutonomyLevel.HITL_EXECUTE,
               allowed_tools=frozenset({"code_execute"}),
               effect_class=EffectClass.EFFECTFUL),
        h.task("b", SpecialistId.CIRCUIT, autonomy=AutonomyLevel.HITL_EXECUTE,
               allowed_tools=frozenset({"code_execute"}),
               effect_class=EffectClass.EFFECTFUL),
        authority_ceiling=AutonomyLevel.HITL_EXECUTE))

    assert h.count("code_execute") == 1, "the same effect ran twice"
    assert h.executor.effect_count("code_execute") == 1
    assert result.executed_effects == 1
    assert result.deduplicated_effects == 1
    assert len(result.specialists_executed) == 2


def test_the_effect_ledger_holds_under_genuinely_concurrent_submission(h):
    """§17 — asserted against the LEDGER, not against scheduler serialisation.

    Two identical calls enter ``aexecute`` at the same time with nothing in
    between. Before M65B both saw an empty ledger and both ran: the ledger is
    read at the top of the gate and written after the handler returns, and there
    are three awaits in between. This is that window, closed.
    """
    async def scenario():
        started = asyncio.Event()
        proceed = asyncio.Event()

        def _slow(_kwargs):
            started.set()

        h.add_tool("code_execute", {"stdout": "1"}, hook=_slow)
        h.executor.begin_effect_epoch("turn:concurrent")
        args = {"code": "print(1)"}
        both = asyncio.gather(
            h.executor.aexecute("code_execute", dict(args), "first"),
            h.executor.aexecute("code_execute", dict(args), "second"))
        proceed.set()
        return await both

    first, second = asyncio.run(scenario())
    assert h.count("code_execute") == 1, (
        "two concurrent submissions of one effect identity both executed")
    assert first == second, (
        "the suppressed caller must receive the recorded result, not an error")


def test_a_concurrent_duplicate_receives_the_recorded_result(h):
    async def scenario():
        h.add_tool("code_execute", {"stdout": "canonical"})
        h.executor.begin_effect_epoch("turn:concurrent2")
        return await asyncio.gather(*(
            h.executor.aexecute("code_execute", {"code": "x"}, f"call{i}")
            for i in range(4)))

    results = asyncio.run(scenario())
    assert h.count("code_execute") == 1
    assert all(r == {"stdout": "canonical"} for r in results)


def test_a_failed_effect_is_never_ledgered_so_a_retry_is_legitimate(h):
    """A failed call left the world unchanged; refusing to repeat it would be
    the ledger inventing a policy it does not have."""
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        return {"error": "the tool failed"}

    setattr(h.executor, "_tool_code_execute", _handler)

    async def scenario():
        h.executor.begin_effect_epoch("turn:failing")
        await h.executor.aexecute("code_execute", {"code": "x"}, "first")
        await h.executor.aexecute("code_execute", {"code": "x"}, "second")

    asyncio.run(scenario())
    assert calls["n"] == 2
    assert h.executor.effect_count("code_execute") == 0


def test_a_new_epoch_is_never_blocked_by_an_older_one(h):
    async def scenario():
        h.add_tool("code_execute", {"stdout": "1"})
        h.executor.begin_effect_epoch("turn:one")
        await h.executor.aexecute("code_execute", {"code": "x"}, "a")
        h.executor.begin_effect_epoch("turn:two")
        await h.executor.aexecute("code_execute", {"code": "x"}, "b")

    asyncio.run(scenario())
    assert h.count("code_execute") == 2


def test_read_only_calls_are_never_reserved_and_never_deduplicated(h):
    async def scenario():
        h.add_tool("read_file", {"content": "x"})
        h.executor.begin_effect_epoch("turn:reads")
        await asyncio.gather(*(
            h.executor.aexecute("read_file", {"path": "/etc/hostname"}, "r")
            for _ in range(3)))

    asyncio.run(scenario())
    assert h.count("read_file") == 3
    assert h.executor._effect_inflight == {}


def test_argument_order_cannot_manufacture_a_second_effect_identity(h):
    async def scenario():
        h.add_tool("code_execute", {"stdout": "1"})
        h.executor.begin_effect_epoch("turn:order")
        await h.executor.aexecute("code_execute", {"a": 1, "b": 2}, "first")
        await h.executor.aexecute("code_execute", {"b": 2, "a": 1}, "second")

    asyncio.run(scenario())
    assert h.count("code_execute") == 1


def test_a_reservation_is_released_even_when_the_gate_refuses(h, monkeypatch):
    """A refusal must not leave a lock behind, or the effect could never run."""
    async def scenario():
        h.add_tool("code_execute", {"stdout": "1"})
        h.hitl_grants = False
        h.executor.begin_effect_epoch("turn:refused")
        refused = await h.executor.aexecute("code_execute", {"code": "x"}, "a")
        h.hitl_grants = True
        allowed = await h.executor.aexecute("code_execute", {"code": "x"}, "b")
        return refused, allowed

    refused, allowed = asyncio.run(scenario())
    assert "error" in refused
    assert h.count("code_execute") == 1, (
        "the refused call left a reservation that blocked the real one")
    assert h.executor._effect_inflight == {}


def test_a_task_retry_re_enters_the_same_effect_identity(h, monkeypatch):
    """§18 — the ledger, not the retry counter, is what holds the count at one."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "print(1)"}
    h.add_tool("code_execute", {"stdout": "1"})
    approve(h, SpecialistId.FORGE, "code_execute", args)
    attempts = {"n": 0}

    # Retries are two-layered and both layers are bounded: the M65A executor
    # retries the INFERENCE up to MAX_ATTEMPTS, and the team retries the TASK.
    # The first task attempt therefore has to exhaust the executor's own retry
    # before the team's is reached — measured, not assumed.
    from core.specialist_execution import MAX_ATTEMPTS

    async def _flaky():
        attempts["n"] += 1
        if attempts["n"] <= MAX_ATTEMPTS:
            raise RuntimeError("the backend dropped the connection")
        return intent_text("code_execute", args)

    h.script["task=a"] = _flaky
    result = h.run(h.plan(
        h.task("a", SpecialistId.FORGE, autonomy=AutonomyLevel.HITL_EXECUTE,
               allowed_tools=frozenset({"code_execute"}),
               effect_class=EffectClass.EFFECTFUL, retry_limit=1),
        authority_ceiling=AutonomyLevel.HITL_EXECUTE))

    assert result.result_for("a").attempts == 2
    assert h.count("code_execute") == 1
    assert result.budget_usage["retries_used"] == 1


# ══════════════════════════════════════════════════════════════════════════════
#  DELEGATION (§19, §20, §21)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_specialist_cannot_instantiate_another_specialist(h):
    """§19 — there is no ``specialist.spawn``. Asserted over the parsed AST so
    this module's own prose cannot satisfy the test that proves it."""
    import ast
    import pathlib

    for name in ("core/specialist_execution.py", "core/specialist_team.py"):
        tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.name != "spawn", f"{name} defines spawn()"
    # The one place a task is constructed from a proposal is the guard.
    import core.specialist_execution as se
    assert not hasattr(se.SpecialistExecutor, "spawn")
    assert not hasattr(se.SpecialistExecutor, "delegate")


def test_the_delegation_grammar_has_nowhere_to_ask_for_authority(h):
    """§20 — mirroring ToolIntent: the claim has no shape to arrive in."""
    fields = {f.name for f in dataclasses.fields(DelegationProposal)}
    for forbidden in ("autonomy", "autonomy_level", "scope", "capability",
                      "approval", "depth", "verification", "ceiling"):
        assert forbidden not in fields


def test_fields_a_specialist_may_not_set_are_dropped_and_reported(h):
    text = delegate_text("trace", "correlate the timeline",
                         extra={"autonomy": 3, "scope": ["0.0.0.0/0"]})
    proposals, warnings = parse_delegation_proposals(
        text, proposed_by=SpecialistId.ATLAS, parent_task_id="a")
    assert len(proposals) == 1
    assert proposals[0].specialist == "trace"
    assert any("autonomy" in w and "scope" in w for w in warnings)


def test_the_delegation_scanner_balances_braces(h):
    """The measured M65A bug, in its M65B shape: a non-greedy regex closes on the
    inner brace and every real proposal is silently dropped."""
    text = ('DELEGATE: {"specialist": "trace", "objective": "look",'
            ' "tools": ["read_file"], "why": "a nested {brace} in prose"}')
    proposals, _ = parse_delegation_proposals(
        text, proposed_by=SpecialistId.ATLAS, parent_task_id="a")
    assert len(proposals) == 1
    assert proposals[0].tools == ("read_file",)


def test_a_valid_delegation_produces_a_child_task_that_actually_runs(h):
    h.script["task=a"] = delegate_text("trace", "correlate the incident timeline")
    result = h.run(h.plan(h.task("a", SpecialistId.GUARDIAN)))

    assert len(result.delegations) == 1 and result.delegations[0].allowed
    child = [r for r in result.task_results if r.depth == 1]
    assert len(child) == 1
    assert child[0].specialist_id is SpecialistId.TRACE
    assert child[0].parent_task_id == "a"
    assert child[0].state is TaskState.SUCCESS
    assert h.counters.delegations_approved == 1


def test_a_delegated_child_never_exceeds_its_parents_authority(h):
    parent = h.task("a", SpecialistId.GUARDIAN, autonomy=AutonomyLevel.ADVISE)
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.GUARDIAN, "a", "trace", "look"),
        parent=parent,
        plan=h.plan(parent, authority_ceiling=AutonomyLevel.HITL_EXECUTE),
        effective_parent_autonomy=AutonomyLevel.ADVISE, remaining_budget=2)
    assert decision.allowed
    assert decision.task.autonomy is AutonomyLevel.ADVISE, (
        "the child took the TEAM ceiling rather than its parent's")


def test_a_delegation_naming_a_tool_the_parent_lacks_is_denied(h):
    parent = h.task("a", SpecialistId.GUARDIAN,
                    allowed_tools=frozenset({"read_file"}))
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.GUARDIAN, "a", "trace", "look",
                           tools=("read_file", "code_execute")),
        parent=parent, plan=h.plan(parent),
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=2)
    assert not decision.allowed
    assert decision.denial is DelegationDenial.TOOL_ESCALATION


def test_a_delegation_asking_for_a_subset_of_the_parents_tools_is_allowed(h):
    """The inverse control: the guard is not simply refusing every tool."""
    parent = h.task("a", SpecialistId.GUARDIAN,
                    allowed_tools=frozenset({"read_file", "list_directory"}))
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.GUARDIAN, "a", "trace", "look",
                           tools=("read_file",)),
        parent=parent, plan=h.plan(parent),
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=2)
    assert decision.allowed
    assert decision.task.allowed_tools == frozenset({"read_file"})


def test_a_child_inherits_its_parents_scope_and_not_the_teams(h):
    """§20 — a parent narrowed to one host may not produce a wider child."""
    parent = h.task("a", SpecialistId.GUARDIAN, scope=("127.0.0.1",))
    plan = h.plan(parent, scope=("127.0.0.1", "10.0.0.5"))
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.GUARDIAN, "a", "trace", "look"),
        parent=parent, plan=plan,
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=2)
    assert decision.allowed
    assert decision.task.scope == ("127.0.0.1",)
    assert scope_subset(decision.task.scope, parent.scope)


def test_delegation_depth_two_is_denied(h):
    """§21 — depth 0 is JARVIS's, depth 1 is one approved delegation, there is
    no depth 2, so there is no recursive agent tree."""
    child = h.task("a.d1", SpecialistId.TRACE, depth=1, parent_task_id="a")
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.TRACE, "a.d1", "forge", "and again"),
        parent=child, plan=h.plan(child),
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=4)
    assert not decision.allowed
    assert decision.denial is DelegationDenial.DEPTH


def test_a_delegated_child_does_not_delegate_again_in_a_live_run(h):
    h.script["task=a"] = delegate_text("trace", "correlate the timeline")
    h.script["correlate the timeline"] = delegate_text("forge", "and again")
    result = h.run(h.plan(h.task("a", SpecialistId.GUARDIAN)))

    depths = {r.depth for r in result.task_results}
    assert depths == {0, 1}
    denials = [d for d in result.delegations if not d.allowed]
    assert any(d.denial is DelegationDenial.DEPTH for d in denials)


def test_a_delegation_to_an_unregistered_specialist_is_denied(h):
    parent = h.task("a", SpecialistId.GUARDIAN)
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.GUARDIAN, "a", "shadow", "look"),
        parent=parent, plan=h.plan(parent),
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=2)
    assert decision.denial is DelegationDenial.UNKNOWN_SPECIALIST


def test_a_delegation_the_registry_forbids_as_a_handoff_is_denied(h):
    """The registry decides who may hand off to whom; a proposal does not."""
    parent = h.task("a", SpecialistId.ARGUS)
    forbidden = [s for s in REGISTRY.ids()
                 if not REGISTRY.handoff_allowed(SpecialistId.ARGUS, s)]
    assert forbidden, "the registry permits every handoff; this test is vacuous"
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.ARGUS, "a", forbidden[0].value, "look"),
        parent=parent, plan=h.plan(parent),
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=2)
    assert decision.denial is DelegationDenial.NOT_A_PERMITTED_HANDOFF


def test_a_delegation_with_no_budget_left_is_denied(h):
    parent = h.task("a", SpecialistId.GUARDIAN)
    decision = authorize_delegation(
        DelegationProposal(SpecialistId.GUARDIAN, "a", "trace", "look"),
        parent=parent, plan=h.plan(parent),
        effective_parent_autonomy=AutonomyLevel.OBSERVE, remaining_budget=0)
    assert decision.denial is DelegationDenial.BUDGET


def test_a_denied_delegation_leaves_the_plan_exactly_as_it_was(h):
    h.script["task=a"] = delegate_text("shadow", "do the impossible")
    result = h.run(h.plan(h.task("a", SpecialistId.GUARDIAN)))
    assert len(result.task_results) == 1
    assert result.delegations and not result.delegations[0].allowed
    assert h.counters.delegations_denied == 1


# ══════════════════════════════════════════════════════════════════════════════
#  CANCELLATION (§25, §26)
# ══════════════════════════════════════════════════════════════════════════════
def test_cancellation_is_one_way(h):
    token = CancellationToken()
    assert not token.cancelled
    token.cancel("operator stopped the team")
    assert token.cancelled and token() is True
    token.cancel("something else")
    assert token.reason == "operator stopped the team"


def test_once_cancelled_no_further_task_starts(h):
    """§25 — pending work becomes CANCELLED, not quietly successful."""
    async def scenario():
        token = CancellationToken()

        async def _first():
            token.cancel("the operator stopped the team")
            return "A observed before the stop."

        h.script["task=a"] = _first
        plan = h.plan(h.task("a", SpecialistId.ATLAS),
                      h.task("b", SpecialistId.MESH, dependencies=("a",)),
                      h.task("c", SpecialistId.TRACE, dependencies=("a",)))
        return await h.orch.run(plan, token=token, admit=False)

    result = asyncio.run(scenario())
    assert result.status is TeamStatus.PARTIAL_SUCCESS
    assert set(result.cancelled) == {"b", "c"}
    assert result.result_for("b").execution is None
    assert "the operator stopped the team" in result.cancelled_reason


def test_cancel_before_the_effect_leaves_the_effect_count_at_zero(h, monkeypatch):
    """§26 — CANCEL BEFORE EFFECT: effect count = 0."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "print(1)"}
    h.add_tool("code_execute", {"stdout": "1"})
    approve(h, SpecialistId.FORGE, "code_execute", args)

    async def scenario():
        token = CancellationToken()

        async def _propose():
            token.cancel("cancelled between reasoning and acting")
            return intent_text("code_execute", args)

        h.script["task=a"] = _propose
        plan = h.plan(
            h.task("a", SpecialistId.FORGE, autonomy=AutonomyLevel.HITL_EXECUTE,
                   allowed_tools=frozenset({"code_execute"}),
                   effect_class=EffectClass.EFFECTFUL),
            authority_ceiling=AutonomyLevel.HITL_EXECUTE)
        return await h.orch.run(plan, token=token, admit=False)

    result = asyncio.run(scenario())
    assert h.count("code_execute") == 0
    assert h.executor.effect_count("code_execute") == 0
    assert result.executed_effects == 0


def test_cancel_after_a_commit_keeps_the_effect_and_says_so(h, monkeypatch):
    """§26 — CANCEL AFTER COMMIT: one effect, receipt preserved, no rollback
    claimed. Cancellation does not erase committed reality."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "print(1)"}
    approve(h, SpecialistId.FORGE, "code_execute", args)

    async def scenario():
        token = CancellationToken()

        def _after_commit(_kwargs):
            token.cancel("cancelled after the effect had already committed")

        h.add_tool("code_execute", {"stdout": "1"}, hook=_after_commit)
        h.script["task=a"] = intent_text("code_execute", args)
        plan = h.plan(
            h.task("a", SpecialistId.FORGE, autonomy=AutonomyLevel.HITL_EXECUTE,
                   allowed_tools=frozenset({"code_execute"}),
                   effect_class=EffectClass.EFFECTFUL),
            h.task("b", SpecialistId.MESH, dependencies=("a",)),
            authority_ceiling=AutonomyLevel.HITL_EXECUTE)
        return await h.orch.run(plan, token=token, admit=False)

    result = asyncio.run(scenario())
    assert h.count("code_execute") == 1, "the committed effect was lost"
    assert result.executed_effects == 1
    assert result.receipts and result.receipts[0].executed
    assert result.result_for("b").state is TaskState.CANCELLED
    assert result.status is TeamStatus.PARTIAL_SUCCESS


def test_a_retry_after_a_commit_does_not_repeat_the_effect(h, monkeypatch):
    """§18 — effect committed, worker never delivered, orchestrator retried."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "print(1)"}
    h.add_tool("code_execute", {"stdout": "1"})
    approve(h, SpecialistId.FORGE, "code_execute", args)
    from core.specialist_execution import MAX_ATTEMPTS
    seen = {"n": 0}

    async def _crash_then_deliver():
        seen["n"] += 1
        if seen["n"] == 1:
            # The effect commits, and the worker dies before the result is
            # delivered — the shape §18 asks for.
            await h.executor.aexecute("code_execute", dict(args), "pre-crash")
            raise RuntimeError("the worker died before delivering its result")
        if seen["n"] <= MAX_ATTEMPTS:
            raise RuntimeError("still down")
        return intent_text("code_execute", args)

    h.script["task=a"] = _crash_then_deliver
    result = h.run(h.plan(
        h.task("a", SpecialistId.FORGE, autonomy=AutonomyLevel.HITL_EXECUTE,
               allowed_tools=frozenset({"code_execute"}),
               effect_class=EffectClass.EFFECTFUL, retry_limit=1),
        authority_ceiling=AutonomyLevel.HITL_EXECUTE))

    assert h.count("code_execute") == 1, "the retry repeated a committed effect"
    receipts = result.receipts
    assert receipts and receipts[0].deduplicated, (
        "the recovered receipt must say the ledger suppressed the repeat")


def test_a_team_timeout_stops_new_work_and_reports_it_honestly(h):
    async def scenario():
        async def _slow():
            await asyncio.sleep(5.0)
            return "too late"

        h.script["task=a"] = _slow
        plan = h.plan(h.task("a", SpecialistId.ATLAS),
                      h.task("b", SpecialistId.MESH, dependencies=("a",)),
                      timeout_budget_s=0.15)
        return await h.orch.run(plan, admit=False)

    result = asyncio.run(scenario())
    assert result.result_for("a").state is TaskState.TIMED_OUT
    # B is SKIPPED rather than CANCELLED, and the distinction is deliberate:
    # its dependency did not succeed, so B was already unrunnable before the
    # timeout stopped anything. Reporting it as CANCELLED would imply the
    # cancellation is what prevented it, which is not what happened. Skip
    # propagation therefore runs BEFORE cancellation in the scheduler loop, and
    # CANCELLED is reserved for work the stop actually prevented.
    assert result.result_for("b").state is TaskState.SKIPPED
    assert result.status is TeamStatus.CANCELLED
    assert "timeout" in result.cancelled_reason


# ══════════════════════════════════════════════════════════════════════════════
#  BACKPRESSURE (§28, §29)
# ══════════════════════════════════════════════════════════════════════════════
def test_admission_accepts_below_the_limit_queues_at_it_and_rejects_above(h):
    controller = TeamAdmissionController(max_active=2, max_queued=1)
    assert controller.admit()[0] is Admission.ACCEPTED
    assert controller.admit()[0] is Admission.ACCEPTED
    assert controller.admit()[0] is Admission.QUEUED
    decision, reason = controller.admit()
    assert decision is Admission.REJECTED
    assert "capacity" in reason
    assert controller.rejected == 1


def test_a_rejected_plan_starts_nothing_at_all(h):
    h.admission.active = h.admission.max_active
    h.admission.queued = h.admission.max_queued
    result = h.run(h.plan(h.task("a", SpecialistId.ATLAS)), admit=True)
    assert result.status is TeamStatus.FAILED
    assert result.task_results == ()
    assert h.calls == [], "a rejected plan consulted a model"
    assert h.counters.queue_rejections == 1


def test_a_released_slot_is_reused_rather_than_leaked(h):
    controller = TeamAdmissionController(max_active=1, max_queued=1)
    assert controller.admit()[0] is Admission.ACCEPTED
    controller.release()
    assert controller.admit()[0] is Admission.ACCEPTED
    assert controller.active == 1


def test_admission_never_grows_without_bound(h):
    controller = TeamAdmissionController(max_active=1, max_queued=1)
    for _ in range(50):
        controller.admit()
    assert controller.active <= 1 and controller.queued <= 1
    assert controller.rejected == 48


def test_a_normal_run_releases_its_slot(h):
    h.run(h.plan(h.task("a", SpecialistId.ATLAS)), admit=True)
    assert h.admission.active == 0


# ══════════════════════════════════════════════════════════════════════════════
#  HITL_TEAM (§37)
# ══════════════════════════════════════════════════════════════════════════════
def hitl_team_plan(h, *, epoch="turn:test"):
    """A = L1 observation, B = test-only L3 effect depending on A, C = independent
    L1 observation. Exactly the shape §37 specifies."""
    return h.plan(
        h.task("a", SpecialistId.ATLAS),
        h.task("b", SpecialistId.FORGE, dependencies=("a",),
               autonomy=AutonomyLevel.HITL_EXECUTE,
               allowed_tools=frozenset({"code_execute"}),
               effect_class=EffectClass.EFFECTFUL,
               resource_claims=(ResourceClaim("file", "./built.txt",
                                              ClaimMode.WRITE),)),
        h.task("c", SpecialistId.MESH),
        authority_ceiling=AutonomyLevel.HITL_EXECUTE, effect_epoch=epoch)


def test_without_approval_the_observers_finish_and_the_effect_does_not_run(
        h, monkeypatch):
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    h.script["task=b"] = intent_text("code_execute", args)

    result = h.run(hitl_team_plan(h))

    assert result.result_for("a").state is TaskState.SUCCESS
    assert result.result_for("c").state is TaskState.SUCCESS
    assert result.result_for("b").state is TaskState.DENIED
    assert h.count("code_execute") == 0
    receipt = result.result_for("b").receipts[0]
    assert receipt.status is ToolCallStatus.DENIED
    assert "human approval required" in receipt.denial_reason


def test_a_bound_approval_lets_the_effect_run_exactly_once(h, monkeypatch):
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.FORGE, "code_execute", args, single_use=True)
    h.script["task=b"] = intent_text("code_execute", args)

    result = h.run(hitl_team_plan(h))

    assert result.result_for("b").state is TaskState.SUCCESS
    assert h.count("code_execute") == 1
    assert result.executed_effects == 1


def test_an_approval_for_a_different_action_does_not_approve_this_one(
        h, monkeypatch):
    """§37 — approval binds plan, task, target/scope, tool and effect identity."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.FORGE, "code_execute", {"code": "something else"})
    h.script["task=b"] = intent_text("code_execute", {"code": "build()"})

    result = h.run(hitl_team_plan(h))
    assert result.result_for("b").state is TaskState.DENIED
    assert h.count("code_execute") == 0


def test_an_approval_for_a_different_specialist_does_not_transfer(h, monkeypatch):
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.CIRCUIT, "code_execute", args)
    h.script["task=b"] = intent_text("code_execute", args)

    result = h.run(hitl_team_plan(h))
    assert result.result_for("b").state is TaskState.DENIED
    assert h.count("code_execute") == 0


def test_an_approval_bound_to_another_epoch_does_not_carry_over(h, monkeypatch):
    """The epoch is part of the effect identity, so it is part of the binding."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.FORGE, "code_execute", args, epoch="turn:other")
    h.script["task=b"] = intent_text("code_execute", args)

    result = h.run(hitl_team_plan(h, epoch="turn:test"))
    assert result.result_for("b").state is TaskState.DENIED
    assert h.count("code_execute") == 0


def test_the_team_path_never_grants_an_approval_to_itself(h):
    """Nothing in the specialist or team path calls ``grant``. That absence is
    the control, so it is asserted over the parsed AST."""
    import ast
    import pathlib

    for name in ("core/specialist_team.py", "core/specialist_execution.py",
                 "core/mesh_live.py"):
        tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "grant", f"{name} grants an approval"


# ══════════════════════════════════════════════════════════════════════════════
#  TEAM_ARGUS (§44, §45)
# ══════════════════════════════════════════════════════════════════════════════
def test_team_argus_verifies_a_clean_run_and_names_what_it_checked(h):
    result = h.run(h.plan(h.task("a", SpecialistId.ATLAS),
                          h.task("b", SpecialistId.MESH, dependencies=("a",))))
    assert result.verification is not None
    assert result.verification.verdict is Verdict.VERIFIED
    assert set(result.verification.checked) == set(TEAM_CHECKS)
    assert result.verified


def test_team_argus_cannot_authorize_anything(h):
    """§38, §44 — a property with no constructor argument behind it."""
    from core.specialist_team import TeamVerification
    verification = TeamVerification(Verdict.VERIFIED)
    assert verification.grants_authority is False
    fields = {f.name for f in dataclasses.fields(TeamVerification)}
    for forbidden in ("autonomy", "scope", "capability", "approval",
                      "grants_authority", "allowed_tools"):
        assert forbidden not in fields


def test_team_argus_catches_an_impossible_success(h):
    """A node cannot claim SUCCESS with no execution behind it."""
    from core.specialist_team import SpecialistTeamResult, TeamTaskResult
    plan = h.plan(h.task("a", SpecialistId.ATLAS))
    forged = SpecialistTeamResult(
        plan_id=plan.plan_id, plan_identity=plan.canonical_identity(),
        status=TeamStatus.SUCCESS,
        task_results=(TeamTaskResult(task_id="a",
                                     specialist_id=SpecialistId.ATLAS,
                                     state=TaskState.SUCCESS),))
    verification = verify_team(plan, forged)
    assert not verification.passing
    assert any("no execution behind it" in r for r in verification.reasons)


def test_team_argus_catches_a_task_that_ran_on_a_failed_dependency(h):
    from core.specialist_team import SpecialistTeamResult, TeamTaskResult
    plan = h.plan(h.task("a", SpecialistId.ATLAS),
                  h.task("b", SpecialistId.MESH, dependencies=("a",)))
    real = h.run(plan)
    good_b = real.result_for("b")
    tampered = SpecialistTeamResult(
        plan_id=plan.plan_id, plan_identity=plan.canonical_identity(),
        status=TeamStatus.PARTIAL_SUCCESS,
        task_results=(TeamTaskResult(task_id="a",
                                     specialist_id=SpecialistId.ATLAS,
                                     state=TaskState.FAILED, finished_seq=1),
                      good_b))
    verification = verify_team(plan, tampered)
    assert not verification.passing
    assert any("ALL_SUCCESS" in r for r in verification.reasons)


def test_team_argus_catches_a_forged_receipt(h, monkeypatch):
    """A receipt id is recomputed with the SAME function that mints one."""
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.FORGE, "code_execute", args)
    h.script["task=b"] = intent_text("code_execute", args)
    plan = hitl_team_plan(h)
    result = h.run(plan)
    assert result.verification.passing

    node = result.result_for("b")
    receipt = node.receipts[0]
    forged_receipt = dataclasses.replace(receipt, receipt_id="rcpt:convincing")
    forged_execution = dataclasses.replace(node.execution,
                                           tool_receipts=(forged_receipt,))
    forged_node = dataclasses.replace(node, execution=forged_execution)
    tampered = dataclasses.replace(
        result, task_results=tuple(forged_node if r.task_id == "b" else r
                                   for r in result.task_results))
    verification = verify_team(plan, tampered)
    assert not verification.passing
    assert any("does not match" in r for r in verification.reasons)


def test_team_argus_catches_a_receipt_from_the_wrong_specialist(h, monkeypatch):
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.FORGE, "code_execute", args)
    h.script["task=b"] = intent_text("code_execute", args)
    plan = hitl_team_plan(h)
    result = h.run(plan)

    node = result.result_for("b")
    stolen = dataclasses.replace(node.receipts[0],
                                 specialist_id=SpecialistId.CIRCUIT)
    # Re-mint the id so ONLY the attribution is wrong; otherwise the previous
    # check would fire and this one would prove nothing.
    from core.specialist_execution import _receipt_id
    stolen = dataclasses.replace(
        stolen, receipt_id=_receipt_id(SpecialistId.CIRCUIT,
                                       stolen.effect_identity))
    tampered_node = dataclasses.replace(
        node, execution=dataclasses.replace(node.execution,
                                            tool_receipts=(stolen,)))
    tampered = dataclasses.replace(
        result, task_results=tuple(tampered_node if r.task_id == "b" else r
                                   for r in result.task_results))
    verification = verify_team(plan, tampered)
    assert not verification.passing
    assert any("attributed to" in r for r in verification.reasons)


def test_team_argus_catches_a_missing_required_evidence(h):
    from core.specialist_team import VERIFICATION_TEAM
    plan = h.plan(h.task("a", SpecialistId.ATLAS,
                         evidence_requirements=("a tool receipt",)),
                  verification_policy=VERIFICATION_TEAM)
    result = h.run(plan)
    node = result.result_for("a")
    stripped = dataclasses.replace(
        node, execution=dataclasses.replace(node.execution, evidence_ids=()))
    tampered = dataclasses.replace(result, task_results=(stripped,))
    verification = verify_team(plan, tampered)
    assert not verification.passing
    assert any("produced no evidence" in r for r in verification.reasons)


def test_team_argus_catches_an_execution_above_the_team_ceiling(h, monkeypatch):
    plan = h.plan(h.task("a", SpecialistId.ATLAS),
                  authority_ceiling=AutonomyLevel.OBSERVE)
    result = h.run(plan)
    node = result.result_for("a")
    lifted = dataclasses.replace(
        node, execution=dataclasses.replace(
            node.execution, effective_autonomy=AutonomyLevel.HITL_EXECUTE))
    tampered = dataclasses.replace(result, task_results=(lifted,))
    verification = verify_team(plan, tampered)
    assert not verification.passing
    assert any("above the team's" in r for r in verification.reasons)


def test_team_argus_catches_a_status_the_nodes_do_not_support(h):
    result = h.run(h.plan(h.task("a", SpecialistId.ATLAS),
                          h.task("b", SpecialistId.MESH)))
    lied = dataclasses.replace(
        result,
        task_results=(result.task_results[0],
                      dataclasses.replace(result.task_results[1],
                                          state=TaskState.FAILED,
                                          execution=None)),
        status=TeamStatus.SUCCESS)
    verification = verify_team(h.plan(h.task("a", SpecialistId.ATLAS),
                                      h.task("b", SpecialistId.MESH)), lied)
    assert not verification.passing
    assert any("derive" in r for r in verification.reasons)


def test_team_argus_catches_a_receipt_from_a_foreign_effect_epoch(h, monkeypatch):
    elevate(monkeypatch, SpecialistId.FORGE, AutonomyLevel.HITL_EXECUTE)
    args = {"code": "build()"}
    h.add_tool("code_execute", {"stdout": "built"})
    approve(h, SpecialistId.FORGE, "code_execute", args)
    h.script["task=b"] = intent_text("code_execute", args)
    plan = hitl_team_plan(h)
    result = h.run(plan)
    # Verify the SAME result against a plan that opened a different epoch.
    other = hitl_team_plan(h, epoch="turn:elsewhere")
    verification = verify_team(other, result)
    assert not verification.passing
    assert any("effect epoch" in r for r in verification.reasons)


def test_team_argus_catches_a_conflict_the_scheduler_should_have_prevented(h):
    """Read from the recorded overlap trace, so it catches a scheduler that
    ignored the arbiter as well as one that never consulted it."""
    claim = (ResourceClaim("file", "./shared.json", ClaimMode.WRITE),)
    plan = h.plan(
        h.task("a", SpecialistId.ATLAS, resource_claims=claim,
               effect_class=EffectClass.EFFECTFUL),
        h.task("b", SpecialistId.MESH, resource_claims=claim,
               effect_class=EffectClass.EFFECTFUL))
    result = h.run(plan)
    assert result.parallel_overlaps == 0
    overlapped = dataclasses.replace(
        result,
        task_results=(dataclasses.replace(result.task_results[0],
                                          started_seq=1, finished_seq=4),
                      dataclasses.replace(result.task_results[1],
                                          started_seq=2, finished_seq=3)))
    verification = verify_team(plan, overlapped)
    assert not verification.passing
    assert any("concurrently while both claiming" in r
               for r in verification.reasons)


def test_team_argus_reports_limitations_rather_than_failing_a_partial_team(h):
    """A partial team is not a violation; saying so would train an operator to
    ignore the verdict."""
    async def _fail():
        raise RuntimeError("down")
    h.script["task=a"] = _fail
    result = h.run(h.plan(h.task("a", SpecialistId.ATLAS),
                          h.task("b", SpecialistId.MESH)))
    assert result.status is TeamStatus.PARTIAL_SUCCESS
    assert result.verification.verdict is Verdict.VERIFIED_WITH_LIMITATIONS
    assert result.verified
