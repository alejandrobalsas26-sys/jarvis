"""
core/telemetry_auth.py — HMAC-SHA256 telemetry event signing & verification (v27.0).

Prevents indirect prompt injection via telemetry sources: ETW, Sysmon, Falco/eBPF,
canary banners, Zeek DPI.  Each source signs its events; the broadcast pipeline verifies
before any processing reaches the LLM context window.

Key lifecycle:
  core/telemetry_keys.json  — auto-generated on first run; NEVER commit to git.
  sign_event()              — called by each telemetry source before broadcasting.
  verify_and_unwrap()       — called in broadcast() before fan-out.
  make_signed_broadcaster() — factory for per-source auto-signing broadcast wrappers.
"""

import hmac
import hashlib
import json
import secrets
from pathlib import Path

from loguru import logger

_KEYS_PATH = Path(__file__).parent / "telemetry_keys.json"

# Internal event types that are trusted and pass through unsigned.
# External telemetry types (etw_threat_event, sysmon_event, canary_intrusion, etc.)
# are NOT in this set — they must be signed or they are dropped.
_TRUSTED_INTERNAL = {
    "triage", "agentic_incident", "agentic_summary",
    "startup_diagnostic", "hardware_profile", "trust_decision",
    "task_watchdog_event", "error", "system",
    # v27.0 correlator output events
    "compound_incident", "compound_incident_resolved",
}


def _load_or_generate_keys() -> dict[str, str]:
    """Load existing keys or generate new ones. Auto-creates on first run."""
    if _KEYS_PATH.exists():
        return json.loads(_KEYS_PATH.read_text())
    keys = {
        source: secrets.token_hex(32)
        for source in (
            "etw", "sysmon", "canary", "zeek", "ebpf",
            "rf_bridge", "mitigation", "sliver", "environmental",
        )
    }
    _KEYS_PATH.write_text(json.dumps(keys, indent=2))
    logger.info(f"TELEMETRY_AUTH: generated new signing keys → {_KEYS_PATH}")
    return keys


_KEYS: dict[str, str] = _load_or_generate_keys()


def _canonical(event: dict) -> bytes:
    """Deterministic JSON serialization for HMAC computation."""
    return json.dumps(
        event, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()


def sign_event(event: dict, source: str) -> dict:
    """
    Wrap event with HMAC-SHA256 signature envelope.
    Returns the event unchanged if source has no registered key (internal events).
    """
    key = _KEYS.get(source)
    if key is None:
        return event
    sig = hmac.new(key.encode(), _canonical(event), hashlib.sha256).hexdigest()
    return {"__src": source, "__sig": sig, "__payload": event}


def verify_and_unwrap(envelope: dict) -> dict | None:
    """
    Verify HMAC and unwrap the event payload.

    Returns the inner event dict on success.
    Returns None if verification fails — caller MUST drop the event.

    Internal events (no __sig key and type in _TRUSTED_INTERNAL) pass through.
    All other unsigned events are dropped.
    """
    if "__sig" not in envelope:
        event_type = envelope.get("type", "")
        if event_type in _TRUSTED_INTERNAL:
            return envelope
        logger.warning(
            f"TELEMETRY_AUTH: unsigned event type='{event_type}' — dropped"
        )
        return None

    source = envelope.get("__src", "")
    key    = _KEYS.get(source)
    if key is None:
        logger.warning(f"TELEMETRY_AUTH: unknown source '{source}' — dropped")
        return None

    payload  = envelope.get("__payload", {})
    expected = hmac.new(
        key.encode(), _canonical(payload), hashlib.sha256
    ).hexdigest()
    received = envelope.get("__sig", "")

    if not hmac.compare_digest(expected, received):
        logger.warning(
            f"TELEMETRY_AUTH: HMAC mismatch from '{source}' — dropped (possible tampering)"
        )
        return None

    return payload


def make_signed_broadcaster(broadcast_fn, source: str):
    """
    Factory: returns a broadcast_fn wrapper that auto-signs events for a given source.

    Usage in telemetry modules:
        signed_broadcast = make_signed_broadcaster(broadcast_fn, "etw")
        await signed_broadcast({"type": "etw_threat_event", ...})
    """
    async def _signed_broadcast(event: dict) -> None:
        await broadcast_fn(sign_event(event, source))
    return _signed_broadcast
