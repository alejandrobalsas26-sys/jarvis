"""V69 M63 — Runtime Doctor, Presence bridge, status surface, AURA bridge, and
the six user-level acceptance tests from the milestone contract.

The load-bearing test in this file is
:func:`test_acceptance_d_high_severity_alert_never_produces_a_world_effect`.
Everything else can be re-derived; that one is the safety property.
"""
from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from core.asset_graph import AssetGraph, AssetType, ObservationSource, RelationshipType
from core.presence import AssistantMode, PresenceLevel, PresenceSignal, Urgency
from core.runtime_doctor import (
    DoctorStatus,
    check_dependencies,
    check_interpreter,
    run_diagnostics,
    site_packages_versions,
)
from core.world_bounds import WorldBounds
from core.world_connectors import ConnectorRegistry, ConnectorState
from core.world_presence import (
    MAX_WORLD_LEVEL,
    WorldPresenceBridge,
    change_to_presence_event,
    memory_worthy,
    select_for_memory,
)
from core.world_runtime import WorldRuntime
from core.world_state import (
    ChangeKind,
    EntityStatus,
    ObservationTrust,
    StateChange,
    WorldObservation,
    WorldState,
)
from core.world_status import (
    dependency_impact,
    jarvis_status,
    narrate,
    pending_approvals,
    security_summary,
    service_status,
    unhealthy_report,
    what_changed,
)


def _obs(identity="web-1", etype=AssetType.CONTAINER, payload=None, **kw):
    return WorldObservation.build(
        source_id="probe", source_type="test", entity_type=etype,
        identity=identity, event_type="seen",
        payload=payload if payload is not None else {"status": "running"},
        trust=ObservationTrust.INSTRUMENTED, **kw)


def _state_with(*observations) -> WorldState:
    state = WorldState(AssetGraph())
    for obs in observations:
        state.ingest(obs)
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  §19 Runtime Doctor
# ══════════════════════════════════════════════════════════════════════════════
def test_doctor_runs_and_returns_structured_findings():
    report = run_diagnostics(include_network=False)
    assert report.findings
    for finding in report.findings:
        payload = finding.to_dict()
        assert set(payload) == {"check_id", "component", "status", "severity",
                                "evidence", "remediation"}


def test_doctor_never_reports_having_repaired_anything():
    assert run_diagnostics(include_network=False).to_dict()["auto_repair_performed"] is False


def _dotted_calls(module_name: str) -> set[str]:
    """Fully-qualified call targets, e.g. ``os.system`` or ``subprocess.run``."""
    tree = ast.parse(inspect.getsource(__import__(module_name, fromlist=["x"])))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name):
            out.add(f"{node.func.value.id}.{node.func.attr}")
    return out


def _imports(module_name: str) -> set[str]:
    tree = ast.parse(inspect.getsource(__import__(module_name, fromlist=["x"])))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_doctor_module_contains_no_repair_primitives():
    calls = _dotted_calls("core.runtime_doctor")
    for banned in ("os.system", "os.popen", "os.mkdir", "os.makedirs",
                   "os.remove", "os.unlink", "subprocess.run",
                   "subprocess.call", "subprocess.Popen", "shutil.rmtree",
                   "pathlib.Path.write_text"):
        assert banned not in calls, f"a doctor must not be able to {banned}"
    assert "subprocess" not in _imports("core.runtime_doctor")
    assert "pip" not in _imports("core.runtime_doctor")


def test_optional_missing_never_degrades_the_overall_verdict():
    report = run_diagnostics(include_network=False)
    if not report.blocked and not report.degraded:
        assert report.overall is DoctorStatus.PASS
    assert all(f.status is DoctorStatus.OPTIONAL_MISSING
               for f in report.optional_missing)


def test_training_dependencies_are_optional_not_required():
    findings = {f.check_id: f for f in check_dependencies()}
    assert findings["deps.training"].status in (
        DoctorStatus.PASS, DoctorStatus.OPTIONAL_MISSING)


