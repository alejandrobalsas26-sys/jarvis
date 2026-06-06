"""
core/itdr_sentinel.py — JARVIS V52.0 AEGIS  (Identity Threat Detection & Response)
Host/AD identity-attack detection. Polls the Security event log for auth and
account-management events and applies behavioral heuristics: password spray
(T1110.003), account brute force (T1110.001), NTLM/pass-the-hash indicator
(T1550.002), explicit-credential lateral use (T1078/T1021), privileged-group
changes (T1098/T1068) and rogue account creation (T1136.001). Watchdog Pattern:
dormant if non-Windows, pywin32 missing, or not elevated.
"""
from __future__ import annotations
import asyncio, logging, os, re, time
from collections import deque

logger = logging.getLogger("jarvis.itdr_sentinel")

_IS_WINDOWS = os.name == "nt"
try:
    import win32evtlog; _PYWIN_OK = True
except Exception:
    win32evtlog = None; _PYWIN_OK = False

_POLL = 8
_WATCH = {4624, 4625, 4648, 4672, 4720, 4724, 4728, 4732, 4756, 4776}
_SPRAY_WINDOW = 120
_SPRAY_DISTINCT = 5
_BRUTE_COUNT = 8
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_USER = re.compile(r"^[A-Za-z0-9._$-]{1,32}$")

_fail_by_ip = {}
_fail_by_user = {}


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _extract(inserts):
    ip = None; ntlm = False; users = []
    for s in inserts:
        if not s:
            continue
        if ip is None:
            m = _IPV4.search(s)
            if m and m.group(0) not in ("0.0.0.0", "127.0.0.1"):
                ip = m.group(0)
        if "NTLM" in s:
            ntlm = True
        st = s.strip()
        if _USER.match(st) and not st.endswith("$") and st.lower() not in ("-", "na", "null"):
            users.append(st)
    return (ip or "?"), ntlm, (users[0] if users else "?")


def _prune(dq, now, window=_SPRAY_WINDOW):
    while dq and now - dq[0][0] > window:
        dq.popleft()


def _analyze(eid, inserts, now):
    alerts = []
    ip, ntlm, target = _extract(inserts)
    detail = " ".join([s for s in inserts if s][:8])[:300]
    if eid == 4625:
        dqi = _fail_by_ip.setdefault(ip, deque())
        dqi.append((now, target)); _prune(dqi, now)
        distinct = {t for _ts, t in dqi if t != "?"}
        if len(distinct) >= _SPRAY_DISTINCT:
            alerts.append(("password_spray", 9.0, ["T1110.003"],
                           {"src_ip": ip, "distinct_targets": len(distinct)}))
        dqu = _fail_by_user.setdefault(target, deque())
        dqu.append((now, None)); _prune(dqu, now)
        if target != "?" and len(dqu) >= _BRUTE_COUNT:
            alerts.append(("account_bruteforce", 9.0, ["T1110.001"],
                           {"target": target, "failures": len(dqu), "src_ip": ip}))
    elif eid == 4624 and ntlm:
        alerts.append(("ntlm_network_logon", 7.5, ["T1550.002", "T1078"],
                       {"src_ip": ip, "target": target, "note": "NTLM logon — PtH indicator"}))
    elif eid == 4648:
        alerts.append(("explicit_credential_use", 7.0, ["T1078", "T1021"],
                       {"src_ip": ip, "target": target}))
    elif eid in (4728, 4732, 4756):
        alerts.append(("privileged_group_change", 9.0, ["T1098", "T1068"], {"detail": detail}))
    elif eid == 4720:
        alerts.append(("account_created", 7.5, ["T1136.001"], {"detail": detail}))
    elif eid == 4724:
        alerts.append(("password_reset", 6.5, ["T1098"], {"detail": detail}))
    return alerts


def _newest_record():
    try:
        h = win32evtlog.OpenEventLog(None, "Security")
    except Exception:
        return 0
    try:
        ev = win32evtlog.ReadEventLog(
            h, win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ, 0)
        return int(getattr(ev[0], "RecordNumber", 0)) if ev else 0
    except Exception:
        return 0
    finally:
        try:
            win32evtlog.CloseEventLog(h)
        except Exception:
            pass


def _poll_once(last_record):
    try:
        h = win32evtlog.OpenEventLog(None, "Security")
    except Exception:
        return last_record, []
    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
    max_seen = last_record; out = []; done = False; now = time.time()
    try:
        while not done:
            evs = win32evtlog.ReadEventLog(h, flags, 0)
            if not evs:
                break
            for ev in evs:
                rec = int(getattr(ev, "RecordNumber", 0))
                if rec <= last_record:
                    done = True; break
                if rec > max_seen:
                    max_seen = rec
                eid = int(getattr(ev, "EventID", 0)) & 0xFFFF
                if eid not in _WATCH:
                    continue
                inserts = [s for s in (getattr(ev, "StringInserts", None) or []) if s]
                for a in _analyze(eid, inserts, now):
                    out.append((eid, a))
    except Exception as e:
        logger.debug("itdr: read error: %s", e)
    finally:
        try:
            win32evtlog.CloseEventLog(h)
        except Exception:
            pass
    return max_seen, out


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
            logger.error("itdr_sentinel: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("itdr_sentinel: dispatch failed: %s", e)


async def start(correlator=None):
    if not _IS_WINDOWS:
        logger.warning("ITDR_SENTINEL: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not _PYWIN_OK:
        logger.warning("ITDR_SENTINEL: pywin32 missing — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("ITDR_SENTINEL: Security log requires admin — dormant")
        await asyncio.Event().wait(); return
    loop = asyncio.get_running_loop()
    last = await loop.run_in_executor(None, _newest_record)
    logger.info("ITDR_SENTINEL: armed — identity-threat detection on Security log (baseline rec=%d)", last)
    while True:
        await asyncio.sleep(_POLL)
        try:
            last, hits = await loop.run_in_executor(None, _poll_once, last)
        except Exception as e:
            logger.debug("itdr: poll error: %s", e)
            continue
        for eid, (kind, sev, attck, extra) in hits:
            event = {"source": "itdr_sentinel", "type": kind, "severity": sev,
                     "event_id": eid, "attck": attck, "ts": time.time()}
            event.update(extra)
            logger.warning("ITDR: %s (EID %s) sev=%.1f %s", kind, eid, sev, extra)
            await _dispatch(correlator, event)
