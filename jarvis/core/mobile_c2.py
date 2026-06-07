"""
core/mobile_c2.py — JARVIS V54.0 OMEGA
Mobile SOC bridge. aiohttp Telegram long-poll (no heavy wrapper). Pushes Sev>=8.0
alerts to authorized chats and accepts a STRICTLY-ALLOWLISTED command set; every
command routes through an existing guarded module (network_quarantine,
coverage_reporter, health_watchdog) — no shell, no eval, no file ops.

SAFETY:
- Per-chat authorization via JARVIS_TELEGRAM_AUTHORIZED_CHATS (csv of chat IDs).
  Empty list -> refuses to start. Unauthorized chats are dropped + audited.
- Opt-in via JARVIS_MOBILE_C2_ENABLE=1 (so it never collides with telegram_bridge
  polling the same token).
- Allowed commands: /status /help /coverage /quarantine <ip> /release <ip> /ack <id>.
- Per-chat rate limit; every received command audited to logs/mobile_c2_audit.jsonl.
"""
from __future__ import annotations
import asyncio, ipaddress, json, logging, os, time
from collections import defaultdict, deque
from pathlib import Path

logger = logging.getLogger("jarvis.mobile_c2")

try:
    import aiohttp
    _AIOHTTP_OK = True
except Exception:
    aiohttp = None; _AIOHTTP_OK = False

_TOKEN = os.environ.get("JARVIS_TELEGRAM_TOKEN")
_ENABLED = os.environ.get("JARVIS_MOBILE_C2_ENABLE", "0") == "1"
_AUTH_CHATS = {s.strip() for s in
               os.environ.get("JARVIS_TELEGRAM_AUTHORIZED_CHATS", "").split(",") if s.strip()}
_API = "https://api.telegram.org/bot" + (_TOKEN or "INVALID")
_MIN_SEV = float(os.environ.get("JARVIS_MOBILE_C2_MIN_SEV", "8.0"))
_RATE_WINDOW = 60
_RATE_LIMIT = 20
_DEDUP_TTL = 30
_AUDIT_PATH = Path("logs/mobile_c2_audit.jsonl")

_loop = None
_session = None
_offset = 0
_recent_alerts = {}
_recent_cmds = defaultdict(lambda: deque(maxlen=_RATE_LIMIT * 2))


def _audit(rec):
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _authorized(chat_id):
    return str(chat_id) in _AUTH_CHATS


def _rate_ok(chat_id):
    now = time.time()
    dq = _recent_cmds[str(chat_id)]
    dq.append(now)
    while dq and now - dq[0] > _RATE_WINDOW:
        dq.popleft()
    return len(dq) <= _RATE_LIMIT


def _valid_ip(s):
    try:
        ipaddress.ip_address(s); return True
    except Exception:
        return False


def _fmt_alert(event):
    bits = [f"[SEV {event.get('severity','?')}] {event.get('type','?')}  src={event.get('source','?')}"]
    for k in ("ip", "src_ip", "pid", "proc_name", "domain", "lure", "cmdline", "module",
              "decoy", "mac", "rules"):
        v = event.get(k)
        if v:
            bits.append(f"{k}={str(v)[:200]}")
    attck = event.get("attck") or []
    if attck:
        bits.append("ATT&CK=" + ",".join(map(str, attck)))
    return "\n".join(bits)


