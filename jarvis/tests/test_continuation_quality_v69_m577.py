"""tests/test_continuation_quality_v69_m577.py — V69 M57.7/.7.1 and M57.8.

Continuation:
  * deterministic intent classification (continue / more detail / expand section /
    example / summarize / shorten / language switch), most-specific-first;
  * continuation resumes from a STABLE DISPLAYED boundary and never repeats the
    previous answer; language is preserved unless explicitly overridden;
  * a topic change clears the cursor; no previous answer is refused truthfully
    with ZERO generation;
  * checkpoints are taken at structural boundaries only, never per token.

Quality governor:
  * repetition, placeholders, leaked tool JSON, reasoning markers, unclosed fences,
    filler-only answers and ignored brevity are detected deterministically;
  * a completed answer is never rewritten through a model; only a truthful status
    line is added, and blocking actions exist only before any visible content.

Pure — no model, no network.
"""
from __future__ import annotations

import asyncio

from core.continuation import (
    ContinuationIntent,
    ContinuationRefusal,
    build_directive,
    build_state,
    checkpoints_from_fragments,
    classify_continuation,
    clear_continuation,
    describe_refusal,
    evaluate,
    get_continuation,
    set_continuation,
    topic_fingerprint,
    topic_overlap,
)
from core.response_quality import (
    QualityAction,
    QualityIssue,
    close_open_fence,
    detect_language_drift,
    evaluate_answer,
    find_repetition,
    quality_counters,
    record_report,
    reset_quality_counters,
    status_note,
    suppress_duplicate,
)
from core.stream_assembler import Fragment, FragmentKind

_PREV = ("La raíz cúbica de x es x elevado a un tercio. En Python se escribe "
         "x ** (1/3). También existe math.pow(x, 1/3).")


def setup_function(_):
    clear_continuation()
    reset_quality_counters()


def teardown_function(_):
    clear_continuation()


def _state(**over):
    base = dict(turn_id=1, contract="BRIEF", terminal_state="COMPLETED",
                language="es", displayed_text=_PREV,
                question="como saco la raiz cubica de algo")
    base.update(over)
    return build_state(**base)


# ── intent classification ─────────────────────────────────────────────────────
def test_continue_intents():
    for text in ("continúa", "sigue", "continue", "go on"):
        assert classify_continuation(text)[0] is ContinuationIntent.CONTINUE


def test_more_detail_intent():
    assert classify_continuation("dame más detalles")[0] is \
        ContinuationIntent.MORE_DETAIL


def test_shorten_beats_more_detail_when_both_words_appear():
    # "hazlo más corto" contains "más" — precedence must not read it as detail.
    assert classify_continuation("hazlo más corto")[0] is ContinuationIntent.SHORTEN


def test_summarize_and_example_intents():
    assert classify_continuation("resúmelo")[0] is ContinuationIntent.SUMMARIZE
    assert classify_continuation("pon un ejemplo")[0] is ContinuationIntent.EXAMPLE


def test_expand_section_requires_an_ordinal():
    intent, ordinal = classify_continuation("explica el segundo punto")
    assert intent is ContinuationIntent.EXPAND_SECTION
    assert ordinal == 2
    # Without an ordinal it is a NEW question, not an expansion.
    assert classify_continuation("explica el protocolo TCP")[0] is not \
        ContinuationIntent.EXPAND_SECTION


def test_language_switch_intent():
    assert classify_continuation("answer in English")[0] is \
        ContinuationIntent.LANGUAGE_SWITCH


def test_ordinary_question_is_not_a_continuation():
    for text in ("explicame TCP", "que hora es", "", "hola"):
        assert classify_continuation(text)[0] is ContinuationIntent.NONE


