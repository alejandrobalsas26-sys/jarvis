"""
core/industrial_asset_guard.py — JARVIS V54.0 OMEGA
Two-part defensive module:

(1) Live L2/L3 topology graph — passive ARP/IP sniffing in a daemon thread builds
    {mac: {ip, oui, first_seen, last_seen, flows}}. Alerts on rogue devices (new
    MAC) and on flows to known ICS/OT ports (Modbus/502, S7/102, DNP3/20000,
    EtherNet-IP/44818, BACnet/47808, Niagara-Fox/1911) — anomalous on a standard
    segment. Topology JSON is written to logs/topology.json for dashboard/HUD use.

(2) CAD / engineering macro guard — watchdog on configured directories monitors
    AutoCAD .lsp / .fas / .scr (AutoLISP), Blender .py addons (only flagged when
    under blender/scripts/addons paths), and .dvb/.dvs CAD VBA. Flags known
    weaponization tokens (vlax-create-object, vl-load-com, URLDownloadToFile,
    subprocess/os.system in addons, base64 blobs, certutil/powershell shellouts).
    Compiled .fas dropped in a user-writable path is itself suspicious.

Watchdog Pattern: topology dormant if scapy/npcap missing or non-admin; CAD guard
runs independently.
"""
from __future__ import annotations
import asyncio, json, logging, os, re, threading, time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("jarvis.industrial_asset_guard")

try:
    from scapy.all import sniff, ARP, IP, TCP, UDP
    _SCAPY_OK = True
except Exception:
    _SCAPY_OK = False

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except Exception:
    Observer = None; FileSystemEventHandler = object; _WATCHDOG_OK = False

_IS_WINDOWS = os.name == "nt"
_CAD_DIRS = [d for d in os.environ.get("JARVIS_CAD_DIRS",
             str(Path.home() / "Documents")).split(os.pathsep) if d.strip()]
_TOPO_PATH = Path("logs/topology.json")
_TOPO_WRITE_SECS = 30
_ALERT_TTL = 300
_OT_PORTS = {502: "Modbus", 102: "S7comm", 20000: "DNP3", 44818: "EtherNet-IP",
             47808: "BACnet", 1911: "Niagara-Fox"}
_CAD_EXT = {".lsp", ".fas", ".scr", ".dvb", ".dvs"}
_BLENDER_HINTS = ("blender", "scripts", "addons")
_CAD_RED = re.compile(
    r"vlax-create-object|vl-load-com|URLDownloadToFile|WScript\.Shell|"
    r"subprocess|os\.system|urllib\.request|FromBase64|powershell|cmd\.exe|"
    r"command\s+\"\s*_open|certutil|[A-Za-z0-9+/]{120,}={0,2}", re.IGNORECASE)

_topo_lock = threading.Lock()
_hosts = {}
_alerted = {}
_correlator = None
_loop = None
_stop = threading.Event()


def _is_admin():
    if not _IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


async def _dispatch(event):
    logger.warning("INDUSTRIAL: %s", {k: event[k] for k in event if k not in ("source", "ts")})
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
        logger.error("industrial: dispatch failed: %s", e)


def _emit(kind, sev, attck, extra):
    now = time.time()
    key = (kind, extra.get("mac") or extra.get("src_ip") or extra.get("file_path") or "?")
    if now - _alerted.get(key, 0) < _ALERT_TTL:
        return
    _alerted[key] = now
    event = {"source": "industrial_asset_guard", "type": kind, "severity": sev,
             "attck": attck, "ts": now}
    event.update(extra)
    if _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_dispatch(event), _loop)
        except Exception:
            pass


def _on_pkt(pkt):
    if _stop.is_set():
        return
    try:
        mac = ip = None
        if pkt.haslayer(ARP):
            a = pkt[ARP]; mac = (a.hwsrc or "").lower(); ip = a.psrc
        elif pkt.haslayer(IP):
            ip = pkt[IP].src
            try:
                mac = (pkt.src or "").lower()
            except Exception:
                mac = None
        if not mac or not ip or ip in ("0.0.0.0", "255.255.255.255"):
            return
        now = time.time()
        was_new = False
        with _topo_lock:
            h = _hosts.get(mac)
            if h is None:
                h = {"ip": ip, "oui": mac[:8].upper(), "first_seen": now,
                     "last_seen": now, "flows": defaultdict(int)}
                _hosts[mac] = h
                was_new = True
            else:
                h["ip"] = ip; h["last_seen"] = now
        port = proto = None
        if pkt.haslayer(TCP):
            port = int(pkt[TCP].dport); proto = "tcp"
        elif pkt.haslayer(UDP):
            port = int(pkt[UDP].dport); proto = "udp"
        if port in _OT_PORTS:
            with _topo_lock:
                _hosts[mac]["flows"][f"{_OT_PORTS[port]}/{proto}"] += 1
            _emit("ot_protocol_flow", 9.0, ["T0888", "T1046"],
                  {"src_ip": ip, "mac": mac, "port": port, "proto": _OT_PORTS[port]})
        if was_new:
            _emit("rogue_device", 9.0, ["T1200", "T1046"],
                  {"mac": mac, "ip": ip, "oui": mac[:8].upper()})
    except Exception as e:
        logger.debug("topology: pkt: %s", e)


