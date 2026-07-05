"""
core/telegram_bridge.py — JARVIS Telegram mobile command interface (v45.0).

Bidirectional Telegram bridge:
  PUSH (JARVIS → Phone):
    Critical incidents, CVE alerts, hunt findings,
    sensor disconnections, ARES approvals needed.

  PULL (Phone → JARVIS):
    /status   — system health + active operations
    /brief    — full intelligence briefing (text)
    /hunt     — trigger manual threat hunt
    /agents   — list connected sensor agents
    /gaps     — coverage gaps summary
    /hud      — screenshot of AURA HUD
    /campaigns— active threat campaigns
    /help     — command list

Security: only the operator's chat_id receives responses.
All other chat_ids are silently ignored — no auth errors
that would reveal the bot exists.

Requires:
  JARVIS_TELEGRAM_TOKEN   — from @BotFather
  JARVIS_TELEGRAM_CHAT_ID — your personal Telegram user ID
"""

import asyncio, os
from datetime import datetime
from loguru import logger

_TOKEN   = os.getenv("JARVIS_TELEGRAM_TOKEN", "")
_CHAT_ID = int(os.getenv("JARVIS_TELEGRAM_CHAT_ID", "0"))
_app     = None   # telegram.Application singleton
_bot_ref = None   # telegram.Bot for push notifications
_consent = None   # core.ironman_mode.SessionConsent — V62.0 Phase 6
_state   = None   # core.assistant_state.AssistantState — V62.0 Phase 8


async def start_telegram_bridge(broadcast_fn, tts=None, consent=None, state=None) -> None:
    """
    Start the Telegram bot in background.
    Handles both polling and push notifications.

    ``consent`` (core.ironman_mode.SessionConsent): gates /hud's screenshot
    capture — a remote chat_id whitelist is authentication, not consent.
    ``state`` (core.assistant_state.AssistantState): gates push_alert()'s
    proactive notifications by the live AssistantMode.
    """
    global _app, _bot_ref, _consent, _state
    _consent = consent
    _state = state

    if not _TOKEN or not _CHAT_ID:
        logger.info(
            "TELEGRAM: disabled — set JARVIS_TELEGRAM_TOKEN "
            "and JARVIS_TELEGRAM_CHAT_ID to enable"
        )
        await asyncio.Event().wait()   # sleep forever, watchdog stays happy
        return

    try:
        from telegram.ext import Application, CommandHandler

        _app = (
            Application.builder()
            .token(_TOKEN)
            .build()
        )
        _bot_ref = _app.bot

        # Register command handlers
        handlers = {
            "start":     _cmd_start,
            "status":    _cmd_status,
            "brief":     _cmd_brief,
            "hunt":      _cmd_hunt,
            "agents":    _cmd_agents,
            "gaps":      _cmd_gaps,
            "hud":       _cmd_hud,
            "campaigns": _cmd_campaigns,
            "help":      _cmd_help,
        }
        for cmd, handler in handlers.items():
            _app.add_handler(CommandHandler(cmd, handler))

        logger.info(
            f"TELEGRAM: bot active — "
            f"chat_id={_CHAT_ID} "
            f"commands: {list(handlers.keys())}"
        )

        # Send startup notification
        await _push(
            "🟢 *JARVIS ONLINE*\n"
            f"Platform: v45.0 PROMETHEUS\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}\n"
            "Type /help for commands.",
            parse_mode="Markdown",
        )

        # Start polling (non-blocking)
        await _app.initialize()
        await _app.start()
        await _app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )

        logger.info("TELEGRAM: polling active")

    except Exception as e:
        logger.warning(f"TELEGRAM: startup error: {e}")