# ── evaluation / refusal ──────────────────────────────────────────────────────
def test_continuation_without_a_previous_answer_is_refused():
    assert evaluate(ContinuationIntent.CONTINUE, None) is \
        ContinuationRefusal.NO_PREVIOUS_ANSWER
    msg = describe_refusal(ContinuationRefusal.NO_PREVIOUS_ANSWER, language="es")
    assert "No hay" in msg
    assert "previous answer" in describe_refusal(
        ContinuationRefusal.NO_PREVIOUS_ANSWER, language="en")


def test_failed_previous_turn_is_not_continuable():
    st = _state(terminal_state="FAILED", displayed_text="")
    assert st.available is False
    assert evaluate(ContinuationIntent.CONTINUE, st) is \
        ContinuationRefusal.NO_PREVIOUS_ANSWER


def test_bare_continue_is_allowed_because_it_carries_no_topic():
    assert evaluate(ContinuationIntent.CONTINUE, _state(),
                    user_message="continúa") is ContinuationRefusal.OK


def test_topic_change_invalidates_the_cursor():
    refusal = evaluate(ContinuationIntent.MORE_DETAIL, _state(),
                       user_message="dame más detalles sobre kerberos "
                                    "autenticacion tickets dominio")
    assert refusal is ContinuationRefusal.TOPIC_CHANGED


def test_same_topic_expansion_is_allowed():
    assert evaluate(ContinuationIntent.MORE_DETAIL, _state(),
                    user_message="dame más detalles sobre la raiz cubica "
                                 "python") is ContinuationRefusal.OK


def test_language_switch_never_needs_a_previous_answer():
    assert evaluate(ContinuationIntent.LANGUAGE_SWITCH, None) is \
        ContinuationRefusal.OK


def test_topic_helpers_are_deterministic():
    a = topic_fingerprint("la raiz cubica en python")
    assert a == topic_fingerprint("la raiz cubica en python")
    assert topic_overlap(a, a) == 1.0
    assert topic_overlap(a, frozenset()) == 0.0


# ── directives ────────────────────────────────────────────────────────────────
def test_directive_carries_only_displayed_text_and_forbids_repetition():
    directive = build_directive(ContinuationIntent.CONTINUE, _state())
    assert "NO repitas" in directive
    assert "TEXTO YA MOSTRADO" in directive
    assert _PREV[-50:] in directive


def test_directive_never_leaks_hidden_state_or_runtime_errors():
    directive = build_directive(ContinuationIntent.CONTINUE,
                                _state(terminal_state="TIMED_OUT"))
    low = directive.lower()
    assert "<think>" not in low and "traceback" not in low
    assert "httpx" not in low and "exception" not in low
    assert "TIMED_OUT" in directive       # the STATE is named, not the error


def test_incomplete_previous_answer_is_announced_as_resuming():
    directive = build_directive(ContinuationIntent.CONTINUE,
                                _state(terminal_state="INTERRUPTED_BY_OPERATOR"))
    assert "incompleta" in directive.lower()


def test_directive_language_follows_the_turn():
    es = build_directive(ContinuationIntent.CONTINUE, _state(), language="es")
    en = build_directive(ContinuationIntent.CONTINUE, _state(), language="en")
    assert "CONTINUACIÓN" in es and "CONTINUATION" in en


def test_expand_section_directive_names_the_section():
    st = build_state(turn_id=1, contract="TECHNICAL", terminal_state="COMPLETED",
                     language="es", displayed_text=_PREV, question="kerberos",
                     sections=["## Tickets", "## KDC", "## Realm"])
    directive = build_directive(ContinuationIntent.EXPAND_SECTION, st, ordinal=2)
    assert "KDC" in directive


def test_boundary_is_bounded():
    st = build_state(turn_id=1, contract="TECHNICAL", terminal_state="COMPLETED",
                     language="es", displayed_text="x" * 5000, question="q")
    assert len(st.last_boundary) <= 320


