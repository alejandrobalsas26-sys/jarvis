"""tools/sysmon_bridge.py — VM Sysmon telemetry bridge (v25.0).

V69 M61.7 (Bandit B314) — this parsed attacker-reachable XML with the stdlib
``xml.etree.ElementTree.fromstring``. The input is a Sysmon event log written by a
monitored VM: precisely untrusted data, since the events describe an attacker's own
process activity and the log is the thing an attacker touches first. stdlib ElementTree
resolves DTDs and entities, so a crafted event could mount billion-laughs entity
expansion (memory exhaustion in the JARVIS process) or an XXE external-entity read
that exfiltrates a local file into a broadcast HUD event.

Parsing is now ``defusedxml``, which forbids DTDs, entity declarations and external
references outright. Absence of ``defusedxml`` is FAIL-CLOSED: the bridge refuses to
parse rather than silently falling back to the unsafe parser. Two size bounds were
added alongside it, because defusedxml does not bound input volume: the reassembly
buffer and each individual event are capped, so an unterminated ``<Event `` can no
longer grow the buffer without limit.
"""

import asyncio, os
from datetime import datetime, timezone
import aiofiles
from loguru import logger

# Hardened XML parsing. NOTE the deliberate absence of an ElementTree fallback: a
# fallback would mean the protection silently disappears in exactly the environment
# that lacks the dependency.
try:
    from defusedxml.ElementTree import ParseError as XMLParseError
    from defusedxml.ElementTree import fromstring as _xml_fromstring
    from defusedxml.common import DefusedXmlException
    _XML_HARDENED = True
except ImportError:                                    # pragma: no cover
    _xml_fromstring = None
    _XML_HARDENED = False

    class XMLParseError(Exception):
        """Placeholder so the except clauses below stay well-formed."""

    class DefusedXmlException(Exception):
        """Placeholder so the except clauses below stay well-formed."""


SYSMON_LOG_PATH = os.getenv("SYSMON_LOG_PATH", "")

#: Hard cap on a single reassembled event. Real Sysmon events are a few KB; anything
#: past this is malformed or hostile, and parsing it buys nothing.
MAX_EVENT_CHARS = 256 * 1024
#: Hard cap on the reassembly buffer. Without it, a log containing "<Event " and no
#: "</Event>" grew the buffer by 4 KB per second forever.
MAX_BUFFER_CHARS = 1024 * 1024

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

    if not _XML_HARDENED:
        # Fail closed and loudly. The alternative — parsing untrusted VM telemetry
        # with the stdlib parser — is the vulnerability this guard exists to prevent.
        logger.error(
            "SYSMON_BRIDGE: refusing to start — 'defusedxml' is not installed, and "
            "Sysmon event XML is untrusted input. Install the soc profile "
            "(pip install -r requirements/soc.txt) to enable this bridge."
        )
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
                if len(buffer) > MAX_BUFFER_CHARS:
                    # An unterminated event, or a log deliberately shaped to grow the
                    # buffer. Keep only the tail so a genuine event straddling the
                    # boundary can still complete, and report the drop honestly.
                    logger.warning(
                        "SYSMON_BRIDGE: reassembly buffer exceeded %d chars with no "
                        "complete event — discarding %d buffered chars",
                        MAX_BUFFER_CHARS, len(buffer) - MAX_EVENT_CHARS,
                    )
                    buffer = buffer[-MAX_EVENT_CHARS:]
    except FileNotFoundError:
        logger.info("SYSMON_BRIDGE: Sysmon not detected — bridge dormant")
        await asyncio.Event().wait()
        return
    except Exception as e:
        logger.error(f"SYSMON_BRIDGE: error: {e}")
        raise


async def _parse_event(xml_str: str, broadcast_fn) -> None:
    if not _XML_HARDENED:                              # pragma: no cover
        return
    if len(xml_str) > MAX_EVENT_CHARS:
        # Reported by length only — the payload itself is never logged.
        logger.warning(
            "SYSMON_BRIDGE: dropped an oversized event (%d chars > %d limit)",
            len(xml_str), MAX_EVENT_CHARS,
        )
        return
    try:
        root = _xml_fromstring(xml_str)
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
    except DefusedXmlException as exc:
        # A DTD, an entity declaration or an external reference — i.e. an actual
        # attack against the parser, not a malformed log line. Reported by TYPE only:
        # the raw event is never logged, so a payload crafted to be read back out of
        # the log (or out of a diagnostics bundle) has nowhere to land.
        logger.warning(
            "SYSMON_BRIDGE: refused a hostile XML event (%s) — %d chars discarded",
            type(exc).__name__, len(xml_str),
        )
    except XMLParseError:
        # Ordinary malformed XML (a truncated tail, an interleaved write). Expected
        # during normal log tailing, so it stays silent as before.
        pass
    except (AttributeError, TypeError, ValueError):
        # Well-formed XML that is not a Sysmon event: a missing EventID makes
        # `root.find(...).text` fail. Not a parser attack, but not an event either.
        pass
