"""
core/boot_sequence.py — JARVIS dramatic boot sequence (v46.0).

The moment of awakening. TTS narrates each subsystem coming online.
Visual boot log streams to AURA in real-time.

Boot phases:
  T+0    Hardware detection
  T+2    Core systems online
  T+4    Detection subsystems active
  T+6    Intelligence engines ready
  T+8    Autonomous services active
  T+10   JARVIS ready

Not just logging — this is the cinematic moment.
"""

import asyncio
from datetime import datetime, timezone
from loguru import logger

# V68.1 M48 — the previous fixed script narrated states that could contradict
# reality (Moondream/ETW/Sysmon/Telegram/"all nominal"). When a truthful
# core.boot_state.BootState snapshot is supplied, narration is derived from it;
# these constants are only the honest, capability-neutral fallback used when no
# snapshot is available (they no longer assert specific integrations are active).
_FALLBACK_LINES = [
    ("hardware",      "Hardware profile loaded.",                    0.5),
    ("memory",        "Episodic memory subsystem initialized.",      0.7),
    ("detection",     "Detection subsystems initializing.",          0.9),
    ("correlation",   "Correlation engine warming.",                 0.7),
    ("vision",        "Vision subsystem initializing.",              0.6),
    ("ready",         "JARVIS online.",                              1.0),
]
# Phases that are spoken aloud (the rest stream only to AURA/logs).
_SPOKEN_PHASES = frozenset({"detection", "ready"})


def _lines_from_state(boot_state) -> list[tuple[str, str, float]]:
    """Attach display pauses to the truthful narration lines from a BootState."""
    pause_by_phase = {
        "hardware": 0.5, "memory": 0.6, "llm": 0.6, "detection": 0.9,
        "correlation": 0.6, "vision": 0.6, "persistence": 0.6,
        "communication": 0.6, "ready": 1.0,
    }
    return [
        (phase, message, pause_by_phase.get(phase, 0.6))
        for phase, message in boot_state.narration_lines()
    ]


async def execute_boot_sequence(
    broadcast_fn,
    tts,
    skip_voice: bool = False,
    boot_state=None,
) -> None:
    """
    Execute the JARVIS boot sequence.
    Streams visual to AURA. Optionally speaks each phase.

    When *boot_state* (core.boot_state.BootState) is provided, every narrated
    line reflects the real subsystem state — no fabricated "Moondream loaded"
    or "all systems nominal". Without it, a capability-neutral fallback is used.
    """
    import os
    if os.getenv("JARVIS_QUIET_BOOT", "0") == "1":
        skip_voice = True

    logger.info("BOOT_SEQUENCE: initiating JARVIS awakening…")

    boot_lines = _lines_from_state(boot_state) if boot_state is not None else _FALLBACK_LINES

    await broadcast_fn({
        "type":      "boot_sequence_started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # V69 M54.9 — boot narration is LOW priority so it is dropped first under
    # backpressure and can be cancelled the moment the operator starts interacting
    # (tts.cancel_boot_narration()). The final "ready" line is NORMAL so it survives
    # a little longer. Each phase uses a coalesce key so a re-narrated phase never
    # stacks duplicates.
    try:
        from core.tts_queue import TTSPriority
    except Exception:  # pragma: no cover - tts_queue is always present
        TTSPriority = None

    def _prio(phase_name: str):
        if TTSPriority is None:
            return {}
        return {
            "priority": TTSPriority.NORMAL if phase_name == "ready" else TTSPriority.LOW,
            "coalesce_key": f"boot:{phase_name}",
        }

    # Initial wake message
    if tts and not skip_voice:
        from core.personality import get_boot_greeting
        greeting = get_boot_greeting()
        asyncio.create_task(tts.speak_async(greeting, **_prio("greeting")))
        await asyncio.sleep(2.5)

    for phase, message, pause in boot_lines:
        await broadcast_fn({
            "type":      "boot_phase",
            "phase":     phase,
            "message":   message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(f"BOOT: {phase.upper()} — {message}")

        if tts and not skip_voice and phase in _SPOKEN_PHASES:
            asyncio.create_task(tts.speak_async(message, **_prio(phase)))

        await asyncio.sleep(pause)

    await broadcast_fn({
        "type":      "boot_sequence_complete",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("BOOT_SEQUENCE: complete — JARVIS online")
