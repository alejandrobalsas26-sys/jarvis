"""
core/amsi_bridge.py — JARVIS V49.0 OMNISCIENCE
AMSI/ETW visibility. Subscribes to the Microsoft-Antimalware-Scan-Interface ETW
provider via pywintrace and inspects post-deobfuscation script buffers in real
time. Matches malicious PowerShell tradecraft (T1059.001) AND AMSI-bypass/tamper
artifacts (T1562.001), dispatching alerts to the correlator.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import deque

logger = logging.getLogger("jarvis.amsi_bridge")

_IS_WINDOWS = os.name == "nt"

try:
    from etw import ETW, ProviderInfo
    from etw.GUID import GUID
    _PYWINTRACE_OK = True
except Exception:
    ETW = None
    ProviderInfo = None
    GUID = None
    _PYWINTRACE_OK = False

try:
    import yara
    _YARA_OK = True
except Exception:
    yara = None
    _YARA_OK = False

_AMSI_GUID = "{2A576B87-09A7-520E-C21A-4942F0271D67}"
_YARA_DIR = os.environ.get("JARVIS_YARA_RULES", "rules")
_LOG_ALL = os.environ.get("JARVIS_AMSI_DEBUG", "0") == "1"

_MALICIOUS = re.compile(
    r"\bIEX\b|Invoke-Expression|DownloadString|DownloadFile|DownloadData|"
    r"Net\.WebClient|Invoke-WebRequest|Start-BitsTransfer|EncodedCommand|"
    r"\s-enc\b|FromBase64String|Reflection\.Assembly|Add-Type|"
    r"Invoke-Mimikatz|Invoke-Shellcode|Invoke-DllInjection|"
    r"\brundll32\b|\bregsvr32\b|\bmshta\b|certutil.+-decode",
    re.IGNORECASE)
_TAMPER = re.compile(
    r"amsiInitFailed|AmsiScanBuffer|AmsiUtils|amsiContext|amsiSession|"
    r"\[Ref\]\.Assembly|GetField\(.+amsi|VirtualProtect|amsi\.dll",
    re.IGNORECASE)

_compiled_yara = None
_recent = deque(maxlen=256)
_recent_set = set()


def _compile_yara():
    global _compiled_yara
    if not _YARA_OK or not os.path.isdir(_YARA_DIR):
        return
    srcs = {}
    i = 0
    for root, _dirs, files in os.walk(_YARA_DIR):
        for fn in files:
            if fn.lower().endswith((".yar", ".yara")):
                srcs[f"ns{i}"] = os.path.join(root, fn)
                i += 1
    if srcs:
        try:
            _compiled_yara = yara.compile(filepaths=srcs)
        except Exception as e:
            logger.debug("amsi_bridge: yara compile failed: %s", e)


def _dedup(text: str) -> bool:
    h = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
    if h in _recent_set:
        return True
    _recent.append(h)
    _recent_set.add(h)
    while len(_recent_set) > _recent.maxlen:
        old = _recent.popleft()
        _recent_set.discard(old)
    return False


def _scan(text: str) -> dict:
    hits = {"malicious": sorted(set(m.group(0) for m in _MALICIOUS.finditer(text)))[:20],
            "tamper": sorted(set(m.group(0) for m in _TAMPER.finditer(text)))[:20],
            "yara": []}
    if _compiled_yara is not None:
        try:
            hits["yara"] = [m.rule for m in _compiled_yara.match(data=text.encode("utf-8", "ignore"))][:20]
        except Exception:
            pass
    return hits


def _event_text(event) -> str:
    parts = []
    try:
        items = event.items() if hasattr(event, "items") else []
    except Exception:
        items = []
    for _k, v in items:
        if isinstance(v, str) and len(v) > 3:
            parts.append(v)
    return "\n".join(parts)


async def _alert(correlator, kind: str, hits: dict, sample: str) -> None:
    if kind == "tamper":
        etype, sev, attck = "amsi_tamper", 9.5, ["T1562.001", "T1059.001"]
    else:
        etype, sev, attck = "malicious_powershell", 9.0, ["T1059.001"]
    logger.warning("AMSI_BRIDGE: %s — %s", etype, hits)
    event = {"source": "amsi_bridge", "type": etype, "severity": sev,
             "indicators": hits, "sample": sample[:1200], "attck": attck,
             "ts": time.time()}
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
            logger.error("amsi_bridge: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("amsi_bridge: alert dispatch failed: %s", e)


async def start(correlator=None) -> None:
    """main.py startup hook. Watchdog Pattern: dormant if non-Windows, pywintrace
    missing, or not elevated (ETW AMSI session requires admin)."""
    if not _IS_WINDOWS:
        logger.warning("AMSI_BRIDGE: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not _PYWINTRACE_OK:
        logger.warning("AMSI_BRIDGE: pywintrace unavailable — dormant")
        await asyncio.Event().wait(); return
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            logger.warning("AMSI_BRIDGE: not elevated (admin required) — dormant")
            await asyncio.Event().wait(); return
    except Exception:
        logger.warning("AMSI_BRIDGE: admin check failed — dormant")
        await asyncio.Event().wait(); return

    _compile_yara()
    loop = asyncio.get_running_loop()

    def _cb(record):
        try:
            event = record[1] if isinstance(record, (tuple, list)) and len(record) > 1 else record
            text = _event_text(event)
            if not text:
                return
            if _LOG_ALL:
                logger.debug("amsi_bridge[buffer]: %s", text[:500])
            if _dedup(text):
                return
            hits = _scan(text)
            if hits["tamper"]:
                asyncio.run_coroutine_threadsafe(_alert(correlator, "tamper", hits, text), loop)
            elif hits["malicious"] or hits["yara"]:
                asyncio.run_coroutine_threadsafe(_alert(correlator, "malicious", hits, text), loop)
        except Exception as e:
            logger.debug("amsi_bridge: callback error: %s", e)

    try:
        providers = [ProviderInfo("AMSI", GUID(_AMSI_GUID))]
        job = ETW(providers=providers, event_callback=_cb)
        await loop.run_in_executor(None, job.start)
    except Exception as e:
        logger.warning("AMSI_BRIDGE: ETW session start failed (%s) — dormant", e)
        await asyncio.Event().wait(); return

    logger.info("AMSI_BRIDGE: armed — consuming AMSI ETW provider (yara=%s, debug=%s)",
                _compiled_yara is not None, _LOG_ALL)
    try:
        await asyncio.Event().wait()
    finally:
        try:
            job.stop()
        except Exception:
            pass
