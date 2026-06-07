"""
core/arp_deception.py — JARVIS V53.0 SHADOW
L2 dark-space deception + ARP scan detection (LaBrea/honeyd pattern). Sniffs ARP in
a daemon thread (scapy is blocking; never touches the loop directly — bridges via
run_coroutine_threadsafe, shared state under a Lock). Detects host-discovery sweeps
(T1046). For OPERATOR-DECLARED decoy space only, answers with a SYNTHETIC
locally-administered MAC to plant phantom hosts.

SAFETY BY CONSTRUCTION (this is deception of EMPTY space, not interception):
- Injects nothing unless JARVIS_DECOY_IP_RANGE is set (DETECT-ONLY by default).
- Never answers for the gateway, our own IPs, or any IP ever seen live.
- Synthetic locally-administered MAC only — never impersonates a real device.
- Never poisons the gateway or a real host's cache; never redirects real traffic.
"""
from __future__ import annotations
import asyncio, hashlib, ipaddress, logging, os, threading, time
from collections import deque

logger = logging.getLogger("jarvis.arp_deception")

try:
    from scapy.all import ARP, Ether, sniff, sendp, conf
    _SCAPY_OK = True
except Exception:
    _SCAPY_OK = False

_IS_WINDOWS = os.name == "nt"
_LAB_SUBNET = os.environ.get("JARVIS_LAB_SUBNET", "192.168.1.0/24")
_DECOY_RANGE = os.environ.get("JARVIS_DECOY_IP_RANGE", "").strip()
_SCAN_WINDOW = 30
_SCAN_DISTINCT = 10
_ALERT_TTL = 120
_REPLY_TTL = 30

_correlator = None
_loop = None
_stop = threading.Event()
_lock = threading.Lock()
_live = set()
_scan_by_src = {}
_alerted = {}
_replied = {}
_OUR = set()
_GW = None


def _is_admin():
    if not _IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _parse_decoy_range():
    if not _DECOY_RANGE:
        return None
    try:
        if "-" in _DECOY_RANGE:
            a, b = _DECOY_RANGE.split("-", 1)
            return (int(ipaddress.ip_address(a.strip())), int(ipaddress.ip_address(b.strip())))
        net = ipaddress.ip_network(_DECOY_RANGE, strict=False)
        return (int(net.network_address), int(net.broadcast_address))
    except Exception as e:
        logger.debug("arp: decoy range parse: %s", e)
        return None


_DECOY = _parse_decoy_range()


def _in_decoy(ip):
    if not _DECOY:
        return False
    try:
        v = int(ipaddress.ip_address(ip))
        return _DECOY[0] <= v <= _DECOY[1]
    except Exception:
        return False


def _synth_mac(ip):
    h = hashlib.sha256(ip.encode("utf-8", "ignore")).digest()
    o = [(h[0] & 0xFE) | 0x02, h[1], h[2], h[3], h[4], h[5]]   # locally-administered, unicast
    return ":".join("%02x" % b for b in o)


def _our_ips():
    ips = set()
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if getattr(a, "address", None):
                    ips.add(a.address.split("%")[0])
    except Exception:
        pass
    return ips


def _gateway():
    try:
        return str(next(ipaddress.ip_network(_LAB_SUBNET, strict=False).hosts()))
    except Exception:
        return None


async def _dispatch(event):
    logger.warning("ARP_DECEPTION: %s", {k: event[k] for k in event if k not in ("source", "ts")})
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
        logger.error("arp_deception: dispatch failed: %s", e)


def _emit(kind, sev, attck, extra):
    now = time.time()
    key = (kind, extra.get("src_ip") or extra.get("decoy_ip") or "?")
    if now - _alerted.get(key, 0) < _ALERT_TTL:
        return
    _alerted[key] = now
    event = {"source": "arp_deception", "type": kind, "severity": sev, "attck": attck, "ts": now}
    event.update(extra)
    if _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_dispatch(event), _loop)
        except Exception:
            pass


def _send_reply(req_pkt, pdst):
    try:
        mac = _synth_mac(pdst)
        eth = Ether(dst=req_pkt[Ether].src, src=mac)
        rep = ARP(op=2, psrc=pdst, hwsrc=mac, pdst=req_pkt[ARP].psrc, hwdst=req_pkt[ARP].hwsrc)
        sendp(eth / rep, verbose=0, iface=conf.iface)
        _emit("arp_decoy_reply", 7.0, ["T1046"],
              {"decoy_ip": pdst, "synthetic_mac": mac, "to": req_pkt[ARP].psrc})
    except Exception as e:
        logger.debug("arp: send reply: %s", e)


def _on_pkt(pkt):
    if _stop.is_set():
        return
    try:
        if not pkt.haslayer(ARP):
            return
        arp = pkt[ARP]
        psrc, pdst, op = arp.psrc, arp.pdst, arp.op
        if psrc and psrc != "0.0.0.0":
            with _lock:
                _live.add(psrc)
        if op != 1:
            return
        now = time.time()
        with _lock:
            dq = _scan_by_src.setdefault(psrc, deque())
            dq.append((now, pdst))
            while dq and now - dq[0][0] > _SCAN_WINDOW:
                dq.popleft()
            distinct = {t for _ts, t in dq}
            is_live = pdst in _live
        if len(distinct) >= _SCAN_DISTINCT:
            _emit("arp_scan", 9.5, ["T1046"], {"src_ip": psrc, "distinct_targets": len(distinct)})
        if (_DECOY and _in_decoy(pdst) and not is_live and pdst != _GW and pdst not in _OUR):
            if now - _replied.get(pdst, 0) >= _REPLY_TTL:
                _replied[pdst] = now
                _send_reply(pkt, pdst)
    except Exception as e:
        logger.debug("arp: pkt error: %s", e)


def _sniff_thread():
    try:
        sniff(filter="arp", prn=_on_pkt, store=0, stop_filter=lambda p: _stop.is_set())
    except Exception as e:
        logger.error("arp_deception: sniff thread crashed: %s", e)


async def start(correlator=None):
    global _correlator, _loop, _OUR, _GW
    _correlator = correlator; _loop = asyncio.get_running_loop()
    if not _SCAPY_OK:
        logger.warning("ARP_DECEPTION: scapy/npcap unavailable — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("ARP_DECEPTION: not elevated — dormant")
        await asyncio.Event().wait(); return
    _OUR = _our_ips(); _GW = _gateway()
    _stop.clear()
    threading.Thread(target=_sniff_thread, name="arp-sniff", daemon=True).start()
    mode = ("decoy+detect (range %s)" % _DECOY_RANGE) if _DECOY \
        else "DETECT-ONLY (set JARVIS_DECOY_IP_RANGE to enable phantom hosts)"
    logger.info("ARP_DECEPTION: armed — %s", mode)
    try:
        await asyncio.Event().wait()
    finally:
        _stop.set()
