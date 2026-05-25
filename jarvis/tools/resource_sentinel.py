"""tools/resource_sentinel.py — Autonomous hardware resource watchdog with VM auto-suspend."""

import asyncio
import time

import psutil
from loguru import logger

from core.config import settings
from core.events import make_event

_last_suspend: float = 0.0

# v32.0 — rolling history for AURA sparklines (60 points = 5 min @ 5s interval)
_cpu_history: list[float] = []
_ram_history: list[float] = []


def _read_cpu_temp() -> float | None:
    # Tier 1 — psutil (Linux)
    try:
        temps = psutil.sensors_temperatures()
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass
    # Tier 2 — LibreHardwareMonitor WMI (Windows, requires LHM running)
    try:
        import wmi
        w = wmi.WMI(namespace="root\\LibreHardwareMonitor")
        for sensor in w.Sensor():
            if sensor.SensorType == "Temperature" and "CPU" in sensor.Name:
                return float(sensor.Value)
    except Exception:
        pass
    return None


async def _broadcast_metrics(broadcast_fn) -> None:
    """v32.0 — Broadcast enhanced metrics every 5s for AURA sparklines."""
    while True:
        try:
            cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
            ram = psutil.virtual_memory()
            try:
                disk = psutil.disk_usage("/")
                disk_free_gb = round(disk.free / (1024**3), 1)
            except Exception:
                disk_free_gb = None

            _cpu_history.append(cpu)
            _ram_history.append(ram.percent)
            if len(_cpu_history) > 60:
                _cpu_history.pop(0)
            if len(_ram_history) > 60:
                _ram_history.pop(0)

            await broadcast_fn({
                "type":         "metrics_update",
                "cpu_percent":  round(cpu, 1),
                "cpu_history":  list(_cpu_history),
                "ram_used_gb":  round(ram.used / (1024**3), 1),
                "ram_total_gb": round(ram.total / (1024**3), 1),
                "ram_percent":  round(ram.percent, 1),
                "ram_history":  list(_ram_history),
                "disk_free_gb": disk_free_gb,
                "timestamp":    time.time(),
            })
        except Exception as e:
            logger.debug(f"resource_sentinel metrics broadcast failed: {e}")
        await asyncio.sleep(5)


async def _critical_loop(broadcast_fn) -> None:
    """Original thermal/RAM critical watch loop."""
    global _last_suspend
    temp_warned = False
    while True:
        vm           = psutil.virtual_memory()
        cpu_temp     = _read_cpu_temp()
        ram_free_pct = 100.0 - vm.percent

        if cpu_temp is None and not temp_warned:
            await broadcast_fn(make_event(
                "error",
                error="Thermal monitoring unavailable (no LibreHardwareMonitor). RAM-only mode.",
            ))
            logger.warning("resource_sentinel: thermal monitoring unavailable — RAM-only mode")
            temp_warned = True

        critical = ram_free_pct < settings.ram_free_floor or (
            cpu_temp is not None and cpu_temp > settings.cpu_temp_ceil
        )

        if critical and (time.monotonic() - _last_suspend) > settings.suspend_cooldown:
            _last_suspend = time.monotonic()
            await broadcast_fn(make_event(
                "resource_critical_alert",
                cpu_temp=cpu_temp,
                ram_free_pct=round(ram_free_pct, 1),
            ))
            for vmx in settings.get_secondary_vms():
                proc = await asyncio.create_subprocess_exec(
                    settings.vmrun_path, "-T", "ws", "suspend", vmx,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

        await asyncio.sleep(5)


async def start_resource_sentinel(broadcast_fn) -> None:
    """v32.0 — concurrent metrics broadcast + critical watch."""
    await asyncio.gather(
        _broadcast_metrics(broadcast_fn),
        _critical_loop(broadcast_fn),
    )
