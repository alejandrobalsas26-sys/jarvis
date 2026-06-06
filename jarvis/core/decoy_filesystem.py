"""
core/decoy_filesystem.py — JARVIS V51.0 DECEPTION GRID
Breadcrumb deception. Seeds believable lure files and monitors them. watchdog
catches tamper/move/delete (no admin). If elevated AND JARVIS_DECOY_AUDIT=1, sets
a Read audit ACE on the lures and watches Security 4663 to also beacon on
READ/OPEN. Real user secret files (~/.aws, ~/.ssh) are never overwritten or
monitored. T1083 / T1552-bait.
"""
from __future__ import annotations
import asyncio, logging, os, time
from pathlib import Path

logger = logging.getLogger("jarvis.decoy_filesystem")

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except Exception:
    Observer = None; FileSystemEventHandler = object; _WATCHDOG_OK = False

try:
    import win32evtlog; _PYWIN_OK = True
except Exception:
    win32evtlog = None; _PYWIN_OK = False

_IS_WINDOWS = os.name == "nt"
_AUDIT = os.environ.get("JARVIS_DECOY_AUDIT", "0") == "1"
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0
_KDBX = b"\x03\xd9\xa2\x9a\x67\xfb\x4b\xb5\x00\x00\x00\x00JARVIS-DECOY"
_LURES = set()


def _lure_specs():
    h = Path.home()
    return [
        (h / "Documents" / "Passwords_Export_2025.kdbx", _KDBX, False),
        (h / "Documents" / "VPN_Prod_Config.ovpn",
         b"client\ndev tun\nproto udp\nremote vpn.corp.local 1194\nauth-user-pass\n", False),
        (h / "Documents" / "DB_Backup_Prod.sql.bak",
         b"-- MySQL dump 10.13  Host: db-prod  Database: billing\n-- DECOY\n", False),
        (h / ".aws" / "credentials",
         b"[default]\naws_access_key_id=AKIADECOY0000DECOY00\naws_secret_access_key=decoyDECOYdecoyDECOYdecoyDECOYdecoy0000\n", True),
    ]


def _is_admin():
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _seed():
    created = []
    for path, content, skip_if_exists in _lure_specs():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if skip_if_exists and path.exists():
                continue
            if not path.exists():
                path.write_bytes(content)
            created.append(str(path))
        except Exception as e:
            logger.debug("decoy_fs: seed %s: %s", path, e)
    return created


def _enable_read_audit(paths):
    if not (_IS_WINDOWS and _AUDIT and _is_admin()):
        if _AUDIT:
            logger.info("decoy_fs: read-audit requested but not elevated — tamper-only")
        return False
    import subprocess
    try:
        subprocess.run(["auditpol", "/set", "/subcategory:File System", "/success:enable"],
                       capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    except Exception as e:
        logger.debug("decoy_fs: auditpol failed: %s", e); return False
    tmpl = ("$p='{p}'; $a=Get-Acl -LiteralPath $p; "
            "$r=New-Object System.Security.AccessControl.FileSystemAuditRule("
            "'Everyone','Read','Success'); $a.AddAuditRule($r); "
            "Set-Acl -LiteralPath $p -AclObject $a")
    ok = False
    for p in paths:
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", tmpl.format(p=p)],
                           capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
            ok = True
        except Exception as e:
            logger.debug("decoy_fs: SACL %s: %s", p, e)
    return ok


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
            logger.error("decoy_fs: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("decoy_fs: dispatch failed: %s", e)


async def _alert(correlator, kind, path, extra=None):
    logger.critical("DECOY_FS TRIPPED: %s on lure %s", kind, path)
    event = {"source": "decoy_filesystem", "type": "decoy_breadcrumb", "severity": 10.0,
             "access": kind, "lure": path, "detail": extra or {},
             "attck": ["T1083", "T1552.001"], "ts": time.time()}
    await _dispatch(correlator, event)


class _LureHandler(FileSystemEventHandler):
    def __init__(self, lures, loop, correlator):
        self._lures = lures; self._loop = loop; self._c = correlator; self._fired = set()
    def _match(self, *paths):
        for raw in paths:
            if raw and os.path.normcase(os.path.abspath(raw)) in self._lures:
                return os.path.normcase(os.path.abspath(raw))
        return None
    def _go(self, kind, path):
        if path in self._fired:
            return
        self._fired.add(path)
        asyncio.run_coroutine_threadsafe(_alert(self._c, kind, path), self._loop)
    def on_modified(self, e):
        if not e.is_directory:
            p = self._match(e.src_path)
            if p:
                self._go("modified", p)
    def on_deleted(self, e):
        p = self._match(e.src_path)
        if p:
            self._go("deleted", p)
    def on_moved(self, e):
        p = self._match(getattr(e, "src_path", None), getattr(e, "dest_path", None))
        if p:
            self._go("moved", p)


def _newest_security_record():
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


def _poll_4663(last_record, lures_lower):
    hits = []
    try:
        h = win32evtlog.OpenEventLog(None, "Security")
    except Exception:
        return last_record, hits
    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
    max_seen = last_record; done = False
    try:
        while not done:
            evs = win32evtlog.ReadEventLog(h, flags, 0)
            if not evs:
                break
            for ev in evs:
                rec = int(getattr(ev, "RecordNumber", 0))
                if rec <= last_record:
                    done = True; break
                max_seen = max(max_seen, rec)
                if (int(getattr(ev, "EventID", 0)) & 0xFFFF) != 4663:
                    continue
                ins = [s for s in (getattr(ev, "StringInserts", None) or []) if s]
                joined = " ".join(ins).lower()
                for lp in lures_lower:
                    if lp in joined:
                        hits.append({"record": rec, "lure": lp})
                        break
    except Exception as e:
        logger.debug("decoy_fs: 4663 read error: %s", e)
    finally:
        try:
            win32evtlog.CloseEventLog(h)
        except Exception:
            pass
    return max_seen, hits


async def start(correlator=None):
    if not _WATCHDOG_OK:
        logger.warning("DECOY_FILESYSTEM: watchdog unavailable — dormant")
        await asyncio.Event().wait(); return
    created = _seed()
    if not created:
        logger.warning("DECOY_FILESYSTEM: no lures seeded — dormant")
        await asyncio.Event().wait(); return
    global _LURES
    _LURES = {os.path.normcase(os.path.abspath(p)) for p in created}
    loop = asyncio.get_running_loop()
    observer = Observer()
    handler = _LureHandler(_LURES, loop, correlator)
    dirs = {str(Path(p).parent) for p in created}
    for d in dirs:
        try:
            observer.schedule(handler, d, recursive=False)
        except Exception as e:
            logger.debug("decoy_fs: schedule %s: %s", d, e)
    try:
        observer.start()
    except Exception as e:
        logger.warning("DECOY_FILESYSTEM: observer start failed (%s) — dormant", e)
        await asyncio.Event().wait(); return

    read_audit = _PYWIN_OK and _enable_read_audit(created)
    logger.info("DECOY_FILESYSTEM: armed — %d lure(s), read-audit=%s", len(created), read_audit)
    try:
        if read_audit:
            lures_lower = [p.lower() for p in created]
            last = await loop.run_in_executor(None, _newest_security_record)
            while True:
                await asyncio.sleep(8)
                last, hits = await loop.run_in_executor(None, _poll_4663, last, lures_lower)
                for hh in hits:
                    await _alert(correlator, "read", hh["lure"], {"record": hh["record"]})
        else:
            await asyncio.Event().wait()
    finally:
        try:
            observer.stop(); observer.join(timeout=5)
        except Exception:
            pass
