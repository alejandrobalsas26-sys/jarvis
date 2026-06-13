"""
core/punisher.py — Active Defense / Punisher Mode (v46.0).

When severity >= 9.0: JARVIS auto-executes defensive actions.

SAFE (auto-execute, reversible):
  - Network isolation: block hostile IP via Windows Firewall
  - JARVIS announces via TTS before executing

DESTRUCTIVE (requires NATO OTP via AURA):
  - Process suspension: suspend malicious thread in memory

All actions logged to logs/punisher_actions.jsonl
"""

import asyncio, subprocess, json
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_LOG_PATH        = Path("logs/punisher_actions.jsonl")
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

_PUNISHER_ENABLED = True   # set False to disable globally
_AUTO_THRESHOLD   = 9.0    # severity >= this triggers auto-execute
_BLOCKED_IPS: set[str] = set()


def _log_action(action: str, target: str, success: bool,
                detail: str = "") -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action":    action,
        "target":    target,
        "success":   success,
        "detail":    detail,
    }
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info(f"PUNISHER: {action} → {target} — "
                f"{'OK' if success else 'FAIL'} {detail}")


async def isolate_ip(ip: str, reason: str = "") -> bool:
    """
    Block a hostile IP via Windows Firewall (both directions).
    Reversible: undo_isolation(ip) removes the rules.
    """
    if ip in _BLOCKED_IPS:
        logger.debug(f"PUNISHER: {ip} already blocked")
        return True

    safe_ip = ip.replace(".", "_")
    cmds = [
        f'netsh advfirewall firewall add rule '
        f'name="JARVIS_BLOCK_{safe_ip}_OUT" '
        f'dir=out action=block remoteip={ip} enable=yes',

        f'netsh advfirewall firewall add rule '
        f'name="JARVIS_BLOCK_{safe_ip}_IN" '
        f'dir=in action=block remoteip={ip} enable=yes',
    ]
    try:
        for cmd in cmds:
            result = subprocess.run(
                cmd, shell=True, capture_output=True,
                text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr)

        _BLOCKED_IPS.add(ip)
        _log_action("isolate_ip", ip, True, reason[:80])
        return True

    except Exception as e:
        _log_action("isolate_ip", ip, False, str(e)[:80])
        return False


async def undo_isolation(ip: str) -> bool:
    """Remove firewall block for an IP."""
    safe_ip = ip.replace(".", "_")
    cmds = [
        f'netsh advfirewall firewall delete rule '
        f'name="JARVIS_BLOCK_{safe_ip}_OUT"',
        f'netsh advfirewall firewall delete rule '
        f'name="JARVIS_BLOCK_{safe_ip}_IN"',
    ]
    try:
        for cmd in cmds:
            subprocess.run(cmd, shell=True, timeout=10)
        _BLOCKED_IPS.discard(ip)
        _log_action("undo_isolation", ip, True)
        return True
    except Exception as e:
        _log_action("undo_isolation", ip, False, str(e)[:80])
        return False


async def punisher_response(
    incident: dict,
    tts,
    broadcast_fn,
) -> None:
    """
    Auto-execute defensive actions for high-severity incident.
    Called from correlator when severity >= _AUTO_THRESHOLD.
    """
    if not _PUNISHER_ENABLED:
        return

    severity = incident.get("severity_score", 0)
    if severity < _AUTO_THRESHOLD:
        return

    hosts = list(incident.get("involved_hosts", set()))
    hostile_ips = [
        str(h) for h in hosts
        if str(h) not in ("127.0.0.1", "::1", "localhost")
        and not str(h).startswith("192.168.1.")
        # Don't block local lab IPs automatically
    ]

    if not hostile_ips:
        logger.debug("PUNISHER: no external IPs to block")
        return

    await broadcast_fn({
        "type":      "punisher_activated",
        "severity":  severity,
        "targets":   hostile_ips,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if tts:
        asyncio.create_task(tts.speak_async(
            f"Punisher mode activated. "
            f"Severity {severity:.0f}. "
            f"Isolating {len(hostile_ips)} hostile endpoint"
            f"{'s' if len(hostile_ips) > 1 else ''}."
        ))

    results = []
    for ip in hostile_ips[:5]:  # cap at 5 IPs per incident
        success = await isolate_ip(
            ip,
            reason=incident.get("kill_chain_phase", "")
        )
        results.append((ip, success))
        await asyncio.sleep(0.5)

    blocked = [ip for ip, ok in results if ok]
    failed  = [ip for ip, ok in results if not ok]

    summary = f"Blocked: {len(blocked)}"
    if failed:
        summary += f". Failed: {len(failed)}"

    logger.warning(f"PUNISHER: response complete — {summary}")

    await broadcast_fn({
        "type":      "punisher_complete",
        "blocked":   blocked,
        "failed":    failed,
        "severity":  "CRITICAL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if tts and blocked:
        asyncio.create_task(tts.speak_async(
            f"Done. {len(blocked)} IP"
            f"{'s' if len(blocked) > 1 else ''} isolated. "
            "You can review the action log."
        ))
