"""
core/cve_intel.py — Real-time CVE intelligence engine (v37.0).

Pulls new CVEs from NVD API v2 (free, no auth required for basic use).
Matches against software inventory observed in JARVIS telemetry.
Searches GitHub for PoC exploits.
Delivers proactive TTS briefings for critical CVEs.

Poll interval: every 6 hours.
Minimum CVSS: 7.0 (high severity only).
"""

import asyncio, os, re
from datetime import datetime, timezone, timedelta
from loguru import logger

_CVSS_MIN       = 7.0
_POLL_INTERVAL  = 21600   # 6 hours
_NVD_API        = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_GITHUB_API     = "https://api.github.com"
_GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")

# Observed software inventory (populated by Zeek/ETW/Sysmon)
_software_inventory: set[str] = set()

_briefed_cves: set[str] = set()   # prevent re-briefing


def update_software_inventory(software_name: str) -> None:
    """Called by Zeek/ETW when a new service/software is observed."""
    normalized = software_name.lower().strip()
    if normalized and len(normalized) > 2:
        _software_inventory.add(normalized)


def _extract_software_from_cve(cve_data: dict) -> list[str]:
    """Extract affected software names from CVE data."""
    software = []
    desc = cve_data.get("descriptions", [{}])
    desc_text = next(
        (d["value"] for d in desc if d.get("lang") == "en"), ""
    ).lower()

    # Common software names in CVE descriptions
    common = [
        "apache", "nginx", "openssh", "openssl", "php", "mysql",
        "postgresql", "windows", "linux", "python", "nodejs",
        "log4j", "spring", "struts", "wordpress", "drupal",
        "vmware", "vsphere", "esxi",
    ]
    for sw in common:
        if sw in desc_text:
            software.append(sw)
    return software


async def _search_github_poc(cve_id: str) -> list[dict]:
    """Search GitHub for PoC exploits for a CVE."""
    import aiohttp
    headers = {}
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{_GITHUB_API}/search/repositories"
                f"?q={cve_id}&sort=stars&per_page=3",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                return [
                    {
                        "name":  item["full_name"],
                        "stars": item["stargazers_count"],
                        "url":   item["html_url"],
                        "desc":  item.get("description", "")[:100],
                    }
                    for item in data.get("items", [])[:3]
                ]
    except Exception:
        return []


async def poll_nvd(broadcast_fn, tts) -> list[dict]:
    """
    Poll NVD for new CVEs in the last 24 hours.
    Match against software inventory. Brief operator on critical finds.
    """
    import aiohttp

    end_date   = datetime.now(timezone.utc)
    start_date = end_date - timedelta(hours=24)

    params = {
        "pubStartDate": start_date.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "pubEndDate":   end_date.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "cvssV3Severity": "HIGH",
    }

    new_cves = []

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                _NVD_API, params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    logger.debug(f"CVE_INTEL: NVD returned {r.status}")
                    return []
                data = await r.json()

        vulns = data.get("vulnerabilities", [])
        logger.info(f"CVE_INTEL: {len(vulns)} new CVEs in last 24h")

        for vuln in vulns:
            cve     = vuln.get("cve", {})
            cve_id  = cve.get("id", "")
            if cve_id in _briefed_cves:
                continue

            # Get CVSS score
            metrics  = cve.get("metrics", {})
            cvss_v3  = metrics.get("cvssMetricV31", [{}])[0]
            cvss_data= cvss_v3.get("cvssData", {})
            score    = cvss_data.get("baseScore", 0.0)

            if score < _CVSS_MIN:
                continue

            # Extract description
            descs   = cve.get("descriptions", [{}])
            desc    = next(
                (d["value"] for d in descs if d.get("lang") == "en"), ""
            )[:300]

            # Match against lab software inventory
            affected_sw = _extract_software_from_cve(cve)
            lab_match   = [
                sw for sw in affected_sw
                if any(sw in inv for inv in _software_inventory)
            ]

            # Search for PoC if high severity
            pocs = []
            if score >= 8.0:
                pocs = await _search_github_poc(cve_id)

            entry = {
                "cve_id":    cve_id,
                "score":     score,
                "severity":  cvss_data.get("baseSeverity", "HIGH"),
                "vector":    cvss_data.get("vectorString", ""),
                "description": desc,
                "lab_match": lab_match,
                "pocs":      pocs,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            new_cves.append(entry)
            _briefed_cves.add(cve_id)

            # Broadcast to HUD
            await broadcast_fn({
                "type":      "cve_alert",
                "severity":  "CRITICAL" if score >= 9.0 else "HIGH",
                **entry,
            })

            # TTS brief for lab matches or critical CVEs
            if lab_match or score >= 9.0:
                brief = (
                    f"{cve_id}: score {score}. "
                    + (f"Affects {', '.join(lab_match[:2])} in your lab. " if lab_match else "")
                    + (f"{len(pocs)} proof-of-concept exploits on GitHub." if pocs else "")
                )
                logger.warning(f"CVE_INTEL: {brief}")
                if tts:
                    asyncio.create_task(tts.speak_async(brief))

    except Exception as e:
        logger.debug(f"CVE_INTEL: poll error: {e}")

    return new_cves


async def start_cve_monitor(broadcast_fn, tts) -> None:
    """Background CVE polling task."""
    logger.info("CVE_INTEL: monitor started — polling NVD every 6 hours")
    while True:
        await poll_nvd(broadcast_fn, tts)
        await asyncio.sleep(_POLL_INTERVAL)
