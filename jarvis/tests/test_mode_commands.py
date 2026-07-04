"""
tests/test_mode_commands.py — V62.0 Phase 8: AssistantMode switch command parser.

The only way the live AssistantState.mode changes is through these explicit
EN/ES phrases, recognized identically from voice and text — same design as
core.consent_commands for SessionConsent.
"""
from __future__ import annotations

import pytest

from core.ironman_mode import AssistantMode
from core.mode_commands import parse_mode_command, describe_mode


@pytest.mark.parametrize("text,expected", [
    ("focus mode", AssistantMode.FOCUS),
    ("enable focus mode", AssistantMode.FOCUS),
    ("modo enfoque", AssistantMode.FOCUS),
    ("war room mode", AssistantMode.WAR_ROOM),
    ("let's go war room", AssistantMode.WAR_ROOM),
    ("sala de guerra", AssistantMode.WAR_ROOM),
    ("presentation mode", AssistantMode.PRESENTATION),
    ("modo presentación", AssistantMode.PRESENTATION),
    ("passive mode", AssistantMode.PASSIVE),
    ("go passive", AssistantMode.PASSIVE),
    ("modo pasivo", AssistantMode.PASSIVE),
    ("active mode", AssistantMode.ACTIVE),
    ("modo activo", AssistantMode.ACTIVE),
])
def test_parse_mode_command_recognizes_explicit_phrases(text, expected):
    assert parse_mode_command(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "hello jarvis",
    "what's my focus for today",       # contains "focus" but not "focus mode"
    "tell me about the war in ukraine",  # contains "war" but not "war room"
    "can you present this to me",
])
def test_ordinary_conversation_never_triggers_mode_command(text):
    assert parse_mode_command(text) is None


def test_describe_mode_returns_nonempty_string_for_every_mode():
    for mode in AssistantMode:
        desc = describe_mode(mode)
        assert isinstance(desc, str) and desc
