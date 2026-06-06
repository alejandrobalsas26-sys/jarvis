"""
core/decoy_service.py — JARVIS V51.0 DECEPTION GRID
Protocol-emulating honeypots. Binds decoy listeners on free service ports, sends a
believable banner, captures the first client bytes (truncated in logs), and raises
a T1046/T1190 alert with the source IP (feeds network_quarantine via the existing
correlator hook). Local/lab/gateway IPs are allowlisted. Logging only — no real
protocol stack, nothing exploitable here.
"""
from __future__ import annotations
import asyncio, ipaddress, json, logging, os, time
from pathlib import Path

logger = logging.getLogger("jarvis.decoy_service")

try:
    import psutil; _PSUTIL_OK = True
except Exception:
    psutil = None; _PSUTIL_OK = False

_LAB_SUBNET = os.environ.get("JARVIS_LAB_SUBNET", "192.168.1.0/24")
_LOG_PATH = Path("logs/decoy_service.jsonl")
_ALERT_TTL = 600
_CAPTURE = 512
_alerted = {}

_SERVICES = {
    8081:  ("http-admin", b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=\"admin\"\r\nServer: nginx\r\n\r\n"),
    5432:  ("postgres",   b"R\x00\x00\x00\x08\x00\x00\x00\x05"),
    6379:  ("redis",      b"-NOAUTH Authentication required.\r\n"),
    1521:  ("oracle-tns", b"\x00\x00\x00\x10\x00\x00\x00\x00"),
    11211: ("memcached",  b"ERROR\r\n"),
}
_PORTS = [int(p) for p in os.environ.get("JARVIS_DECOY_PORTS",
          ",".join(str(p) for p in _SERVICES)).split(",") if p.strip().isdigit()]
_servers = []


def _local_ips():
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


def _gateway():
    try:
        return {str(next(ipaddress.ip_network(_LAB_SUBNET, strict=False).hosts()))}
    except Exception:
        return set()


def _allowlisted(ip):
    if ip in _local_ips() or ip in _gateway():
        return True
    try:
        a = ipaddress.ip_address(ip)
        return a.is_loopback or a.is_multicast or a.is_unspecified
    except ValueError:
        return True


def _audit(rec):
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


async def _dispatch(correlator, event):
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
            logger.error("decoy_service: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("decoy_service: dispatch failed: %s", e)


async def _alert(correlator, ip, port, label, captured):
    now = time.time()
    if now - _alerted.get((ip, port), 0) < _ALERT_TTL:
        return
    _alerted[(ip, port)] = now
    safe = captured[:120].hex()
    logger.warning("DECOY_SERVICE: %s probed %s:%d (%s)", ip, ip, port, label)
    event = {"source": "decoy_service", "type": "honeypot_interaction", "severity": 9.0,
             "src_ip": ip, "ip": ip, "decoy_port": port, "service": label,
             "captured_hex": safe, "attck": ["T1046", "T1190"], "ts": now}
    _audit({"ip": ip, "port": port, "service": label, "captured_hex": safe, "ts": now})
    await _dispatch(correlator, event)


def _make_handler(port, label, banner, correlator):
    async def _handle(reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = peer[0] if isinstance(peer, (tuple, list)) else "?"
        try:
            if _allowlisted(ip):
                writer.close(); return
            try:
                writer.write(banner); await writer.drain()
            except Exception:
                pass
            try:
                data = await asyncio.wait_for(reader.read(_CAPTURE), timeout=10)
            except Exception:
                data = b""
            await _alert(correlator, ip, port, label, data or b"")
        except Exception as e:
            logger.debug("decoy_service: handler %s:%d: %s", ip, port, e)
        finally:
            try:
                writer.close()
            except Exception:
                pass
    return _handle


async def start(correlator=None):
    loop = asyncio.get_running_loop()
    bound = []
    for port in _PORTS:
        label, banner = _SERVICES.get(port, ("tcp", b""))
        try:
            srv = await asyncio.start_server(
                _make_handler(port, label, banner, correlator), host="0.0.0.0", port=port)
            _servers.append(srv); bound.append(port)
        except OSError as e:
            logger.info("decoy_service: port %d unavailable (%s) — skipped", port, e)
        except Exception as e:
            logger.debug("decoy_service: bind %d: %s", port, e)
    if not bound:
        logger.warning("DECOY_SERVICE: no decoy ports bound — dormant")
        await asyncio.Event().wait(); return
    logger.info("DECOY_SERVICE: armed — honeypots on %s", bound)
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
