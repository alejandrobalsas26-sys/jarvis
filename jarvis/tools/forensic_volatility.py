"""
tools/forensic_volatility.py — Automated Live Memory Forensics Orchestrator (v19.0).

Trigger sequence on canary detection (correct ordering):
  1. vmrun snapshot  — freeze VM state at exact detection moment (before cleanup)
  2. vmrun suspend   — flush RAM to .vmem dump on host SSD
  3. Volatility 3    — targeted plugins only: windows.malfind, windows.pslist

All vmrun calls use asyncio.create_subprocess_exec with shell=False.
Volatility analysis runs inside _vol_pool (ProcessPoolExecutor, max_workers=1).
broadcast_fn is NOT passed to the worker process (not picklable); the async
wrapper handles broadcasting after loop.run_in_executor() returns.

Do NOT share _vol_pool with _mesh_pool or _graph_pool.
"""

import asyncio
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

VMRUN_PATH = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"

_vol_pool = ProcessPoolExecutor(max_workers=1)


def _derive_vmem_path(vmx_path: str) -> str:
    """Infer the .vmem dump path from the .vmx file path (same dir, same stem)."""
    vmx = Path(vmx_path)
    return str(vmx.parent / (vmx.stem + ".vmem"))


def _run_volatility(vmem_path: str) -> list[dict]:
    """
    Runs in worker process (not in the asyncio event loop).

    Executes Volatility 3 windows.malfind and windows.pslist.
    Parses stdout line by line — returns list of anomaly dicts.
    broadcast_fn is intentionally absent: only data crosses the process boundary.
    """
    anomalies: list[dict] = []

    # Locate vol.py / volatility3 binary
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
                    offset = parts[2].strip() if len(parts) > 2 else "0x0"
                    line_low = line.lower()
                    if "hollow" in line_low:
                        technique = "ProcessHollowing"
                    elif "inject" in line_low:
                        technique = "CodeInjection"
                    else:
                        technique = default_technique
                else:
                    # pslist: PID PPID ImageFileName Offset(V) …
                    offset = parts[3].strip() if len(parts) > 3 else "0x0"
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
    """
    Full forensic capture pipeline.

    Step 1 — snapshot first: freezes VM state at the exact detection moment so
             the malicious process cannot perform anti-forensic cleanup before
             we capture it.
    Step 2 — suspend: flushes RAM contents to a .vmem dump on the host SSD.
    Step 3 — Volatility analysis in _vol_pool, then stream results.
    """
    await broadcast_fn({
        "type":      "forensic_capture_start",
        "vmx":       vmx_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # ── Step 1: snapshot ─────────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            VMRUN_PATH, "-T", "ws", "snapshot", vmx_path, "FORENSIC_CAPTURE",
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
        logger.warning(f"VOL: vmrun.exe not found at {VMRUN_PATH!r}")
        await broadcast_fn({
            "type":  "error",
            "error": "vmrun.exe not found — forensic capture aborted",
        })
        return
    except Exception as exc:
        logger.warning(f"VOL: snapshot error — {exc}")

    # ── Step 2: suspend (creates .vmem) ──────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            VMRUN_PATH, "-T", "ws", "suspend", vmx_path,
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

    # ── Step 3: Volatility analysis ───────────────────────────────────────────
    vmem_path = _derive_vmem_path(vmx_path)
    if not Path(vmem_path).exists():
        logger.warning(f"VOL: .vmem not found at {vmem_path!r}")
        await broadcast_fn({
            "type":  "error",
            "error": f"Memory dump not found: {vmem_path}",
        })
        return

    loop = asyncio.get_running_loop()
    try:
        anomalies = await loop.run_in_executor(_vol_pool, _run_volatility, vmem_path)
        await stream_volatility_results(anomalies, broadcast_fn)
    except Exception as exc:
        logger.error(f"VOL: executor error — {exc}")
        await broadcast_fn({"type": "error", "error": f"Volatility analysis failed: {exc}"})


async def stream_volatility_results(anomalies: list[dict], broadcast_fn) -> None:
    """Broadcast each anomaly dict to the AURA pipeline."""
    for anomaly in anomalies:
        await broadcast_fn({
            "type":      "kernel_anomaly_detected",
            "pid":       anomaly["pid"],
            "offset":    anomaly["offset"],
            "technique": anomaly["technique"],
        })
