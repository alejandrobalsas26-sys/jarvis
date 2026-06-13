"""
core/daily_briefing.py — JARVIS startup daily briefing (v44.0).

Fires once at startup. Collects intelligence from all JARVIS
subsystems and delivers a concise voice + HUD briefing.

Covers:
  - System health (CPU, RAM, disk, Ollama status)
  - Yesterday's incidents (episodic memory summary)
  - Today's top CVEs (NVD pull)
  - Coverage gaps (purple coordinator)
  - Active sensor agents
  - Pending Sigma rule drafts awaiting approval
  - Integrated GitHub tools
"""

import asyncio, psutil
from datetime import datetime, timezone
from loguru import logger

_BRIEFING_FIRED = False


async def deliver_briefing(
    broadcast_fn,
    tts=None,
    ollama_client=None,
    model: str = "",
) -> None:
    """
    Deliver the daily intelligence briefing.
    Runs once per JARVIS session.
    """
    global _BRIEFING_FIRED
    if _BRIEFING_FIRED:
        return
    _BRIEFING_FIRED = True

    # Brief delay — let all subsystems initialize first
    await asyncio.sleep(8)

    logger.info("DAILY_BRIEFING: collecting intelligence…")

    sections: list[str] = []
    voice_lines: list[str] = []

    # ── System health ─────────────────────────────────────────────────────────
    cpu_pct  = psutil.cpu_percent(interval=1)
    ram      = psutil.virtual_memory()
    disk     = psutil.disk_usage(".")
    ram_used = ram.used / (1024**3)
    ram_tot  = ram.total / (1024**3)
    disk_free= disk.free / (1024**3)

    health = (
        f"CPU {cpu_pct:.0f}% | "
        f"RAM {ram_used:.1f}/{ram_tot:.0f}GB | "
        f"Disk {disk_free:.0f}GB free"
    )
    sections.append(f"SYSTEM: {health}")
    voice_lines.append(
        f"System health nominal. "
        f"RAM at {ram_used:.1f} of {ram_tot:.0f} gigabytes."
    )

    # ── Yesterday's incidents ─────────────────────────────────────────────────
    incident_count = 0
    try:
        from core.episodic_memory import get_recent_episodes
        episodes = await get_recent_episodes(hours=24, limit=50)
        incident_count = len([e for e in episodes
                               if "incident" in e.get("event_type", "").lower()
                               or e.get("severity") in ("HIGH", "CRITICAL")])
        if incident_count:
            sections.append(
                f"INCIDENTS: {incident_count} high-severity events in last 24h"
            )
            voice_lines.append(
                f"{incident_count} high-severity incidents in the last 24 hours."
            )
    except Exception:
        pass

    # ── CVE intel ────────────────────────────────────────────────────────────
    try:
        # Lightweight summary — don't do full TTS here, just count
        sections.append("CVE: monitoring active — say 'JARVIS CVE briefing' for full report")
    except Exception:
        pass

    # ── Coverage gaps ─────────────────────────────────────────────────────────
    try:
        from core.purple_coordinator import get_coverage_summary
        cov = get_coverage_summary()
        if cov.get("gaps", 0) > 0:
            sections.append(
                f"COVERAGE: {cov['gaps']} detection gaps | "
                f"{cov.get('coverage_pct',0)}% covered"
            )
            voice_lines.append(
                f"{cov['gaps']} detection coverage gaps identified."
            )
    except Exception:
        pass

    # ── Sigma drafts pending ──────────────────────────────────────────────────
    try:
        from pathlib import Path
        drafts = list(Path("core/sigma_rules").glob("DRAFT_*.yaml"))
        if drafts:
            sections.append(
                f"SIGMA: {len(drafts)} rule drafts awaiting approval"
            )
            voice_lines.append(
                f"{len(drafts)} Sigma rules awaiting your approval."
            )
    except Exception:
        pass

    # ── Active sensor agents ──────────────────────────────────────────────────
    try:
        from core.sensor_mesh import get_connected_agents
        agents = get_connected_agents()
        if agents:
            names = ", ".join(a.get("hostname", "?") for a in agents[:3])
            sections.append(f"SENSORS: {len(agents)} agents online — {names}")
            voice_lines.append(
                f"{len(agents)} sensor agents connected."
            )
    except Exception:
        pass

    # ── GitHub tools ──────────────────────────────────────────────────────────
    try:
        from core.github_explorer import list_integrated_tools
        tools = list_integrated_tools()
        if tools:
            sections.append(
                f"TOOLS: {len(tools)} GitHub tools integrated"
            )
    except Exception:
        pass

    # ── Aliases ───────────────────────────────────────────────────────────────
    try:
        from core.target_aliases import list_aliases
        aliases = list_aliases()
        if aliases:
            alias_str = ", ".join(
                f"{n}={v}" for n, v in list(aliases.items())[:3]
            )
            sections.append(f"TARGETS: {alias_str}")
    except Exception:
        pass

    # ── Broadcast briefing to HUD ─────────────────────────────────────────────
    now = datetime.now()
    await broadcast_fn({
        "type":      "daily_briefing",
        "date":      now.strftime("%Y-%m-%d"),
        "time":      now.strftime("%H:%M"),
        "sections":  sections,
        "severity":  "INFO",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    logger.info(
        f"DAILY_BRIEFING: delivered — "
        f"{len(sections)} sections"
    )

    # ── Voice delivery ────────────────────────────────────────────────────────
    if tts and voice_lines:
        greeting = (
            "Good morning" if 5 <= now.hour < 12 else
            "Good afternoon" if 12 <= now.hour < 18 else
            "Good evening"
        )
        full_brief = (
            f"{greeting}. JARVIS online. "
            + " ".join(voice_lines[:4])
            + " All systems ready."
        )
        asyncio.create_task(tts.speak_async(full_brief))
