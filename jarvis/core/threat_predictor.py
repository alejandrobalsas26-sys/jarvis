"""
core/threat_predictor.py — ATT&CK-based predictive threat engine (v36.0).

When a compound incident reaches a known kill chain phase, this engine
predicts the next 1-3 likely techniques the adversary will use based on
documented ATT&CK tactic sequences.

For each prediction:
  - Heightens monitoring sensitivity for that technique family
  - Pre-arms the relevant JARVIS detection subsystem
  - Broadcasts prediction to AURA with confidence score
  - Triggers proactive canary/tarpit positioning if applicable

No ML — pure ATT&CK graph traversal. Zero latency.
"""

import asyncio
from datetime import datetime, timezone
from loguru import logger


# ── ATT&CK technique progression graph ──────────────────────────────────────
# {current_technique: [(next_technique, confidence, description)]}
# Confidence: 0.0-1.0 based on documented frequency in threat reports

_PROGRESSION: dict[str, list[tuple[str, float, str]]] = {
    # Execution → Privilege Escalation / Credential Access
    "T1059.001": [
        ("T1055.012", 0.75, "PowerShell → Process Hollowing for evasion"),
        ("T1547.001", 0.60, "PowerShell → Registry persistence"),
        ("T1003.001", 0.55, "PowerShell → LSASS dump"),
    ],
    "T1059.003": [
        ("T1082",     0.80, "CMD → System Discovery"),
        ("T1049",     0.70, "CMD → Network Connection Discovery"),
        ("T1021.002", 0.65, "CMD → SMB lateral movement"),
    ],

    # Process Injection → Credential Access / Persistence
    "T1055.012": [
        ("T1003.001", 0.85, "Process Hollow → LSASS credential dump"),
        ("T1082",     0.65, "Process Hollow → System Discovery"),
        ("T1071.001", 0.55, "Process Hollow → C2 communication"),
    ],
    "T1055.001": [
        ("T1003.001", 0.80, "DLL Injection → credential theft"),
        ("T1562.001", 0.60, "DLL Injection → disable security tools"),
    ],

    # Credential Access → Lateral Movement
    "T1003.001": [
        ("T1021.002", 0.90, "Cred dump → SMB lateral movement (PtH)"),
        ("T1021.001", 0.75, "Cred dump → RDP lateral movement"),
        ("T1550.002", 0.70, "Cred dump → Pass the Hash"),
        ("T1558.003", 0.55, "Cred dump → Kerberoasting"),
    ],
    "T1558.003": [
        ("T1021.002", 0.85, "Kerberoast → SMB lateral movement"),
        ("T1021.006", 0.65, "Kerberoast → WinRM lateral movement"),
    ],

    # Lateral Movement → Persistence / Collection
    "T1021.002": [
        ("T1547.001", 0.80, "SMB lateral → Registry persistence"),
        ("T1053.005", 0.70, "SMB lateral → Scheduled Task persistence"),
        ("T1560.001", 0.60, "SMB lateral → Data collection/archive"),
    ],
    "T1021.001": [
        ("T1547.001", 0.75, "RDP lateral → Registry persistence"),
        ("T1056.001", 0.65, "RDP lateral → Keylogger deployment"),
    ],

    # C2 → Exfiltration
    "T1071.001": [
        ("T1041",     0.80, "HTTP C2 → Exfiltration Over C2"),
        ("T1048",     0.70, "HTTP C2 → Exfiltration Alt Protocol"),
        ("T1029",     0.55, "HTTP C2 → Scheduled Transfer"),
    ],
    "T1071.004": [
        ("T1048.001", 0.85, "DNS C2 → DNS Exfiltration"),
        ("T1071.001", 0.70, "DNS C2 → switch to HTTP C2"),
    ],

    # Persistence → Evasion
    "T1547.001": [
        ("T1562.001", 0.75, "Registry persist → disable security tools"),
        ("T1070.004", 0.65, "Registry persist → log cleanup"),
    ],
}

