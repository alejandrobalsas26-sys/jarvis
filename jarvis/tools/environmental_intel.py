"""tools/environmental_intel.py — Async environmental telemetry + forensic NTP chrono-sync."""

import asyncio
import time
import ntplib
import aiohttp
from datetime import datetime, timezone

DEFAULT_LAT = 9.3592    # Colón, Panama — operator-overridable
DEFAULT_LON = -79.9014
POLL_INTERVAL = 900     # 15 min — hard floor (rate-limit OPSEC)
NTP_SERVERS = ["pool.ntp.org", "time.google.com"]

_cache: dict = {"data": None, "fetched_at": 0.0}
_clock_offset: float = 0.0   # NTP drift correction for forensic timestamps


async def _sync_ntp() -> None:
    """Sync forensic clock offset against NTP. Runs in executor (ntplib is blocking)."""
    loop = asyncio.get_running_loop()
    for server in NTP_SERVERS:
        try:
            resp = await loop.run_in_executor(
                None, lambda s=server: ntplib.NTPClient().request(s, timeout=5)
            )
            global _clock_offset
            _clock_offset = resp.offset
            return
        except Exception:
            continue


def forensic_now() -> str:
    """ISO timestamp corrected by NTP offset — use for all audit/manifest stamps."""
    return datetime.fromtimestamp(time.time() + _clock_offset, timezone.utc).isoformat()


async def start_environmental_polling(broadcast_fn,
                                      lat: float = DEFAULT_LAT,
                                      lon: float = DEFAULT_LON) -> None:
    await _sync_ntp()
    ntp_counter = 0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                url = (
                    f"https://api.open-meteo.com/v1/forecast"
                    f"?latitude={lat}&longitude={lon}"
                    f"&current=temperature_2m,relative_humidity_2m,precipitation_probability"
                )
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    j = await r.json()
                cur = j.get("current", {})
                _cache["data"] = cur
                _cache["fetched_at"] = time.time()
                await broadcast_fn({
                    "type":             "environmental_telemetry",
                    "temperature":      cur.get("temperature_2m"),
                    "humidity":         cur.get("relative_humidity_2m"),
                    "rain_probability": cur.get("precipitation_probability", 0),
                    "timestamp":        forensic_now(),
                })
            except Exception as e:
                await broadcast_fn({"type": "error", "error": f"Weather fetch failed: {e}"})

            ntp_counter += 1
            if ntp_counter * POLL_INTERVAL >= 21600:   # re-sync NTP every 6h
                await _sync_ntp()
                ntp_counter = 0
            await asyncio.sleep(POLL_INTERVAL)
