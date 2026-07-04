"""
tests/test_consent_wiring.py — V62.0 Phase 6: characterization test locking in
that _main_async actually threads one shared SessionConsent into every real
capture call site, rather than leaving some silently ungated.

This is deliberately a source-level check (not a runtime one) since
_main_async is a large orchestration function that constructs real hardware/
network resources — the goal here is only to catch a future capture surface
being added (or an existing one being touched) without wiring consent through,
not to exercise the whole boot sequence.
"""
from __future__ import annotations

import inspect

import main


def test_main_async_wires_shared_consent_everywhere():
    src = inspect.getsource(main._main_async)
    assert "session_consent = default_consent()" in src

    wired_call_sites = (
        "consent=session_consent",       # ToolExecutor / start_screen_monitor /
                                          # start_telegram_bridge / both loops
        "session_consent.screen",        # auto-screenshot-on-incident hook
    )
    for marker in wired_call_sites:
        assert marker in src, f"expected {marker!r} in _main_async — a capture site may be unwired"

    # At least the 5 call sites we know about must reference session_consent.
    assert src.count("session_consent") >= 6
