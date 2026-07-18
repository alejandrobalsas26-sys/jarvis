"""
tests/test_ollama_posture_v69_m555.py — V69 M55.5 Ollama posture truth.

The live boot printed `OLLAMA CONFIG: OLLAMA_NUM_PARALLEL=1 ... MAX_LOADED_MODELS=1`,
which reads like verified server configuration — but JARVIS never sets those and the
Ollama server is a separate process whose settings the API cannot confirm. These tests
lock the truthful, category-separated posture and the deterministic residency guidance,
and prove nothing is presented as verified or mutated.
"""
from __future__ import annotations

import asyncio
import os

from loguru import logger

from core.model_router import configure_ollama_for_hardware
from core.ollama_env import collect_ollama_env
from core.ollama_native import NativeCapability, NativeProbeState, reset_native_capability


def teardown_function(_):
    reset_native_capability()


def _capture_info(coro_factory) -> str:
    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="INFO")
    try:
        asyncio.run(coro_factory())
    finally:
        logger.remove(sink)
    return "".join(msgs)


class _HW:
    recommended_pools = 1
    is_dual_channel = True


# ── The misleading log is gone; recommendation is clearly advisory ────────────
def test_configure_logs_recommended_not_verified_config():
    out = _capture_info(lambda: configure_ollama_for_hardware(_HW()))
    assert "OLLAMA CONFIG:" not in out               # the verified-looking label is gone
    assert "RECOMMENDED" in out
    assert "advisory" in out.lower()
    # residency recommendation: room for FAST + embedding
    assert "OLLAMA_MAX_LOADED_MODELS=2" in out
    # the process env is explicitly NOT the server's
    assert "NOT the server" in out or "not the server" in out.lower()


# ── The five honest categories, never conflated ───────────────────────────────
def test_posture_report_separates_five_categories(monkeypatch):
    monkeypatch.delenv("OLLAMA_MAX_LOADED_MODELS", raising=False)
    monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)
    cap = NativeCapability(
        state=NativeProbeState.NATIVE_READY, model="qwen3:8b", server_version="0.32.0",
        active_models=("qwen3:8b",), think_false_accepted=True, reasoning_omitted=True)
    r = collect_ollama_env(capability=cap).posture_report()
    assert {"recommended", "jarvis_process_env", "server_observed",
            "server_settings_verified", "unknown"} <= set(r)
    assert r["server_settings_verified"] is False               # the API cannot verify
    assert r["jarvis_process_env"]["OLLAMA_MAX_LOADED_MODELS"] == "unset"
    assert r["server_observed"]["observed_loaded_models"] == 1
    assert r["server_observed"]["active_models"] == ["qwen3:8b"]
    assert any("MAX_LOADED_MODELS(server)" in u for u in r["unknown"])


def test_loaded_model_count_never_proves_max_loaded(monkeypatch):
    monkeypatch.delenv("OLLAMA_MAX_LOADED_MODELS", raising=False)
    # One observed resident model must NOT be inferred as MAX_LOADED_MODELS=1.
    cap = NativeCapability(state=NativeProbeState.NATIVE_READY, active_models=("qwen3:8b",))
    t = collect_ollama_env(capability=cap)
    assert t.observed_loaded_models == 1
    assert t.settings_verified is False
    assert t.max_loaded_applied() == "not-applied"              # env unset, not "1 => applied"


def test_env_set_is_configured_but_unverified(monkeypatch):
    monkeypatch.setenv("OLLAMA_MAX_LOADED_MODELS", "2")
    t = collect_ollama_env()
    assert t.max_loaded_applied() == "configured-but-unverified"
    assert t.settings_verified is False                          # still never verified


# ── Residency guidance: deterministic, no unmeasured claim ────────────────────
def test_residency_guidance_is_deterministic_and_makes_no_unmeasured_claim(monkeypatch):
    monkeypatch.delenv("OLLAMA_MAX_LOADED_MODELS", raising=False)
    cap = NativeCapability(state=NativeProbeState.NATIVE_READY,
                           active_models=("nomic-embed-text:latest",))
    t = collect_ollama_env(capability=cap)
    g1 = t.residency_guidance()
    g2 = t.residency_guidance()
    assert g1 == g2                                              # deterministic
    assert "restart the Ollama service" in g1
    assert "OLLAMA_MAX_LOADED_MODELS=2" in g1
    assert "measurement" in g1.lower()                          # no unmeasured savings claim
    assert "EVICT FAST" in g1


def test_posture_and_guidance_never_mutate_environment(monkeypatch):
    monkeypatch.delenv("OLLAMA_MAX_LOADED_MODELS", raising=False)
    before = dict(os.environ)
    cap = NativeCapability(state=NativeProbeState.NATIVE_READY, active_models=("qwen3:8b",))
    t = collect_ollama_env(capability=cap)
    _ = t.posture_report()
    _ = t.residency_guidance()
    _ = t.snapshot()
    assert dict(os.environ) == before                           # no persistent env mutation
