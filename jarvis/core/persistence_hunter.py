"""
core/persistence_hunter.py — JARVIS V50.0 NEXUS
Autostart/persistence threat hunting (read-only). Enumerates Run/RunOnce keys,
Winlogon Shell/Userinit, IFEO Debuggers, AppInit_DLLs, services, Startup folders,
Scheduled Tasks (schtasks /query /XML) and WMI event subscriptions
(root\\subscription). Flags obfuscation (Base64/IEX/encoded commands) and
user-writable/external binary paths. Baseline-diff dedup. Sweeps every 4h.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from hashlib import sha256
from pathlib import Path

logger = logging.getLogger("jarvis.persistence_hunter")

_IS_WINDOWS = os.name == "nt"

try:
    import winreg
    _WINREG_OK = True
except Exception:
    winreg = None
    _WINREG_OK = False

_SWEEP_SECONDS = 4 * 3600
_WARMUP_SECONDS = 60
_STATE_PATH = Path("logs/persistence_baseline.json")
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0

_OBF = re.compile(
    r"-enc(?:odedcommand)?\b|\bIEX\b|Invoke-Expression|FromBase64String|"
    r"DownloadString|DownloadFile|-w\s*hidden|-windowstyle\s+hidden|-nop\b|"
    r"-noprofile\b|\bbypass\b|certutil\s+-decode|\bmshta\b|regsvr32.*scrobj|"
    r"rundll32.*javascript|[A-Za-z0-9+/]{80,}={0,2}", re.IGNORECASE)
_SYS_DIRS = ("c:\\windows", "c:\\program files", "c:\\program files (x86)")
_ANOMALOUS_DIRS = ("\\temp\\", "\\tmp\\", "\\downloads\\", "\\users\\public\\",
                   "\\appdata\\local\\temp\\", "\\windows\\temp\\",
                   "\\$recycle.bin\\", "\\perflogs\\", "\\programdata\\temp\\")
_PATH_RE = re.compile(r'"?([a-zA-Z]:\\[^"]+?\.(?:exe|dll|scr|bat|cmd|ps1|vbs|js|com|pif))"?', re.I)


def _primary_path(value):
    m = _PATH_RE.search(value or "")
    return m.group(1) if m else None


def _authenticode_batch(paths):
    res = {}
    paths = sorted({p for p in paths if p})
    if not paths or not _IS_WINDOWS:
        return res
    arr = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    ps = ("Get-AuthenticodeSignature -LiteralPath @(" + arr + ") "
          "-ErrorAction SilentlyContinue | Select-Object Path,Status | "
          "ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=120,
                             creationflags=_NO_WINDOW)
        data = json.loads(out.stdout or "null")
        if isinstance(data, dict):
            data = [data]
        for d in (data or []):
            if d and d.get("Path"):
                res[os.path.normcase(str(d["Path"]))] = str(d.get("Status"))
    except Exception as e:
        logger.debug("persistence: authenticode batch failed: %s", e)
    return res

if _WINREG_OK:
    _HKLM = winreg.HKEY_LOCAL_MACHINE
    _HKCU = winreg.HKEY_CURRENT_USER
else:
    _HKLM = _HKCU = None

_RUN_KEYS = []
if _WINREG_OK:
    _RUN_KEYS = [
        (_HKCU, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (_HKCU, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
        (_HKLM, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (_HKLM, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
        (_HKLM, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run"),
        (_HKLM, r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ]


def _read_values(hive, subkey):
    out = []
    try:
        k = winreg.OpenKey(hive, subkey)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name, val, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            out.append((f"Registry:{subkey}", str(name), str(val)))
            i += 1
    finally:
        winreg.CloseKey(k)
    return out


def _read_single(hive, subkey, value):
    try:
        k = winreg.OpenKey(hive, subkey)
    except OSError:
        return None
    try:
        v, _t = winreg.QueryValueEx(k, value)
        return str(v)
    except OSError:
        return None
    finally:
        winreg.CloseKey(k)


def _enum_subkeys(hive, subkey):
    names = []
    try:
        k = winreg.OpenKey(hive, subkey)
    except OSError:
        return names
    try:
        i = 0
        while True:
            try:
                names.append(winreg.EnumKey(k, i))
            except OSError:
                break
            i += 1
    finally:
        winreg.CloseKey(k)
    return names


def _collect_registry():
    findings = []
    for hive, sk in _RUN_KEYS:
        findings += _read_values(hive, sk)
    wl = r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon"
    for v in ("Shell", "Userinit"):
        val = _read_single(_HKLM, wl, v)
        if val:
            findings.append((f"Winlogon:{v}", v, val))
    appinit = _read_single(
        _HKLM, r"Software\Microsoft\Windows NT\CurrentVersion\Windows", "AppInit_DLLs")
    if appinit:
        findings.append(("AppInit_DLLs", "AppInit_DLLs", appinit))
    ifeo = r"Software\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
    for sub in _enum_subkeys(_HKLM, ifeo):
        dbg = _read_single(_HKLM, ifeo + "\\" + sub, "Debugger")
        if dbg:
            findings.append((f"IFEO:{sub}", "Debugger", dbg))
    svc_root = r"System\CurrentControlSet\Services"
    for sub in _enum_subkeys(_HKLM, svc_root)[:600]:
        ip = _read_single(_HKLM, svc_root + "\\" + sub, "ImagePath")
        if ip:
            low = ip.lower()
            if any(t in low for t in _ANOMALOUS_DIRS):
                findings.append((f"Service:{sub}", "ImagePath", ip))
    return findings


def _collect_startup_folders():
    findings = []
    folders = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        folders.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" /
                       "Programs" / "Startup")
    if programdata:
        folders.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" /
                       "Programs" / "Startup")
    for fol in folders:
        try:
            if fol.is_dir():
                for f in fol.iterdir():
                    findings.append(("StartupFolder", f.name, str(f)))
        except Exception:
            continue
    return findings


def _collect_scheduled_tasks():
    findings = []
    if not _IS_WINDOWS:
        return findings
    try:
        out = subprocess.run(["schtasks", "/query", "/XML", "ONE"],
                             capture_output=True, text=True, timeout=90,
                             creationflags=_NO_WINDOW)
        raw = out.stdout or ""
        for m in re.finditer(r"<Command>(.*?)</Command>", raw, re.S | re.I):
            findings.append(("SchedTask", "Command", m.group(1).strip()))
        for m in re.finditer(r"<Arguments>(.*?)</Arguments>", raw, re.S | re.I):
            findings.append(("SchedTask", "Arguments", m.group(1).strip()))
    except Exception as e:
        logger.debug("persistence: schtasks failed: %s", e)
    return findings


def _collect_wmi_subscriptions():
    findings = []
    if not _IS_WINDOWS:
        return findings
    queries = [
        ("CommandLineEventConsumer",
         "Get-WmiObject -Namespace root\\subscription -Class CommandLineEventConsumer "
         "-ErrorAction SilentlyContinue | Select-Object Name,CommandLineTemplate | "
         "ConvertTo-Json -Compress"),
        ("ActiveScriptEventConsumer",
         "Get-WmiObject -Namespace root\\subscription -Class ActiveScriptEventConsumer "
         "-ErrorAction SilentlyContinue | Select-Object Name,ScriptText | "
         "ConvertTo-Json -Compress"),
    ]
    for label, ps in queries:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=90, creationflags=_NO_WINDOW)
            txt = (out.stdout or "").strip()
            if txt and txt not in ("null", "[]"):
                findings.append((f"WMI:{label}", "subscription", txt[:4000]))
        except Exception as e:
            logger.debug("persistence: WMI %s failed: %s", label, e)
    return findings


def _suspicious(value: str, sig_status: str | None = None):
    reasons = []
    if _OBF.search(value or ""):
        reasons.append("obfuscation/encoded-command")
    low = (value or "").lower()
    if any(t in low for t in _ANOMALOUS_DIRS):
        reasons.append("anomalous-autostart-dir")
    if sig_status and sig_status != "Valid":
        reasons.append(f"unsigned-or-invalid:{sig_status}")
    return reasons


def _load_state():
    try:
        return set(json.loads(_STATE_PATH.read_text(encoding="utf-8")).get("alerted", []))
    except Exception:
        return set()


def _save_state(alerted: set):
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps({"alerted": sorted(alerted)}),
                               encoding="utf-8")
    except Exception as e:
        logger.debug("persistence: state save failed: %s", e)


def _sweep_blocking():
    findings = []
    if _WINREG_OK:
        findings += _collect_registry()
    findings += _collect_startup_folders()
    findings += _collect_scheduled_tasks()
    findings += _collect_wmi_subscriptions()

    paths = [_primary_path(value) for _, _, value in findings]
    sig_map = _authenticode_batch([p for p in paths if p])

    suspicious = []
    seen = set()
    for (vector, name, value), p in zip(findings, paths):
        sig_status = sig_map.get(os.path.normcase(p)) if p else None
        reasons = _suspicious(value, sig_status)
        if not reasons:
            continue
        h = sha256(f"{vector}|{value}".encode("utf-8", "ignore")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        suspicious.append({"vector": vector, "name": name,
                           "value": (value or "")[:500],
                           "reasons": reasons, "h": h})
    return suspicious


async def _alert(correlator, new_findings: list) -> None:
    logger.warning("PERSISTENCE_HUNTER: %d new suspicious autorun(s)", len(new_findings))
    event = {"source": "persistence_hunter", "type": "persistence_detected",
             "severity": 9.0, "findings": new_findings[:50],
             "attck": ["T1546", "T1053", "T1547"], "ts": time.time()}
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
            logger.error("persistence_hunter: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("persistence_hunter: alert dispatch failed: %s", e)


async def _run_sweep(correlator) -> None:
    loop = asyncio.get_running_loop()
    suspicious = await loop.run_in_executor(None, _sweep_blocking)
    alerted = _load_state()
    new = [f for f in suspicious if f["h"] not in alerted]
    if new:
        await _alert(correlator, new)
        for f in new:
            alerted.add(f["h"])
        _save_state(alerted)
    else:
        logger.info("persistence_hunter: sweep clean (%d known suspicious)", len(suspicious))


async def start(correlator=None) -> None:
    """main.py startup hook. Watchdog Pattern: dormant on non-Windows."""
    if not _IS_WINDOWS:
        logger.warning("PERSISTENCE_HUNTER: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    logger.info("PERSISTENCE_HUNTER: armed — sweeping every %dh (warmup %ds)",
                _SWEEP_SECONDS // 3600, _WARMUP_SECONDS)
    await asyncio.sleep(_WARMUP_SECONDS)
    while True:
        try:
            await _run_sweep(correlator)
        except Exception as e:
            logger.debug("persistence_hunter: sweep error: %s", e)
        await asyncio.sleep(_SWEEP_SECONDS)
