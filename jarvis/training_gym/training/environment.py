"""training_gym/training/environment.py — V69 M62 S3A: what this host can actually do.

WHY DETECTION IS SPLIT FROM PROBING
-----------------------------------
Cheap facts — OS family, Python version, CPU count, RAM, free disk — come from ``sys``,
``platform``, ``os`` and ``psutil`` (already a base dependency). None of them loads a
framework or touches an accelerator.

Whether CUDA *works* is not a cheap fact. The only reliable answer requires initialising
a runtime, and the library that answers it is the same library that would download and
train. So an accelerator probe is never run implicitly: :meth:`HardwareCapabilityReport.
detect` performs one only when a caller passes an explicit probe callable, and the
default is a probe that reports ``UNKNOWN``.

PACKAGE PRESENCE IS NOT CAPABILITY
----------------------------------
``find_spec("torch") is not None`` means a wheel is installed. It does not mean a GPU
exists, that its driver matches, or that ``bitsandbytes`` will import. This module keeps
those as separate fields and never lets one stand in for the other — the single most
common way a planner ends up declaring a run feasible that cannot start.

WHAT IS NEVER RECORDED
----------------------
Hostname, username, GPU UUID, serial numbers, MAC addresses, absolute paths, and the
environment block. RAM and disk are reported as a rounded whole number of gigabytes and
a coarse category: enough to size a run, not enough to fingerprint a machine.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..schemas import sha256_obj
from .config import DevicePolicy, PrecisionPolicy, TrainingMethod

#: Bumped when the report's shape changes. Part of the plan hash.
HARDWARE_REPORT_VERSION = "m62.hardware_report.1"


class Availability(str, Enum):
    """Three answers, and the third is not a failure to produce one of the first two."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class SizeCategory(str, Enum):
    """Coarse buckets. Precise enough to plan with, coarse enough not to identify."""

    UNKNOWN = "unknown"
    UNDER_8GB = "under_8gb"
    FROM_8_TO_16GB = "8_to_16gb"
    FROM_16_TO_32GB = "16_to_32gb"
    FROM_32_TO_64GB = "32_to_64gb"
    AT_LEAST_64GB = "at_least_64gb"


def categorize_gb(gigabytes: float) -> SizeCategory:
    if gigabytes <= 0:
        return SizeCategory.UNKNOWN
    if gigabytes < 8:
        return SizeCategory.UNDER_8GB
    if gigabytes < 16:
        return SizeCategory.FROM_8_TO_16GB
    if gigabytes < 32:
        return SizeCategory.FROM_16_TO_32GB
    if gigabytes < 64:
        return SizeCategory.FROM_32_TO_64GB
    return SizeCategory.AT_LEAST_64GB


