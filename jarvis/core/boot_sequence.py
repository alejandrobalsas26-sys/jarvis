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

_BOOT_LINES = [
    ("hardware",      "Hardware profile loaded.",                         0.5),
    ("memory",        "Episodic memory online.",                          0.7),
    ("detection",     "Detection subsystems active. ETW, Sysmon, canaries armed.", 0.9),
    ("correlation",   "Correlation engine warm. Five rules loaded.",      0.7),
    ("vision",        "Visual cortex online. Moondream loaded.",          0.6),
    ("predictive",    "Predictive threat engine ready.",                  0.7),
    ("hunting",       "Autonomous threat hunting scheduled.",             0.7),
    ("intelligence",  "Intelligence fusion database connected.",          0.6),
    ("communication", "Telegram bridge established.",                     0.6),
    ("ready",         "All systems nominal. JARVIS at your service.",     1.0),
]


async def execute_boot_sequence(
    broadcast_fn,
    tts,
    skip_voice: bool = False,
) -> None:
    """
    Execute the JARVIS boot sequence.
    Streams visual to AURA. Optionally speaks each phase.
    """
    import os
    if os.getenv("JARVIS_QUIET_BOOT", "0") == "1":
        skip_voice = True

    logger.info("BOOT_SEQUENCE: initiating JARVIS awakening…")

    await broadcast_fn({
        "type":      "boot_sequence_started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Initial wake message
    if tts and not skip_voice:
        from core.personality import get_boot_greeting
        greeting = get_boot_greeting()
        asyncio.create_task(tts.speak_async(greeting))
        await asyncio.sleep(2.5)

    for phase, message, pause in _BOOT_LINES:
        await broadcast_fn({
            "type":      "boot_phase",
            "phase":     phase,
            "message":   message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(f"BOOT: {phase.upper()} — {message}")

        if tts and not skip_voice and phase in ("detection", "ready"):
            asyncio.create_task(tts.speak_async(message))

        await asyncio.sleep(pause)

    await broadcast_fn({
        "type":      "boot_sequence_complete",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("BOOT_SEQUENCE: complete — JARVIS online")
