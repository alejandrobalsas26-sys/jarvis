"""
tools/active_tarpit.py — Active deception TCP tarpit (v31.0).

Traps network scanners on ports commonly probed but NOT covered by the
canary matrix. Sends a realistic protocol banner, then drips random bytes
at 10-15 second intervals — keeping the scanner's connection open for
minutes and poisoning its timeout budget.

Tarpit ports (do NOT overlap with canaries on 21/2222/8445/3389/1433):
  4444  — common reverse-shell / C2 port
  5900  — VNC decoy
  8080  — HTTP proxy / admin panel decoy
  9200  — Elasticsearch decoy
  27017 — MongoDB decoy

CPU profile: pure asyncio I/O — near-zero CPU per trapped connection.
"""

import asyncio
import random
from datetime import datetime, timezone

from loguru import logger

from core.telemetry_auth import make_signed_broadcaster

# Realistic opening banners per port — scanners parse these
_BANNERS = {
    4444:  b"220 ProFTPD Server ready\r\n",
    5900:  b"RFB 003.008\n",
    8080:  b"HTTP/1.1 200 OK\r\nServer: Apache/2.4.41\r\n\r\n",
    9200:  b'{"name":"node-1","cluster_name":"jarvis","version":{"number":"8.0.0"}}\n',
    27017: b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xd4\x07\x00\x00",
}

# Maximum time to keep a single trapped connection alive (seconds)
_MAX_TRAP_DURATION = 600   # 10 minutes max per connection

_TARPIT_PORTS = [4444, 5900, 8080, 9200, 27017]


def _make_handler(port: int, broadcast_fn):
    """
    Factory returning a proper async coroutine function for this port.
    asyncio.start_server requires async def, not lambda.
    """
    async def _handler(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        addr = writer.get_extra_info("peername")
        ip   = addr[0] if addr else "unknown"

        logger.warning(f"TARPIT: scanner trapped — {ip} → TCP/{port}")
        await broadcast_fn({
            "type":        "tarpit_trap",
            "attacker_ip": ip,
            "port":        port,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "severity":    "WARNING",
        })

        # v32.0 — fire-and-forget IP geolocation for AURA globe markers
        try:
            from tools.geo_resolver import resolve_ip
            asyncio.create_task(resolve_ip(ip, broadcast_fn))
        except Exception:
            pass

        # v37.0 — fire-and-forget OSINT enrichment (Shodan/VT/OTX/ipinfo)
        try:
            from tools.osint_engine import enrich_ip
            asyncio.create_task(enrich_ip(ip, broadcast_fn))
        except Exception:
            pass

        try:
            # Send realistic banner to fool the scanner
            banner = _BANNERS.get(port, b"220 Service ready\r\n")
            writer.write(banner)
            await writer.drain()

            # Slow-drip garbage until scanner gives up or max duration
            loop = asyncio.get_event_loop()
            deadline = loop.time() + _MAX_TRAP_DURATION
            while loop.time() < deadline:
                junk = bytes([random.randint(0x20, 0x7E)
                              for _ in range(random.randint(1, 8))])
                writer.write(junk)
                await writer.drain()
                await asyncio.sleep(random.uniform(10.0, 15.0))

        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass   # scanner disconnected — normal outcome
        except Exception as e:
            logger.debug(f"TARPIT: {ip}:{port} — {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    return _handler   # returns a proper async def, not a lambda


async def start_tarpit(broadcast_fn) -> None:
    """
    Start tarpit servers on all decoy ports.
    Silent failure per port — a port already in use is skipped gracefully.
    """
    signed_bcast = make_signed_broadcaster(broadcast_fn, "mitigation")
    servers: list[asyncio.base_events.Server] = []

    for port in _TARPIT_PORTS:
        try:
            srv = await asyncio.start_server(
                _make_handler(port, signed_bcast),
                host  = "0.0.0.0",
                port  = port,
                limit = 1024,      # small read buffer — we never read from traps
            )
            servers.append(srv)
            logger.info(f"TARPIT: deception active on TCP/{port}")
        except OSError as e:
            logger.debug(f"TARPIT: port {port} unavailable: {e}")
        except Exception as e:
            logger.debug(f"TARPIT: could not bind {port}: {e}")

    if not servers:
        logger.warning("TARPIT: no ports bound — all may be in use")
        return

    async with asyncio.TaskGroup() as tg:
        for srv in servers:
            tg.create_task(srv.serve_forever())
