"""tests/test_residency_integration_v69_m568.py — V69 M56.8 boot, health and UX.

Locks the integration invariants: runtime health EXTENDS its one surface (advisory,
never degrading the verdict), the operator panel never claims verification it does not
have, TEXT_READY stays independent of model warmth, and the boot/command wiring is
present and read-only.
"""
from __future__ import annotations

import ast
import pathlib

from core.fast_readiness import WARMING_STATES, FastReadiness, FastState
from core.residency_status import ResidencyStatusView, build_status_view
from core.runtime_health import _residency_subsystem, collect_runtime_health

FAST = "qwen3:8b"
EMBED = "nomic-embed-text:latest"
MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


# ── runtime health ───────────────────────────────────────────────────────────
def test_residency_subsystem_exposes_all_four_metric_families():
    sub = _residency_subsystem(
        residency={"residency_state": "FAST_EVICTED", "observed_models": [EMBED],
                   "preferred_models": [FAST, EMBED], "fast_evictions": 2,
                   "embedding_evictions": 0, "restoration_attempts": 1,
                   "restoration_successes": 1, "last_switch_reason": "embedding_request",
                   "last_observation_at": 1000.0},
        governor={"active_role": "fast", "active_priority": "INTERACTIVE",
                  "queue_depth": 1, "queue_capacity": 64, "high_watermark": 3,
                  "average_wait_ms": 120.0, "background_deferrals": 4,
                  "cancellations": 1, "starvation_preventions": 2},
        prewarm={"mode": "BACKGROUND", "state": "READY", "model": FAST, "attempts": 1,
                 "successes": 1, "failures": 0, "cancellations": 0,
                 "last_load_ms": 9000.0, "last_first_token_ms": 1200.0,
                 "last_total_ms": 1400.0, "last_failure_reason": None},
        power={"profile": "BATTERY_SAVER", "source": "BATTERY", "detected_at": 1.0,
               "override": None})
    m = sub.metrics
    for key in ("residency_state", "fast_evictions", "restoration_successes",
                "last_switch_reason", "queue_depth", "queue_capacity",
                "high_watermark", "average_wait_ms", "background_deferrals",
                "starvation_preventions", "prewarm_mode", "prewarm_state",
                "prewarm_attempts", "prewarm_last_load_ms",
                "prewarm_last_first_token_ms", "prewarm_last_failure_reason",
                "power_profile", "power_source", "power_detected_at",
                "power_override"):
        assert key in m, f"missing metric {key}"
    assert m["residency_state"] == "FAST_EVICTED"
    assert m["power_profile"] == "BATTERY_SAVER"


def test_residency_metrics_carry_no_prompts_or_content():
    sub = _residency_subsystem(residency={"residency_state": "UNKNOWN"}, governor={},
                               prewarm={}, power={})
    flat = repr(sub.metrics).lower()
    for forbidden in ("prompt", "hola", "message", "vector", "embedding_text"):
        assert forbidden not in flat


def test_evicted_fast_is_advisory_and_never_degrades_the_verdict():
    """An evicted model or a battery-disabled prewarm is a PERFORMANCE fact, not a
    runtime fault."""
    sub = _residency_subsystem(
        residency={"residency_state": "FAST_EVICTED", "fast_evictions": 5},
        governor={}, prewarm={"state": "FAILED"}, power={"profile": "BATTERY_SAVER"})
    assert sub.status.value.lower() in ("optional", "degraded_optional", "ok", "healthy")
    from core.runtime_health import _STATUS_RANK
    assert _STATUS_RANK.get(sub.status, 0) == 0, "residency must stay rank 0 (advisory)"


def test_residency_subsystem_is_optional_when_nothing_is_available():
    sub = _residency_subsystem(residency={}, governor={}, prewarm={}, power={})
    assert sub.metrics == {}
    assert "not available" in sub.detail


def test_health_snapshot_includes_residency_and_stays_one_surface():
    snap = collect_runtime_health()
    names = [s.name for s in snap.subsystems]
    assert "residency" in names
    assert names.count("residency") == 1, "no second health system"
    # The pre-M56 subsystems are all still present.
    for legacy in ("fast_inference", "ollama_env", "model_runtime", "interactive"):
        assert legacy in names


# ── operator panel ───────────────────────────────────────────────────────────
def test_panel_never_claims_verification_it_does_not_have():
    view = ResidencyStatusView(server_reachable=True, server_version="0.12.0",
                               settings_verified=False, fast_model=FAST,
                               embedding_model=EMBED)
    text = view.render()
    assert "settings_verified=false" in text
    assert "OLLAMA / MODEL RESIDENCY" in text
    assert text.isascii()


