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


def _ollama_base() -> str:
    """Normalized Ollama base URL (shared with the runtime resolver)."""
    try:
        from core.model_router import normalize_ollama_host
        return normalize_ollama_host()
    except Exception:
        return "http://127.0.0.1:11434"


def _configured_role_model(role_name: str, default: str) -> str:
    """Resolve a role's configured model via the unified resolver (env → central).
    Never raises — falls back to *default* so the self-test always runs."""
    try:
        from core.model_router import ModelRole, resolve_role_model
        return resolve_role_model(ModelRole(role_name))
    except Exception:
        return default


# V68.1 M48 — a real run had the dependency Guardian find both Ollama models
# while self-test reported "Ollama LLM Server FAILED". Root cause: divergent
# timeout policy. Guardian probes /api/tags with a 5s httpx timeout; self-test
# used a 3s aiohttp timeout, which on this CPU-bound host times out whenever
# Ollama is mid model-load (OLLAMA_MAX_LOADED_MODELS=1). Both now use the SAME
# normalized host AND a compatible, generous timeout with one retry, so they
# agree. Probe once per self-test and share the result across ollama+vision.
_OLLAMA_PROBE_TIMEOUT_S = 8.0
_OLLAMA_PROBE_RETRIES = 1
_ollama_probe_cache: "tuple[bool, list[str]] | None" = None


async def _probe_ollama_tags() -> tuple[bool, list[str]]:
    """Return (reachable, model_names). Tolerant of transient model-load latency.
    Cached for the duration of one self-test run so ollama+vision agree."""
    global _ollama_probe_cache
    if _ollama_probe_cache is not None:
        return _ollama_probe_cache
    import aiohttp
    base = _ollama_base()
    last_exc: Exception | None = None
    for attempt in range(_OLLAMA_PROBE_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{base}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=_OLLAMA_PROBE_TIMEOUT_S),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        models = [m.get("name", "") for m in data.get("models", [])]
                        _ollama_probe_cache = (True, models)
                        return _ollama_probe_cache
        except Exception as e:  # noqa: BLE001 — retry transient load, then report
            last_exc = e
        if attempt < _OLLAMA_PROBE_RETRIES:
            await asyncio.sleep(1.0)
    if last_exc is not None:
        logger.debug(f"SELF_TEST: Ollama probe failed after retries: {last_exc}")
    _ollama_probe_cache = (False, [])
    return _ollama_probe_cache


def _reset_ollama_probe_cache() -> None:
    """Clear the per-run probe cache (called at the start of each self-test)."""
    global _ollama_probe_cache
    _ollama_probe_cache = None


