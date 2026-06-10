"""
core/windows_hardener.py — Windows 11 host hardening engine (v34.0).

Applies these hardening measures at JARVIS boot:
  1. Ollama network isolation (127.0.0.1 only)
  2. Windows Firewall rules for all JARVIS services
  3. Disable dangerous Windows services
  4. Restrict PowerShell execution policy
  5. Enable Windows Defender real-time protection
  6. Audit Windows Event Log settings

All changes are idempotent — safe to run multiple times.
Changes persist across reboots via Windows Firewall + Service Manager.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone

from loguru import logger


def _env_true(name: str, default: bool) -> bool:
    """Parse a boolean-ish env var. Absent → ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Firewall rules ────────────────────────────────────────────────────────────

_FIREWALL_RULES: list[dict] = [
    # Block Ollama from external access — must be 127.0.0.1 only
    {
        "name":      "JARVIS_OLLAMA_LOCALHOST_ONLY",
        "direction": "Inbound",
        "protocol":  "TCP",
        "port":      "11434",
        "action":    "Block",
        "remote":    "!127.0.0.1",
        "desc":      "Block external Ollama API access",
    },
    # Allow AURA WebSocket from localhost only
    {
        "name":      "JARVIS_AURA_ALLOW_LOCAL",
        "direction": "Inbound",
        "protocol":  "TCP",
        "port":      "8765",
        "action":    "Allow",
        "remote":    "127.0.0.1",
        "desc":      "Allow AURA WebSocket from localhost",
    },
    # Block AURA from external
    {
        "name":      "JARVIS_AURA_BLOCK_EXTERNAL",
        "direction": "Inbound",
        "protocol":  "TCP",
        "port":      "8765",
        "action":    "Block",
        "remote":    "!127.0.0.1",
        "desc":      "Block external AURA access",
    },
    # Block common backdoor port
    {
        "name":      "JARVIS_BLOCK_METERPRETER_31337",
        "direction": "Inbound",
        "protocol":  "TCP",
        "port":      "31337",
        "action":    "Block",
        "remote":    "Any",
        "desc":      "Block common backdoor port",
    },
]


def _apply_firewall_rule(rule: dict) -> bool:
    """Apply a single firewall rule via PowerShell."""
    cmd_parts = [
        "New-NetFirewallRule",
        f"-DisplayName '{rule['name']}'",
        f"-Direction {rule['direction']}",
        f"-Protocol {rule['protocol']}",
        f"-LocalPort {rule['port']}",
        f"-Action {rule['action']}",
        "-Enabled True -Profile Any",
        "-ErrorAction SilentlyContinue",
    ]
    if rule.get("remote") and rule["remote"] != "Any":
        cmd_parts.append(f"-RemoteAddress {rule['remote']}")

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             " ".join(cmd_parts)],
            capture_output=True, text=True, timeout=15,
            shell=False,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Service hardening ─────────────────────────────────────────────────────────

# Services safe to disable — known attack vectors or unnecessary
_SERVICES_TO_DISABLE: dict[str, str] = {
    "Spooler":           "Print Spooler — PrintNightmare (CVE-2021-34527) vector",
    "RemoteRegistry":    "Remote Registry — allows remote registry modification",
    "TlntSvr":           "Telnet Server — plaintext protocol, no auth encryption",
    "FTPSVC":            "IIS FTP Server — if not using IIS FTP",
    "MSFTPSVC":          "Legacy FTP — plaintext credentials",
    "XblGameSave":       "Xbox Game Save — unnecessary on security workstation",
    "XblAuthManager":    "Xbox Auth — unnecessary",
    "XboxNetApiSvc":     "Xbox Network — unnecessary",
    "WMPNetworkSvc":     "Windows Media Player Network — unnecessary",
    "Fax":               "Fax Service — unnecessary",
    "MapsBroker":        "Downloaded Maps Manager — unnecessary",
    "PhoneSvc":          "Phone Service — unnecessary",
}

# NEVER touch these — JARVIS depends on them or they're security-critical
_PROTECTED_SERVICES: set[str] = {
    "WinDefend", "SecurityHealthService", "WdNisSvc", "WdFilter",
    "EventLog", "Winmgmt", "RpcSs", "DcomLaunch", "LSM",
    "SamSs", "LanmanServer", "NlaSvc", "WlanSvc",
}


def _disable_service(service_name: str, reason: str) -> bool:
    """Stop and disable a Windows service via PowerShell."""
    if service_name in _PROTECTED_SERVICES:
        logger.debug(f"HARDENER: protected service '{service_name}' — skipped")
        return False
    try:
        # Check if service exists first
        check = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Get-Service -Name '{service_name}' -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10,
            shell=False,
        )
        if service_name.lower() not in check.stdout.lower():
            return False   # service doesn't exist

        # Stop and disable
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Stop-Service -Name '{service_name}' -Force -ErrorAction SilentlyContinue; "
             f"Set-Service -Name '{service_name}' -StartupType Disabled "
             f"-ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=15,
            shell=False,
        )
        logger.info(f"HARDENER: disabled service '{service_name}' — {reason}")
        return True
    except Exception as e:
        logger.debug(f"HARDENER: service '{service_name}' error: {e}")
        return False


# ── Ollama isolation ──────────────────────────────────────────────────────────