def test_panel_reports_loaded_and_not_loaded_per_model():
    view = ResidencyStatusView(fast_model=FAST, embedding_model=EMBED,
                               observed_models=(FAST,))
    text = view.render()
    assert f"{FAST} loaded" in text
    assert f"{EMBED} not loaded" in text


def test_panel_is_tag_tolerant_about_residency():
    view = ResidencyStatusView(embedding_model="nomic-embed-text:latest",
                               observed_models=("nomic-embed-text",))
    assert "nomic-embed-text:latest loaded" in view.render()


def test_panel_states_restart_requirement_and_no_changes_applied():
    view = ResidencyStatusView(restart_required=True, changes_applied=False,
                               recommended={"OLLAMA_NUM_PARALLEL": "1",
                                            "OLLAMA_MAX_LOADED_MODELS": "2"})
    text = view.render()
    assert "server restart required to apply: true" in text
    assert "changes applied: false" in text
    assert "dry-run available" in text


def test_panel_summary_is_one_compact_line():
    view = ResidencyStatusView(fast_model=FAST, embedding_model=EMBED,
                               residency_state="DUAL_RESIDENT_OBSERVED",
                               power_profile="AC_PERFORMANCE")
    s = view.summary()
    assert "\n" not in s and s.isascii()
    assert "OLLAMA RESIDENCY:" in s
    assert len(s) < 200, "ordinary startup must not print a wall of diagnostics"


def test_build_status_view_is_read_only_and_never_raises():
    view = build_status_view()
    assert isinstance(view, ResidencyStatusView)
    text = view.render()
    assert "OLLAMA / MODEL RESIDENCY" in text
    # It is honest by default: verification is False unless genuinely established.
    assert isinstance(view.settings_verified, bool)


def test_status_view_notes_a_battery_disabled_prewarm():
    view = build_status_view(
        env=None, process_truth=None, residency=None, prewarm=None,
        power={"profile": "BATTERY_SAVER", "source": "BATTERY",
               "policy": {"background_prewarm_allowed": False}})
    assert any("background prewarm disabled" in n for n in view.notes)


# ── TEXT_READY independence ──────────────────────────────────────────────────
def test_every_warming_state_still_accepts_input():
    for state in WARMING_STATES:
        fast = FastReadiness(model=FAST)
        fast._state = state
        assert fast.accepts_input() is True, f"{state} must not close the prompt"


def test_warming_states_are_distinct_and_each_has_a_hint():
    assert FastState.MODEL_LOADING in WARMING_STATES
    assert FastState.PREWARMING in WARMING_STATES
    hints = set()
    for state in WARMING_STATES:
        fast = FastReadiness(model=FAST)
        fast._state = state
        hint = fast.warming_hint()
        assert hint, f"{state} must explain itself to the operator"
        hints.add(hint)
    assert len(hints) == len(WARMING_STATES), "each wait explains itself differently"


def test_readiness_is_not_ready_merely_because_a_model_name_exists():
    fast = FastReadiness(model=FAST)
    assert fast.state is FastState.CONFIGURED
    assert fast.state is not FastState.READY


# ── boot / command wiring (static, no boot required) ─────────────────────────
def _main_source() -> str:
    return MAIN.read_text(encoding="utf-8")


def test_main_parses_and_wires_the_prewarm():
    src = _main_source()
    ast.parse(src)                        # main.py must remain syntactically valid
    assert "from core.fast_prewarm import" in src
    assert "run_before_text_ready" in src
    assert "note_prewarm_started" in src
    assert "note_prewarm_result" in src


def test_main_respects_the_power_policy_before_prewarming():
    src = _main_source()
    assert "background_prewarm_allowed" in src
    assert "PREWARM: skipped by power policy" in src


def test_main_stops_residency_work_during_shutdown():
    src = _main_source()
    assert "get_fast_prewarm().cancel()" in src
    assert "get_governor().close()" in src


def test_main_command_surface_refuses_effectful_posture_actions():
    src = _main_source()
    assert "_handle_residency_command" in src
    assert "EFFECTFUL_ACTIONS" in src
    assert "requires explicit " in src
    # The interactive surface must never mint an authorization.
    assert "OperatorAuthorization(" not in src


def test_main_prints_one_compact_residency_line_at_boot():
    src = _main_source()
    assert "render_summary" in src
    assert "render_status()" not in src.split("_handle_residency_command")[0], (
        "the full diagnostics panel must be operator-requested, not printed at boot")
