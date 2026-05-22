"""
core/severity_calibrator.py — Episodic-memory-backed adaptive severity scoring (v27.0).

Recalibrates event type severity weights hourly based on historical incident outcomes.
Techniques that appeared in CRITICAL resolved incidents gain weight; low-severity ones lose.
Falls back to default weights silently when no episodic data is available.
"""

import asyncio

from loguru import logger

_DEFAULT_WEIGHTS: dict[str, float] = {
    "etw_threat_event":          3.0,
    "sysmon_event":              2.5,
    "canary_intrusion":          2.0,
    "dpi_alert":                 2.0,
    "ebpf_alert":                2.5,
    "neutralized":               1.5,
    "deception_tripped":         4.0,
    "kernel_anomaly_detected":   4.5,
    "binary_inversion_complete": 2.0,
}

_CALIBRATED: dict[str, float] = dict(_DEFAULT_WEIGHTS)
_CALIBRATION_INTERVAL = 3600  # recalibrate every hour


async def calibrate_from_memory() -> None:
    """
    Query episodic memory for past compound_incident episodes.
    Adjust severity weights via exponential moving average based on MITRE technique outcomes.
    """
    try:
        from core.episodic_memory import query_similar_episodes
        episodes = await query_similar_episodes("compound incident CRITICAL", n_results=10)
        if not episodes:
            return

        tech_severity: dict[str, list[float]] = {}
        for ep in episodes:
            content  = ep.get("content", "")
            sev_str  = ep.get("severity", "INFO")
            sev_val  = {"CRITICAL": 1.0, "HIGH": 0.7, "INFO": 0.3}.get(sev_str, 0.5)
            for event_type in _DEFAULT_WEIGHTS:
                if event_type in content:
                    tech_severity.setdefault(event_type, []).append(sev_val)

        alpha = 0.3
        for event_type, severities in tech_severity.items():
            avg      = sum(severities) / len(severities)
            base     = _DEFAULT_WEIGHTS[event_type]
            adjusted = base * (0.8 + avg * 0.4)
            _CALIBRATED[event_type] = (
                alpha * adjusted + (1 - alpha) * _CALIBRATED[event_type]
            )

        logger.debug(
            f"SEVERITY_CALIBRATOR: recalibrated {len(tech_severity)} weights from episodic memory"
        )
    except Exception as e:
        logger.debug(f"SEVERITY_CALIBRATOR: calibration skipped: {e}")


async def start_calibration_loop(broadcast_fn) -> None:
    """Background task: recalibrate severity weights every hour."""
    while True:
        await calibrate_from_memory()
        await asyncio.sleep(_CALIBRATION_INTERVAL)


def get_event_severity_weight(event_type: str) -> float:
    """Return calibrated severity weight for an event type."""
    return _CALIBRATED.get(event_type, 0.5)
