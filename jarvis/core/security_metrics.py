"""core/security_metrics.py — V69 M66A.1: bounded four-layer security counters.

WHY THIS EXISTS
---------------
The four-layer defence (L1 authority, L2 resource, L3 execution, L4 truth) makes
decisions that are invisible unless something is counted. §32 requires reason-code
metrics for the events that matter — a denied path, a blocked SSRF target, a
stripped credential, an approval-identity mismatch, an execution profile actually
used, a security status that could not be observed.

CONTRACT
--------
* Counters describe REAL events only. A no-op must never inflate a counter — every
  ``incr`` call sits on the branch that actually happened, never on a wrapper that
  runs regardless.
* This is process-local, in-memory, thread-safe and unbounded-key-safe (the key
  space is a fixed, closed vocabulary defined here — arbitrary keys are refused so
  a model-supplied string can never mint a counter).
* No payloads, no secrets, no free text. A counter is a name and an integer.

This module holds NO authority. It observes; it never gates. Reading it can never
change a decision, which is what lets every layer call it without creating a new
place a bug could grant permission.
"""
from __future__ import annotations

import threading

#: The closed vocabulary of security counters. A name not in this set is refused
#: (KeyError is swallowed to a no-op in `incr`, but `_ALLOWED` is asserted in
#: tests) so the counter namespace cannot be widened from a hot path by accident.
_ALLOWED: frozenset[str] = frozenset({
    # L2 filesystem
    "file_policy_allows",
    "file_policy_denials",
    # L2 network
    "http_target_allows",
    "http_target_denials",
    "redirect_blocks",
    "sensitive_headers_stripped",
    # L1 authority / approval
    "hitl_challenges",
    "hitl_identity_mismatch_blocks",
    # L3 execution
    "execution_profile_direct",
    "execution_profile_restricted",
    "execution_profile_sandboxed",
    "containment_control_unavailable",
    # L4 truth / status
    "status_active",
    "status_inactive",
    "status_unknown",
    "status_error",
})

_lock = threading.Lock()
_counters: dict[str, int] = {}


def incr(name: str, n: int = 1) -> None:
    """Add *n* to counter *name*. A name outside the closed vocabulary is a no-op.

    Deliberately silent on an unknown name: a metric bug must never raise on a
    security hot path and turn an observation into a failure. Tests assert the
    vocabulary directly; production tolerates drift.
    """
    if name not in _ALLOWED or n <= 0:
        return
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def snapshot() -> dict[str, int]:
    """A copy of the counters that have actually fired (zero-valued keys omitted)."""
    with _lock:
        return dict(_counters)


def get(name: str) -> int:
    with _lock:
        return _counters.get(name, 0)


def reset() -> None:
    """Test-only: clear all counters. Not called from any production path."""
    with _lock:
        _counters.clear()
