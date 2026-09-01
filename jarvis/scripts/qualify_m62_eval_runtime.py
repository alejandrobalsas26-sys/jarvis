#!/usr/bin/env python3
"""qualify_m62_eval_runtime.py — V69 M62 S4E: what this host would measure in.

WHAT THIS IS FOR
----------------
A Protocol V4 paired attempt binds a RUNTIME REPORT DIGEST into its plan hash, so the
token an operator types authorises a measurement taken under a specific, recorded stack.
This script produces that report and its digest, and nothing else: it loads no model,
attaches no adapter, generates no token and installs nothing.

WHAT GOES IN THE DIGEST, AND WHAT DELIBERATELY DOES NOT
--------------------------------------------------------
IN: the interpreter, the framework versions, the device and precision posture, the
offline posture, and the CATEGORIES of memory and disk.

OUT: available RAM bytes, free disk bytes, hostname, PID, timestamps, and any absolute
path. Those drift while nobody is looking, and a plan hash that moved between being
printed and being typed would teach an operator to paste a token without reading it —
which is the one control the token exists to be. The raw numbers are still REPORTED, as
evidence beside the digest, because an operator sizing a run needs them.

This mirrors the rule ``HardwareCapabilityReport.identity`` already applies, and for the
same recorded reason.

THE DECLARED-RUNTIME DEVIATION THIS SCRIPT EXISTS TO SURFACE
-------------------------------------------------------------
PROGRESS.md's frozen invariants name ``.venv-m62-eval-linux`` as "the runtime the
measurements of record were taken in", and say it stays IMMUTABLE. It does not say every
later evaluation must execute inside it, and on this host it can no longer execute
anything at all: it was built against ``/usr/bin/python3.13``, which no longer exists,
so its interpreter symlinks now resolve to 3.14 while its packages sit in a 3.13
site-packages directory. Reporting that as "torch is missing" would send an operator to
the wrong problem, so :func:`declared_runtime_status` measures and names it.

Nothing here repairs it. Repairing it would mean installing packages, which the frozen
invariants forbid outright.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess  # nosec B404 — fixed argv, shell=False, no interpolation
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Bumped when the report's shape changes.
RUNTIME_REPORT_VERSION = "m62.eval_runtime_report.1"

#: The packages the evaluation backend actually imports. ``peft`` is required even for an
#: arm that would not attach an adapter, so a run cannot begin, measure one arm, and only
#: then discover it cannot load the other.
EVALUATION_PACKAGES: tuple[str, ...] = ("torch", "transformers", "peft")

#: Reported, and NOT required: the evaluation path never imports them. Recorded so a
#: reader can tell an evaluation profile from a training profile at a glance.
REPORTED_PACKAGES: tuple[str, ...] = (
    "accelerate", "safetensors", "tokenizers", "numpy", "huggingface_hub", "datasets")

#: The runtime PROGRESS.md's frozen invariants name as the measurement runtime.
DECLARED_EVAL_VENV = ".venv-m62-eval-linux"

#: Size buckets. Categories, not bytes, are what enters the digest.
_RAM_BUCKETS = ((64, "AT_LEAST_64GB"), (32, "AT_LEAST_32GB"), (16, "AT_LEAST_16GB"),
                (8, "AT_LEAST_8GB"), (0, "UNDER_8GB"))
_DISK_BUCKETS = ((200, "AT_LEAST_200GB"), (100, "AT_LEAST_100GB"),
                 (50, "AT_LEAST_50GB"), (10, "AT_LEAST_10GB"), (0, "UNDER_10GB"))


def _bucket(value_gb: float, buckets) -> str:
    for floor, label in buckets:
        if value_gb >= floor:
            return label
    return "UNKNOWN"


def canonical_json(payload: object) -> str:
    """The ONE serialization this report's digest is taken over."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n"


