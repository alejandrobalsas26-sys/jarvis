"""
tools/rf_bridge.py — RF/RFID Hardware Hotplug State Machine (v23.0).

Silent by default. Produces ZERO output until a physical device connects.
State transitions are the only thing that produces log output.
shell=False on all subprocess calls — no exceptions.
"""

import asyncio
import shutil
from contextlib import suppress
from datetime import datetime, timezone
from enum import Enum

import psutil
import serial.tools.list_ports
from loguru import logger


class DeviceState(Enum):
    ABSENT     = "absent"
    CONNECTED  = "connected"
    CAPTURING  = "capturing"


_device_state: dict[str, DeviceState] = {
    "proxmark3": DeviceState.ABSENT,
    "alfa":      DeviceState.ABSENT,
}
_capture_tasks: dict[str, asyncio.Task] = {}
_known_interfaces: set[str] = set()
_known_com_ports:  set[str] = set()
_warned_missing:   set[str] = set()

_WIFI_TOKENS = ("wi-fi", "wireless", "wlan", "alfa", "802.11")
_PM3_VID_PID = (0x9AC4, 0x4B8F)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _resolve_tshark_interface(hint_name: str) -> str | None:
    """Run tshark -D and return the interface index/name matching hint_name."""
    tshark = shutil.which("tshark")
    if tshark is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            tshark, "-D",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        for line in out.decode(errors="replace").splitlines():
            if any(tok in line.lower() for tok in _WIFI_TOKENS):
                idx = line.split(".", 1)[0].strip()
                return idx if idx.isdigit() else line.split(".", 1)[1].split("(")[0].strip()
    except (asyncio.TimeoutError, Exception):
        return None
    return None


async def _capture_tshark(interface: str, broadcast_fn) -> None:
    """Stream 802.11 frames from ALFA via tshark EK JSON output."""
    tshark = shutil.which("tshark")
    _device_state["alfa"] = DeviceState.CAPTURING
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            tshark, "-i", interface, "-T", "ek", "-l", "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if not text or text.startswith('{"index"'):
                continue
            await broadcast_fn({
                "type":      "rf_frame",
                "hw_source": "alfa",
                "interface": interface,
                "raw":       text[:200],
                "timestamp": _now_iso(),
            })
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.terminate()
        raise
    except Exception as e:
        logger.debug(f"RF_BRIDGE tshark capture ended: {e}")
        _device_state["alfa"] = DeviceState.ABSENT


async def _capture_proxmark(pm3_exe: str, port: str, broadcast_fn) -> None:
    """Open Proxmark3 session and stream UID/tag reads."""
    _device_state["proxmark3"] = DeviceState.CAPTURING
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            pm3_exe, "--port", port, "--flush",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            if "UID" in text or "ATQA" in text or "SAK" in text:
                await broadcast_fn({
                    "type":      "rf_frame",
                    "hw_source": "proxmark3",
                    "port":      port,
                    "raw":       text[:200],
                    "timestamp": _now_iso(),
                })
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.terminate()
        raise
    except Exception as e:
        logger.debug(f"RF_BRIDGE pm3 capture ended: {e}")
        _device_state["proxmark3"] = DeviceState.ABSENT


