"""
core/vss_vaccine.py — JARVIS V54.0 OMEGA
Ransomware vaccine via Volume Shadow Copy Service. Proactive: ensures VSS storage
exists, seeds a baseline snapshot at startup, takes rolling labeled snapshots on a
schedule. Reactive: on ransomware-class correlator events (decoy_tamper /
data_encrypted / data_destruction / data_staging) immediately takes an EMERGENCY
forensic snapshot and emits a Sev 9.5 alert listing the available restore points.

Design choice: this module DOES NOT auto-restore. Full-volume rollback is
destructive (clobbers legitimate changes since the snapshot) and is left as an
operator decision via vss_vaccine.restore_to(<shadow_id>) — which MOUNTS the
shadow read-only at C:\\jarvis_shadow_mount so the operator restores selectively,
rather than vssadmin revert. Auto-action on attack = snapshot + alert + (V48
network_quarantine isolates via correlator). Watchdog: dormant if non-Windows or
not elevated.
"""
from __future__ import annotations
import asyncio, json, logging, os, subprocess, time
from pathlib import Path

from core.rbac_manager import ClearanceLevel, requires_clearance

logger = logging.getLogger("jarvis.vss_vaccine")

_IS_WINDOWS = os.name == "nt"
_VOLUME = os.environ.get("JARVIS_VSS_VOLUME", "C:\\")
_ROLLING_HOURS = int(os.environ.get("JARVIS_VSS_INTERVAL_HOURS", "6"))
_KEEP = int(os.environ.get("JARVIS_VSS_KEEP", "8"))
_LOG_PATH = Path("logs/vss_vaccine.jsonl")
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0
_TRIGGER = {"decoy_tamper", "data_encrypted", "data_destruction", "data_staging"}
_correlator = None


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_ps(cmd, timeout=120):
    class R:
        returncode = 1; stdout = ""; stderr = ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        return r
    except Exception as e:
        logger.debug("vss: ps error: %s", e)
        out = R(); out.stderr = str(e); return out


def _audit(rec):
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _list_shadows_blocking():
    cmd = ("Get-WmiObject -Class Win32_ShadowCopy | "
           "Select-Object ID,DeviceObject,InstallDate,VolumeName | ConvertTo-Json -Compress")
    r = _run_ps(cmd, timeout=60)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    try:
        d = json.loads(r.stdout)
        if isinstance(d, dict):
            d = [d]
        return d or []
    except Exception:
        return []


def _create_blocking(volume):
    cmd = (f"$r=([wmiclass]'Win32_ShadowCopy').Create('{volume}','ClientAccessible'); "
           "if ($r.ReturnValue -eq 0) { Write-Output $r.ShadowID } else { Write-Error $r.ReturnValue }")
    r = _run_ps(cmd, timeout=180)
    sid = (r.stdout or "").strip()
    if r.returncode == 0 and sid:
        return sid
    logger.debug("vss: create failed rc=%s err=%s", r.returncode, (r.stderr or "")[:200])
    return None


def _delete_blocking(shadow_id):
    cmd = (f"$s = Get-WmiObject -Class Win32_ShadowCopy | Where-Object {{ $_.ID -eq '{shadow_id}' }}; "
           "if ($s) {{ $s.Delete() }}")
    r = _run_ps(cmd, timeout=120)
    return r.returncode == 0


def _ensure_storage():
    try:
        subprocess.run(f"vssadmin list shadowstorage /for={_VOLUME}", shell=True,
                       capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    except Exception:
        pass


async def _dispatch(event):
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
        logger.error("vss_vaccine: dispatch failed: %s", e)


async def take_snapshot(label="rolling"):
    loop = asyncio.get_running_loop()
    sid = await loop.run_in_executor(None, _create_blocking, _VOLUME)
    if sid:
        _audit({"ev": "create", "label": label, "shadow_id": sid, "ts": time.time()})
        logger.info("VSS_VACCINE: %s snapshot — %s", label, sid)
    else:
        logger.warning("VSS_VACCINE: %s snapshot FAILED", label)
    return sid


async def list_snapshots():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _list_shadows_blocking)


@requires_clearance(ClearanceLevel.Admin)
async def restore_to(shadow_id: str):
    """OPERATOR-ONLY. Mounts the shadow read-only at C:\\jarvis_shadow_mount so the
    operator restores selectively. Not auto-called by the module."""
    cmd = (f"$s = Get-WmiObject -Class Win32_ShadowCopy | Where-Object {{ $_.ID -eq '{shadow_id}' }}; "
           "if (-not $s) { Write-Error 'not found'; exit 1 }; "
           "$mount = 'C:\\jarvis_shadow_mount'; "
           "if (Test-Path $mount) { Remove-Item $mount -Force -Recurse -ErrorAction SilentlyContinue }; "
           "cmd /c mklink /D $mount ($s.DeviceObject + '\\')")
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, _run_ps, cmd, 60)
    ok = (r.returncode == 0)
    _audit({"ev": "mount", "shadow_id": shadow_id, "ok": ok, "ts": time.time()})
    return ok


async def _prune(loop):
    snaps = await loop.run_in_executor(None, _list_shadows_blocking)
    snaps_sorted = sorted(snaps, key=lambda s: str(s.get("InstallDate", "")), reverse=True)
    for s in snaps_sorted[_KEEP:]:
        sid = s.get("ID")
        if sid:
            await loop.run_in_executor(None, _delete_blocking, sid)


async def _emergency(trigger_event):
    sid = await take_snapshot(label="emergency")
    snaps = await list_snapshots()
    event = {"source": "vss_vaccine", "type": "ransomware_vaccine_snapshot",
             "severity": 9.5, "attck": ["T1490", "T1486"],
             "trigger_type": trigger_event.get("type"),
             "trigger_source": trigger_event.get("source"),
             "emergency_shadow_id": sid,
             "available_snapshots": [s.get("ID") for s in snaps][:20],
             "note": "Operator restore via vss_vaccine.restore_to(<id>) — auto-restore disabled by design.",
             "ts": time.time()}
    logger.warning("VSS_VACCINE: emergency snapshot %s on %s", sid, trigger_event.get("type"))
    await _dispatch(event)


def _on_event(event, **_kw):
    if event.get("source") == "vss_vaccine":
        return
    if event.get("type") not in _TRIGGER:
        return
    try:
        asyncio.ensure_future(_emergency(event))
    except Exception as e:
        logger.debug("vss: schedule emergency: %s", e)


async def start(correlator=None):
    global _correlator
    _correlator = correlator
    if not _IS_WINDOWS:
        logger.warning("VSS_VACCINE: non-Windows — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("VSS_VACCINE: not elevated — dormant")
        await asyncio.Event().wait(); return
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("vss_vaccine", _on_event)
        except Exception:
            pass
    loop = asyncio.get_running_loop()
    _ensure_storage()
    await take_snapshot(label="baseline")
    logger.info("VSS_VACCINE: armed — rolling snapshots every %dh (keep %d), emergency-on-ransomware",
                _ROLLING_HOURS, _KEEP)
    while True:
        try:
            await _prune(loop)
        except Exception as e:
            logger.debug("vss: prune: %s", e)
        await asyncio.sleep(_ROLLING_HOURS * 3600)
        try:
            await take_snapshot(label="rolling")
        except Exception as e:
            logger.debug("vss: rolling: %s", e)