def _spec_present(module: str) -> bool:
    """Is a module importable? Answered from metadata; the module is never loaded."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


@dataclass(frozen=True)
class AcceleratorProbe:
    """The result of an explicit, bounded accelerator probe. Never produced implicitly."""

    cuda_runtime: Availability = Availability.UNKNOWN
    cuda_device_count: int = 0
    cuda_compute_capability: str = ""
    cuda_bf16_supported: Availability = Availability.UNKNOWN
    mps_runtime: Availability = Availability.UNKNOWN
    accelerator_memory_gb: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cuda_runtime": self.cuda_runtime.value,
            "cuda_device_count": self.cuda_device_count,
            "cuda_compute_capability": self.cuda_compute_capability,
            "cuda_bf16_supported": self.cuda_bf16_supported.value,
            "mps_runtime": self.mps_runtime.value,
            "accelerator_memory_gb": self.accelerator_memory_gb,
        }


def no_accelerator_probe() -> AcceleratorProbe:
    """The default. Answers ``UNKNOWN`` without loading anything.

    Deliberately not "answers UNAVAILABLE": this host may well have a working GPU, and
    claiming otherwise from a probe that was never run is exactly the fabrication the
    module exists to prevent.
    """
    return AcceleratorProbe()


@dataclass(frozen=True)
class HardwareCapabilityReport:
    """A passive, path-free, host-identifying-detail-free description of this machine."""

    os_family: str
    python_version: str
    process_architecture: str
    cpu_architecture: str
    logical_cpus: int
    total_ram_gb: int
    total_ram_category: SizeCategory
    available_ram_gb: int = 0
    available_ram_category: SizeCategory = SizeCategory.UNKNOWN
    torch_package: Availability = Availability.UNKNOWN
    bitsandbytes_package: Availability = Availability.UNKNOWN
    directml_package: Availability = Availability.UNKNOWN
    accelerator: AcceleratorProbe = AcceleratorProbe()
    accelerator_probe_ran: bool = False
    output_disk_free_gb: int = 0
    output_disk_category: SizeCategory = SizeCategory.UNKNOWN
    report_version: str = HARDWARE_REPORT_VERSION

    # ── capability questions, each answered from one field and never another ──
    @property
    def cpu_available(self) -> Availability:
        """A CPU always exists. Whether training on it is *sensible* is a separate
        question, answered by the feasibility report, not by this one."""
        return Availability.AVAILABLE

    @property
    def cuda_available(self) -> Availability:
        """Runtime evidence only. An installed torch wheel is not a GPU."""
        return self.accelerator.cuda_runtime

    @property
    def mps_available(self) -> Availability:
        return self.accelerator.mps_runtime

    @property
    def directml_available(self) -> Availability:
        """Package presence only — and DirectML is an inference path.

        Recognising it is not a claim that Transformers/PEFT can train through it, and
        the device selector refuses it for training for exactly that reason.
        """
        return self.directml_package

    @property
    def qlora_supported(self) -> Availability:
        """4-bit training needs BOTH a CUDA runtime and bitsandbytes. Neither alone."""
        if (self.cuda_available is Availability.AVAILABLE
                and self.bitsandbytes_package is Availability.AVAILABLE):
            return Availability.AVAILABLE
        if (self.cuda_available is Availability.UNAVAILABLE
                or self.bitsandbytes_package is Availability.UNAVAILABLE):
            return Availability.UNAVAILABLE
        return Availability.UNKNOWN

    @property
    def supported_precisions(self) -> tuple[str, ...]:
        """Only what the measurements support. FP32 on CPU is always among them."""
        found = [PrecisionPolicy.FP32.value]
        if self.cuda_available is Availability.AVAILABLE:
            found.append(PrecisionPolicy.FP16.value)
            if self.accelerator.cuda_bf16_supported is Availability.AVAILABLE:
                found.append(PrecisionPolicy.BF16.value)
            if self.qlora_supported is Availability.AVAILABLE:
                found.append(PrecisionPolicy.INT4_QLORA.value)
        return tuple(found)

    @property
    def usable_memory_gb(self) -> float:
        """The budget training may lean on.

        Accelerator memory when a probe measured some; otherwise 60% of host RAM — CPU
        training must leave headroom for the operating system and for JARVIS itself.
        The same fraction :class:`core.training_pipeline.HardwareProfile` already uses,
        kept identical so the two authorities cannot disagree about the same host.
        """
        if (self.cuda_available is Availability.AVAILABLE
                and self.accelerator.accelerator_memory_gb > 0):
            return round(self.accelerator.accelerator_memory_gb, 1)
        return round(self.total_ram_gb * 0.6, 1)

    def to_dict(self) -> dict:
        return {
            "os_family": self.os_family,
            "python_version": self.python_version,
            "process_architecture": self.process_architecture,
            "cpu_architecture": self.cpu_architecture,
            "logical_cpus": self.logical_cpus,
            "total_ram_gb": self.total_ram_gb,
            "total_ram_category": self.total_ram_category.value,
            "available_ram_gb": self.available_ram_gb,
            "available_ram_category": self.available_ram_category.value,
            "torch_package": self.torch_package.value,
            "bitsandbytes_package": self.bitsandbytes_package.value,
            "directml_package": self.directml_package.value,
            "accelerator": self.accelerator.to_dict(),
            "accelerator_probe_ran": self.accelerator_probe_ran,
            "cuda_available": self.cuda_available.value,
            "mps_available": self.mps_available.value,
            "qlora_supported": self.qlora_supported.value,
            "supported_precisions": list(self.supported_precisions),
            "usable_memory_gb": self.usable_memory_gb,
            "output_disk_free_gb": self.output_disk_free_gb,
            "output_disk_category": self.output_disk_category.value,
            "report_version": self.report_version,
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def detect(cls, *, output_root: str | Path | None = None,
               accelerator_probe=None) -> "HardwareCapabilityReport":
        """Read the cheap facts. Runs an accelerator probe only if given one.

        Every individual probe is wrapped: a failure becomes ``UNKNOWN`` or zero, never
        an exception and never a fabricated capability. ``output_root`` is the ONLY path
        consulted, and only for free space — no other drive is touched and nothing is
        created.
        """
        total_gb, available_gb, cpus = 0, 0, 0
        try:
            import psutil

            memory = psutil.virtual_memory()
            total_gb = int(memory.total / 1e9)
            available_gb = int(memory.available / 1e9)
            cpus = psutil.cpu_count(logical=True) or 0
        except Exception:  # noqa: BLE001 — an unmeasured host reports zero, not a guess
            try:
                cpus = os.cpu_count() or 0
            except Exception:  # noqa: BLE001 — pragma: no cover
                cpus = 0

        disk_gb = 0
        if output_root is not None:
            try:
                root = Path(output_root)
                if root.is_dir() and not root.is_symlink():
                    disk_gb = int(shutil.disk_usage(root).free / 1e9)
            except (OSError, ValueError):
                disk_gb = 0

        probe = no_accelerator_probe()
        probe_ran = False
        if accelerator_probe is not None:
            try:
                result = accelerator_probe()
                if isinstance(result, AcceleratorProbe):
                    probe, probe_ran = result, True
            except Exception:  # noqa: BLE001 — a probe that crashed proved nothing
                probe, probe_ran = no_accelerator_probe(), False

        return cls(
            os_family=platform.system() or "unknown",
            python_version=".".join(str(p) for p in sys.version_info[:3]),
            process_architecture="64bit" if sys.maxsize > 2**32 else "32bit",
            cpu_architecture=(platform.machine() or "unknown").lower(),
            logical_cpus=int(cpus),
            total_ram_gb=total_gb, total_ram_category=categorize_gb(total_gb),
            available_ram_gb=available_gb,
            available_ram_category=categorize_gb(available_gb),
            torch_package=(Availability.AVAILABLE if _spec_present("torch")
                           else Availability.UNAVAILABLE),
            bitsandbytes_package=(Availability.AVAILABLE
                                  if _spec_present("bitsandbytes")
                                  else Availability.UNAVAILABLE),
            directml_package=(Availability.AVAILABLE if _spec_present("torch_directml")
                              else Availability.UNAVAILABLE),
            accelerator=probe, accelerator_probe_ran=probe_ran,
            output_disk_free_gb=disk_gb, output_disk_category=categorize_gb(disk_gb))


# ══════════════════════════════════════════════════════════════════════════════
#  Device selection
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class DeviceSelection:
    """Which device was chosen, why, and — if none could be — exactly what is missing."""

    requested: DevicePolicy
    selected: str
    reason: str
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {"requested": self.requested.value, "selected": self.selected,
                "reason": self.reason, "blockers": list(self.blockers)}


def select_device(policy: DevicePolicy,
                  report: HardwareCapabilityReport) -> DeviceSelection:
    """Resolve a device policy against measured capability.

    An explicit request is never silently downgraded. Asking for CUDA on a host with no
    CUDA evidence produces a refusal, not a CPU run: the operator asked for a
    thirty-minute job and would otherwise get a twelve-hour one and no warning.
    """
    if policy is DevicePolicy.CPU:
        return DeviceSelection(policy, "cpu", "explicitly requested")

    if policy is DevicePolicy.CUDA:
        if report.cuda_available is Availability.AVAILABLE:
            return DeviceSelection(policy, "cuda", "explicitly requested and measured "
                                                   "present")
        detail = ("no accelerator probe was run, so there is no evidence either way"
                  if not report.accelerator_probe_ran
                  else "a probe ran and found no usable CUDA runtime")
        return DeviceSelection(
            policy, "", "requested cuda is not available",
            (f"cuda was requested explicitly but {detail}; falling back to the CPU "
             f"would silently turn a short run into a very long one, so this is a "
             f"refusal rather than a substitution",))

    if policy is DevicePolicy.MPS:
        if report.mps_available is Availability.AVAILABLE:
            return DeviceSelection(policy, "mps", "explicitly requested and measured "
                                                  "present")
        return DeviceSelection(
            policy, "", "requested mps is not available",
            ("mps was requested but no probe measured a usable Metal runtime",))

    if policy is DevicePolicy.DIRECTML:
        return DeviceSelection(
            policy, "", "directml is not a supported training backend",
            ("torch-directml is recognised as an INFERENCE path on this repository's "
             "Windows hosts; it is not an established Transformers/PEFT training "
             "backend, and this milestone will not claim otherwise on its behalf",))

    # AUTO_SAFE — choose only from what was measured, in descending capability order.
    if report.cuda_available is Availability.AVAILABLE:
        return DeviceSelection(policy, "cuda", "measured cuda runtime present")
    if report.mps_available is Availability.AVAILABLE:
        return DeviceSelection(policy, "mps", "measured metal runtime present")
    return DeviceSelection(
        policy, "cpu",
        "no accelerator runtime was measured; the cpu is the only device with evidence")


# ══════════════════════════════════════════════════════════════════════════════
#  Precision selection
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PrecisionSelection:
    requested: PrecisionPolicy
    selected: str
    reason: str
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {"requested": self.requested.value, "selected": self.selected,
                "reason": self.reason, "blockers": list(self.blockers)}


def select_precision(policy: PrecisionPolicy, *, device: str,
                     report: HardwareCapabilityReport,
                     method: TrainingMethod) -> PrecisionSelection:
    """Resolve a precision policy against the measured device.

    Unsupported precision is refused rather than downgraded. A run that quietly becomes
    fp32 when bf16 was asked for produces different numbers, a different memory profile
    and a different wall clock from the one that was approved.
    """
    supported = report.supported_precisions

    if policy is PrecisionPolicy.AUTO_SAFE:
        if method.needs_quantization:
            if report.qlora_supported is Availability.AVAILABLE:
                return PrecisionSelection(policy, PrecisionPolicy.INT4_QLORA.value,
                                          "cuda and bitsandbytes both measured present")
            return PrecisionSelection(
                policy, "", "qlora has no supported precision on this host",
                ("4-bit training requires a measured CUDA runtime AND bitsandbytes; "
                 "quantized INFERENCE support elsewhere on this host is not evidence "
                 "that quantized TRAINING works",))
        if device == "cuda":
            if PrecisionPolicy.BF16.value in supported:
                return PrecisionSelection(policy, PrecisionPolicy.BF16.value,
                                          "measured bf16 support on the selected device")
            return PrecisionSelection(policy, PrecisionPolicy.FP16.value,
                                      "cuda present; bf16 support was not measured")
        return PrecisionSelection(
            policy, PrecisionPolicy.FP32.value,
            f"{device or 'cpu'} has no measured reduced-precision training support")

    if policy is PrecisionPolicy.FP32:
        return PrecisionSelection(policy, PrecisionPolicy.FP32.value,
                                  "explicitly requested and universally supported")

    if policy is PrecisionPolicy.FP16:
        if device == "cpu":
            return PrecisionSelection(
                policy, "", "fp16 was requested on the cpu",
                ("fp16 training on a CPU is not a supported configuration: there is no "
                 "hardware path for it here, and a silent promotion to fp32 would "
                 "change the numerics of a run that was approved as fp16",))
        if PrecisionPolicy.FP16.value not in supported:
            return PrecisionSelection(
                policy, "", "fp16 support was not measured",
                ("fp16 was requested but no probe measured an accelerator that supports "
                 "it",))
        return PrecisionSelection(policy, PrecisionPolicy.FP16.value,
                                  "explicitly requested and measured supported")

    if policy is PrecisionPolicy.BF16:
        if PrecisionPolicy.BF16.value not in supported:
            return PrecisionSelection(
                policy, "", "bf16 support was not measured",
                ("bf16 was requested but nothing measured a device that supports it; "
                 "assuming it because the hardware is recent is how a run fails at "
                 "step zero after a long model load",))
        return PrecisionSelection(policy, PrecisionPolicy.BF16.value,
                                  "explicitly requested and measured supported")

    if policy is PrecisionPolicy.INT4_QLORA:
        if report.qlora_supported is not Availability.AVAILABLE:
            return PrecisionSelection(
                policy, "", "4-bit training is not supported here",
                ("int4_qlora requires a measured CUDA runtime and bitsandbytes; "
                 f"cuda={report.cuda_available.value}, "
                 f"bitsandbytes={report.bitsandbytes_package.value}",))
        return PrecisionSelection(policy, PrecisionPolicy.INT4_QLORA.value,
                                  "cuda and bitsandbytes both measured present")

    # INT8_TRAINING
    return PrecisionSelection(
        policy, "", "int8 training has no support evidence on this host",
        ("int8 TRAINING requires explicit support evidence this milestone has not "
         "gathered; int8 inference support is a different claim",))


__all__ = [
    "HARDWARE_REPORT_VERSION", "AcceleratorProbe", "Availability", "DeviceSelection",
    "HardwareCapabilityReport", "PrecisionSelection", "SizeCategory", "categorize_gb",
    "no_accelerator_probe", "select_device", "select_precision",
]
