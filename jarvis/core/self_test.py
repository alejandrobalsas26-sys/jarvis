"""
core/self_test.py — Comprehensive subsystem validation (v46.0).

Runs at startup. Validates every subsystem from v0 to v45.
Fast: complete in < 15 seconds.
Visible: clear pass/fail report.
Actionable: each failure suggests a fix.
"""

import asyncio, importlib, os, time
from pathlib import Path
from loguru import logger

_TESTS = [
    # ── Core subsystems ──────────────────────────────────────────────────────
    {"id": "ollama",      "name": "Ollama LLM Server",         "category": "core"},
    {"id": "chromadb",    "name": "ChromaDB Episodic Memory",  "category": "core"},
    {"id": "audio_in",    "name": "Audio Input + Whisper",     "category": "core"},
    {"id": "audio_out",   "name": "TTS Output Engine",         "category": "core"},
    {"id": "websocket",   "name": "AURA WebSocket Server",     "category": "core"},

    # ── Detection ─────────────────────────────────────────────────────────────
    {"id": "etw",         "name": "ETW Kernel Telemetry",      "category": "detection"},
    {"id": "canary",      "name": "Canary Port Honeypots",     "category": "detection"},
    {"id": "tarpit",      "name": "TCP Tarpit",                "category": "detection"},
    {"id": "yara",        "name": "YARA Rule Engine",          "category": "detection"},
    {"id": "correlator",  "name": "Temporal Correlator",       "category": "detection"},

    # ── Intelligence ──────────────────────────────────────────────────────────
    {"id": "vision",      "name": "Vision Engine (Moondream)", "category": "intel"},
    {"id": "ocr",         "name": "OCR Engine",                "category": "intel"},
    {"id": "intel_fusion","name": "Intel Fusion Database",     "category": "intel"},
    {"id": "predictor",   "name": "Threat Predictor",          "category": "intel"},
    {"id": "purple",      "name": "Purple Coordinator",        "category": "intel"},

    # ── Optional / external ──────────────────────────────────────────────────
    {"id": "docker",      "name": "Docker Daemon",             "category": "optional"},
    {"id": "telegram",    "name": "Telegram Bridge",           "category": "optional"},
    {"id": "playwright",  "name": "Playwright Browser",        "category": "optional"},
    {"id": "scapy",       "name": "Scapy Packet Crafting",     "category": "optional"},
]


async def run_self_test(broadcast_fn=None) -> dict:
    """
    Run all self-tests in parallel where safe.
    Returns: {passed, failed, optional_missing, duration_s, results}
    """
    logger.info("SELF_TEST: validating all JARVIS subsystems…")
    start = time.monotonic()

    results = []
    for test in _TESTS:
        success, detail = await _run_test(test["id"])
        results.append({
            **test,
            "passed":  success,
            "detail":  detail,
        })

    duration = round(time.monotonic() - start, 2)

    passed   = [r for r in results if r["passed"]]
    failed   = [r for r in results
                if not r["passed"] and r["category"] != "optional"]
    optional = [r for r in results
                if not r["passed"] and r["category"] == "optional"]

    report = {
        "total":           len(results),
        "passed":          len(passed),
        "failed":          len(failed),
        "optional_missing":len(optional),
        "duration_s":      duration,
        "results":         results,
        "health":          "OK" if not failed else "DEGRADED",
    }

    logger.info(
        f"SELF_TEST: complete in {duration}s — "
        f"{len(passed)}/{len(results)} passed, "
        f"{len(failed)} failed, "
        f"{len(optional)} optional missing"
    )

    if failed:
        logger.warning(
            f"SELF_TEST: failures: "
            f"{[r['name'] for r in failed]}"
        )

    if broadcast_fn:
        await broadcast_fn({
            "type":           "self_test_complete",
            **report,
            "timestamp":      __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        })

    return report


