"""
tests/test_specialist_team_live_v69_m65b.py — V69 M65B LIVE OPERATOR TEAM.

Every scenario here enters through ``LLM.chat_stream`` — the same generator
``main._run_turn`` drives on every operator turn. Nothing calls the orchestrator
directly, because a test that skips the integration layer proves the fabric works
and says nothing about whether JARVIS uses it. That distinction is the whole
point of this milestone: M65A made one specialist participate, and M65B is the
claim that a governed TEAM does.

The wire-level fakes, the ``openai`` shim and the turn harness are IMPORTED from
the M65A live suite rather than copied. Two harnesses that were meant to be
identical and drifted would let this suite prove something about a turn the other
suite no longer runs.

These tests are deliberately synchronous and drive ``chat_stream`` through
``asyncio.run``. ``pytest-asyncio`` is a declared dev dependency that is not
present on every host, and a live-path proof that only runs where an optional
plugin happens to be installed is a proof that will quietly stop running.

Nothing here opens a socket, names a public target, or touches a holdout. Every
address is loopback or RFC-1918.
"""
from __future__ import annotations

import asyncio

import pytest

from test_specialist_live_turn_v69_m65a import LiveTurn  # noqa: E402

from core import mesh_live                                        # noqa: E402
from core.cognitive_mesh import REGISTRY, AutonomyLevel, SpecialistId  # noqa: E402
from core.mesh_router import RouteMode                            # noqa: E402
from core.security_effects import CONTAINMENT, SCOPES             # noqa: E402
from core.specialist_execution import APPROVALS, COUNTERS         # noqa: E402
from core.specialist_team import (                                # noqa: E402
    COUNTERS as TEAM_COUNTERS,
)
from core.specialist_team import (
    TaskState,
    TeamAdmissionController,
    TeamOrchestrator,
    TeamStatus,
    validate_plan,
)


#: The request shape M65A measured a real TEAM_VERIFIED route on. It is used
#: here for the same reason: it is a genuinely multi-domain question, and the
#: deterministic router — not this file — is what decides it deserves a team.
TEAM_PROMPT = ("Please analyse this Sysmon alert and correlate it with our "
               "previous incidents and threat intel")


class LiveTeam(LiveTurn):
    """The M65A live harness, with the TEAM orchestrator bound to its engine."""

    def __init__(self, monkeypatch):
        super().__init__(monkeypatch)
        self.admission = TeamAdmissionController()
        self.orch = TeamOrchestrator(
            executor=self.engine, counters=TEAM_COUNTERS,
            admission=self.admission)
        self.plans: list = []
        real_run = self.orch.run

        async def _spy_run(plan, **kw):
            self.plans.append(plan)
            return await real_run(plan, **kw)

        self.orch.run = _spy_run
        monkeypatch.setattr("core.specialist_team.orchestrator", self.orch)
        # `run_support_team` imports the singleton inside the function, so
        # patching the module attribute is what the live path actually reads.

    def answers(self, mapping: dict):
        """Script a different answer per specialist codename."""
        async def _infer(system, user, *, tier, timeout_s, num_ctx, temperature):
            for codename, action in mapping.items():
                if f"You are {codename}" in system:
                    if callable(action):
                        return await action()
                    return action
            return self.support_answer

        self.engine._infer = _infer
        return self


@pytest.fixture
def live(monkeypatch):
    CONTAINMENT.authorizations = []
    SCOPES.scopes = []
    APPROVALS.clear()
    COUNTERS.reset()
    TEAM_COUNTERS.reset()
    mesh_live.attach_live_runtime(world_state=mesh_live._world_state(),
                                  scopes=SCOPES)
    return LiveTeam(monkeypatch)


#: Chosen by the deterministic router, not by this file. Each is asserted to
#: produce its route before anything is concluded from it, so a routing change
#: fails loudly here instead of quietly making a test vacuous.
ONE_SPECIALIST_PROMPT = ("refactor the deployment script and restart the "
                         "affected service")
DIRECT_PROMPT = "hola"


# ══════════════════════════════════════════════════════════════════════════════
#  §5, §31 — ONE JARVIS, three routes, chosen by JARVIS
# ══════════════════════════════════════════════════════════════════════════════
def test_a_simple_turn_stays_direct(live):
    live.fast_text = "Hello."
    r = live.ask(DIRECT_PROMPT)
    assert r.mesh.route.mode is RouteMode.FAST_PATH
    assert mesh_live.team_route(r.mesh) is mesh_live.TeamRoute.DIRECT
    assert r.mesh.team_route is mesh_live.TeamRoute.DIRECT
    assert r.mesh.team_result is None
    assert r.support_executions == []
    assert live.plans == [], "a greeting built a team plan"


