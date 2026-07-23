"""tests/test_progressive_tts_v69_m574.py — V69 M57.4/.4.1: progressive speech.

Proves the pure speech planner:

  * only complete, intelligible prose units are spoken — never a partial sentence,
    never a token, never a code line, never raw Markdown or a URL;
  * the operator's own answer outranks background narration, so
    ``cancel_boot_narration()`` can no longer silence it;
  * the speech backlog is bounded: when TTS falls behind, intermediate speech is
    dropped while the conclusion and the runtime status survive — and the TEXT is
    never altered to accommodate it;
  * duplicate sentences are not spoken twice and over-long sentences are split at
    clause boundaries so barge-in stays responsive.

Pure — no engine, no thread, no asyncio.
"""
from __future__ import annotations

from core.response_contract import SpeechPolicy
from core.speech_stream import (
    SpeechPlanner,
    build_planner,
    speakable_text,
    split_long_utterance,
)
from core.stream_assembler import Fragment, FragmentKind, StreamAssembler
from core.tts_queue import TTSGovernor, TTSPriority


def _frag(text: str, kind: FragmentKind = FragmentKind.SENTENCE,
          in_code: bool = False) -> Fragment:
    return Fragment(kind=kind, text=text, in_code=in_code)


def _planner(**kw) -> SpeechPlanner:
    kw.setdefault("backlog_cap", 4)
    return SpeechPlanner(**kw)


# ── eligibility ───────────────────────────────────────────────────────────────
def test_completed_sentence_is_queued():
    p = _planner()
    out = p.plan(_frag("La raíz cúbica de x es x elevado a un tercio. "))
    assert len(out) == 1
    assert out[0].text.startswith("La raíz cúbica")
    assert out[0].priority is TTSPriority.HIGH


def test_partial_sentence_is_never_queued_because_it_is_never_a_fragment():
    # The assembler is what guarantees this: a boundary is not confirmed until
    # whitespace follows the terminator.
    asm = StreamAssembler()
    assert asm.push("La raíz cúbi", now=0.0) == []
    p = _planner()
    assert p.plan(_frag("")) == []


def test_code_is_not_spoken_by_default():
    p = _planner()
    assert p.plan(_frag("    return x ** (1/3)\n", FragmentKind.CODE_LINE,
                        in_code=True)) == []
    assert p.plan(_frag("```python\n", FragmentKind.CODE_BLOCK_BOUNDARY)) == []
    assert p.snapshot()["code_skipped"] == 2


def test_markdown_syntax_is_never_spoken_literally():
    p = _planner()
    out = p.plan(_frag("## Conceptos **clave** con `codigo` aquí. ",
                       FragmentKind.PARAGRAPH))
    assert out
    spoken = out[0].text
    assert "#" not in spoken and "**" not in spoken and "`" not in spoken
    assert "Conceptos" in spoken and "clave" in spoken


def test_urls_are_removed_from_speech():
    assert "http" not in speakable_text("Mira https://ejemplo.com/guia para más.")
    assert "Mira" in speakable_text("Mira https://ejemplo.com/guia para más.")


def test_punctuation_only_fragment_is_not_spoken():
    p = _planner()
    assert p.plan(_frag("--- \n", FragmentKind.PARAGRAPH)) == []
    assert p.plan(_frag("   ")) == []


def test_silent_policy_speaks_nothing():
    p = _planner(policy=SpeechPolicy.SILENT)
    assert p.plan(_frag("Una frase perfectamente hablable. ")) == []


def test_speak_lead_stops_after_the_leading_sentences():
    p = _planner(policy=SpeechPolicy.SPEAK_LEAD)
    for i in range(6):
        p.plan(_frag(f"Frase numero {i} con contenido suficiente. "))
    assert p.snapshot()["queued"] == 3


def test_speak_lead_still_speaks_the_final_status():
    p = _planner(policy=SpeechPolicy.SPEAK_LEAD)
    for i in range(6):
        p.plan(_frag(f"Frase numero {i} con contenido suficiente. "))
    out = p.plan(_frag("Respuesta interrumpida.", FragmentKind.FINAL_STATUS))
    assert out and out[0].priority is TTSPriority.CRITICAL


# ── mute ──────────────────────────────────────────────────────────────────────
def test_muted_planner_speaks_nothing_including_status():
    p = _planner(muted=True)
    assert p.plan(_frag("Una frase hablable. ")) == []
    assert p.plan(_frag("Estado final.", FragmentKind.FINAL_STATUS)) == []
    assert p.snapshot()["muted_skipped"] == 2


def test_disabled_progressive_tts_speaks_nothing():
    p = _planner(enabled=False)
    assert p.plan(_frag("Una frase hablable. ")) == []
    assert p.snapshot()["progressive_enabled"] is False


# ── priority: the answer outranks background narration ────────────────────────
def test_answer_speech_survives_cancel_boot_narration():
    q = TTSGovernor()
    p = _planner()
    for instr in p.plan(_frag("La respuesta del operador es esta. ")):
        q.put(instr.text, priority=instr.priority, key=instr.coalesce_key)
    q.put("narración de arranque", priority=TTSPriority.LOW, key="boot:1")
    removed = q.cancel_below(TTSPriority.HIGH)
    assert removed == 1
    assert len(q) == 1
    assert "operador" in q.pop().text


