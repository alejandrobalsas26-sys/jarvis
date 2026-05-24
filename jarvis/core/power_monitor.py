"""
core/power_monitor.py — Real-time battery/AC power state monitor (v30.0).

Polls psutil.sensors_battery() every 30s.
When power state changes (AC → battery or battery → AC):
  - Updates hw_profile in place
  - Reconfigures model router
  - Broadcasts power_state_change event to AURA HUD
  - Logs the configuration change
"""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import psutil

_POLL_INTERVAL = 30  # seconds
_last_plugged: bool | None = None


async def start_power_monitor(broadcast_fn, hw_profile) -> None:
    """
    Background task: detect AC/battery transitions and reconfigure.
    Silent on desktops (sensors_battery() returns None).
    """
    global _last_plugged
    try:
        bat = psutil.sensors_battery()
    except Exception:
        return
    if bat is None:
        return  # desktop — no battery, nothing to monitor

    _last_plugged = bat.power_plugged

    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            bat = psutil.sensors_battery()
            if bat is None:
                continue

            now_plugged = bat.power_plugged
            if now_plugged == _last_plugged:
                continue  # no state change

            _last_plugged = now_plugged
            _apply_power_config(hw_profile, now_plugged, bat.percent)

            try:
                await broadcast_fn({
                    "type":        "power_state_change",
                    "on_ac":       now_plugged,
                    "battery_pct": round(bat.percent, 1),
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                    "new_ctx":     hw_profile.recommended_ctx,
                    "new_pools":   hw_profile.recommended_pools,
                    "new_model":   hw_profile.model_fast,
                })
            except Exception as e:
                logger.debug(f"POWER_MONITOR: broadcast failed: {e}")

        except Exception as e:
            logger.debug(f"POWER_MONITOR: {e}")


def _apply_power_config(hw_profile, on_ac: bool, pct: float) -> None:
    """Mutate hw_profile in place when power state changes."""
    from core.hardware_profile import TDP_CONFIGS, BATTERY_OVERRIDE

    if on_ac:
        # Restore TDP-tier config
        cfg = TDP_CONFIGS.get(hw_profile.cpu_tdp_tier,
                              TDP_CONFIGS["U_SERIES_15W"])
        hw_profile.on_battery        = False
        hw_profile.recommended_pools = cfg["pools"]
        hw_profile.recommended_ctx   = cfg["ctx"]
        hw_profile.model_fast        = cfg["model_fast"]
        logger.info(
            f"POWER: AC restored → pools={hw_profile.recommended_pools} "
            f"ctx={hw_profile.recommended_ctx} "
            f"fast={hw_profile.model_fast}"
        )
    else:
        # Battery mode
        hw_profile.on_battery        = True
        hw_profile.battery_percent   = pct
        hw_profile.recommended_pools = BATTERY_OVERRIDE["pools"]
        hw_profile.recommended_ctx   = BATTERY_OVERRIDE["ctx"]
        hw_profile.model_fast        = BATTERY_OVERRIDE["model_fast"]
        logger.warning(
            f"POWER: on battery ({pct:.0f}%) → power-save mode — "
            f"pools={hw_profile.recommended_pools} "
            f"ctx={hw_profile.recommended_ctx}"
        )

    # Propagate to model router
    try:
        import core.model_router as mr
        if hasattr(mr, "MODEL_FAST"):
            mr.MODEL_FAST = hw_profile.model_fast
    except Exception:
        pass

    # Refresh module-level pools constant for downstream importers
    try:
        import core.hardware_profile as hp
        hp.recommended_pools = hw_profile.recommended_pools
    except Exception:
        pass
