"""
tests/test_consent_commands.py — V62.0 Phase 6: consent grant/revoke command parser.

core.ironman_mode.SessionConsent defaults every sensitive surface to OFF and
is never granted implicitly. core.consent_commands is the ONLY way an
operator turns a surface on for the session — these tests cover the parser's
false-positive resistance (ordinary conversation must never trip it) and the
grant/revoke mutation itself.
"""
from __future__ import annotations

import pytest

from core.consent_commands import parse_consent_command, apply_consent_command
from core.ironman_mode import SessionConsent


@pytest.mark.parametrize("text,expected", [
    ("enable screen access", ("screen", True)),
    ("please allow camera access", ("camera", True)),
    ("grant clipboard access", ("clipboard", True)),
    ("activa el acceso a la pantalla", ("screen", True)),
    ("habilita la cámara", ("camera", True)),
    ("disable screen access", ("screen", False)),
    ("revoke camera access", ("camera", False)),
    ("deny clipboard access", ("clipboard", False)),
    ("desactiva la pantalla", ("screen", False)),
    ("enable mic", ("microphone", True)),
    ("enable webcam", ("camera", True)),
])
def test_parse_consent_command_recognizes_explicit_phrases(text, expected):
    assert parse_consent_command(text) == expected


@pytest.mark.parametrize("text", [
    "what's on my screen right now?",
    "take a screenshot of the error",
    "my camera isn't working",
    "can you check the clipboard",
    "",
    "hello jarvis how are you",
])
def test_ordinary_conversation_never_triggers_consent_command(text):
    """False-positive guard: mentioning a surface name alone, with no
    grant/revoke verb present, must not silently flip consent."""
    assert parse_consent_command(text) is None


def test_known_limitation_grant_verb_plus_surface_word_in_unrelated_prose():
    """Documented limitation of the keyword-based (not semantic) parser: a
    sentence that happens to contain both a grant verb and a surface name
    reads as a command even without real intent. Same trade-off the existing
    core.voice_interrupt keyword matcher already accepts elsewhere."""
    assert parse_consent_command("allow me to explain how the camera driver works") == ("camera", True)


def test_ambiguous_both_grant_and_revoke_keywords_ignored():
    assert parse_consent_command("enable and then disable screen access") is None


@pytest.mark.parametrize("text,expected", [
    ("desactiva la pantalla", ("screen", False)),
    ("deshabilita la cámara", ("camera", False)),
])
def test_revoke_word_not_misread_as_grant_via_substring(text, expected):
    """Regression guard: 'activa'/'habilita' are literal substrings of their
    own revoke-side counterparts ('desactiva', 'deshabilita'). A naive
    substring check reads both as matched (ambiguous) and silently drops the
    command instead of revoking — word-boundary matching must prevent this."""
    assert parse_consent_command(text) == expected


def test_apply_consent_command_grants_and_revokes():
    consent = SessionConsent()
    assert consent.screen is False

    msg = apply_consent_command(consent, "screen", True)
    assert consent.screen is True
    assert "enabled" in msg.lower()

    msg = apply_consent_command(consent, "screen", False)
    assert consent.screen is False
    assert "disabled" in msg.lower()


def test_apply_consent_command_only_touches_targeted_surface():
    consent = SessionConsent()
    apply_consent_command(consent, "camera", True)
    assert consent.camera is True
    assert consent.screen is False
    assert consent.clipboard is False
