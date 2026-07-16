"""
tests/test_greeting_v691.py — V69 M54.1.10/.11: deterministic greeting + routing.

The live run greeted the operator with:

    "Pues ahora mismo son [hora actual]."

The literal placeholder reached the user. The cause was the prompt (main.py:200),
which ORDERED the model to state the time and never SUPPLIED it:

    f"Saluda a {user}. Dile la hora actual y pregúntale en qué lo puedes ayudar. "

Also covers the exact live fixture "como saco la raiz cubica de algo", which
matched no marker and fell through to ORDINARY_CONVERSATION.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import host_time
from core.greeting import (
    find_placeholders,
    has_unresolved_placeholder,
    render_greeting,
    safe_user_text,
)
from core.host_time import HostTime
from core.turn_budget import budget_for
from core.turn_policy import ReasonCode, RequestClass, VerifyPolicy, classify_request


def _frozen(hour=20, minute=5, second=0) -> HostTime:
    tz = timezone(timedelta(hours=-5))
    return HostTime(datetime(2026, 7, 15, hour, minute, second, tzinfo=tz))


# -- The placeholder leak ------------------------------------------------------
def test_greeting_has_no_unresolved_placeholder_es():
    text = render_greeting(name="Alejandro", language="es", now=_frozen())
    assert find_placeholders(text) == [], f"placeholder leaked: {text!r}"
    assert "[hora actual]" not in text
    assert "20:05:00" in text, "the real host time must be rendered, not described"
    assert text.startswith("Hola, Alejandro.")


def test_greeting_has_no_unresolved_placeholder_en():
    text = render_greeting(name="Alejandro", language="en", now=_frozen())
    assert find_placeholders(text) == []
    assert text.startswith("Hello, Alejandro.")
    assert "20:05:00" in text


def test_greeting_uses_the_injected_host_clock_deterministically():
    """Time is a host fact — same clock in, same string out, no model involved."""
    a = render_greeting(name="A", language="es", now=_frozen(9, 7, 3))
    b = render_greeting(name="A", language="es", now=_frozen(9, 7, 3))
    assert a == b
    assert "09:07:03" in a


def test_greeting_can_omit_time_entirely():
    text = render_greeting(name="Alejandro", language="es", include_time=False,
                           now=_frozen())
    assert "20:05" not in text
    assert find_placeholders(text) == []


def test_greeting_carries_the_truthful_readiness_claim():
    """Preserves the M54 win: never 'All systems nominal' when semantic memory is
    degraded."""
    text = render_greeting(
        name="Alejandro", language="es", now=_frozen(),
        readiness="JARVIS está listo con memoria semántica degradada.",
    )
    assert "memoria semántica degradada" in text
    assert "Son las 20:05:00" in text


def test_greeting_reads_the_real_module_clock_when_none_injected():
    fixed = _frozen(7, 30, 0)
    host_time.set_clock(lambda: fixed.dt)
    try:
        text = render_greeting(name="A", language="es")
        assert "07:30:00" in text
    finally:
        host_time.reset_clock()


# -- Placeholder detection -----------------------------------------------------
def test_detects_every_forbidden_placeholder_form():
    for bad in ("[hora actual]", "{hora actual}", "{current_time}",
                "{{current_time}}", "<current_time>", "TODO_TIME"):
        assert has_unresolved_placeholder(f"Son las {bad}."), f"missed {bad}"


def test_markdown_links_are_not_placeholders():
    """A legitimate bracketed link must not trip the guard."""
    assert not has_unresolved_placeholder("Mira [la doc](https://x.dev) para más.")
    assert not has_unresolved_placeholder("![img](a.png)")


def test_safe_fallback_when_a_placeholder_survives():
    """Never show a broken template — substitute a safe deterministic string."""
    out = safe_user_text("Son las [hora actual].", fallback="Hola.")
    assert out == "Hola."
    assert safe_user_text("Son las 20:05.", fallback="Hola.") == "Son las 20:05."


def test_render_greeting_falls_back_rather_than_emitting_a_broken_template():
    """Even if a name somehow carried a placeholder, the user never sees it."""
    text = render_greeting(name="{user_name}", language="es", now=_frozen())
    assert find_placeholders(text) == [], f"leaked: {text!r}"


# -- M54.1.11 — the exact live fixture -----------------------------------------
def test_cube_root_question_routes_direct_fast_no_tools_no_rag_no_verifier():
    """THE live first turn. It matched no marker and fell through to
    ORDINARY_CONVERSATION; it is ordinary educational mathematics."""
    p = classify_request("como saco la raiz cubica de algo")

    assert p.request_class is RequestClass.GENERAL_EDUCATIONAL
    assert p.reason_code is ReasonCode.DIRECT_FAST
    assert p.verify_policy is VerifyPolicy.DETERMINISTIC_CHECKS_ONLY
    assert p.wants_llm_verifier() is False, "no verifier model for basic maths"
    assert p.knowledge_vault_allowed is False, "must never touch the private vault"
    assert p.security_sensitive is False

    # No private-vault tool may be offered for this turn.
    tools = [
        {"function": {"name": "query_knowledge"}},
        {"function": {"name": "get_datetime"}},
    ]
    names = {t["function"]["name"] for t in p.filter_tools(tools)}
    assert "query_knowledge" not in names
    assert budget_for(p) <= 60.0, "a simple maths turn must stay tightly bounded"


def test_accented_and_english_forms_route_the_same():
    for q in ("¿Cómo saco la raíz cúbica de 27?",
              "como calculo la raiz cubica",
              "how do I compute a cube root",
              "how to calculate a cube root"):
        p = classify_request(q)
        assert p.request_class is RequestClass.GENERAL_EDUCATIONAL, q
        assert p.knowledge_vault_allowed is False, q


def test_howto_marker_does_not_swallow_security_or_effectful_turns():
    """Precedence must hold: a how-to FRAME never promotes an offensive or
    effectful request to 'educational'."""
    p = classify_request("como ejecuto un exploit contra 10.0.0.5")
    assert p.request_class is RequestClass.CYBER_SENSITIVE, "security precedence lost"
    assert p.security_sensitive is True

    p2 = classify_request("ejecuta nmap contra el host")
    assert p2.request_class is RequestClass.EFFECTFUL_TOOL


def test_howto_marker_does_not_swallow_greetings():
    """'cómo estás' is a greeting, not a lesson — the allowlist is verb-specific."""
    p = classify_request("como estas")
    assert p.request_class is RequestClass.ORDINARY_CONVERSATION


def test_private_document_query_still_reaches_the_vault():
    """M53/M54 behavior preserved — the vault is still reachable when asked for."""
    p = classify_request("que archivos tengo en mis documentos")
    assert p.knowledge_vault_allowed is True
