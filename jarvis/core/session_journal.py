"""
core/session_journal.py — Automatic session journal (v44.0).

Collects a structured log of every significant JARVIS event during
the session. Written as a Markdown file on clean shutdown.

Sections:
  - Session metadata (start time, hardware, models)
  - Voice commands issued
  - Detections and incidents
  - BAS simulations run
  - Coverage gaps found
  - Tools used
  - CVE alerts
  - Sigma rules generated
  - Summary (LLM-generated, 1 paragraph)

Output: logs/journals/session_YYYYMMDD_HHMMSS.md
"""

import asyncio, time
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_JOURNALS_DIR = Path("logs/journals")
_JOURNALS_DIR.mkdir(parents=True, exist_ok=True)

_SESSION_START = datetime.now()

# Event log — accumulated throughout session
_events: list[dict] = []

# Tracked categories
_TRACKED_TYPES = {
    "compound_incident",
    "canary_intrusion",
    "etw_threat_event",
    "cve_alert",
    "bas_scenario_complete",
    "purple_coverage_gap",
    "sigma_rule_deployed",
    "github_tool_integrated",
    "memory_dump_complete",
    "ares_campaign_started",
    "ares_stage_complete",
    "proxy_credential_found",
    "sensor_connected",
    "clipboard_artifact_detected",
}


def record_event(event: dict) -> None:
    """
    Record a significant event.
    Called from the broadcast pipeline — never blocks.
    """
    if event.get("type") in _TRACKED_TYPES:
        _events.append({
            "time":     datetime.now().strftime("%H:%M:%S"),
            "type":     event.get("type", ""),
            "summary":  _summarize_event(event),
            "severity": event.get("severity", "INFO"),
        })


def record_voice_command(text: str) -> None:
    """Record a voice command or text input from the operator."""
    _events.append({
        "time":     datetime.now().strftime("%H:%M:%S"),
        "type":     "operator_command",
        "summary":  text[:100],
        "severity": "INFO",
    })


def _summarize_event(event: dict) -> str:
    t = event.get("type", "")
    if t == "compound_incident":
        return (
            f"Incident {event.get('incident_id','?')} — "
            f"severity {event.get('severity_score','?')} — "
            f"{event.get('kill_chain_phase','?')}"
        )
    if t == "canary_intrusion":
        return f"Canary hit from {event.get('attacker_ip','?')} on port {event.get('port','?')}"
    if t == "cve_alert":
        return f"CVE {event.get('cve_id','?')} CVSS {event.get('score','?')}"
    if t == "bas_scenario_complete":
        cov = event.get("coverage", {})
        return (
            f"BAS {event.get('scenario','?')} — "
            f"coverage {cov.get('coverage_pct','?')}% — "
            f"{cov.get('gaps','?')} gaps"
        )
    if t == "purple_coverage_gap":
        return f"Detection gap: {event.get('technique','?')}"
    if t == "sigma_rule_deployed":
        return f"Sigma rule deployed: {event.get('rule','?')}"
    if t == "ares_campaign_started":
        return f"ARES campaign against {event.get('target_ip','?')}"
    if t == "clipboard_artifact_detected":
        return (
            f"Clipboard: {event.get('artifact_type','?')} "
            f"{event.get('value','?')[:30]}"
        )
    return str(event.get("summary", ""))[:80]


async def write_journal(
    ollama_client=None,
    model: str = "",
) -> str | None:
    """
    Write the session journal to disk.
    Called on JARVIS shutdown.
    Returns file path.
    """
    if not _events:
        logger.info("SESSION_JOURNAL: no significant events to journal")
        return None

    session_end = datetime.now()
    duration    = session_end - _SESSION_START
    hours, rem  = divmod(int(duration.total_seconds()), 3600)
    minutes     = rem // 60

    # Group events by category
    incidents   = [e for e in _events if "incident" in e["type"]]
    detections  = [e for e in _events if e["type"] in {
                    "canary_intrusion", "etw_threat_event"}]
    commands    = [e for e in _events if e["type"] == "operator_command"]
    bas_events  = [e for e in _events if "bas" in e["type"]]
    gaps        = [e for e in _events if "gap" in e["type"]]

    # LLM session summary (optional, skip if no client)
    llm_summary = ""
    if ollama_client and _events:
        events_str = "\n".join(
            f"{e['time']} [{e['type']}] {e['summary']}"
            for e in _events[:30]
        )
        try:
            resp = await asyncio.wait_for(
                ollama_client.chat.completions.create(
                    model    = model,
                    messages = [{
                        "role": "user",
                        "content": (
                            f"JARVIS session events ({len(_events)} total):\n"
                            f"{events_str}\n\n"
                            "Write a 2-sentence analyst summary of this session. "
                            "What was the key security activity? "
                            "What are the most important findings?"
                        ),
                    }],
                    stream = False,
                    extra_body = {"options": {
                        "num_ctx": 1024, "temperature": 0.3
                    }},
                ),
                timeout=20.0,
            )
            llm_summary = resp.choices[0].message.content.strip()
        except Exception:
            pass

    # Build markdown
    ts       = _SESSION_START.strftime("%Y%m%d_%H%M%S")
    filename = f"session_{ts}.md"
    filepath = _JOURNALS_DIR / filename

    def _section(title: str, events: list[dict]) -> str:
        if not events:
            return ""
        rows = "\n".join(
            f"- `{e['time']}` [{e['severity']}] {e['summary']}"
            for e in events
        )
        return f"\n## {title}\n{rows}\n"

    content = f"""# JARVIS Session Journal
**Date:** {_SESSION_START.strftime('%Y-%m-%d')}
**Start:** {_SESSION_START.strftime('%H:%M:%S')}
**End:** {session_end.strftime('%H:%M:%S')}
**Duration:** {hours}h {minutes}m
**Total Events:** {len(_events)}

{('## Session Summary\n' + llm_summary + '\n') if llm_summary else ''}
{_section('Operator Commands', commands)}
{_section('Security Incidents', incidents)}
{_section('Canary / ETW Detections', detections)}
{_section('BAS Simulations', bas_events)}
{_section('Coverage Gaps Detected', gaps)}
{_section('Other Events',
    [e for e in _events if e not in incidents + detections
     + commands + bas_events + gaps]
)}
---
*Generated by JARVIS v44.0 Session Journal*
"""

    filepath.write_text(content, encoding="utf-8")
    logger.info(f"SESSION_JOURNAL: written → {filename}")
    return str(filepath)
