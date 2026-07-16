"""core/deterministic_bypass.py — V69 M55.11: answer known facts WITHOUT a model.

Some questions have a single trusted local answer already sitting in the runtime:
the host clock, the lifecycle state, which FAST model/transport is active, whether
the Knowledge Vault is empty. Calling a CPU-bound LLM merely to restate a value we
already hold is pure latency. This module answers those deterministically, in the
turn's ACTIVE language (ES/EN continuity preserved), and returns ``None`` for
everything else so the turn falls through to the normal path.

It is deterministic (substring markers, not an LLM), bounded (no model, no network,
no heavy backend load — the vault check reports only if the backend is already
initialized), and never invents a value: if a trusted source is unavailable it
returns ``None`` rather than guessing, so the model/tool path handles it honestly.
"""
from __future__ import annotations

from enum import Enum


class BypassKind(str, Enum):
    NONE = "none"
    TIME = "time"
    DATE = "date"
    LIFECYCLE_STATE = "lifecycle_state"
    FAST_MODEL = "fast_model"
    VAULT_EMPTY = "vault_empty"


def _en(language: str | None) -> bool:
    return (language or "es").lower().startswith("en")


# ── Deterministic marker vocab (EN + ES), narrow to avoid hijacking real turns ─
_TIME_MARKERS = (
    "qué hora es", "que hora es", "hora actual", "qué hora", "que hora",
    "hora es", "what time", "current time", "time is it", "what's the time",
    "whats the time",
)
_DATE_MARKERS = (
    "qué fecha", "que fecha", "qué día es hoy", "que dia es hoy", "fecha de hoy",
    "día es hoy", "dia es hoy", "what date", "what day", "today's date",
    "todays date", "date is it", "what day is it",
)
# Narrow: only an explicit lifecycle-state question, not broad "system status"
# (which benefits from the model synthesizing the richer runtime-health view).
_LIFECYCLE_MARKERS = (
    "lifecycle state", "estado del ciclo de vida", "ciclo de vida",
    "en qué estado del ciclo", "en que estado del ciclo", "runtime state",
    "current lifecycle",
)
_FAST_MODEL_MARKERS = (
    "qué modelo fast", "que modelo fast", "modelo fast activo", "which fast model",
    "what fast model", "fast model active", "qué modelo estás usando",
    "que modelo estas usando", "qué modelo usas", "que modelo usas",
    "what model are you using", "which model are you using", "modelo activo",
)
_VAULT_MARKERS = (
    "knowledge vault empty", "vault empty", "vault vacía", "vault vacia",
    "base de conocimiento está vacía", "base de conocimiento vacia",
    "está vacía la base de conocimiento", "esta vacia la base de conocimiento",
    "is the knowledge vault empty", "is the vault empty",
)


def _hit(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def classify_bypass(user_message: str) -> BypassKind:
    """Classify a turn into a deterministic bypass kind, or NONE. Precedence:
    time → date → lifecycle → fast-model → vault (most specific first)."""
    text = (user_message or "").lower().strip()
    if not text:
        return BypassKind.NONE
    if _hit(text, _TIME_MARKERS):
        return BypassKind.TIME
    if _hit(text, _DATE_MARKERS):
        return BypassKind.DATE
    if _hit(text, _LIFECYCLE_MARKERS):
        return BypassKind.LIFECYCLE_STATE
    if _hit(text, _FAST_MODEL_MARKERS):
        return BypassKind.FAST_MODEL
    if _hit(text, _VAULT_MARKERS):
        return BypassKind.VAULT_EMPTY
    return BypassKind.NONE


# ── Answer builders (each returns None when its trusted source is unavailable) ─
def _answer_time(en: bool) -> str | None:
    try:
        from core import host_time
        ht = host_time.now()
    except Exception:  # noqa: BLE001
        return None
    if en:
        return f"It's {ht.time_hms()} ({ht.timezone})."
    return f"Son las {ht.time_hms()} ({ht.timezone})."


def _answer_date(en: bool) -> str | None:
    try:
        from core import host_time
        ht = host_time.now()
    except Exception:  # noqa: BLE001
        return None
    if en:
        weekday = ht.dt.strftime("%A")
        return f"Today is {weekday}, {ht.dt.strftime('%B %d, %Y')}."
    return f"Hoy es {ht.date_es()}."


def _answer_lifecycle(en: bool) -> str | None:
    try:
        from core.lifecycle import lifecycle
        snap = lifecycle.snapshot()
        state = str(snap.get("state", "")) or None
    except Exception:  # noqa: BLE001
        return None
    if not state:
        return None
    if en:
        return f"Current lifecycle state: {state}."
    return f"Estado actual del ciclo de vida: {state}."


def _answer_fast_model(en: bool) -> str | None:
    """Report the active FAST model + transport + think mode from trusted config."""
    model = ""
    transport = "auto"
    think_off = True
    try:
        from core.config import settings
        transport = getattr(settings, "fast_transport", "auto")
        think_off = settings.fast_think_value() is False
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.fast_readiness import get_fast_readiness
        model = get_fast_readiness().model or ""
    except Exception:  # noqa: BLE001
        pass
    if not model:
        try:
            from core.model_router import ModelRole, resolve_role_model
            model = resolve_role_model(ModelRole.FAST)
        except Exception:  # noqa: BLE001
            return None
    # Reflect the ACTUALLY selected transport (auto resolves via capability).
    resolved = transport
    try:
        from core.ollama_native import NativeProbeState, get_native_capability
        state = get_native_capability().state
        if transport == "auto":
            resolved = ("native" if state in (NativeProbeState.NATIVE_READY,
                                              NativeProbeState.UNKNOWN,
                                              NativeProbeState.PROBING) else "openai")
    except Exception:  # noqa: BLE001
        pass
    think_txt_en = "reasoning off" if think_off else "reasoning on"
    think_txt_es = "razonamiento desactivado" if think_off else "razonamiento activado"
    if en:
        return f"Active FAST model: {model} (transport={resolved}, {think_txt_en})."
    return f"Modelo FAST activo: {model} (transporte={resolved}, {think_txt_es})."


def _answer_vault(en: bool) -> str | None:
    try:
        from core.knowledge import vault_count_if_loaded
        count = vault_count_if_loaded()
    except Exception:  # noqa: BLE001
        return None
    if count is None:
        return None  # backend not loaded — let the tool path report honestly
    if count == 0:
        return ("The Knowledge Vault is empty." if en
                else "La base de conocimiento está vacía.")
    if en:
        return f"The Knowledge Vault has {count} indexed fragment(s)."
    return f"La base de conocimiento tiene {count} fragmento(s) indexado(s)."


_ANSWERERS = {
    BypassKind.TIME: _answer_time,
    BypassKind.DATE: _answer_date,
    BypassKind.LIFECYCLE_STATE: _answer_lifecycle,
    BypassKind.FAST_MODEL: _answer_fast_model,
    BypassKind.VAULT_EMPTY: _answer_vault,
}


def answer_bypass(kind: BypassKind, *, language: str | None = None) -> str | None:
    """Build the localized deterministic answer for *kind*, or None if unavailable."""
    fn = _ANSWERERS.get(kind)
    if fn is None:
        return None
    return fn(_en(language))


def maybe_bypass(user_message: str, *, language: str | None = None) -> str | None:
    """Top-level: return a deterministic localized answer for a known-fact question,
    or ``None`` to fall through to the model/tool path. Never raises."""
    try:
        kind = classify_bypass(user_message)
        if kind is BypassKind.NONE:
            return None
        return answer_bypass(kind, language=language)
    except Exception:  # noqa: BLE001
        return None
