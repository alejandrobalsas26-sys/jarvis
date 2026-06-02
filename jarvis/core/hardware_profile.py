"""
core/hardware_profile.py — Universal Adaptive Hardware Profiler (v30.0).

Probes the host at every boot via CimInstance (wmic fallback removed —
wmic was deprecated in Windows 11 24H2). Self-configures JARVIS for any
hardware it finds: U-series laptop, H-series workstation, desktop, VM.

TDP Tiers and their configuration targets:
  U_SERIES_15W  → pools=1, ctx=4096,  fast=q5_K_M  (thermal-limited)
  H_SERIES_45W  → pools=2, ctx=8192,  fast=q8_0    (balanced mobile)
  HX_SERIES_55W → pools=3, ctx=16384, fast=q8_0    (performance mobile)
  DESKTOP       → pools=4, ctx=16384, fast=q8_0    (unconstrained)
  VM_GUEST      → pools=1, ctx=4096,  fast=q4_K_M  (shared resources)

Battery override: if on battery → pools=1, ctx=2048, fast=q4_K_M
"""

import subprocess
from dataclasses import dataclass
from loguru import logger
import psutil


# ── TDP tier definitions ──────────────────────────────────────────────────────

TDP_CONFIGS = {
    "U_SERIES_15W":  {"pools": 1, "ctx": 4096,  "model_fast": "qwen2.5:7b-instruct-q5_K_M"},
    "H_SERIES_45W":  {"pools": 2, "ctx": 8192,  "model_fast": "qwen2.5:7b-instruct-q8_0"},
    "HX_SERIES_55W": {"pools": 3, "ctx": 16384, "model_fast": "qwen2.5:7b-instruct-q8_0"},
    "DESKTOP":       {"pools": 4, "ctx": 16384, "model_fast": "qwen2.5:7b-instruct-q8_0"},
    "VM_GUEST":      {"pools": 1, "ctx": 4096,  "model_fast": "qwen2.5:7b-instruct-q4_K_M"},
}
# Battery mode: use q4_K_M (smaller, lower power consumption)
# q5_K_M is used on AC power for better output quality
BATTERY_OVERRIDE = {
    "pools": 1, "ctx": 2048, "model_fast": "qwen2.5:7b-instruct-q4_K_M"
}
MODEL_DEEP = "qwen2.5:14b-instruct-q4_K_M"


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class HardwareProfile:
    # RAM
    total_ram_gb:      float
    is_dual_channel:   bool
    ram_speed_mts:     int        # MT/s — 3200, 4800, 6400, etc.

    # CPU
    cpu_name:          str
    cpu_cores:         int
    cpu_tdp_tier:      str        # one of TDP_CONFIGS keys
    is_u_series:       bool
    is_vm_guest:       bool

    # GPU
    gpu_name:          str
    gpu_vram_mb:       int
    has_directml:      bool       # AMD RDNA / Intel Xe → True

    # Storage
    storage_type:      str        # "NVMe", "SATA_SSD", "HDD", "Unknown"

    # Power
    battery_present:   bool
    on_battery:        bool
    battery_percent:   float

    # Network
    has_alfa_adapter:  bool       # AWUS036ACM detected

    # Derived operational config (set after profiling)
    recommended_pools: int
    recommended_ctx:   int
    model_fast:        str
    model_deep:        str        = MODEL_DEEP


# ── WMI query helper ──────────────────────────────────────────────────────────

def _cim(wmi_class: str, props: list[str], timeout: int = 6) -> list[dict]:
    """
    Query WMI via PowerShell Get-CimInstance.
    Returns list of property dicts. Empty list on any failure.
    wmic was removed from Windows 11 24H2+; this is the only safe path.
    """
    select = ", ".join(props)
    cmd = (
        f"Get-CimInstance -ClassName {wmi_class} "
        f"| Select-Object {select} "
        f"| ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            text=True, timeout=timeout, stderr=subprocess.DEVNULL,
        ).strip()
        if not out:
            return []
        import json
        data = json.loads(out)
        return [data] if isinstance(data, dict) else data
    except Exception:
        return []


# ── Sub-probes ────────────────────────────────────────────────────────────────

