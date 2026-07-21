"""tests/test_context_composer_v69_m576.py — V69 M57.6/.6.1/.6.2.

Proves the bounded conversation context:

  * the composed prompt stays inside its token budget as a conversation grows;
  * security instructions, the current user message and required tool evidence are
    NEVER trimmed — a token budget may not change what is true or allowed;
  * old small talk and repeated runtime status are trimmed before real content;
  * the digest is extractive and every item carries an epistemic label; EXPLICIT
    and OBSERVED never blur, and a model-assisted item can never mint EXPLICIT nor
    overwrite something the user actually said;
  * no chain of thought, secret or unrelated tool result is ever composed in;
  * an incompatible prompt-cache identity is a DIFFERENT key.

Pure — no model, no network.
"""
from __future__ import annotations

from core.context_composer import (
    PROTECTED_LAYERS,
    ContextLayer,
    compose_context,
    context_cache_key,
    resolve_context_budget,
)
from core.conversation_digest import (
    ConversationDigest,
    DigestItem,
    Evidence,
    ItemKind,
    build_digest,
    estimate_tokens,
    merge_model_assisted,
)

_SYS = ("You are JARVIS. SECURITY: never run an effectful tool without explicit "
        "authorization. LANGUAGE DIRECTIVE: reply in Spanish.")


def _history(turns: int, *, prefix: str = "tema") -> list[dict]:
    out: list[dict] = []
    for i in range(turns):
        out.append({"role": "user", "content": f"pregunta {i} sobre {prefix} "
                                               f"con bastante texto de relleno"})
        out.append({"role": "assistant", "content": f"respuesta {i} sobre {prefix} "
                                                    + ("detalle " * 40)})
    return out


# ── budget ────────────────────────────────────────────────────────────────────
def test_composed_context_stays_within_budget():
    ctx = compose_context(system_prompt=_SYS, history=_history(40),
                          token_budget=800)
    assert ctx.estimated_total_tokens <= 800
    assert ctx.over_budget is False
    assert ctx.trimmed_items > 0


def test_context_does_not_grow_with_conversation_length():
    small = compose_context(system_prompt=_SYS, history=_history(5),
                            token_budget=800)
    large = compose_context(system_prompt=_SYS, history=_history(80),
                            token_budget=800)
    assert large.estimated_total_tokens <= 800
    assert large.estimated_total_tokens <= small.estimated_total_tokens + 800


def test_budget_is_bounded_below_the_model_context():
    from core.config import Settings
    s = Settings(response_context_tokens=8000)
    assert resolve_context_budget(settings=s, num_ctx=2048) <= 1536
    assert resolve_context_budget(settings=s, num_ctx=2048) >= 256


# ── protected layers ──────────────────────────────────────────────────────────
def test_security_instructions_are_never_trimmed():
    ctx = compose_context(system_prompt=_SYS, history=_history(80),
                          token_budget=300)
    assert "SECURITY" in ctx.messages[0]["content"]
    assert "LANGUAGE DIRECTIVE" in ctx.messages[0]["content"]


def test_current_user_message_is_never_trimmed():
    history = _history(60)
    history.append({"role": "user", "content": "esta es la pregunta actual"})
    ctx = compose_context(system_prompt=_SYS, history=history, token_budget=300)
    assert ctx.messages[-1]["content"] == "esta es la pregunta actual"
    assert ctx.messages[-1]["role"] == "user"


def test_tool_evidence_for_this_turn_is_retained():
    history = _history(40)
    history.append({"role": "user", "content": "que dijo la herramienta"})
    tool = [{"role": "tool", "tool_call_id": "t1",
             "content": "resultado critico de la herramienta"}]
    ctx = compose_context(system_prompt=_SYS, history=history, tool_evidence=tool,
                          token_budget=500)
    assert any("resultado critico" in str(m.get("content")) for m in ctx.messages)


def test_protected_layers_are_declared():
    assert ContextLayer.SYSTEM in PROTECTED_LAYERS
    assert ContextLayer.CURRENT in PROTECTED_LAYERS
    assert ContextLayer.TOOL in PROTECTED_LAYERS
    assert ContextLayer.DIGEST not in PROTECTED_LAYERS


