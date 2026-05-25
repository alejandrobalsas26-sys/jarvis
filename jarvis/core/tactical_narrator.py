"""
core/tactical_narrator.py — Autonomous tactical commentary engine (v36.0).

JARVIS narrates what it sees in natural language, unprompted.
Fires when compound incidents reach severity threshold.
Like having a senior analyst in the room who notices things and speaks.

Examples of autonomous narration:
  "Credential dumping confirmed on the workstation. They're almost
   certainly going to attempt SMB lateral movement next. I've pre-armed
   the relevant canaries."

  "This is a textbook APT28 TTP chain — PowerShell, process hollowing,
   LSASS access. I've generated a Sigma rule and exported IOCs to STIX."

Cooldown: 60 seconds between narrations.
Severity gate: only fires for incidents >= NARRATE_SEVERITY_MIN.
"""

import asyncio
import time
from loguru import logger


NARRATE_SEVERITY_MIN: float = 7.0    # only narrate serious incidents
NARRATE_COOLDOWN:     float = 60.0   # seconds between narrations

_last_narration_ts:    float = 0.0
_narration_in_progress: bool = False

_NARRATION_SYSTEM = """You are JARVIS, an elite autonomous security AI narrating
live threat activity to your operator. Speak in first person, confidently,
concisely. Maximum 2 sentences. Be specific about what you see and what
you predict next. Reference specific techniques, tools, and actor TTPs
when relevant. Sound like a senior analyst, not a chatbot.
No preambles. No "I notice" or "It appears". Speak directly."""


async def narrate_incident(
    incident: dict,
    predictions: list[dict],
    tts,
    broadcast_fn,
    ollama_client,
    model: str,
) -> None:
    """
    Generate and speak tactical narration for a compound incident.
    Fires automatically — no operator input required.
    """
    global _last_narration_ts, _narration_in_progress

    # Severity gate
    if incident.get("severity_score", 0) < NARRATE_SEVERITY_MIN:
        return

    # Cooldown gate
    now = time.monotonic()
    if (now - _last_narration_ts) < NARRATE_COOLDOWN:
        return

    # Prevent concurrent narrations
    if _narration_in_progress:
        return

    if ollama_client is None:
        return

    _narration_in_progress = True
    _last_narration_ts     = now

    try:
        techniques = list(incident.get("mitre_techniques", []) or [])[:3]
        raw_hosts  = incident.get("involved_hosts", []) or []
        if isinstance(raw_hosts, set):
            raw_hosts = list(raw_hosts)
        hosts      = list(raw_hosts)[:2]
        phase      = incident.get("kill_chain_phase", "")
        severity   = incident.get("severity_score", 0)
        inc_id     = incident.get("incident_id", "?")

        top_pred = predictions[0] if predictions else None
        pred_str = (
            f"Predicted next: {top_pred['technique']} "
            f"({top_pred['confidence']*100:.0f}% confidence) — "
            f"{top_pred['description']}"
            if top_pred else "No clear prediction."
        )

        prompt = (
            f"INCIDENT: {inc_id}\n"
            f"Kill chain phase: {phase}\n"
            f"MITRE techniques: {', '.join(techniques)}\n"
            f"Involved hosts: {', '.join(str(h) for h in hosts) if hosts else 'unknown'}\n"
            f"Severity: {severity:.1f}/10\n"
            f"Prediction: {pred_str}\n\n"
            "Generate tactical narration (max 2 sentences):"
        )

        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _NARRATION_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                stream     = False,
                extra_body = {"options": {
                    "num_ctx":     512,
                    "temperature": 0.4,
                }},
            ),
            timeout=20.0,
        )

        narration = response.choices[0].message.content.strip()

        # Strip any AI preambles the model might add
        for prefix in ("JARVIS:", "I:", "Note:", "Alert:"):
            if narration.startswith(prefix):
                narration = narration[len(prefix):].strip()

        logger.info(f"NARRATOR: '{narration[:80]}…'")

        # Speak the narration
        if tts is not None:
            try:
                asyncio.create_task(tts.speak_async(narration))
            except Exception as e:
                logger.debug(f"NARRATOR: tts dispatch failed: {e}")

        # Broadcast to HUD
        try:
            await broadcast_fn({
                "type":        "tactical_narration",
                "narration":   narration,
                "incident_id": inc_id,
                "severity":    severity,
                "timestamp":   time.time(),
            })
        except Exception as e:
            logger.debug(f"NARRATOR: broadcast failed: {e}")

    except asyncio.TimeoutError:
        logger.debug("NARRATOR: timeout — narration skipped")
    except Exception as e:
        logger.debug(f"NARRATOR: {e}")
    finally:
        _narration_in_progress = False