def _probe_cpu() -> tuple[str, str, bool, bool]:
    """Returns (cpu_name, tdp_tier, is_u_series, is_vm_guest)."""
    rows = _cim("Win32_Processor", ["Name", "NumberOfCores"])
    cpu_name = rows[0].get("Name", "") if rows else ""

    # VM guest detection
    sys_rows = _cim("Win32_ComputerSystem", ["Model", "Manufacturer"])
    model = (sys_rows[0].get("Model", "") if sys_rows else "").lower()
    mfr   = (sys_rows[0].get("Manufacturer", "") if sys_rows else "").lower()
    is_vm = any(k in model or k in mfr for k in
                ("vmware", "virtualbox", "hyper-v", "kvm", "qemu", "xen"))

    if is_vm:
        return cpu_name, "VM_GUEST", False, True

    tokens = cpu_name.replace("-", " ").split()

    # U-series: mobile token ending in U with digits (e.g. 7430U, 1265U)
    is_u = any(t.endswith("U") and any(c.isdigit() for c in t) for t in tokens)
    # H-series: HX, HS, H suffix
    is_hx = any(t.endswith("HX") or t.endswith("HS") for t in tokens)
    is_h  = any(t.endswith("H") and any(c.isdigit() for c in t)
                for t in tokens) and not is_hx

    if is_u:
        tier = "U_SERIES_15W"
    elif is_hx:
        tier = "HX_SERIES_55W"
    elif is_h:
        tier = "H_SERIES_45W"
    else:
        tier = "DESKTOP"

    return cpu_name, tier, is_u, False


def _probe_gpu() -> tuple[str, int, bool]:
    """Returns (gpu_name, vram_mb, has_directml)."""
    rows = _cim("Win32_VideoController",
                ["Name", "AdapterRAM", "VideoProcessor"])
    if not rows:
        return "Unknown", 0, False

    # Pick the best (highest VRAM) adapter
    def _vram(r: dict) -> int:
        try:
            return int(r.get("AdapterRAM") or 0)
        except (TypeError, ValueError):
            return 0

    best = max(rows, key=_vram)
    name     = best.get("Name", "Unknown") or "Unknown"
    vram_raw = _vram(best)
    vram_mb  = vram_raw // (1024 * 1024)

    # DirectML: AMD RDNA, Intel Xe, NVIDIA all support it on Windows
    has_dm = any(k in name.lower() for k in ("radeon", "intel", "nvidia", "arc"))
    return name, vram_mb, has_dm


def _probe_ram() -> tuple[float, bool, int]:
    """Returns (total_gb, is_dual_channel, speed_mts)."""
    total_gb = psutil.virtual_memory().total / (1024 ** 3)
    rows = _cim("Win32_PhysicalMemory", ["Capacity", "Speed", "BankLabel"])

    slots_used = len(rows)
    speed_mts  = 0
    if rows:
        speeds: list[int] = []
        for r in rows:
            try:
                speeds.append(int(r.get("Speed") or 0))
            except (TypeError, ValueError):
                continue
        if speeds:
            speed_mts = max(speeds)

    dual = slots_used >= 2
    return total_gb, dual, speed_mts


def _detect_storage_type() -> str:
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PhysicalDisk | Select-Object -ExpandProperty MediaType"],
            capture_output=True, text=True, timeout=5)
        out = r.stdout.upper()
        if "SSD" in out: return "SSD"
        if "HDD" in out: return "HDD"
        r2 = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_DiskDrive | Select -ExpandProperty Caption"],
            capture_output=True, text=True, timeout=5)
        caps = r2.stdout.upper()
        if any(k in caps for k in ("NVME","SSD","SOLID","M.2","FLASH")):
            return "SSD"
        return "SSD"
    except Exception:
        return "SSD"


def _probe_battery() -> tuple[bool, bool, float]:
    """Returns (battery_present, on_battery, percent)."""
    try:
        bat = psutil.sensors_battery()
    except Exception:
        return False, False, 100.0
    if bat is None:
        return False, False, 100.0
    return True, not bat.power_plugged, bat.percent


def _probe_alfa() -> bool:
    """Detect ALFA AWUS036ACM via network adapter names."""
    rows = _cim("Win32_NetworkAdapter", ["Name", "Description"])
    for r in rows:
        name = ((r.get("Name", "") or "") + (r.get("Description", "") or "")).lower()
        if "alfa" in name or "awus" in name or "mt7612" in name or "rtl8812" in name:
            return True
    return False


