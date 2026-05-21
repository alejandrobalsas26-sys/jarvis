"""core/canary.py — Multi-port async honeypot matrix (Windows-hardened).

Port selection avoids kernel-locked Windows services:
  - 445 (SMB/lanmanserver) → 8445
  - 22  (OpenSSH)          → 2222
  - Port 21 requires elevation on Windows; skipped gracefully via pre-bind check.
"""

import asyncio
import socket
from datetime import datetime, timezone

from loguru import logger

_CANARY_PORTS: dict[int, str] = {
    21:   "FTP",
    2222: "SSH-ALT",
    8445: "SMB-DECOY",
    3389: "RDP-DECOY",
    1433: "MSSQL-DECOY",
}


def _port_available(port: int) -> bool:
    """Return True if the port is not already bound or locked on this host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


async def canary_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    port: int,
    broadcast_fn,
    tool_executor_ref=None,
    llm_client_ref=None,
) -> None:
    peer = writer.get_extra_info("peername")
    attacker_ip = peer[0] if peer else "unknown"
    service = _CANARY_PORTS.get(port, "UNKNOWN")

    try:
        banner = await asyncio.wait_for(reader.read(256), timeout=1.0)
    except (asyncio.TimeoutError, Exception):
        banner = b""

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    logger.warning(
        f"CANARY HIT: port={port} ({service}) src={attacker_ip} "
        f"banner={banner[:48]!r}"
    )
    asyncio.create_task(broadcast_fn({
        "type":           "canary_intrusion",
        "port":           port,
        "service":        service,
        "attacker_ip":    attacker_ip,
        "banner_preview": banner[:64].decode("utf-8", errors="replace"),
        "timestamp":      datetime.now(timezone.utc).isoformat(),
    }))

    # High-confidence detection: attacker sent a meaningful banner (>16 bytes).
    # Trigger live forensic capture + agentic SOC loop to freeze malicious state
    # before any anti-forensic cleanup can occur.
    banner_hex = banner.hex()
    if banner_hex and len(banner_hex) > 32:
        try:
            from core.config import settings
            from tools.forensic_volatility import trigger_forensic_capture
            if settings.vmx_target_path:
                asyncio.create_task(
                    trigger_forensic_capture(settings.vmx_target_path, broadcast_fn),
                    name="forensic-capture",
                )
        except ImportError:
            pass

        if tool_executor_ref is not None and llm_client_ref is not None:
            try:
                from core.agentic_loop import run_agentic_incident
                asyncio.create_task(
                    run_agentic_incident(
                        trigger_event={
                            "type":        "canary_intrusion",
                            "attacker_ip": attacker_ip,
                            "port":        port,
                            "banner_hex":  banner.hex(),
                        },
                        tool_executor=tool_executor_ref,
                        broadcast_fn=broadcast_fn,
                        llm_client=llm_client_ref,
                    ),
                    name="agentic-incident",
                )
            except ImportError:
                pass


async def start_canaries(
    broadcast_fn,
    tool_executor_ref=None,
    llm_client_ref=None,
) -> None:
    """Bind honeypot listeners on all available ports and serve indefinitely."""
    servers = []

    for port, service in _CANARY_PORTS.items():
        if not _port_available(port):
            logger.info(f"CANARY: port {port} ({service}) unavailable — skipped")
            continue
        try:
            # CRITICAL CLOSURE FIX: freeze port + refs into lambda via default keyword args
            server = await asyncio.start_server(
                lambda r, w, p=port, te=tool_executor_ref, lc=llm_client_ref: (
                    canary_handler(r, w, p, broadcast_fn, te, lc)
                ),
                "0.0.0.0",
                port,
            )
            servers.append(server)
            logger.info(f"CANARY: listening on :{port} ({service})")
        except Exception as exc:
            logger.warning(f"CANARY: failed to bind port {port} ({service}) — {exc}")

    if not servers:
        logger.warning("CANARY: no ports could be bound — honeypot matrix offline")
        return

    await asyncio.gather(*[s.serve_forever() for s in servers])