# Technique → detection subsystem to heighten
_TECHNIQUE_SUBSYSTEM: dict[str, str] = {
    "T1003.001": "etw",       # LSASS → ETW Kernel-Process
    "T1021.002": "canary",    # SMB → canary port 445
    "T1021.001": "canary",    # RDP → canary port 3389
    "T1071.001": "zeek",      # HTTP C2 → Zeek DPI
    "T1071.004": "zeek",      # DNS C2 → Zeek DNS
    "T1547.001": "sysmon",    # Registry → Sysmon EID 13
    "T1053.005": "sysmon",    # Sched Task → Sysmon EID 1
    "T1562.001": "etw",       # Disable security → ETW
    "T1055.012": "etw",       # Process hollow → ETW
    "T1041":     "zeek",      # Exfil → Zeek
}

# Active predictions cache {incident_id: [predictions]}
_active_predictions: dict[str, list[dict]] = {}


def predict_next_techniques(
    current_techniques: list[str],
    kill_chain_phase: str,
) -> list[dict]:
    """
    Given observed techniques, predict next likely moves.
    Returns sorted list of predictions by confidence.
    """
    predictions: dict[str, dict] = {}

    for technique in current_techniques:
        for next_tech, confidence, description in \
                _PROGRESSION.get(technique, []):
            if next_tech not in predictions or \
               predictions[next_tech]["confidence"] < confidence:
                predictions[next_tech] = {
                    "technique":    next_tech,
                    "confidence":   confidence,
                    "description":  description,
                    "triggered_by": technique,
                    "subsystem":    _TECHNIQUE_SUBSYSTEM.get(next_tech, "general"),
                }

    # Sort by confidence descending; top 3
    sorted_preds = sorted(
        predictions.values(),
        key=lambda x: x["confidence"],
        reverse=True,
    )[:3]

    return sorted_preds


async def analyze_and_predict(
    incident: dict,
    broadcast_fn,
) -> list[dict]:
    """
    Analyze a compound incident and predict next adversary moves.
    Pre-arms relevant detection subsystems.
    """
    inc_id     = incident.get("incident_id", "?")
    techniques = incident.get("mitre_techniques", []) or []
    phase      = incident.get("kill_chain_phase", "")

    if not techniques:
        return []

    predictions = predict_next_techniques(techniques, phase)
    if not predictions:
        return []

    _active_predictions[inc_id] = predictions

    # Broadcast predictions to AURA
    try:
        await broadcast_fn({
            "type":          "threat_prediction",
            "incident_id":   inc_id,
            "current_phase": phase,
            "predictions":   predictions,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "severity":      "HIGH" if predictions[0]["confidence"] > 0.7 else "MEDIUM",
        })
    except Exception as e:
        logger.debug(f"PREDICTOR: broadcast failed: {e}")

    # Log predictions
    for pred in predictions:
        logger.warning(
            f"PREDICTOR: {inc_id} → likely next: {pred['technique']} "
            f"({pred['confidence']*100:.0f}%) — {pred['description']}"
        )

    # Pre-arm detection subsystems for top prediction
    top = predictions[0]
    await _prearm_subsystem(top["subsystem"], top["technique"], broadcast_fn)

    return predictions


async def _prearm_subsystem(
    subsystem: str,
    technique: str,
    broadcast_fn,
) -> None:
    """Notify the relevant detection subsystem to heighten sensitivity."""
    logger.info(
        f"PREDICTOR: pre-arming {subsystem} for predicted {technique}"
    )
    try:
        await broadcast_fn({
            "type":      "subsystem_prearmed",
            "subsystem": subsystem,
            "technique": technique,
            "reason":    "predictive pre-arm",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.debug(f"PREDICTOR: prearm broadcast failed: {e}")


def get_active_predictions() -> dict:
    return {k: list(v) for k, v in _active_predictions.items()}