def test_recent_thread_survives_even_a_tiny_budget():
    history = _history(30)
    history.append({"role": "user", "content": "pregunta actual"})
    ctx = compose_context(system_prompt=_SYS, history=history, token_budget=128)
    roles = [m["role"] for m in ctx.messages]
    assert roles[0] == "system" and roles[-1] == "user"
    assert len(ctx.messages) >= 2


# ── trim priority ─────────────────────────────────────────────────────────────
def test_small_talk_is_trimmed_before_real_content():
    history = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡Hola! ¿En qué puedo ayudarte?"},
        {"role": "user", "content": "gracias"},
        {"role": "assistant", "content": "De nada."},
        {"role": "user", "content": "explicame el algoritmo de dijkstra en detalle"},
        {"role": "assistant", "content": "Dijkstra calcula caminos minimos " * 30},
        {"role": "user", "content": "y ahora?"},
    ]
    ctx = compose_context(system_prompt=_SYS, history=history, token_budget=280)
    body = " ".join(str(m.get("content")) for m in ctx.messages[1:])
    assert "Dijkstra" in body
    assert "De nada." not in body


def test_repeated_runtime_status_is_trimmed_before_content():
    history = []
    for _ in range(6):
        history.append({"role": "user", "content": "sigue"})
        history.append({"role": "assistant",
                        "content": "No pude completar la respuesta dentro del "
                                   "límite de tiempo."})
    history.append({"role": "user", "content": "explicame kerberos a fondo"})
    history.append({"role": "assistant", "content": "Kerberos usa tickets " * 30})
    history.append({"role": "user", "content": "y el KDC?"})
    ctx = compose_context(system_prompt=_SYS, history=history, token_budget=260)
    body = " ".join(str(m.get("content")) for m in ctx.messages[1:])
    assert "Kerberos" in body


# ── digest: extraction and epistemic labels ───────────────────────────────────
def test_explicit_preference_is_labelled_explicit():
    history = [
        {"role": "user", "content": "de ahora en adelante hazlo corto por favor"},
        {"role": "assistant", "content": "entendido"},
    ] + _history(6)
    d = build_digest(history)
    prefs = [i for i in d.items if i.kind is ItemKind.PREFERENCE]
    assert prefs and prefs[0].evidence is Evidence.EXPLICIT


def test_recurring_topic_is_observed_not_explicit():
    d = build_digest(_history(10, prefix="kerberos"))
    topics = [i for i in d.items if i.kind is ItemKind.TOPIC]
    assert topics
    for t in topics:
        assert t.evidence is Evidence.OBSERVED, (
            "a measured topic is not something the user stated")


def test_stated_goal_is_explicit():
    history = [{"role": "user",
                "content": "estoy construyendo un runtime local de IA en python"},
               {"role": "assistant", "content": "ok"}] + _history(6)
    d = build_digest(history)
    goals = [i for i in d.items if i.kind is ItemKind.GOAL]
    assert goals and goals[0].evidence is Evidence.EXPLICIT


def test_unfinished_answer_becomes_an_observed_open_question():
    history = [
        {"role": "user", "content": "explicame kerberos con mucho detalle"},
        {"role": "assistant",
         "content": "No pude completar la respuesta dentro del límite de tiempo."},
    ] + _history(6)
    d = build_digest(history)
    opens = [i for i in d.items if i.kind is ItemKind.OPEN_QUESTION]
    assert opens and opens[0].evidence is Evidence.OBSERVED


def test_digest_render_carries_the_labels_into_the_prompt():
    d = build_digest([{"role": "user", "content": "prefiero respuestas cortas"},
                      {"role": "assistant", "content": "ok"}] + _history(6))
    text = d.render(language="es")
    assert "[EXPLICIT]" in text
    assert "EXPLICIT = lo dijo el usuario" in text
    en = d.render(language="en")
    assert "EXPLICIT = the user said it" in en


def test_digest_is_bounded():
    d = build_digest(_history(200, prefix="kerberos"), max_chars=400)
    assert len(d.render()) <= 400 + 200          # header + items, hard bounded
    assert len(d.items) <= 20


def test_digest_of_a_short_conversation_is_empty():
    assert build_digest(_history(2)).is_empty()
    assert build_digest([]).is_empty()