async def _send(chat_id, text):
    if not _session:
        return
    try:
        await _session.post(_API + "/sendMessage",
                            json={"chat_id": chat_id, "text": text[:3800]},
                            timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.debug("mobile_c2: send: %s", e)


async def _broadcast(text):
    for chat in _AUTH_CHATS:
        await _send(chat, text)


def push(event: dict):
    """Loop/thread-safe. Forwards Sev>=_MIN_SEV events to authorized chats. Dedup'd."""
    if not (_ENABLED and _loop and _session is not None):
        return
    try:
        sev = float(event.get("severity", 0) or 0)
    except Exception:
        sev = 0.0
    if sev < _MIN_SEV:
        return
    key = (event.get("type", ""), event.get("ip") or event.get("src_ip")
           or event.get("decoy") or event.get("mac") or "")
    now = time.time()
    if now - _recent_alerts.get(key, 0) < _DEDUP_TTL:
        return
    _recent_alerts[key] = now
    try:
        asyncio.run_coroutine_threadsafe(_broadcast("JARVIS ALERT\n" + _fmt_alert(event)), _loop)
    except Exception:
        pass


_HELP = ("Commands:\n"
         "/status — supervised module summary\n"
         "/coverage — emit ATT&CK Navigator layer\n"
         "/quarantine <ip> — host-firewall isolate (via network_quarantine)\n"
         "/release <ip> — release a quarantined host\n"
         "/ack <id> — acknowledge an alert\n"
         "/help — this message")


async def _cmd_status(chat_id, args):
    try:
        from core import health_watchdog
        sup = getattr(health_watchdog, "_SUP", {}) or {}
        live = sum(1 for v in sup.values() if v.get("task") and not v["task"].done())
        await _send(chat_id, f"JARVIS modules: {live}/{len(sup)} alive")
    except Exception:
        await _send(chat_id, "JARVIS status: ok (health_watchdog unavailable)")


async def _cmd_coverage(chat_id, args):
    try:
        from core import coverage_reporter
        p = await coverage_reporter.generate()
        await _send(chat_id, f"Navigator layer: {p}")
    except Exception as e:
        await _send(chat_id, f"coverage failed: {e}")


async def _cmd_quarantine(chat_id, args, correlator):
    if not args or not _valid_ip(args[0]):
        await _send(chat_id, "usage: /quarantine <ip>"); return
    try:
        from core import network_quarantine
        res = await network_quarantine.quarantine(args[0], reason="mobile_c2", correlator=correlator)
        await _send(chat_id, f"quarantine {args[0]}: {res}")
    except Exception as e:
        await _send(chat_id, f"quarantine failed: {e}")


async def _cmd_release(chat_id, args):
    if not args or not _valid_ip(args[0]):
        await _send(chat_id, "usage: /release <ip>"); return
    try:
        from core import network_quarantine
        res = await network_quarantine.release(args[0])
        await _send(chat_id, f"release {args[0]}: {res}")
    except Exception as e:
        await _send(chat_id, f"release failed: {e}")


async def _cmd_ack(chat_id, args):
    await _send(chat_id, "ack noted: " + " ".join(args)[:200])


async def _handle_update(update, correlator):
    msg = update.get("message") or update.get("edited_message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    user = (msg.get("from") or {}).get("username", "?")
    if chat is None or not text:
        return
    if not _authorized(chat):
        logger.warning("mobile_c2: UNAUTHORIZED chat=%s user=%s text=%r", chat, user, text[:80])
        _audit({"ev": "unauth", "chat": chat, "user": user, "text": text[:200], "ts": time.time()})
        return
    if not _rate_ok(chat):
        await _send(chat, "rate limit exceeded"); return
    parts = text.split()
    cmd = parts[0].lower(); args = parts[1:]
    _audit({"ev": "cmd", "chat": chat, "user": user, "cmd": cmd, "args": args, "ts": time.time()})
    if cmd == "/status":     await _cmd_status(chat, args)
    elif cmd == "/help":     await _send(chat, _HELP)
    elif cmd == "/coverage": await _cmd_coverage(chat, args)
    elif cmd == "/release":  await _cmd_release(chat, args)
    elif cmd == "/ack":      await _cmd_ack(chat, args)
    elif cmd == "/quarantine": await _cmd_quarantine(chat, args, correlator)
    else: await _send(chat, "unknown command. /help")


async def _poll_loop(correlator):
    global _offset
    while True:
        try:
            async with _session.get(_API + "/getUpdates",
                                    params={"offset": _offset, "timeout": 25},
                                    timeout=aiohttp.ClientTimeout(total=35)) as r:
                if r.status != 200:
                    await asyncio.sleep(5); continue
                data = await r.json()
            for upd in data.get("result", []):
                _offset = max(_offset, upd.get("update_id", 0) + 1)
                try:
                    await _handle_update(upd, correlator)
                except Exception as e:
                    logger.debug("mobile_c2: handler: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("mobile_c2: poll: %s", e)
            await asyncio.sleep(5)


async def start(correlator=None):
    global _loop, _session
    _loop = asyncio.get_running_loop()
    if not _AIOHTTP_OK:
        logger.warning("MOBILE_C2: aiohttp unavailable — dormant"); await asyncio.Event().wait(); return
    if not _TOKEN:
        logger.warning("MOBILE_C2: JARVIS_TELEGRAM_TOKEN missing — dormant"); await asyncio.Event().wait(); return
    if not _ENABLED:
        logger.info("MOBILE_C2: token present but JARVIS_MOBILE_C2_ENABLE!=1 — dormant (avoids telegram_bridge poll conflict)")
        await asyncio.Event().wait(); return
    if not _AUTH_CHATS:
        logger.warning("MOBILE_C2: JARVIS_TELEGRAM_AUTHORIZED_CHATS empty — refusing to start (no authorized recipients)")
        await asyncio.Event().wait(); return
    _session = aiohttp.ClientSession()
    logger.info("MOBILE_C2: armed — %d authorized chat(s), min_sev=%.1f", len(_AUTH_CHATS), _MIN_SEV)
    try:
        await _poll_loop(correlator)
    finally:
        try:
            await _session.close()
        except Exception:
            pass
