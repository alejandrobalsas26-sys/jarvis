"""core/ollama_env.py — V69 M55.8: truthful Ollama environment state.

Startup logs intended values like ``OLLAMA_MAX_LOADED_MODELS=1`` (via
``model_router.configure_ollama_for_hardware``), but those are only LOGGED
RECOMMENDATIONS — JARVIS never sets them, and even if it did, the Ollama server is
a SEPARATE process that read its own environment when it launched. So a printed
value proves nothing about the running server.

This module separates the four honest categories and refuses to conflate them:

  configured_by_jarvis   what JARVIS's hardware profile RECOMMENDS (advisory only)
  process_environment    OLLAMA_* vars present in THIS python process (still not the
                         server's, which is a different process)
  server_observed        what the server's API actually reveals — version and the
                         count of currently-resident models (/api/ps), captured by
                         the cached capability probe
  unknown                the server's real OLLAMA_NUM_PARALLEL / MAX_LOADED_MODELS —
                         the Ollama API does NOT expose them, so we say so

It NEVER claims ``settings_verified`` (the API cannot confirm the server's config)
and NEVER restarts or kills the server — it only produces operator guidance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _process_env() -> dict:
    """OLLAMA_* variables visible in THIS process (not proof of the server's)."""
    keys = ("OLLAMA_HOST", "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS",
            "OLLAMA_KEEP_ALIVE")
    return {k: os.environ.get(k) for k in keys}


def _jarvis_recommendation() -> dict:
    """What JARVIS's hardware profile RECOMMENDS (advisory; not applied to the
    server). Mirrors model_router.configure_ollama_for_hardware's computation."""
    out = {"num_parallel": None, "max_loaded_models": None, "keep_alive": None}
    try:
        from core.hardware_profile import get_cached_profile
        hw = get_cached_profile()
        if hw is not None:
            parallel = getattr(hw, "recommended_pools", getattr(hw, "pools", 1))
            out["num_parallel"] = parallel
            out["max_loaded_models"] = parallel
            out["keep_alive"] = "30m" if getattr(hw, "is_dual_channel", False) else "10m"
    except Exception:  # noqa: BLE001
        pass
    return out


@dataclass
class OllamaEnvTruth:
    """A truthful, bounded view of the Ollama environment state."""

    process_environment: dict = field(default_factory=dict)
    configured_by_jarvis: dict = field(default_factory=dict)
    server_version: str | None = None
    observed_loaded_models: int | None = None
    active_models: tuple[str, ...] = ()
    capability_state: str = "UNKNOWN"
    think_false_supported: bool | None = None
    active_transport: str = "auto"
    fast_model: str = ""
    # The API cannot confirm the server's real parallel/max-loaded config.
    settings_verified: bool = False

    def max_loaded_applied(self) -> str:
        """Honest verdict on OLLAMA_MAX_LOADED_MODELS: 'configured-but-unverified'
        when set in THIS process, else 'not-applied' — never 'active'."""
        if self.process_environment.get("OLLAMA_MAX_LOADED_MODELS"):
            return "configured-but-unverified"
        return "not-applied"

    def guidance(self) -> str:
        """Operator guidance only — JARVIS never restarts the server itself."""
        if self.max_loaded_applied() == "not-applied":
            return (
                "OLLAMA_MAX_LOADED_MODELS is not set in the Ollama server's "
                "environment. To pin a single model in RAM on this CPU host, set "
                "OLLAMA_MAX_LOADED_MODELS=1 (and OLLAMA_NUM_PARALLEL=1) in the "
                "Ollama SERVER's environment and restart the Ollama service."
            )
        return (
            "OLLAMA_MAX_LOADED_MODELS is present in this process, but the API "
            "cannot confirm the running server uses it (the server is a separate "
            "process). Verify via the Ollama service configuration."
        )

    def snapshot(self) -> dict:
        return {
            "process_environment": self.process_environment,
            "configured_by_jarvis": self.configured_by_jarvis,
            "server_version": self.server_version,
            "observed_loaded_models": self.observed_loaded_models,
            "active_models": list(self.active_models),
            "capability_probe_state": self.capability_state,
            "think_false_supported": self.think_false_supported,
            "active_transport": self.active_transport,
            "fast_model": self.fast_model,
            "settings_verified": self.settings_verified,
            "max_loaded_applied": self.max_loaded_applied(),
            "guidance": self.guidance(),
        }

    def summary(self) -> str:
        """A compact ASCII one-liner (Windows/TTS-safe)."""
        return (
            "OLLAMA SERVER: transport={} fast_model={} think_false_supported={} "
            "server_version={} loaded_models={} max_loaded_models={}".format(
                self.active_transport, self.fast_model or "?",
                self.think_false_supported, self.server_version or "?",
                self.observed_loaded_models if self.observed_loaded_models is not None else "?",
                self.max_loaded_applied(),
            )
        )


def collect_ollama_env(*, capability=None) -> OllamaEnvTruth:
    """Compose the truthful env view from the CACHED capability (no live probe —
    safe to call from the non-blocking runtime-health path). Pass ``capability`` to
    inject; otherwise the process-global cached probe result is used."""
    if capability is None:
        try:
            from core.ollama_native import get_native_capability
            capability = get_native_capability()
        except Exception:  # noqa: BLE001
            capability = None

    truth = OllamaEnvTruth(
        process_environment=_process_env(),
        configured_by_jarvis=_jarvis_recommendation(),
    )
    if capability is not None:
        state = getattr(capability, "state", None)
        truth.capability_state = getattr(state, "value", str(state)) if state else "UNKNOWN"
        truth.server_version = getattr(capability, "server_version", None)
        active = tuple(getattr(capability, "active_models", ()) or ())
        truth.active_models = active
        truth.observed_loaded_models = len(active) if active else None
        truth.think_false_supported = getattr(capability, "think_false_supported", None)
        truth.fast_model = getattr(capability, "model", "") or ""
    try:
        from core.config import settings
        truth.active_transport = getattr(settings, "fast_transport", "auto")
        if not truth.fast_model:
            truth.fast_model = getattr(settings, "fast_model", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return truth
