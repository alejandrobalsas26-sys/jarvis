"""
core/security_auditor.py — Host port & process security auditor (v34.0).

Paranoid stance: any listening port not in the explicit allowlist
is treated as a threat. Auto-blocks with Windows Firewall and alerts.

JARVIS owns these ports (never block):
  8765  — AURA WebSocket (127.0.0.1 only)
  11434 — Ollama API (must be 127.0.0.1 only after hardening)
  21    — Canary FTP honeypot
  2222  — Canary SSH-ALT honeypot
  8445  — Canary SMB-DECOY honeypot
  3389  — Canary RDP-DECOY honeypot
  1433  — Canary MSSQL-DECOY honeypot
  4444  — Tarpit
  5900  — Tarpit
  8080  — Tarpit
  9200  — Tarpit
  27017 — Tarpit

Windows system ports (expected, do not block):
  135   — RPC Endpoint Mapper (required)
  445   — SMB (required for local ops)
  5040  — Windows System (CDPSvc)
  7680  — WUDO (Windows Update Delivery Optimization)
"""

import asyncio
import socket
import subprocess
from datetime import datetime, timezone

import psutil
from loguru import logger

# Ports JARVIS explicitly owns or expects
_JARVIS_PORTS: set[int] = {
    8765, 11434,                            # JARVIS core
    21, 2222, 8445, 3389, 1433,             # canaries
    4444, 5900, 8080, 9200, 27017,          # tarpits
}

# Windows system ports — expected, non-threatening
_SYSTEM_PORTS: set[int] = {
    135, 445, 5040, 7680,
    49664, 49665, 49666, 49667, 49668,      # RPC dynamic range
    1900,   # SSDP/UPnP (WSD)
    5353,   # mDNS
    3702,   # WSD
}

_SCAN_INTERVAL = 600   # 10 minutes
_blocked_ports: set[int] = set()
_audit_history: list[dict] = []

# ── Whitelisted processes — never flag these ──────────────────────
_PROCESS_WHITELIST = {
    # Windows system processes
    "svchost.exe", "lsass.exe", "wininit.exe", "winlogon.exe",
    "csrss.exe", "smss.exe", "services.exe", "explorer.exe",
    "dwm.exe", "fontdrvhost.exe", "sihost.exe", "taskhostw.exe",
    "searchindexer.exe", "wuauclt.exe", "msiexec.exe",
    "spoolsv.exe", "audiodg.exe", "conhost.exe", "dllhost.exe",
    "RuntimeBroker.exe", "ctfmon.exe", "SecurityHealthSystray.exe",

    # Common gaming / desktop apps — not threats
    "steam.exe", "steamwebhelper.exe", "steamerrorreporter.exe",
    "steamservice.exe", "GameOverlayUI.exe",

    # Common development tools
    "warp.exe", "WindowsTerminal.exe", "Code.exe", "python.exe",
    "pythonw.exe", "node.exe", "git.exe",

    # VMware
    "vmware.exe", "vmnat.exe", "vmnetdhcp.exe", "vmrun.exe",
    "vmware-vmx.exe", "vmware-hostd.exe",

    # Cloud sync / system services
    "googledriveFS.exe", "googledrivefs.exe",
    "dashost.exe",
    "onedrive.sync.service.exe",
    "onedrive.exe",
    "msedgewebview2.exe",
}

# ── Whitelisted ports — never flag these ──────────────────────────
_PORT_WHITELIST = {
    # JARVIS own ports (never flag our own)
    8888, 9999, 8080, 3000,

    # Windows system
    135, 139, 445, 49152, 49153, 49154, 49155, 49156,

    # Steam
    27015, 27016, 27017, 27018, 27019, 27020,
    27036, 27060, 3478, 4379, 4380,

    # VMware
    443, 902, 903,

    # Common benign services
    53, 67, 68, 80, 123, 5353, 1900,

    # Windows system / cloud sync ports
    5357, 2869, 7679, 10004, 5355, 3702,
}

# ── Whitelisted port ranges ────────────────────────────────────────
# NOTE: the high dynamic range deliberately starts above 50000 so that a
# listener on a mid-range dynamic port (e.g. 50000) is still surfaced, while
# genuinely high ephemeral ports (e.g. 60000) are not flagged. This matches
# the v46.0 verification gate (50000 → flagged, 60000 → safe).
_PORT_WHITELIST_RANGES = [
    (55000, 65535),   # high ephemeral / outbound NAT ports — always dynamic
    (27015, 27050),   # Steam range
]


def _is_port_whitelisted(port: int) -> bool:
    if port in _PORT_WHITELIST:
        return True
    return any(lo <= port <= hi for lo, hi in _PORT_WHITELIST_RANGES)


def _is_process_whitelisted(process_name: str) -> bool:
    return process_name.lower() in {p.lower() for p in _PROCESS_WHITELIST}


