"""
tools/osint_engine.py — Automated OSINT IP enrichment engine (v37.0).

Enriches every IP observed in canary/tarpit/Zeek/ETW events.
Sources used based on available API keys:
  - ipinfo.io (free, no key, geo + ASN)
  - Shodan (needs SHODAN_API_KEY)
  - VirusTotal (needs VT_API_KEY, free tier 4 req/min)
  - AlienVault OTX (needs OTX_API_KEY, free)

Results cached in memory. Same IP enriched once per session.
Broadcasts osint_enriched event to HUD with all findings.
"""

import asyncio, ipaddress, os, time
from loguru import logger

_SHODAN_KEY = os.getenv("SHODAN_API_KEY", "")
_VT_KEY     = os.getenv("VT_API_KEY", "")
_OTX_KEY    = os.getenv("OTX_API_KEY", "")

_cache: dict[str, dict] = {}
_rate_limiters: dict[str, float] = {}

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_NETS)
    except ValueError:
        return True


async def _rate_limited(source: str, min_interval: float) -> None:
    """Simple rate limiter per source."""
    last = _rate_limiters.get(source, 0)
    wait = min_interval - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _rate_limiters[source] = time.monotonic()


async def _ipinfo(ip: str) -> dict:
    """Free geo + ASN lookup via ipinfo.io."""
    import aiohttp
    await _rate_limited("ipinfo", 1.0)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"https://ipinfo.io/{ip}/json",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return {
                        "country": data.get("country", ""),
                        "city":    data.get("city", ""),
                        "org":     data.get("org", ""),
                        "asn":     data.get("org", "").split()[0] if data.get("org") else "",
                    }
    except Exception:
        pass
    return {}


async def _shodan(ip: str) -> dict:
    """Shodan host lookup."""
    if not _SHODAN_KEY:
        return {}
    await _rate_limited("shodan", 1.0)
    try:
        import shodan
        loop = asyncio.get_running_loop()
        api  = shodan.Shodan(_SHODAN_KEY)
        host = await loop.run_in_executor(None, api.host, ip)
        return {
            "ports":    host.get("ports", [])[:10],
            "vulns":    list(host.get("vulns", {}).keys())[:5],
            "os":       host.get("os", ""),
            "isp":      host.get("isp", ""),
            "shodan_tags": host.get("tags", []),
        }
    except Exception:
        return {}


async def _virustotal(ip: str) -> dict:
    """VirusTotal IP report (4 req/min free tier)."""
    if not _VT_KEY:
        return {}
    await _rate_limited("virustotal", 15.0)   # 4/min = 15s interval
    import aiohttp
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"https://www.virustotal.com/api/v3/ip_addresses/{ip}",
                headers={"x-apikey": _VT_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    stats = data.get("data", {}).get(
                        "attributes", {}
                    ).get("last_analysis_stats", {})
                    return {
                        "vt_malicious": stats.get("malicious", 0),
                        "vt_suspicious": stats.get("suspicious", 0),
                        "vt_harmless":  stats.get("harmless", 0),
                    }
    except Exception:
        pass
    return {}


async def _otx(ip: str) -> dict:
    """AlienVault OTX threat intelligence."""
    if not _OTX_KEY:
        return {}
    await _rate_limited("otx", 2.0)
    import aiohttp
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"https://otx.alienvault.com/api/v1/indicators/IPv4/{ip}/general",
                headers={"X-OTX-API-KEY": _OTX_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return {
                        "otx_pulse_count": data.get("pulse_info", {}).get("count", 0),
                        "otx_malicious":   data.get("validation", []) != [],
                        "otx_country":     data.get("country_name", ""),
                    }
    except Exception:
        pass
    return {}


async def enrich_ip(ip: str, broadcast_fn) -> dict | None:
    """
    Full OSINT enrichment pipeline for an IP.
    Cached — same IP enriched once per session.
    """
    if not ip or _is_private(ip):
        return None
    if ip in _cache:
        return _cache[ip]

    logger.info(f"OSINT: enriching {ip}")

    # Run all sources concurrently (rate limiters ensure compliance)
    results = await asyncio.gather(
        _ipinfo(ip),
        _shodan(ip),
        _virustotal(ip),
        _otx(ip),
        return_exceptions=True,
    )

    enrichment = {"ip": ip}
    for r in results:
        if isinstance(r, dict):
            enrichment.update(r)

    # Threat score
    threat_score = 0
    if enrichment.get("vt_malicious", 0) > 0:
        threat_score += enrichment["vt_malicious"] * 2
    if enrichment.get("otx_pulse_count", 0) > 0:
        threat_score += min(enrichment["otx_pulse_count"], 5)
    if enrichment.get("vulns"):
        threat_score += len(enrichment["vulns"])
    enrichment["threat_score"] = threat_score

    _cache[ip] = enrichment

    severity = "HIGH" if threat_score >= 5 else \
               "MEDIUM" if threat_score >= 2 else "INFO"

    await broadcast_fn({
        "type":     "osint_enriched",
        "severity": severity,
        **enrichment,
    })

    if threat_score >= 5:
        logger.warning(
            f"OSINT: {ip} is HIGH THREAT — "
            f"VT={enrichment.get('vt_malicious',0)} "
            f"OTX={enrichment.get('otx_pulse_count',0)} "
            f"vulns={enrichment.get('vulns',[])}"
        )

    return enrichment
