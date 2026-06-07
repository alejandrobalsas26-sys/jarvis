"""
core/dns_sinkhole.py — JARVIS V53.0 SHADOW
Local DNS sinkhole/resolver (asyncio UDP/53). Clients pointed at this resolver get
blocklisted malware/C2 domains answered with a sinkhole IP (logged, T1568);
everything else is forwarded upstream so normal DNS keeps working. Also flags
likely DNS tunneling/DGA (over-long/high-entropy labels, TXT/NULL). Pure asyncio,
non-blocking; transient timeout-bounded upstream forwarding. Watchdog Pattern:
dormant if 53 is in use or not elevated.
"""
from __future__ import annotations
import asyncio, logging, math, os, struct, time
from collections import Counter
from pathlib import Path

logger = logging.getLogger("jarvis.dns_sinkhole")

_HOST = os.environ.get("JARVIS_DNS_BIND", "0.0.0.0")
_PORT = 53
_SINKHOLE_IP = os.environ.get("JARVIS_SINKHOLE_IP", "127.0.0.1")
_UPSTREAM = os.environ.get("JARVIS_DNS_UPSTREAM", "1.1.1.1")
_UPSTREAM_PORT = 53
_BLOCKLIST_FILE = os.environ.get("JARVIS_DNS_BLOCKLIST", "config/dns_blocklist.txt")
_FWD_TIMEOUT = 4.0
_ALERT_TTL = 120

_correlator = None
_loop = None
_blocklist = set()
_alerted = {}


def _is_admin():
    if os.name != "nt":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _load_blocklist():
    global _blocklist
    s = set()
    try:
        p = Path(_BLOCKLIST_FILE)
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    s.add(line.rstrip("."))
    except Exception as e:
        logger.debug("dns: blocklist load: %s", e)
    s.update({"malware-c2.invalid", "badactor.test", "sinkhole-me.example"})  # RFC-reserved test entries
    _blocklist = s


def _parse_qname(data, offset):
    labels = []
    safety = 0
    while offset < len(data) and safety < 128:
        safety += 1
        ln = data[offset]
        if ln == 0:
            offset += 1
            break
        if ln & 0xC0 == 0xC0:
            offset += 2
            break
        offset += 1
        labels.append(data[offset:offset + ln].decode("ascii", "ignore"))
        offset += ln
    return ".".join(labels), offset


def _parse_query(data):
    if len(data) < 12:
        return None
    qd = struct.unpack("!H", data[4:6])[0]
    if qd < 1:
        return None
    qname, off = _parse_qname(data, 12)
    if off + 4 > len(data):
        return None
    qtype, _qclass = struct.unpack("!HH", data[off:off + 4])
    return {"qname": qname.lower().rstrip("."), "qtype": qtype, "qend": off + 4}


def _build_a_response(data, qend, ip):
    header = bytearray(data[:12])
    header[2] = 0x81; header[3] = 0x80
    struct.pack_into("!H", header, 6, 1)
    struct.pack_into("!H", header, 8, 0)
    struct.pack_into("!H", header, 10, 0)
    question = data[12:qend]
    try:
        rdata = bytes(int(o) for o in ip.split("."))
    except Exception:
        rdata = b"\x7f\x00\x00\x01"
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + rdata
    return bytes(header) + question + answer


def _build_rcode(data, rcode=2):
    header = bytearray(data[:12])
    header[2] = 0x81; header[3] = 0x80 | (rcode & 0x0F)
    struct.pack_into("!H", header, 6, 0)
    struct.pack_into("!H", header, 8, 0)
    struct.pack_into("!H", header, 10, 0)
    return bytes(header) + data[12:]


def _domain_blocked(qname):
    if qname in _blocklist:
        return True
    parts = qname.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in _blocklist:
            return True
    return False