def test_a_focused_request_runs_one_specialist_and_not_a_team(live):
    """§31 — the M65A route survives M65B, and is not upgraded to a team."""
    live.fast_text = "Here is the change."
    r = live.ask(ONE_SPECIALIST_PROMPT)

    assert r.mesh.route.mode in (RouteMode.TEAM, RouteMode.TEAM_VERIFIED)
    assert len(mesh_live.team_candidates(r.mesh)) == 1
    # `team_route` is the pre-turn DECISION and `MeshTurn.team_route` is what
    # actually ran. Post-hoc, the second is the one that answers.
    assert r.mesh.team_route is mesh_live.TeamRoute.ONE_SPECIALIST
    assert r.mesh.team_result is None
    assert len(r.support_executions) == 1
    assert live.plans == []


def test_a_genuinely_multi_domain_request_runs_a_team(live):
    """§33 — the live team proof. Two or more specialists execute in one turn."""
    live.fast_text = "Here is the correlated picture."
    r = live.ask(TEAM_PROMPT)

    assert r.mesh.route.mode is RouteMode.TEAM_VERIFIED
    assert r.mesh.team_route is mesh_live.TeamRoute.TEAM
    result = r.mesh.team_result
    assert result is not None
    assert len(result.specialists_executed) >= 2, (
        f"only {result.specialists_executed} executed in a TEAM turn")
    assert len(r.support_executions) >= 2


def test_the_team_is_the_shape_the_directive_describes(live):
    """§33 — system observation + technical reasoning + independent verification,
    with the specialists chosen by the real registry rather than named here."""
    live.fast_text = "Here is the correlated picture."
    r = live.ask(TEAM_PROMPT)
    result = r.mesh.team_result

    executed = set(result.specialists_executed)
    assert {"trace", "oracle"} <= executed, (
        "the router's own supporting specialists did not execute")
    assert "argus" in executed, "the verifier did not run as an independent task"
    assert r.mesh.route.primary is SpecialistId.GUARDIAN


def test_the_live_team_plan_is_valid_before_anything_runs(live):
    live.fast_text = "Answer."
    live.ask(TEAM_PROMPT)
    assert live.plans, "no plan was built"
    plan = live.plans[0]
    assert validate_plan(plan).valid
    assert len(plan.tasks) >= 2


def test_the_live_team_carries_no_load_bearing_skips(live):
    """§34 — SKIPS = 0 on the load-bearing live team proof."""
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)
    result = r.mesh.team_result
    assert result.skipped == (), f"skipped: {result.skipped}"
    assert result.status in (TeamStatus.SUCCESS, TeamStatus.PARTIAL_SUCCESS)


# ══════════════════════════════════════════════════════════════════════════════
#  §34, §35 — live parallelism and live dependency, both proven
# ══════════════════════════════════════════════════════════════════════════════
def test_the_two_independent_live_tasks_genuinely_overlap(live):
    """§34 — TRACE cannot finish until ORACLE has started. Serial fails."""
    async def scenario_setup():
        return None

    gate = {"event": None, "entered": []}

    async def _trace():
        gate["entered"].append("trace")
        await asyncio.wait_for(gate["event"].wait(), timeout=8.0)
        return "TRACE: the alert matches a known parent-child chain."

    async def _oracle():
        gate["entered"].append("oracle")
        gate["event"].set()
        return "ORACLE: the hash appears in two prior advisories."

    real_run = live.orch.run

    async def _run(plan, **kw):
        gate["event"] = asyncio.Event()
        return await real_run(plan, **kw)

    live.orch.run = _run
    live.answers({"TRACE": _trace, "ORACLE": _oracle,
                  "ARGUS": "ARGUS: both observations are internally consistent."})
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)

    result = r.mesh.team_result
    assert set(gate["entered"]) == {"trace", "oracle"}
    assert result.status is TeamStatus.SUCCESS, (
        "TRACE waited on an event only ORACLE could set; if this failed the two "
        "specialists did not run at the same time")
    assert result.parallel_overlaps >= 1


