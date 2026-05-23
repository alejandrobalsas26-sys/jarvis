"""
core/hardware_profile.py — Hardware Auto-Profile for RAM/CPU-aware configuration.

Detects total RAM at boot and returns optimized config.
>48GB signals dual-channel 64GB upgrade — zero code changes required.
"""

import psutil
from dataclasses import dataclass
from loguru import logger


@dataclass
class HardwareProfile:
    total_ram_gb:      float
    is_dual_channel:   bool
    cpu_cores:         int
    recommended_pools: int
    recommended_ctx:   int
    model_fast:        str
    model_deep:        str


def detect_hardware() -> HardwareProfile:
    """
    Auto-detect RAM and return optimized config.
    >48GB = dual-channel upgrade detected.
    Zero code changes needed when RAM is upgraded.
    """
    total_gb = psutil.virtual_memory().total / (1024 ** 3)
    cores    = psutil.cpu_count(logical=False) or 4
    dual     = total_gb >= 48

    is_u_series = False
    cpu_name = ""
    try:
        import subprocess
        # Primary: wmic (legacy Windows ≤ 11 22H2).
        try:
            result = subprocess.check_output(
                ["wmic", "cpu", "get", "name"],
                text=True,
                timeout=5,
            )
            cpu_lines = [
                l.strip() for l in result.splitlines()
                if l.strip() and l.strip() != "Name"
            ]
            cpu_name = cpu_lines[0] if cpu_lines else ""
        except FileNotFoundError:
            # wmic was removed from Windows 11 24H2+. Fall back to CIM via
            # PowerShell — shell=False, fixed argv, no user input.
            result = subprocess.check_output(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance Win32_Processor).Name",
                ],
                text=True,
                timeout=5,
            )
            cpu_name = result.strip().splitlines()[0].strip() if result.strip() else ""

        # AMD/Intel mobile U-series tokens look like "Ryzen 5 7430U" or
        # "i7-1265U" — match the model code itself, not just trailing "U".
        tokens = cpu_name.replace("-", " ").split()
        is_u_series = any(t.endswith("U") and any(c.isdigit() for c in t) for t in tokens)

        if is_u_series:
            logger.info(
                f"HARDWARE: U-Series Low-Power CPU detected ({cpu_name}) "
                f"— applying thermal throttling overrides"
            )
        elif cpu_name:
            logger.info(f"HARDWARE: CPU detected: {cpu_name}")
    except Exception:
        pass

    if dual:
        p = HardwareProfile(
            total_ram_gb      = total_gb,
            is_dual_channel   = True,
            cpu_cores         = cores,
            recommended_pools = 1 if is_u_series else 2,
            recommended_ctx   = 4096 if is_u_series else 8192,
            model_fast = (
                "qwen2.5:7b-instruct-q5_K_M"
                if is_u_series else
                "qwen2.5:7b-instruct-q8_0"
            ),
            model_deep = "qwen2.5:14b-instruct-q4_K_M",
        )
        logger.info(
            f"HARDWARE: {total_gb:.0f}GB DUAL-CHANNEL MODE — "
            f"pools={p.recommended_pools} ctx={p.recommended_ctx} "
            f"fast={p.model_fast}"
            + (" [U-SERIES OVERRIDE]" if is_u_series else "")
        )
    else:
        p = HardwareProfile(
            total_ram_gb    = total_gb,
            is_dual_channel = False,
            cpu_cores       = cores,
            recommended_pools = 1,
            recommended_ctx   = 4096,
            model_fast = "qwen2.5:7b-instruct-q4_K_M",
            model_deep = "qwen2.5:14b-instruct-q4_K_M",
        )
        logger.info(
            f"HARDWARE: {total_gb:.0f}GB SINGLE-CHANNEL MODE — "
            f"pools={p.recommended_pools} ctx={p.recommended_ctx}"
        )
    return p


# Module-level constant — computed once at import time.
# Tool files import this to size their ProcessPoolExecutors without
# needing a reference to the HardwareProfile instance from main.py.
recommended_pools: int = detect_hardware().recommended_pools