def test_background_narration_never_precedes_the_active_answer():
    q = TTSGovernor()
    q.put("narración de arranque", priority=TTSPriority.LOW, key="boot:1")
    p = _planner()
    for instr in p.plan(_frag("La respuesta del operador es esta. ")):
        q.put(instr.text, priority=instr.priority, key=instr.coalesce_key)
    assert "operador" in q.pop().text


# ── backpressure (M57.4.1) ────────────────────────────────────────────────────
def test_backlog_drops_intermediate_speech_but_never_the_conclusion():
    p = _planner(backlog_cap=2)
    assert p.plan(_frag("Frase intermedia con longitud suficiente. "),
                  pending=5) == []
    assert p.snapshot()["stale_dropped"] == 1
    out = p.plan(_frag("La conclusion importante del analisis. "), pending=5,
                 final=True)
    assert out and out[0].priority is TTSPriority.CRITICAL


def test_backlog_never_drops_the_final_status():
    p = _planner(backlog_cap=1)
    out = p.plan(_frag("Respuesta incompleta.", FragmentKind.FINAL_STATUS),
                 pending=99)
    assert out and out[0].priority is TTSPriority.CRITICAL


def test_high_watermark_is_tracked():
    p = _planner(backlog_cap=8)
    p.plan(_frag("Una frase con longitud suficiente aqui. "), pending=3)
    p.note_depth(7)
    p.note_depth(2)
    snap = p.snapshot()
    assert snap["high_watermark"] >= 7
    assert snap["current_depth"] == 2


def test_speech_backpressure_does_not_alter_the_text():
    # The planner returns speech instructions only; it has no way to touch the
    # rendered answer. This asserts the API shape that makes that true.
    p = _planner(backlog_cap=1)
    frag = _frag("Frase intermedia con longitud suficiente. ")
    p.plan(frag, pending=99)
    assert frag.text == "Frase intermedia con longitud suficiente. "


# ── duplicates and splitting ──────────────────────────────────────────────────
def test_duplicate_sentence_is_not_spoken_twice():
    p = _planner()
    assert p.plan(_frag("Exactamente la misma frase larga. "))
    assert p.plan(_frag("Exactamente la misma frase larga. ")) == []
    assert p.snapshot()["suppressed_duplicates"] == 1


def test_long_sentence_is_split_at_clause_boundaries():
    long_text = ("primera clausula bastante larga, " * 12).strip()
    parts = split_long_utterance(long_text, limit=120)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 120


def test_long_sentence_split_never_cuts_a_word():
    parts = split_long_utterance("palabra " * 100, limit=60)
    for part in parts:
        assert not part.endswith("palab")
        assert len(part) <= 60


def test_single_giant_word_is_still_returned_whole():
    parts = split_long_utterance("x" * 500, limit=100)
    assert parts == ["x" * 500], "never cut inside a token"


def test_split_is_counted():
    p = _planner()
    p.plan(_frag(("una clausula suficientemente larga, " * 12) + ". "))
    assert p.snapshot()["split_utterances"] == 1


# ── coalescing keys are turn-scoped ───────────────────────────────────────────
def test_coalesce_keys_are_scoped_to_the_turn():
    a = _planner(turn_id=7)
    b = _planner(turn_id=8)
    ka = a.plan(_frag("Primera frase de este turno. "))[0].coalesce_key
    kb = b.plan(_frag("Primera frase de este turno. "))[0].coalesce_key
    assert ka.startswith("answer:7:") and kb.startswith("answer:8:")
    assert ka != kb


def test_keys_are_unique_within_a_turn_so_sentences_never_coalesce_away():
    p = _planner()
    keys = []
    for i in range(5):
        for instr in p.plan(_frag(f"Frase distinta numero {i} con longitud. ")):
            keys.append(instr.coalesce_key)
    assert len(set(keys)) == len(keys) == 5


# ── build from config/contract ────────────────────────────────────────────────
def test_build_planner_inherits_the_contract_speech_policy():
    from core.model_router import ModelDecision, ModelRole
    from core.response_contract import select_contract
    from core.turn_policy import classify_request

    md = ModelDecision(role=ModelRole.FAST, provider="ollama", model="m",
                       complexity=0.1, reason="t", requires_verification=False)
    shape = select_contract("explica Kerberos con mas detalle",
                            turn_policy=classify_request(
                                "explica Kerberos con mas detalle"),
                            model_decision=md)
    p = build_planner(shape=shape, turn_id=1)
    assert p.policy is SpeechPolicy.SPEAK_LEAD


def test_build_planner_honours_operator_config():
    from core.config import Settings
    s = Settings(response_progressive_tts=False, response_tts_backlog=2)
    p = build_planner(settings=s, turn_id=1)
    assert p.enabled is False
    assert p.backlog_cap == 2


def test_metrics_are_bounded_and_content_free():
    p = _planner()
    p.plan(_frag("Mi contraseña secreta es hunter2 y es larga. "))
    snap = p.snapshot()
    blob = " ".join(str(v) for v in snap.values()).lower()
    assert "hunter2" not in blob and "contraseña" not in blob
    assert set(snap) >= {"progressive_enabled", "first_utterance_ms", "queued",
                         "stale_dropped", "current_depth", "high_watermark"}
