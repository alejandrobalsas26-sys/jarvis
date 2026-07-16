"""tests/test_fast_readiness_env_v69_m55.py — V69 M55.8/.10/.13.

Proves the additive FastReadiness transport/latency observability, the truthful
Ollama-environment view, and their integration into runtime health — all bounded,
all honest (never claiming the server's config is verified when the API cannot
confirm it).
"""
from __future__ import annotations

from core.fast_readiness import FastReadiness, reset_fast_readiness
from core.ollama_env import collect_ollama_env
from core.ollama_native import NativeCapability, NativeProbeState


# ── M55.10 FastReadiness extension ────────────────────────────────────────────
def test_snapshot_keeps_existing_keys_and_adds_transport():
    fr = FastReadiness(model="qwen3:8b")
    snap = fr.snapshot()
    # existing keys preserved
    for k in ("state", "model", "last_probe_ms", "last_success_at", "last_error",
              "accepts_input"):
        assert k in snap
    # new additive keys
    for k in ("transport", "think_supported", "native_state", "server_version"):
        assert k in snap


def test_note_capability_folds_native_state():
    fr = FastReadiness(model="qwen3:8b")
    cap = NativeCapability(state=NativeProbeState.NATIVE_READY, model="qwen3:8b",
                           think_false_accepted=True, reasoning_omitted=True,
                           server_version="0.32.0")
    fr.note_capability(cap)
    assert fr.native_state == "NATIVE_READY"
    assert fr.think_supported is True
    assert fr.server_version == "0.32.0"


def test_fast_inference_stats_are_bounded_and_counted():
    fr = FastReadiness(model="qwen3:8b")
    for i in range(30):  # exceed the 20-sample window
        fr.record_fast_turn(first_token_ms=1000 + i, total_ms=5000 + i,
                            tokens_per_second=5.0, transport="native", think=False)
    fr.note_timeout("first_token")
    fr.note_cancellation()
    fr.note_native_fallback()
    s = fr.fast_inference_snapshot()
    assert s["successes"] == 30
    assert s["timeouts"] == 1
    assert s["cancellations"] == 1
    assert s["native_fallbacks"] == 1
    assert s["requests"] == 32          # successes + timeouts + cancellations
    assert s["active_transport"] == "native"
    assert s["think_requested"] is False
    assert s["generation_cap"] == 256
    assert s["average_first_token_ms"] is not None
    assert s["p50_first_token_ms"] is not None
    assert s["post_cancel_busy_ms"] is None   # never invented


def test_fast_inference_dormant_before_any_turn():
    reset_fast_readiness(FastReadiness(model="qwen3:8b"))
    from core.runtime_health import _fast_inference_subsystem
    sub = _fast_inference_subsystem()
    assert sub.status.value.lower() == "dormant"


# ── M55.8 Ollama environment truth ────────────────────────────────────────────
def test_env_not_applied_when_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_MAX_LOADED_MODELS", raising=False)
    t = collect_ollama_env()
    assert t.max_loaded_applied() == "not-applied"
    assert t.settings_verified is False
    assert "restart the Ollama service" in t.guidance()


def test_env_configured_but_unverified_when_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_MAX_LOADED_MODELS", "1")
    t = collect_ollama_env()
    assert t.max_loaded_applied() == "configured-but-unverified"
    # Even when set in THIS process, the API cannot confirm the server uses it.
    assert t.settings_verified is False


def test_env_folds_cached_capability():
    cap = NativeCapability(state=NativeProbeState.NATIVE_READY, model="qwen3:8b",
                           think_false_accepted=True, reasoning_omitted=True,
                           server_version="0.32.0", active_models=("qwen3:8b",))
    t = collect_ollama_env(capability=cap)
    assert t.server_version == "0.32.0"
    assert t.observed_loaded_models == 1
    assert t.think_false_supported is True
    assert t.capability_state == "NATIVE_READY"
    snap = t.snapshot()
    assert snap["capability_probe_state"] == "NATIVE_READY"
    assert snap["max_loaded_applied"] in ("not-applied", "configured-but-unverified")


# ── M55.13 runtime health integration ─────────────────────────────────────────
def test_runtime_health_includes_new_subsystems_and_stays_healthy():
    from core.runtime_health import build_live_runtime_health
    h = build_live_runtime_health()
    names = {s["name"] for s in h["subsystems"]}
    assert "fast_inference" in names
    assert "ollama_env" in names
    # ollama_env is advisory — it never degrades the overall verdict.
    oe = next(s for s in h["subsystems"] if s["name"] == "ollama_env")
    assert oe["healthy"] is True
