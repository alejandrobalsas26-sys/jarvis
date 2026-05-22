"""
tools/forensic_volatility.py — Automated Live Memory Forensics Orchestrator (v24.0).

Trigger sequence on canary detection:
  1. vmrun snapshot  — freeze VM state at exact detection moment
  2. vmrun suspend   — flush RAM to .vmem dump on host SSD
  3. Volatility 3    — targeted plugins: windows.malfind, windows.pslist

All vmrun calls use asyncio.create_subprocess_exec (shell=False).
Volatility analysis runs inside _vol_pool (ProcessPoolExecutor, max_workers=1).
"""

import asyncio
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from core.config import settings
from core.events import make_event
from core.hardware_profile import recommended_pools as _hw_pools

_vol_pool = ProcessPoolExecutor(max_workers=_hw_pools)


def _derive_vmem_path(vmx_path: str) -> str:
    vmx = Path(vmx_path)
    return str(vmx.parent / (vmx.stem + ".vmem"))


def _run_volatility(vmem_path: str) -> list[dict]:
    """Runs in worker process. Executes Volatility 3 plugins and returns anomaly dicts."""
    anomalies: list[dict] = []

    vol_bin = (
        shutil.which("vol")
        or shutil.which("vol.py")
        or shutil.which("volatility3")
    )
    base_cmd: list[str] = (
        [vol_bin, "-f", vmem_path]
        if vol_bin
        else ["python", "-m", "volatility3", "-f", vmem_path]
    )

    plugins = [
        ("windows.malfind.Malfind", "MemoryInjection"),
        ("windows.pslist.PsList",   "ProcessList"),
    ]

    for plugin, default_technique in plugins:
        try:
            result = subprocess.run(
                base_cmd + [plugin],
                shell=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("PID"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                pid_str = parts[0].strip()
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)

                if "malfind" in plugin.lower():
                    offset    = parts[2].strip() if len(parts) > 2 else "0x0"
                    line_low  = line.lower()
                    if "hollow" in line_low:
                        technique = "ProcessHollowing"
                    elif "inject" in line_low:
                        technique = "CodeInjection"
                    else:
                        technique = default_technique
                else:
                    offset    = parts[3].strip() if len(parts) > 3 else "0x0"
                    technique = default_technique

                anomalies.append({"pid": pid, "offset": offset, "technique": technique})

        except FileNotFoundError:
            logger.warning(f"VOL: Volatility not found — skipping {plugin}")
        except subprocess.TimeoutExpired:
            logger.warning(f"VOL: timeout running {plugin} on {vmem_path}")
        except Exception as exc:
            logger.warning(f"VOL: error running {plugin} — {exc}")

    return anomalies


async def trigger_forensic_capture(vmx_path: str, broadcast_fn) -> None:
    """Full forensic capture pipeline: snapshot → suspend → Volatility."""
    await broadcast_fn(make_event("forensic_capture_start", vmx=vmx_path))

    # ── Step 1: snapshot ─────────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.vmrun_path, "-T", "ws", "snapshot", vmx_path, "FORENSIC_CAPTURE",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                f"VOL: vmrun snapshot rc={proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:200]}"
            )
    except FileNotFoundError:
        logger.warning(f"VOL: vmrun.exe not found at {settings.vmrun_path!r}")
        await broadcast_fn(make_event(
            "error", error="vmrun.exe not found — forensic capture aborted"
        ))
        return
    except Exception as exc:
        logger.warning(f"VOL: snapshot error — {exc}")

    # ── Step 2: suspend ───────────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.vmrun_path, "-T", "ws", "suspend", vmx_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                f"VOL: vmrun suspend rc={proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:200]}"
            )
    except Exception as exc:
        logger.warning(f"VOL: suspend error — {exc}")

    # ── Step 3: Volatility ────────────────────────────────────────────────────
    vmem_path = _derive_vmem_path(vmx_path)
    if not Path(vmem_path).exists():
        logger.warning(f"VOL: .vmem not found at {vmem_path!r}")
        await broadcast_fn(make_event("error", error=f"Memory dump not found: {vmem_path}"))
        return

    loop = asyncio.get_running_loop()
    try:
        anomalies = await loop.run_in_executor(_vol_pool, _run_volatility, vmem_path)
        await stream_volatility_results(anomalies, broadcast_fn)
    except Exception as exc:
        logger.error(f"VOL: executor error — {exc}")
        await broadcast_fn(make_event("error", error=f"Volatility analysis failed: {exc}"))


async def stream_volatility_results(anomalies: list[dict], broadcast_fn) -> None:
    """Broadcast each anomaly dict to the AURA pipeline."""
    for anomaly in anomalies:
        await broadcast_fn(make_event(
            "kernel_anomaly_detected",
            pid=anomaly["pid"],
            offset=anomaly["offset"],
            technique=anomaly["technique"],
        ))