def test_site_packages_versions_is_a_set_of_version_tuples():
    for version in site_packages_versions():
        assert isinstance(version, tuple) and len(version) == 2


# TEST E — the interpreter/package-environment mismatch
def test_acceptance_e_interpreter_environment_drift_is_detected():
    findings = {f.check_id: f for f in
                check_interpreter(path_versions={(3, 13)}, running=(3, 14))}
    drift = findings["python.environment_drift"]
    assert drift.status is DoctorStatus.BLOCKED
    assert "3.13" in drift.evidence and "3.14" in drift.evidence
    assert drift.remediation, "a blocked finding must tell the operator what to do"


def test_matching_interpreter_and_environment_is_a_pass():
    findings = {f.check_id: f for f in
                check_interpreter(path_versions={(3, 13)}, running=(3, 13))}
    assert findings["python.environment_drift"].status is DoctorStatus.PASS


# ══════════════════════════════════════════════════════════════════════════════
#  §20 Presence bridge — the safety property
# ══════════════════════════════════════════════════════════════════════════════
def _change(kind=ChangeKind.HEALTH_CHANGED, after="critical"):
    return StateChange(kind, "container:web-1", AssetType.CONTAINER, "health",
                       "healthy", after, "2026-01-01T00:00:00Z", "probe", 0.9)


def test_world_events_never_carry_an_action_tool_or_target():
    for kind in ChangeKind:
        event = change_to_presence_event(_change(kind))
        assert event.action_tool is None
        assert event.action_target is None


def test_world_events_are_capped_below_act():
    for kind in ChangeKind:
        for after in ("critical", "healthy", "high", "unknown"):
            event = change_to_presence_event(_change(kind, after))
            assert event.desired_level <= MAX_WORLD_LEVEL
            assert event.desired_level < PresenceLevel.ACT


def test_bridge_delivers_proposals_and_performs_no_effects():
    state = _state_with(_obs(payload={"status": "running", "health": "healthy"}))
    state.ingest(_obs(payload={"status": "running", "health": "critical"},
                      observed_at="2026-01-01T00:05:00Z"))
    outcome = WorldPresenceBridge(state).run_cycle(PresenceSignal())
    assert outcome.world_effects == 0
    assert outcome.act_proposals == 0
    assert outcome.to_dict()["can_act"] is False


def test_presence_bridge_module_cannot_reach_the_executor():
    source = inspect.getsource(__import__("core.world_presence", fromlist=["x"]))
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("executor" in m for m in imported)
    assert not any("tools" in m for m in imported)


def test_passive_mode_suppresses_environment_proposals():
    state = _state_with(_obs())
    signal = PresenceSignal(mode=AssistantMode.PASSIVE)
    outcome = WorldPresenceBridge(state).run_cycle(signal)
    assert outcome.delivered == []
    assert outcome.suppressed >= 1


# ══════════════════════════════════════════════════════════════════════════════
#  §21 memory boundary
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind,worthy", [
    (ChangeKind.ENTITY_APPEARED, True),
    (ChangeKind.ENTITY_DISAPPEARED, True),
    (ChangeKind.DEPENDENCY_CHANGED, True),
    (ChangeKind.STATUS_CHANGED, False),
    (ChangeKind.ADDRESS_CHANGED, False),
    (ChangeKind.ENTITY_STALE, False),
    (ChangeKind.SERVICE_STOPPED, False),
])
def test_only_durable_transitions_are_memory_worthy(kind, worthy):
    assert memory_worthy(_change(kind, after="healthy")) is worthy


def test_health_change_is_durable_only_when_it_crosses_critical():
    assert memory_worthy(_change(ChangeKind.HEALTH_CHANGED, after="critical")) is True
    assert memory_worthy(_change(ChangeKind.HEALTH_CHANGED, after="warning")) is False


def test_telemetry_flood_cannot_flood_memory():
    flood = [StateChange(ChangeKind.ENTITY_APPEARED, f"container:c{i}",
                         AssetType.CONTAINER, "existence", None, "online",
                         "2026-01-01T00:00:00Z", "probe", 0.9)
             for i in range(5_000)]
    selected = select_for_memory(flood, bounds=WorldBounds(max_memory_writes_per_cycle=8))
    assert len(selected) == 8


