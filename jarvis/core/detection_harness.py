"""
core/detection_harness.py — JARVIS V51.0 PROVING GROUND
Purple detection-validation. ON-DEMAND ONLY (never auto-runs). Drives the existing
core/adversary_emulator, subscribes to correlator events, and measures detected
techniques + MTTD. Sets SIM_ACTIVE and tags coordination events simulation=True so
auto-response (quarantine/kill/Punisher) is suppressed during a run. Results feed
coverage_reporter via LAST_RESULTS.
"""
from __future__ import annotations
import asyncio, json, logging, time
from pathlib import Path

logger = logging.getLogger("jarvis.detection_harness")

SIM_ACTIVE = False
LAST_RESULTS = {}
_RESULTS_PATH = Path("logs/detection_validation.json")

_EXPECTED = {
    "T1055": "ram_hunter/ntdll_monitor",
    "T1003": "honey_credentials",
    "T1486": "ransomware_decoy",
    "T1046": "tarpit/decoy_service",
    "T1059.001": "amsi_bridge",
    "T1048": "exfil_detector",
    "T1552": "dlp_sensor",
    "T1053": "persistence_hunter",
}
_recent = []
_WINDOW = 25.0


def _on_event(event, **_kw):
    try:
        attck = event.get("attck") or event.get("technique") or []
        if isinstance(attck, str):
            attck = [attck]
        _recent.append((time.time(), [str(a).upper() for a in attck]))
        if len(_recent) > 500:
            del _recent[:250]
    except Exception:
        pass


async def _emulate(technique: str) -> bool:
    try:
        from core import adversary_emulator as ae
    except Exception:
        return False
    for name in ("emulate", "run_technique", "execute", "run", "simulate"):
        fn = getattr(ae, name, None)
        if callable(fn):
            try:
                r = fn(technique)
                if asyncio.iscoroutine(r):
                    await r
                return True
            except Exception as e:
                logger.debug("harness: emulate via %s failed: %s", name, e)
    return False


async def run(techniques=None) -> dict:
    """Validate detection coverage. Safe: response is suppressed during the run."""
    global SIM_ACTIVE, LAST_RESULTS
    techs = techniques or list(_EXPECTED.keys())
    results = {"started": time.time(), "techniques": {}}
    SIM_ACTIVE = True
    logger.warning("DETECTION_HARNESS: validation run started (SIM_ACTIVE) — responses suppressed")
    try:
        for t in techs:
            t_up = t.upper()
            mark = time.time()
            _recent.clear()
            launched = await _emulate(t)
            detected = False; latency = None
            if launched:
                deadline = time.time() + _WINDOW
                while time.time() < deadline:
                    await asyncio.sleep(0.5)
                    for ts, attck in list(_recent):
                        if any(a.split(".")[0] == t_up.split(".")[0] or a == t_up for a in attck):
                            detected = True; latency = round(ts - mark, 2); break
                    if detected:
                        break
            results["techniques"][t_up] = {
                "expected_by": _EXPECTED.get(t_up, "?"),
                "emulator_launched": launched,
                "detected": detected,
                "mttd_seconds": latency,
            }
    finally:
        SIM_ACTIVE = False
        logger.warning("DETECTION_HARNESS: validation run complete — responses re-enabled")
    det = sum(1 for v in results["techniques"].values() if v["detected"])
    results["summary"] = {"total": len(techs), "detected": det,
                          "coverage_pct": round(100 * det / max(1, len(techs)), 1)}
    LAST_RESULTS = results
    try:
        _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.debug("harness: results write failed: %s", e)
    logger.info("DETECTION_HARNESS: %d/%d detected (%.1f%%)",
                det, len(techs), results["summary"]["coverage_pct"])
    return results


async def start(correlator=None):
    """main.py startup hook. Registers an event observer and stays resident.
    Validation is triggered manually via run() — it never auto-executes."""
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("detection_harness", _on_event)
        except Exception as e:
            logger.debug("harness: responder registration failed: %s", e)
    logger.info("DETECTION_HARNESS: ready — call run() to validate (on-demand only)")
    await asyncio.Event().wait()