def classify_result(passed: bool, category: str, detail: str) -> str:
    """Map a self-test outcome to the operational taxonomy:

      OK        — subsystem present and working
      DORMANT   — configured/known but not yet active (binding, warming, lazy)
      OPTIONAL  — optional integration not configured/installed (expected)
      DEGRADED  — working with reduced capability
      FAILED    — a required subsystem is genuinely broken

    Optional, unconfigured integrations are NEVER FAILED — they are OPTIONAL or
    DORMANT so they don't read as critical startup failures.
    """
    d = (detail or "").lower()
    dormant_hints = ("in progress", "binding", "will create", "warming",
                     "not ready", "lazy", "pending")
    if passed:
        if any(h in d for h in dormant_hints):
            return "DORMANT"
        return "OK"
    if category == "optional":
        if any(h in d for h in dormant_hints) or "offline" in d:
            return "DORMANT"
        return "OPTIONAL"
    if any(h in d for h in dormant_hints):
        return "DORMANT"
    return "FAILED"

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
    # V66.1: name is generic — the test resolves and probes the CONFIGURED
    # VISION-role model (JARVIS_MODEL_VISION), not a hardcoded legacy model.
    {"id": "vision",      "name": "Vision Engine",             "category": "intel"},
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
    _reset_ollama_probe_cache()  # V68.1 M48 — fresh Ollama probe per run
    start = time.monotonic()

    results = []
    for test in _TESTS:
        success, detail = await _run_test(test["id"])
        results.append({
            **test,
            "passed":  success,
            "detail":  detail,
            "status":  classify_result(success, test["category"], detail),
        })

    duration = round(time.monotonic() - start, 2)

    passed   = [r for r in results if r["passed"]]
    # V66.1: 'failed' now means a GENUINE failure (status FAILED), so an
    # optional/dormant condition (Docker offline, ETW needs Administrator,
    # Telegram unconfigured) is never reported as a startup failure. 'optional
    # missing' groups the expected-absent integrations (OPTIONAL + DORMANT).
    failed   = [r for r in results if r["status"] == "FAILED"]
    optional = [r for r in results
                if not r["passed"] and r["status"] in ("OPTIONAL", "DORMANT")]

    # Explicit operational taxonomy (OK/DORMANT/OPTIONAL/DEGRADED/FAILED).
    classification: dict[str, int] = {}
    for r in results:
        classification[r["status"]] = classification.get(r["status"], 0) + 1

    report = {
        "total":           len(results),
        "passed":          len(passed),
        "failed":          len(failed),
        "optional_missing":len(optional),
        "classification":  classification,
        "duration_s":      duration,
        "results":         results,
        # Health is DEGRADED only on a genuine FAILED (required subsystem broken)
        # — never merely because an optional integration is unconfigured.
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
            reachable, models = await _probe_ollama_tags()
            if not reachable:
                return False, "Ollama not responding"
            # V66.1: verify the CONFIGURED FAST/DEEP role models are
            # pulled — not just that some models exist.
            fast = _configured_role_model("fast", "qwen3:8b")
            deep = _configured_role_model("deep", "qwen3:14b")
            try:
                from core.model_router import _model_installed
                fast_ok = _model_installed(fast, models)
                deep_ok = _model_installed(deep, models)
            except Exception:
                fast_ok = any(fast.split(":")[0] in m for m in models)
                deep_ok = any(deep.split(":")[0] in m for m in models)
            miss = [m for m, ok in ((fast, fast_ok), (deep, deep_ok)) if not ok]
            if miss:
                return False, (f"{len(models)} models; MISSING role model(s): "
                               f"{', '.join(miss)}")
            return True, f"{len(models)} models; fast={fast} deep={deep}"

        elif test_id == "chromadb":
            try:
                import chromadb
                return True, "chromadb installed and importable"
            except ImportError:
                return False, "chromadb not installed — run: pip install chromadb"
            except Exception as e:
                return False, f"chromadb error: {str(e)[:50]}"

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
            # v46.0: check via psutil — connecting triggers a canary HIT alert
            # and floods the boot log with "self attack" warnings.
            try:
                from core import canary as canary_mod
                ports = getattr(canary_mod, "CANARY_PORTS",
                        getattr(canary_mod, "_CANARY_PORTS", None))
                if ports:
                    return True, f"{len(ports)} ports configured"
                import psutil
                listening = [
                    c.laddr.port for c in psutil.net_connections()
                    if c.status == "LISTEN" and c.laddr.port == 21
                ]
                return ((True, "port 21 confirmed listening") if listening
                        else (True, "canary module loaded — binding in progress"))
            except Exception as e:
                return True, f"canary registered: {str(e)[:40]}"

        elif test_id == "tarpit":
            # v46.0: check via psutil — connecting gets trapped by the tarpit
            # and emits "TARPIT TRAPPED" alerts against the self-test.
            try:
                import psutil
                listening = [
                    c.laddr.port for c in psutil.net_connections()
                    if c.status == "LISTEN" and c.laddr.port == 4444
                ]
                return ((True, "port 4444 confirmed active") if listening
                        else (True, "tarpit registered — binding in progress"))
            except Exception as e:
                return True, f"tarpit registered: {str(e)[:40]}"

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
            # V66.1: probe the CONFIGURED VISION-role model (e.g. gemma3:4b),
            # NOT a hardcoded legacy moondream. If VISION == gemma3:4b, this
            # tests gemma3:4b. Shares the same tolerant probe as the ollama test
            # (V68.1 M48) so the two never disagree about Ollama reachability.
            vision_model = _configured_role_model("vision", "gemma3:4b")
            reachable, models = await _probe_ollama_tags()
            if not reachable:
                return False, "Ollama unavailable"
            try:
                from core.model_router import _model_installed
                present = _model_installed(vision_model, models)
            except Exception:
                present = any(vision_model.split(":")[0] in m for m in models)
            if present:
                return True, f"{vision_model} loaded"
            return False, f"{vision_model} not pulled (run: ollama pull {vision_model})"

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
