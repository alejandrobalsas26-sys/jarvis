"""tests/test_stream_assembler_v69_m573.py — V69 M57.3: sentence-aware assembly.

Proves the bounded assembler between native chunks and presentation:

  * words split across chunks are never emitted half-written;
  * decimals, abbreviations, initials, URLs, times and ellipses do not split;
  * Spanish inverted punctuation and English punctuation both segment correctly;
  * headings, list items, inline code and fenced code keep their structure;
  * idle flush, buffer ceiling and final flush all bound the buffer;
  * duplicates are suppressed and ordering is preserved exactly;
  * a token-by-token stream does NOT produce a fragment per token.

Pure — no console, no TTS, no asyncio.
"""
from __future__ import annotations

from core.stream_assembler import (
    Fragment,
    FragmentKind,
    StreamAssembler,
    build_assembler,
    find_sentence_end,
)


def _drain(asm: StreamAssembler, chunks, *, now=0.0) -> list[Fragment]:
    out: list[Fragment] = []
    for c in chunks:
        out.extend(asm.push(c, now=now))
    out.extend(asm.finish(now=now))
    return out


def _texts(frags) -> list[str]:
    return [f.text for f in frags]


def _joined(frags) -> str:
    return "".join(f.text for f in frags)


# ── the conservation invariant ────────────────────────────────────────────────
def test_fragments_reproduce_the_stream_exactly():
    chunks = ["La raíz ", "cúbica de x ", "es x ** (1/3). ", "Por ejemplo, ",
              "la raíz cúbica de 27 es 3.\n\n", "Se puede calcular así:\n",
              "- con `x ** (1/3)`\n", "- con math.pow\n"]
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, chunks)
    assert _joined(frags) == "".join(chunks)


def test_no_text_is_lost_when_the_stream_ends_mid_sentence():
    chunks = ["Una respuesta ", "que se corta a mitad"]
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, chunks)
    assert _joined(frags) == "".join(chunks)
    assert frags[-1].kind is FragmentKind.TEXT


# ── words split across chunks ─────────────────────────────────────────────────
def test_word_split_across_chunks_is_never_emitted_half_written():
    asm = StreamAssembler()
    assert asm.push("La raíz cúbi", now=0.0) == []
    assert asm.push("ca es sencilla", now=0.0) == []
    frags = asm.push(". Siguiente.", now=0.0)
    assert frags[0].text.startswith("La raíz cúbica es sencilla.")


def test_terminator_alone_does_not_confirm_a_boundary():
    asm = StreamAssembler()
    asm.push("Hola mundo", now=0.0)
    assert asm.push(".", now=0.0) == [], "a trailing dot may still be a decimal"
    frags = asm.push(" Y ahora sí.", now=0.0)
    assert frags and frags[0].text.strip() == "Hola mundo."


