"""
tools/geo_resolver.py — Async IP geolocation for AURA globe (v32.0).

Uses ip-api.com free API (no key, 45 req/min).
Caches results — same IP is never queried twice per session.
Rate-limited to 1 request per 2.5s to stay within free tier.
Private/RFC1918 IPs are skipped silently.
"""

import asyncio
import ipaddress
import time

from loguru import logger

_cache:     dict[str, dict] = {}
_last_call: float = 0.0
_RATE_LIMIT   = 2.5   # seconds between calls
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


async def resolve_ip(ip: str, broadcast_fn) -> dict | None:
    """
    Resolve an IP to geo coordinates and broadcast geo_resolved event.
    Returns cached result if available. Skips private IPs.
    """
    global _last_call

    if not ip or _is_private(ip):
        return None

    if ip in _cache:
        return _cache[ip]

    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_call)
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        import aiohttp
        _last_call = time.monotonic()
        url = f"http://ip-api.com/json/{ip}?fields=status,country,city,lat,lon,isp"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return None
                data = await r.json()

        if data.get("status") != "success":
            return None

        result = {
            "ip":      ip,
            "country": data.get("country", "Unknown"),
            "city":    data.get("city", ""),
            "lat":     float(data.get("lat", 0)),
            "lon":     float(data.get("lon", 0)),
            "isp":     data.get("isp", ""),
        }
        _cache[ip] = result

        await broadcast_fn({
            "type":    "geo_resolved",
            "ip":      ip,
            "country": result["country"],
            "city":    result["city"],
            "lat":     result["lat"],
            "lon":     result["lon"],
        })
        return result

    except Exception as e:
        logger.debug(f"GEO: resolve {ip} failed: {e}")
        return None


async def watch_and_resolve(broadcast_fn) -> None:
    """
    No-op background coroutine. Geo resolution is called inline
    from canary/tarpit handlers when they have an IP.
    This function exists for watchdog registration compatibility.
    """
    while True:
        await asyncio.sleep(3600)
