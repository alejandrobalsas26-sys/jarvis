"""
core/voice_interrupt.py — Voice interrupt command processor (v35.0).

Intercepts transcribed text before LLM routing.
If the text is an interrupt command, fires cancel_all() immediately
and returns True (caller should NOT forward to LLM).

Interrupt vocabulary (multilingual — English + Spanish):
  "stop", "abort", "cancel", "enough", "quiet", "silence", "shut up",
  "para", "detente", "cancela", "basta", "silencio", "callate",
  "hold on", "wait", "pause", "nevermind"

Priority commands (execute without full LLM inference):
  "status"      → broadcast active operations
  "what are you doing" → broadcast current operation
  "reset"       → reset cancel bus, fresh start
"""

import re
from datetime import datetime, timezone

from loguru import logger

# ── Interrupt vocabulary ──────────────────────────────────────────────────────

_ABORT_PATTERNS = [
    # English
    r"\bstop\b", r"\babort\b", r"\bcancel\b",
    r"\benough\b", r"\bquiet\b", r"\bsilence\b",
    r"\bshut up\b", r"\bhold on\b", r"\bwait\b",
    r"\bpause\b", r"\bnevermind\b", r"\bnever mind\b",
    # Spanish
    r"\bpara\b", r"\bdetente\b", r"\bcancela\b",
    r"\bbasta\b", r"\bsilencio\b", r"\bcállate\b",
    r"\bcallate\b", r"\bespera\b", r"\besperate\b",
    r"\bespérate\b",
]

_STATUS_PATTERNS = [
    r"\bstatus\b", r"\bwhat are you doing\b",
    r"\bwhat('s| is) happening\b",
    r"\bqué (haces|estás haciendo)\b",
]

_RESET_PATTERNS = [
    r"\breset\b", r"\bstart over\b",
    r"\bfresh start\b", r"\bempezar de nuevo\b",
]

_COMPILED_ABORT  = [re.compile(p, re.IGNORECASE) for p in _ABORT_PATTERNS]
_COMPILED_STATUS = [re.compile(p, re.IGNORECASE) for p in _STATUS_PATTERNS]
_COMPILED_RESET  = [re.compile(p, re.IGNORECASE) for p in _RESET_PATTERNS]


def is_interrupt_command(text: str) -> str | None:
    """
    Check if text is an interrupt command.
    Returns "abort", "status", "reset", or None.
    Short-circuits on first match — O(n) worst case.
    """
    text = text.strip()

    # Only check short commands — real interrupts are usually brief
    if len(text.split()) > 8:
        return None   # too long to be an interrupt

    for pattern in _COMPILED_ABORT:
        if pattern.search(text):
            return "abort"

    for pattern in _COMPILED_STATUS:
        if pattern.search(text):
            return "status"

    for pattern in _COMPILED_RESET:
        if pattern.search(text):
            return "reset"

    return None


async def handle_interrupt(
    command: str,
    broadcast_fn,
) -> None:
    """
    Execute the interrupt command.
    Called from the STT pipeline before LLM routing.
    """
    from core.cancel_bus import (
        cancel_all, reset_all, get_active_operations
    )

    if command == "abort":
        count = cancel_all()
        logger.warning(f"VOICE_INTERRUPT: ABORT — {count} operations cancelled")
        try:
            await broadcast_fn({
                "type":      "voice_interrupt",
                "command":   "abort",
                "cancelled": count,
                "severity":  "WARNING",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    elif command == "status":
        ops = get_active_operations()
        logger.info(f"VOICE_INTERRUPT: STATUS — {ops}")
        try:
            await broadcast_fn({
                "type":       "voice_interrupt",
                "command":    "status",
                "active_ops": ops,
                "severity":   "INFO",
                "timestamp":  datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    elif command == "reset":
        reset_all()
        logger.info("VOICE_INTERRUPT: RESET — cancel bus cleared")
        try:
            await broadcast_fn({
                "type":      "voice_interrupt",
                "command":   "reset",
                "severity":  "INFO",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
