"""core/residency_status.py — V69 M56.8: the operator's residency status view.

ONE compact, honest panel instead of a wall of internal diagnostics. Boot prints the
short form; the operator asks for the full one. Every line distinguishes what is
RECOMMENDED, what is OBSERVED and what is VERIFIED, and the panel refuses to say
"verified" unless a verification actually succeeded against the server process.

Pure rendering over already-collected snapshots — it performs no probe, no scan and
no mutation of its own, so it is safe to call from any surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _yes(value) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


@dataclass
class ResidencyStatusView:
    """The data the panel renders. Every field is optional and defaults to unknown."""

    server_reachable: bool | None = None
    server_version: str | None = None
    settings_verified: bool = False
    launch_mode: str = "UNKNOWN"
    server_pid: int | None = None
    observed_models: tuple[str, ...] = ()
    fast_model: str = ""
    embedding_model: str = ""
    residency_state: str = "UNKNOWN"
    prewarm_mode: str = "BACKGROUND"
    prewarm_state: str = "IDLE"
    fast_readiness: str = "CONFIGURED"
    power_profile: str = "UNKNOWN"
    recommended: dict = field(default_factory=dict)
    restart_required: bool = True
    changes_applied: bool = False
    notes: tuple[str, ...] = ()

    def _model_line(self, model: str) -> str:
        from core.residency import model_matches

        if not model:
            return "not configured"
        loaded = any(model_matches(n, model) for n in self.observed_models)
        return f"{model} {'loaded' if loaded else 'not loaded'}"

    def render(self) -> str:
        """The full operator panel. ASCII, compact, and honest about every claim."""
        rec = self.recommended or {}
        rec_line = " ".join(f"{k}={v}" for k, v in rec.items()) or "unavailable"
        lines = [
            "OLLAMA / MODEL RESIDENCY",
            "",
            "Server:",
            f"  reachable={_yes(self.server_reachable)}",
            f"  version={self.server_version or 'unknown'}",
            f"  launch={self.launch_mode} pid={self.server_pid if self.server_pid else '?'}",
            # The line M55 could never truthfully print, and M56 still refuses to fake.
            f"  settings_verified={str(self.settings_verified).lower()}",
            "",
            "Observed:",
            f"  FAST      {self._model_line(self.fast_model)}",
            f"  EMBEDDING {self._model_line(self.embedding_model)}",
            "",
            "Preferred:",
            "  FAST + EMBEDDING resident together",
            "",
            f"Residency:  {self.residency_state}",
            f"Prewarm:    {self.prewarm_mode} / {self.prewarm_state}",
            f"FAST:       {self.fast_readiness}",
            f"Power:      {self.power_profile}",
            "",
            "Recommendation:",
            f"  {rec_line}",
            f"  server restart required to apply: {str(self.restart_required).lower()}",
            f"  changes applied: {str(self.changes_applied).lower()}",
            "  dry-run available: ollama-posture-dry-run",
        ]
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)

    def summary(self) -> str:
        """The one-line form for ordinary startup — no wall of diagnostics."""
        return (
            "OLLAMA RESIDENCY: fast={} embedding={} residency={} prewarm={}/{} "
            "power={} settings_verified={}".format(
                self.fast_model or "?", self.embedding_model or "?",
                self.residency_state, self.prewarm_mode, self.prewarm_state,
                self.power_profile, str(self.settings_verified).lower(),
            )
        )


def build_status_view(*, env=None, process_truth=None, residency=None, prewarm=None,
                      power=None, fast_readiness=None) -> ResidencyStatusView:
    """Compose the view from live sources (each injectable, each guarded).

    Never probes: it reads the cached capability, the cached process truth and the
    in-memory metrics, so calling it costs nothing on the interactive path.
    """
    view = ResidencyStatusView()

    # ── Ollama env / capability (M55.5 + M56.1) ──
    try:
        if env is None:
            from core.ollama_env import collect_ollama_env
            env = collect_ollama_env()
        view.server_version = getattr(env, "server_version", None)
        view.server_reachable = bool(view.server_version) or bool(
            getattr(env, "active_models", ()))
        view.observed_models = tuple(getattr(env, "active_models", ()) or ())
        view.fast_model = getattr(env, "fast_model", "") or ""
        rec = getattr(env, "configured_by_jarvis", {}) or {}
        view.recommended = {
            "OLLAMA_NUM_PARALLEL": rec.get("num_parallel"),
            "OLLAMA_MAX_LOADED_MODELS": rec.get("max_loaded_models"),
            "OLLAMA_KEEP_ALIVE": rec.get("keep_alive"),
        }
    except Exception:  # noqa: BLE001
        pass

    # ── process discovery (M56.1) — the ONLY source that may set settings_verified ──
    try:
        if process_truth is None:
            from core.ollama_process import get_process_truth
            process_truth = get_process_truth()
        view.launch_mode = getattr(getattr(process_truth, "launch_mode", None),
                                   "value", "UNKNOWN")
        view.server_pid = getattr(process_truth, "primary_pid", None)
        view.restart_required = bool(process_truth.restart_required())
        # Verified means: the server process's OWN environment block was read. A
        # matching Windows value is never enough (M56.1).
        view.settings_verified = bool(getattr(process_truth, "server_env_readable", False))
        if not view.settings_verified:
            view.notes += ("server environment unreadable; posture is not verified",)
    except Exception:  # noqa: BLE001
        pass

    # ── residency observations (M56.3) ──
    try:
        if residency is None:
            from core.residency import get_residency_metrics
            residency = get_residency_metrics().snapshot()
        view.residency_state = residency.get("residency_state", "UNKNOWN")
        if residency.get("observed_models"):
            view.observed_models = tuple(residency["observed_models"])
        preferred = residency.get("preferred_models") or []
        if len(preferred) >= 2:
            view.fast_model = view.fast_model or preferred[0]
            view.embedding_model = preferred[1]
    except Exception:  # noqa: BLE001
        pass
    if not view.embedding_model:
        try:
            from core.model_router import resolve_embedding_model
            view.embedding_model = resolve_embedding_model()
        except Exception:  # noqa: BLE001
            pass

    # ── prewarm (M56.4) ──
    try:
        if prewarm is None:
            from core.fast_prewarm import get_fast_prewarm
            prewarm = get_fast_prewarm().snapshot()
        view.prewarm_mode = prewarm.get("mode", "BACKGROUND")
        view.prewarm_state = prewarm.get("state", "IDLE")
    except Exception:  # noqa: BLE001
        pass

    # ── power profile (M56.6) ──
    try:
        if power is None:
            from core.runtime_profile import get_runtime_profile
            power = get_runtime_profile().detect().snapshot()
        view.power_profile = power.get("profile", "UNKNOWN")
        policy = power.get("policy") or {}
        if policy.get("background_prewarm_allowed") is False:
            view.notes += ("background prewarm disabled by the current power profile",)
    except Exception:  # noqa: BLE001
        pass

    # ── FAST readiness (M55.3 + M56.4) ──
    try:
        if fast_readiness is None:
            from core.fast_readiness import get_fast_readiness
            fast_readiness = get_fast_readiness().snapshot()
        view.fast_readiness = fast_readiness.get("state", "CONFIGURED")
        view.fast_model = view.fast_model or fast_readiness.get("model", "")
    except Exception:  # noqa: BLE001
        pass
    return view


def render_status() -> str:
    """The full operator panel from live sources."""
    return build_status_view().render()


def render_summary() -> str:
    """The one-line startup form."""
    return build_status_view().summary()
