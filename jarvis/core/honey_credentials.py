"""
core/honey_credentials.py — JARVIS V48.0 VANGUARD
Credential deception. On startup, plants a synthetic high-privilege decoy
credential in the Windows Credential Manager (CRED_TYPE_GENERIC) and watches the
Security event log (4624/4625) for any logon referencing the honeytoken account.
A hit is high-fidelity evidence of credential theft/use (T1003 / T1078) and is
dispatched to the correlator as CRITICAL.

Uses the documented Credential Manager API only — it does not write into LSASS
process memory.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

logger = logging.getLogger("jarvis.honey_credentials")

_IS_WINDOWS = os.name == "nt"

try:
    import win32evtlog
    import win32cred
    _PYWIN_OK = True
except Exception:
    win32evtlog = None
    win32cred = None
    _PYWIN_OK = False

# --- Config — looks like a saved RDP cred to a DC (high-value bait) ----------
_HONEY_USER = os.environ.get("JARVIS_HONEY_USER", "svc_backup_admin")
_HONEY_DOMAIN = os.environ.get("JARVIS_HONEY_DOMAIN", "CORP")
_HONEY_TARGET = os.environ.get("JARVIS_HONEY_TARGET", "TERMSRV/DC01.corp.local")
_POLL_SECONDS = 8
_WATCH_EIDS = {4624, 4625}


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _plant_credential() -> bool:
    try:
        cred = {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": _HONEY_TARGET,
            "UserName": f"{_HONEY_DOMAIN}\\{_HONEY_USER}",
            "CredentialBlob": secrets.token_urlsafe(18),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "Comment": "JARVIS-HONEYTOKEN do-not-use",
        }
        win32cred.CredWrite(cred, 0)
        logger.info("honey_credentials: decoy planted (%s\\%s -> %s)",
                    _HONEY_DOMAIN, _HONEY_USER, _HONEY_TARGET)
        return True
    except Exception as e:
        logger.warning("honey_credentials: CredWrite failed: %s", e)
        return False


def _newest_record() -> int:
    try:
        h = win32evtlog.OpenEventLog(None, "Security")
    except Exception:
        return 0
    try:
        flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
        events = win32evtlog.ReadEventLog(h, flags, 0)
        if events:
            return int(getattr(events[0], "RecordNumber", 0))
        return int(win32evtlog.GetNumberOfEventLogRecords(h))
    except Exception:
        return 0
    finally:
        try:
            win32evtlog.CloseEventLog(h)
        except Exception:
            pass


def _poll_once(last_record: int):
    """Re-open and page newest->oldest until reaching already-seen records.
    Returns (max_record_seen, [hits])."""
    try:
        h = win32evtlog.OpenEventLog(None, "Security")
    except Exception:
        return last_record, []
    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
    max_seen = last_record
    hits = []
    done = False
    try:
        while not done:
            events = win32evtlog.ReadEventLog(h, flags, 0)
            if not events:
                break
            for ev in events:
                rec = int(getattr(ev, "RecordNumber", 0))
                if rec <= last_record:
                    done = True
                    break
                if rec > max_seen:
                    max_seen = rec
                eid = int(getattr(ev, "EventID", 0)) & 0xFFFF
                if eid not in _WATCH_EIDS:
                    continue
                inserts = getattr(ev, "StringInserts", None) or []
                inserts = [s for s in inserts if s]
                if _HONEY_USER.lower() in " ".join(inserts).lower():
                    hits.append({"event_id": eid, "record": rec,
                                 "time": str(getattr(ev, "TimeGenerated", "")),
                                 "inserts": inserts[:24]})
    except Exception as e:
        logger.debug("honey_credentials: read cycle error: %s", e)
    finally:
        try:
            win32evtlog.CloseEventLog(h)
        except Exception:
            pass
    return max_seen, hits


async def _alert(correlator, hit: dict) -> None:
    logger.critical("HONEY_CREDENTIALS TRIPPED: honeytoken '%s' used in EID %s (rec %s)",
                    _HONEY_USER, hit.get("event_id"), hit.get("record"))
    event = {
        "source": "honey_credentials",
        "type": "honeytoken_use",
        "severity": 10.0,
        "username": f"{_HONEY_DOMAIN}\\{_HONEY_USER}",
        "event_id": hit.get("event_id"),
        "record": hit.get("record"),
        "logon_time": hit.get("time"),
        "raw_inserts": hit.get("inserts"),
        "attck": ["T1003", "T1078"],
        "ts": time.time(),
    }
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
            logger.error("honey_credentials: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("honey_credentials: alert dispatch failed: %s", e)


async def start(correlator=None) -> None:
    """main.py startup hook. JARVIS Watchdog Pattern: dormant if non-Windows,
    pywin32 missing, not elevated (Security log requires admin), or cred plant
    fails."""
    if not _IS_WINDOWS:
        logger.warning("HONEY_CREDENTIALS: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not _PYWIN_OK:
        logger.warning("HONEY_CREDENTIALS: pywin32 (win32evtlog/win32cred) missing — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("HONEY_CREDENTIALS: Security log access requires admin — dormant")
        await asyncio.Event().wait(); return
    if not _plant_credential():
        logger.warning("HONEY_CREDENTIALS: could not plant decoy — dormant")
        await asyncio.Event().wait(); return

    loop = asyncio.get_running_loop()
    last_record = await loop.run_in_executor(None, _newest_record)
    logger.info("HONEY_CREDENTIALS: armed — watching 4624/4625 for '%s' (baseline rec=%d)",
                _HONEY_USER, last_record)
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        try:
            last_record, hits = await loop.run_in_executor(None, _poll_once, last_record)
        except Exception as e:
            logger.debug("honey_credentials: poll error: %s", e)
            continue
        for h in hits:
            await _alert(correlator, h)
