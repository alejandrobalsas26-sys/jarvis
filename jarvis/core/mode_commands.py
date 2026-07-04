"""
core/mode_commands.py — V62.0 Phase 8: explicit AssistantMode switch commands.

Recognizes operator phrases (EN/ES) that switch the live AssistantState.mode
— the same command-surface pattern core.consent_commands established for
SessionConsent: one parser, used identically from voice and text input, so
there is exactly one way the operating posture changes.
"""
from __future__ import annotations

from core.ironman_mode import AssistantMode

_MODE_ALIASES: dict[AssistantMode, tuple[str, ...]] = {
    AssistantMode.PASSIVE: (
        "passive mode", "go passive", "modo pasivo",
    ),
    AssistantMode.ACTIVE: (
        "active mode", "modo activo",
    ),
    AssistantMode.FOCUS: (
        "focus mode", "modo enfoque", "modo concentracion", "modo concentración",
    ),
    AssistantMode.WAR_ROOM: (
        "war room mode", "war room", "modo sala de guerra", "sala de guerra",
    ),
    AssistantMode.PRESENTATION: (
        "presentation mode", "modo presentacion", "modo presentación",
    ),
}


def parse_mode_command(text: str) -> AssistantMode | None:
    """Return the requested AssistantMode if *text* explicitly asks to
    switch modes, else None. Longer/more specific phrases are checked before
    shorter ones so e.g. "war room mode" doesn't get lost to a broader match."""
    t = (text or "").lower().strip()
    if not t:
        return None
    for mode, aliases in _MODE_ALIASES.items():
        if any(alias in t for alias in aliases):
            return mode
    return None


def describe_mode(mode: AssistantMode) -> str:
    """Short operator-facing confirmation for a mode switch."""
    names = {
        AssistantMode.PASSIVE: "Passive mode — I'll respond only, no proactive actions.",
        AssistantMode.ACTIVE: "Active mode.",
        AssistantMode.FOCUS: "Focus mode — minimal interruptions.",
        AssistantMode.WAR_ROOM: "War room mode — SOC workflow suggestions on.",
        AssistantMode.PRESENTATION: "Presentation mode — no sensitive screen reading, quiet voice.",
    }
    return names.get(mode, f"{mode.value} mode.")