def package_versions(interpreter: Path | None = None) -> dict:
    """Version strings, read from metadata in the interpreter that would run the eval.

    Runs in a SUBPROCESS when an interpreter is named, because importing a framework into
    this process to ask its version is a model-facing risk taken for a metadata question.
    """
    names = list(EVALUATION_PACKAGES) + list(REPORTED_PACKAGES)
    program = (
        "import json,importlib.metadata as m\n"
        f"names={names!r}\n"
        "out={}\n"
        "for n in names:\n"
        "    try: out[n]=m.version(n)\n"
        "    except Exception: out[n]=''\n"
        "print(json.dumps(out))\n")
    executable = str(interpreter) if interpreter else sys.executable
    try:
        done = subprocess.run(  # nosec B603 — fixed argv, shell=False
            [executable, "-c", program], capture_output=True, text=True,
            timeout=120, check=False)
    except Exception as exc:  # noqa: BLE001 — an unmeasured runtime reports absence
        return {n: "" for n in names} | {"_probe_error": type(exc).__name__}
    if done.returncode != 0:
        return {n: "" for n in names} | {"_probe_error": done.stderr.strip()[:200]}
    try:
        return json.loads(done.stdout.strip())
    except json.JSONDecodeError:
        return {n: "" for n in names} | {"_probe_error": "unparseable probe output"}


def interpreter_version(interpreter: Path | None = None) -> str:
    if interpreter is None:
        return platform.python_version()
    try:
        done = subprocess.run(  # nosec B603 — fixed argv, shell=False
            [str(interpreter), "-c", "import platform;print(platform.python_version())"],
            capture_output=True, text=True, timeout=60, check=False)
        return done.stdout.strip() if done.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def pip_check(interpreter: Path | None = None) -> dict:
    """``pip check``, reported as a fact rather than assumed to pass."""
    executable = str(interpreter) if interpreter else sys.executable
    try:
        done = subprocess.run(  # nosec B603 — fixed argv, shell=False
            [executable, "-m", "pip", "check"], capture_output=True, text=True,
            timeout=300, check=False)
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "ok": False, "detail": type(exc).__name__}
    return {"ran": True, "ok": done.returncode == 0,
            "detail": (done.stdout or done.stderr).strip()[:400]}


def declared_runtime_status() -> dict:
    """Whether the runtime the frozen invariants name can still execute.

    Measured, never assumed. A venv whose interpreter symlink dangles onto a removed
    system Python reports its packages as missing, which is true and misleading at the
    same time; this separates "the packages are absent" from "the interpreter is gone".
    """
    venv = REPO_ROOT / DECLARED_EVAL_VENV
    status = {"name": DECLARED_EVAL_VENV, "present": venv.is_dir(),
              "declared_python": "", "declared_interpreter": "",
              "interpreter_exists": False, "site_packages_present": False,
              "executable": False, "note": ""}
    config = venv / "pyvenv.cfg"
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                status["declared_python"] = line.split("=", 1)[-1].strip()
            if line.startswith("executable"):
                status["declared_interpreter"] = line.split("=", 1)[-1].strip()
    interpreter = status["declared_interpreter"]
    status["interpreter_exists"] = bool(interpreter) and Path(interpreter).exists()
    major_minor = ".".join(status["declared_python"].split(".")[:2])
    site = venv / "lib" / f"python{major_minor}" / "site-packages"
    status["site_packages_present"] = site.is_dir()
    status["executable"] = status["interpreter_exists"] and status["site_packages_present"]
    if status["present"] and not status["interpreter_exists"]:
        status["note"] = (
            f"the interpreter this environment was built against ({interpreter}) is no "
            f"longer installed, so its entry points resolve to whatever /usr/bin/python3 "
            f"now is and cannot see its own site-packages. The environment is UNCHANGED "
            f"and stays immutable; it is simply not executable on this host")
    return status