# ── checkpoints (M57.7.1) ─────────────────────────────────────────────────────
def test_checkpoints_only_at_structural_boundaries():
    frags = [
        Fragment(FragmentKind.PARAGRAPH, "## Tickets\n"),
        Fragment(FragmentKind.SENTENCE, "Una frase cualquiera. "),
        Fragment(FragmentKind.LIST_ITEM, "- primero\n"),
        Fragment(FragmentKind.CODE_BLOCK_BOUNDARY, "```\n"),
        Fragment(FragmentKind.CODE_LINE, "x = 1\n"),
    ]
    marks = checkpoints_from_fragments(frags)
    assert "## Tickets" in marks
    assert "- primero" in marks
    assert "<code block>" in marks
    assert not any("Una frase" in m for m in marks)


def test_checkpoints_are_bounded():
    frags = [Fragment(FragmentKind.LIST_ITEM, f"- item {i}\n") for i in range(50)]
    assert len(checkpoints_from_fragments(frags)) <= 8


def test_state_registry_is_ephemeral():
    set_continuation(_state())
    assert get_continuation() is not None
    clear_continuation()
    assert get_continuation() is None


# ── quality governor ──────────────────────────────────────────────────────────
def test_repeated_sentence_is_detected():
    text = ("Esta es una frase bastante larga y sustantiva. "
            "Esta es una frase bastante larga y sustantiva.")
    assert find_repetition(text)
    assert evaluate_answer(text).has(QualityIssue.REPEATED_SENTENCE)


def test_short_repeats_are_not_flagged():
    assert not find_repetition("Sí. Sí. Sí.")


def test_unresolved_placeholder_is_blocked():
    for text in ("Usa [inserta tu clave] aquí en el ejemplo del sistema.",
                 "Configura {{ api_key }} en el archivo de configuracion.",
                 "TODO: completar esta parte de la explicacion tecnica."):
        report = evaluate_answer(text)
        assert report.has(QualityIssue.UNRESOLVED_PLACEHOLDER), text


def test_tool_json_leak_is_blocked():
    text = '{"name": "run_command", "arguments": {"cmd": "ls"}}'
    report = evaluate_answer(text, pre_content=True)
    assert report.has(QualityIssue.TOOL_JSON_LEAK)
    assert QualityAction.BLOCK_DISPLAY in report.actions()


def test_prose_about_json_is_not_a_tool_leak():
    text = ("JSON usa pares clave-valor como en un diccionario de Python, "
            "por ejemplo un objeto con nombre y edad.")
    assert not evaluate_answer(text).has(QualityIssue.TOOL_JSON_LEAK)


def test_reasoning_markers_are_blocked():
    for text in ("<think>el usuario quiere...</think> La respuesta es 4.",
                 "[THINKING] hmm [/THINKING] La respuesta es 4.",
                 "Okay, let me think about this problem carefully."):
        assert evaluate_answer(text).has(QualityIssue.REASONING_MARKER), text


def test_unclosed_code_fence_is_repaired_not_rewritten():
    text = "Aqui tienes:\n```python\nx = 1\n"
    repaired, closed = close_open_fence(text)
    assert closed is True
    assert repaired.startswith(text)          # the words are untouched
    assert repaired.rstrip().endswith("```")
    report = evaluate_answer(text)
    assert report.has(QualityIssue.UNCLOSED_CODE_FENCE)
    assert report.repaired_text is not None


def test_balanced_fences_are_left_alone():
    text = "```python\nx = 1\n```\n"
    assert close_open_fence(text) == (text, False)


def test_truncation_at_cap_is_reported_honestly():
    report = evaluate_answer("Una respuesta que se corta", truncated_by_cap=True)
    assert report.has(QualityIssue.TRUNCATED_AT_CAP)
    assert "acortada" in status_note(report, language="es")
    assert "shortened" in status_note(report, language="en")


def test_filler_only_answer_is_flagged():
    assert evaluate_answer("Claro.").has(QualityIssue.FILLER_ONLY)
    assert evaluate_answer("ok").has(QualityIssue.FILLER_ONLY)