async def _push(
    text: str,
    parse_mode: str = "Markdown",
    photo_path: str = "",
) -> None:
    """
    Push a message or photo to the operator's Telegram.
    Never raises — if Telegram is unavailable, silently logs.
    """
    if not _bot_ref or not _CHAT_ID:
        return
    try:
        if photo_path:
            with open(photo_path, "rb") as f:
                await _bot_ref.send_photo(
                    chat_id   = _CHAT_ID,
                    photo     = f,
                    caption   = text[:1024],
                    parse_mode= parse_mode,
                )
        else:
            await _bot_ref.send_message(
                chat_id    = _CHAT_ID,
                text       = text[:4096],
                parse_mode = parse_mode,
            )
    except Exception as e:
        logger.debug(f"TELEGRAM: push error: {e}")


def _auth(update) -> bool:
    """Silently reject unauthorized users."""
    return update.effective_chat.id == _CHAT_ID


async def _cmd_start(update, context) -> None:
    if not _auth(update): return
    await update.message.reply_text(
        "🔴 JARVIS PROMETHEUS ONLINE\nType /help",
        parse_mode="Markdown",
    )


async def _cmd_status(update, context) -> None:
    if not _auth(update): return
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=1)
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage(".")

        # Active operations
        from core.cancel_bus import get_active_operations
        ops = get_active_operations()

        # Sensor agents
        try:
            from core.sensor_mesh import get_connected_agents
            agents = len(get_connected_agents())
        except Exception:
            agents = 0

        # Active campaigns
        try:
            from core.red_team_operator import ares_operator
            campaigns = len(ares_operator.get_active_campaigns())
        except Exception:
            campaigns = 0

        msg = (
            f"*JARVIS STATUS* — {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"🖥 CPU: `{cpu:.0f}%`\n"
            f"💾 RAM: `{ram.used/1e9:.1f}/{ram.total/1e9:.0f} GB`\n"
            f"💿 Disk: `{disk.free/1e9:.0f} GB free`\n\n"
            f"⚡ Active ops: `{', '.join(ops.keys()) or 'none'}`\n"
            f"🌐 Sensor agents: `{agents}`\n"
            f"⚔ ARES campaigns: `{campaigns}`\n"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_brief(update, context) -> None:
    if not _auth(update): return
    try:
        from core.purple_coordinator import get_coverage_summary
        from core.session_journal    import _events

        cov     = get_coverage_summary()
        n_events= len(_events)

        # Last 5 significant events
        recent = "\n".join(
            f"  `{e['time']}` {e['summary'][:50]}"
            for e in _events[-5:][::-1]
        ) or "  None yet"

        msg = (
            f"*INTELLIGENCE BRIEF*\n\n"
            f"📊 Coverage: `{cov.get('coverage_pct',0)}%` "
            f"({cov.get('gaps',0)} gaps)\n"
            f"⏱ MTTD: `{cov.get('mttd_ms',0):.0f}ms`\n"
            f"📝 Session events: `{n_events}`\n\n"
            f"*Recent Activity:*\n{recent}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_hunt(update, context) -> None:
    if not _auth(update): return
    await update.message.reply_text(
        "🔍 Initiating threat hunt… results in ~60s"
    )
    try:
        from core.hunt_scheduler import run_single_hunt
        # Run one hypothesis and report back
        result = await run_single_hunt(hypothesis_index=0)
        findings = result.get("findings", [])
        n = len(findings)
        msg = (
            f"*HUNT COMPLETE* — "
            f"{result.get('hypothesis','?')[:50]}\n\n"
            f"Findings: `{n}`\n"
            + ("\n".join(f"  • {f[:60]}" for f in findings[:5])
               if findings else "  No findings.")
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Hunt error: {e}")


async def _cmd_agents(update, context) -> None:
    if not _auth(update): return
    try:
        from core.sensor_mesh import get_connected_agents
        agents = get_connected_agents()
        if not agents:
            await update.message.reply_text("No sensor agents connected.")
            return
        lines = "\n".join(
            f"  🟢 `{a.get('hostname','?')}` "
            f"({a.get('ip','?')}) — "
            f"{a.get('events_received',0)} events"
            for a in agents
        )
        await update.message.reply_text(
            f"*SENSOR MESH* — {len(agents)} agents\n{lines}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_gaps(update, context) -> None:
    if not _auth(update): return
    try:
        from core.purple_coordinator import get_coverage_matrix
        gaps = [r for r in get_coverage_matrix() if r["tier"] == "GAP"]
        if not gaps:
            await update.message.reply_text("✅ No coverage gaps detected.")
            return
        lines = "\n".join(
            f"  🔴 `{g['technique']}` — {g['attacks']} attacks undetected"
            for g in gaps[:10]
        )
        await update.message.reply_text(
            f"*COVERAGE GAPS* — {len(gaps)} total\n{lines}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_hud(update, context) -> None:
    if not _auth(update): return
    if _consent is None or not _consent.screen:
        await update.message.reply_text(
            "Screen access isn't enabled for this session — say "
            "'enable screen access' to JARVIS locally to allow it."
        )
        return
    await update.message.reply_text("📸 Capturing AURA HUD…")
    try:
        from core.vision_engine import _capture_screen, _save_screenshot
        import asyncio
        loop  = asyncio.get_running_loop()
        data  = await loop.run_in_executor(None, _capture_screen)
        path  = _save_screenshot(data, "telegram_hud")
        await _push(
            f"AURA HUD — {datetime.now().strftime('%H:%M:%S')}",
            photo_path=str(path),
        )
    except Exception as e:
        await update.message.reply_text(f"Screenshot error: {e}")


async def _cmd_campaigns(update, context) -> None:
    if not _auth(update): return
    try:
        from core.intel_fusion import get_active_campaigns_summary
        campaigns = await get_active_campaigns_summary()
        if not campaigns:
            await update.message.reply_text("No tracked campaigns.")
            return
        lines = "\n".join(
            f"  ⚔ `{c['id']}` — {c['target']} — "
            f"{c['technique_count']} techniques — "
            f"{c['incident_count']} incidents"
            for c in campaigns[:5]
        )
        await update.message.reply_text(
            f"*THREAT CAMPAIGNS*\n{lines}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_help(update, context) -> None:
    if not _auth(update): return
    await update.message.reply_text(
        "*JARVIS PROMETHEUS COMMANDS*\n\n"
        "/status   — system health\n"
        "/brief    — intelligence brief\n"
        "/hunt     — run threat hunt\n"
        "/agents   — sensor mesh status\n"
        "/gaps     — coverage gaps\n"
        "/hud      — screenshot AURA\n"
        "/campaigns— threat campaigns\n"
        "/help     — this message",
        parse_mode="Markdown",
    )


async def push_alert(
    alert_type: str,
    message: str,
    severity: str = "HIGH",
) -> None:
    """
    Push a security alert to the operator's phone.
    Called from the broadcast pipeline for critical events.

    V62.0 Phase 8 — gated by the live AssistantMode (core.assistant_state):
    PASSIVE suppresses everything proactive; FOCUS/PRESENTATION suppress
    routine notifications but still let CRITICAL alerts through; ACTIVE/
    WAR_ROOM allow both. No state wired (state=None, e.g. a caller that
    never went through start_telegram_bridge) fails open — push as before,
    so this is purely additive when unconfigured.
    """
    if _state is not None:
        from core.presence import mode_permits_notification, urgency_from_severity
        if not mode_permits_notification(
            _state.mode, _consent, urgency_from_severity(severity)
        ):
            logger.debug(
                f"TELEGRAM: push suppressed (mode={_state.mode.value}, "
                f"severity={severity}): {alert_type}"
            )
            return

    icon = {"CRITICAL": "🔴", "HIGH": "🟠",
            "MEDIUM": "🟡", "INFO": "🟢"}.get(severity, "⚪")

    await _push(
        f"{icon} *{alert_type.upper()}*\n{message[:400]}",
        parse_mode="Markdown",
    )


async def stop_telegram_bridge() -> None:
    """Clean shutdown of Telegram bot."""
    global _app
    if _app:
        try:
            await _push("🔴 JARVIS OFFLINE — shutting down.")
            await _app.updater.stop()
            await _app.stop()
            await _app.shutdown()
        except Exception:
            pass
