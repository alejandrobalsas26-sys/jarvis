"""
core/grc_auditor.py — V57.0 NEXUS: Automated GRC Auditor.

Periodic HTML + Markdown compliance reports mapped to NIST CSF and Panama Ley 81.
Writes to logs/grc_report.html and timestamped immutable copies under logs/grc_reports/.
Generates a degraded report with clear status when no telemetry is available.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# ── NIST CSF technique-to-function map ───────────────────────────────────────

_NIST_TECHNIQUE_MAP: dict[str, list[str]] = {
    "ID": ["T1046", "T1595", "T1018", "T1592", "T1590"],
    "PR": ["T1548", "T1547", "T1053", "T1562", "T1078"],
    "DE": ["T1059", "T1055", "T1003", "T1204", "kernel_anomaly"],
    "RS": ["T1071", "T1048", "T1041", "T1567", "T1020"],
    "RC": ["T1486", "T1490", "T1491", "T1561"],
}

_NIST_LABELS: dict[str, str] = {
    "ID": "Identify",
    "PR": "Protect",
    "DE": "Detect",
    "RS": "Respond",
    "RC": "Recover",
}

# Panama Ley 81 — personal-data exfiltration triggers
_LEY81_TECHNIQUES = frozenset({"T1041", "T1567", "T1020", "T1074", "T1005"})
_LEY81_TYPES      = frozenset({"exfil", "data_exfil", "dlp", "data_leak"})


# ── GRCAuditor ────────────────────────────────────────────────────────────────

class GRCAuditor:
    """
    Generates periodic GRC compliance reports from alert telemetry.
    Dormant (loop still runs but skips generation) when JARVIS_GRC_ENABLED is unset.
    """

    _task: asyncio.Task | None = None

    def is_enabled(self) -> bool:
        return os.environ.get("JARVIS_GRC_ENABLED", "").lower() in ("1", "true", "yes")

    def _report_path(self) -> Path:
        return Path(
            os.environ.get("JARVIS_GRC_REPORT_PATH", "logs/grc_report.html")
        )

    def _interval(self) -> int:
        return max(60, int(os.environ.get("JARVIS_GRC_INTERVAL_SECONDS", "86400")))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Periodic audit loop. Runs once immediately then every interval."""
        interval = self._interval()
        logger.info(
            "GRC_AUDITOR: %s — interval=%ds report=%s",
            "ENABLED" if self.is_enabled() else "DISABLED (set JARVIS_GRC_ENABLED=1)",
            interval,
            self._report_path(),
        )
        while True:
            if self.is_enabled():
                try:
                    summary = await self.run_once()
                    logger.info(
                        "GRC_AUDITOR: report written — alerts=%d critical=%d",
                        summary.get("total_alerts", 0),
                        summary.get("critical_alerts", 0),
                    )
                except Exception as e:
                    logger.error("GRC_AUDITOR: run_once failed: %s", e)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Core pipeline ─────────────────────────────────────────────────────────

    async def run_once(self) -> dict:
        """Full audit pass. Returns the summary dict."""
        alerts  = await asyncio.to_thread(self.collect_alerts_24h)
        summary = await asyncio.to_thread(self.map_to_controls, alerts)
        md      = await asyncio.to_thread(self.render_markdown, summary)
        html    = await asyncio.to_thread(self.render_html, summary)
        paths   = await self.write_immutable_report(html, md)
        summary["report_paths"] = paths
        return summary

    def collect_alerts_24h(self) -> list[dict]:
        """
        Collect alerts from the last 24 hours.
        Reads correlator active incidents; falls back gracefully if unavailable.
        """
        alerts: list[dict] = []

        # 1. Active incidents from the in-memory correlator
        try:
            from core.correlator import correlator
            for inc in correlator.get_active_incidents():
                alerts.append(inc)
        except Exception as e:
            logger.debug("GRC_AUDITOR: correlator unavailable: %s", e)

        # 2. DB query if V55 db_manager pool is live
        try:
            from core.correlator import correlator as _corr
            db = getattr(_corr, "_db_manager", None)
            if db is not None and getattr(db, "_pool", None) is not None:
                cutoff = time.time() - 86400
                import asyncio as _a
                import json as _json

                async def _fetch() -> list[dict]:
                    async with db._pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT payload FROM jarvis_alerts "
                            "WHERE created_at >= $1 ORDER BY created_at DESC LIMIT 1000",
                            cutoff,
                        )
                    return [_json.loads(r["payload"]) for r in rows]

                loop = _a.get_event_loop()
                if loop.is_running():
                    # We are already inside an async context; skip blocking call.
                    # The caller (run_once) must await this differently if needed.
                    pass
                else:
                    db_alerts = loop.run_until_complete(_fetch())
                    alerts.extend(db_alerts)
        except Exception as e:
            logger.debug("GRC_AUDITOR: DB query skipped: %s", e)

        logger.debug("GRC_AUDITOR: collected %d alerts for 24h window", len(alerts))
        return alerts

    def map_to_controls(self, alerts: list[dict]) -> dict:
        """Map alerts to NIST CSF functions and compute management metrics."""
        now_ts   = datetime.now(timezone.utc).isoformat()
        total    = len(alerts)
        critical = 0
        high     = 0
        tactics: dict[str, int] = {}
        hosts:   set[str]       = set()
        contain_ok   = 0
        contain_fail = 0
        ley81_hits   = 0

        nist: dict[str, list[str]] = {k: [] for k in _NIST_LABELS}

        for alert in alerts:
            sev = float(alert.get("severity_score") or alert.get("severity") or 0)
            if sev >= 9.0:
                critical += 1
            elif sev >= 7.0:
                high += 1

            phase = alert.get("kill_chain_phase", "Unknown")
            tactics[phase] = tactics.get(phase, 0) + 1

            for h in (alert.get("involved_hosts") or []):
                hosts.add(str(h))

            status = str(alert.get("status", "")).upper()
            if status == "RESOLVED":
                contain_ok += 1
            elif status == "ACTIVE":
                contain_fail += 1

            techniques = alert.get("mitre_techniques") or []
            if isinstance(techniques, str):
                techniques = [techniques]

            for func, func_tech in _NIST_TECHNIQUE_MAP.items():
                for t in techniques:
                    if any(str(t).upper().startswith(ft.upper()) for ft in func_tech):
                        rule_id = f"{alert.get('incident_id','?')}/{t}"
                        if rule_id not in nist[func]:
                            nist[func].append(rule_id)

            etype = str(alert.get("type", "")).lower()
            if (any(str(t) in _LEY81_TECHNIQUES for t in techniques)
                    or any(kw in etype for kw in _LEY81_TYPES)):
                ley81_hits += 1

        # MTTD estimate: mean of (last_seen - first_seen) as detection dwell proxy
        mttd_secs = 0.0
        if alerts:
            deltas: list[float] = []
            for a in alerts:
                try:
                    from datetime import datetime as _dt
                    fs = _dt.fromisoformat(
                        str(a["first_seen"]).replace("Z", "+00:00")
                    )
                    ls = _dt.fromisoformat(
                        str(a["last_seen"]).replace("Z", "+00:00")
                    )
                    deltas.append((ls - fs).total_seconds())
                except Exception:
                    pass
            if deltas:
                mttd_secs = sum(deltas) / len(deltas)

        top_tactics = sorted(tactics.items(), key=lambda x: -x[1])[:5]
        recommendations = _build_recommendations(nist, critical, high, ley81_hits)

        return {
            "generated_at":        now_ts,
            "window_hours":        24,
            "total_alerts":        total,
            "critical_alerts":     critical,
            "high_alerts":         high,
            "mttd_seconds":        round(mttd_secs, 1),
            "containment_success": contain_ok,
            "containment_failure": contain_fail,
            "top_tactics":         top_tactics,
            "affected_hosts":      sorted(hosts),
            "panama_ley81_hits":   ley81_hits,
            "nist_csf":            {
                k: {"label": _NIST_LABELS[k], "events": v}
                for k, v in nist.items()
            },
            "recommendations":     recommendations,
            "data_available":      total > 0,
        }

    def render_markdown(self, summary: dict) -> str:
        lines = [
            "# JARVIS GRC Compliance Report",
            f"**Generated:** {summary['generated_at']}  ",
            f"**Window:** Last {summary['window_hours']}h  ",
            "",
        ]
        if not summary.get("data_available"):
            lines.append("> **Status:** No telemetry available in this window.")
            lines.append("")

        # Always include summary table and NIST section (shows zeros on degraded report)
        lines += [
            "## Executive Summary",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Total Alerts | {summary['total_alerts']} |",
            f"| Critical (≥9.0) | {summary['critical_alerts']} |",
            f"| High (≥7.0) | {summary['high_alerts']} |",
            f"| Est. MTTD | {_fmt_duration(summary['mttd_seconds'])} |",
            f"| Containment Success | {summary['containment_success']} |",
            f"| Containment Failure | {summary['containment_failure']} |",
            f"| Panama Ley 81 Relevant | {summary['panama_ley81_hits']} |",
            "",
            "## NIST CSF Coverage",
        ]
        for func, data in summary["nist_csf"].items():
            hit = len(data["events"])
            lines.append(f"- **{func} — {data['label']}**: {hit} event(s)")
        lines += [
            "",
            "## Top Tactics",
            *([f"- {t}: {c}" for t, c in summary["top_tactics"]] or ["- No data"]),
            "",
            "## Affected Hosts",
            *([f"- {h}" for h in summary["affected_hosts"]] or ["- None"]),
            "",
            "## Recommendations",
            *[f"- {r}" for r in summary["recommendations"]],
        ]
        return "\n".join(lines)

    def render_html(self, summary: dict) -> str:
        ts     = html.escape(str(summary["generated_at"]))
        avail  = summary.get("data_available", False)
        crit   = summary.get("critical_alerts", 0)
        badge_color = "#e74c3c" if crit > 0 else "#27ae60"

        esc = html.escape
        nist_rows = "".join(
            f"<tr><td><b>{esc(str(func))}</b></td>"
            f"<td>{esc(str(data['label']))}</td>"
            f"<td style='color:{'#e74c3c' if len(data['events']) else '#27ae60'};"
            f"font-weight:bold'>{len(data['events'])}</td></tr>"
            for func, data in summary.get("nist_csf", {}).items()
        )
        recs = "".join(
            f"<li>{esc(str(r))}</li>" for r in summary.get("recommendations", [])
        )
        tactics_rows = "".join(
            f"<tr><td>{esc(str(t))}</td><td>{esc(str(c))}</td></tr>"
            for t, c in summary.get("top_tactics", [])
        )
        hosts_list = "".join(
            f"<li>{esc(str(h))}</li>" for h in summary.get("affected_hosts", [])
        ) or "<li>None</li>"
        status_block = (
            '<div class="warn">&#9888; No telemetry available in this window.</div>'
            if not avail else ""
        )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>JARVIS GRC Report &mdash; {ts[:10]}</title>
