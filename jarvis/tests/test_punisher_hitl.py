"""
tests/test_punisher_hitl.py — Phase 2C / F6: Punisher HITL approval gate.

Severity >= 9.0 must NOT auto-execute an offensive / state-changing response.
Detection / alerting still fire, but isolate_ip runs only after explicit
approval; approval requested / denied / granted / executed are all audited.
Lower severity is unchanged.
"""
from __future__ import annotations

import asyncio

import pytest

import core.punisher as punisher


@pytest.fixture
def wired(monkeypatch):
    """Capture broadcasts, audit log entries, and isolate_ip calls."""
    events: list = []
    audits: list = []
    isolated: list = []

    async def _broadcast(evt):
        events.append(evt)

    def _log(action, target, success, detail=""):
        audits.append((action, success))

    async def _isolate(ip, reason=""):
        isolated.append(ip)
        return True

    monkeypatch.setattr(punisher, "_log_action", _log)
    monkeypatch.setattr(punisher, "isolate_ip", _isolate)
    monkeypatch.setattr("core.mitigation._is_public_ip", lambda ip: True)
    # ensure no globally-wired approver bleeds in from another test
    monkeypatch.setattr(punisher, "_APPROVAL_HOOK", None)
    return events, audits, isolated, _broadcast


def _incident(sev=9.5):
    return {"severity_score": sev, "involved_hosts": {"203.0.113.7"}, "kill_chain_phase": "c2"}


def _actions(audits):
    return [a for a, _ in audits]


def test_severity9_requests_approval_but_does_not_execute(wired):
    events, audits, isolated, bc = wired
    asyncio.run(punisher.punisher_response(_incident(), tts=None, broadcast_fn=bc))
    # Detection / alert fired …
    assert any(e["type"] == "punisher_activated" for e in events)
    assert any(e["type"] == "punisher_approval_required" for e in events)
    # … but with NO approver wired it fails closed — nothing isolated.
    assert isolated == []
    assert "approval_requested" in _actions(audits)
    assert "approval_denied" in _actions(audits)


def test_denied_approval_blocks_execution(wired):
    events, audits, isolated, bc = wired

    async def _deny(incident, targets):
        return False

    asyncio.run(punisher.punisher_response(_incident(), None, bc, approval_fn=_deny))
    assert isolated == []
    assert "approval_denied" in _actions(audits)


def test_approved_action_executes_and_audits(wired):
    events, audits, isolated, bc = wired

    async def _approve(incident, targets):
        return True

    asyncio.run(punisher.punisher_response(_incident(), None, bc, approval_fn=_approve))
    assert isolated == ["203.0.113.7"]
    assert "approval_granted" in _actions(audits)
    assert "action_executed" in _actions(audits)


def test_lower_severity_unchanged(wired):
    events, audits, isolated, bc = wired
    asyncio.run(punisher.punisher_response(_incident(sev=5.0), None, bc))
    assert events == []           # below threshold: no activation, no approval
    assert isolated == []
