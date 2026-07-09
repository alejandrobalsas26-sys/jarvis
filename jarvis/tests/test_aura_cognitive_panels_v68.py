"""tests/test_aura_cognitive_panels_v68.py — V68 M37 cognitive command center dispatch.

Proves the new read-only AURA panels are wired, allowlisted, bounded and non-mutating:
  * each V68 panel command is in the HUD allowlist and none is high/medium-risk (they take
    no world-effect, so they must dispatch without an approval gate);
  * _dispatch_hud_command returns a well-formed bounded dict for each panel over the empty
    live singletons (no crash on cold state);
  * decision_support is advisory ONLY — it ranks operator-supplied options and its result
    always declares auto_execute=False / operator_action_required=True, and malformed
    options are skipped, never run;
  * cognitive_synthesis degrades to the deterministic grounded answer when no model is
    reachable (never blocks, never errors).

Pure: no websocket, no network; dispatch is called directly with a dummy executor.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from aura import server

V68_PANELS = ["collector_telemetry", "sensor_intel", "causal_timeline",
              "operational_state_health", "cognitive_synthesis", "decision_support"]


def _dispatch(cmd, args=None):
    return asyncio.run(server._dispatch_hud_command(cmd, args or {}, executor=None,
                                                    broadcast_fn=None))


class TestAllowlisting:
    def test_all_panels_allowlisted(self):
        for cmd in V68_PANELS:
            assert cmd in server._HUD_ALLOWED_COMMANDS

    def test_panels_are_not_risk_gated(self):
        # read-only panels must never require HITL/OTP or confirmation
        for cmd in V68_PANELS:
            assert cmd not in server._HIGH_RISK_HUD
            assert cmd not in server._MEDIUM_RISK_HUD


class TestDispatchShapes:
    def test_telemetry_shape(self):
        r = _dispatch("collector_telemetry")
        assert r["panel"] == "collector_telemetry" and isinstance(r["collectors"], dict)

    def test_sensor_intel_shape(self):
        r = _dispatch("sensor_intel")
        assert r["panel"] == "sensor_intel" and "coverage_ratio" in r

    def test_causal_timeline_shape(self):
        r = _dispatch("causal_timeline")
        assert r["panel"] == "causal_timeline" and "entries" in r and "counts" in r

    def test_state_health_shape(self):
        r = _dispatch("operational_state_health")
        assert r["panel"] == "operational_state_health" and "durable" in r


class TestDecisionSupportAdvisoryOnly:
    def test_ranks_supplied_options_and_never_executes(self):
        r = _dispatch("decision_support", {"options": [
            {"option_id": "a", "title": "safe diagnostic", "risk": "low", "impact": "low",
             "reversibility": "high", "info_gain": "high", "uncertainty_reduction": "high"},
            {"option_id": "b", "title": "risky remediation", "risk": "high", "impact": "high",
             "reversibility": "low", "info_gain": "low", "uncertainty_reduction": "med"},
        ]})
        assert r["panel"] == "decision_support"
        assert r["auto_execute"] is False and r["operator_action_required"] is True
        assert r["options"][0]["option_id"] == "a"        # safe option ranked first

    def test_malformed_options_skipped_not_run(self):
        r = _dispatch("decision_support", {"options": ["not-a-dict", 42, None]})
        assert r["options"] == [] and r["auto_execute"] is False


class TestCognitiveSynthesisDegrades:
    def test_synthesis_degrades_without_model(self, monkeypatch):
        # Pin the "no model reachable" path deterministically (the dev host may run
        # Ollama): the panel must return the grounded deterministic answer, not block.
        import core.cognitive_synthesis as cs
        monkeypatch.setattr(cs, "_live_synthesizer", lambda: None)
        r = _dispatch("cognitive_synthesis", {"question": "what is happening?"})
        assert r["panel"] == "cognitive_synthesis"
        assert r["grounded"] is True
        assert r["source"] == "deterministic"
