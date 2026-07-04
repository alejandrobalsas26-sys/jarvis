"""
tests/test_session_manager_redaction.py — V62.0 Phase 3: crash-resume snapshot
secret redaction.

core/session_manager.py's save_session() persists the raw conversation to
disk unconditionally every turn, for crash/restart resume — unlike the
episodic-memory write path, it never called memory_router.redact_secrets(),
so a credential typed or returned mid-conversation landed in plaintext on
disk every turn until overwritten.
"""
from __future__ import annotations

import json

import core.session_manager as sm


def test_save_session_redacts_secret_in_content(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "SESSION_DIR", tmp_path)

    history = [
        {"role": "user", "content": "my api_key = sk-ABCDEFGH12345678ZXCV"},
        {"role": "assistant", "content": "Got it, noted."},
    ]
    sm.save_session(history, session_id="test")

    saved = json.loads((tmp_path / "test.json").read_text(encoding="utf-8"))
    contents = [t["content"] for t in saved["turns"]]
    assert "sk-ABCDEFGH12345678ZXCV" not in contents[0]
    assert "[REDACTED-SECRET]" in contents[0]
    assert contents[1] == "Got it, noted."


def test_save_session_preserves_none_content_for_tool_call_turns(tmp_path, monkeypatch):
    """An assistant turn with only tool_calls has content=None — must not crash."""
    monkeypatch.setattr(sm, "SESSION_DIR", tmp_path)

    history = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "result text"},
    ]
    sm.save_session(history, session_id="test2")

    saved = json.loads((tmp_path / "test2.json").read_text(encoding="utf-8"))
    assert saved["turns"][0]["content"] is None
    assert saved["turns"][1]["content"] == "result text"


def test_save_session_preserves_non_secret_content_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "SESSION_DIR", tmp_path)

    history = [{"role": "user", "content": "what time is it?"}]
    sm.save_session(history, session_id="test3")

    saved = json.loads((tmp_path / "test3.json").read_text(encoding="utf-8"))
    assert saved["turns"][0]["content"] == "what time is it?"


def test_redact_turn_does_not_mutate_original_dict():
    original = {"role": "user", "content": "api_key = sk-ABCDEFGH12345678ZXCV"}
    redacted = sm._redact_turn(original)

    assert redacted is not original
    assert "[REDACTED-SECRET]" in redacted["content"]
    # Original history list (still referenced by LLM.history) must be untouched.
    assert original["content"] == "api_key = sk-ABCDEFGH12345678ZXCV"