# ── punctuation edge cases ────────────────────────────────────────────────────
def test_decimals_do_not_split():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["El resultado es 3.14 aproximadamente. Fin. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "El resultado es 3.14 aproximadamente." in sentences


def test_abbreviations_do_not_split():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["El Dr. House lo explica. Luego seguimos. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "El Dr. House lo explica." in sentences


def test_initials_do_not_split():
    assert find_sentence_end("J. R. R. Tolkien escribió mucho. Fin ") > 0
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["J. R. R. Tolkien escribió mucho. Fin. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "J. R. R. Tolkien escribió mucho." in sentences


def test_urls_do_not_split_mid_path():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["Mira https://ejemplo.com/guia.html para más. Fin. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "Mira https://ejemplo.com/guia.html para más." in sentences


def test_ellipsis_is_not_a_sentence_end():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["Espera... ya casi está listo. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "Espera... ya casi está listo." in sentences


def test_clock_time_and_semicolon_do_not_split():
    # The old regex split on ':' and ';' — "10:30" became two fragments.
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["Son las 10:30; nos vemos luego. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "Son las 10:30; nos vemos luego." in sentences


def test_spanish_inverted_punctuation_segments_correctly():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["¿Qué es POO? Es un paradigma. ¡Muy útil! "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "¿Qué es POO?" in sentences
    assert "Es un paradigma." in sentences
    assert "¡Muy útil!" in sentences


def test_english_punctuation_segments_correctly():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["This is one. And this is two! Is this three? "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert sentences[:3] == ["This is one.", "And this is two!", "Is this three?"]


def test_inline_code_containing_a_dot_does_not_split():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["Usa `math.sqrt(x)` para la raíz. Fin. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert "Usa `math.sqrt(x)` para la raíz." in sentences


# ── structure ─────────────────────────────────────────────────────────────────
def test_markdown_heading_becomes_a_paragraph_fragment():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["## Conceptos\n", "El primero es la herencia. "])
    kinds = [f.kind for f in frags]
    assert FragmentKind.PARAGRAPH in kinds
    assert frags[0].stripped() == "## Conceptos"


def test_list_items_are_emitted_one_per_item():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["- uno\n", "- dos\n", "1. tres\n"])
    items = [f.stripped() for f in frags if f.kind is FragmentKind.LIST_ITEM]
    assert items == ["- uno", "- dos", "1. tres"]


def test_fenced_code_is_emitted_line_by_line_with_boundaries():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["```python\n", "def f(x):\n", "    return x ** (1/3)\n",
                         "```\n", "Eso es todo. "])
    kinds = [f.kind for f in frags]
    assert kinds[0] is FragmentKind.CODE_BLOCK_BOUNDARY
    assert FragmentKind.CODE_LINE in kinds
    assert kinds.count(FragmentKind.CODE_BLOCK_BOUNDARY) == 2
    code = [f.stripped() for f in frags if f.kind is FragmentKind.CODE_LINE]
    assert code == ["def f(x):", "return x ** (1/3)"]


def test_code_lines_are_never_sentence_split():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["```\n", "a = 1. b = 2. c = 3.\n", "```\n"])
    code = [f for f in frags if f.kind is FragmentKind.CODE_LINE]
    assert len(code) == 1
    assert code[0].stripped() == "a = 1. b = 2. c = 3."


def test_identical_code_lines_are_not_deduplicated():
    asm = StreamAssembler(dedup=True)
    frags = _drain(asm, ["```\n", "    return None\n", "    return None\n", "```\n"])
    code = [f for f in frags if f.kind is FragmentKind.CODE_LINE]
    assert len(code) == 2


def test_unclosed_code_fence_is_reported():
    asm = StreamAssembler(dedup=False)
    _drain(asm, ["```python\n", "x = 1\n"])
    assert asm.snapshot()["unclosed_code_fence"] is True


# ── bounds ────────────────────────────────────────────────────────────────────
def test_buffer_ceiling_forces_a_word_boundary_flush():
    asm = StreamAssembler(max_buffer_chars=60, dedup=False)
    long_clause = "palabra " * 40          # no terminator at all
    frags = []
    for i in range(0, len(long_clause), 7):
        frags.extend(asm.push(long_clause[i:i + 7], now=0.0))
    assert frags, "the buffer must flush before growing without bound"
    assert asm.snapshot()["max_buffer_chars"] <= 60 + 8
    for f in frags:
        assert not f.text.endswith("palab"), "never flush mid-word"


def test_idle_flush_emits_when_no_boundary_arrives():
    asm = StreamAssembler(idle_flush_ms=500, dedup=False)
    assert asm.push("una respuesta sin punto final", now=0.0) == []
    assert asm.tick(now=0.2) == []
    frags = asm.tick(now=1.0)
    assert frags and frags[0].kind is FragmentKind.TEXT
    assert asm.snapshot()["idle_flushes"] == 1


def test_idle_flush_never_cuts_a_code_line():
    asm = StreamAssembler(idle_flush_ms=100, dedup=False)
    asm.push("```python\n", now=0.0)
    asm.push("def f(x):", now=0.0)
    assert asm.tick(now=5.0) == []


def test_idle_flush_does_not_emit_a_lone_partial_word():
    asm = StreamAssembler(idle_flush_ms=100, dedup=False)
    asm.push("palabr", now=0.0)
    assert asm.tick(now=5.0) == []


def test_final_flush_happens_exactly_once():
    asm = StreamAssembler(dedup=False)
    asm.push("Texto pendiente", now=0.0)
    first = asm.finish(now=0.0)
    assert first
    assert asm.finish(now=0.0) == []
    assert asm.push("mas", now=0.0) == []


def test_final_status_is_appended_after_content():
    asm = StreamAssembler(dedup=False)
    asm.push("Respuesta parcial", now=0.0)
    frags = asm.finish(now=0.0, status="[interrumpido]")
    assert frags[-1].kind is FragmentKind.FINAL_STATUS
    assert frags[-1].text == "[interrumpido]"


# ── duplicates and ordering ───────────────────────────────────────────────────
def test_duplicate_prose_fragment_is_suppressed_and_counted():
    asm = StreamAssembler(dedup=True)
    text = "Esta es una frase suficientemente larga. "
    frags = _drain(asm, [text, text])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert sentences.count(text.strip()) == 1
    assert asm.snapshot()["duplicate_fragments_suppressed"] == 1


def test_short_repeats_are_not_suppressed():
    asm = StreamAssembler(dedup=True)
    frags = _drain(asm, ["Sí. ", "Sí. "])
    sentences = [f.stripped() for f in frags if f.kind is FragmentKind.SENTENCE]
    assert sentences.count("Sí.") == 2


def test_ordering_is_preserved():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["Uno alfa beta. ", "Dos gamma delta. ", "Tres epsilon. "])
    order = [f.index for f in frags]
    assert order == sorted(order)
    joined = _joined(frags)
    assert joined.index("Uno") < joined.index("Dos") < joined.index("Tres")


# ── no character flood ────────────────────────────────────────────────────────
def test_token_by_token_stream_does_not_produce_a_fragment_per_token():
    text = ("La raíz cúbica de x es x elevado a un tercio. "
            "Se escribe x ** (1/3) en Python. "
            "También sirve math.pow(x, 1/3) para el mismo cálculo. ")
    tokens = [text[i:i + 3] for i in range(0, len(text), 3)]
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, tokens)
    assert asm.snapshot()["chunks_received"] == len(tokens)
    assert len(frags) <= 4, "one fragment per sentence, not per token"
    for f in frags:
        assert len(f.text.strip()) > 1


def test_malformed_and_empty_chunks_are_harmless():
    asm = StreamAssembler(dedup=False)
    frags = _drain(asm, ["", "\n\n", "Hola.", "", " ", "Adiós. "])
    assert _joined(frags).strip().startswith("Hola.")
    assert asm.snapshot()["fragments_emitted"] >= 1


# ── metrics ───────────────────────────────────────────────────────────────────
def test_metrics_are_bounded_and_content_free():
    asm = StreamAssembler(dedup=False, started_at=0.0)
    asm.push("Mi contraseña secreta es hunter2. ", now=0.5)
    asm.finish(now=1.0)
    snap = asm.snapshot()
    blob = " ".join(str(v) for v in snap.values()).lower()
    assert "hunter2" not in blob and "contraseña" not in blob
    assert snap["first_fragment_ms"] == 500.0
    assert snap["first_sentence_ms"] == 500.0
    assert snap["final_flush"] is True
    assert set(snap) >= {"chunks_received", "fragments_emitted", "first_fragment_ms",
                         "first_sentence_ms", "buffered_chars", "max_buffer_chars",
                         "duplicate_fragments_suppressed", "final_flush"}


def test_build_assembler_uses_operator_config():
    from core.config import Settings
    s = Settings(response_max_buffer_chars=120, response_stream_flush_ms=250)
    asm = build_assembler(settings=s)
    assert asm.max_buffer_chars == 120
    assert abs(asm.idle_flush_s - 0.25) < 1e-6


def test_build_assembler_clamps_absurd_config():
    from core.config import Settings
    s = Settings(response_max_buffer_chars=1, response_stream_flush_ms=1)
    assert s.response_max_buffer_chars == 80
    assert s.response_stream_flush_ms == 100