def _sniff_thread():
    try:
        sniff(filter="arp or ip", prn=_on_pkt, store=0,
              stop_filter=lambda p: _stop.is_set())
    except Exception as e:
        logger.error("industrial: sniff crashed: %s", e)


async def _topology_writer():
    while not _stop.is_set():
        try:
            with _topo_lock:
                snap = {"ts": time.time(),
                        "hosts": [{"mac": m, "ip": v["ip"], "oui": v["oui"],
                                   "first_seen": v["first_seen"], "last_seen": v["last_seen"],
                                   "flows": dict(v["flows"])}
                                  for m, v in _hosts.items()]}
            _TOPO_PATH.parent.mkdir(parents=True, exist_ok=True)
            _TOPO_PATH.write_text(json.dumps(snap, default=str, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug("topology write: %s", e)
        await asyncio.sleep(_TOPO_WRITE_SECS)


def _cad_scan(path):
    try:
        p = Path(path)
    except Exception:
        return None
    suf = p.suffix.lower()
    if suf not in _CAD_EXT and suf != ".py":
        return None
    low = str(p).lower()
    if suf == ".py" and not any(h in low for h in _BLENDER_HINTS):
        return None
    try:
        if p.stat().st_size > 4_000_000:
            return None
        data = p.read_bytes().decode("utf-8", "ignore")
    except Exception:
        return None
    hits = list(set(m.group(0) for m in _CAD_RED.finditer(data)))[:8]
    if hits:
        return {"reasons": [f"suspicious token: {h[:60]}" for h in hits]}
    if suf == ".fas":
        return {"reasons": ["compiled AutoLISP (.fas) in user-writable path"]}
    return None


class _CADHandler(FileSystemEventHandler):
    def __init__(self, loop):
        self._loop = loop
    def _go(self, path):
        r = _cad_scan(path)
        if not r:
            return
        event = {"source": "industrial_asset_guard", "type": "cad_macro_anomaly",
                 "severity": 9.0, "file_path": path, "reasons": r["reasons"],
                 "attck": ["T1059.005", "T1218", "T1204.002"], "ts": time.time()}
        try:
            asyncio.run_coroutine_threadsafe(_dispatch(event), self._loop)
        except Exception:
            pass
    def on_created(self, e):
        if not e.is_directory:
            self._go(e.src_path)
    def on_modified(self, e):
        if not e.is_directory:
            self._go(e.src_path)


async def start(correlator=None):
    global _correlator, _loop
    _correlator = correlator; _loop = asyncio.get_running_loop()
    topology_armed = False; observer = None; started_cad = []
    if _SCAPY_OK and _is_admin():
        _stop.clear()
        threading.Thread(target=_sniff_thread, name="topology-sniff", daemon=True).start()
        asyncio.create_task(_topology_writer())
        topology_armed = True
        logger.info("INDUSTRIAL: topology sensor armed — live L2/L3 map → %s", _TOPO_PATH)
    else:
        logger.warning("INDUSTRIAL: topology dormant (scapy/admin missing) — CAD guard continues")
    if _WATCHDOG_OK:
        observer = Observer(); h = _CADHandler(_loop)
        for d in _CAD_DIRS:
            try:
                if os.path.isdir(d):
                    observer.schedule(h, d, recursive=True); started_cad.append(d)
            except Exception:
                pass
        if started_cad:
            observer.start()
            logger.info("INDUSTRIAL: CAD/macro guard armed — %s", started_cad)
        else:
            observer = None
            logger.info("INDUSTRIAL: CAD guard found no valid dirs (set JARVIS_CAD_DIRS)")
    else:
        logger.warning("INDUSTRIAL: watchdog lib missing — CAD guard off")
    if not topology_armed and observer is None:
        logger.warning("INDUSTRIAL_ASSET_GUARD: nothing armed — dormant")
        await asyncio.Event().wait(); return
    try:
        await asyncio.Event().wait()
    finally:
        _stop.set()
        if observer:
            try:
                observer.stop(); observer.join(timeout=5)
            except Exception:
                pass
