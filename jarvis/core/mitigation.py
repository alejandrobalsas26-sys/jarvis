"""core/mitigation.py — Non-blocking SOAR engine with TTL-based firewall isolation.

Infrastructure safety: RFC1918 private and loopback ranges are NEVER isolated.
Isolation gate: entropy AND-gate with critical MITRE technique check before any action.
"""

import asyncio
import ipaddress
from datetime import datetime, timezone

from loguru import logger

from core.config import settings
from core.events import make_event

_PRIVATE_NETWORKS: list[ipaddress.IPv4Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]

_CRITICAL_TECHNIQUES: frozenset[str] = frozenset({
    "T1059.001",  # PowerShell / Script Execution
    "T1055",      # Process Injection
    "T1562.001",  # Security Software Tampering
    "T1036",      # Masquerading
})


def _is_public_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return not any(addr in net for net in _PRIVATE_NETWORKS) and not addr.is_loopback
    except ValueError:
        return False


def should_isolate(triage: dict) -> list[str]:
    """Return public IPs to isolate, or [] if AND-gate conditions are not met."""
    entropy = triage.get("entropy", 0.0)
    if entropy < settings.entropy_threshold:
        return []

    detections   = triage.get("mitre_detections", [])
    detected_ids = {d.get("technique", "") for d in detections}
    if not detected_ids & _CRITICAL_TECHNIQUES:
        return []

    return [ip for ip in triage.get("extracted_ips", []) if _is_public_ip(ip)]


async def isolate_ip(ip: str, broadcast_fn, ttl_minutes: int = 60) -> None:
    """Block outbound traffic to ip via Windows Defender Firewall with TTL auto-expiry."""
    from core.telemetry_auth import make_signed_broadcaster
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "mitigation")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        logger.warning(f"mitigation: invalid IP '{ip}' — isolation aborted")
        return

    rule_name     = f"JARVIS_BLOCK_{ip}"
    sleep_seconds = ttl_minutes * 60
    ps_cmd = (
        f"New-NetFirewallRule -DisplayName '{rule_name}' "
        f"-Direction Outbound -Action Block -RemoteAddress {ip}; "
        f"Start-Job -ScriptBlock {{Start-Sleep -Seconds {sleep_seconds}; "
        f"Remove-NetFirewallRule -DisplayName '{rule_name}'}}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            logger.info(f"mitigation: firewall rule added — BLOCK {ip} (TTL={ttl_minutes}m)")
            await broadcast_fn(make_event(
                "firewall_block",
                isolated_ip=ip,
                ttl_minutes=ttl_minutes,
                rule_name=rule_name,
            ))
        else:
            err = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(
                f"mitigation: PowerShell returned {proc.returncode} for {ip} — {err[:200]}"
            )
    except FileNotFoundError:
        logger.warning("mitigation: powershell.exe not found — firewall isolation unavailable")
    except Exception as exc:
        logger.warning(f"mitigation: isolation failed for {ip} — {exc}")