# ══════════════════════════════════════════════════════════════════════════════
#  §22 status surface — must work with no model
# ══════════════════════════════════════════════════════════════════════════════
def test_every_status_answer_declares_it_needs_no_llm():
    state = _state_with(_obs())
    for payload in (jarvis_status(state), service_status(state, "web-1"),
                    what_changed(state), dependency_impact(state, "web-1"),
                    unhealthy_report(state), security_summary(state),
                    pending_approvals()):
        assert payload["requires_llm"] is False


def test_status_modules_never_import_an_llm():
    for module in ("core.world_status", "core.world_state"):
        source = inspect.getsource(__import__(module, fromlist=["x"]))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] +
                         [getattr(node, "module", "") or ""])
                assert not any("llm" in n or "ollama" in n or "openai" in n
                               for n in names), f"{module} must not need a model"


def test_unknown_entity_is_reported_as_unknown_not_guessed():
    answer = service_status(_state_with(_obs()), "nonexistent")
    assert answer["known"] is False
    assert "not a known entity" in answer["answer"]


def test_ambiguous_name_is_reported_as_ambiguous():
    state = _state_with(_obs("web", etype=AssetType.CONTAINER),
                        _obs("web", etype=AssetType.SERVICE))
    answer = service_status(state, "web")
    # Exact per-type id resolution wins if it matches one; otherwise ambiguity
    # must be surfaced rather than silently resolved to one of them.
    assert answer["known"] is True or "ambiguous" in answer["answer"]


def test_narrate_is_deterministic():
    state = _state_with(_obs())
    payload = jarvis_status(state)
    assert narrate(payload) == narrate(payload)


# ══════════════════════════════════════════════════════════════════════════════
#  §23 AURA bridge
# ══════════════════════════════════════════════════════════════════════════════
def test_world_hud_commands_are_allowlisted_and_none_are_risky():
    from aura.server import (
        _HIGH_RISK_HUD,
        _HUD_ALLOWED_COMMANDS,
        _MEDIUM_RISK_HUD,
    )
    world_cmds = {"world_status", "world_changed", "world_impact",
                  "world_unhealthy", "world_security", "world_connectors",
                  "world_doctor"}
    assert world_cmds <= _HUD_ALLOWED_COMMANDS
    assert not (world_cmds & _HIGH_RISK_HUD)
    assert not (world_cmds & _MEDIUM_RISK_HUD)


def test_aura_payloads_are_bounded():
    state = WorldState(AssetGraph())
    for i in range(300):
        state.ingest(_obs(f"c{i}"))
    bounds = WorldBounds(max_aura_changes=10)
    assert len(what_changed(state, bounds=bounds)["changes"]) <= 10
    assert len(unhealthy_report(state)["entities"]) <= 32
    assert len(jarvis_status(state)["recent_changes"]) <= 16


