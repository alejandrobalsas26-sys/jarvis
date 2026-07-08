"""
core/hardware_model_profile.py — VRAM-tier → model recommendation (V60.0, Phase 4).

Complements core/hardware_profile.py (which tunes Ollama runtime flags) by
mapping best-effort hardware capability to a concrete set of recommended models
per cognitive ROLE, plus the `ollama pull` commands to obtain them.

GPU tiers (dedicated-VRAM capability — an ADVISORY axis only):
  LOW      — laptop / CPU-only / no useful dedicated VRAM
  MID      — 12–16 GB VRAM                         → 7B–14B comfortably
  HIGH     — 24–32 GB VRAM                         → 14B–32B
  EXTREME  — 48 GB+ VRAM                           → 32B–70B class

V66.1: a LOW GPU tier does NOT mean a weak machine. On an integrated-GPU host
with abundant system RAM (e.g. Ryzen 5 7430U + 64 GB), inference runs on the
CPU and *system RAM* — not VRAM — is the model-capacity ceiling. So when the GPU
is not accel-capable we recommend by a separate CPU/RAM policy instead of the
anemic LOW-VRAM set, and we NEVER pretend 64 GB of RAM is useless.

These are RECOMMENDATIONS only. The operator's explicit JARVIS_MODEL_* config
(resolved by core.model_router) always overrides them; this module never selects
a live model.

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


_ROLES = ("fast", "coder", "deep", "vision", "embedding", "verifier")

# Per-GPU-tier recommended model set, keyed by router role name. Modernized in
# V66.1 to the qwen3 / qwen2.5-coder / gemma3 family the runtime actually
# defaults to (no legacy deepseek-r1 / moondream / llava recommendations).
_TIER_MODELS: dict[HardwareTier, dict[str, str]] = {
    HardwareTier.LOW: {
        "fast":      "qwen3:8b",
        "coder":     "qwen2.5-coder:latest",
        "deep":      "qwen3:8b",
        "vision":    "gemma3:4b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:8b",
    },
    HardwareTier.MID: {
        "fast":      "qwen3:8b",
        "coder":     "qwen2.5-coder:latest",
        "deep":      "qwen3:14b",
        "vision":    "gemma3:4b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:8b",
    },
    HardwareTier.HIGH: {
        "fast":      "qwen3:8b",
        "coder":     "qwen2.5-coder:32b",
        "deep":      "qwen3:32b",
        "vision":    "llama3.2-vision:11b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:14b",
    },
    HardwareTier.EXTREME: {
        "fast":      "qwen3:14b",
        "coder":     "qwen2.5-coder:32b",
        "deep":      "qwen3:32b",
        "vision":    "llama3.2-vision:11b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:32b",
    },
}


class RamTier(str, Enum):
    """System-RAM capacity tier — the model-capacity ceiling for CPU inference."""
    CONSTRAINED = "CONSTRAINED"   # < 16 GB
    AMPLE = "AMPLE"               # 16–48 GB
    ABUNDANT = "ABUNDANT"         # >= 48 GB


def _classify_ram(total_ram_gb: float) -> RamTier:
    if total_ram_gb >= 48:
        return RamTier.ABUNDANT
    if total_ram_gb >= 16:
        return RamTier.AMPLE
    return RamTier.CONSTRAINED


# CPU-inference recommendation by system-RAM tier. Used when the GPU is NOT
# accel-capable (LOW tier) — so a 64 GB iGPU laptop gets the full qwen3 pair
# rather than the anemic small-VRAM set.
_CPU_RAM_MODELS: dict[RamTier, dict[str, str]] = {
    RamTier.ABUNDANT: {
        "fast":      "qwen3:8b",
        "coder":     "qwen2.5-coder:latest",
        "deep":      "qwen3:14b",
        "vision":    "gemma3:4b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:8b",
    },
    RamTier.AMPLE: {
        "fast":      "qwen3:8b",
        "coder":     "qwen2.5-coder:latest",
        "deep":      "qwen3:8b",
        "vision":    "gemma3:4b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:8b",
    },
    RamTier.CONSTRAINED: {
        "fast":      "qwen3:8b",
        "coder":     "qwen2.5-coder:latest",
        "deep":      "qwen3:8b",
        "vision":    "gemma3:4b",
        "embedding": "nomic-embed-text:latest",
        "verifier":  "qwen3:8b",
    },
}

# Dedicated VRAM (MB) below which the GPU is treated as non-accel-capable and
# inference is assumed CPU-bound.
_GPU_ACCEL_MIN_VRAM_GB = 4.0


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
    # V66.1 — explicit, separable capability axes (advisory).
    gpu_accel_capable: bool = False       # dedicated VRAM >= _GPU_ACCEL_MIN_VRAM_GB
    ram_tier: RamTier = RamTier.AMPLE
    inference_mode: str = "cpu"           # "gpu" | "cpu"

    def capability_summary(self) -> str:
        return (
            f"inference={self.inference_mode} | "
            f"gpu={'accel' if self.gpu_accel_capable else 'iGPU/shared'} "
            f"({self.gpu_vendor} {self.gpu_vram_gb}GB VRAM) | "
            f"system-RAM={self.total_ram_gb}GB [{self.ram_tier.value}] | "
            f"{self.cpu_cores} cores"
        )

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
    ram_tier = _classify_ram(total_ram_gb)
    gpu_accel = gpu_vram_gb >= _GPU_ACCEL_MIN_VRAM_GB

    # When the GPU is accel-capable, recommend by its VRAM tier. Otherwise
    # inference is CPU-bound and system RAM is the ceiling — recommend by the
    # CPU/RAM policy so an iGPU host with abundant RAM is not undersold.
    if gpu_accel:
        recommended = dict(_TIER_MODELS[tier])
        inference_mode = "gpu"
    else:
        recommended = dict(_CPU_RAM_MODELS[ram_tier])
        inference_mode = "cpu"

    return HardwareModelProfile(
        tier=tier,
        total_ram_gb=total_ram_gb,
        cpu_cores=int(cores),
        os_name=platform.system(),
        gpu_vendor=gpu_vendor,
        gpu_vram_gb=gpu_vram_gb,
        on_battery=on_battery,
        recommended_models=recommended,
        gpu_accel_capable=gpu_accel,
        ram_tier=ram_tier,
        inference_mode=inference_mode,
    )


def recommended_models_for_tier(tier: HardwareTier) -> dict[str, str]:
    return dict(_TIER_MODELS[tier])


def recommended_models_for_cpu_ram(total_ram_gb: float) -> dict[str, str]:
    """CPU-inference recommendation keyed by system-RAM tier (advisory)."""
    return dict(_CPU_RAM_MODELS[_classify_ram(total_ram_gb)])
