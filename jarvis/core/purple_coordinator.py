"""
core/purple_coordinator.py — Autonomous Purple Team Orchestrator (v43.0).

Runs red and blue operations simultaneously and measures:
  - Detection latency per MITRE technique (ms precision)
  - Coverage gaps (techniques that fired but were never detected)
  - False negative rate per detection subsystem
  - Mean Time to Detect (MTTD) per kill chain phase

How it works:
  1. ARES (or BAS) executes an attack technique
  2. Purple coordinator timestamps the attack
  3. Monitors correlator/ETW/Sysmon for matching detection
  4. Measures gap between attack timestamp and detection
  5. Updates coverage matrix
  6. Techniques undetected after timeout → gap → trigger detection engineer
"""

import asyncio
import time
from datetime import datetime, timezone

from loguru import logger

_DETECTION_TIMEOUT_S = 30.0

_coverage: dict[str, "CoverageRecord"] = {}
_pending:  dict[str, dict]             = {}

_ollama_client_ref = None
_model_ref         = None


class CoverageRecord:
    """Tracks detection performance for a single MITRE technique."""

    def __init__(self, technique_id: str):
        self.technique_id   = technique_id
        self.attack_count   = 0
        self.detected_count = 0
        self.gap_count      = 0
        self.latencies_ms:  list[float] = []
        self.last_attacked  = 0.0
        self.last_detected  = 0.0
        self.detection_subsystems: list[str] = []

    @property
    def detection_rate(self) -> float:
        if self.attack_count == 0:
            return 0.0
        return self.detected_count / self.attack_count

    @property
    def mean_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return sum(self.latencies_ms) / len(self.latencies_ms)

    @property
    def coverage_tier(self) -> str:
        rate = self.detection_rate
        if rate >= 0.9:
            return "COVERED"
        if rate >= 0.5:
            return "PARTIAL"
        if rate > 0.0:
            return "WEAK"
        return "GAP"

    def to_dict(self) -> dict:
        return {
            "technique":       self.technique_id,
            "attacks":         self.attack_count,
            "detected":        self.detected_count,
            "gaps":            self.gap_count,
            "detection_rate":  round(self.detection_rate * 100, 1),
            "mean_latency_ms": round(self.mean_latency_ms, 1),
            "tier":            self.coverage_tier,
            "subsystems":      self.detection_subsystems[-3:],
        }


def attach_llm(client, model: str) -> None:
    """Inject the LLM client + model used by detection_engineer drafts."""
    global _ollama_client_ref, _model_ref
    _ollama_client_ref = client
    _model_ref         = model


async def register_attack_event(
    technique_id: str,
    event_id: str,
    source: str,
    broadcast_fn,
) -> None:
    """
    Called when ARES or BAS executes a technique.
    Starts the detection timer.
    """
    if technique_id not in _coverage:
        _coverage[technique_id] = CoverageRecord(technique_id)

    rec = _coverage[technique_id]
    rec.attack_count += 1
    rec.last_attacked = time.monotonic()

    _pending[event_id] = {
        "technique":  technique_id,
        "start_time": time.monotonic(),
        "source":     source,
        "resolved":   False,
    }

    logger.info(
        f"PURPLE: attack registered — {technique_id} "
        f"from {source} [event_id={event_id}]"
    )

    asyncio.create_task(
        _watch_for_detection(event_id, technique_id, broadcast_fn)
    )


async def register_detection_event(
    technique_id: str,
    detection_subsystem: str,
    broadcast_fn,
) -> None:
    """
    Called when correlator/ETW/Sysmon fires a detection.
    Matches against pending attacks and measures latency.
    """
    now = time.monotonic()

    matched_id = None
    for event_id, pending in _pending.items():
        if (pending["technique"] == technique_id and
                not pending["resolved"] and
                (now - pending["start_time"]) < _DETECTION_TIMEOUT_S):
            matched_id = event_id
            break

    if not matched_id:
        return

    pending = _pending[matched_id]
    latency = (now - pending["start_time"]) * 1000
    pending["resolved"] = True

    rec = _coverage.get(technique_id)
    if rec:
        rec.detected_count += 1
        rec.last_detected = now
        rec.latencies_ms.append(latency)
        rec.latencies_ms = rec.latencies_ms[-50:]
        if detection_subsystem not in rec.detection_subsystems:
            rec.detection_subsystems.append(detection_subsystem)

    logger.info(
        f"PURPLE: detection confirmed — {technique_id} "
        f"latency={latency:.1f}ms via {detection_subsystem}"
    )

    try:
        await broadcast_fn({
            "type":       "purple_detection_confirmed",
            "technique":  technique_id,
            "latency_ms": round(latency, 1),
            "subsystem":  detection_subsystem,
            "tier":       rec.coverage_tier if rec else "UNKNOWN",
            "severity":   "INFO",
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass


async def _watch_for_detection(
    event_id: str,
    technique_id: str,
    broadcast_fn,
) -> None:
    """
    Wait for detection. If timeout → coverage gap.
    Triggers detection engineer to write a new Sigma rule.
    """
    await asyncio.sleep(_DETECTION_TIMEOUT_S)

    pending = _pending.get(event_id, {})
    if pending.get("resolved"):
        return

    pending["resolved"] = True
    rec = _coverage.get(technique_id)
    if rec:
        rec.gap_count += 1

    logger.warning(
        f"PURPLE: COVERAGE GAP — {technique_id} "
        f"not detected within {_DETECTION_TIMEOUT_S}s"
    )

    try:
        await broadcast_fn({
            "type":       "purple_coverage_gap",
            "technique":  technique_id,
            "timeout_s":  _DETECTION_TIMEOUT_S,
            "total_gaps": rec.gap_count if rec else 1,
            "severity":   "HIGH",
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    asyncio.create_task(
        _request_new_detection_rule(technique_id, broadcast_fn)
    )


async def _request_new_detection_rule(
    technique_id: str,
    broadcast_fn,
) -> None:
    """Notify detection engineer of the gap (auto-draft if LLM attached)."""
    if _ollama_client_ref is not None and _model_ref:
        try:
            from core.detection_engineer import draft_rule_for_gap
            asyncio.create_task(draft_rule_for_gap(
                technique_id, broadcast_fn,
                _ollama_client_ref, _model_ref,
            ))
            return
        except Exception as e:
            logger.debug(f"PURPLE: detection_engineer dispatch failed: {e}")

    try:
        await broadcast_fn({
            "type":      "detection_rule_needed",
            "technique": technique_id,
            "reason":    "coverage_gap",
            "priority":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass


def get_coverage_matrix() -> list[dict]:
    """Return full coverage matrix sorted by gap count."""
    return sorted(
        [r.to_dict() for r in _coverage.values()],
        key=lambda x: (-x["gaps"], x["detection_rate"]),
    )


def get_coverage_summary() -> dict:
    """Return high-level coverage statistics."""
    records = list(_coverage.values())
    if not records:
        return {"total": 0, "covered": 0, "gaps": 0, "mttd_ms": 0}

    covered = sum(1 for r in records if r.coverage_tier == "COVERED")
    gaps    = sum(1 for r in records if r.coverage_tier == "GAP")
    all_lat = [ms for r in records for ms in r.latencies_ms]
    mttd    = sum(all_lat) / len(all_lat) if all_lat else 0

    return {
        "total":        len(records),
        "covered":      covered,
        "partial":      sum(1 for r in records if r.coverage_tier == "PARTIAL"),
        "gaps":         gaps,
        "coverage_pct": round(covered / len(records) * 100, 1),
        "mttd_ms":      round(mttd, 1),
    }
