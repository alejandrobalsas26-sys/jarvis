"""
core/runtime_doctor.py — V69 M63: one in-process diagnostic, no repairs.

WHAT IT IS
----------
A structured, deterministic answer to "can this machine actually run JARVIS
right now, and what is missing?". Every check returns the same shaped finding —
``check_id``, ``component``, ``status``, ``severity``, ``evidence``,
``remediation`` — so the HUD, the CLI and a test can all consume one vocabulary.

WHAT IT IS NOT
--------------
It does not repair anything. There is no ``--fix``, no pip invocation, no
mutation of a virtualenv and no write to a config file anywhere in this module.
A doctor that silently edits the patient makes the next diagnosis a lie about
what the operator's machine actually looked like. Findings carry a
``remediation`` STRING for a human to run; nothing here runs it.

RELATIONSHIP TO THE EXISTING HEALTH CODE
----------------------------------------
:mod:`core.runtime_health` answers "how are the LIVE subsystems doing" for a
running process (collectors, inference, tasks) and is unchanged. ``scripts/
doctor.py`` is the standalone CLI and is also unchanged. This module is the
in-process ENVIRONMENT diagnosis those two do not cover, and it is the only one
that checks the thing that actually breaks homelab installs: an interpreter
that does not match the environment its packages were installed into.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# The doctor must be importable in the environment it is asked to diagnose, and a
# broken or deliberately minimal environment is precisely where it earns its keep: a
# training venv holding the pinned backend and nothing else has no `loguru`, so a
# hard import here made the one module that detects interpreter/site-packages drift
# the one module that could not run inside it. `logger` is used exactly once, for a
# warning in an exception handler, and `logging.Logger.warning` is the same call, so
# the fallback costs nothing but the module's stdlib-only import surface.
try:
    from loguru import logger
except ModuleNotFoundError:  # pragma: no cover - covered by the stdlib-only test
    import logging

    logger = logging.getLogger("runtime_doctor")

SCHEMA_VERSION = "runtime-doctor-1"


class DoctorStatus(str, Enum):
    PASS = "pass"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    OPTIONAL_MISSING = "optional_missing"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Finding:
    check_id: str
    component: str
    status: DoctorStatus
    severity: Severity
    evidence: str
    remediation: str = ""

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id, "component": self.component,
            "status": self.status.value, "severity": self.severity.value,
            "evidence": self.evidence[:512], "remediation": self.remediation[:512],
        }


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> list[Finding]:
        return [f for f in self.findings if f.status is DoctorStatus.BLOCKED]

    @property
    def degraded(self) -> list[Finding]:
        return [f for f in self.findings if f.status is DoctorStatus.DEGRADED]

    @property
    def optional_missing(self) -> list[Finding]:
        return [f for f in self.findings if f.status is DoctorStatus.OPTIONAL_MISSING]

    @property
    def overall(self) -> DoctorStatus:
        """Optional-missing NEVER degrades the overall verdict — that is the
        whole point of the category: a homelab without Proxmox is healthy."""
        if self.blocked:
            return DoctorStatus.BLOCKED
        if self.degraded:
            return DoctorStatus.DEGRADED
        return DoctorStatus.PASS

    def to_dict(self) -> dict:
        by_status: dict[str, int] = {}
        for f in self.findings:
            by_status[f.status.value] = by_status.get(f.status.value, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "overall": self.overall.value,
            "counts": dict(sorted(by_status.items())),
            "blocked": [f.check_id for f in self.blocked],
            "optional_missing": [f.check_id for f in self.optional_missing],
            "findings": [f.to_dict() for f in self.findings],
            "auto_repair_performed": False,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Interpreter / environment drift — the check that matters most
# ══════════════════════════════════════════════════════════════════════════════
_PY_DIR_RE = re.compile(r"python(\d+)\.(\d+)")


def site_packages_versions() -> set[tuple[int, int]]:
    """Every ``pythonX.Y`` version stamped into the active import path.

    A healthy install yields exactly one, matching the running interpreter. Two
    means packages were installed by one interpreter and are being imported by
    another — the classic "it works in the venv but not from the launcher"
    failure, which otherwise surfaces as a baffling ImportError.
    """
    found: set[tuple[int, int]] = set()
    for entry in sys.path:
        if "site-packages" not in entry and "dist-packages" not in entry:
            continue
        match = _PY_DIR_RE.search(entry.replace("\\", "/"))
        if match:
            found.add((int(match.group(1)), int(match.group(2))))
    return found


def check_interpreter(*, path_versions: set[tuple[int, int]] | None = None,
                      running: tuple[int, int] | None = None) -> list[Finding]:
    """Detect a Python 3.X package environment invoked from a Python 3.Y.

    Both inputs are injectable so the mismatch can be tested deterministically
    without needing two interpreters installed on the test machine.
    """
    running = running or (sys.version_info.major, sys.version_info.minor)
    versions = path_versions if path_versions is not None else site_packages_versions()
    out = [Finding(
        "python.executable", "python", DoctorStatus.PASS, Severity.INFO,
        f"{sys.executable} ({running[0]}.{running[1]} on {platform.system()})")]

    if running < (3, 11):
        out.append(Finding(
            "python.version", "python", DoctorStatus.BLOCKED, Severity.CRITICAL,
            f"running Python {running[0]}.{running[1]}; this codebase uses 3.11+ syntax",
            "install Python 3.11 or newer and recreate the virtualenv"))

    mismatched = {v for v in versions if v != running}
    if mismatched:
        pretty = ", ".join(f"{a}.{b}" for a, b in sorted(mismatched))
        out.append(Finding(
            "python.environment_drift", "python", DoctorStatus.BLOCKED,
            Severity.CRITICAL,
            f"the running interpreter is {running[0]}.{running[1]} but the import "
            f"path contains site-packages built for {pretty}. Packages installed "
            f"under one interpreter are being imported by another",
            f"run JARVIS with the interpreter that owns those packages, or "
            f"reinstall them under {running[0]}.{running[1]} "
            f"(python{running[0]}.{running[1]} -m pip install -e .)"))
    elif versions:
        out.append(Finding(
            "python.environment_drift", "python", DoctorStatus.PASS, Severity.INFO,
            f"import path site-packages match the running "
            f"{running[0]}.{running[1]} interpreter"))

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    out.append(Finding(
        "python.venv", "python",
        DoctorStatus.PASS if in_venv else DoctorStatus.DEGRADED,
        Severity.INFO if in_venv else Severity.LOW,
        sys.prefix if in_venv else "not running inside a virtualenv",
        "" if in_venv else "create and activate .venv, then reinstall"))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Imports and dependencies
# ══════════════════════════════════════════════════════════════════════════════
#: Packages JARVIS genuinely cannot start without. Absent is BLOCKED.
_CORE_MODULES = ("pydantic", "pydantic_settings", "loguru", "httpx", "psutil", "yaml")

#: Declared base dependencies beyond the core. Absent is DEGRADED, not BLOCKED:
#: the repository's own base-import gate (``scripts/check_base_import.py``)
#: passes without some of these, so calling their absence fatal would make this
#: doctor disagree with the packaging check that actually governs the install.
_BASE_MODULES = ("openai", "requests", "aiohttp", "aiosqlite")

#: Optional by DESIGN. Absent is ``OPTIONAL_MISSING``, never a failure — the
#: base install deliberately does not carry the training stack.
_TRAINING_MODULES = ("torch", "transformers", "peft", "trl", "datasets", "accelerate")

_PROFILE_MODULES = {
    "voice": ("sounddevice", "faster_whisper", "pyttsx3"),
    "soc": ("yara", "scapy"),
    "lab": ("docker",),
    "docs": ("pdfplumber", "docx"),
}


def _installed(module: str) -> bool:
    """Presence WITHOUT importing. Importing a heavy optional package to see if
    it is there costs seconds and can have side effects; a spec lookup cannot."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def check_dependencies() -> list[Finding]:
    out: list[Finding] = []
    missing_core = [m for m in _CORE_MODULES if not _installed(m)]
    out.append(Finding(
        "deps.core", "dependencies",
        DoctorStatus.PASS if not missing_core else DoctorStatus.BLOCKED,
        Severity.INFO if not missing_core else Severity.CRITICAL,
        "all core packages present" if not missing_core
        else f"missing: {', '.join(missing_core)} — JARVIS cannot start",
        "" if not missing_core else "pip install -e . from the jarvis/ directory"))

    missing_base = [m for m in _BASE_MODULES if not _installed(m)]
    out.append(Finding(
        "deps.base", "dependencies",
        DoctorStatus.PASS if not missing_base else DoctorStatus.DEGRADED,
        Severity.INFO if not missing_base else Severity.MEDIUM,
        "all declared base packages present" if not missing_base
        else f"declared but absent: {', '.join(missing_base)}; features that use "
             f"them are unavailable, the base import surface still loads",
        "" if not missing_base else "pip install -e . from the jarvis/ directory"))

    present_training = [m for m in _TRAINING_MODULES if _installed(m)]
    if len(present_training) == len(_TRAINING_MODULES):
        status, evidence = DoctorStatus.PASS, "full training stack present"
    elif present_training:
        status = DoctorStatus.OPTIONAL_MISSING
        evidence = (f"partial training stack: present "
                    f"{', '.join(present_training)}; missing "
                    f"{', '.join(m for m in _TRAINING_MODULES if m not in present_training)}")
    else:
        status, evidence = DoctorStatus.OPTIONAL_MISSING, "no training stack installed"
    out.append(Finding(
        "deps.training", "dependencies", status, Severity.INFO, evidence,
        "" if status is DoctorStatus.PASS
        else "pip install -e '.[training]' — only needed to TRAIN, never to run"))

    for profile, modules in _PROFILE_MODULES.items():
        missing = [m for m in modules if not _installed(m)]
        out.append(Finding(
            f"deps.profile.{profile}", "dependencies",
            DoctorStatus.PASS if not missing else DoctorStatus.OPTIONAL_MISSING,
            Severity.INFO,
            "present" if not missing else f"missing: {', '.join(missing)}",
            "" if not missing else f"pip install -e '.[{profile}]'"))
    return out


