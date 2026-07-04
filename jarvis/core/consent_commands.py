"""
core/consent_commands.py — V62.0 Phase 6: explicit consent grant/revoke commands.

core.ironman_mode.SessionConsent defaults every sensitive surface (screen,
camera, clipboard, microphone) to OFF and is never granted implicitly — "no
silent surveillance" is the whole point of that module. This is the other
half: the operator's own explicit grant/revoke phrases (EN/ES), recognized
identically from text and voice input, so there is exactly one command
surface that can ever turn a surface on.
"""
from __future__ import annotations

import re

from core.ironman_mode import SessionConsent

_SURFACE_ALIASES: dict[str, tuple[str, ...]] = {
    "screen": ("screen", "pantalla"),
    "camera": ("camera", "webcam", "cámara", "camara"),
    "clipboard": ("clipboard", "portapapeles"),
    "microphone": ("microphone", "mic", "micrófono", "microfono"),
}

_GRANT_KW = ("enable", "allow", "grant", "activa", "habilita", "permite", "autoriza")
_REVOKE_KW = ("disable", "revoke", "deny", "desactiva", "deshabilita", "revoca")

# Word-boundary matching — "activa"/"habilita" are substrings of their own
# revoke-side counterparts ("desactiva", "deshabilita"), so a plain `in`
# check would read every revoke phrase as ambiguous (both grant AND revoke
# "matched") and silently drop it.
_GRANT_RE = re.compile(r"\b(?:" + "|".join(_GRANT_KW) + r")\b")
_REVOKE_RE = re.compile(r"\b(?:" + "|".join(_REVOKE_KW) + r")\b")


def parse_consent_command(text: str) -> tuple[str, bool] | None:
    """Return (surface, grant) if *text* is an explicit consent command, else None.

    Requires BOTH a grant/revoke verb AND a recognized surface name, so
    ordinary conversation that happens to mention "camera" or "screen"
    without an explicit enable/disable intent never trips this.
    """
    t = (text or "").lower().strip()
    if not t:
        return None
    is_grant = bool(_GRANT_RE.search(t))
    is_revoke = bool(_REVOKE_RE.search(t))
    if is_grant == is_revoke:  # neither matched, or both did (ambiguous) — ignore
        return None
    for surface, aliases in _SURFACE_ALIASES.items():
        if any(alias in t for alias in aliases):
            return surface, is_grant
    return None


def apply_consent_command(consent: SessionConsent, surface: str, grant: bool) -> str:
    """Mutate *consent* in place and return an operator-facing confirmation."""
    setattr(consent, surface, grant)
    state = "enabled" if grant else "disabled"
    return f"{surface.capitalize()} access {state} for this session."
