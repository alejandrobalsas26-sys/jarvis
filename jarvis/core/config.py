"""
core/config.py — Single source of truth for all JARVIS configuration.

All environment variables pass through here — never os.getenv() directly in
other modules.  Pydantic BaseSettings validates types at startup.
"""

import re
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = str(Path(__file__).parent.parent / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", _ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Persona ───────────────────────────────────────────────────────────────
    assistant_name: str = "Alicia"
    user_name:      str = "Alejandro"
    city:           str = "Panama"

    # ── LLM (Ollama local) ────────────────────────────────────────────────────
    llm_model:      str = "qwen2.5-coder"
    llm_max_tokens: int = 2048

    # ── Whisper STT ───────────────────────────────────────────────────────────
    whisper_model:    str = "small"
    whisper_language: str = "es"
    record_seconds:   int = 5
    sample_rate:      int = 16000

    # ── VMware / Forensics ────────────────────────────────────────────────────
    # Path to the .vmx file used for live forensic capture (canary trigger).
    vmx_target_path: str = ""
    # Path to the vmrun.exe binary (used by forensic_volatility, resource_sentinel).
    vmrun_path: str = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
    # Comma-separated list of secondary .vmx paths to suspend on resource pressure.
    secondary_vms: str = ""

    # ── Zeek DPI ──────────────────────────────────────────────────────────────
    zeek_log_dir:              str = "/mnt/zeek/logs/current"
    dns_query_len_threshold:   int = 52
    dns_query_rate_threshold:  int = 100

    # ── Environmental Intel ───────────────────────────────────────────────────
    default_lat:         float = 9.3592    # Colón, Panama
    default_lon:         float = -79.9014
    env_poll_interval:   int   = 900       # seconds (15 min floor — rate-limit OPSEC)

    # ── Threat Feed ───────────────────────────────────────────────────────────
    threat_feed_sync_interval: int = 86400  # 24 h

    # ── Resource Sentinel ─────────────────────────────────────────────────────
    ram_free_floor:    float = 8.0    # % free RAM threshold
    cpu_temp_ceil:     float = 85.0   # °C threshold
    suspend_cooldown:  int   = 120    # seconds — hysteresis to prevent flapping

    # ── Mitigation (SOAR) ────────────────────────────────────────────────────
    entropy_threshold: float = 5.0    # AND-gate threshold for IP isolation

    # ── Agentic Loop ─────────────────────────────────────────────────────────
    agentic_max_cycles:   int = 8
    agentic_loop_timeout: int = 120   # seconds

    # ── Claude / Anthropic (optional — deep reasoning backend) ───────────────
    anthropic_api_key: str = ""

    # ── OpenRouter (optional — cloud model fallback, OpenAI-compatible) ──────
    openrouter_api_key:  str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model:    str = "anthropic/claude-sonnet-4-6"

    # ── Model overrides (override auto-detected fast/deep models) ────────────
    model_fast_override: str = ""
    model_deep_override: str = ""

    # ── Embedding runtime (V69 M52 — unified semantic embedding) ──────────────
    # The configured EMBEDDING role (core.model_router, JARVIS_MODEL_EMBEDDING →
    # nomic-embed-text via Ollama) is ALWAYS the primary provider. These knobs
    # tune the ONE runtime every semantic consumer resolves through. Operator-only
    # (env/.env), never sourced from LLM/tool input.
    #   embedding_fallback_enabled : opt IN to the sentence-transformers /
    #       all-MiniLM-L6-v2 fallback. Off by default → no silent provider switch
    #       and no torch import unless the operator asks for it. The fallback
    #       carries a distinct fingerprint/dimension, so callers are always told
    #       which provider is active.
    #   embedding_timeout_s  : hard per-call timeout for a provider embed.
    #   embedding_batch_size : bounded batch size (CPU-only host discipline).
    embedding_fallback_enabled: bool = False
    embedding_timeout_s:        float = 30.0
    embedding_batch_size:       int = 16

    @field_validator("embedding_fallback_enabled", mode="before")
    @classmethod
    def _coerce_embedding_fallback(cls, v) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    @field_validator("embedding_batch_size")
    @classmethod
    def validate_embedding_batch_size(cls, v: int) -> int:
        if not 1 <= v <= 128:
            raise ValueError("embedding_batch_size must be between 1 and 128.")
        return v

    # ── Filesystem watch policy (V69 M54.1.3) ────────────────────────────────
    # The live boot flooded the console with QueueFull tracebacks because the YARA
    # watcher hardcoded `~/Downloads` recursive — which CONTAINS this repo — with a
    # 100-slot queue and no dedup, so JARVIS scanning its own writes saturated it.
    # These make the roots and the noise policy operator-configurable instead:
    #   watch_include      : extra roots to observe (os.pathsep/','-separated)
    #   watch_exclude      : extra directory NAMES or absolute paths to ignore
    #   watch_queue_size   : bounded event queue capacity
    #   watch_debounce_ms  : window in which repeats of one path coalesce
    #   watch_security_root: observe ~/Downloads for executables (SECURITY_SCAN)
    # Bounds are clamped, never raised: an operator typo must not create an
    # unbounded queue or disable debouncing entirely.
    watch_include:       str = ""
    watch_exclude:       str = ""
    watch_queue_size:    int = 512
    watch_debounce_ms:   int = 1000
    watch_security_root: bool = True

    @field_validator("watch_queue_size")
    @classmethod
    def validate_watch_queue_size(cls, v: int) -> int:
        # Clamp rather than raise: a bad value must not stop the runtime booting.
        return max(16, min(int(v), 8192))

    @field_validator("watch_debounce_ms")
    @classmethod
    def validate_watch_debounce_ms(cls, v: int) -> int:
        return max(50, min(int(v), 60_000))

    @field_validator("watch_security_root", mode="before")
    @classmethod
    def _coerce_watch_security_root(cls, v) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    # ── Interactive turn deadlines (V69 M54.1.5-.7) ──────────────────────────
    # The live first turn never returned: AsyncOpenAI was built with no timeout=,
    # inheriting the SDK default read=600 (ten minutes nobody chose), and the M54
    # TurnBudget only ever bounded the verifier. These make the real bounds
    # explicit and operator-tunable — within hard caps, so a typo cannot recreate
    # an effectively unlimited wait.
    #   turn_budget_scale         : multiplies every risk-sized total (0.25..3.0)
    #   turn_first_token_timeout_s: connect -> first token (covers the cold model
    #                               swap that OLLAMA_MAX_LOADED_MODELS=1 forces)
    #   turn_stream_idle_timeout_s: max gap between chunks once streaming started
    #   turn_connect_timeout_s    : HTTP connection establishment
    turn_budget_scale:          float = 1.0
    turn_first_token_timeout_s: float = 45.0
    turn_stream_idle_timeout_s: float = 20.0
    turn_connect_timeout_s:     float = 5.0

    @field_validator("turn_budget_scale")
    @classmethod
    def validate_turn_budget_scale(cls, v: float) -> float:
        return max(0.25, min(float(v), 3.0))

    @field_validator("turn_first_token_timeout_s")
    @classmethod
    def validate_turn_first_token(cls, v: float) -> float:
        return max(2.0, min(float(v), 180.0))

    @field_validator("turn_stream_idle_timeout_s")
    @classmethod
    def validate_turn_stream_idle(cls, v: float) -> float:
        return max(1.0, min(float(v), 120.0))

    @field_validator("turn_connect_timeout_s")
    @classmethod
    def validate_turn_connect(cls, v: float) -> float:
        return max(0.5, min(float(v), 30.0))

    # ── AURA HUD server (loopback WebSocket telemetry / command HUD) ──────────
    # CSWSH defense for the /ws handshake: by default only loopback origins
    # (localhost / 127.0.0.0/8 / ::1) may open the AURA WebSocket; missing or
    # foreign Origins are rejected. Additional trusted origins may be allowlisted
    # here as a comma-separated list of exact Origin values (scheme://host[:port]),
    # e.g. "http://hud.lab:8765". Operator config only — never set from LLM input.
    aura_allowed_origins: str = ""
    # Optional fixed per-session token for the AURA /ws handshake. When empty,
    # the server generates a random per-process token at startup. Operator config
    # only — never sourced from LLM/tool input.
    aura_ws_token: str = ""

    # ── Trusted lab mode (operator-only; NEVER set from LLM/tool input) ───────
    # When True, the executor honors local-config security overrides and allows
    # HTTP requests to private/loopback ranges (for an isolated homelab). This
    # flag is read ONLY from the environment / .env (JARVIS_TRUSTED_LAB), so a
    # model-generated tool argument can never enable it. Default: hardened off.
    trusted_lab_mode: bool = False

    @field_validator("trusted_lab_mode", mode="before")
    @classmethod
    def _coerce_trusted_lab(cls, v) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    # ── Source Trust Registry (V64 M10) — operator-only content-trust knobs ───
    # Tune how retrieved *sources* are trusted as evidence — a separate axis from
    # trusted_lab_mode / authority. NEVER set from LLM/tool input (env/.env only).
    #   source_trust_allowlist : CSV of "domain" (→ trusted_secondary) or
    #       "domain=tier" (primary|trusted_secondary|community|untrusted|blocked).
    #   source_trust_blocklist : CSV of domains forced to BLOCKED (fail-closed).
    #   source_require_https   : demote non-HTTPS sources to at most COMMUNITY
    #       (auto-relaxed under trusted_lab_mode for isolated http homelabs).
    source_trust_allowlist: str = ""
    source_trust_blocklist: str = ""
    source_require_https:   bool = True

    @field_validator("source_require_https", mode="before")
    @classmethod
    def _coerce_source_https(cls, v) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: str) -> str:
        allowed = {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3"}
        if v not in allowed:
            raise ValueError(f"whisper_model must be one of: {allowed}")
        return v

    @field_validator("whisper_language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        if not re.match(r'^[a-z]{2,3}(-[A-Z]{2})?$|^auto$', v):
            raise ValueError(
                "Invalid language code. Use ISO 639-1 (e.g. 'es', 'en') or 'auto'."
            )
        return v

    @field_validator("record_seconds")
    @classmethod
    def validate_record_seconds(cls, v: int) -> int:
        if not 1 <= v <= 60:
            raise ValueError("record_seconds must be between 1 and 60.")
        return v

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: int) -> int:
        if v not in {8000, 16000, 22050, 44100, 48000}:
            raise ValueError(
                "Invalid sample_rate. Allowed: 8000, 16000, 22050, 44100, 48000."
            )
        return v

    @field_validator("llm_max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: int) -> int:
        if not 256 <= v <= 8192:
            raise ValueError("llm_max_tokens must be between 256 and 8192.")
        return v

    def get_secondary_vms(self) -> list[str]:
        """Parse the comma-separated secondary_vms string into a list."""
        return [v.strip() for v in self.secondary_vms.split(",") if v.strip()]

    def get_aura_allowed_origins(self) -> list[str]:
        """Parse the comma-separated aura_allowed_origins into a normalized list."""
        return [o.strip().rstrip("/") for o in self.aura_allowed_origins.split(",") if o.strip()]

    # ── Role-model view (V67 M27) ─────────────────────────────────────────────
    # NOTE: role→model configuration deliberately stays authoritative in
    # core.model_router (``_ROLE_DEFAULTS`` + ``JARVIS_MODEL_*`` env, resolved by
    # ``resolve_role_model``). Promoting those into Settings fields would create a
    # SECOND source of truth and re-introduce the V66.1 split-brain. This is a
    # read-THROUGH facade only — it delegates to the one resolver so the config
    # layer can *report* the active role models (doctors / AURA) without owning
    # or duplicating them. env override → central default precedence is unchanged.
    def resolved_role_models(self, *, installed=None) -> dict[str, str]:
        """Resolved concrete model per cognitive role (read-through to the router).

        Pass ``installed`` (pulled model names) to gate against availability; omit
        for a pure config view (env override → central default, no Ollama query).
        """
        from core.model_router import ModelRole, resolve_role_model
        roles = (ModelRole.FAST, ModelRole.CODER, ModelRole.DEEP,
                 ModelRole.VISION, ModelRole.EMBEDDING, ModelRole.VERIFIER)
        return {r.value: resolve_role_model(r, installed=installed) for r in roles}


# Singleton — import from here throughout the project
settings = Settings()
