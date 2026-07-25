"""core/recovery_state.py — V69 M60.9: the bounded last-result publication seam.

Recovery, backup and diagnostics each produce ONE result per run (or per operator
command). Runtime health needs to report those results, but it must never RUN them:
building a diagnostics bundle or a backup inside a health snapshot would turn a
read-only view into an expensive side effect on a 15 W CPU.

So each producer PUBLISHES its content-free snapshot here and the health collector
READS it — the same pattern ``core.tool_loop.publish_tool_metrics`` already uses for
last-turn tool metrics. Nothing in this module computes anything; it is three bounded
dicts and their accessors.

A published snapshot is content-free by contract: the producers build them that way,
and :func:`_bounded` additionally caps size so a future producer bug cannot make the
health surface unbounded.
"""
from __future__ import annotations

_MAX_KEYS = 40
_MAX_VALUE_CHARS = 200

_recovery: dict = {}
_backup: dict = {}
_diagnostics: dict = {}


def _bounded(snapshot: dict | None) -> dict:
    """Copy at most ``_MAX_KEYS`` entries, truncating oversized scalar values."""
    out: dict = {}
    for key, value in list((snapshot or {}).items())[:_MAX_KEYS]:
        if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            value = value[:_MAX_VALUE_CHARS]
        elif isinstance(value, (list, tuple)):
            value = list(value)[:12]
        out[str(key)[:48]] = value
    return out


def publish_recovery(snapshot: dict | None) -> None:
    global _recovery
    _recovery = _bounded(snapshot)


def publish_backup(snapshot: dict | None) -> None:
    global _backup
    _backup = _bounded(snapshot)


def publish_diagnostics(snapshot: dict | None) -> None:
    global _diagnostics
    _diagnostics = _bounded(snapshot)


def last_recovery_snapshot() -> dict:
    return dict(_recovery)


def last_backup_snapshot() -> dict:
    return dict(_backup)


def last_diagnostics_snapshot() -> dict:
    return dict(_diagnostics)


def reset_recovery_state() -> None:
    """Tests / a fresh process."""
    global _recovery, _backup, _diagnostics
    _recovery, _backup, _diagnostics = {}, {}, {}
