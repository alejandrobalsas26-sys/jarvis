"""
core/c2_dashboard.py — JARVIS V52.0 AEGIS
Local tactical SOC dashboard. aiohttp server bound to 127.0.0.1 serving a single
dark-themed page over WebSocket; streams correlator events (severity > 5.0),
quarantine actions, DLP findings and health telemetry in real time. READ-ONLY (no
action endpoints); all event text is HTML-escaped client-side (textContent).
"""
from __future__ import annotations
import asyncio, json, logging, os, time
from collections import deque

logger = logging.getLogger("jarvis.c2_dashboard")

try:
    from aiohttp import web, WSMsgType
    _AIOHTTP_OK = True
except Exception:
    web = None; WSMsgType = None; _AIOHTTP_OK = False

_HOST = os.environ.get("JARVIS_DASHBOARD_HOST", "127.0.0.1")
_PORT = int(os.environ.get("JARVIS_DASHBOARD_PORT", "8787"))
_MIN_SEV = 5.0
_BUFFER = deque(maxlen=200)
_clients = set()
_loop = None
_PASSTHROUGH = {"host_quarantined", "sensitive_data_exposure", "decoy_breadcrumb",
                "decoy_tamper", "honeypot_interaction", "health_alert", "health_status"}

_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>JARVIS AEGIS C2</title>
<style>
body{background:#0a0e14;color:#c8d3e0;font-family:Consolas,Monaco,monospace;margin:0}
header{padding:12px 18px;background:#0d1420;border-bottom:1px solid #1f2a3a;display:flex;gap:24px;align-items:center}
h1{font-size:16px;margin:0;color:#4ec9b0;letter-spacing:2px}
.stat{font-size:13px}.stat b{color:#fff;font-size:18px}
#health{padding:8px 18px;font-size:12px;border-bottom:1px solid #1f2a3a;display:flex;flex-wrap:wrap;gap:12px}
.hk{padding:2px 8px;border-radius:3px;background:#11283a}
.ok{color:#8ec843}.bad{color:#ff6b6b}
#feed{padding:0 18px}
.row{padding:8px 0;border-bottom:1px solid #141c28;display:flex;gap:14px;align-items:baseline}
.sev{min-width:42px;text-align:center;border-radius:3px;padding:1px 6px;font-weight:bold}
.s-crit{background:#5a1620;color:#ff6b6b}.s-hi{background:#5a3a16;color:#ffb454}.s-md{background:#16385a;color:#6ab0ff}
.t{color:#4ec9b0;min-width:190px}.src{color:#7e8aa0;min-width:150px}.sum{color:#aeb9c9}
.ts{color:#5a6678;min-width:90px}
</style></head><body>
<header><h1>JARVIS // AEGIS C2</h1>
<div class="stat">EVENTS <b id="c_total">0</b></div>
<div class="stat">CRITICAL <b id="c_crit">0</b></div>
<div class="stat">CONTAINMENT <b id="c_q">0</b></div>
<div class="stat" id="link"></div></header>
<div id="health">awaiting health telemetry...</div>
<div id="feed"></div>
<script>
var total=0,crit=0,quar=0;
function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}
function sevClass(s){if(s>=9)return 's-crit';if(s>=7)return 's-hi';return 's-md';}
function fmtTs(t){try{return new Date(t*1000).toLocaleTimeString();}catch(e){return '';}}
function renderHealth(rec){
  var h=document.getElementById('health');h.innerHTML='';
  (rec.checks||[]).forEach(function(c){
    var d=el('span','hk');d.appendChild(el('span',c.ok?'ok':'bad',(c.ok?'OK ':'X ')+c.name));
    d.appendChild(document.createTextNode(' '+(c.info||'')));h.appendChild(d);});
  var sup=rec.supervised||{};Object.keys(sup).forEach(function(k){
    var s=sup[k];var d=el('span','hk');
    d.appendChild(el('span',s.alive?'ok':'bad',(s.alive?'UP ':'DOWN ')+k));
    if(s.restarts)d.appendChild(document.createTextNode(' r='+s.restarts));h.appendChild(d);});
}
function addRow(rec){
  if(rec.type==='health_status'){renderHealth(rec);return;}
  total++;document.getElementById('c_total').textContent=total;
  if(rec.severity>=9){crit++;document.getElementById('c_crit').textContent=crit;}
  if(rec.type==='host_quarantined'){quar++;document.getElementById('c_q').textContent=quar;}
  var row=el('div','row');
  row.appendChild(el('span','ts',fmtTs(rec.ts)));
  row.appendChild(el('span','sev '+sevClass(rec.severity),(rec.severity||0).toFixed(1)));
  row.appendChild(el('span','t',rec.type||'?'));
  row.appendChild(el('span','src',rec.source||'?'));
  var sum=(rec.attck&&rec.attck.length?('['+rec.attck.join(',')+'] '):'')+(rec.summary||'');
  row.appendChild(el('span','sum',sum));
  var feed=document.getElementById('feed');feed.insertBefore(row,feed.firstChild);
  while(feed.childNodes.length>250)feed.removeChild(feed.lastChild);
}
function connect(){
  var ws=new WebSocket('ws://'+location.host+'/ws');
  document.getElementById('link').textContent='connecting...';
  ws.onopen=function(){document.getElementById('link').textContent='LIVE';};
  ws.onmessage=function(m){try{addRow(JSON.parse(m.data));}catch(e){}};
  ws.onclose=function(){document.getElementById('link').textContent='reconnecting...';setTimeout(connect,2000);};
  ws.onerror=function(){try{ws.close();}catch(e){}};
}
connect();
</script></body></html>"""


def _summary(event):
    bits = []
    for k in ("ip", "src_ip", "pid", "proc_name", "file_path", "decoy", "lure",
              "service", "categories", "rules", "reason", "module", "detail", "target"):
        v = event.get(k)
        if v not in (None, "", [], {}):
            bits.append(f"{k}={v}")
    return " | ".join(bits)[:400]


async def _broadcast(rec):
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_str(json.dumps(rec, default=str))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def push(event: dict):
    """Loop/thread-safe. Buffers + broadcasts events of interest to the UI."""
    try:
        sev = float(event.get("severity", 0) or 0)
    except Exception:
        sev = 0.0
    if sev <= _MIN_SEV and event.get("type") not in _PASSTHROUGH:
        return
    rec = {"ts": event.get("ts", time.time()), "severity": sev,
           "type": event.get("type", "?"), "source": event.get("source", "?"),
           "attck": event.get("attck", []), "summary": _summary(event),
           "checks": event.get("checks"), "supervised": event.get("supervised")}
    _BUFFER.append(rec)
    if _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(rec), _loop)
    except Exception:
        pass


async def _index(request):
    return web.Response(text=_HTML, content_type="text/html")


async def _ws(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _clients.add(ws)
    try:
        for rec in list(_BUFFER):
            await ws.send_str(json.dumps(rec, default=str))
        async for msg in ws:                       # read-only: ignore inbound
            if msg.type == WSMsgType.ERROR:
                break
    except Exception:
        pass
    finally:
        _clients.discard(ws)
    return ws


async def start(correlator=None):
    global _loop
    if not _AIOHTTP_OK:
        logger.warning("C2_DASHBOARD: aiohttp unavailable — dormant")
        await asyncio.Event().wait(); return
    _loop = asyncio.get_running_loop()
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/ws", _ws)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        await web.TCPSite(runner, _HOST, _PORT).start()
    except Exception as e:
        logger.warning("C2_DASHBOARD: bind %s:%d failed (%s) — dormant", _HOST, _PORT, e)
        await asyncio.Event().wait(); return
    logger.info("C2_DASHBOARD: live → http://%s:%d (localhost-only, read-only)", _HOST, _PORT)
    try:
        await asyncio.Event().wait()
    finally:
        try:
            await runner.cleanup()
        except Exception:
            pass