def check_entrypoints() -> list[Finding]:
    """Console scripts the packaging declares. Absent means "installed without
    -e, or not installed at all", which is a real and common homelab state."""
    found = [name for name in ("jarvis", "jarvis-doctor") if shutil.which(name)]
    return [Finding(
        "packaging.entrypoints", "packaging",
        DoctorStatus.PASS if found else DoctorStatus.DEGRADED,
        Severity.INFO if found else Severity.LOW,
        f"console scripts on PATH: {', '.join(found)}" if found
        else "no jarvis console script on PATH",
        "" if found else "pip install -e . to create the console entrypoints")]


# ══════════════════════════════════════════════════════════════════════════════
#  Host resources and directories
# ══════════════════════════════════════════════════════════════════════════════
def check_resources() -> list[Finding]:
    out: list[Finding] = []
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return [Finding("host.resources", "host", DoctorStatus.DEGRADED,
                        Severity.MEDIUM, f"psutil unavailable: {exc}",
                        "pip install psutil")]

    ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    out.append(Finding(
        "host.ram", "host",
        DoctorStatus.PASS if ram_gb >= 8 else DoctorStatus.DEGRADED,
        Severity.INFO if ram_gb >= 8 else Severity.MEDIUM,
        f"{ram_gb:.1f} GiB total RAM",
        "" if ram_gb >= 8 else "local model inference wants 8 GiB or more"))

    cpus = psutil.cpu_count(logical=True) or 1
    out.append(Finding(
        "host.cpu", "host",
        DoctorStatus.PASS if cpus >= 2 else DoctorStatus.DEGRADED,
        Severity.INFO, f"{cpus} logical CPUs"))

    try:
        usage = psutil.disk_usage(str(Path.cwd()))
        free_gb = usage.free / (1024 ** 3)
        out.append(Finding(
            "host.disk", "host",
            DoctorStatus.PASS if free_gb >= 5 else DoctorStatus.DEGRADED,
            Severity.INFO if free_gb >= 5 else Severity.HIGH,
            f"{free_gb:.1f} GiB free ({usage.percent:.0f}% used)",
            "" if free_gb >= 5 else "free disk space before training or model pulls"))
    except OSError as exc:
        out.append(Finding("host.disk", "host", DoctorStatus.DEGRADED,
                           Severity.MEDIUM, f"disk usage unreadable: {exc}"))
    return out