def _entropy(s):
    if not s:
        return 0.0
    c = Counter(s); n = len(s)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def _suspicious_qname(qname, qtype):
    if qtype in (16, 10) and len(qname) > 50:
        return True
    for label in qname.split("."):
        if len(label) >= 30 and _entropy(label) >= 3.8:
            return True
    return False


async def _dispatch(event):
    if _correlator is None:
        return
    try:
        if hasattr(_correlator, "ingest_event"):
            await _correlator.ingest_event(event)
        elif hasattr(_correlator, "add_event"):
            r = _correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
    except Exception as e:
        logger.error("dns_sinkhole: dispatch failed: %s", e)


async def _alert(src, qname, kind, sev, attck):
    now = time.time(); key = (src, qname, kind)
    if now - _alerted.get(key, 0) < _ALERT_TTL:
        return
    _alerted[key] = now
    event = {"source": "dns_sinkhole", "type": kind, "severity": sev,
             "src_ip": src, "domain": qname, "attck": attck, "ts": now}
    logger.warning("DNS_SINKHOLE: %s %s from %s", kind, qname, src)
    await _dispatch(event)


class _ForwardProto(asyncio.DatagramProtocol):
    def __init__(self, fut):
        self.fut = fut
    def datagram_received(self, data, addr):
        if not self.fut.done():
            self.fut.set_result(data)
    def error_received(self, exc):
        if not self.fut.done():
            self.fut.set_exception(exc)


async def _forward(data):
    fut = _loop.create_future()
    try:
        transport, _ = await _loop.create_datagram_endpoint(
            lambda: _ForwardProto(fut), remote_addr=(_UPSTREAM, _UPSTREAM_PORT))
    except Exception:
        return None
    try:
        transport.sendto(data)
        return await asyncio.wait_for(fut, _FWD_TIMEOUT)
    except Exception:
        return None
    finally:
        try:
            transport.close()
        except Exception:
            pass


class _DNSProto(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, addr):
        asyncio.ensure_future(self._handle(data, addr))
    async def _handle(self, data, addr):
        try:
            q = _parse_query(data)
        except Exception:
            q = None
        if not q:
            return
        src = addr[0]; qname = q["qname"]
        try:
            if _domain_blocked(qname):
                self.transport.sendto(_build_a_response(data, q["qend"], _SINKHOLE_IP), addr)
                await _alert(src, qname, "dns_sinkhole_hit", 10.0, ["T1568"])
                return
            if _suspicious_qname(qname, q["qtype"]):
                await _alert(src, qname, "dns_tunneling_suspected", 8.5, ["T1071.004", "T1568.002"])
            fwd = await _forward(data)
            self.transport.sendto(fwd if fwd else _build_rcode(data, 2), addr)
        except Exception as e:
            logger.debug("dns: handle error: %s", e)
            try:
                self.transport.sendto(_build_rcode(data, 2), addr)
            except Exception:
                pass


async def start(correlator=None):
    global _correlator, _loop
    _correlator = correlator; _loop = asyncio.get_running_loop()
    if not _is_admin():
        logger.warning("DNS_SINKHOLE: not elevated — dormant")
        await asyncio.Event().wait(); return
    _load_blocklist()
    try:
        transport, _ = await _loop.create_datagram_endpoint(_DNSProto, local_addr=(_HOST, _PORT))
    except OSError as e:
        logger.warning("DNS_SINKHOLE: bind %s:%d failed (%s — in use?) — dormant", _HOST, _PORT, e)
        await asyncio.Event().wait(); return
    except Exception as e:
        logger.warning("DNS_SINKHOLE: start failed (%s) — dormant", e)
        await asyncio.Event().wait(); return
    logger.info("DNS_SINKHOLE: live on %s:%d — %d blocklisted domains, upstream %s",
                _HOST, _PORT, len(_blocklist), _UPSTREAM)
    try:
        await asyncio.Event().wait()
    finally:
        try:
            transport.close()
        except Exception:
            pass
