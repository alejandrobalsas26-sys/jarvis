"""tools/sysmon_bridge.py — VM Sysmon telemetry bridge (v25.0)."""

import asyncio, os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import aiofiles
from loguru import logger

SYSMON_LOG_PATH = os.getenv("SYSMON_LOG_PATH", "")

SENSITIVE_EVENT_IDS = {1, 3, 7, 8, 10, 11, 25}

TECHNIQUE_MAP = {
    1:  "T1059 — Process Create",
    3:  "T1071 — Network Connection",
    7:  "T1055.001 — DLL Injection",
    8:  "T1055 — CreateRemoteThread",
    10: "T1003.001 — LSASS Credential Access",
    11: "T1105 — File Create",
    25: "T1055.012 — Process Hollowing",
}


async def start_sysmon_bridge(broadcast_fn) -> None:
    from core.telemetry_auth import make_signed_broadcaster
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "sysmon")

    if not SYSMON_LOG_PATH:
        logger.info("SYSMON_BRIDGE: Sysmon not detected — bridge dormant")
        await asyncio.Event().wait()
        return

    if not os.path.exists(SYSMON_LOG_PATH):
        logger.info("SYSMON_BRIDGE: Sysmon not detected — bridge dormant")
        await asyncio.Event().wait()
        return

    try:
        async with aiofiles.open(SYSMON_LOG_PATH, mode="r",
                                  encoding="utf-8", errors="replace") as f:
            await f.seek(0, 2)
            buffer = ""
            while True:
                chunk = await f.read(4096)
                if not chunk:
                    await asyncio.sleep(1.0)
                    continue
                buffer += chunk
                while "<Event " in buffer and "</Event>" in buffer:
                    start = buffer.find("<Event ")
                    end   = buffer.find("</Event>") + len("</Event>")
                    await _parse_event(buffer[start:end], broadcast_fn)
                    buffer = buffer[end:]
    except FileNotFoundError:
        logger.info("SYSMON_BRIDGE: Sysmon not detected — bridge dormant")
        await asyncio.Event().wait()
        return
    except Exception as e:
        logger.error(f"SYSMON_BRIDGE: error: {e}")
        raise


async def _parse_event(xml_str: str, broadcast_fn) -> None:
    try:
        root = ET.fromstring(xml_str)
        ns   = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
        eid  = int(root.find(".//e:EventID", ns).text)
        if eid not in SENSITIVE_EVENT_IDS:
            return
        data = {d.get("Name"): d.text for d in root.findall(".//e:Data", ns)}
        try:
            pid = int(data.get("ProcessId", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        event = {
            "type":        "sysmon_event",
            "event_id":    eid,
            "pid":         pid,
            "technique":   TECHNIQUE_MAP.get(eid, f"EventID {eid}"),
            "process":     (data.get("Image", "") or "")[-60:],
            "commandline": (data.get("CommandLine", "") or "")[:120],
            "parent":      (data.get("ParentImage", "") or "")[-60:],
            "target_ip":   data.get("DestinationIp", ""),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
        await broadcast_fn(event)

        # v33.0 — injection sub-technique classification
        if eid in {1, 3, 8, 10, 25, 30} and pid:
            try:
                import asyncio
                from tools.injection_classifier import analyze_and_broadcast
                asyncio.create_task(analyze_and_broadcast(pid, event, broadcast_fn))
            except Exception:
                pass
    except ET.ParseError:
        pass
