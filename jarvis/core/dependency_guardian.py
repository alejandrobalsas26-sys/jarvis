"""
core/dependency_guardian.py — Dependency auto-resolution (v30.0).

At boot: ensures Ollama is running, finds the best available model,
installs jq silently, checks disk space, and auto-installs missing
Python packages — all before JARVIS tries to use any of them.
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


async def _install_missing_packages() -> list[str]:
    """
    Auto-install Python packages that keep appearing as MISSING
    in the startup diagnostic. Runs pip in a subprocess — non-blocking.
    Only installs what's actually missing.
    """
    loop = asyncio.get_running_loop()

    def _do_install():
        installed = []
        for import_name, pip_name in AUTO_INSTALL_PACKAGES:
            try:
                __import__(import_name)
            except ImportError:
                logger.info(f"GUARDIAN: installing missing package: {pip_name}")
                try:
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install",
                         "--quiet", "--break-system-packages", pip_name],
                        timeout=120,
                        capture_output=True,
                    )
                    installed.append(pip_name)
                except Exception as e:
                    logger.debug(f"GUARDIAN: pip install {pip_name} failed: {e}")
        return installed

    installed = await loop.run_in_executor(None, _do_install)
    if installed:
        logger.info(f"GUARDIAN: auto-installed packages: {installed}")
    return installed


async def _ensure_jq() -> str:
    """
    Install jq via winget if missing — silences the Warp hook error
    that has appeared in every session since v20.
    """
    if shutil.which("jq"):
        return "already_installed"

    loop = asyncio.get_running_loop()

    def _install():
        winget = shutil.which("winget")
        if not winget:
            return "winget_not_found"
        try:
            subprocess.run(
                [winget, "install", "--silent", "jqlang.jq",
                 "--accept-source-agreements",
                 "--accept-package-agreements"],
                timeout=60,
                capture_output=True,
            )
            logger.info("GUARDIAN: jq installed — Warp hook errors eliminated")
            return "installed"
        except Exception as e:
            return f"error: {e}"

    return await loop.run_in_executor(None, _install)