async def _teardown_device(device: str, broadcast_fn) -> None:
    """Cancel capture task, reset state, broadcast disconnect. Once only."""
    if _device_state[device] == DeviceState.ABSENT:
        return
    _device_state[device] = DeviceState.ABSENT
    logger.warning(f"RF_BRIDGE: {device} disconnected")
    await broadcast_fn({"type": "rf_device_disconnected", "device": device})

    task = _capture_tasks.pop(device, None)
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _poll_alfa(broadcast_fn) -> None:
    """Detect ALFA via psutil interface diff. Silent unless state changes."""
    global _known_interfaces

    while True:
        try:
            current = set(psutil.net_if_stats().keys())
            new_ifaces = current - _known_interfaces

            alfa_iface = next(
                (i for i in new_ifaces
                 if any(tok in i.lower() for tok in _WIFI_TOKENS)),
                None,
            )

            if alfa_iface and _device_state["alfa"] == DeviceState.ABSENT:
                tshark_iface = await _resolve_tshark_interface(alfa_iface)
                if tshark_iface is None:
                    if "tshark" not in _warned_missing:
                        logger.warning("RF_BRIDGE: tshark not found — install Wireshark/Npcap to capture")
                        _warned_missing.add("tshark")
                else:
                    _device_state["alfa"] = DeviceState.CONNECTED
                    logger.info(f"RF_BRIDGE: ALFA detected → {alfa_iface} (tshark: {tshark_iface})")
                    await broadcast_fn({
                        "type":      "rf_device_connected",
                        "device":    "alfa",
                        "interface": alfa_iface,
                    })
                    _capture_tasks["alfa"] = asyncio.create_task(
                        _capture_tshark(tshark_iface, broadcast_fn),
                        name="rf-alfa-capture",
                    )

            if _device_state["alfa"] in (DeviceState.CONNECTED, DeviceState.CAPTURING):
                alive = any(
                    any(tok in i.lower() for tok in _WIFI_TOKENS)
                    for i in current
                )
                if not alive:
                    await _teardown_device("alfa", broadcast_fn)

            _known_interfaces = current
        except Exception as e:
            logger.debug(f"RF_BRIDGE alfa poll error: {e}")

        await asyncio.sleep(10)


async def _poll_proxmark(broadcast_fn) -> None:
    """Detect Proxmark3 via COM port diff. Silent unless state changes."""
    global _known_com_ports
    loop = asyncio.get_running_loop()

    while True:
        try:
            ports = await loop.run_in_executor(
                None, lambda: list(serial.tools.list_ports.comports())
            )
            current = {p.device for p in ports}
            new_ports = current - _known_com_ports

            pm3_port = None
            for p in ports:
                if p.device not in new_ports:
                    continue
                is_pm3 = (
                    (p.vid, p.pid) == _PM3_VID_PID
                    or (p.description and "proxmark" in p.description.lower())
                )
                if is_pm3:
                    pm3_port = p.device
                    break

            if pm3_port and _device_state["proxmark3"] == DeviceState.ABSENT:
                pm3_exe = shutil.which("pm3") or shutil.which("proxmark3")
                if pm3_exe is None:
                    if "pm3" not in _warned_missing:
                        logger.warning("RF_BRIDGE: pm3.exe not found — install Proxmark3 client to enable")
                        _warned_missing.add("pm3")
                else:
                    _device_state["proxmark3"] = DeviceState.CONNECTED
                    logger.info(f"RF_BRIDGE: Proxmark3 detected → {pm3_port}")
                    await broadcast_fn({
                        "type":   "rf_device_connected",
                        "device": "proxmark3",
                        "port":   pm3_port,
                    })
                    _capture_tasks["proxmark3"] = asyncio.create_task(
                        _capture_proxmark(pm3_exe, pm3_port, broadcast_fn),
                        name="rf-pm3-capture",
                    )

            if _device_state["proxmark3"] in (DeviceState.CONNECTED, DeviceState.CAPTURING):
                still_present = any(
                    (p.vid, p.pid) == _PM3_VID_PID
                    or (p.description and "proxmark" in p.description.lower())
                    for p in ports
                )
                if not still_present:
                    await _teardown_device("proxmark3", broadcast_fn)

            _known_com_ports = current
        except Exception as e:
            logger.debug(f"RF_BRIDGE proxmark poll error: {e}")

        await asyncio.sleep(10)


async def start_rf_bridge(broadcast_fn) -> None:
    """Silent hotplug monitor. Produces no output until a device connects."""
    await asyncio.gather(
        _poll_alfa(broadcast_fn),
        _poll_proxmark(broadcast_fn),
        return_exceptions=True,
    )