def test_digest_explicit_items_survive_a_tight_render_budget():
    items = tuple(
        [DigestItem(ItemKind.PREFERENCE, "prefiero respuestas cortas",
                    Evidence.EXPLICIT)]
        + [DigestItem(ItemKind.TOPIC, f"tema{i} (x3)", Evidence.OBSERVED)
           for i in range(10)]
    )
    d = ConversationDigest(items=items)
    text = d.render(max_chars=300)
    assert "prefiero respuestas cortas" in text


# ── model-assisted compaction validator ───────────────────────────────────────
def test_model_assisted_items_are_forced_to_inferred():
    base = build_digest(_history(10, prefix="kerberos"))
    merged = merge_model_assisted(base, [
        DigestItem(ItemKind.DECISION, "se eligió qwen3:8b", Evidence.EXPLICIT),
    ])
    added = [i for i in merged.items if i.kind is ItemKind.DECISION]
    assert added and added[0].evidence is Evidence.INFERRED
    assert merged.model_assisted is True


def test_model_assisted_cannot_overwrite_an_explicit_item():
    base = build_digest([{"role": "user", "content": "prefiero respuestas cortas"},
                         {"role": "assistant", "content": "ok"}] + _history(6))
    explicit_text = base.by_evidence(Evidence.EXPLICIT)[0].text
    merged = merge_model_assisted(base, [
        DigestItem(ItemKind.TOPIC, explicit_text, Evidence.INFERRED),
    ])
    assert len([i for i in merged.items if i.text == explicit_text]) == 1
    assert merged.by_evidence(Evidence.EXPLICIT)[0].text == explicit_text


def test_model_assisted_cannot_contribute_preferences_or_goals():
    base = build_digest(_history(10))
    merged = merge_model_assisted(base, [
        DigestItem(ItemKind.PREFERENCE, "el usuario odia los ejemplos",
                   Evidence.INFERRED),
        DigestItem(ItemKind.GOAL, "quiere reescribir todo", Evidence.INFERRED),
    ])
    assert merged.items == base.items, (
        "a model may not invent what the operator prefers or wants")


def test_model_assisted_merge_is_bounded():
    base = build_digest(_history(10))
    merged = merge_model_assisted(base, [
        DigestItem(ItemKind.TOPIC, f"tema inventado {i}", Evidence.INFERRED)
        for i in range(50)
    ])
    inferred = merged.by_evidence(Evidence.INFERRED)
    assert len(inferred) <= 3


def test_deterministic_digest_is_the_fallback_when_no_model_runs():
    base = build_digest(_history(10, prefix="kerberos"))
    assert merge_model_assisted(base, []) is base
    assert not base.is_empty()
    assert base.model_assisted is False


# ── what must never be composed in ────────────────────────────────────────────
def test_no_reasoning_or_secret_leaks_into_metrics():
    history = _history(20)
    history.append({"role": "user", "content": "mi api_key=sk-secreto123"})
    ctx = compose_context(system_prompt=_SYS, history=history, token_budget=400)
    blob = str(ctx.snapshot()).lower()
    assert "sk-secreto123" not in blob and "api_key" not in blob


def test_history_is_never_mutated():
    history = _history(30)
    before = [dict(m) for m in history]
    compose_context(system_prompt=_SYS, history=history, token_budget=200)
    assert history == before


def test_estimate_tokens_is_bounded_and_total():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("x" * 400) == 100


# ── cache safety (M57.6.2) ────────────────────────────────────────────────────
def _key(**over) -> str:
    base = dict(model="qwen3:8b", role="fast", transport="native", num_ctx=2048,
                system_prompt=_SYS, language="es", contract="BRIEF")
    base.update(over)
    return context_cache_key(**base)


def test_identical_inputs_produce_the_same_cache_key():
    assert _key() == _key()


def test_cache_key_changes_on_every_incompatible_dimension():
    baseline = _key()
    for field, value in (("model", "qwen3:14b"), ("role", "deep"),
                         ("transport", "openai"), ("num_ctx", 4096),
                         ("language", "en"), ("contract", "TECHNICAL"),
                         ("system_prompt", _SYS + " EXTRA POLICY")):
        assert _key(**{field: value}) != baseline, field


def test_cache_key_never_contains_the_system_prompt_verbatim():
    key = _key()
    assert "SECURITY" not in key and "JARVIS" not in key
