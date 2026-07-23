"""V69 M58.9 — the advisory prompt/prefix-cache runtime-health subsystem."""
from __future__ import annotations

from core.runtime_health import (
    HealthStatus,
    _prompt_cache_subsystem,
    collect_runtime_health,
)


def test_prompt_cache_subsystem_is_registered_and_advisory():
    snap = collect_runtime_health()
    names = [s.name for s in snap.subsystems]
    assert "prompt_cache" in names
    ps = next(s for s in snap.subsystems if s.name == "prompt_cache")
    # advisory: OPTIONAL never degrades the overall verdict
    assert ps.status is HealthStatus.OPTIONAL


def test_prompt_cache_subsystem_covers_every_required_dimension():
    ss = _prompt_cache_subsystem(
        prompt={"core_fingerprint": "abc", "session_fingerprint": "def",
                "contract_schema_version": "m58.1",
                "stable_prefix_estimated_tokens": 90,
                "contract_delta_estimated_tokens": 30,
                "compatibility_identity": "id",
                "size": {"duplicate_sections_removed": 0, "total_tokens": 200,
                         "budget_tokens": 1400}},
        cache={"cache_state": "PREFIX_REUSE_OBSERVED", "invalidations": 1,
               "last_invalidation_reason": "NUM_CTX_CHANGED",
               "recent_prompt_eval_ms": 800.0, "warm_prompt_eval_ms": 700.0,
               "cold_prompt_eval_ms": 4000.0, "observed_reuse_ratio": 0.5},
        prewarm={"mode": "BACKGROUND_FAMILIES", "family_states": {"CONCISE": "READY"},
                 "attempts": 2, "successes": 2, "cancellations": 0,
                 "last_family": "CONCISE", "last_first_token_ms": 120.0,
                 "last_prompt_eval_ms": 200.0, "stale_fingerprints": 0},
        compaction={"scheduled": 1, "completed": 1, "cancelled_for_user": 0,
                    "validation_failures": 0, "context_tokens_saved": 40,
                    "digest_version": 2, "last_duration_ms": 900.0},
        tools={"tool_rounds": 2, "malformed_calls": 0, "denied_calls": 0,
               "final_response_tokens": 96, "tool_schema_fingerprint": "ff",
               "eligible_tool_count": 3, "schema_estimated_tokens": 500},
        barge={"mode": "ACTIVE_CONSOLE_KEY", "supported": True,
               "active_interruptions": 1, "command_interruptions": 0,
               "cancellation_latency_ms": 12.0, "terminal_restore_failures": 0},
        response={"late_chunks_suppressed": 0},
    )
    m = ss.metrics
    # one representative key from each required dimension
    assert m["core_fingerprint"] == "abc"
    assert m["cache_state"] == "PREFIX_REUSE_OBSERVED"
    assert m["prewarm_mode"] == "BACKGROUND_FAMILIES"
    assert m["compaction_completed"] == 1
    assert m["tool_rounds"] == 2
    assert m["barge_in_mode"] == "ACTIVE_CONSOLE_KEY"


def test_prompt_cache_metrics_are_content_free():
    snap = collect_runtime_health()
    ps = next(s for s in snap.subsystems if s.name == "prompt_cache")
    blob = repr(ps.metrics)
    # fingerprints/enums/counts only — never prompt/answer/key content
    assert "local AI assistant" not in blob
    assert "UNTRUSTED" not in blob
    assert "\x1b" not in blob
    assert "[RESPONSE_CONTRACT]" not in blob


def test_empty_inputs_report_no_turn_yet():
    ss = _prompt_cache_subsystem(prompt={}, cache={}, prewarm={}, compaction={},
                                 tools={}, barge={}, response={})
    assert ss.status is HealthStatus.OPTIONAL
    assert "no interactive turn yet" in ss.detail
