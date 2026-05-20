"""
core/config.py — Configuración centralizada con validación de tipos via Pydantic.

Entorno air-gapped: sin API keys externas. El LLM corre en Ollama local.
Todas las variables de entorno pasan por aquí — nunca os.getenv() directo
en otros módulos.
"""

import re
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Busca .env en el directorio jarvis/ (padre de core/)
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
    user_name: str = "Alejandro"
    city: str = "Panama"

    # ── LLM (Ollama local) ────────────────────────────────────────────────────
    llm_model: str = "qwen2.5-coder"
    llm_max_tokens: int = 2048

    # ── Whisper STT ───────────────────────────────────────────────────────────
    whisper_model: str = "small"
    whisper_language: str = "es"
    record_seconds: int = 5
    sample_rate: int = 16000

    # ── Forensics ─────────────────────────────────────────────────────────────
    # Absolute path to the .vmx file used for live forensic capture.
    # Leave empty to disable the canary → vmrun trigger.
    vmx_target_path: str = ""

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: str) -> str:
        allowed = {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3"}
        if v not in allowed:
            raise ValueError(f"whisper_model debe ser uno de: {allowed}")
        return v

    @field_validator("whisper_language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        if not re.match(r'^[a-z]{2,3}(-[A-Z]{2})?$|^auto$', v):
            raise ValueError(
                "Código de idioma inválido. Usa ISO 639-1 (ej: 'es', 'en') o 'auto'."
            )
        return v

    @field_validator("record_seconds")
    @classmethod
    def validate_record_seconds(cls, v: int) -> int:
        if not 1 <= v <= 60:
            raise ValueError("record_seconds debe estar entre 1 y 60.")
        return v

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: int) -> int:
        if v not in {8000, 16000, 22050, 44100, 48000}:
            raise ValueError(
                "sample_rate inválido. Valores permitidos: 8000, 16000, 22050, 44100, 48000."
            )
        return v

    @field_validator("llm_max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: int) -> int:
        if not 256 <= v <= 8192:
            raise ValueError("llm_max_tokens debe estar entre 256 y 8192.")
        return v


# Singleton — importar desde aquí en todo el proyecto
settings = Settings()