def check_directories(root: Path | None = None) -> list[Finding]:
    """Runtime directories and whether they are actually writable.

    Writability is tested by ``os.access``, not by creating a probe file: a
    doctor that writes to diagnose has changed the thing it is diagnosing.
    """
    root = root or Path(__file__).resolve().parent.parent
    out: list[Finding] = []
    for name in ("data", "logs"):
        path = root / name
        if not path.exists():
            out.append(Finding(
                f"dirs.{name}", "filesystem", DoctorStatus.DEGRADED, Severity.LOW,
                f"{name}/ does not exist yet",
                f"it is created on first run; mkdir {name} to pre-create it"))
        elif not os.access(path, os.W_OK):
            out.append(Finding(
                f"dirs.{name}", "filesystem", DoctorStatus.BLOCKED, Severity.HIGH,
                f"{name}/ exists but is not writable by this user",
                f"chown/chmod {path} so the JARVIS user can write it"))
        else:
            out.append(Finding(f"dirs.{name}", "filesystem", DoctorStatus.PASS,
                               Severity.INFO, f"{name}/ present and writable"))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  External services — every one of them optional
# ══════════════════════════════════════════════════════════════════════════════
def check_external_tools() -> list[Finding]:
    out: list[Finding] = []
    for check_id, binary, why in (
        ("tools.docker", "docker", "container inventory and the lab sandbox"),
        ("tools.ollama", "ollama", "local model inference"),
        ("tools.zeek", "zeek", "network DPI telemetry"),
    ):
        present = shutil.which(binary)
        out.append(Finding(
            check_id, "external",
            DoctorStatus.PASS if present else DoctorStatus.OPTIONAL_MISSING,
            Severity.INFO,
            f"{binary} found at {present}" if present
            else f"{binary} not on PATH — {why} is unavailable",
            "" if present else f"install {binary} if you want {why}"))
    return out


