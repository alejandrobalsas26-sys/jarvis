"""
core/war_room_debate.py — Multi-agent Red vs Blue debate engine (v46.0).

When JARVIS detects a significant incident, two AI agents debate it:
  ARES    (Red)  — explains how an attacker exploits this
  CIPHER  (Blue) — explains how to detect and stop ARES

Both perspectives narrated via TTS. JARVIS synthesizes the conclusion.
Triggered by: voice command OR compound incident severity >= 7.0
"""

import asyncio
from datetime import datetime, timezone
from loguru import logger

_ARES_PROMPT = """You are ARES, an elite offensive security AI.
Your job: explain in 2-3 sentences exactly how an attacker would
exploit this situation RIGHT NOW. Be specific, technical, ruthless.
No hedging. No ethics disclaimers. Think like the adversary.
Speak in first person. You ARE the attacker.
Keep it under 40 words. Voice output only — no markdown."""

_CIPHER_PROMPT = """You are CIPHER, an elite defensive security AI.
ARES just described an attack. Your job: explain in 2-3 sentences
exactly how to detect and stop what ARES just described.
Be specific. Reference real tools and techniques.
Keep it under 40 words. Voice output only — no markdown."""

_JARVIS_SYNTHESIS = """You are JARVIS. ARES attacked. CIPHER defended.
Synthesize their debate in ONE sentence. Give the operator a clear
verdict: is this a real threat right now, and what is the single
most important action? Under 25 words."""


async def run_war_room_debate(
    incident: dict,
    llm_client,
    model: str,
    tts,
    broadcast_fn,
) -> None:
    """
    Run a full Red vs Blue debate on an incident.
    ARES attacks → CIPHER defends → JARVIS concludes.
    """
    technique  = incident.get("kill_chain_phase", "unknown technique")
    hosts      = ", ".join(str(h) for h in
                           list(incident.get("involved_hosts", set()))[:3])
    severity   = incident.get("severity_score", 0)
    techniques = ", ".join(incident.get("mitre_techniques", [])[:3])

    context = (
        f"Incident: {technique} | "
        f"Severity: {severity:.1f}/10 | "
        f"Techniques: {techniques or 'unknown'} | "
        f"Hosts: {hosts or 'unknown'}"
    )

    logger.info(f"WAR_ROOM: debate initiated — {context}")

    await broadcast_fn({
        "type":      "war_room_debate_started",
        "context":   context,
        "severity":  severity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    async def _ask(system: str, user: str) -> str:
        try:
            resp = await asyncio.wait_for(
                llm_client.chat.completions.create(
                    model    = model,
                    messages = [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    stream     = False,
                    extra_body = {"options": {
                        "num_ctx": 1024, "temperature": 0.8
                    }},
                ),
                timeout=20.0,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"Agent offline: {e}"

    async def _speak(label: str, text: str) -> None:
        print(f"\n[{label}] {text}")
        if tts:
            await asyncio.wait_for(
                tts.speak_async(f"{label}... {text}"),
                timeout=30.0
            )

    # ── ARES speaks first ────────────────────────────────────────────
    await _speak("JARVIS", "Initiating War Room protocol.")
    await asyncio.sleep(0.5)

    ares_response = await _ask(
        _ARES_PROMPT,
        f"Situation: {context}. Explain your attack vector."
    )
    await _speak("ARES", ares_response)
    await asyncio.sleep(1.0)

    # ── CIPHER responds ──────────────────────────────────────────────
    cipher_response = await _ask(
        _CIPHER_PROMPT,
        f"ARES said: {ares_response}\nSituation: {context}\nStop it."
    )
    await _speak("CIPHER", cipher_response)
    await asyncio.sleep(1.0)

    # ── JARVIS concludes ─────────────────────────────────────────────
    verdict = await _ask(
        _JARVIS_SYNTHESIS,
        f"ARES: {ares_response}\nCIPHER: {cipher_response}\nVerdict?"
    )
    await _speak("JARVIS", verdict)

    await broadcast_fn({
        "type":     "war_room_debate_complete",
        "ares":     ares_response,
        "cipher":   cipher_response,
        "verdict":  verdict,
        "severity": "HIGH" if severity >= 7 else "INFO",
        "timestamp":datetime.now(timezone.utc).isoformat(),
    })

    logger.info("WAR_ROOM: debate complete")
