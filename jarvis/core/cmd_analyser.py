"""
core/cmd_analyser.py — JARVIS V53.0 SHADOW
Command-line execution heuristics. Polls Security 4688 (process creation with
command line — requires 'Audit Process Creation' + the 'Include command line in
process creation events' policy). Flags obfuscation (caret ^, env-var substring
%X:~, base64 -enc / FromBase64String, -bxor, [char]), LOLBin abuse (mshta,
certutil, rundll32, regsvr32, bitsadmin, wmic, mshta, installutil, msbuild), and
suspicious parent->child lineage (office/script host -> shell). T1059 / T1218.
Watchdog Pattern: dormant if non-Windows, pywin32 missing, or not elevated.
"""
from __future__ import annotations
import asyncio, hashlib, logging, os, re, time
from collections import deque

logger = logging.getLogger("jarvis.cmd_analyser")

_IS_WINDOWS = os.name == "nt"
try:
    import win32evtlog; _PYWIN_OK = True
except Exception:
    win32evtlog = None; _PYWIN_OK = False

_POLL = 8
_WATCH = {4688}
_ALERT_TTL = 90
_alerted = {}

_LOLBINS = {"mshta.exe", "certutil.exe", "rundll32.exe", "regsvr32.exe", "bitsadmin.exe",
            "wmic.exe", "installutil.exe", "msbuild.exe", "cscript.exe", "wscript.exe",
            "cmstp.exe", "mavinject.exe", "forfiles.exe", "scrcons.exe"}
_OBF = re.compile(
    r"\^|FromBase64String|-enc(?:odedcommand)?\b|\s-e[c]?\s|%[A-Za-z0-9_]+:~|\bIEX\b|"
    r"Invoke-Expression|-w\s*hidden|-windowstyle\s+hidden|-nop\b|\[char\]|-bxor|"
    r"certutil.+(?:-urlcache|-decode|-f)|rundll32.+javascript|regsvr32.+scrobj|"
    r"bitsadmin.+/transfer|[A-Za-z0-9+/]{120,}={0,2}", re.IGNORECASE)
_SUSP_PARENTS = {"winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe", "mshta.exe",
                 "wscript.exe", "cscript.exe", "wmiprvse.exe", "msaccess.exe"}
_SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe"}


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _fields(inserts):
    procs = [s for s in inserts if isinstance(s, str) and s.lower().endswith(".exe")]
    newproc = procs[0] if procs else "?"
    parent = procs[-1] if len(procs) >= 2 else "?"
    cmd = ""
    for s in inserts:
        if s and " " in s and len(s) > len(cmd):
            cmd = s
    if not cmd:
        cmd = newproc
    return newproc, parent, cmd


def _analyze(inserts):
    newproc, parent, cmd = _fields(inserts)
    base = os.path.basename(newproc).lower() if newproc != "?" else "?"
    pbase = os.path.basename(parent).lower() if parent != "?" else "?"
    low = cmd.lower()
    reasons = []; attck = {"T1059"}
    if _OBF.search(cmd):
        reasons.append("obfuscation/encoded-command")
    if base in _LOLBINS or any(t in low for t in
                               ("mshta", "certutil", "regsvr32", "rundll32", "bitsadmin", "wmic")):
        reasons.append("LOLBin abuse"); attck.add("T1218")
    if pbase in _SUSP_PARENTS and base in _SHELLS:
        reasons.append("suspicious lineage %s->%s" % (pbase, base)); attck.add("T1059.001")
    if not reasons:
        return None
    strong = sum(1 for r in reasons if "LOLBin" in r or "obfuscation" in r)
    sev = 9.0 if (any("lineage" in r for r in reasons) or strong >= 2) else 8.5
    return {"new_process": newproc, "parent": parent, "cmdline": cmd[:600],
            "reasons": reasons, "attck": sorted(attck), "severity": sev}


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
    max_seen = last_record; out = []; done = False
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
                if (int(getattr(ev, "EventID", 0)) & 0xFFFF) not in _WATCH:
                    continue
                inserts = [s for s in (getattr(ev, "StringInserts", None) or []) if s]
                r = _analyze(inserts)
                if r:
                    out.append(r)
    except Exception as e:
        logger.debug("cmd: read error: %s", e)
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
            logger.error("cmd_analyser: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("cmd_analyser: dispatch failed: %s", e)


async def start(correlator=None):
    if not _IS_WINDOWS:
        logger.warning("CMD_ANALYSER: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not _PYWIN_OK:
        logger.warning("CMD_ANALYSER: pywin32 missing — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("CMD_ANALYSER: Security log requires admin — dormant")
        await asyncio.Event().wait(); return
    loop = asyncio.get_running_loop()
    last = await loop.run_in_executor(None, _newest_record)
    logger.info("CMD_ANALYSER: armed — 4688 command-line heuristics (baseline rec=%d)", last)
    while True:
        await asyncio.sleep(_POLL)
        try:
            last, hits = await loop.run_in_executor(None, _poll_once, last)
        except Exception as e:
            logger.debug("cmd: poll error: %s", e)
            continue
        for r in hits:
            key = hashlib.sha256(r["cmdline"].encode("utf-8", "ignore")).hexdigest()
            now = time.time()
            if now - _alerted.get(key, 0) < _ALERT_TTL:
                continue
            _alerted[key] = now
            event = {"source": "cmd_analyser", "type": "suspicious_commandline",
                     "severity": r["severity"], "new_process": r["new_process"],
                     "parent": r["parent"], "cmdline": r["cmdline"], "reasons": r["reasons"],
                     "attck": r["attck"], "ts": now}
            logger.warning("CMD_ANALYSER: %s | %s", r["reasons"], r["cmdline"][:160])
            await _dispatch(correlator, event)
