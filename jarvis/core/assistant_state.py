"""
core/assistant_state.py — V62.0 Phase 8: live, mutable AssistantMode holder.

core.ironman_mode.AssistantMode and its predicates (allowed_proactive_actions,
should_run_background_tasks, should_listen_continuously) were fully
implemented and unit-tested but had zero production callers — there was no
live "current mode" anywhere in the running assistant for those predicates
to be evaluated against, only the enum and the policy tables. AssistantState
is that missing piece: one shared, mutable, session-scoped mode, the same
pattern core.ironman_mode.SessionConsent already established for consent
(constructed once in main._main_async, threaded into whatever needs to read
or change it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.ironman_mode import AssistantMode


@dataclass
class AssistantState:
    """Session-scoped operating posture. Defaults to ACTIVE — the natural
    state for a running, engaged assistant session (voice + memory; sensors
    only if SessionConsent separately allows)."""
    mode: AssistantMode = AssistantMode.ACTIVE
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def set_mode(self, mode: AssistantMode) -> bool:
        """Change the live mode. Returns whether it actually changed (a
        no-op re-set of the current mode returns False) so callers know
        whether to broadcast a ModeEvent."""
        if mode == self.mode:
            return False
        self.mode = mode
        self.updated_at = datetime.now(timezone.utc)
        return True


def default_state() -> AssistantState:
    """A fresh AssistantState at the default mode (ACTIVE)."""
    return AssistantState()