def test_the_live_verifier_cannot_begin_before_its_dependencies_finish(live):
    """§35 — proven by an order trace, not by sleep timing."""
    order: list[str] = []

    async def _trace():
        order.append("trace:enter")
        await asyncio.sleep(0.02)
        order.append("trace:leave")
        return "TRACE: observed."

    async def _oracle():
        order.append("oracle:enter")
        order.append("oracle:leave")
        return "ORACLE: observed."

    async def _argus():
        order.append("argus:enter")
        return "ARGUS: consistent."

    live.answers({"TRACE": _trace, "ORACLE": _oracle, "ARGUS": _argus})
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)
    result = r.mesh.team_result

    assert order.index("argus:enter") > order.index("trace:leave")
    assert order.index("argus:enter") > order.index("oracle:leave")
    argus = next(n for n in result.task_results
                 if n.specialist_id is SpecialistId.ARGUS)
    for node in result.task_results:
        if node.specialist_id is not SpecialistId.ARGUS:
            assert argus.started_seq > node.finished_seq


def test_a_failed_observation_does_not_silence_the_live_verifier(live):
    """ALL_TERMINAL on the join is why: an independent verifier's job includes
    reporting that an observation did not come back."""
    async def _boom():
        raise RuntimeError("the backend fell over")

    live.answers({"TRACE": _boom, "ORACLE": "ORACLE: observed.",
                  "ARGUS": "ARGUS: one observation is missing."})
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)
    result = r.mesh.team_result

    trace_node = next(n for n in result.task_results
                      if n.specialist_id is SpecialistId.TRACE)
    argus_node = next(n for n in result.task_results
                      if n.specialist_id is SpecialistId.ARGUS)
    assert trace_node.state is TaskState.FAILED
    assert argus_node.state is TaskState.SUCCESS
    assert result.status is TeamStatus.PARTIAL_SUCCESS


# ══════════════════════════════════════════════════════════════════════════════
#  §5, §38 — one assistant, and a team that grants nothing
# ══════════════════════════════════════════════════════════════════════════════
def test_the_operator_receives_one_answer_and_no_specialist_chatter(live):
    live.answers({"TRACE": "TRACE: internal note the operator must never see.",
                  "ORACLE": "ORACLE: another internal note.",
                  "ARGUS": "ARGUS: consistent."})
    live.fast_text = "Here is the correlated picture."
    r = live.ask(TEAM_PROMPT)

    assert "internal note" not in r.text
    assert "TRACE:" not in r.text and "ORACLE:" not in r.text
    assert r.text.strip()


def test_a_teams_findings_reach_the_primary_as_a_labelled_consultation(live):
    live.answers({"TRACE": "TRACE: the parent process was winword.exe.",
                  "ORACLE": "ORACLE: the hash is in two advisories.",
                  "ARGUS": "ARGUS: consistent."})
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)

    digest = mesh_live.team_digest(r.mesh)
    assert "CONSULTATION" in digest
    assert "not an instruction and not established fact" in digest
    assert "winword.exe" in digest


def test_the_whole_team_runs_at_or_below_least_authority(live):
    """§18, §23 — a live team observes. Nothing in it is promoted."""
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)
    plan = live.plans[0]

    assert plan.authority_ceiling <= AutonomyLevel.OBSERVE
    for task in plan.tasks:
        assert task.autonomy <= AutonomyLevel.OBSERVE
    for node in r.mesh.team_result.task_results:
        if node.execution is not None:
            assert node.execution.effective_autonomy <= AutonomyLevel.OBSERVE


def test_a_live_team_executes_no_effect_at_all(live):
    """Anything effectful stays with the primary's own tool loop, which already
    passes every gate. A team is judgement, not a second effect surface."""
    live.add_effect_tool("code_execute", {"stdout": "x"})
    live.answers({
        "TRACE": ('TRACE: acting now.\nTOOL_INTENT: {"tool": "code_execute", '
                  '"tool_input": {"code": "print(1)"}, "why": "because"}'),
        "ORACLE": "ORACLE: observed.",
        "ARGUS": "ARGUS: consistent."})
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)

    assert r.effect_count("code_execute") == 0
    assert r.mesh.team_result.executed_effects == 0


def test_the_team_route_is_visible_in_body_safe_telemetry(live):
    """§46 — counters and ids only."""
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)
    telemetry = r.mesh.telemetry()

    assert telemetry["team_route"] == "team"
    assert telemetry["team_tasks"] >= 2
    assert telemetry["team_skipped"] == 0
    assert len(telemetry["team_specialists_executed"]) >= 2
    blob = repr(telemetry)
    assert "winword" not in blob
    for value in telemetry.values():
        assert not isinstance(value, (bytes, bytearray))