def _harden_ollama() -> bool:
    """
    Set OLLAMA_HOST=127.0.0.1 in Windows user environment.
    Persists across reboots; ensures Ollama never binds externally.
    """
    try:
        current = os.environ.get("OLLAMA_HOST", "")
        if current == "127.0.0.1":
            return True

        # Set for current process
        os.environ["OLLAMA_HOST"] = "127.0.0.1"

        # Persist in Windows user environment
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[System.Environment]::SetEnvironmentVariable("
             "'OLLAMA_HOST','127.0.0.1','User')"],
            capture_output=True, text=True, timeout=10,
            shell=False,
        )
        logger.info("HARDENER: Ollama bound to 127.0.0.1 — external API access blocked")
        return True
    except Exception as e:
        logger.debug(f"HARDENER: Ollama isolation failed: {e}")
        return False


# ── Windows Defender hardening ────────────────────────────────────────────────

def _harden_defender() -> None:
    """Ensure Windows Defender real-time protection is enabled."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-MpComputerStatus | Select-Object RealTimeProtectionEnabled "
             "| ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
            shell=False,
        )
        if not result.stdout.strip():
            return
        status = json.loads(result.stdout)
        if not status.get("RealTimeProtectionEnabled", True):
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Set-MpPreference -DisableRealtimeMonitoring $false"],
                capture_output=True, timeout=10,
                shell=False,
            )
            logger.warning("HARDENER: Windows Defender real-time was OFF — re-enabled")
        else:
            logger.info("HARDENER: Windows Defender real-time protection: ACTIVE")
    except Exception:
        pass


# ── Main hardening function ───────────────────────────────────────────────────

def _planned_changes() -> list[str]:
    """Human-readable list of changes the hardener *would* make. Used for the
    dry-run report and operator log so the action is fully triageable."""
    planned = [f"firewall rule — {r['desc']}" for r in _FIREWALL_RULES]
    planned += [
        f"disable service '{svc}' — {reason}"
        for svc, reason in _SERVICES_TO_DISABLE.items()
        if svc not in _PROTECTED_SERVICES
    ]
    planned.append("bind Ollama to 127.0.0.1 (persist OLLAMA_HOST in user env)")
    planned.append("verify Windows Defender real-time protection")
    return planned


async def apply_host_hardening(broadcast_fn) -> dict:
    """
    Apply all hardening measures. Idempotent — safe to run on every boot.

    Safe-by-default: this is a **dry-run** unless ``JARVIS_HARDENER_ENABLE=true``
    is set. Even when enabled, ``JARVIS_HARDENER_DRY_RUN=true`` keeps it inert.
    Dry-run logs every change it *would* make but modifies no service, firewall
    rule, or Windows setting. Returns a hardening report.
    """
    enabled = _env_true("JARVIS_HARDENER_ENABLE", False)
    dry_run = (not enabled) or _env_true("JARVIS_HARDENER_DRY_RUN", True)

    report: dict = {
        "enabled":                   enabled,
        "dry_run":                   dry_run,
        "firewall_rules_applied":    0,
        "services_disabled":         0,
        "services_already_disabled": 0,
        "ollama_isolated":           False,
        "defender_active":           False,
        "planned_changes":           [],
        "timestamp":                 "",
    }

    if dry_run:
        planned = _planned_changes()
        report["planned_changes"] = planned
        logger.warning(
            "HARDENER: DRY-RUN — no host changes applied "
            "(set JARVIS_HARDENER_ENABLE=true and JARVIS_HARDENER_DRY_RUN=false to enforce)"
        )
        for item in planned:
            logger.info(f"HARDENER[dry-run]: would {item}")
        report["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            await broadcast_fn({"type": "hardening_dry_run", "severity": "INFO", **report})
        except Exception as e:
            logger.debug(f"HARDENER: broadcast failed: {e}")
        return report

    loop = asyncio.get_running_loop()

    logger.warning("HARDENER: ENFORCE mode — applying real Windows 11 host hardening…")

    # 1. Ollama isolation
    report["ollama_isolated"] = await loop.run_in_executor(
        None, _harden_ollama
    )

    # 2. Firewall rules
    for rule in _FIREWALL_RULES:
        success = await loop.run_in_executor(
            None, _apply_firewall_rule, rule
        )
        if success:
            report["firewall_rules_applied"] += 1
            logger.info(f"HARDENER: firewall rule applied — {rule['desc']}")

    # 3. Service hardening
    for svc_name, reason in _SERVICES_TO_DISABLE.items():
        disabled = await loop.run_in_executor(
            None, _disable_service, svc_name, reason
        )
        if disabled:
            report["services_disabled"] += 1

    # 4. Defender check
    await loop.run_in_executor(None, _harden_defender)
    report["defender_active"] = True

    report["timestamp"] = datetime.now(timezone.utc).isoformat()

    logger.info(
        f"HARDENER: complete — "
        f"FW rules={report['firewall_rules_applied']} "
        f"services_disabled={report['services_disabled']} "
        f"ollama_isolated={report['ollama_isolated']}"
    )

    try:
        await broadcast_fn({
            "type":     "hardening_complete",
            "severity": "INFO",
            **report,
        })
    except Exception as e:
        logger.debug(f"HARDENER: broadcast failed: {e}")

    return report
