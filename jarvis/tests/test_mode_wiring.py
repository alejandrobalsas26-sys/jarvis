"""
tests/test_mode_wiring.py — V62.0 Phase 8: characterization test locking in
that _main_async actually threads one shared AssistantState into every real
proactive/background-work call site, rather than leaving core.ironman_mode's
policy predicates as pure dead code with no live consumer.

Deliberately source-level (not runtime) — see tests/test_consent_wiring.py
for the same rationale: _main_async constructs real hardware/network
resources, so this only guards against a future proactive/background surface
being added without wiring state through.
"""
from __future__ import annotations

import inspect

import main


def test_main_async_wires_shared_assistant_state():
    src = inspect.getsource(main._main_async)
    assert "assistant_state = default_state()" in src

    wired_call_sites = (
        "state=assistant_state",  # start_telegram_bridge / start_hunt_scheduler /
                                   # both loops
    )
    for marker in wired_call_sites:
        assert marker in src, f"expected {marker!r} in _main_async — a proactive/background site may be unwired"

    # ToolExecutor/start_screen_monitor/loops/telegram/hunt-scheduler.
    assert src.count("assistant_state") >= 5
