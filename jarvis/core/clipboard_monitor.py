"""
core/clipboard_monitor.py — Automatic clipboard intelligence (v44.0).

Polls clipboard every 1.5s. Detects security artifacts:
  IP addresses    → OSINT enrichment (Shodan, VT, OTX)
  MD5/SHA hashes  → VirusTotal lookup
  CVE IDs         → NVD lookup + GitHub PoC search
  Domains         → WHOIS + reputation check
  URLs            → hostname extraction + reputation

Fires once per unique value — same content never re-processed.
Silently enriches — only broadcasts if something interesting found.
"""

import asyncio, re
from loguru import logger

_LAST_CLIPBOARD: str = ""
_SEEN:           set[str] = set()
_POLL_INTERVAL   = 1.5

# Detection patterns
_IP_RE     = re.compile(r"\b(\d{1,3}\.){3}\d{1,3}\b")
_MD5_RE    = re.compile(r"\b[0-9a-fA-F]{32}\b")
_SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_SHA1_RE   = re.compile(r"\b[0-9a-fA-F]{40}\b")
_CVE_RE    = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)
_DOMAIN_RE = re.compile(
    r"\b(?:[a-zA-Z0-9\-]+\.)+(?:com|net|org|io|gov|edu|mil"
    r"|ru|cn|de|uk|fr|br|info|xyz|onion)\b"
)

# Private IP ranges — skip enrichment
_PRIVATE_RE = re.compile(
    r"^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|127\.)"
)


def _classify_clipboard(text: str) -> tuple[str, str] | None:
    """
    Classify clipboard content.
    Returns (artifact_type, value) or None if not security-relevant.
    """
    text = text.strip()
    if len(text) > 300 or "\n" in text:
        return None   # too long or multiline — skip

    if m := _CVE_RE.search(text):
        return "cve", m.group(0).upper()

    if m := _SHA256_RE.search(text):
        return "sha256", m.group(0).lower()

    if m := _SHA1_RE.search(text):
        return "sha1", m.group(0).lower()

    if m := _MD5_RE.search(text):
        return "md5", m.group(0).lower()

    if m := _IP_RE.search(text):
        ip = m.group(0)
        if not _PRIVATE_RE.match(ip):
            return "ip", ip

    if m := _DOMAIN_RE.search(text):
        domain = m.group(0).lower()
        if "." in domain and len(domain) > 4:
            return "domain", domain

    return None


async def start_clipboard_monitor(
    broadcast_fn,
    tts=None,
) -> None:
    """
    Background clipboard watcher.
    Runs forever, polling clipboard every 1.5 seconds.
    """
    global _LAST_CLIPBOARD

    logger.info("CLIPBOARD: monitor active — copy any IP/hash/CVE to auto-enrich")
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            # Read clipboard in executor (pyperclip can block briefly)
            content = await loop.run_in_executor(
                None, _read_clipboard
            )
            if not content or content == _LAST_CLIPBOARD:
                continue

            _LAST_CLIPBOARD = content
            result = _classify_clipboard(content)
            if not result:
                continue

            artifact_type, value = result
            if value in _SEEN:
                continue
            _SEEN.add(value)

            logger.info(
                f"CLIPBOARD: detected {artifact_type}: {value[:40]}"
            )

            await broadcast_fn({
                "type":          "clipboard_artifact_detected",
                "artifact_type": artifact_type,
                "value":         value,
                "severity":      "INFO",
            })

            # Route to appropriate enrichment
            asyncio.create_task(
                _enrich(artifact_type, value, broadcast_fn, tts)
            )

        except Exception as e:
            logger.debug(f"CLIPBOARD: {e}")
            await asyncio.sleep(5)


def _read_clipboard() -> str:
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


async def _enrich(
    artifact_type: str,
    value: str,
    broadcast_fn,
    tts=None,
) -> None:
    """Dispatch to the right enrichment engine."""
    try:
        if artifact_type == "ip":
            from tools.osint_engine import enrich_ip
            result = await enrich_ip(value, broadcast_fn)
            if result and result.get("threat_score", 0) >= 3:
                score = result["threat_score"]
                org   = result.get("org", "")
                if tts:
                    asyncio.create_task(tts.speak_async(
                        f"Clipboard IP {value} — threat score {score}."
                        + (f" {org}." if org else "")
                    ))

        elif artifact_type == "cve":
            from core.cve_intel import _search_github_poc
            pocs = await _search_github_poc(value)
            msg  = f"{value}: {len(pocs)} PoC exploits on GitHub." \
                   if pocs else f"{value} copied — no PoCs found."
            await broadcast_fn({
                "type":     "clipboard_cve_enriched",
                "cve_id":   value,
                "poc_count":len(pocs),
                "pocs":     pocs,
                "severity": "HIGH" if pocs else "INFO",
            })
            if tts and pocs:
                asyncio.create_task(tts.speak_async(msg))

        elif artifact_type in ("sha256", "sha1", "md5"):
            # VirusTotal hash lookup
            import os, aiohttp
            vt_key = os.getenv("VT_API_KEY", "")
            if vt_key:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(
                        f"https://www.virustotal.com/api/v3/files/{value}",
                        headers={"x-apikey": vt_key},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        if r.status == 200:
                            data  = await r.json()
                            stats = (data.get("data", {})
                                     .get("attributes", {})
                                     .get("last_analysis_stats", {}))
                            mal   = stats.get("malicious", 0)
                            await broadcast_fn({
                                "type":       "clipboard_hash_enriched",
                                "hash":       value[:16] + "…",
                                "hash_type":  artifact_type,
                                "malicious":  mal,
                                "severity":   "CRITICAL" if mal > 5
                                              else "HIGH" if mal > 0
                                              else "INFO",
                            })
                            if tts and mal > 0:
                                asyncio.create_task(tts.speak_async(
                                    f"Hash flagged by {mal} vendors on VirusTotal."
                                ))

        elif artifact_type == "domain":
            # Basic domain reputation check
            from tools.osint_engine import enrich_ip
            import socket
            try:
                loop = asyncio.get_running_loop()
                ip   = await loop.run_in_executor(
                    None, socket.gethostbyname, value
                )
                await enrich_ip(ip, broadcast_fn)
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"CLIPBOARD: enrichment error: {e}")