def host_facts() -> dict:
    """The volatile half. Reported as evidence, excluded from the digest."""
    total_gb = available_gb = 0.0
    logical = physical = 0
    try:
        import psutil  # noqa: PLC0415

        memory = psutil.virtual_memory()
        total_gb = memory.total / 1e9
        available_gb = memory.available / 1e9
        logical = psutil.cpu_count(logical=True) or 0
        physical = psutil.cpu_count(logical=False) or 0
    except Exception:  # noqa: BLE001 — an unmeasured host reports zero, never a guess
        logical = os.cpu_count() or 0
    swap_gb = 0.0
    try:
        import psutil  # noqa: PLC0415

        swap_gb = psutil.swap_memory().total / 1e9
    except Exception:  # noqa: BLE001
        pass
    free_disk_gb = 0.0
    try:
        free_disk_gb = shutil.disk_usage(REPO_ROOT).free / 1e9
    except Exception:  # noqa: BLE001
        pass
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[-1].strip()
                break
    except Exception:  # noqa: BLE001
        model = platform.processor()
    return {
        "cpu_model": model, "cpu_architecture": platform.machine(),
        "logical_cores": logical, "physical_cores": physical,
        "total_ram_gb": round(total_gb, 2), "available_ram_gb": round(available_gb, 2),
        "swap_total_gb": round(swap_gb, 2), "free_disk_gb": round(free_disk_gb, 2),
        "total_ram_category": _bucket(total_gb, _RAM_BUCKETS),
        "available_ram_category": _bucket(available_gb, _RAM_BUCKETS),
        "free_disk_category": _bucket(free_disk_gb, _DISK_BUCKETS),
    }


def model_cache_facts(cache_root: Path | None, model_id: str, revision: str) -> dict:
    """Whether the pinned weights are already local. Nothing here downloads."""
    facts = {"cache_root_named": cache_root is not None, "revision_present": False,
             "weights_present": False, "cache_bytes": 0}
    if cache_root is None:
        return facts
    snapshot = (Path(cache_root) / f"models--{model_id.replace('/', '--')}"
                / "snapshots" / revision)
    facts["revision_present"] = snapshot.is_dir()
    if facts["revision_present"]:
        facts["weights_present"] = any(snapshot.glob("*.safetensors"))
        try:
            facts["cache_bytes"] = sum(
                p.stat().st_size for p in snapshot.rglob("*") if p.is_file())
        except Exception:  # noqa: BLE001
            facts["cache_bytes"] = 0
    return facts


def build_report(*, interpreter: Path | None, cache_root: Path | None,
                 model_id: str, revision: str) -> dict:
    """The full report: a stable identity, plus the evidence that is not in it."""
    versions = package_versions(interpreter)
    python_version = interpreter_version(interpreter)
    host = host_facts()
    missing = [p for p in EVALUATION_PACKAGES if not versions.get(p)]

    # THE DIGESTED HALF. Nothing volatile, nothing host-identifying, no path.
    identity = {
        "runtime_report_version": RUNTIME_REPORT_VERSION,
        "python_version": python_version,
        "implementation": platform.python_implementation(),
        "cpu_architecture": host["cpu_architecture"],
        "logical_cores": host["logical_cores"],
        "total_ram_category": host["total_ram_category"],
        "package_versions": {p: versions.get(p, "") for p in EVALUATION_PACKAGES},
        "reported_package_versions": {p: versions.get(p, "") for p in REPORTED_PACKAGES},
        "device_policy": "cpu",
        "precision_policy": "fp32",
        "trust_remote_code": False,
        "local_files_only": True,
        "backend_id": "transformers_peft",
        "evaluation_packages_present": not missing,
    }
    report = {
        "identity": identity,
        "runtime_report_sha256": hashlib.sha256(
            canonical_json(identity).encode("utf-8")).hexdigest(),
        # THE UNDIGESTED HALF. Evidence for an operator, excluded from the plan hash so
        # a token cannot expire because a browser opened.
        "evidence": {
            "host": host,
            "pip_check": pip_check(interpreter),
            "model_cache": model_cache_facts(cache_root, model_id, revision),
            "declared_eval_runtime": declared_runtime_status(),
            "missing_evaluation_packages": missing,
            "interpreter_named": interpreter is not None,
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Body-free evaluation runtime report. Loads no model.")
    parser.add_argument("--interpreter", default="",
                        help="the python that would run the evaluation")
    parser.add_argument("--model-cache-root", default="",
                        help="reviewed cache root; probed for presence only")
    parser.add_argument("--model-id", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--revision",
                        default="c1899de289a04d12100db370d81485cdf75e47ca")
    parser.add_argument("--out", default="", help="write the report here")
    args = parser.parse_args(argv)

    report = build_report(
        interpreter=Path(args.interpreter) if args.interpreter else None,
        cache_root=Path(args.model_cache_root) if args.model_cache_root else None,
        model_id=args.model_id, revision=args.revision)
    text = canonical_json(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
