"""
core/soar_enrichment.py — JARVIS V50.0 NEXUS
SOAR CTI enrichment. Extracts IoCs (public IPv4 / file hashes) from an event and
asynchronously queries VirusTotal v3 and AlienVault OTX (aiohttp), appending
vt_score and otx_tags to the event dict. TTL-cached; VT public API rate-limited.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import time

logger = logging.getLogger("jarvis.soar_enrichment")

try:
    import aiohttp
    _AIOHTTP_OK = True
except Exception:
    aiohttp = None
    _AIOHTTP_OK = False

_VT_KEY = os.environ.get("JARVIS_VT_API_KEY") or os.environ.get("VT_API_KEY")
_OTX_KEY = os.environ.get("JARVIS_OTX_API_KEY") or os.environ.get("OTX_API_KEY")

_CACHE_TTL = 3600
_VT_MIN_INTERVAL = 16.0          # public API ~4 req/min
_HTTP_TIMEOUT = 15
_MAX_IOCS = 5

_CACHE: dict = {}
_last_vt = 0.0
_vt_lock = asyncio.Lock()
_HASH_RE = re.compile(r"\b([a-fA-F0-9]{64}|[a-fA-F0-9]{40}|[a-fA-F0-9]{32})\b")


def _cache_get(key):
    v = _CACHE.get(key)
    if v and (time.time() - v[0]) < _CACHE_TTL:
        return v[1]
    return None


def _cache_put(key, data):
    _CACHE[key] = (time.time(), data)


def _extract_iocs(event: dict):
    ips, hashes = set(), set()
    for k in ("ip", "src_ip", "source_ip", "remote_ip", "dest_ip"):
        v = event.get(k)
        if not v:
            continue
        try:
            a = ipaddress.ip_address(str(v))
            if a.version == 4 and not (a.is_private or a.is_loopback
                                       or a.is_multicast or a.is_unspecified):
                ips.add(str(v))
        except ValueError:
            pass
    for k in ("sha256", "md5", "sha1", "hash"):
        v = event.get(k)
        if v and _HASH_RE.fullmatch(str(v)):
            hashes.add(str(v))
    blob = " ".join(str(event.get(k, "")) for k in
                    ("file_path", "rules", "sample", "indicators"))
    for m in _HASH_RE.finditer(blob):
        hashes.add(m.group(1))
    return ips, hashes


async def _vt_throttle():
    global _last_vt
    async with _vt_lock:
        dt = time.monotonic() - _last_vt
        if dt < _VT_MIN_INTERVAL:
            await asyncio.sleep(_VT_MIN_INTERVAL - dt)
        _last_vt = time.monotonic()


async def _vt_query(session, kind: str, ioc: str):
    path = "ip_addresses" if kind == "ip" else "files"
    url = f"https://www.virustotal.com/api/v3/{path}/{ioc}"
    await _vt_throttle()
    try:
        async with session.get(url, headers={"x-apikey": _VT_KEY}) as r:
            if r.status != 200:
                return None
            data = await r.json()
        stats = (data.get("data", {}).get("attributes", {})
                 .get("last_analysis_stats", {}) or {})
        return {"malicious": int(stats.get("malicious", 0)),
                "suspicious": int(stats.get("suspicious", 0)),
                "harmless": int(stats.get("harmless", 0)),
                "undetected": int(stats.get("undetected", 0))}
    except Exception as e:
        logger.debug("soar: VT query failed for %s: %s", ioc, e)
        return None


async def _otx_query(session, kind: str, ioc: str):
    seg = "IPv4" if kind == "ip" else "file"
    url = f"https://otx.alienvault.com/api/v1/indicators/{seg}/{ioc}/general"
    try:
        async with session.get(url, headers={"X-OTX-API-KEY": _OTX_KEY}) as r:
            if r.status != 200:
                return None
            data = await r.json()
        pi = data.get("pulse_info", {}) or {}
        tags = set()
        for p in (pi.get("pulses") or [])[:10]:
            for t in (p.get("tags") or []):
                tags.add(t)
        return {"pulse_count": int(pi.get("count", 0)), "tags": sorted(tags)[:25]}
    except Exception as e:
        logger.debug("soar: OTX query failed for %s: %s", ioc, e)
        return None


async def _enrich_one(session, kind, ioc, vt_out, otx_out):
    if _VT_KEY:
        ck = ("vt", ioc)
        cached = _cache_get(ck)
        if cached is None:
            cached = await _vt_query(session, kind, ioc)
            if cached is not None:
                _cache_put(ck, cached)
        if cached:
            vt_out[ioc] = cached
    if _OTX_KEY:
        ck = ("otx", ioc)
        cached = _cache_get(ck)
        if cached is None:
            cached = await _otx_query(session, kind, ioc)
            if cached is not None:
                _cache_put(ck, cached)
        if cached:
            otx_out[ioc] = cached


async def enrich(event: dict) -> dict:
    """Mutate the event in place with vt_score / otx_tags. No-op if dormant."""
    if not (_AIOHTTP_OK and (_VT_KEY or _OTX_KEY)):
        return event
    ips, hashes = _extract_iocs(event)
    if not ips and not hashes:
        return event
    vt_out, otx_out = {}, {}
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [_enrich_one(session, "ip", ip, vt_out, otx_out)
                     for ip in list(ips)[:_MAX_IOCS]]
            tasks += [_enrich_one(session, "file", h, vt_out, otx_out)
                      for h in list(hashes)[:_MAX_IOCS]]
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.debug("soar: enrichment session error: %s", e)
    if vt_out:
        event["vt_score"] = vt_out
    if otx_out:
        event["otx_tags"] = otx_out
    event["cti_enriched"] = bool(vt_out or otx_out)
    if vt_out or otx_out:
        logger.info("soar: enriched %d IoC(s) (vt=%s otx=%s)",
                    len(ips) + len(hashes), bool(vt_out), bool(otx_out))
    return event


async def start(correlator=None) -> None:
    """main.py startup hook. Watchdog Pattern: dormant if aiohttp missing or no
    API keys are set. Enrichment is invoked by the correlator via enrich()."""
    if not _AIOHTTP_OK:
        logger.warning("SOAR_ENRICHMENT: aiohttp unavailable — dormant")
        await asyncio.Event().wait(); return
    if not (_VT_KEY or _OTX_KEY):
        logger.warning("SOAR_ENRICHMENT: no VT/OTX API keys in env — dormant")
        await asyncio.Event().wait(); return
    logger.info("SOAR_ENRICHMENT: armed — VT=%s OTX=%s", bool(_VT_KEY), bool(_OTX_KEY))
    await asyncio.Event().wait()
