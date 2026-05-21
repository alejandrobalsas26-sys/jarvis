"""tools/resource_sentinel.py — Autonomous hardware resource watchdog with VM auto-suspend."""

import asyncio
import time

import psutil
from loguru import logger

VMRUN_PATH       = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
SECONDARY_VMS: list[str] = []   # operator fills: [r"C:\path\to\secondary.vmx", ...]
RAM_FREE_FLOOR   = 8.0    # % free RAM threshold
CPU_TEMP_CEIL    = 85.0   # °C threshold
SUSPEND_COOLDOWN = 120    # seconds — hysteresis to prevent flapping

_last_suspend: float = 0.0


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
    # Tier 3 — unavailable
    return None


async def start_resource_sentinel(broadcast_fn) -> None:
    global _last_suspend
    temp_warned = False
    while True:
        vm           = psutil.virtual_memory()
        cpu_temp     = _read_cpu_temp()
        ram_free_pct = 100.0 - vm.percent

        if cpu_temp is None and not temp_warned:
            await broadcast_fn({
                "type":  "error",
                "error": "Thermal monitoring unavailable (no LibreHardwareMonitor). RAM-only mode.",
            })
            logger.warning("resource_sentinel: thermal monitoring unavailable — RAM-only mode")
            temp_warned = True

        critical = ram_free_pct < RAM_FREE_FLOOR or (
            cpu_temp is not None and cpu_temp > CPU_TEMP_CEIL
        )

        if critical and (time.monotonic() - _last_suspend) > SUSPEND_COOLDOWN:
            _last_suspend = time.monotonic()
            await broadcast_fn({
                "type":         "resource_critical_alert",
                "cpu_temp":     cpu_temp,
                "ram_free_pct": round(ram_free_pct, 1),
            })
            for vmx in SECONDARY_VMS:
                proc = await asyncio.create_subprocess_exec(
                    VMRUN_PATH, "-T", "ws", "suspend", vmx,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

        await asyncio.sleep(5)
