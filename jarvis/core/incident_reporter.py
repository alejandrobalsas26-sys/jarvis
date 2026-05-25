"""
core/incident_reporter.py — Automated incident report generator (v36.0).

Generates professional Purple Team incident reports from compound
incident data, episodic memory, and STIX IOC exports.

Report structure:
  1. Executive Summary (non-technical, 3 sentences)
  2. Incident Timeline (chronological events)
  3. Technical Analysis (MITRE ATT&CK mapping, techniques used)
  4. IOC Summary (IPs, processes, hashes, domains)
  5. Attack Chain Reconstruction (kill chain narrative)
  6. Detection Coverage Assessment
  7. Remediation Recommendations (prioritized)
  8. Lessons Learned

Format: Markdown → saved to logs/reports/
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger


_REPORTS_DIR = Path("logs/reports")
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_REPORT_SYSTEM = """You are a senior Purple Team security analyst writing a
formal incident report. Be precise, technical, and thorough. Use proper
security terminology. Structure the report clearly with markdown headers.
Base analysis ONLY on the provided data — do not invent indicators."""


async def generate_incident_report(
    incident: dict,
    related_episodes: list[dict],
    broadcast_fn,
    ollama_client,
    model: str,
) -> str | None:
    """
    Generate a full incident report as markdown.
    Returns file path or None on failure.
    """
    if ollama_client is None:
        logger.warning("REPORTER: no ollama client available — abort")
        return None

    inc_id     = incident.get("incident_id", "UNK")
    techniques = incident.get("mitre_techniques", []) or []
    raw_hosts  = incident.get("involved_hosts", []) or []
    if isinstance(raw_hosts, set):
        raw_hosts = list(raw_hosts)
    hosts      = list(raw_hosts)
    phase      = incident.get("kill_chain_phase", "")
    severity   = incident.get("severity_score", 0)
    sub_events = (incident.get("sub_events") or [])[:10]

    # Build event timeline from sub_events
    timeline = "\n".join(
        f"- [{e.get('type','?')}] {e.get('process','?')} — "
        f"PID {e.get('pid','?')} — {e.get('technique','?')}"
        for e in sub_events
    )

    # Related episodes context
    episode_context = "\n".join(
        (ep.get("content") or "")[:200]
        for ep in (related_episodes or [])[:3]
    ) if related_episodes else "No related historical episodes."

    prompt = f"""Generate a complete incident report for this security incident.

INCIDENT DATA:
ID: {inc_id}
Severity: {severity}/10
Kill Chain Phase: {phase}
MITRE Techniques: {', '.join(techniques)}
Involved Hosts: {', '.join(str(h) for h in hosts) if hosts else 'Unknown'}

EVENT TIMELINE:
{timeline}

HISTORICAL CONTEXT:
{episode_context}

Write the full report with these sections:
# Incident Report: {inc_id}
## Executive Summary
## Incident Timeline
## Technical Analysis
## IOC Summary
## Attack Chain Reconstruction
## Detection Coverage
## Remediation (prioritized numbered list)
## Lessons Learned
"""

    try:
        await broadcast_fn({
            "type":        "report_generating",
            "incident_id": inc_id,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    try:
        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _REPORT_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                stream     = False,
                extra_body = {"options": {
                    "num_ctx":     4096,
                    "temperature": 0.15,
                }},
            ),
            timeout=120.0,
        )

        report_text = response.choices[0].message.content.strip()

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"incident_{inc_id}_{ts}.md"
        filepath = _REPORTS_DIR / filename
        filepath.write_text(report_text, encoding="utf-8")

        logger.info(f"REPORTER: incident report saved → {filename}")

        try:
            await broadcast_fn({
                "type":        "report_generated",
                "incident_id": inc_id,
                "filename":    filename,
                "filepath":    str(filepath),
                "size_bytes":  len(report_text.encode()),
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

        return str(filepath)

    except asyncio.TimeoutError:
        logger.warning("REPORTER: LLM timeout — report not generated")
        return None
    except Exception as e:
        logger.debug(f"REPORTER: {e}")
        return None
