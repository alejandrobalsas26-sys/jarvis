"""
core/iot_bridge.py — Smart home IoT webhook bridge (v46.0).

Connects JARVIS to smart lights and home automation.
On canary hit → lights flash red.
On critical incident → sustained red alert.
On all clear → restore normal state.

Supports:
  - Home Assistant (webhook or REST API)
  - Philips Hue (native bridge API)
  - Generic HTTP webhook (any platform)
  - Tapo (via generic webhook)

Configure in jarvis_config.yaml under 'iot' section.
"""

import asyncio, os
from loguru import logger

# ── Config from env vars or jarvis_config.yaml ───────────────────────────────
_HA_URL       = os.getenv("JARVIS_HA_URL", "")
_HA_TOKEN     = os.getenv("JARVIS_HA_TOKEN", "")
_HA_ENTITY    = os.getenv("JARVIS_HA_ENTITY", "light.all")

_HUE_BRIDGE   = os.getenv("JARVIS_HUE_BRIDGE", "")
_HUE_USER     = os.getenv("JARVIS_HUE_USER", "")
_HUE_LIGHT    = os.getenv("JARVIS_HUE_LIGHT", "1")

_WEBHOOK_URL  = os.getenv("JARVIS_IOT_WEBHOOK", "")

_ENABLED = bool(_HA_URL or _HUE_BRIDGE or _WEBHOOK_URL)


async def _ha_set_light(
    rgb: tuple[int,int,int],
    brightness: int = 255,
    flash: bool = False,
) -> None:
    """Control light via Home Assistant REST API."""
    if not _HA_URL or not _HA_TOKEN:
        return
    try:
        import aiohttp
        payload = {
            "entity_id": _HA_ENTITY,
            "rgb_color":  list(rgb),
            "brightness": brightness,
        }
        if flash:
            payload["flash"] = "short"

        async with aiohttp.ClientSession() as s:
            await s.post(
                f"{_HA_URL}/api/services/light/turn_on",
                headers={"Authorization": f"Bearer {_HA_TOKEN}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as e:
        logger.debug(f"IOT_BRIDGE: HA error: {e}")


async def _hue_set_light(
    hue: int,
    sat: int,
    bri: int,
    alert: str = "none",
) -> None:
    """Control Philips Hue light directly."""
    if not _HUE_BRIDGE or not _HUE_USER:
        return
    try:
        import aiohttp
        url     = (f"http://{_HUE_BRIDGE}/api/{_HUE_USER}"
                   f"/lights/{_HUE_LIGHT}/state")
        payload = {"hue": hue, "sat": sat, "bri": bri,
                   "on": True, "alert": alert}
        async with aiohttp.ClientSession() as s:
            await s.put(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            )
    except Exception as e:
        logger.debug(f"IOT_BRIDGE: Hue error: {e}")


async def _webhook(event: str, color: str,
                   severity: str = "") -> None:
    """Generic HTTP webhook for any smart home platform."""
    if not _WEBHOOK_URL:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            await s.post(
                _WEBHOOK_URL,
                json={"event": event, "color": color,
                      "severity": severity, "source": "JARVIS"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as e:
        logger.debug(f"IOT_BRIDGE: webhook error: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

async def alert_red(flash: bool = True, reason: str = "") -> None:
    """Flash red — canary hit or high incident."""
    if not _ENABLED: return
    logger.debug(f"IOT_BRIDGE: red alert — {reason}")
    await asyncio.gather(
        _ha_set_light((255, 0, 0), brightness=255, flash=flash),
        _hue_set_light(hue=0, sat=254, bri=254,
                       alert="lselect" if flash else "select"),
        _webhook("alert", "red", reason),
        return_exceptions=True,
    )


async def alert_orange(reason: str = "") -> None:
    """Orange — medium severity incident."""
    if not _ENABLED: return
    await asyncio.gather(
        _ha_set_light((255, 80, 0), brightness=200),
        _hue_set_light(hue=5000, sat=254, bri=200),
        _webhook("warning", "orange", reason),
        return_exceptions=True,
    )


async def alert_clear() -> None:
    """Restore normal lighting — all clear."""
    if not _ENABLED: return
    logger.debug("IOT_BRIDGE: all clear — restoring lights")
    await asyncio.gather(
        _ha_set_light((255, 255, 255), brightness=180),
        _hue_set_light(hue=8418, sat=140, bri=180, alert="none"),
        _webhook("clear", "white"),
        return_exceptions=True,
    )


async def startup_pulse() -> None:
    """JARVIS boot pulse — brief green flash."""
    if not _ENABLED: return
    await _ha_set_light((0, 255, 65), brightness=255, flash=True)
    await _hue_set_light(hue=25500, sat=254, bri=254, alert="select")
    await asyncio.sleep(2)
    await alert_clear()


def is_configured() -> bool:
    return _ENABLED
