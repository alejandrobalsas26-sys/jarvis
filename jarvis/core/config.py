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


# Singleton — import from here throughout the project
settings = Settings()
