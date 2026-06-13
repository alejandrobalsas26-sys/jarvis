"""
core/hardware_model_profile.py — VRAM-tier → model recommendation (V60.0, Phase 4).

Complements core/hardware_profile.py (which tunes Ollama runtime flags) by
mapping best-effort hardware capability to a concrete set of recommended models
per cognitive ROLE, plus the `ollama pull` commands to obtain them.

Tiers:
  LOW      — laptop / CPU-only / no useful VRAM   → small quantized models
  MID      — 12–16 GB VRAM                         → 7B–14B comfortably
  HIGH     — 24–32 GB VRAM                         → 14B–32B
  EXTREME  — 48 GB+ VRAM                           → 32B–70B class

Detection is best-effort and NEVER raises: missing GPU info degrades to LOW.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, field
from enum import Enum

import psutil


class HardwareTier(str, Enum):
    LOW = "LOW"
    MID = "MID"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


# Per-tier recommended model set, keyed by router role name.
_TIER_MODELS: dict[HardwareTier, dict[str, str]] = {
    HardwareTier.LOW: {
        "fast":      "qwen2.5-coder:7b",
        "coder":     "qwen2.5-coder:7b",
        "deep":      "qwen2.5:7b-instruct",
        "vision":    "moondream",
        "embedding": "nomic-embed-text",
        "verifier":  "qwen2.5-coder:7b",
    },
    HardwareTier.MID: {
        "fast":      "qwen2.5-coder:7b",
        "coder":     "qwen2.5-coder:14b",
        "deep":      "deepseek-r1:14b",
        "vision":    "llava:13b",
        "embedding": "nomic-embed-text",
        "verifier":  "qwen2.5-coder:7b",
    },
    HardwareTier.HIGH: {
        "fast":      "qwen2.5-coder:7b",
        "coder":     "qwen2.5-coder:32b",
        "deep":      "deepseek-r1:32b",
        "vision":    "llama3.2-vision:11b",
        "embedding": "nomic-embed-text",
        "verifier":  "qwen2.5-coder:14b",
    },
    HardwareTier.EXTREME: {
        "fast":      "qwen2.5-coder:14b",
        "coder":     "qwen2.5-coder:32b",
        "deep":      "deepseek-r1:70b",
        "vision":    "llama3.2-vision:11b",
        "embedding": "nomic-embed-text",
        "verifier":  "qwen2.5-coder:32b",
    },
}


@dataclass
class HardwareModelProfile:
    tier: HardwareTier
    total_ram_gb: float
    cpu_cores: int
    os_name: str
    gpu_vendor: str
    gpu_vram_gb: float
    on_battery: bool
    recommended_models: dict[str, str] = field(default_factory=dict)

    def pull_commands(self) -> list[str]:
        """Deduplicated `ollama pull` commands for the recommended set."""
        seen: list[str] = []
        for model in self.recommended_models.values():
            cmd = f"ollama pull {model}"
            if cmd not in seen:
                seen.append(cmd)
        return seen


# ── Best-effort GPU probe ────────────────────────────────────────────────────

def _probe_gpu() -> tuple[str, float]:
    """Return (vendor, vram_gb). Best-effort; (\"none\", 0.0) on failure.

    Tries the existing Windows CIM probe first, then nvidia-smi, else none.
    """
    # Reuse the Windows WMI probe when available.
    try:
        from core.hardware_profile import _probe_gpu as _win_probe  # type: ignore
        name, vram_mb, _ = _win_probe()
        if vram_mb and vram_mb > 0:
            vendor = _vendor_from_name(name)
            return vendor, round(vram_mb / 1024, 1)
    except Exception:
        pass

    # nvidia-smi (cross-platform if NVIDIA drivers present).
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True, timeout=4, stderr=subprocess.DEVNULL,
        ).strip().splitlines()
        if out:
            mb = max(int(v.strip()) for v in out if v.strip().isdigit())
            return "nvidia", round(mb / 1024, 1)
    except Exception:
        pass

    return "none", 0.0


def _vendor_from_name(name: str) -> str:
    low = (name or "").lower()
    if "nvidia" in low or "geforce" in low or "rtx" in low or "quadro" in low:
        return "nvidia"
    if "radeon" in low or "amd" in low:
        return "amd"
    if "intel" in low or "arc" in low or "iris" in low:
        return "intel"
    return "unknown" if name else "none"


def _classify_tier(gpu_vram_gb: float) -> HardwareTier:
    if gpu_vram_gb >= 48:
        return HardwareTier.EXTREME
    if gpu_vram_gb >= 24:
        return HardwareTier.HIGH
    if gpu_vram_gb >= 12:
        return HardwareTier.MID
    return HardwareTier.LOW


def detect_model_profile() -> HardwareModelProfile:
    """Probe hardware and return the recommended model profile. Never raises."""
    try:
        total_ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        total_ram_gb = 0.0
    try:
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
    except Exception:
        cores = 1
    try:
        bat = psutil.sensors_battery()
        on_battery = bool(bat and not bat.power_plugged)
    except Exception:
        on_battery = False

    gpu_vendor, gpu_vram_gb = _probe_gpu()
    tier = _classify_tier(gpu_vram_gb)

    return HardwareModelProfile(
        tier=tier,
        total_ram_gb=total_ram_gb,
        cpu_cores=int(cores),
        os_name=platform.system(),
        gpu_vendor=gpu_vendor,
        gpu_vram_gb=gpu_vram_gb,
        on_battery=on_battery,
        recommended_models=dict(_TIER_MODELS[tier]),
    )


def recommended_models_for_tier(tier: HardwareTier) -> dict[str, str]:
    return dict(_TIER_MODELS[tier])
