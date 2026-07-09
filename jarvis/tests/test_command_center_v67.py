"""tests/test_command_center_v67.py — V67 M31 live operator command center.

Proves the command center EXTENDS the existing AURA projection layer (ops_views) —
it composes the existing bounded/redacted panels with the new V67 collector,
environment and model/runtime sources plus the operator's five-question digest, and:
  * every required panel is present (system/asset/incidents/drift/sensors/collectors/
    correlations/runbooks/environments/model-runtime + situation + digest);
  * payloads stay bounded and NO forbidden key (credential/token/command_line/raw/…)
    ever reaches the HUD, at any nesting depth;
  * the environments panel never leaks the credentials reference;
  * the model/runtime panel resolves roles WITHOUT probing Ollama (no I/O / no block);
  * the empty-state answer is honest — 'no evidence of an active incident', never
    'everything is secure' (unknown != safe).

Pure: builds off the deterministic scenario spine + fresh registries; no HUD, no Ollama.
"""
from __future__ import annotations

from core.collector_fabric import CollectorFabric, CollectorSpec
from core.config import settings
from core.environment_registry import EnvironmentRegistry, EnvironmentType
from core.ops_views import (
    _FORBIDDEN_KEYS,
    build_live_command_center,
    collectors_panel,
    command_center,
    environments_panel,
    model_runtime_panel,
    operational_digest,
)
from core.situation_engine import SituationEngine
from core.scenario_harness import SCENARIOS, ScenarioHarness

_REQUIRED_PANELS = {
    "system_status", "assets", "incidents", "drift", "sensors", "collectors",
    "correlations", "runbooks", "environments", "model_runtime",
}


def _deep_keys(obj) -> set:
    keys: set = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k).lower())
            keys |= _deep_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys |= _deep_keys(v)
    return keys


def _populated_cc() -> dict:
    """A command center built from a real scenario outcome (situation/drift/findings/
    incidents) plus a fresh registry + fabric with live content."""
    out = ScenarioHarness().run(SCENARIOS["new_service_exposure"])

    reg = EnvironmentRegistry()
    reg.enroll("docker-local", EnvironmentType.DOCKER, "Local Docker",
               endpoint="npipe://docker", authorized=True)
    reg.enroll("edge-01", EnvironmentType.REMOTE_LINUX, "Edge host",
               credentials_ref="env:LAB_SSH_KEY_PATH")

    fab = CollectorFabric()
    fab.register(CollectorSpec("sysmon-bridge", "sysmon", "Sysmon",
                               is_configured=lambda: True, signed_source="sysmon"))

    return command_center(
        open_cases=out.incidents, twin_snapshot=out.drift, findings=out.findings,
        situation=out.situation, sensors={"edge-01-agent": "ok"},
        environments=reg, fabric=fab, settings=settings)


# ── composition ────────────────────────────────────────────────────────────────
class TestComposition:
    def test_all_required_panels_present(self):
        cc = _populated_cc()
        assert cc["panel"] == "command_center"
        assert _REQUIRED_PANELS.issubset(set(cc["panels"]))
        assert "situation" in cc and "digest" in cc

    def test_digest_answers_five_questions(self):
        cc = _populated_cc()
        assert set(cc["digest"]) == {"happening", "changed", "matters", "uncertain", "do_next"}


# ── the new V67 sources ──────────────────────────────────────────────────────
class TestNewSources:
    def test_environments_panel_never_leaks_credentials(self):
        reg = EnvironmentRegistry()
        reg.enroll("edge-01", EnvironmentType.REMOTE_LINUX, "Edge",
                   credentials_ref="env:LAB_SSH_KEY_PATH")
        panel = environments_panel(reg)
        for row in panel["environments"]:
            assert "credentials_ref" not in row
        # the presence flag is fine; the reference itself never is
        assert any(r.get("has_credentials") for r in panel["environments"])

    def test_collectors_panel_bounded_and_safe(self):
        fab = CollectorFabric()
        fab.register(CollectorSpec("sysmon-bridge", "sysmon", "Sysmon",
                                   is_configured=lambda: True, signed_source="sysmon"))
        panel = collectors_panel(fab)
        assert panel["panel"] == "collectors"
        assert len(panel["collectors"]) <= 12

    def test_model_runtime_panel_resolves_without_probing(self):
        panel = model_runtime_panel(settings)
        assert panel["panel"] == "model_runtime"
        assert panel["roles"]                       # resolved role→model map
        assert panel["probe"] == "not_checked"       # reachability NOT queried (no I/O)


# ── redaction / bounds (defence in depth) ────────────────────────────────────
class TestRedaction:
    def test_no_forbidden_key_anywhere(self):
        cc = _populated_cc()
        leaked = _deep_keys(cc) & _FORBIDDEN_KEYS
        assert not leaked, f"forbidden keys reached the HUD: {leaked}"


# ── honest empty state ───────────────────────────────────────────────────────
class TestEmptyStateHonesty:
    def test_empty_digest_says_no_evidence_not_secure(self):
        snap = SituationEngine().build()   # nothing observed
        digest = operational_digest(snap)
        do_next = digest["do_next"].lower()
        assert "do not have evidence of an active incident" in do_next
        assert "secure" not in do_next     # unknown != safe — never claim "all secure"
        assert digest["matters"] is None

    def test_empty_command_center_still_well_formed(self):
        snap = SituationEngine().build()
        cc = command_center(situation=snap)
        assert cc["panel"] == "command_center"
        assert _REQUIRED_PANELS.issubset(set(cc["panels"]))


# ── live singleton path is pure/sync (never blocks the loop) ─────────────────
class TestLivePath:
    def test_build_live_command_center_is_sync_and_bounded(self):
        # Callable with no running event loop → it does no awaiting / no I/O.
        cc = build_live_command_center(sensors={"probe": "ok"})
        assert cc["panel"] == "command_center"
        assert _REQUIRED_PANELS.issubset(set(cc["panels"]))
        assert not (_deep_keys(cc) & _FORBIDDEN_KEYS)
