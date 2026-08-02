"""scripts/check_base_import.py — V69 M61.3: the text-mode floor must stay minimal.

``requirements/base.txt`` exists so that ``python main.py`` in text mode runs without
audio, OCR, ML, SOC drivers or lab/offensive tooling. That promise is only real if
something checks it, so this script imports the declared BASE IMPORT SURFACE and
fails loudly if any of it needs a package outside the base profile.

TWO MODES
---------
``(default)``                  import every module in the surface; non-zero on the
                               first ImportError, naming the module and the missing
                               package.
``--assert-optional-absent``   verify the optional packages are genuinely NOT
                               installed. Without this, a green run in a fat
                               environment proves nothing — it is what makes the CI
                               base-install job meaningful rather than decorative.

Read-only. No network, no service, no host mutation.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

#: The text-mode floor. Every module here MUST import with only requirements/base.txt
#: installed. Adding a module to this list is a promise; adding a heavy import to a
#: module already on it breaks the build, which is the point.
BASE_IMPORT_SURFACE = (
    # release identity & hygiene
    "core.version",
    "core.release_check",
    "core.dependency_authority",
    # configuration and managed state
    "core.config",
    "core.managed_paths",
    "core.managed_logging",
    # runtime spine
    "core.lifecycle",
    "core.boot_state",
    "core.console",
    "core.optional_service",
    "core.task_watchdog",
    "core.shutdown_manager",
    "core.runtime_health",
    # durable state & continuity (M60)
    "core.operational_store",
    "core.session_journal",
    "core.session_continuity",
    # security posture
    "core.authority",
    "core.risk_classes",
    "core.redaction_policy",
    # the orchestrator itself must be importable without running it
    "main",
)

#: Packages from the voice / docs / soc / lab profiles. If any of these imports
#: successfully, the environment is NOT a base environment and a pass is not proof.
OPTIONAL_PACKAGES = (
    # voice
    "sounddevice", "faster_whisper", "pyttsx3", "webrtcvad", "pyaudio",
    # docs / OCR
    "pdfplumber", "easyocr", "pytesseract", "pandas", "docx",
    # soc
    "yara", "scapy", "paramiko", "capstone", "asyncpg", "redis",
    # lab / offensive / GUI
    "mitmproxy", "pymetasploit3", "sliver", "pyautogui", "keyboard", "docker",
    # training / training-cuda (V69 M62 S3A) — a base install must never reach these
    "torch", "transformers", "peft", "trl", "accelerate", "bitsandbytes",
)


def _import_surface() -> list[tuple[str, str]]:
    failures = []
    for module in BASE_IMPORT_SURFACE:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 — any failure is the finding
            failures.append((module, f"{type(exc).__name__}: {exc}"))
    return failures


def _installed_optional() -> list[str]:
    present = []
    for package in OPTIONAL_PACKAGES:
        try:
            if importlib.util.find_spec(package) is not None:
                present.append(package)
        except (ImportError, ValueError):
            continue
    return present


def main() -> int:
    ap = argparse.ArgumentParser(description="M61.3 base import surface check")
    ap.add_argument("--assert-optional-absent", action="store_true",
                    help="also fail if any optional package is installed")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    failures = _import_surface()
    present = _installed_optional()

    result = {
        "surface_size": len(BASE_IMPORT_SURFACE),
        "import_failures": [{"module": m, "error": e} for m, e in failures],
        "optional_present": present,
        "verdict": "PASS" if not failures else "FAIL",
    }
    if args.assert_optional_absent and present:
        result["verdict"] = "FAIL"

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"base import surface: {len(BASE_IMPORT_SURFACE)} modules")
        for module, error in failures:
            print(f"  FAIL {module}: {error}")
        if args.assert_optional_absent:
            print(f"optional packages present: {present or 'none (correct)'}")
        print(f"VERDICT: {result['verdict']}")

    if failures:
        return 1
    if args.assert_optional_absent and present:
        print("this environment is not a base environment, so a pass proves nothing")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
