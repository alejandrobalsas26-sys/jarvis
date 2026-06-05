"""
core/ransomware_decoy.py — JARVIS V47.0 TITAN
Canary tripwire. On startup, materializes 3 high-value decoy files in a hidden
vault and monitors EXACTLY those paths via watchdog. Any modify/delete/move/
recreate against a decoy => resolve offending PID(s) (Restart Manager + psutil
fallback), terminate (guarded against critical/system processes and self), and
dispatch a CRITICAL incident to the correlator.

NOTE: watchdog reports the EVENT, not the actor. PID attribution is best-effort
via the Windows Restart Manager (current lockers) with a psutil open-handle
fallback; the CRITICAL alert fires regardless of attribution success.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.ransomware_decoy")

# --- Optional dependency gates ----------------------------------------------
try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except Exception:
    Observer = None
    FileSystemEventHandler = object   # keep class def importable when missing
    _WATCHDOG_OK = False

_IS_WINDOWS = os.name == "nt"

# --- Config ------------------------------------------------------------------
_VAULT_DIR = Path(os.environ.get(
    "JARVIS_DECOY_VAULT", str(Path.home() / "Documents" / ".jarvis_vault")))
_AUTO_KILL_ENABLED = True            # toggle (mirrors core/punisher.py house style)
_PROTECTED_PIDS = {0, 4}
_PROTECTED_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe",
    "fontdrvhost.exe", "dwm.exe", "explorer.exe", "memcompression",
}


# --- Decoy builders (minimal but plausible, dependency-free) ----------------
def _zip_ooxml(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return buf.getvalue()


def _build_docx() -> bytes:
    return _zip_ooxml({
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>',
        "word/document.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Employee Records - CONFIDENTIAL</w:t></w:r></w:p></w:body>'
            '</w:document>',
    })


def _build_xlsx() -> bytes:
    return _zip_ooxml({
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Q4" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
        "xl/worksheets/sheet1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>REVENUE</t></is></c></row></sheetData>'
            '</worksheet>',
    })


def _build_pdf() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 60>>stream\n"
        b"BT /F1 12 Tf 72 720 Td (Disaster Recovery Plan) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


_DECOY_SPECS = [
    ("Q4_Financials_2025.xlsx", _build_xlsx),
    ("Employee_Records_Confidential.docx", _build_docx),
    ("Disaster_Recovery_Plan.pdf", _build_pdf),
]


def _hide(path) -> None:
    if not _IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2 | 0x4)  # HIDDEN|SYSTEM
    except Exception as e:
        logger.debug("ransomware_decoy: hide attr failed for %s: %s", path, e)


def _materialize_vault() -> list[str]:
    _VAULT_DIR.mkdir(parents=True, exist_ok=True)
    _hide(_VAULT_DIR)
    created = []
    for fname, builder in _DECOY_SPECS:
        fp = _VAULT_DIR / fname
        try:
            fp.write_bytes(builder())
            _hide(fp)
            created.append(str(fp))
        except Exception as e:
            logger.debug("ransomware_decoy: decoy build failed for %s: %s", fname, e)
    return created


# --- PID attribution (best-effort) ------------------------------------------
def _find_lockers_rm(path: str) -> list[int]:
    """Windows Restart Manager: PIDs currently holding a handle to `path`."""
    if not _IS_WINDOWS:
        return []
    try:
        import ctypes
        from ctypes import wintypes

        rstrtmgr = ctypes.WinDLL("rstrtmgr", use_last_error=True)
        CCH_RM_SESSION_KEY = 33
        CCH_RM_MAX_APP_NAME = 255
        CCH_RM_MAX_SVC_NAME = 63

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD),
                        ("ProcessStartTime", wintypes.FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ("Process", RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
                ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
                ("ApplicationType", ctypes.c_int),
                ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL),
            ]

        session = wintypes.DWORD(0)
        key = (ctypes.c_wchar * CCH_RM_SESSION_KEY)()
        if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            resources = (ctypes.c_wchar_p * 1)(ctypes.c_wchar_p(path))
            if rstrtmgr.RmRegisterResources(session, 1, resources,
                                            0, None, 0, None) != 0:
                return []
            needed = wintypes.UINT(0)
            have = wintypes.UINT(0)
            reason = wintypes.DWORD(0)
            rstrtmgr.RmGetList(session, ctypes.byref(needed),
                               ctypes.byref(have), None, ctypes.byref(reason))
            n = needed.value
            if n == 0:
                return []
            arr = (RM_PROCESS_INFO * n)()
            have = wintypes.UINT(n)
            if rstrtmgr.RmGetList(session, ctypes.byref(needed),
                                  ctypes.byref(have), arr,
                                  ctypes.byref(reason)) != 0:
                return []
            return [arr[i].Process.dwProcessId for i in range(have.value)]
        finally:
            rstrtmgr.RmEndSession(session)
    except Exception as e:
        logger.debug("ransomware_decoy: RM locker lookup failed: %s", e)
        return []


def _find_lockers_psutil(path: str) -> list[int]:
    if not _PSUTIL_OK:
        return []
    target = os.path.normcase(os.path.abspath(path))
    pids = []
    for p in psutil.process_iter(["pid"]):
        try:
            for of in p.open_files():
                if os.path.normcase(of.path) == target:
                    pids.append(p.pid)
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        except Exception:
            continue
    return pids


def _resolve_offenders(path: str) -> list[int]:
    seen, ordered = set(), []
    for pid in (_find_lockers_rm(path) + _find_lockers_psutil(path)):
        if pid and pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return ordered


def _neutralize(pid: int) -> dict:
    out = {"pid": pid, "killed": False, "name": None, "reason": None}
    if not _AUTO_KILL_ENABLED:
        out["reason"] = "auto-kill disabled"
        return out
    if pid in _PROTECTED_PIDS or pid == os.getpid():
        out["reason"] = "protected/self — kill refused"
        return out
    try:
        p = psutil.Process(pid)
        name = (p.name() or "").lower()
        out["name"] = name
        if name in _PROTECTED_NAMES:
            out["reason"] = "critical system process — kill refused"
            return out
        p.kill()
        out["killed"] = True
        out["reason"] = "terminated"
    except psutil.NoSuchProcess:
        out["reason"] = "process already gone"
    except psutil.AccessDenied:
        out["reason"] = "access denied (insufficient privilege)"
    except Exception as e:
        out["reason"] = f"kill failed: {e}"
    return out


async def _alert(event_type: str, path: str, outcomes: list, correlator) -> None:
    logger.critical("RANSOMWARE_DECOY TRIPPED: %s on %s | offenders=%s",
                    event_type, path, outcomes)
    event = {
        "source": "ransomware_decoy",
        "type": "decoy_tamper",
        "severity": 10.0,
        "event": event_type,
        "decoy": path,
        "offenders": outcomes,
        "killed_pids": [o["pid"] for o in outcomes if o["killed"]],
        "attck": ["T1486", "T1485", "T1490"],  # Encrypt / Destroy / Inhibit Recovery
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
            logger.error("ransomware_decoy: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("ransomware_decoy: alert dispatch failed: %s", e)


class _DecoyHandler(FileSystemEventHandler):
    def __init__(self, decoys: set, loop, correlator):
        self._decoys = decoys           # set of normcase abs paths
        self._loop = loop
        self._correlator = correlator
        self._armed = False
        self._fired = set()             # per-path debounce

    def arm(self):
        self._armed = True

    def _match(self, *paths) -> Optional[str]:
        for raw in paths:
            if not raw:
                continue
            np = os.path.normcase(os.path.abspath(raw))
            if np in self._decoys:
                return np
        return None

    def _dispatch(self, event_type: str, path: str):
        if not self._armed or path in self._fired:
            return
        self._fired.add(path)
        # Resolve + kill synchronously in the observer thread to minimize the
        # window in which the offending handle is still open.
        outcomes = [_neutralize(pid) for pid in _resolve_offenders(path)]
        # Async correlator alert handed back to the main loop.
        asyncio.run_coroutine_threadsafe(
            _alert(event_type, path, outcomes, self._correlator), self._loop)

    def on_modified(self, event):
        if not getattr(event, "is_directory", False):
            p = self._match(getattr(event, "src_path", None))
            if p:
                self._dispatch("modified", p)

    def on_deleted(self, event):
        p = self._match(getattr(event, "src_path", None))
        if p:
            self._dispatch("deleted", p)

    def on_moved(self, event):
        p = self._match(getattr(event, "src_path", None),
                        getattr(event, "dest_path", None))
        if p:
            self._dispatch("moved", p)

    def on_created(self, event):
        p = self._match(getattr(event, "src_path", None))
        if p:
            self._dispatch("created", p)


async def start(correlator=None) -> None:
    """main.py startup hook. JARVIS Watchdog Pattern: on missing deps / vault
    failure / observer failure, log + dormant sleep (never bare return)."""
    if not _PSUTIL_OK:
        logger.warning("RANSOMWARE_DECOY: psutil unavailable — dormant")
        await asyncio.Event().wait()
        return
    if not _WATCHDOG_OK:
        logger.warning("RANSOMWARE_DECOY: watchdog lib unavailable — dormant")
        await asyncio.Event().wait()
        return
    try:
        decoys = _materialize_vault()
    except Exception as e:
        logger.warning("RANSOMWARE_DECOY: vault setup failed (%s) — dormant", e)
        await asyncio.Event().wait()
        return
    if not decoys:
        logger.warning("RANSOMWARE_DECOY: no decoys materialized — dormant")
        await asyncio.Event().wait()
        return

    loop = asyncio.get_running_loop()
    handler = _DecoyHandler(
        {os.path.normcase(os.path.abspath(d)) for d in decoys}, loop, correlator)
    observer = Observer()
    try:
        observer.schedule(handler, str(_VAULT_DIR), recursive=False)
        observer.start()
    except Exception as e:
        logger.warning("RANSOMWARE_DECOY: observer start failed (%s) — dormant", e)
        await asyncio.Event().wait()
        return

    handler.arm()
    logger.info("RANSOMWARE_DECOY: armed — %d tripwire(s) in %s",
                len(decoys), _VAULT_DIR)
    try:
        await asyncio.Event().wait()   # resident; observer runs in its own thread
    finally:
        try:
            observer.stop()
            observer.join(timeout=5)
        except Exception:
            pass
