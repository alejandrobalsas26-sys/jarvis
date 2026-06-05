"""
core/tarpit_deception.py — JARVIS V50.0 NEXUS
Endlessh-style TCP tarpit + deception. Binds decoy listeners on attractive ports
and, on connect, holds the inbound socket open, dribbling one random byte every
15s to hang scanners/brute tools. Each new non-allowlisted source IP raises a
Sev 9.0 T1046 alert (feeding network_quarantine). Passive trap on the local host
only — it never initiates anything toward the peer.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger("jarvis.tarpit_deception")

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

# --- Config ------------------------------------------------------------------
_PORTS = [int(p) for p in os.environ.get(
    "JARVIS_TARPIT_PORTS", "22,445,3389,2222,8022,1433").split(",") if p.strip().isdigit()]
_DRIP_SECONDS = 15
_MAX_CONNS = 500
_ALERT_TTL = 600
_LAB_SUBNET = os.environ.get("JARVIS_LAB_SUBNET", "192.168.1.0/24")
_LOG_PATH = Path("logs/tarpit_deception.jsonl")

_active = 0
_alerted: dict = {}             # ip -> ts
_servers: list = []


def _local_ips() -> set:
    ips = {"127.0.0.1", "::1", "0.0.0.0"}
    if _PSUTIL_OK:
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if getattr(a, "address", None):
                        ips.add(a.address.split("%")[0])
        except Exception:
            pass
    return ips


def _gateway() -> set:
    gws = set()
    try:
        gws.add(str(next(ipaddress.ip_network(_LAB_SUBNET, strict=False).hosts())))
    except Exception:
        pass
    return gws


def _is_allowlisted(ip: str) -> bool:
    if ip in _local_ips() or ip in _gateway():
        return True
    try:
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_multicast or a.is_unspecified:
            return True
    except ValueError:
        return True
    return False


def _audit(rec: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


async def _alert(correlator, ip: str, port: int) -> None:
    now = time.time()
    last = _alerted.get(ip, 0)
    if now - last < _ALERT_TTL:
        return
    _alerted[ip] = now
    logger.warning("TARPIT_DECEPTION: scanner %s hit decoy port %d", ip, port)
    event = {"source": "tarpit_deception", "type": "tarpit_connection",
             "severity": 9.0, "src_ip": ip, "ip": ip, "decoy_port": port,
             "attck": ["T1046"], "ts": now}
    if correlator is None:
        return
    try:
        if hasattr(correlator, "ingest_event"):
            await correlator.ingest_event(event)
        elif hasattr(correlator, "add_event"):
            r = correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
        else:
            logger.error("tarpit_deception: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("tarpit_deception: alert dispatch failed: %s", e)


def _make_handler(port: int, correlator):
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        global _active
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = peer[0] if isinstance(peer, (tuple, list)) else "?"
        started = time.time()
        try:
            if _is_allowlisted(ip):
                writer.close()
                return
            if _active >= _MAX_CONNS:
                await _alert(correlator, ip, port)
                writer.close()
                return
            _active += 1
            await _alert(correlator, ip, port)
            _audit({"ev": "open", "ip": ip, "port": port, "ts": started})
            # Endlessh-style: hold the socket, dribble one random byte every 15s.
            while True:
                try:
                    writer.write(bytes([random.randint(0, 255)]))
                    await writer.drain()
                except Exception:
                    break
                await asyncio.sleep(_DRIP_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("tarpit_deception: handler error %s:%d: %s", ip, port, e)
        finally:
            held = round(time.time() - started, 1)
            try:
                writer.close()
            except Exception:
                pass
            if not _is_allowlisted(ip):
                _active = max(0, _active - 1)
                _audit({"ev": "close", "ip": ip, "port": port, "held_s": held})
    return _handle


async def start(correlator=None) -> None:
    """main.py startup hook. Watchdog Pattern: dormant if no decoy port can be
    bound (e.g., 445/3389 already owned by the OS are skipped)."""
    global _servers
    loop = asyncio.get_running_loop()
    bound = []
    for port in _PORTS:
        try:
            srv = await asyncio.start_server(_make_handler(port, correlator),
                                             host="0.0.0.0", port=port)
            _servers.append(srv)
            bound.append(port)
        except OSError as e:
            logger.info("tarpit_deception: port %d unavailable (%s) — skipped", port, e)
        except Exception as e:
            logger.debug("tarpit_deception: bind %d failed: %s", port, e)
    if not bound:
        logger.warning("TARPIT_DECEPTION: no decoy ports could be bound — dormant")
        await asyncio.Event().wait(); return
    logger.info("TARPIT_DECEPTION: armed — decoy listeners on %s", bound)
    try:
        await asyncio.gather(*(s.serve_forever() for s in _servers))
    except asyncio.CancelledError:
        pass
    finally:
        for s in _servers:
            try:
                s.close()
            except Exception:
                pass