def check_configuration() -> list[Finding]:
    """Config sanity. Reads NOTHING secret and prints no value, only presence."""
    out: list[Finding] = []
    try:
        from core.config import settings
        out.append(Finding("config.settings", "config", DoctorStatus.PASS,
                           Severity.INFO, "core.config.Settings loaded"))
        for check_id, attr, label in (
            ("config.telegram", "telegram_bot_token", "Telegram bridge"),
            ("config.aura_token", "aura_ws_token", "AURA WebSocket auth"),
        ):
            configured = bool(getattr(settings, attr, None) or
                              os.environ.get(attr.upper(), ""))
            out.append(Finding(
                check_id, "config",
                DoctorStatus.PASS if configured else DoctorStatus.OPTIONAL_MISSING,
                Severity.INFO,
                f"{label} is configured" if configured
                else f"{label} is not configured",
                "" if configured else f"set {attr.upper()} in .env to enable {label}"))
    except Exception as exc:  # noqa: BLE001
        out.append(Finding("config.settings", "config", DoctorStatus.BLOCKED,
                           Severity.HIGH, f"settings failed to load: {exc}",
                           "check .env for a malformed value"))
    return out


def check_model_runtime() -> list[Finding]:
    """Whether a local model runtime is reachable. Never downloads anything."""
    if not shutil.which("ollama"):
        return [Finding("model.ollama", "model_runtime",
                        DoctorStatus.OPTIONAL_MISSING, Severity.INFO,
                        "ollama not installed",
                        "install Ollama for local inference")]
    try:
        import httpx
        from core.config import settings
        base = getattr(settings, "ollama_host", "") or "http://127.0.0.1:11434"
        response = httpx.get(f"{base.rstrip('/')}/api/tags", timeout=3.0)
        models = [m.get("name", "") for m in (response.json().get("models") or [])]
        return [Finding(
            "model.ollama", "model_runtime",
            DoctorStatus.PASS if models else DoctorStatus.DEGRADED,
            Severity.INFO if models else Severity.MEDIUM,
            f"ollama reachable with {len(models)} models: "
            f"{', '.join(models[:6])}" if models
            else "ollama is reachable but has no models pulled",
            "" if models else "ollama pull <model>")]
    except Exception as exc:  # noqa: BLE001 — unreachable is a finding, not a crash
        return [Finding("model.ollama", "model_runtime", DoctorStatus.DEGRADED,
                        Severity.MEDIUM,
                        f"ollama is installed but not answering: {type(exc).__name__}",
                        "start the ollama service (systemctl start ollama)")]