def test_intro_boilerplate_is_noticed_but_not_acted_on():
    report = evaluate_answer("Claro, con mucho gusto te explico esto. La raíz "
                             "cúbica es sencilla.")
    assert report.has(QualityIssue.INTRO_BOILERPLATE)
    # Noticing is not censoring: a completed answer is never rewritten.
    assert QualityAction.BLOCK_DISPLAY not in report.actions()
    assert report.repaired_text is None


def test_question_echo_is_detected():
    q = "como saco la raiz cubica de un numero en python"
    text = ("Para saber como sacar la raiz cubica de un numero en python, "
            "primero hay que entenderlo.")
    assert evaluate_answer(text, question=q).has(QualityIssue.QUESTION_ECHO)


def test_answer_first_style_is_not_flagged_as_echo():
    q = "como saco la raiz cubica de un numero en python"
    text = "x ** (1/3) devuelve la raíz cúbica. Funciona con cualquier float."
    assert not evaluate_answer(text, question=q).has(QualityIssue.QUESTION_ECHO)


def test_brevity_request_ignored_is_flagged():
    from core.model_router import ModelDecision, ModelRole
    from core.response_contract import select_contract
    from core.turn_policy import classify_request

    md = ModelDecision(role=ModelRole.FAST, provider="ollama", model="m",
                       complexity=0.1, reason="t", requires_verification=False)
    shape = select_contract("hazlo mas corto",
                            turn_policy=classify_request("hazlo mas corto"),
                            model_decision=md)
    long_answer = "palabra " * 900
    assert evaluate_answer(long_answer, shape=shape).has(QualityIssue.BREVITY_IGNORED)


def test_language_drift_detection_is_conservative():
    assert detect_language_drift("This is clearly an English answer with many "
                                 "common English words in it", "es") is True
    assert detect_language_drift("Esta es una respuesta en español con muchas "
                                 "palabras comunes", "es") is False
    assert detect_language_drift("POO", "es") is False       # too short to judge


def test_no_model_is_ever_called_by_the_governor(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the quality governor must never call a model")

    monkeypatch.setattr("core.ollama_native.chat_stream", _boom)
    evaluate_answer("Una respuesta cualquiera con contenido.", question="q")


def test_duplicate_fragment_suppression_is_pre_display_only():
    recent = ["Esta es una frase bastante larga y sustantiva."]
    assert suppress_duplicate("Esta es una frase bastante larga y sustantiva.",
                              recent) is True
    assert suppress_duplicate("Sí.", recent) is False


def test_counters_are_bounded_and_content_free():
    reset_quality_counters()
    record_report(evaluate_answer("TODO: pendiente de completar esta seccion",
                                  truncated_by_cap=True))
    counters = quality_counters()
    assert counters["evaluations"] == 1
    assert counters["placeholder_blocks"] == 1
    assert counters["truncation_notices"] == 1
    assert all(isinstance(v, int) for v in counters.values())


def test_status_note_emits_at_most_one_line():
    report = evaluate_answer("TODO pendiente", truncated_by_cap=True)
    note = status_note(report, language="es")
    assert note.count("(") <= 1


# ── live wiring: continuation refusal costs zero generation ───────────────────
class _StubExecutor:
    authority = None

    async def aexecute(self, *a, **k):
        raise AssertionError("no tool on a DIRECT_FAST turn")


def test_continue_without_a_previous_answer_never_calls_the_model(monkeypatch):
    calls = {"n": 0}

    async def _never(**kw):
        calls["n"] += 1
        yield None

    monkeypatch.setattr("core.ollama_native.chat_stream", _never)
    clear_continuation()

    async def _run():
        from core.llm import LLM
        llm = LLM(_StubExecutor())
        try:
            out = await llm.chat("continúa")
            assert "No hay" in out
        finally:
            await llm.aclose()

    asyncio.run(_run())
    assert calls["n"] == 0, "a refusal must cost zero generation"
