"""tests/test_scenario_harness_v67.py — V67 M30 end-to-end scenario harness.

Proves the REAL spine detection-to-response chain is exercised (not mocked) and that
the control plane holds throughout:
  * each canonical scenario (A–E) satisfies its declared facts AND non-facts;
  * correlation rule names/semantics are the real ones (auth_failures_then_success,
    new_service_exposure_then_connection);
  * a container/sensor/exposure change drives the real digital-twin drift types;
  * the recommended runbook is grounded in the real situation output and is only ever
    DRY-RUN — the engine has no ToolExecutor, so a world effect is impossible, and a
    HIGH-impact step (active scan) surfaces as HITL-gated, never executed;
  * re-observation produces a verification outcome (drift clears);
  * duplicate/out-of-order events do NOT explode into multiple findings/incidents;
  * the run is deterministic — identical finding/incident ids on replay.

Pure: no Ollama, no network, no subprocess — the whole chain replays off fixed fixtures.
"""
from __future__ import annotations

import pytest

from core.scenario_harness import (
    SCENARIOS,
    ScenarioHarness,
    adapter_dedup_probe,
    run_scenario,
)


# ── every scenario satisfies all its declared expectations ────────────────────
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_scenario_passes_all_checks(scenario_id):
    out = run_scenario(scenario_id)
    failed = [f"{c.name}: {c.detail}" for c in out.checks if not c.passed]
    assert not failed, f"{scenario_id} failed checks: {failed}"
    assert out.checks, "a scenario must assert at least one check"


# ── A: authentication sequence ────────────────────────────────────────────────
class TestAuthSequence:
    def test_real_auth_rule_fires_into_one_incident(self):
        out = run_scenario("auth_sequence")
        assert [f.rule for f in out.findings] == ["auth_failures_then_success"]
        assert len(out.incidents) == 1
        assert "AUTH_FAILURE_TRIAGE" in out.priority_runbooks()

    def test_triage_runbook_is_read_only_dry_run(self):
        out = run_scenario("auth_sequence")
        assert out.plan is not None
        assert out.plan.runbook == "AUTH_FAILURE_TRIAGE"
        assert out.plan.status == "dry_run"
        assert out.plan.plan.requires_hitl_steps == []   # triage is read-only


# ── B: new service exposure (drift + correlation + HITL-gated scan) ───────────
class TestNewServiceExposure:
    def test_exposure_drift_and_correlation(self):
        out = run_scenario("new_service_exposure")
        assert "new_service_exposure_then_connection" in {f.rule for f in out.findings}
        drift_types = {f.drift_type.value for f in out.drift.findings}
        assert "network_exposure_drift" in drift_types

    def test_scan_step_is_hitl_gated_never_run(self):
        out = run_scenario("new_service_exposure")
        assert out.plan.runbook == "NEW_SERVICE_EXPOSURE_REVIEW"
        assert out.plan.status == "dry_run"                 # planned, not executed
        assert out.plan.plan.requires_hitl_steps            # the active scan is HITL-gated
        # every gated step is an ACTION the operator must approve — nothing auto-ran
        gated = [s for s in out.plan.plan.steps if s["requires_hitl"]]
        assert gated and all(s["action"] for s in gated)


# ── C: sensor coverage loss (uncertainty, NO fabricated compromise) ───────────
class TestSensorLoss:
    def test_no_incident_and_uncertainty_surfaced(self):
        out = run_scenario("sensor_loss")
        assert len(out.incidents) == 0            # sensor loss != compromise
        assert len(out.findings) == 0             # nothing correlated
        assert len(out.situation.uncertainties) > 0

    def test_coverage_and_unknown_drift(self):
        out = run_scenario("sensor_loss")
        drift_types = {f.drift_type.value for f in out.drift.findings}
        assert "sensor_coverage_drift" in drift_types
        assert "state_unknown" in drift_types     # unknown stays unknown, not "safe"
        assert "HOST_CONNECTIVITY_DIAGNOSIS" in out.priority_runbooks()


# ── D: container failure (drift → recommend → verify on re-observation) ───────
class TestContainerFailure:
    def test_workload_stopped_drift(self):
        out = run_scenario("container_failure")
        drift_types = {f.drift_type.value for f in out.drift.findings}
        assert "workload_stopped" in drift_types
        assert out.plan.runbook == "CONTAINER_HEALTH_CHECK"

    def test_verification_only_after_reobservation(self):
        out = run_scenario("container_failure")
        assert out.verification is not None
        assert out.verification["verified"] is True
        assert out.verification["drift_after"] < out.verification["drift_before"]


# ── E: duplicate / out-of-order events do not explode ─────────────────────────
class TestDuplicateOutOfOrder:
    def test_single_finding_and_incident_despite_duplicates(self):
        out = run_scenario("duplicate_out_of_order")
        assert len(out.findings) == 1             # dedup by (rule, entity)
        assert len(out.incidents) == 1            # one case, not one-per-event

    def test_ingestion_boundary_dedups_by_content_hash(self):
        first_new, second_dup = adapter_dedup_probe(
            {"type": "sensor_disconnected", "host": "db-01",
             "message": "heartbeat lost", "severity": "high"})
        assert first_new is True
        assert second_dup is True


# ── control-plane invariants + determinism (hold across the whole harness) ────
class TestControlPlaneAndDeterminism:
    def test_no_scenario_can_effect_the_world(self):
        # A scenario harness constructs the runbook engine WITHOUT a ToolExecutor;
        # every plan is dry-run and nothing can reach the host.
        for sid in SCENARIOS:
            out = run_scenario(sid)
            if out.plan is not None:
                assert out.plan.status == "dry_run"

    def test_deterministic_ids_on_replay(self):
        h = ScenarioHarness()
        a = h.run(SCENARIOS["auth_sequence"])
        b = h.run(SCENARIOS["auth_sequence"])
        assert [f.finding_id for f in a.findings] == [f.finding_id for f in b.findings]
        assert [c.incident_id for c in a.incidents] == [c.incident_id for c in b.incidents]

    def test_aura_events_carry_no_secret_or_raw_credential_keys(self):
        banned = {"credentials_ref", "credentials", "secret", "token",
                  "private_key", "password", "command_line", "raw"}
        for sid in SCENARIOS:
            for ev in run_scenario(sid).aura_events():
                assert banned.isdisjoint(_deep_keys(ev)), f"{sid} leaked a key in {ev.get('type')}"


def _deep_keys(obj) -> set:
    keys: set = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _deep_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys |= _deep_keys(v)
    return keys
