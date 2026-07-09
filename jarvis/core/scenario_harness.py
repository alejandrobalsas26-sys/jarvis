"""core/scenario_harness.py — V67 M30: deterministic end-to-end operational scenarios.

Proves the REAL V66 spine end-to-end without mocking it: a scenario is a set of
canonical :class:`~core.ops_events.OperationalEvent` fixtures + digital-twin state
fixtures that are driven through the *actual* components —

    Fixture → OperationalEvent → CorrelatorV2 → CorrelationFinding
            → IncidentWorkspace (case) → DigitalTwin drift → SituationEngine
            → recommended Runbook → RunbookEngine.dry_run (plan only)
            → re-observation → verification → (AURA projection via ops_views)

Nothing here bypasses the control plane. The RunbookEngine is constructed WITHOUT a
ToolExecutor, so any world-effect step fails closed by construction — a scenario can
only ever produce a *dry-run plan*; it can never touch the host. HIGH-impact steps
(e.g. an active scan) surface in the plan as HITL-gated, they are never executed.

Determinism: every timestamp derives from a fixed anchor (:data:`T0`); no wall-clock
enters the evaluated path, so finding-ids, incident-ids and drift-ids are stable and
the whole chain replays identically. External telemetry is treated as untrusted data
that can populate evidence but can never expand scope, authorize a tool, or run one.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.asset_graph import AssetGraph, AssetType, ObservationSource
from core.correlation_v2 import CorrelationFinding, CorrelatorV2
from core.digital_twin import DigitalTwin, DriftType, FactKind, TwinSnapshot
from core.incident_workspace import IncidentCase, IncidentWorkspace
from core.ops_events import (
    EntityReference,
    EntityType,
    EventCategory,
    EventProvenance,
    EventSeverity,
    EventSource,
    EvidenceReference,
    OperationalEvent,
    normalize_event,
)
from core.runbook_engine import RunbookEngine, RunbookResult
from core.situation_engine import SituationEngine, SituationSnapshot

# ── deterministic clock anchor — NO wall-clock in the evaluated path ───────────
T0 = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _iso(offset_sec: float) -> str:
    """A stable ISO-8601 timestamp `offset_sec` after the fixed scenario anchor."""
    return (T0 + timedelta(seconds=offset_sec)).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class FixtureEvent:
    """A scenario event fixture → one canonical OperationalEvent (built with the
    real content-hash identity, so duplicates collide exactly like production)."""
    offset_sec: float
    category: EventCategory
    severity: EventSeverity
    signature: str = ""
    source: EventSource = EventSource.JARVIS_INTERNAL
    host: str | None = None
    user: str | None = None
    src_ip: str | None = None
    dst_ip: str | None = None
    process: str | None = None
    rule_id: str | None = None
    techniques: tuple[str, ...] = ()

    def _entities(self) -> tuple[EntityReference, ...]:
        out: list[EntityReference] = []
        if self.host:
            out.append(EntityReference(EntityType.HOST, self.host))
        if self.src_ip:
            out.append(EntityReference(EntityType.IP, self.src_ip))
        if self.dst_ip and self.dst_ip != self.src_ip:
            out.append(EntityReference(EntityType.IP, self.dst_ip))
        if self.user:
            out.append(EntityReference(EntityType.USER, self.user))
        if self.process:
            out.append(EntityReference(EntityType.PROCESS, self.process))
        return tuple(out)

    def to_event(self) -> OperationalEvent:
        iso = _iso(self.offset_sec)
        prov = EventProvenance(
            source=self.source, source_instance="scenario",
            adapter="scenario_fixture", signed=True, ingested_at=iso,
        )
        ev = OperationalEvent(
            event_id="", provenance=prov, source=self.source, category=self.category,
            severity=self.severity, timestamp=iso, observed_at=iso, confidence=0.9,
            host=self.host, user=self.user, src_ip=self.src_ip, dst_ip=self.dst_ip,
            process=self.process, rule_id=self.rule_id, signature=self.signature,
            mitre_techniques=self.techniques, entities=self._entities(),
            evidence=(EvidenceReference("telemetry", f"scenario:{self.signature[:40]}", ""),),
        )
        chash = ev.compute_content_hash()
        return dataclasses.replace(ev, content_hash=chash, event_id=f"oe_{chash[:16]}")


@dataclass(frozen=True)
class FactFixture:
    """A digital-twin state fact (expected baseline OR an observation)."""
    asset: str
    key: str
    value: str | None
    kind: FactKind


@dataclass(frozen=True)
class SeedAsset:
    """A minimal evidence-backed asset-graph seed so the situation engine can
    account for known assets (operator-declared provenance)."""
    asset_type: AssetType
    identity: str
    attribute: str
    value: str


@dataclass(frozen=True)
class Expectation:
    """The expected facts AND non-facts a scenario must satisfy — asserted against
    the real component output, never fabricated."""
    expected_rule: str | None = None            # a correlation rule that MUST fire
    max_findings: int | None = None             # dedup bound (out-of-order/duplicate)
    expect_incident: bool = False
    max_incidents: int | None = None
    forbid_incident: bool = False               # sensor loss must NOT fabricate compromise
    expected_drift_types: tuple[str, ...] = ()
    expected_runbook: str | None = None         # MUST appear among priority recommendations
    expect_hitl_in_plan: bool | None = None     # dry-run plan surfaces a HITL-gated step
    expect_uncertainty: bool = False
    min_situation_rank: int = 0                 # SituationSeverity.rank floor
    expect_verified: bool | None = None         # re-observation clears the drift


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    description: str
    primary_target: str                          # clean host/ip for runbook params
    events: tuple[FixtureEvent, ...] = ()
    expected: tuple[FactFixture, ...] = ()        # twin baseline (operator/config-owned)
    observed: tuple[FactFixture, ...] = ()        # twin observations (state change)
    reobserved: tuple[FactFixture, ...] = ()      # re-observation after the (dry-run) action
    sensor_health: dict = field(default_factory=dict)
    seed_assets: tuple[SeedAsset, ...] = ()
    expectation: Expectation = Expectation()


# ══════════════════════════════════════════════════════════════════════════════
#  Outcome
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class ScenarioOutcome:
    scenario_id: str
    findings: list[CorrelationFinding]
    incidents: list[IncidentCase]
    drift: TwinSnapshot
    situation: SituationSnapshot
    plan: RunbookResult | None
    verification: dict | None
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def priority_runbooks(self) -> set[str]:
        return {p.recommended_runbook for p in self.situation.priorities}

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "incidents": [c.to_dict() for c in self.incidents],
            "drift": self.drift.to_dict(),
            "situation": self.situation.to_dict(),
            "plan": self.plan.to_dict() if self.plan else None,
            "verification": self.verification,
            "checks": [c.to_dict() for c in self.checks],
        }

    def aura_events(self) -> list[dict]:
        """Bounded, redaction-safe AURA projections of this run (via ops_views).
        Demonstrates the M31 wiring surface — no credentials/secrets/raw text."""
        from core import ops_views
        evts: list[dict] = []
        for f in self.findings:
            evts.append(ops_views.correlation_finding_event(f))
        for c in self.incidents:
            evts.append(ops_views.incident_case_event(c))
        for df in self.drift.findings:
            evts.append(ops_views.drift_finding_event(df))
        evts.append(ops_views.situation_event(self.situation))
        if self.plan is not None:
            evts.append(ops_views.runbook_plan_event(self.plan))
        return evts


# ══════════════════════════════════════════════════════════════════════════════
#  Harness — drives fixtures through the REAL spine
# ══════════════════════════════════════════════════════════════════════════════
class ScenarioHarness:
    """Runs a :class:`Scenario` against fresh instances of the real components and
    evaluates its expectations. Constructs the RunbookEngine with NO ToolExecutor,
    so the world-effect path is fail-closed — a scenario can only dry-run."""

    def run(self, scenario: Scenario) -> ScenarioOutcome:
        graph = AssetGraph()
        correlator = CorrelatorV2(asset_graph=graph)
        workspace = IncidentWorkspace()
        twin = DigitalTwin()
        situation_engine = SituationEngine()
        runbooks = RunbookEngine()  # no tool_executor → world-effect fails closed

        last_offset = max((e.offset_sec for e in scenario.events), default=0.0)
        t_end = _iso(last_offset + 1)

        # ── evidence-backed asset seeds (operator declaration) ────────────────
        for s in scenario.seed_assets:
            graph.add_observation(
                s.asset_type, s.identity, s.attribute, s.value,
                source=ObservationSource.OPERATOR_DECLARATION, observer="scenario",
                event_refs=(f"scenario:{scenario.scenario_id}",), now_iso=_iso(0))

        # ── 1) canonical events → correlator → findings → incident cases ──────
        findings: list[CorrelationFinding] = []
        cases: dict[str, IncidentCase] = {}
        for fe in scenario.events:
            for f in correlator.ingest_event(fe.to_event()):
                findings.append(f)
                case = workspace.ingest_finding(f, now_iso=t_end)
                cases[case.incident_id] = case

        # ── 2) twin baseline + observations → drift ───────────────────────────
        for ff in scenario.expected:
            twin.set_expected(ff.asset, ff.key, ff.value, kind=ff.kind, now_iso=_iso(0))
        for ff in scenario.observed:
            twin.observe(ff.asset, ff.key, ff.value, kind=ff.kind,
                         source="canonical_event", confidence=0.7, now_iso=t_end)
        drift = twin.compute_drift(now_iso=t_end)

        # attach drift as EVIDENCE to an open case on the same asset (never opens one)
        for df in drift.findings:
            for case in workspace.open_cases():
                if any(df.asset == ref.split(":")[-1] for ref in case.affected_assets):
                    DigitalTwin.drift_to_incident_evidence(df, case)

        # ── 3) situation snapshot (deterministic synthesis) ───────────────────
        situation = situation_engine.build(
            asset_graph=graph, incidents=workspace.open_cases(), drift=drift,
            correlation_findings=findings, sensor_health=scenario.sensor_health,
            now_iso=t_end)

        # ── 4) recommended runbook → DRY RUN (plan only, gated) ───────────────
        priority_runbooks = {p.recommended_runbook for p in situation.priorities}
        chosen = scenario.expectation.expected_runbook or (
            situation.recommendations[0].runbook if situation.recommendations else None)
        plan: RunbookResult | None = None
        if chosen:
            plan = runbooks.dry_run(
                chosen, {"host": scenario.primary_target, "target": scenario.primary_target})

        # ── 5) re-observation + verification (still NO world effect) ──────────
        verification: dict | None = None
        if scenario.reobserved:
            re_iso = _iso(last_offset + 60)
            for ff in scenario.reobserved:
                twin.observe(ff.asset, ff.key, ff.value, kind=ff.kind,
                             source="reobservation", confidence=0.8, now_iso=re_iso)
            drift_after = twin.compute_drift(now_iso=re_iso)
            before = {f.finding_id for f in drift.findings}
            after = {f.finding_id for f in drift_after.findings}
            cleared = sorted(before - after)
            verification = {
                "reobserved_at": re_iso,
                "drift_before": len(before),
                "drift_after": len(after),
                "cleared_findings": cleared,
                "verified": bool(cleared) and len(after) < len(before),
            }

        outcome = ScenarioOutcome(
            scenario_id=scenario.scenario_id, findings=findings,
            incidents=list(cases.values()), drift=drift, situation=situation,
            plan=plan, verification=verification)
        _evaluate(scenario, outcome, priority_runbooks, runbooks)
        return outcome


def _evaluate(scenario: Scenario, out: ScenarioOutcome,
              priority_runbooks: set[str], runbooks: RunbookEngine) -> None:
    exp = scenario.expectation
    fired = {f.rule for f in out.findings}
    drift_types = {f.drift_type.value for f in out.drift.findings}

    if exp.expected_rule is not None:
        out.checks.append(Check(
            "correlation_rule_fired",
            exp.expected_rule in fired,
            f"expected {exp.expected_rule!r}; fired {sorted(fired)}"))

    if exp.max_findings is not None:
        out.checks.append(Check(
            "findings_bounded", len(out.findings) <= exp.max_findings,
            f"{len(out.findings)} findings (max {exp.max_findings})"))

    if exp.expect_incident:
        out.checks.append(Check("incident_opened", len(out.incidents) >= 1,
                                f"{len(out.incidents)} case(s)"))
    if exp.forbid_incident:
        out.checks.append(Check("no_fabricated_incident", len(out.incidents) == 0,
                                f"{len(out.incidents)} case(s) — must be 0"))
    if exp.max_incidents is not None:
        out.checks.append(Check("incidents_bounded",
                                len(out.incidents) <= exp.max_incidents,
                                f"{len(out.incidents)} case(s) (max {exp.max_incidents})"))

    for dt in exp.expected_drift_types:
        out.checks.append(Check(f"drift:{dt}", dt in drift_types,
                                f"drift types {sorted(drift_types)}"))

    if exp.expected_runbook is not None:
        out.checks.append(Check(
            "runbook_recommended_grounded",
            exp.expected_runbook in priority_runbooks,
            f"expected {exp.expected_runbook!r}; recommended {sorted(priority_runbooks)}"))

    if exp.expect_uncertainty:
        out.checks.append(Check("uncertainty_surfaced",
                                len(out.situation.uncertainties) > 0,
                                f"{len(out.situation.uncertainties)} uncertain item(s)"))

    out.checks.append(Check(
        "situation_severity_floor",
        out.situation.severity.rank >= exp.min_situation_rank,
        f"severity={out.situation.severity.value} (rank {out.situation.severity.rank} "
        f">= {exp.min_situation_rank})"))

    # ── control-plane invariants (asserted on EVERY scenario) ─────────────────
    if out.plan is not None:
        out.checks.append(Check("runbook_dry_run_only", out.plan.status == "dry_run",
                                f"status={out.plan.status}"))
        hitl = out.plan.plan.requires_hitl_steps if out.plan.plan else []
        if exp.expect_hitl_in_plan is True:
            out.checks.append(Check("hitl_gate_present", len(hitl) > 0,
                                    f"hitl steps={hitl}"))
        elif exp.expect_hitl_in_plan is False:
            out.checks.append(Check("no_hitl_read_only", len(hitl) == 0,
                                    f"hitl steps={hitl}"))
    # the engine has NO executor wired → a world-effect is structurally impossible
    out.checks.append(Check("world_effect_fail_closed",
                            runbooks._tool_executor is None,
                            "RunbookEngine has no ToolExecutor - cannot effect the host"))

    if exp.expect_verified is not None:
        got = bool(out.verification and out.verification.get("verified"))
        out.checks.append(Check("verification_outcome", got == exp.expect_verified,
                                f"verified={got} (expected {exp.expect_verified})"))


# ══════════════════════════════════════════════════════════════════════════════
#  The five canonical scenarios (A–E)
# ══════════════════════════════════════════════════════════════════════════════
def _scenario_auth_sequence() -> Scenario:
    """A — repeated auth failures → success → the real `auth_failures_then_success`
    correlation → incident case → AUTH_FAILURE_TRIAGE recommended (read-only)."""
    host = "workstation-7"
    events = (
        FixtureEvent(0, EventCategory.AUTH, EventSeverity.HIGH,
                     "Windows 4625 logon failure (bad password)", host=host, user="admin",
                     rule_id="4625", source=EventSource.SYSMON),
        FixtureEvent(5, EventCategory.AUTH, EventSeverity.HIGH,
                     "Windows 4625 logon failure (bad password)", host=host, user="admin",
                     rule_id="4625", source=EventSource.SYSMON),
        FixtureEvent(10, EventCategory.AUTH, EventSeverity.HIGH,
                     "Windows 4625 logon failure invalid credentials", host=host, user="admin",
                     rule_id="4625", source=EventSource.SYSMON),
        FixtureEvent(20, EventCategory.AUTH, EventSeverity.HIGH,
                     "Windows 4624 logon success (interactive)", host=host, user="admin",
                     rule_id="4624", source=EventSource.SYSMON, techniques=("T1110",)),
    )
    return Scenario(
        scenario_id="auth_sequence", name="Authentication sequence",
        description="Repeated authentication failures followed by a success on one host.",
        primary_target=host, events=events,
        seed_assets=(SeedAsset(AssetType.PHYSICAL_HOST, host, "os", "Windows 11"),),
        expectation=Expectation(
            expected_rule="auth_failures_then_success", expect_incident=True,
            max_incidents=1, expected_runbook="AUTH_FAILURE_TRIAGE",
            expect_hitl_in_plan=False, min_situation_rank=3),
    )


def _scenario_new_service_exposure() -> Scenario:
    """B — a newly observed service/exposure → connection → exposure drift +
    `new_service_exposure_then_connection` correlation → NEW_SERVICE_EXPOSURE_REVIEW
    (whose active scan is HITL-gated in the plan)."""
    ip = "10.0.0.5"
    events = (
        FixtureEvent(0, EventCategory.NETWORK, EventSeverity.MEDIUM,
                     "new service listening/bind on 8080 (exposure observed)",
                     src_ip=ip, source=EventSource.NETWORK_BASELINE),
        FixtureEvent(8, EventCategory.NETWORK, EventSeverity.MEDIUM,
                     "inbound connection attempt to 10.0.0.5:8080",
                     src_ip=ip, dst_ip=ip, source=EventSource.ZEEK_CONN),
    )
    return Scenario(
        scenario_id="new_service_exposure", name="New service exposure",
        description="A newly exposed service on a host, then a connection attempt.",
        primary_target=ip, events=events,
        observed=(FactFixture(ip, "exposure:8080", "external", FactKind.EXPOSURE),),
        seed_assets=(SeedAsset(AssetType.UNKNOWN, ip, "role", "lab endpoint"),),
        expectation=Expectation(
            expected_rule="new_service_exposure_then_connection",
            expected_drift_types=(DriftType.NETWORK_EXPOSURE_DRIFT.value,),
            expected_runbook="NEW_SERVICE_EXPOSURE_REVIEW",
            expect_hitl_in_plan=True, min_situation_rank=3),
    )


def _scenario_sensor_loss() -> Scenario:
    """C — a sensor heartbeat disappears → SENSOR_COVERAGE_DRIFT + an expected fact
    that can no longer be verified (STATE_UNKNOWN → uncertainty). NO fabricated
    compromise (no incident), operator recommendation is HOST_CONNECTIVITY_DIAGNOSIS."""
    host = "edge-01"
    return Scenario(
        scenario_id="sensor_loss", name="Sensor coverage loss",
        description="A sensor heartbeat disappears; coverage degrades, state becomes uncertain.",
        primary_target=host,
        expected=(
            FactFixture(host, "sensor:mesh", "connected", FactKind.SENSOR),
            # a service we can no longer observe once the sensor is blind → unknown
            FactFixture(host, "service:api", "active", FactKind.SERVICE),
        ),
        observed=(
            FactFixture(host, "sensor:mesh", "disconnected", FactKind.SENSOR),
            # note: service:api is deliberately NOT observed → STATE_UNKNOWN
        ),
        sensor_health={"edge-01-agent": "disconnected"},
        seed_assets=(SeedAsset(AssetType.SERVER, host, "os", "ubuntu-22.04"),),
        expectation=Expectation(
            forbid_incident=True,
            expected_drift_types=(DriftType.SENSOR_COVERAGE_DRIFT.value,
                                  DriftType.STATE_UNKNOWN.value),
            expected_runbook="HOST_CONNECTIVITY_DIAGNOSIS",
            expect_hitl_in_plan=False, expect_uncertainty=True, min_situation_rank=3),
    )


def _scenario_container_failure() -> Scenario:
    """D — a container/workload stops → WORKLOAD_STOPPED drift → CONTAINER_HEALTH_CHECK;
    verification only AFTER re-observation shows it running again."""
    container = "container-web"
    return Scenario(
        scenario_id="container_failure", name="Container failure",
        description="A container/workload stops; drift is observed and verified on recovery.",
        primary_target=container,
        expected=(FactFixture(container, "workload:state", "running", FactKind.WORKLOAD),),
        observed=(FactFixture(container, "workload:state", "stopped", FactKind.WORKLOAD),),
        reobserved=(FactFixture(container, "workload:state", "running", FactKind.WORKLOAD),),
        seed_assets=(SeedAsset(AssetType.CONTAINER, container, "image", "nginx:latest"),),
        expectation=Expectation(
            forbid_incident=True,
            expected_drift_types=(DriftType.WORKLOAD_STOPPED.value,),
            expected_runbook="CONTAINER_HEALTH_CHECK",
            expect_hitl_in_plan=False, min_situation_rank=2, expect_verified=True),
    )


def _scenario_duplicate_out_of_order() -> Scenario:
    """E — duplicate + out-of-order + clock-skewed events must NOT explode into
    multiple findings/incidents. The auth pattern still resolves to ONE finding and
    ONE case (content-hash identity + (rule,entity) dedup)."""
    host = "db-01"

    def fail(off):
        return FixtureEvent(off, EventCategory.AUTH, EventSeverity.HIGH,
                            "Windows 4625 logon failure denied", host=host, user="svc",
                            rule_id="4625", source=EventSource.SYSMON)

    def ok(off):
        return FixtureEvent(off, EventCategory.AUTH, EventSeverity.HIGH,
                            "Windows 4624 logon success accepted", host=host, user="svc",
                            rule_id="4624", source=EventSource.SYSMON)

    events = (
        fail(0), fail(5), fail(0),   # 3rd is an exact DUPLICATE of the 1st
        fail(10), ok(20), ok(20),    # 6th is an exact DUPLICATE success
        fail(8),                     # a LATE, out-of-order failure (clock skew)
    )
    return Scenario(
        scenario_id="duplicate_out_of_order", name="Duplicate / out-of-order events",
        description="Duplicate, late and clock-skewed events must not create duplicate incidents.",
        primary_target=host, events=events,
        seed_assets=(SeedAsset(AssetType.PHYSICAL_HOST, host, "os", "Linux"),),
        expectation=Expectation(
            expected_rule="auth_failures_then_success", expect_incident=True,
            max_findings=1, max_incidents=1, expected_runbook="AUTH_FAILURE_TRIAGE",
            expect_hitl_in_plan=False, min_situation_rank=3),
    )


SCENARIOS: dict[str, Scenario] = {
    s.scenario_id: s for s in (
        _scenario_auth_sequence(),
        _scenario_new_service_exposure(),
        _scenario_sensor_loss(),
        _scenario_container_failure(),
        _scenario_duplicate_out_of_order(),
    )
}


def run_scenario(scenario_id: str) -> ScenarioOutcome:
    """Run one named scenario through the real spine and return its outcome."""
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise KeyError(f"unknown scenario {scenario_id!r}; "
                       f"available: {sorted(SCENARIOS)}")
    return ScenarioHarness().run(scenario)


def run_all() -> dict[str, ScenarioOutcome]:
    harness = ScenarioHarness()
    return {sid: harness.run(s) for sid, s in SCENARIOS.items()}


def adapter_dedup_probe(payload: dict) -> tuple[bool, bool]:
    """Exercise the REAL adapter-registry dedup: normalize the same payload twice
    and report (first_is_new, second_is_duplicate). Used by Scenario E to prove the
    ingestion boundary dedups by content hash, not just the correlator."""
    first = normalize_event(payload, now_iso=_iso(0))
    second = normalize_event(payload, now_iso=_iso(0))
    return (first.ok and not first.duplicate, second.duplicate)
