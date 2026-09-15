"""core/security_control_state.py — V69 M66A.1 (L4): observed security-control state.

WHY THIS EXISTS
---------------
`report["defender_active"] = True` was set unconditionally, right after a probe
that swallowed every exception with `try/except: pass` and never re-queried after
an enable attempt. A control that was never observed was reported ACTIVE. §29
requires that ACTIVE be earned by an affirmative post-observation, and that every
other outcome be a distinct, typed, non-optimistic value.

THE STATES
----------
* ACTIVE    — an affirmative observation says the control IS on.
* INACTIVE  — an affirmative observation says the control is OFF.
* UNKNOWN   — the control could not be observed (no output, unavailable command,
              timeout, malformed result, unsupported platform). NOT a synonym for
              inactive, and NEVER a synonym for active.
* ERROR     — an unexpected fault while observing.

THE RULE (§29)
--------------
    query says active            -> ACTIVE
    query says inactive          -> INACTIVE
    query unavailable/malformed  -> UNKNOWN
    observation raised           -> ERROR
    change command returned 0    -> still REQUERY; only a successful requery that
                                     observes "on" may produce ACTIVE.

`command_issued != active`. A control is ACTIVE iff `state is SecurityControlState.ACTIVE`
after an affirmative post-observation — never because a mutation subprocess exited 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class SecurityControlState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass
class SecurityControlObservation:
    """A typed, evidence-carrying report about ONE external security control."""
    control: str
    state: SecurityControlState
    source: str = ""
    reason_code: str = ""
    observed_at: str = ""
    attempted_change: bool = False
    post_change_observation: SecurityControlState | None = None

    def __post_init__(self) -> None:
        if not self.observed_at:
            self.observed_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_active(self) -> bool:
        """The ONLY sanctioned truthy check. `bool(True)` is never overloaded to
        mean "probably active" anywhere — a caller asks this and gets a fact."""
        return self.state is SecurityControlState.ACTIVE

    def to_dict(self) -> dict:
        return {
            "control": self.control,
            "state": self.state.value,
            "is_active": self.is_active,
            "source": self.source,
            "reason_code": self.reason_code,
            "observed_at": self.observed_at,
            "attempted_change": self.attempted_change,
            "post_change_observation": (
                self.post_change_observation.value
                if self.post_change_observation is not None else None
            ),
        }


# ── Constructors that encode the §29 mapping so no caller re-invents it ────────

def active(control: str, source: str, reason_code: str = "observed_on",
           *, attempted_change: bool = False) -> SecurityControlObservation:
    return SecurityControlObservation(
        control=control, state=SecurityControlState.ACTIVE, source=source,
        reason_code=reason_code, attempted_change=attempted_change,
        post_change_observation=(SecurityControlState.ACTIVE if attempted_change else None),
    )


def inactive(control: str, source: str, reason_code: str = "observed_off",
             *, attempted_change: bool = False) -> SecurityControlObservation:
    return SecurityControlObservation(
        control=control, state=SecurityControlState.INACTIVE, source=source,
        reason_code=reason_code, attempted_change=attempted_change,
        post_change_observation=(SecurityControlState.INACTIVE if attempted_change else None),
    )


def unknown(control: str, source: str, reason_code: str) -> SecurityControlObservation:
    return SecurityControlObservation(
        control=control, state=SecurityControlState.UNKNOWN, source=source,
        reason_code=reason_code,
    )


def error(control: str, source: str, reason_code: str) -> SecurityControlObservation:
    return SecurityControlObservation(
        control=control, state=SecurityControlState.ERROR, source=source,
        reason_code=reason_code,
    )