def check_memory_stores() -> list[Finding]:
    """The durable stores. Presence and openability only — no content is read."""
    out: list[Finding] = []
    for check_id, module, label in (
        ("memory.operational_store", "core.operational_store", "operational store"),
        ("memory.fabric", "core.memory_fabric", "memory fabric"),
    ):
        try:
            __import__(module)
            out.append(Finding(check_id, "memory", DoctorStatus.PASS,
                               Severity.INFO, f"{label} importable"))
        except Exception as exc:  # noqa: BLE001
            out.append(Finding(check_id, "memory", DoctorStatus.DEGRADED,
                               Severity.MEDIUM, f"{label} unavailable: {exc}"))
    present = _installed("chromadb")
    out.append(Finding(
        "memory.vector", "memory",
        DoctorStatus.PASS if present else DoctorStatus.OPTIONAL_MISSING,
        Severity.INFO,
        "chromadb present" if present else "chromadb not installed",
        "" if present else "pip install chromadb for vector memory"))
    return out


def check_speech() -> list[Finding]:
    out: list[Finding] = []
    for check_id, module, label in (("speech.stt", "faster_whisper", "STT"),
                                    ("speech.tts", "pyttsx3", "TTS")):
        present = _installed(module)
        out.append(Finding(
            check_id, "speech",
            DoctorStatus.PASS if present else DoctorStatus.OPTIONAL_MISSING,
            Severity.INFO,
            f"{label} backend {module} present" if present
            else f"{label} backend {module} not installed",
            "" if present else "pip install -e '.[voice]'"))
    return out


def check_aura() -> list[Finding]:
    present = _installed("fastapi") and _installed("uvicorn")
    return [Finding(
        "aura.server", "aura",
        DoctorStatus.PASS if present else DoctorStatus.OPTIONAL_MISSING,
        Severity.INFO,
        "FastAPI + uvicorn present" if present
        else "AURA server dependencies not installed",
        "" if present else "pip install -e '.[aura]' to enable the HUD")]


# ══════════════════════════════════════════════════════════════════════════════
#  The whole examination
# ══════════════════════════════════════════════════════════════════════════════
def run_diagnostics(*, include_network: bool = True) -> DoctorReport:
    """Run every check. Never raises: a check that explodes becomes a finding.

    ``include_network`` gates only the local-loopback model-runtime probe. There
    is no flag that makes this module contact a remote host, scan a range or
    discover a target — every network touch it can make is to an address the
    operator already configured.
    """
    report = DoctorReport()
    checks = [check_interpreter, check_dependencies, check_entrypoints,
              check_resources, check_directories, check_external_tools,
              check_configuration, check_memory_stores, check_speech, check_aura]
    if include_network:
        checks.append(check_model_runtime)
    for check in checks:
        try:
            report.findings.extend(check())
        except Exception as exc:  # noqa: BLE001 — a broken check is a finding
            logger.warning(f"RUNTIME_DOCTOR: check {check.__name__} failed: {exc}")
            report.findings.append(Finding(
                f"internal.{check.__name__}", "doctor", DoctorStatus.DEGRADED,
                Severity.LOW, f"check raised {type(exc).__name__}: {exc}"))
    return report


__all__ = [
    "SCHEMA_VERSION", "DoctorReport", "DoctorStatus", "Finding", "Severity",
    "check_aura", "check_configuration", "check_dependencies",
    "check_directories", "check_entrypoints", "check_external_tools",
    "check_interpreter", "check_memory_stores", "check_model_runtime",
    "check_resources", "check_speech", "run_diagnostics",
    "site_packages_versions",
]
