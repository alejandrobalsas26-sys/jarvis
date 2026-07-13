"""core/canary.py — Multi-port async honeypot matrix (Windows-hardened).

Port selection avoids kernel-locked Windows services:
  - 445 (SMB/lanmanserver) → 8445
  - 22  (OpenSSH)          → 2222
  - Port 21 requires elevation on Windows; skipped gracefully via pre-bind check.
"""

import asyncio
import os
import socket

from loguru import logger

from core.events import make_event

_CANARY_PORTS: dict[int, str] = {
    21:   "FTP",
    2222: "SSH-ALT",
    8445: "SMB-DECOY",
    3389: "RDP-DECOY",
    1433: "MSSQL-DECOY",
}

# V68.1 M50 — deception services default to LOCALHOST ONLY. Binding a honeypot
# matrix on 0.0.0.0 silently exposes decoy services to the whole home/public
# network. External exposure now requires an EXPLICIT operator opt-in and is
# logged with the real, proven bind address. Secure default, not a denylist.
_EXPOSE_ENV = "JARVIS_CANARY_EXPOSE"       # "1"/"true" → allow non-localhost bind
_BIND_ENV = "JARVIS_CANARY_BIND"           # explicit bind address when exposing
_DEFAULT_LOCAL_HOST = "127.0.0.1"


def _canary_bind_host() -> str:
    """Resolve the canary bind address. Localhost unless the operator explicitly
    enables authorized lab exposure. Any exposure is deliberate and auditable."""
    expose = os.environ.get(_EXPOSE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    if not expose:
        return _DEFAULT_LOCAL_HOST
    # Explicit exposure: honor an operator-specified bind address, else all-ifaces.
    host = os.environ.get(_BIND_ENV, "").strip() or "0.0.0.0"
    logger.warning(
        f"CANARY: authorized lab exposure ENABLED via {_EXPOSE_ENV} — binding {host} "
        "(decoy services reachable off-host). Ensure this is an authorized lab network."
    )
    return host


def _port_available(port: int, host: str | None = None) -> bool:
    """Return True if *port* is not already bound or locked on *host*.
    Probes the SAME address the listener will use so collision detection is real."""
    host = host or _canary_bind_host()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
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
    cognitive_engine=None,
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
    asyncio.create_task(broadcast_fn(make_event(
        "canary_intrusion",
        port=port,
        service=service,
        attacker_ip=attacker_ip,
        banner_preview=banner[:64].decode("utf-8", errors="replace"),
    )))

    # v32.0 — fire-and-forget IP geolocation for AURA globe markers
    try:
        from tools.geo_resolver import resolve_ip
        asyncio.create_task(resolve_ip(attacker_ip, broadcast_fn))
    except Exception:
        pass

    # v37.0 — fire-and-forget OSINT enrichment (Shodan/VT/OTX/ipinfo)
    try:
        from tools.osint_engine import enrich_ip
        asyncio.create_task(enrich_ip(attacker_ip, broadcast_fn))
    except Exception:
        pass

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
                        cognitive_engine=cognitive_engine,
                    ),
                    name="agentic-incident",
                )
            except ImportError:
                pass


async def start_canaries(
    broadcast_fn,
    tool_executor_ref=None,
    llm_client_ref=None,
    cognitive_engine=None,
) -> None:
    """Bind honeypot listeners on all available ports and serve indefinitely."""
    from core.telemetry_auth import make_signed_broadcaster
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "canary")

    servers = []
    bind_host = _canary_bind_host()
    logger.info(
        f"CANARY: bind scope = {bind_host} "
        f"({'localhost-only (secure default)' if bind_host == _DEFAULT_LOCAL_HOST else 'EXPOSED — authorized lab'})"
    )

    for port, service in _CANARY_PORTS.items():
        if not _port_available(port, bind_host):
            logger.info(f"CANARY: port {port} ({service}) unavailable/in use on "
                        f"{bind_host} — skipped (collision)")
            continue
        try:
            # CRITICAL CLOSURE FIX: freeze port + refs into lambda via default keyword args
            server = await asyncio.start_server(
                lambda r, w, p=port, te=tool_executor_ref, lc=llm_client_ref, \
                       ce=cognitive_engine: (
                    canary_handler(r, w, p, broadcast_fn, te, lc, ce)
                ),
                bind_host,
                port,
            )
            servers.append(server)
            # Log the REAL, proven bind address from the socket — never assume.
            try:
                real = server.sockets[0].getsockname()
                real_str = f"{real[0]}:{real[1]}"
            except Exception:
                real_str = f"{bind_host}:{port}"
            logger.info(f"CANARY: listening on {real_str} ({service})")
        except Exception as exc:
            logger.warning(f"CANARY: failed to bind port {port} ({service}) on "
                           f"{bind_host} — {exc}")

    if not servers:
        logger.warning("CANARY: no ports could be bound — honeypot matrix offline")
        return

    await asyncio.gather(*[s.serve_forever() for s in servers])