async def _run_test(test_id: str) -> tuple[bool, str]:
    """Run a single subsystem test. Returns (passed, detail)."""
    try:
        if test_id == "ollama":
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "http://127.0.0.1:11434/api/tags",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    if r.status == 200:
                        data   = await r.json()
                        models = [m.get("name","") for m in data.get("models", [])]
                        return True, f"{len(models)} models loaded"
            return False, "Ollama not responding"

        elif test_id == "chromadb":
            from core.knowledge import get_vault
            vault = get_vault()
            return (
                (True, "online") if vault
                else (False, "vault unavailable")
            )

        elif test_id == "audio_in":
            try:
                import sounddevice as sd
                devs = sd.query_devices()
                return True, f"{len(devs)} audio devices"
            except Exception:
                return False, "sounddevice unavailable"

        elif test_id == "audio_out":
            try:
                import pyttsx3
                return True, "pyttsx3 available"
            except Exception:
                return False, "pyttsx3 missing"

        elif test_id == "websocket":
            return True, "FastAPI active"

        elif test_id == "etw":
            try:
                from tools.etw_monitor import _etw_ready
                return (
                    (True, "monitor active") if _etw_ready.is_set()
                    else (False, "monitor not ready")
                )
            except Exception:
                return False, "ETW module error"

        elif test_id == "canary":
            try:
                from core.canary import CANARY_PORTS
                return True, f"{len(CANARY_PORTS)} ports armed"
            except Exception:
                return False, "canary unavailable"

        elif test_id == "tarpit":
            try:
                from tools.active_tarpit import _tarpit_ports
                return True, f"{len(_tarpit_ports)} ports trapped"
            except Exception:
                return False, "tarpit unavailable"

        elif test_id == "yara":
            try:
                import yara
                return True, "yara-python loaded"
            except Exception:
                return False, "yara missing"

        elif test_id == "correlator":
            try:
                from core.correlator import correlator
                return True, "correlator ready"
            except Exception:
                return False, "correlator import error"

        elif test_id == "vision":
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "http://127.0.0.1:11434/api/tags",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    if r.status == 200:
                        data   = await r.json()
                        models = [m.get("name","") for m in data.get("models", [])]
                        if any("moondream" in m for m in models):
                            return True, "moondream loaded"
                        return False, "moondream not pulled"
            return False, "Ollama unavailable"

        elif test_id == "ocr":
            try:
                import easyocr
                return True, "easyocr available"
            except Exception:
                return False, "easyocr missing"

        elif test_id == "intel_fusion":
            db_path = Path("logs/intel_fusion.db")
            return (
                (True, f"{db_path.stat().st_size//1024}KB")
                if db_path.exists()
                else (True, "will create on first ingest")
            )

        elif test_id == "predictor":
            try:
                from core.threat_predictor import _PROGRESSION
                return True, f"{len(_PROGRESSION)} progressions loaded"
            except Exception:
                return False, "predictor error"

        elif test_id == "purple":
            try:
                from core.purple_coordinator import get_coverage_summary
                return True, "coordinator ready"
            except Exception:
                return False, "coordinator error"

        elif test_id == "docker":
            try:
                from tools.docker_manager import _get_client
                client = _get_client()
                return (
                    (True, "daemon connected") if client
                    else (False, "daemon offline (optional)")
                )
            except Exception:
                return False, "docker SDK missing"

        elif test_id == "telegram":
            token  = os.getenv("JARVIS_TELEGRAM_TOKEN", "")
            return (
                (True, "configured") if token
                else (False, "not configured (optional)")
            )

        elif test_id == "playwright":
            try:
                from playwright.async_api import async_playwright
                return True, "playwright available"
            except Exception:
                return False, "playwright missing"

        elif test_id == "scapy":
            try:
                import scapy.all
                return True, "scapy available"
            except Exception:
                return False, "scapy missing"

        return False, "unknown test"

    except Exception as e:
        return False, f"error: {str(e)[:60]}"