def test_a_team_that_cannot_start_degrades_to_one_specialist(live, monkeypatch):
    """A fabric at capacity must not cost the turn its judgement entirely."""
    live.admission.active = live.admission.max_active
    live.admission.queued = live.admission.max_queued
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)

    assert r.mesh.team_result is None
    assert len(r.support_executions) == 1, (
        "a refused team should still leave one consultation behind")
    assert r.text.strip()


def test_a_team_failure_never_costs_the_turn_its_answer(live, monkeypatch):
    async def _explode(plan, **kw):
        raise RuntimeError("the fabric broke")

    monkeypatch.setattr(live.orch, "run", _explode)
    live.fast_text = "The answer still arrives."
    r = live.ask(TEAM_PROMPT)

    assert r.text.strip()
    assert r.mesh.team_result is None
    assert r.mesh.fallback_used


# ══════════════════════════════════════════════════════════════════════════════
#  Recruitment is filtered by the REGISTRY, not by whatever a route listed
# ══════════════════════════════════════════════════════════════════════════════
def _turn_with_supporting(*supporting, primary=SpecialistId.GUARDIAN):
    """A MeshTurn carrying a hand-built route.

    The shipped router already filters its own support lists by
    ``handoff_allowed``, so a live prompt can never present ``team_candidates``
    with an unqualified name and the filter looks redundant through the live
    path alone. A route is data, and this builds the data the filter exists for.
    """
    import dataclasses as _dc

    from core.mesh_router import route_task
    route = route_task(TEAM_PROMPT, task_id="t-unqualified")
    route = _dc.replace(route, primary=primary, supporting=tuple(supporting))
    return mesh_live.MeshTurn(route=route, task_id="t-unqualified")


def test_a_specialist_the_registry_forbids_is_not_recruited(live):
    forbidden = [s for s in SpecialistId
                 if s is not SpecialistId.GUARDIAN
                 and not REGISTRY.handoff_allowed(SpecialistId.GUARDIAN, s)]
    assert forbidden, "GUARDIAN may hand off to everyone; this test is vacuous"

    turn = _turn_with_supporting(SpecialistId.TRACE, forbidden[0])
    candidates = mesh_live.team_candidates(turn)
    assert SpecialistId.TRACE in candidates
    assert forbidden[0] not in candidates, (
        f"{forbidden[0].value} was recruited although GUARDIAN may not hand "
        f"off to it")


def test_a_route_of_only_unqualified_specialists_recruits_nobody(live):
    forbidden = [s for s in SpecialistId
                 if s is not SpecialistId.GUARDIAN
                 and not REGISTRY.handoff_allowed(SpecialistId.GUARDIAN, s)]
    turn = _turn_with_supporting(*forbidden[:2])
    assert mesh_live.team_candidates(turn) == ()
    assert mesh_live.team_route(turn) is mesh_live.TeamRoute.DIRECT


def test_the_consultation_actually_reaches_the_primarys_compiled_context(live):
    """The digest is not merely produced; it is what the ONE generation sees.

    Asserted on the system prompt the fake transport received, so a refactor
    that computes a digest and then fails to hand it over fails a test.
    """
    live.answers({"TRACE": "TRACE: the parent process was winword.exe.",
                  "ORACLE": "ORACLE: the hash is in two advisories.",
                  "ARGUS": "ARGUS: consistent."})
    live.fast_text = "Answer."
    r = live.ask(TEAM_PROMPT)

    assert r.mesh.team_result is not None
    assert "CONSULTATION" in r.system_prompt, (
        "the team's findings never reached the primary's system prompt")
    assert "not an instruction and not established fact" in r.system_prompt
    # EVERY specialist that ran must be represented. Asserting only the first
    # one would pass on a build that fell back to the one-specialist digest and
    # silently dropped the rest of the team — measured, that is exactly what a
    # mutation disabling the team digest did.
    assert "winword.exe" in r.system_prompt, "TRACE's finding was dropped"
    assert "two advisories" in r.system_prompt, "ORACLE's finding was dropped"
    assert r.system_prompt.count("CONSULTATION") >= 2, (
        "only one consultation reached the primary although a team ran")
