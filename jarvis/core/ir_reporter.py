"""
core/ir_reporter.py — JARVIS V48.0 VANGUARD
Automated IR forensics. Converts a mitigated/critical incident (event JSON) into
a compliance-ready Markdown report under logs/ir_reports/, mapping activity and
response actions to MITRE ATT&CK and NIST CSF 2.0.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.ir_reporter")

_REPORT_DIR = Path("logs/ir_reports")

_ATTCK = {
    "T1003": "OS Credential Dumping",
    "T1018": "Remote System Discovery",
    "T1021": "Remote Services (Lateral Movement)",
    "T1046": "Network Service Discovery",
    "T1055": "Process Injection",
    "T1059": "Command and Scripting Interpreter",
    "T1078": "Valid Accounts",
    "T1190": "Exploit Public-Facing Application",
    "T1485": "Data Destruction",
    "T1486": "Data Encrypted for Impact",
    "T1490": "Inhibit System Recovery",
    "T1620": "Reflective Code Loading",
}

_NIST = {
    "decoy_tamper":       ("DETECT",  "DE.CM-9 Deception technology monitoring"),
    "honeytoken_use":     ("DETECT",  "DE.CM-3 Personnel/credential activity monitored"),
    "memory_yara_match":  ("DETECT",  "DE.AE-2 Detected events analyzed"),
    "lateral_movement":   ("DETECT",  "DE.CM-1 Network monitoring"),
    "network_scan":       ("DETECT",  "DE.CM-1 Network monitoring"),
    "host_quarantined":   ("RESPOND", "RS.MI-1 Incidents are contained"),
    "process_terminated": ("RESPOND", "RS.MI-1 Incidents are contained"),
}


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def _attck_rows(event: dict) -> str:
    tids = event.get("attck") or event.get("technique") or []
    if isinstance(tids, str):
        tids = [tids]
    if not tids:
        return "| _none mapped_ |  |  |"
    rows = []
    for t in tids:
        t = str(t).upper()
        name = _ATTCK.get(t.split(".")[0], _ATTCK.get(t, "Uncategorized"))
        rows.append(f"| {t} | {name} | observed |")
    return "\n".join(rows)


def _build_markdown(event: dict) -> str:
    ts = event.get("ts", time.time())
    sev = event.get("severity", "n/a")
    etype = str(event.get("type", "unknown"))
    nist = _NIST.get(etype, ("DETECT", "DE.AE Anomalies and events"))
    offenders = event.get("offenders") or event.get("killed_pids") or []
    m = []
    m.append(f"# JARVIS Incident Report — {etype}")
    m.append("")
    m.append(f"- Report generated: {_fmt_ts(time.time())}")
    m.append(f"- Incident timestamp: {_fmt_ts(ts)}")
    m.append(f"- Severity: {sev}")
    m.append(f"- Detection source: {event.get('source', 'unknown')}")
    m.append(f"- Primary classification: NIST CSF {nist[0]} — {nist[1]}")
    m.append("")
    m.append("## 1. Executive Summary")
    m.append(f"A severity-{sev} incident of type `{etype}` was detected by "
             f"`{event.get('source', 'unknown')}` and processed by the JARVIS "
             f"correlator. Automated containment was applied where policy permitted.")
    m.append("")
    m.append("## 2. Affected Assets / Indicators")
    any_ind = False
    for k in ("pid", "proc_name", "proc_path", "decoy", "ip", "src_ip", "rules",
              "username", "event_id", "record"):
        if event.get(k) is not None:
            m.append(f"- **{k}**: `{event.get(k)}`")
            any_ind = True
    if not any_ind:
        m.append("- _no discrete indicators recorded_")
    m.append("")
    m.append("## 3. MITRE ATT&CK Mapping")
    m.append("| Technique | Name | Status |")
    m.append("|---|---|---|")
    m.append(_attck_rows(event))
    m.append("")
    m.append("## 4. NIST CSF 2.0 Mapping")
    m.append("| Function | Category | Evidence |")
    m.append("|---|---|---|")
    m.append(f"| {nist[0]} | {nist[1]} | detection event `{etype}` |")
    if event.get("killed_pids") or event.get("host_isolated"):
        m.append("| RESPOND | RS.MI-1 Incidents are contained | automated containment executed |")
    m.append("")
    m.append("## 5. Response Actions")
    acted = False
    if offenders:
        m.append(f"- Offending processes/handles: `{json.dumps(offenders, default=str)[:500]}`")
        acted = True
    if event.get("killed_pids"):
        m.append(f"- Terminated PID(s): `{event.get('killed_pids')}`")
        acted = True
    if event.get("host_isolated"):
        m.append("- Host-firewall quarantine applied (bidirectional block).")
        acted = True
    if event.get("nac_isolated"):
        m.append("- Network infrastructure quarantine requested (NAC/switchport).")
        acted = True
    if not acted:
        m.append("- Detection only; no automated containment recorded for this event.")
    m.append("")
    if event.get("compliance"):
        m.append("## 5b. Regulatory / Compliance Impact")
        m.append("| Framework | Control |")
        m.append("|---|---|")
        for c in event.get("compliance", [])[:25]:
            m.append(f"| {c.get('framework','')} | {c.get('control','')} |")
        m.append("")
    m.append("## 6. Raw Event")
    m.append("    " + json.dumps(event, default=str)[:4000])  # indented block (no fences)
    m.append("")
    m.append("## 7. Recommendations")
    m.append("- Validate containment, preserve volatile artifacts, confirm no lateral spread.")
    m.append("- Review correlated events within the incident window for additional TTPs.")
    return "\n".join(m)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def generate_report(event: dict, correlator=None) -> Optional[str]:
    try:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        safe_type = "".join(c if c.isalnum() else "_" for c in str(event.get("type", "incident")))
        path = _REPORT_DIR / f"{stamp}_{safe_type}.md"
        loop = asyncio.get_running_loop()
        md = _build_markdown(event)
        await loop.run_in_executor(None, _write, path, md)
        logger.info("ir_reporter: report written %s", path)
        return str(path)
    except Exception as e:
        logger.error("ir_reporter: report generation failed: %s", e)
        return None


async def start(correlator=None) -> None:
    """main.py startup hook. JARVIS Watchdog Pattern: dormant if the report
    directory cannot be created."""
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("IR_REPORTER: cannot create report dir (%s) — dormant", e)
        await asyncio.Event().wait(); return
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("ir_reporter", generate_report)
        except Exception:
            pass
    logger.info("IR_REPORTER: armed — compliance reporting ready (%s)", _REPORT_DIR)
    await asyncio.Event().wait()