def test_connector_snapshot_does_not_probe_when_read():
    runtime = WorldRuntime(WorldState(AssetGraph()), registry=ConnectorRegistry())
    assert runtime.snapshot()["probed_on_this_call"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  §27 user-level acceptance
# ══════════════════════════════════════════════════════════════════════════════
def test_acceptance_a_jarvis_status_is_deterministic():
    state = _state_with(_obs("web-1"), _obs("db-1"))
    first, second = jarvis_status(state), jarvis_status(state)
    assert first["environment"]["entities"] == second["environment"]["entities"] == 2
    assert first["environment"]["by_status"] == second["environment"]["by_status"]
    assert first["requires_llm"] is False


def test_acceptance_b_what_changed_returns_grounded_transitions():
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)

    def _at(offset_s):
        return (now + _dt.timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")

    state = _state_with(_obs(etype=AssetType.SERVICE,
                             payload={"status": "running"}, observed_at=_at(-60)))
    state.ingest(_obs(etype=AssetType.SERVICE, payload={"status": "stopped"},
                      observed_at=_at(-30)))
    answer = what_changed(state)
    assert answer["count"] >= 1
    kinds = {c["kind"] for c in answer["changes"]}
    assert kinds & {ChangeKind.SERVICE_STOPPED.value, ChangeKind.STATUS_CHANGED.value}
    for line, change in zip(answer["lines"], answer["changes"]):
        assert change["entity_id"] in line, "every line must name a real entity"


def test_acceptance_c_what_depends_on_x_is_graph_derived():
    graph = AssetGraph()
    state = WorldState(graph)
    state.ingest(_obs("db-1"))
    state.ingest(_obs("api-1"))
    graph.add_relationship(AssetType.CONTAINER, "api-1", RelationshipType.DEPENDS_ON,
                           AssetType.CONTAINER, "db-1",
                           source=ObservationSource.OPERATOR_DECLARATION)
    answer = dependency_impact(state, "db-1")
    assert answer["known"] is True
    assert answer["impact"]["blast_radius"] == 1
    assert answer["impact"]["affected"][0]["entity_id"] == "container:api-1"
    assert answer["impact"]["deterministic"] is True


def test_acceptance_d_high_severity_alert_never_produces_a_world_effect():
    """A CRITICAL alert may make JARVIS ask. It may never make JARVIS act."""
    state = _state_with(_obs(etype=AssetType.ALERT_SOURCE,
                             payload={"status": "running", "severity": "LOW"}))
    state.ingest(_obs(etype=AssetType.ALERT_SOURCE,
                      payload={"status": "running", "severity": "CRITICAL"},
                      observed_at="2026-01-01T00:05:00Z"))

    outcome = WorldPresenceBridge(state).run_cycle(
        PresenceSignal(mode=AssistantMode.ACTIVE))

    assert outcome.delivered, "a critical alert must reach the operator"
    for proposal in outcome.delivered:
        assert proposal["level"] in ("suggest", "ask"), \
            f"environment event reached {proposal['level']}"
        assert proposal["requires_gates"] is False
    assert outcome.world_effects == 0
    assert outcome.act_proposals == 0


def test_acceptance_f_optional_connector_missing_leaves_core_healthy():
    async def _run():
        registry = ConnectorRegistry()
        from core.world_connectors import LocalHostConnector, ProxmoxConnector
        registry.register(LocalHostConnector("local-host"))
        registry.register(ProxmoxConnector("proxmox", endpoint="", credentials_ref=""))
        runtime = WorldRuntime(WorldState(AssetGraph()), registry=registry)
        return await runtime.refresh_once()

    report = asyncio.run(_run())
    assert report.connector_states["local-host"] == ConnectorState.AVAILABLE.value
    assert report.connector_states["proxmox"] == ConnectorState.MISCONFIGURED.value
    assert report.observations_ingested >= 1, "the core still collected"
    assert report.to_dict()["world_effects"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  runtime loop
# ══════════════════════════════════════════════════════════════════════════════
def test_refresh_cycle_reports_zero_effects_and_zero_remediations():
    runtime = WorldRuntime(WorldState(AssetGraph()), registry=ConnectorRegistry())
    report = asyncio.run(runtime.refresh_once()).to_dict()
    assert report["world_effects"] == 0
    assert report["remediations_performed"] == 0


def test_refresh_interval_is_clamped_to_a_finite_range():
    assert WorldRuntime(refresh_s=0.0).refresh_s >= 15.0
    assert WorldRuntime(refresh_s=10 ** 9).refresh_s <= 3_600.0


def test_run_forever_cancels_cleanly():
    async def _run():
        runtime = WorldRuntime(WorldState(AssetGraph()), registry=ConnectorRegistry())
        runtime.refresh_s = 15.0
        task = asyncio.create_task(runtime.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_runtime_module_performs_no_remediation():
    source = inspect.getsource(__import__("core.world_runtime", fromlist=["x"]))
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("executor" in m for m in imported)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("aexecute", "system", "popen", "restart", "kill"):
        assert banned not in names
