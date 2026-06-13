"""
core/ironman_mode.py — Iron Man Mode policy foundation (V61, Phase 7).

A *pure, consent-gated* policy layer for an always-available, multimodal
workstation assistant. It deliberately contains NO runtime loops, NO capture,
and NO I/O — it only answers "is this behavior allowed right now?" so the
existing voice / screen / clipboard loops can call these predicates before
acting. Nothing here ever enables a sensor on its own.

Safety invariants:
  * No silent surveillance. Screen / camera / clipboard / microphone use is
    gated on EXPLICIT per-session consent (default: all OFF).
  * Mode governs *proactive* (unprompted) behavior; consent governs *sensor*
    access. Both must agree before a proactive multimodal action is allowed.
  * Dangerous tools always remain HITL/NATO-gated downstream — proactive
    actions returned here are suggestions/awareness, never tool execution.
  * Hardware-aware: background work backs off on a CPU-bound, on-battery host.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Hardware back-off ceilings (Rule of Silicon — never choke the 15W host).
_CPU_CEIL_PCT = 85.0
_RAM_CEIL_PCT = 90.0

_ON_BATTERY_TOKENS = {"battery", "discharging", "unplugged", "on_battery", "low"}


class AssistantMode(str, Enum):
    """Operating posture of the assistant for the current session."""
    PASSIVE = "passive"            # respond only; no proactive sensor use
    ACTIVE = "active"              # voice + memory; sensors only if consented
    FOCUS = "focus"               # minimal interruptions; quiet background
    WAR_ROOM = "war_room"          # cyber lab; SOC workflow suggestions
    PRESENTATION = "presentation"  # no sensitive screen reading; no noisy TTS


@dataclass
class SessionConsent:
    """Per-session, explicitly-granted consent for sensitive surfaces.

    Defaults are all False: the operator must opt each surface in for the
    current session. There is no persistent/implicit consent.
    """
    screen: bool = False
    clipboard: bool = False
    camera: bool = False
    microphone: bool = False
    shell: bool = False
    browser: bool = False


def default_consent() -> SessionConsent:
    """A fresh, fully-revoked consent object (everything OFF)."""
    return SessionConsent()


def parse_mode(value: "str | AssistantMode | None") -> AssistantMode:
    """Coerce *value* into an AssistantMode, defaulting safely to PASSIVE."""
    if isinstance(value, AssistantMode):
        return value
    try:
        return AssistantMode(str(value).strip().lower())
    except (ValueError, AttributeError):
        return AssistantMode.PASSIVE


def _is_on_battery(battery_state: "str | bool | None") -> bool:
    """Interpret a battery descriptor. ``True`` means running on battery."""
    if isinstance(battery_state, bool):
        return battery_state
    return str(battery_state or "").strip().lower() in _ON_BATTERY_TOKENS


# Explicit screen-intent vocabulary (EN + ES). Used to confirm the operator is
# actually asking about their screen before any screen context is consulted.
_SCREEN_INTENT_KW = (
    "screen", "pantalla", "monitor", "what am i", "qué estoy", "que estoy",
    "what do you see", "qué ves", "que ves", "this error", "este error",
    "analyze this", "analiza esto", "read the screen", "lee la pantalla",
    "what's on", "que hay en pantalla", "qué hay en pantalla", "look at this",
)


def should_use_screen_context(user_message: str, consent: SessionConsent) -> bool:
    """True only if screen consent is granted AND the user explicitly asks.

    This is a *responsive* gate (the user asked about their screen), never a
    proactive one. With ``consent.screen`` False it is always False — there is
    no silent screen capture under any mode.
    """
    if not consent or not consent.screen:
        return False
    text = (user_message or "").lower()
    return any(kw in text for kw in _SCREEN_INTENT_KW)


def should_listen_continuously(mode: AssistantMode) -> bool:
    """Whether the mode wants an always-on listening loop.

    (Microphone activation additionally requires ``consent.microphone`` at the
    call site — this only expresses the mode's intent.)
    """
    return mode in (AssistantMode.ACTIVE, AssistantMode.WAR_ROOM)


def should_run_background_tasks(
    mode: AssistantMode,
    battery_state: "str | bool | None",
    cpu_pct: float,
    ram_pct: float,
) -> bool:
    """Whether autonomous background work is permitted right now.

    Quiet modes (FOCUS, PRESENTATION) and PASSIVE never run autonomous work.
    Even in ACTIVE/WAR_ROOM we back off under CPU/RAM pressure or on battery.
    """
    if mode in (AssistantMode.FOCUS, AssistantMode.PRESENTATION, AssistantMode.PASSIVE):
        return False
    try:
        if float(cpu_pct) >= _CPU_CEIL_PCT or float(ram_pct) >= _RAM_CEIL_PCT:
            return False
    except (TypeError, ValueError):
        return False
    if _is_on_battery(battery_state):
        return False
    return mode in (AssistantMode.ACTIVE, AssistantMode.WAR_ROOM)


def allowed_proactive_actions(
    mode: AssistantMode,
    consent: SessionConsent,
) -> list[str]:
    """The proactive (unprompted) actions permitted in *mode* given *consent*.

    Returned identifiers are awareness/suggestion capabilities only — they never
    execute dangerous tools, which always stay HITL/NATO-gated downstream.

      * PASSIVE      → [] (respond only).
      * FOCUS        → ["notify_urgent"] (minimal interruptions).
      * PRESENTATION → ["notify_urgent"] (no sensitive screen reading / no TTS).
      * ACTIVE       → text suggestions + consent-gated screen/clipboard/camera.
      * WAR_ROOM     → ACTIVE surface + SOC workflow / threat-hunt suggestions.
    """
    consent = consent or default_consent()
    if mode == AssistantMode.PASSIVE:
        return []
    if mode in (AssistantMode.FOCUS, AssistantMode.PRESENTATION):
        return ["notify_urgent"]

    actions = ["suggest", "notify"]
    if consent.screen:
        actions.append("screen_suggestions")
    if consent.clipboard:
        actions.append("clipboard_intel")
    if consent.camera:
        actions.append("room_awareness")
    if mode == AssistantMode.WAR_ROOM:
        actions.extend(["soc_workflow_suggestions", "threat_hunt_suggestions"])
    return actions
