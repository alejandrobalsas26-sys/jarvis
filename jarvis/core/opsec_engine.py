"""
core/opsec_engine.py — OPSEC risk scoring engine (v43.0).

Analyzes planned attack actions and scores their OPSEC risk.
Provides specific recommendations to reduce detection probability.

OPSEC score: 0 (burned immediately) to 100 (nation-state silent)
"""

import asyncio
import json
import re
from datetime import datetime

from loguru import logger

_OPSEC_SYSTEM = """You are an elite red team OPSEC advisor.
Score the provided attack action for operational security risk.
Consider: noise level, detectability, traffic patterns, tool signatures.
Response as JSON:
{
  "opsec_score": 0-100,
  "risk_level": "CRITICAL|HIGH|MEDIUM|LOW",
  "detection_probability_pct": 0-100,
  "noisy_factors": ["list of what makes this detectable"],
  "recommendations": ["specific improvements to reduce detection"],
  "alternative_technique": "T-code of lower-noise alternative"
}
Output ONLY the JSON."""


async def score_action(
    action: str,
    technique_id: str,
    target_ip: str,
    broadcast_fn,
    ollama_client,
    model: str,
) -> dict:
    """
    Score an attack action for OPSEC risk.
    Called before ARES executes each stage.
    """
    current_hour = datetime.now().hour
    time_context = (
        "business hours (08:00-18:00)" if 8 <= current_hour <= 18
        else "off-hours"
    )

    prompt = (
        f"Attack action: {action}\n"
        f"MITRE technique: {technique_id}\n"
        f"Target: {target_ip}\n"
        f"Time of execution: {time_context}\n\n"
        "Score OPSEC risk and provide recommendations:"
    )

    try:
        resp = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _OPSEC_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                stream     = False,
                extra_body = {"options": {
                    "num_ctx":     1024,
                    "temperature": 0.1,
                }},
            ),
            timeout=20.0,
        )
        text = resp.choices[0].message.content.strip()
        text = re.sub(r'^```json\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text).strip()
        result = json.loads(text)
    except Exception:
        result = {
            "opsec_score":               50,
            "risk_level":                "MEDIUM",
            "detection_probability_pct": 50,
            "noisy_factors":             ["analysis unavailable"],
            "recommendations":           [],
            "alternative_technique":     technique_id,
        }

    score = int(result.get("opsec_score", 50))
    risk  = str(result.get("risk_level", "MEDIUM"))

    logger.info(
        f"OPSEC: {technique_id} scored {score}/100 "
        f"(risk={risk}, detect={result.get('detection_probability_pct','?')}%)"
    )

    try:
        from datetime import timezone
        await broadcast_fn({
            "type":           "opsec_score",
            "technique":      technique_id,
            "action_preview": str(action)[:80],
            "opsec_score":    score,
            "risk_level":     risk,
            "detect_pct":     result.get("detection_probability_pct", 50),
            "recommendations": list(result.get("recommendations", []))[:2],
            "severity":       "HIGH" if score < 40 else "MEDIUM",
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    return result


def get_opsec_rating(score: int) -> str:
    """Convert numeric score to operator-friendly rating."""
    if score >= 80:
        return "◈◈◈◈◈ GHOST"
    if score >= 60:
        return "◈◈◈◈○ SILENT"
    if score >= 40:
        return "◈◈◈○○ MODERATE"
    if score >= 20:
        return "◈◈○○○ NOISY"
    return "◈○○○○ BURNED"
