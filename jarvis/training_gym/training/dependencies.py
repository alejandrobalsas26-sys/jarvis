"""training_gym/training/dependencies.py — V69 M62 S3A: what is installed, passively.

THE ONE RULE
------------
Answering "is torch available?" must not load torch. ``importlib.util.find_spec`` and
``importlib.metadata.version`` answer from package metadata; ``import torch`` answers by
executing a multi-hundred-megabyte library, initialising CUDA, and — for some builds —
touching the filesystem and the GPU. A planner advertised as pure that imports the
training stack to find out whether the training stack is there is not pure.

AND THE ONE THING THIS NEVER DOES
---------------------------------
It never installs anything. There is no pip call, no ``uv``/``poetry``/``conda``
invocation, no subprocess, no package-manager shell-out, and no generated command that
suggests one as an executable next step. A missing package is *reported*, and the
documentation tells the operator the command to run themselves.

``UNKNOWN`` is a real answer. A probe that raised proved nothing, and reporting that as
``MISSING`` would let a caller conclude "this host cannot train" from a metadata glitch —
or, far worse, let a future stage treat a successful-looking ``INSTALLED`` as licence to
skip its own check.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..schemas import sha256_obj
from .config import DependencyProfile, TrainingMethod

#: Bumped when the report's shape changes. Part of the plan hash.
DEPENDENCY_REPORT_VERSION = "m62.dependency_report.1"

#: The import name for each distribution whose two names differ.
_IMPORT_NAMES: dict[str, str] = {
    "sentencepiece": "sentencepiece",
    "safetensors": "safetensors",
}

#: The minimum version each package must report for this milestone's contracts to hold.
#: Floors, not pins: see ``requirements/training.txt`` for why no upper bound is asserted.
MINIMUM_VERSIONS: dict[str, str] = {
    "torch": "2.3.0",
    "transformers": "4.44.0",
    "datasets": "2.20.0",
    "accelerate": "0.33.0",
    "peft": "0.12.0",
    "trl": "0.9.6",
    "safetensors": "0.4.3",
    "sentencepiece": "0.2.0",
    "bitsandbytes": "0.43.0",
}

#: The packages each profile declares, mirroring ``requirements/<profile>.txt``.
PROFILE_PACKAGES: dict[DependencyProfile, tuple[str, ...]] = {
    DependencyProfile.TRAINING: (
        "torch", "transformers", "datasets", "accelerate", "peft", "trl",
        "safetensors", "sentencepiece"),
    DependencyProfile.TRAINING_CUDA: (
        "torch", "transformers", "datasets", "accelerate", "peft", "trl",
        "safetensors", "sentencepiece", "bitsandbytes"),
}

#: Sanitised version strings only: digits, dots, and the usual pre/post markers. A
#: version is metadata an arbitrary package controls, and it lands in a hashed record.
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+!-]{1,64}$")


class DependencyState(str, Enum):
    INSTALLED = "installed"
    MISSING = "missing"
    VERSION_INCOMPATIBLE = "version_incompatible"
    UNKNOWN = "unknown"


def _parse_version(text: str) -> tuple[int, ...]:
    """The leading numeric release segment. ``2.3.0rc1`` -> ``(2, 3, 0)``.

    Deliberately not a full PEP 440 implementation: this is used only to compare against
    a floor, and a comparison it cannot make is reported as ``UNKNOWN`` by the caller
    rather than guessed.
    """
    parts: list[int] = []
    for chunk in str(text).split("."):
        match = re.match(r"^(\d+)", chunk)
        if match is None:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def _sanitize_version(raw: object) -> str:
    text = str(raw or "").strip()
    return text if _VERSION_RE.match(text) else ""


@dataclass(frozen=True)
class PackageAvailability:
    """One package, as metadata describes it. No path, ever."""

    package: str
    state: DependencyState
    version: str = ""
    minimum: str = ""

    def to_dict(self) -> dict:
        return {"package": self.package, "state": self.state.value,
                "version": self.version, "minimum": self.minimum}


def probe_package(package: str) -> PackageAvailability:
    """Inspect one package without importing it.

    ``find_spec`` locates the module; ``importlib.metadata.version`` reads the installed
    distribution's recorded version. Neither executes the package's code.
    """
    minimum = MINIMUM_VERSIONS.get(package, "")
    module = _IMPORT_NAMES.get(package, package.replace("-", "_"))
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError, ModuleNotFoundError):
        # A namespace collision or a broken sys.path entry proved nothing either way.
        return PackageAvailability(package, DependencyState.UNKNOWN, minimum=minimum)
    if spec is None:
        return PackageAvailability(package, DependencyState.MISSING, minimum=minimum)
    try:
        version = _sanitize_version(importlib.metadata.version(package))
    except importlib.metadata.PackageNotFoundError:
        version = ""
    except Exception:  # noqa: BLE001 — a metadata read that crashed proved nothing
        return PackageAvailability(package, DependencyState.UNKNOWN, minimum=minimum)
    if not version:
        # Importable but with no readable distribution metadata: present, unverifiable.
        return PackageAvailability(package, DependencyState.UNKNOWN, minimum=minimum)
    if minimum:
        found, floor = _parse_version(version), _parse_version(minimum)
        if found and floor and found < floor:
            return PackageAvailability(package, DependencyState.VERSION_INCOMPATIBLE,
                                       version=version, minimum=minimum)
        if not found:
            return PackageAvailability(package, DependencyState.UNKNOWN,
                                       version=version, minimum=minimum)
    return PackageAvailability(package, DependencyState.INSTALLED, version=version,
                               minimum=minimum)


@dataclass(frozen=True)
class DependencyReport:
    """Whether a future execution stage *could* run — not an attempt to make it run."""

    profile: DependencyProfile
    packages: tuple[PackageAvailability, ...]
    method_packages: tuple[str, ...] = ()
    report_version: str = DEPENDENCY_REPORT_VERSION

    def state_of(self, package: str) -> DependencyState:
        for entry in self.packages:
            if entry.package == package:
                return entry.state
        return DependencyState.UNKNOWN

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(e.package for e in self.packages
                     if e.state is DependencyState.MISSING)

    @property
    def incompatible(self) -> tuple[str, ...]:
        return tuple(e.package for e in self.packages
                     if e.state is DependencyState.VERSION_INCOMPATIBLE)

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(e.package for e in self.packages
                     if e.state is DependencyState.UNKNOWN)

    @property
    def ready(self) -> bool:
        """True only when every package the execution needs is installed and in range.

        An empty requirement is not a satisfied one. A report that was never told what
        the run needs knows nothing about this host, and answering "ready" to that
        question is how a dependency gate becomes decorative — which is exactly what
        happened to the evaluation CLI, whose three call sites all omitted the argument.
        """
        if not self.method_packages:
            return False
        return all(self.state_of(p) is DependencyState.INSTALLED
                   for p in self.method_packages)

    def blockers(self) -> tuple[str, ...]:
        """Why a future execution could not start today. Empty means it could."""
        problems: list[str] = []
        if not self.method_packages:
            return ("no execution requirement was declared, so this report proves "
                    "nothing about this host; a dependency gate asked no question "
                    "cannot be passed",)
        for package in self.method_packages:
            state = self.state_of(package)
            if state is DependencyState.MISSING:
                problems.append(
                    f"{package} is not installed; the method cannot run without it")
            elif state is DependencyState.VERSION_INCOMPATIBLE:
                entry = next(e for e in self.packages if e.package == package)
                problems.append(
                    f"{package} {entry.version} is below the {entry.minimum} floor this "
                    f"milestone's contracts were written against")
            elif state is DependencyState.UNKNOWN:
                problems.append(
                    f"{package}: availability could not be determined; an undetermined "
                    f"dependency is not a satisfied one")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "profile": self.profile.value,
            "report_version": self.report_version,
            "packages": [e.to_dict() for e in self.packages],
            "method_packages": list(self.method_packages),
            "ready": self.ready,
            "missing": list(self.missing),
            "incompatible": list(self.incompatible),
            "unknown": list(self.unknown),
            "installs_anything": False,
            "install_command_is_operator_run": True,
        }

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def install_hint(self) -> str:
        """The command an OPERATOR may choose to run. Never executed from here."""
        return (f"pip install -r requirements/base.txt "
                f"-r requirements/{self.profile.value}.txt")


def build_dependency_report(
        *, profile: DependencyProfile, method: TrainingMethod | None = None,
        required_packages: Sequence[str] = ()) -> DependencyReport:
    """Probe every package the profile declares. Imports nothing, installs nothing.

    The method's requirements are probed too, even when the chosen profile does not
    declare them: a QLoRA run planned against the CPU ``training`` profile needs
    ``bitsandbytes``, and reporting that as "not asked about" rather than "not installed"
    would hide the exact reason the run cannot happen.

    *required_packages* states the same thing for a caller that has no ``TrainingMethod``
    to hand. Evaluation is that caller: it selects a *backend*, not a training method, so
    before this existed every evaluation call site passed neither and received a report
    that was ready by construction. See
    :func:`training_gym.evaluation.backends.backend_required_packages`.
    """
    ordered: list[str] = list(method.required_packages) if method is not None else []
    for package in required_packages:
        name = str(package).strip()
        if name and name not in ordered:
            ordered.append(name)
    required = tuple(ordered)
    names = list(PROFILE_PACKAGES[profile])
    names += [p for p in required if p not in names]
    packages = tuple(probe_package(p) for p in names)
    return DependencyReport(profile=profile, packages=packages,
                            method_packages=tuple(required))


__all__ = [
    "DEPENDENCY_REPORT_VERSION", "MINIMUM_VERSIONS", "PROFILE_PACKAGES",
    "DependencyReport", "DependencyState", "PackageAvailability",
    "build_dependency_report", "probe_package",
]
