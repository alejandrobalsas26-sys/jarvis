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

    if dual:
        p = HardwareProfile(
            total_ram_gb    = total_gb,
            is_dual_channel = True,
            cpu_cores       = cores,
            recommended_pools = 2,
            recommended_ctx   = 8192,
            model_fast = "qwen2.5:7b-instruct-q8_0",
            model_deep = "qwen2.5:14b-instruct-q4_K_M",
        )
        logger.info(
            f"HARDWARE: {total_gb:.0f}GB DUAL-CHANNEL MODE — "
            f"pools={p.recommended_pools} ctx={p.recommended_ctx} "
            f"fast={p.model_fast}"
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
