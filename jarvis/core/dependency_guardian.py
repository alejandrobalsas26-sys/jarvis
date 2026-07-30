"""
core/dependency_guardian.py — Dependency resolution & reporting (v30.0).

At boot: ensures Ollama is running, finds the best available model, checks disk
space, and REPORTS missing Python packages — before JARVIS tries to use any of them.

V69 M61.3 — HOST MUTATION IS NOW OPT-IN
---------------------------------------
This module used to install things onto the operator's machine on every boot,
without being asked: ``pip install --break-system-packages`` for eight packages, and
``winget install jqlang.jq``. Two problems with that, both real:

  * ``--break-system-packages`` exists specifically to override the guard that stops
    pip from writing into an externally-managed Python. Running it unattended at boot
    can damage a system interpreter the operator did not volunteer;
  * an assistant that silently changes the machine it runs on cannot be reasoned
    about. A missing package should be *reported* honestly, not papered over — that is
    what ``scripts/doctor.py`` and the startup diagnostic are for.

Both paths are preserved but now require an explicit opt-in:
``JARVIS_AUTO_INSTALL_DEPS=true``. The default is REPORT-ONLY: the same names are
surfaced, prefixed ``would_install:``, with the exact command the operator can run.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from loguru import logger
import psutil

from core.model_router import normalize_ollama_host

OLLAMA_HOST  = normalize_ollama_host()
MIN_DISK_GB  = 10.0

#: Opt-in flag for host mutation. Absent/false ⇒ report-only (the default).
_AUTO_INSTALL_ENV = "JARVIS_AUTO_INSTALL_DEPS"


def host_mutation_allowed() -> bool:
    """True only when the operator explicitly authorized host package installs.

    Read at call time, never cached, so a test or an operator can flip it without
    reimporting the module.
    """
    return os.environ.get(_AUTO_INSTALL_ENV, "").strip().lower() in {"1", "true", "yes"}

# Legacy ordered fallback chains — retained for backward compatibility only.
# The live path now resolves models through core.model_router.resolve_role_model
# (env override → central role config → hardware hint → installed-compatible →
# safe fallback), so these are no longer consulted for the fast/deep pair.
MODEL_FAST_CHAIN = [
    "qwen2.5:7b-instruct-q5_K_M",
    "qwen2.5:7b-instruct-q4_K_M",
    "qwen2.5:7b-instruct-q4_0",
    "qwen2.5:7b",
]
MODEL_DEEP_CHAIN = [
    "qwen2.5:14b-instruct-q4_K_M",
    "qwen2.5:14b-instruct-q4_0",
    "qwen2.5:14b",
    "qwen2.5:7b-instruct-q5_K_M",  # last resort: use fast model for deep too
]

# Missing pip packages that keep appearing as MISSING in startup diagnostic
AUTO_INSTALL_PACKAGES = [
    ("matplotlib",   "matplotlib"),
    ("serial",       "pyserial"),
    ("yara",         "yara-python"),
    ("watchdog",     "watchdog"),
    ("mmh3",         "mmh3"),
    ("paramiko",     "paramiko"),
    ("cryptography", "cryptography"),
    ("yaml",         "PyYAML"),
]


async def ensure_all(hw_profile=None) -> dict:
    """
    Run all dependency checks concurrently.
    Returns a status dict with results for each check.
    """
    results = await asyncio.gather(
        _ensure_ollama_running(),
        _check_disk_space(),
        _install_missing_packages(),
        _ensure_jq(),
        return_exceptions=True,
    )
    return {
        "ollama":   results[0],
        "disk":     results[1],
        "packages": results[2],
        "jq":       results[3],
    }


async def resolve_models(hw_profile) -> tuple[str, str]:
    """
    Resolve the concrete FAST and DEEP models through the unified precedence in
    core.model_router (explicit JARVIS_MODEL_* env override → central role config
    → hardware recommendation → installed-compatible fallback → safe fallback),
    validated against the models actually pulled in Ollama.

    Returns (model_fast, model_deep). The operator's explicit env config always
    wins; the hardware profile is advisory only. When Ollama is unreachable the
    installed set is unknown, so env/central config is honored without noise.
    """
    from core.model_router import (
        ModelRole, resolve_role_model, _model_installed,
    )

    loop = asyncio.get_running_loop()
    pulled = await loop.run_in_executor(None, _get_pulled_models)
    installed = sorted(pulled) if pulled else None  # None → skip install-checking

    hw_fast = getattr(hw_profile, "model_fast", None) if hw_profile else None
    hw_deep = getattr(hw_profile, "model_deep", None) if hw_profile else None

    fast = resolve_role_model(ModelRole.FAST, installed=installed, hw_recommendation=hw_fast)
    deep = resolve_role_model(ModelRole.DEEP, installed=installed, hw_recommendation=hw_deep)

    for role_name, chosen in (("fast", fast), ("deep", deep)):
        if installed is None:
            # Ollama unreachable — cannot verify. WARN (recoverable), not ERROR.
            logger.warning(
                f"GUARDIAN: {role_name} model → {chosen} "
                f"(Ollama unreachable — availability not verified)"
            )
        elif _model_installed(chosen, installed):
            logger.info(f"GUARDIAN: {role_name} model → {chosen}")
        else:
            logger.warning(
                f"GUARDIAN: {role_name} model → {chosen} "
                f"(NOT pulled — run: ollama pull {chosen})"
            )

    return fast, deep


def _get_pulled_models() -> set[str]:
    """Blocking — call via run_in_executor."""
    try:
        import httpx
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if r.status_code == 200:
            return {m["name"] for m in r.json().get("models", [])}
    except Exception:
        pass
    return set()


def _pick_from_chain(chain: list[str], pulled: set[str],
                     preferred: str | None) -> str | None:
    # Try preferred first
    if preferred:
        for p in pulled:
            if preferred in p or p.startswith(preferred.split(":")[0]):
                return preferred
    # Walk fallback chain
    for model in chain:
        for p in pulled:
            if model in p or p.startswith(model.split(":")[0]):
                return model
    return None


async def _ensure_ollama_running() -> str:
    """Start Ollama if not already running. Returns status string."""
    loop = asyncio.get_running_loop()

    def _check_and_start():
        # Check if ollama process exists
        for proc in psutil.process_iter(["name"]):
            try:
                if "ollama" in (proc.info["name"] or "").lower():
                    return "already_running"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Find ollama executable
        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            logger.warning("GUARDIAN: ollama not found in PATH — install from https://ollama.com")
            return "not_found"

        # Start ollama serve detached
        try:
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [ollama_exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
            logger.info("GUARDIAN: started ollama serve — waiting 3s for readiness")
            return "started"
        except Exception as e:
            logger.warning(f"GUARDIAN: could not start Ollama: {e}")
            return f"error: {e}"

    result = await loop.run_in_executor(None, _check_and_start)
    if result == "started":
        await asyncio.sleep(3)   # let Ollama bind to port
    return result


async def _check_disk_space() -> str:
    """Warn if free disk space < MIN_DISK_GB."""
    try:
        usage = shutil.disk_usage(Path.home())
        free_gb = usage.free / (1024 ** 3)
        if free_gb < MIN_DISK_GB:
            logger.warning(
                f"GUARDIAN: low disk space — {free_gb:.1f}GB free "
                f"(Ollama models need 4-8GB each)"
            )
            return f"low: {free_gb:.1f}GB"
        logger.info(f"GUARDIAN: disk space OK — {free_gb:.1f}GB free")
        return f"ok: {free_gb:.1f}GB"
    except Exception as e:
        return f"error: {e}"


def missing_packages() -> list[str]:
    """The pip names from :data:`AUTO_INSTALL_PACKAGES` that are not importable.

    Pure detection: imports nothing that is already absent beyond the probe itself,
    mutates nothing. This is what the report-only default surfaces.
    """
    missing = []
    for import_name, pip_name in AUTO_INSTALL_PACKAGES:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)
    return missing


async def _install_missing_packages() -> list[str]:
    """Report — and, ONLY with explicit opt-in, install — missing Python packages.

    Default (``JARVIS_AUTO_INSTALL_DEPS`` unset): returns ``["would_install:<name>",
    …]`` and logs the exact command the operator can run. Nothing is executed.

    With the opt-in set: installs into the CURRENT interpreter. Note that
    ``--break-system-packages`` is deliberately NOT passed any more — if the
    interpreter is externally managed, pip's own guard is the correct outcome and the
    failure is reported rather than overridden.
    """
    loop = asyncio.get_running_loop()
    missing = await loop.run_in_executor(None, missing_packages)
    if not missing:
        return []

    if not host_mutation_allowed():
        logger.warning(
            f"GUARDIAN: {len(missing)} package(s) missing — NOT installing "
            f"(host mutation is opt-in). Install them yourself with: "
            f"pip install -r requirements/soc.txt   [{_AUTO_INSTALL_ENV}=true to "
            f"let JARVIS do it]")
        return [f"would_install:{name}" for name in missing]

    def _do_install():
        installed = []
        for pip_name in missing:
            logger.info(f"GUARDIAN: installing missing package: {pip_name}")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", pip_name],
                    timeout=120, capture_output=True,
                )
                if result.returncode == 0:
                    installed.append(pip_name)
                else:
                    # Content-free: the package NAME and the exit code only. pip's
                    # stderr can echo a private path, so it is not logged.
                    logger.warning(
                        f"GUARDIAN: pip install {pip_name} failed (exit "
                        f"{result.returncode})")
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning(
                    f"GUARDIAN: pip install {pip_name} failed: {type(e).__name__}")
        return installed

    installed = await loop.run_in_executor(None, _do_install)
    if installed:
        logger.info(f"GUARDIAN: operator-authorized install: {installed}")
    return installed


async def _ensure_jq() -> str:
    """Report whether ``jq`` is present; install it via winget ONLY with the opt-in.

    ``jq`` is not a JARVIS runtime dependency — it was installed to silence a
    third-party shell hook. Installing an OS-level package unattended is exactly the
    class of host mutation M61.3 removed from the default path.
    """
    if shutil.which("jq"):
        return "already_installed"
    if not host_mutation_allowed():
        return "missing_not_installed"

    loop = asyncio.get_running_loop()

    def _install():
        winget = shutil.which("winget")
        if not winget:
            return "winget_not_found"
        try:
            result = subprocess.run(
                [winget, "install", "--silent", "jqlang.jq",
                 "--accept-source-agreements",
                 "--accept-package-agreements"],
                timeout=60,
                capture_output=True,
            )
            if result.returncode != 0:
                logger.warning(f"GUARDIAN: jq install failed (exit {result.returncode})")
                return "install_failed"
            logger.info("GUARDIAN: jq installed (operator-authorized)")
            return "installed"
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"GUARDIAN: jq install failed: {type(e).__name__}")
            return f"error: {type(e).__name__}"

    return await loop.run_in_executor(None, _install)
