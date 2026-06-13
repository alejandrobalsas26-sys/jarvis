#!/usr/bin/env python3
"""
scripts/doctor.py - JARVIS environment diagnostic (V60.0, Phase 1).

Prints a PASS / WARN / FAIL table covering the things that actually break a
fresh install. Uses only the standard library so it runs even before any
profile is installed.

Exit code:
  - Non-zero ONLY when a base-breaking check FAILs (Python version, the core
    `base` imports text mode needs, or an unwritable working dir).
  - WARNs (missing optional profiles, Ollama down, no .env) never fail the run.

Run from the jarvis/ directory:
    python scripts/doctor.py
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_JARVIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_JARVIS_DIR))

_rows: list[tuple[str, str, str]] = []
_base_failed = False


def _record(name: str, status: str, detail: str = "") -> None:
    global _base_failed
    _rows.append((name, status, detail))


def _check_python() -> None:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    _record("Python >= 3.11", PASS if ok else FAIL, platform.python_version())


def _check_platform() -> None:
    _record("Platform", PASS, f"{platform.system()} {platform.release()} ({platform.machine()})")


def _check_venv() -> None:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    _record("Virtualenv active", PASS if in_venv else WARN,
            sys.prefix if in_venv else "not in a venv (recommended: .venv)")


# (module, friendly, is_base)
_BASE_IMPORTS = [
    ("pydantic", "pydantic", True),
    ("pydantic_settings", "pydantic-settings", True),
    ("loguru", "loguru", True),
    ("httpx", "httpx", True),
    ("openai", "openai (Ollama REST)", True),
    ("psutil", "psutil", True),
    ("requests", "requests", True),
    ("yaml", "PyYAML", True),
]

_PROFILE_IMPORTS = {
    "voice": [("sounddevice", "sounddevice"), ("faster_whisper", "faster-whisper"), ("pyttsx3", "pyttsx3")],
    "soc":   [("yara", "yara-python"), ("scapy", "scapy")],
    "lab":   [("docker", "docker")],
    "docs":  [("pdfplumber", "pdfplumber"), ("docx", "python-docx")],
}


def _check_base_imports() -> None:
    global _base_failed
    for mod, friendly, _ in _BASE_IMPORTS:
        try:
            importlib.import_module(mod)
            _record(f"base import: {friendly}", PASS)
        except Exception as e:
            _base_failed = True
            _record(f"base import: {friendly}", FAIL, str(e)[:60])


def _check_profile_imports() -> None:
    for profile, mods in _PROFILE_IMPORTS.items():
        missing = []
        for mod, friendly in mods:
            try:
                importlib.import_module(mod)
            except Exception:
                missing.append(friendly)
        if not missing:
            _record(f"profile [{profile}]", PASS, "all present")
        else:
            _record(f"profile [{profile}]", WARN, f"missing: {', '.join(missing)}")


def _check_ollama() -> None:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = [m.get("name", "") for m in data.get("models", [])]
        _record("Ollama reachable", PASS, host)
        _record("Ollama models pulled", PASS if names else WARN,
                f"{len(names)} model(s)" if names else "none pulled yet")
    except (urllib.error.URLError, OSError, ValueError):
        _record("Ollama reachable", WARN, f"not reachable at {host} (start `ollama serve`)")


def _check_env_file() -> None:
    env = _JARVIS_DIR / ".env"
    example = _JARVIS_DIR / ".env.example"
    if env.exists():
        _record(".env present", PASS, str(env))
    elif example.exists():
        _record(".env present", WARN, "missing - copy .env.example to .env")
    else:
        _record(".env present", WARN, "no .env or .env.example found")


def _check_write_perms() -> None:
    global _base_failed
    probe = _JARVIS_DIR / ".doctor_write_probe.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _record("Working dir writable", PASS, str(_JARVIS_DIR))
    except Exception as e:
        _base_failed = True
        _record("Working dir writable", FAIL, str(e)[:60])


def main() -> int:
    _check_python()
    _check_platform()
    _check_venv()
    _check_base_imports()
    _check_profile_imports()
    _check_ollama()
    _check_env_file()
    _check_write_perms()

    width = max(len(n) for n, _, _ in _rows)
    print("\n  JARVIS DOCTOR\n  " + "=" * (width + 28))
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for name, status, detail in _rows:
        counts[status] += 1
        marker = {PASS: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[status]
        line = f"  {marker}  {name.ljust(width)}"
        if detail:
            line += f"  - {detail}"
        print(line)
    print("  " + "=" * (width + 28))
    print(f"  {counts[PASS]} pass | {counts[WARN]} warn | {counts[FAIL]} fail\n")

    if _base_failed:
        print("  Base environment is broken - text mode will not run.")
        print("  Fix the FAIL rows above (install requirements/base.txt).\n")
        return 1
    if counts[WARN]:
        print("  Text mode is ready. WARNs are optional features you can enable later.\n")
    else:
        print("  All systems green.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