# ── Storage-aware async pool sizing ──────────────────────────────────────────

def _storage_pool_bonus(storage_type: str) -> int:
    """NVMe gets +1 async I/O worker over SATA. HDD gets -1."""
    return {"NVMe": 1, "SATA_SSD": 0, "HDD": -1}.get(storage_type, 0)


# ── Main detector ─────────────────────────────────────────────────────────────

def detect_hardware() -> HardwareProfile:
    """
    Full hardware probe. Runs once at boot.
    Never raises — all sub-probes are individually try/excepted.
    """
    total_gb, dual, ram_speed = _probe_ram()
    cpu_name, tier, is_u, is_vm = _probe_cpu()
    gpu_name, gpu_vram, has_dm  = _probe_gpu()
    storage_type                 = _detect_storage_type()
    bat_present, on_bat, bat_pct = _probe_battery()
    has_alfa                     = _probe_alfa()
    cores                        = psutil.cpu_count(logical=False) or 4

    cfg = dict(TDP_CONFIGS[tier])  # copy

    # Battery override: power-saving mode when unplugged
    if bat_present and on_bat:
        logger.warning("HARDWARE: on battery — applying power-save override")
        cfg = dict(BATTERY_OVERRIDE)

    # Storage bonus/penalty on pools
    pool_adj = _storage_pool_bonus(storage_type)
    cfg["pools"] = max(1, cfg["pools"] + pool_adj)

    p = HardwareProfile(
        total_ram_gb      = total_gb,
        is_dual_channel   = dual,
        ram_speed_mts     = ram_speed,
        cpu_name          = cpu_name,
        cpu_cores         = cores,
        cpu_tdp_tier      = tier,
        is_u_series       = is_u,
        is_vm_guest       = is_vm,
        gpu_name          = gpu_name,
        gpu_vram_mb       = gpu_vram,
        has_directml      = has_dm,
        storage_type      = storage_type,
        battery_present   = bat_present,
        on_battery        = on_bat,
        battery_percent   = bat_pct,
        has_alfa_adapter  = has_alfa,
        recommended_pools = cfg["pools"],
        recommended_ctx   = cfg["ctx"],
        model_fast        = cfg["model_fast"],
        model_deep        = MODEL_DEEP,
    )

    _log_profile(p)

    # Publish module-level pools value for tool files that import it directly
    import core.hardware_profile as _self
    _self.recommended_pools = p.recommended_pools

    return p


def _log_profile(p: HardwareProfile) -> None:
    tier_label = {
        "U_SERIES_15W":  "U-Series 15W",
        "H_SERIES_45W":  "H-Series 45W",
        "HX_SERIES_55W": "HX-Series 55W",
        "DESKTOP":       "Desktop/Workstation",
        "VM_GUEST":      "VM Guest",
    }.get(p.cpu_tdp_tier, p.cpu_tdp_tier)

    logger.info(
        f"HARDWARE: {p.cpu_name} [{tier_label}] | "
        f"RAM: {p.total_ram_gb:.0f}GB "
        f"{'DUAL' if p.is_dual_channel else 'SINGLE'}-CH "
        f"{p.ram_speed_mts}MT/s | "
        f"GPU: {p.gpu_name} ({p.gpu_vram_mb}MB VRAM) | "
        f"Storage: {p.storage_type}"
    )
    logger.info(
        f"HARDWARE: CONFIG → pools={p.recommended_pools} "
        f"ctx={p.recommended_ctx} "
        f"fast={p.model_fast}"
        + (" [BATTERY]" if p.on_battery else "")
        + (" [VM]"      if p.is_vm_guest else "")
    )
    if p.has_alfa_adapter:
        logger.info("HARDWARE: ALFA AWUS036ACM detected — RF bridge available")


# ── Exported singleton ────────────────────────────────────────────────────────

_cached_profile: HardwareProfile | None = None


def get_cached_profile() -> HardwareProfile | None:
    return _cached_profile


def set_cached_profile(p: HardwareProfile) -> None:
    global _cached_profile
    _cached_profile = p


# Module-level pools value — populated after detect_hardware() runs from main.py.
# Tool files that do `from core.hardware_profile import recommended_pools`
# will read this value at import time; main.py refreshes it during boot.
recommended_pools: int = 1