<style>
body{{font-family:monospace;background:#0d0d0d;color:#e0e0e0;margin:2em;}}
h1{{color:#00d4ff;}}
h2{{color:#00b4d8;border-bottom:1px solid #333;padding-bottom:4px;}}
table{{border-collapse:collapse;width:100%;margin:1em 0;}}
th,td{{border:1px solid #333;padding:8px 12px;text-align:left;}}
th{{background:#1a1a2e;color:#00d4ff;}}
tr:nth-child(even){{background:#111;}}
.badge{{display:inline-block;background:{badge_color};color:#fff;
        padding:4px 12px;border-radius:12px;font-weight:bold;}}
.warn{{background:#5a2d00;color:#ffa500;padding:10px;border-radius:4px;margin:1em 0;}}
ul{{margin:0.5em 0;}} li{{margin:0.3em 0;}}
</style>
</head>
<body>
<h1>JARVIS GRC Compliance Report</h1>
<p>Generated: <b>{ts}</b> &nbsp;|&nbsp; Window: Last {summary.get('window_hours',24)}h</p>
{status_block}
<h2>Executive Summary</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Total Alerts</td><td>{summary.get('total_alerts',0)}</td></tr>
<tr><td>Critical (&ge;9.0)</td>
    <td><span class="badge">{crit}</span></td></tr>
<tr><td>High (&ge;7.0)</td><td>{summary.get('high_alerts',0)}</td></tr>
<tr><td>Estimated MTTD</td><td>{_fmt_duration(summary.get('mttd_seconds',0))}</td></tr>
<tr><td>Containment Success</td><td>{summary.get('containment_success',0)}</td></tr>
<tr><td>Containment Failure</td><td>{summary.get('containment_failure',0)}</td></tr>
<tr><td>Panama Ley 81 Relevant</td><td>{summary.get('panama_ley81_hits',0)}</td></tr>
</table>
<h2>NIST CSF Coverage</h2>
<table>
<tr><th>Function</th><th>Category</th><th>Events Detected</th></tr>
{nist_rows}
</table>
<h2>Top Tactics / Kill Chain Phases</h2>
<table><tr><th>Phase</th><th>Count</th></tr>{tactics_rows or '<tr><td colspan="2">No data</td></tr>'}</table>
<h2>Affected Hosts</h2>
<ul>{hosts_list}</ul>
<h2>Recommendations</h2>
<ul>{recs}</ul>
<hr>
<p style="color:#555;font-size:0.85em">JARVIS V57.0 NEXUS &mdash; Automated GRC Auditor</p>
</body>
</html>"""

    @staticmethod
    def _append_manifest(archive_dir: Path, files: dict[str, str]) -> None:
        """
        Append a SHA256 integrity record for the archived report to
        logs/grc_reports/manifest.jsonl (one JSON object per line).
        """
        try:
            entry = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "files": {},
            }
            for label, path_str in files.items():
                p = Path(path_str)
                if not p.exists():
                    continue
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                entry["files"][label] = {
                    "name":   p.name,
                    "sha256": digest,
                    "bytes":  p.stat().st_size,
                }
            manifest = archive_dir / "manifest.jsonl"
            with open(manifest, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("GRC_AUDITOR: manifest update failed: %s", e)

    async def write_immutable_report(self, html: str, markdown: str) -> dict:
        """Write the current report and an immutable timestamped archive copy."""
        try:
            import aiofiles
        except ImportError:
            logger.warning("GRC_AUDITOR: aiofiles not available — using sync write")
            return await asyncio.to_thread(
                self._write_sync, html, markdown
            )

        report_path = self._report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)

        archive_dir = report_path.parent / "grc_reports"
        archive_dir.mkdir(parents=True, exist_ok=True)

        ts_str       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive_html = archive_dir / f"grc_report_{ts_str}.html"
        archive_md   = archive_dir / f"grc_report_{ts_str}.md"

        try:
            async with aiofiles.open(report_path, "w", encoding="utf-8") as f:
                await f.write(html)
            async with aiofiles.open(archive_html, "w", encoding="utf-8") as f:
                await f.write(html)
            async with aiofiles.open(archive_md, "w", encoding="utf-8") as f:
                await f.write(markdown)

            await asyncio.to_thread(
                self._append_manifest, archive_dir,
                {"html": str(archive_html), "md": str(archive_md)},
            )
            logger.info("GRC_AUDITOR: report written → %s", report_path)
            return {
                "current":      str(report_path),
                "archive_html": str(archive_html),
                "archive_md":   str(archive_md),
            }
        except Exception as e:
            logger.error("GRC_AUDITOR: write failed: %s", e)
            return {"error": str(e)}

    def _write_sync(self, html: str, markdown: str) -> dict:
        report_path = self._report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        archive_dir = report_path.parent / "grc_reports"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts_str       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive_html = archive_dir / f"grc_report_{ts_str}.html"
        archive_md   = archive_dir / f"grc_report_{ts_str}.md"
        report_path.write_text(html, encoding="utf-8")
        archive_html.write_text(html, encoding="utf-8")
        archive_md.write_text(markdown, encoding="utf-8")
        self._append_manifest(
            archive_dir, {"html": str(archive_html), "md": str(archive_md)}
        )
        return {
            "current":      str(report_path),
            "archive_html": str(archive_html),
            "archive_md":   str(archive_md),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_duration(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


def _build_recommendations(
    nist: dict[str, list[str]],
    critical: int,
    high: int,
    ley81: int,
) -> list[str]:
    # nist values are plain lists of event IDs (not dicts)
    recs: list[str] = []
    if critical > 0:
        recs.append(
            f"CRITICAL: {critical} incident(s) require immediate executive escalation."
        )
    if nist.get("RS"):
        recs.append(
            "Respond: Review active C2/exfiltration incidents and isolate affected hosts."
        )
    if nist.get("DE"):
        recs.append(
            "Detect: Validate YARA/Sigma rule coverage for detected injection/execution techniques."
        )
    if nist.get("PR"):
        recs.append(
            "Protect: Audit persistence mechanisms, scheduled tasks, and privilege escalation paths."
        )
    if ley81 > 0:
        recs.append(
            f"Panama Ley 81: {ley81} potential personal-data incident(s) — "
            "notify the Autoridad Nacional de Protección de Datos within the statutory window."
        )
    if not recs:
        recs.append("No critical findings this period. Maintain current monitoring cadence.")
    return recs


# Module-level singleton
grc_auditor = GRCAuditor()