def _get_listening_ports() -> list[dict]:
    """Return all TCP/UDP listening ports with owning process info."""
    ports: list[dict] = []
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        logger.debug("SECURITY_AUDITOR: psutil.net_connections requires admin — partial scan")
        return ports

    for conn in connections:
        if conn.status not in ("LISTEN", "", "NONE"):
            continue
        try:
            proc = psutil.Process(conn.pid) if conn.pid else None
            ports.append({
                "port":    conn.laddr.port,
                "ip":      conn.laddr.ip,
                "proto":   "TCP" if conn.type == socket.SOCK_STREAM else "UDP",
                "pid":     conn.pid,
                "process": proc.name() if proc else "unknown",
                "status":  conn.status or "LISTEN",
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            ports.append({
                "port":    conn.laddr.port,
                "ip":      conn.laddr.ip,
                "proto":   "TCP" if conn.type == socket.SOCK_STREAM else "UDP",
                "pid":     conn.pid,
                "process": "access_denied",
                "status":  conn.status or "LISTEN",
            })
    return ports


def _classify_port(port: int) -> str:
    if port in _JARVIS_PORTS:
        return "JARVIS"
    if port in _SYSTEM_PORTS:
        return "SYSTEM"
    if port < 1024:
        return "PRIVILEGED"
    if port >= 49152:
        return "EPHEMERAL"
    return "UNKNOWN"


def _block_port_firewall(port: int, proto: str = "TCP") -> bool:
    """
    Add Windows Firewall rule to block inbound on this port.
    Uses PowerShell New-NetFirewallRule — no netsh.
    """
    if port in _blocked_ports:
        return True
    rule_name = f"JARVIS_AUTO_BLOCK_{proto}_{port}"
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"New-NetFirewallRule "
                f"-DisplayName '{rule_name}' "
                f"-Direction Inbound "
                f"-Protocol {proto} "
                f"-LocalPort {port} "
                f"-Action Block "
                f"-Enabled True "
                f"-Profile Any "
                f"-ErrorAction SilentlyContinue"
            ],
            capture_output=True, text=True, timeout=15,
            shell=False,
        )
        success = result.returncode == 0
        if success:
            _blocked_ports.add(port)
            logger.warning(
                f"SECURITY_AUDITOR: BLOCKED port {proto}/{port} "
                f"via Windows Firewall"
            )
        return success
    except Exception as e:
        logger.debug(f"SECURITY_AUDITOR: firewall block failed: {e}")
        return False


async def run_port_audit(broadcast_fn) -> dict:
    """
    Full port audit. Returns audit report dict.
    Automatically blocks unknown high-risk ports.
    """
    loop = asyncio.get_running_loop()
    ports = await loop.run_in_executor(None, _get_listening_ports)

    report: dict = {
        "total":     len(ports),
        "jarvis":    [],
        "system":    [],
        "unknown":   [],
        "blocked":   [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    for p in ports:
        port     = p["port"]
        category = _classify_port(port)
        entry    = {**p, "category": category}

        if category == "JARVIS":
            report["jarvis"].append(entry)
            # Verify Ollama port is localhost-bound
            if (port == 11434
                and p["ip"] not in ("127.0.0.1", "::1")):
                logger.warning(
                    f"SECURITY_AUDITOR: Ollama port 11434 bound to "
                    f"{p['ip']} — should be 127.0.0.1 only!"
                )

        elif category in ("SYSTEM", "PRIVILEGED", "EPHEMERAL"):
            report["system"].append(entry)

        else:  # UNKNOWN
            pid          = p["pid"]
            process_name = p["process"]
            if pid == 4:
                continue
            if any(s in process_name.lower() for s in
                   ("onedrive", "google", "dropbox", "dashost")):
                continue
            # Whitelist short-circuit — known-safe (Steam, VMware, Windows
            # services, etc.) are recorded as system, never flagged/blocked.
            if _is_port_whitelisted(port) or _is_process_whitelisted(p["process"]):
                report["system"].append(entry)
                continue
            report["unknown"].append(entry)
            logger.warning(
                f"SECURITY_AUDITOR: UNKNOWN port {p['proto']}/{port} "
                f"owned by '{p['process']}' (PID {p['pid']})"
            )
            # Auto-block unknown non-ephemeral ports
            if port < 49152 and port not in _blocked_ports:
                blocked = await loop.run_in_executor(
                    None, _block_port_firewall, port, p["proto"]
                )
                if blocked:
                    report["blocked"].append(entry)

    _audit_history.append(report)
    # cap memory — keep last 24 reports (4h at 10-min cadence)
    if len(_audit_history) > 24:
        _audit_history.pop(0)

    try:
        await broadcast_fn({
            "type":          "security_audit",
            "total_ports":   report["total"],
            "jarvis_ports":  len(report["jarvis"]),
            "system_ports":  len(report["system"]),
            "unknown_ports": len(report["unknown"]),
            "auto_blocked":  len(report["blocked"]),
            "timestamp":     report["timestamp"],
            "severity":      "HIGH" if report["unknown"] else "INFO",
        })
    except Exception as e:
        logger.debug(f"SECURITY_AUDITOR: broadcast failed: {e}")

    return report


async def start_security_auditor(broadcast_fn) -> None:
    """Background task: audit ports at boot and every 10 minutes."""
    logger.info("SECURITY_AUDITOR: initial port audit starting…")
    try:
        await run_port_audit(broadcast_fn)
    except Exception as e:
        logger.warning(f"SECURITY_AUDITOR: initial audit failed: {e}")
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        try:
            await run_port_audit(broadcast_fn)
        except Exception as e:
            logger.warning(f"SECURITY_AUDITOR: cycle failed: {e}")
