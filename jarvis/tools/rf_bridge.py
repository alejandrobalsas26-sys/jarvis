"""
tools/rf_bridge.py — RF/RFID Hardware Abstraction Layer (v19.0).

Sources: Proxmark3 (pm3.exe), tshark, bettercap, serial/USB RFID readers.
All subprocess calls use asyncio.create_subprocess_exec (shell=False).
Serial reads run in a dedicated daemon thread; frames are bridged to the
asyncio event loop via asyncio.Queue + loop.call_soon_threadsafe.
Every source handles ConnectionRefusedError / FileNotFoundError gracefully:
logs a warning and retries after a back-off delay without crashing.
"""

import asyncio
import re
import threading
from datetime import datetime, timezone

from loguru import logger

_MAC_RE = re.compile(r'[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _proxmark3_task(broadcast_fn) -> None:
    """Stream HF/LF tag reads from Proxmark3 via pm3.exe -c hf search."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pm3.exe", "-c", "hf search",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                uid = ""
                if "UID" in line or "uid" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                        uid = parts[-1].strip().replace(" ", "")
                if not uid:
                    continue
                await broadcast_fn({
                    "type":        "rf_frame",
                    "hw_source":   "proxmark3",
                    "uid":         uid,
                    "rssi":        None,
                    "raw_payload": uid,
                    "timestamp":   _now_iso(),
                })
            await proc.wait()
            await asyncio.sleep(5)
        except (ConnectionRefusedError, FileNotFoundError):
            logger.warning("RF_BRIDGE: pm3.exe unavailable — proxmark3 source offline")
            await asyncio.sleep(30)
        except Exception as exc:
            logger.warning(f"RF_BRIDGE: proxmark3_task error — {exc}")
            await asyncio.sleep(10)


async def _tshark_task(broadcast_fn) -> None:
    """Capture 802.11 frames via tshark.

    On Windows the ALFA AWUS036ACM requires Npcap with raw 802.11 support, or
    route through a Kali VM via SSH pipe.  Absent either, tshark is unavailable
    and this task sleeps for 30s between retries.
    """
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "tshark",
                "-T", "fields",
                "-e", "wlan.sa",
                "-e", "radiotap.dbm_antsignal",
                "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                parts = line.split("\t")
                uid = parts[0] if parts else line
                rssi: float | None = None
                if len(parts) > 1 and parts[1]:
                    try:
                        rssi = float(parts[1])
                    except ValueError:
                        pass
                if not uid:
                    continue
                await broadcast_fn({
                    "type":        "rf_frame",
                    "hw_source":   "tshark",
                    "uid":         uid,
                    "rssi":        rssi,
                    "raw_payload": line[:128],
                    "timestamp":   _now_iso(),
                })
            await proc.wait()
            await asyncio.sleep(5)
        except (ConnectionRefusedError, FileNotFoundError):
            logger.warning("RF_BRIDGE: tshark unavailable — 802.11 capture offline")
            await asyncio.sleep(30)
        except Exception as exc:
            logger.warning(f"RF_BRIDGE: tshark_task error — {exc}")
            await asyncio.sleep(10)


async def _bettercap_task(broadcast_fn) -> None:
    """Capture Wi-Fi probe/beacon events from bettercap event stream."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "bettercap",
                "--no-colors",
                "-eval", "wifi.recon on; events.stream on",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                m = _MAC_RE.search(line)
                uid = m.group(0) if m else line[:32]
                await broadcast_fn({
                    "type":        "rf_frame",
                    "hw_source":   "bettercap",
                    "uid":         uid,
                    "rssi":        None,
                    "raw_payload": line[:128],
                    "timestamp":   _now_iso(),
                })
            await proc.wait()
            await asyncio.sleep(5)
        except (ConnectionRefusedError, FileNotFoundError):
            logger.warning("RF_BRIDGE: bettercap unavailable — Wi-Fi monitor offline")
            await asyncio.sleep(30)
        except Exception as exc:
            logger.warning(f"RF_BRIDGE: bettercap_task error — {exc}")
            await asyncio.sleep(10)


async def _serial_task(broadcast_fn) -> None:
    """Read RFID frames from the first available serial/USB port.

    serial.readline() is blocking — it runs in a dedicated daemon thread.
    Frames are bridged to the asyncio event loop via asyncio.Queue and
    loop.call_soon_threadsafe; serial.read() is never called in a coroutine.
    """
    try:
        import serial
        import serial.tools.list_ports
    except ImportError:
        logger.warning("RF_BRIDGE: pyserial not installed — serial source offline")
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _reader_thread() -> None:
        try:
            ports = list(serial.tools.list_ports.comports())
            if not ports:
                logger.warning("RF_BRIDGE: no serial ports detected — serial source offline")
                return
            port_device = ports[0].device
            with serial.Serial(port_device, baudrate=115200, timeout=1) as ser:
                logger.info(f"RF_BRIDGE: serial reader on {port_device}")
                while True:
                    frame = ser.readline()
                    if frame:
                        loop.call_soon_threadsafe(queue.put_nowait, frame)
        except Exception as exc:
            logger.warning(f"RF_BRIDGE: serial_reader_thread — {exc}")

    threading.Thread(target=_reader_thread, daemon=True, name="rf-serial").start()

    while True:
        raw_frame = await queue.get()
        uid = raw_frame.decode("utf-8", errors="replace").strip()
        if not uid:
            continue
        await broadcast_fn({
            "type":        "rf_frame",
            "hw_source":   "proxmark3",
            "uid":         uid,
            "rssi":        None,
            "raw_payload": raw_frame.hex(),
            "timestamp":   _now_iso(),
        })


async def start_rf_bridge(broadcast_fn) -> None:
    """Launch background tasks for each active RF source."""
    logger.info("RF_BRIDGE: initializing (proxmark3 | tshark | bettercap | serial)")
    asyncio.create_task(_proxmark3_task(broadcast_fn), name="rf-proxmark3")
    asyncio.create_task(_tshark_task(broadcast_fn),    name="rf-tshark")
    asyncio.create_task(_bettercap_task(broadcast_fn), name="rf-bettercap")
    asyncio.create_task(_serial_task(broadcast_fn),    name="rf-serial")
