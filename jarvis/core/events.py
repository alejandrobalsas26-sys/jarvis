"""core/events.py — Standard event envelope for all broadcast_fn payloads.

Every event in the AURA pipeline gets type, timestamp, and schema_version.
Callers can override timestamp by passing it as a keyword argument.
"""

from datetime import datetime, timezone


def make_event(event_type: str, **fields) -> dict:
    """Return a standard event dict with type, timestamp, and schema_version.

    Fields override defaults: pass timestamp=<val> to supply an external
    timestamp (e.g., from NTP-corrected forensic_now() or an ETW timestamp).
    """
    return {
        "type":           event_type,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "schema_version": "1.0",
        **fields,
    }
