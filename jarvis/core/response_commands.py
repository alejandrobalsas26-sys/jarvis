"""core/response_commands.py — V69 M57.5/M57.9: operator response-pipeline commands.

The deterministic operator surface for the adaptive response pipeline, following
the ``core.mode_commands`` / ``core.consent_commands`` pattern: ONE parser used
identically from text and voice, so there is exactly one way response behaviour
changes.

SECURITY POSTURE
----------------
This surface is deliberately the narrowest thing in the runtime:

  * EXACT-MATCH ALLOWLIST — a command is recognised only when the whole line
    matches a known alias. No prefix matching, no regex over operator text, no
    free-form arguments, no shell, no paths, no host configuration;
  * every command is either read-only or flips a bounded, in-process session
    value (verbosity profile, mute, cancel the ACTIVE turn);
  * nothing here can widen authority, change the model, alter Ollama posture,
    bypass HITL, or touch the filesystem. A command that is not on the list is
    not a command — it is an ordinary user turn.

Pure parsing; the caller applies the effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResponseCommand(str, Enum):
    """The complete, closed set of response-pipeline commands."""

    # bounded controls
    BRIEF = "BRIEF"
    STANDARD = "STANDARD"
    DETAILED = "DETAILED"
    AUTO = "AUTO"
    MUTE = "MUTE"
    UNMUTE = "UNMUTE"
    STOP = "STOP"
    CONTINUE = "CONTINUE"
    # read-only status
    RESPONSE_STATUS = "RESPONSE_STATUS"
    RESPONSE_PROFILE = "RESPONSE_PROFILE"
    LATENCY = "LATENCY"
    CONTEXT_STATUS = "CONTEXT_STATUS"
    TTS_STATUS = "TTS_STATUS"


# Commands that only READ state. Everything else mutates one bounded session value.
READ_ONLY: frozenset[ResponseCommand] = frozenset({
    ResponseCommand.RESPONSE_STATUS, ResponseCommand.RESPONSE_PROFILE,
    ResponseCommand.LATENCY, ResponseCommand.CONTEXT_STATUS,
    ResponseCommand.TTS_STATUS,
})
# CONTINUE is not handled here: it is a conversational intent that must reach the
# model with continuation context (M57.7), so the loop treats it as a real turn.
CONVERSATIONAL: frozenset[ResponseCommand] = frozenset({ResponseCommand.CONTINUE})

_ALIASES: dict[ResponseCommand, tuple[str, ...]] = {
    ResponseCommand.BRIEF: ("/brief", "/breve", "/corto"),
    ResponseCommand.STANDARD: ("/standard", "/estandar", "/estándar", "/normal"),
    ResponseCommand.DETAILED: ("/detailed", "/detallado", "/detalle"),
    ResponseCommand.AUTO: ("/auto",),
    ResponseCommand.MUTE: ("/mute", "/silencio", "/callar"),
    ResponseCommand.UNMUTE: ("/unmute", "/hablar", "/sonido"),
    ResponseCommand.STOP: ("/stop", "/interrupt", "/cancel", "/parar", "/detener",
                           "/cancelar"),
    ResponseCommand.CONTINUE: ("/continue", "/continuar", "/continua", "/continúa",
                               "/sigue"),
    ResponseCommand.RESPONSE_STATUS: ("/response-status", "/estado-respuesta"),
    ResponseCommand.RESPONSE_PROFILE: ("/response-profile", "/perfil-respuesta"),
    ResponseCommand.LATENCY: ("/latency", "/latencia"),
    ResponseCommand.CONTEXT_STATUS: ("/context-status", "/estado-contexto"),
    ResponseCommand.TTS_STATUS: ("/tts-status", "/estado-voz"),
}
# Flattened exact-match table, built once. Exact match is the whole security
# argument: "/stop the scan" is NOT /stop, it is a user turn.
_EXACT: dict[str, ResponseCommand] = {
    alias: cmd for cmd, aliases in _ALIASES.items() for alias in aliases
}

_PROFILE_COMMANDS: dict[ResponseCommand, str] = {
    ResponseCommand.BRIEF: "BRIEF",
    ResponseCommand.STANDARD: "STANDARD",
    ResponseCommand.DETAILED: "DETAILED",
    ResponseCommand.AUTO: "AUTO",
}


@dataclass(frozen=True)
class ParsedCommand:
    """One recognised command. Carries no operator text beyond the matched alias."""

    command: ResponseCommand
    alias: str

    @property
    def read_only(self) -> bool:
        return self.command in READ_ONLY

    @property
    def conversational(self) -> bool:
        return self.command in CONVERSATIONAL

    def profile_value(self) -> str | None:
        return _PROFILE_COMMANDS.get(self.command)


def parse_response_command(text: str) -> ParsedCommand | None:
    """Return the command when the WHOLE line is a known alias, else ``None``.

    Case-insensitive and whitespace-tolerant, nothing more. There is deliberately
    no partial or prefix matching: an operator sentence that merely contains a
    command word stays an ordinary turn.
    """
    raw = (text or "").strip().lower()
    if not raw or not raw.startswith("/"):
        return None
    cmd = _EXACT.get(raw)
    return ParsedCommand(command=cmd, alias=raw) if cmd is not None else None


def known_aliases() -> tuple[str, ...]:
    """The complete allowlist, for help output and tests."""
    return tuple(sorted(_EXACT))


# ── Localized confirmations (bounded, never echo operator text) ───────────────
_CONFIRM = {
    ResponseCommand.BRIEF: ("Perfil de respuesta: BREVE.", "Response profile: BRIEF."),
    ResponseCommand.STANDARD: ("Perfil de respuesta: ESTÁNDAR.",
                               "Response profile: STANDARD."),
    ResponseCommand.DETAILED: ("Perfil de respuesta: DETALLADO.",
                               "Response profile: DETAILED."),
    ResponseCommand.AUTO: ("Perfil de respuesta: AUTOMÁTICO.",
                           "Response profile: AUTO."),
    ResponseCommand.MUTE: ("Voz silenciada.", "Speech muted."),
    ResponseCommand.UNMUTE: ("Voz activada.", "Speech unmuted."),
}
_STOP_ACTIVE = ("Generación interrumpida.", "Generation interrupted.")
_STOP_IDLE = ("No hay ninguna respuesta en curso.",
              "There is no answer in progress.")


def describe(command: ResponseCommand, *, language: str = "es",
             active: bool = False) -> str:
    """Short operator-facing confirmation in the active language."""
    en = str(language or "es").lower().startswith("en")
    if command is ResponseCommand.STOP:
        pair = _STOP_ACTIVE if active else _STOP_IDLE
        return pair[1] if en else pair[0]
    pair = _CONFIRM.get(command)
    if pair is None:
        return command.value
    return pair[1] if en else pair[0]
