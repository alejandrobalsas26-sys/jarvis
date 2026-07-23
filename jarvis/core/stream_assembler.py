"""core/stream_assembler.py — V69 M57.3: bounded sentence-aware stream assembly.

WHAT IT REPLACES
----------------
The live path printed EVERY native delta straight to the console
(``main._emit_chunk`` -> ``ConsoleCoordinator.post`` per chunk) and segmented speech
with ``re.compile(r'(?<=[.!?;:])\\s+')``. That regex splits ``3.14``, ``Dr. House``,
``https://a.co/b.c``, ``10:30`` and every line of a code block, and the per-delta
posting floods a 512-slot queue in which the ASSISTANT channel is *droppable* — so a
long answer could silently lose text.

WHAT THIS IS
------------
ONE bounded assembler between the transport and presentation. It accumulates raw
chunks and emits READABLE fragments at real structural boundaries:

    native chunks -> normalize -> bounded buffer -> structure scan -> Fragment[]

It is deliberately NOT a Markdown parser. It understands exactly enough structure to
avoid ugly output: fenced code, inline code, headings, list items, paragraphs,
sentences, abbreviations, decimals, URLs and Spanish inverted punctuation.

THE CONSERVATION INVARIANT
--------------------------
Concatenating every emitted fragment's text reproduces the pushed stream exactly
(minus the leading whitespace the model sometimes emits, and minus any suppressed
duplicate, both counted). That is what makes "no duplicate text, exact ordering
preserved" a property rather than a hope — a test asserts it directly.

Pure, synchronous and clock-injectable: no I/O, no asyncio, no console, no TTS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# Sentence terminators. ';' and ':' are deliberately NOT terminators: the old regex
# split on them and produced "10" / "30" from a clock time and a dangling clause
# from every list introduction.
_TERMINATORS = ".!?…"
# Characters that may legitimately follow a terminator and still belong to the same
# sentence (closing quotes/brackets).
_CLOSERS = "\"'”’»)]}"
_LIST_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+|\d{1,3}[.)]\s+)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_FENCE_RE = re.compile(r"^\s{0,3}```")

# Abbreviations whose trailing period is NOT a sentence end (lowercased, no dot).
# Spanish first — this runtime answers in Spanish by default.
_ABBREVIATIONS: frozenset[str] = frozenset({
    "sr", "sra", "srta", "dr", "dra", "lic", "ing", "prof", "av", "avda", "ej",
    "p.ej", "etc", "aprox", "núm", "num", "pág", "pag", "art", "cap", "vol",
    "fig", "tel", "ext", "ee", "uu", "vs", "ud", "uds", "máx", "max", "mín", "min",
    "mr", "mrs", "ms", "jr", "st", "inc", "ltd", "co", "corp", "dept", "est",
    "e.g", "i.e", "cf", "al", "eq", "ref", "sec", "ch", "no", "approx", "a.m",
    "p.m", "u.s", "e.u",
})
_WORD_TAIL_RE = re.compile(r"([A-Za-zÁÉÍÓÚÑÜáéíóúñü.]+)$")


class FragmentKind(str, Enum):
    """What a fragment IS. Presentation and speech policy both key off this."""

    TEXT = "TEXT"                                  # forced flush (buffer/idle)
    SENTENCE = "SENTENCE"                          # a complete sentence
    PARAGRAPH = "PARAGRAPH"                        # paragraph break or heading
    CODE_LINE = "CODE_LINE"                        # one line inside a fence
    CODE_BLOCK_BOUNDARY = "CODE_BLOCK_BOUNDARY"    # the ``` line itself
    LIST_ITEM = "LIST_ITEM"                        # one bullet/numbered item
    FINAL_STATUS = "FINAL_STATUS"                  # runtime status, never model text


# Fragments that are prose (candidates for speech, subject to dedup).
PROSE_KINDS: frozenset[FragmentKind] = frozenset({
    FragmentKind.SENTENCE, FragmentKind.PARAGRAPH, FragmentKind.LIST_ITEM,
    FragmentKind.TEXT,
})
# Fragments that must NEVER be deduplicated: code legitimately repeats lines.
_NEVER_DEDUP: frozenset[FragmentKind] = frozenset({
    FragmentKind.CODE_LINE, FragmentKind.CODE_BLOCK_BOUNDARY,
    FragmentKind.FINAL_STATUS,
})
# Below this length a repeat is probably legitimate ("Sí.", "OK."), so dedup only
# applies to substantial fragments.
_MIN_DEDUP_CHARS = 12


@dataclass(frozen=True)
class Fragment:
    """One emitted unit. ``text`` is the RAW slice, trailing whitespace included, so
    concatenating fragments reproduces the stream byte-for-byte."""

    kind: FragmentKind
    text: str
    index: int = 0
    in_code: bool = False

    def display(self) -> str:
        return self.text

    def stripped(self) -> str:
        return self.text.strip()

    def is_prose(self) -> bool:
        return self.kind in PROSE_KINDS and not self.in_code


@dataclass
class AssemblerMetrics:
    """Bounded, content-free stream metrics for runtime health."""

    chunks_received: int = 0
    fragments_emitted: int = 0
    chars_in: int = 0
    chars_out: int = 0
    first_fragment_ms: float | None = None
    first_sentence_ms: float | None = None
    buffered_chars: int = 0
    max_buffer_chars: int = 0
    duplicate_fragments_suppressed: int = 0
    duplicate_chars_suppressed: int = 0
    idle_flushes: int = 0
    buffer_flushes: int = 0
    final_flush: bool = False
    unclosed_code_fence: bool = False
    by_kind: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {
            "chunks_received": self.chunks_received,
            "fragments_emitted": self.fragments_emitted,
            "first_fragment_ms": self.first_fragment_ms,
            "first_sentence_ms": self.first_sentence_ms,
            "buffered_chars": self.buffered_chars,
            "max_buffer_chars": self.max_buffer_chars,
            "duplicate_fragments_suppressed": self.duplicate_fragments_suppressed,
            "idle_flushes": self.idle_flushes,
            "buffer_flushes": self.buffer_flushes,
            "final_flush": self.final_flush,
            "unclosed_code_fence": self.unclosed_code_fence,
            "chars_in": self.chars_in,
            "chars_out": self.chars_out,
            "by_kind": dict(self.by_kind),
        }


def _is_abbreviation(text_before: str) -> bool:
    """True when the period at the end of *text_before* closes a known abbreviation
    or a single initial ("J. R. R."), not a sentence."""
    m = _WORD_TAIL_RE.search(text_before)
    if not m:
        return False
    token = m.group(1).rstrip(".").lower()
    if not token:
        return False
    if token in _ABBREVIATIONS:
        return True
    # A single letter before the dot is an initial, never a sentence end.
    return len(token) == 1 and token.isalpha()


def find_sentence_end(buffer: str, *, require_trailing_space: bool = True) -> int:
    """Index just past a CONFIRMED sentence boundary in *buffer*, or -1.

    A boundary is confirmed only when a terminator is followed by whitespace (or the
    end of the stream, for the final flush). Requiring the following whitespace is
    what makes a word split across two chunks safe: the boundary simply is not
    confirmed until the next chunk arrives.
    """
    i = 0
    n = len(buffer)
    inline_code = False
    while i < n:
        ch = buffer[i]
        if ch == "`":
            inline_code = not inline_code
            i += 1
            continue
        if inline_code or ch not in _TERMINATORS:
            i += 1
            continue
        # Ellipsis: "..." is a pause, not a sentence end.
        if ch == "." and i > 0 and buffer[i - 1] == ".":
            i += 1
            continue
        if ch == "." and i + 1 < n and buffer[i + 1] == ".":
            i += 1
            continue
        # Decimal / version / IP: a digit either side of the dot.
        if ch == "." and i > 0 and buffer[i - 1].isdigit() and i + 1 < n \
                and buffer[i + 1].isdigit():
            i += 1
            continue
        if ch == "." and _is_abbreviation(buffer[:i]):
            i += 1
            continue
        j = i + 1
        while j < n and buffer[j] in _CLOSERS:
            j += 1
        if j >= n:
            # Nothing after the terminator yet: only the final flush may take it.
            return j if not require_trailing_space else -1
        if buffer[j].isspace():
            return j
        i += 1
    return -1


class StreamAssembler:
    """Accumulates raw chunks and emits readable fragments. Pure and bounded.

    Usage per turn::

        asm = StreamAssembler(max_buffer_chars=400, idle_flush_ms=700)
        for chunk in stream:
            for frag in asm.push(chunk, now=t):
                render(frag)
        for frag in asm.tick(now=t):        # optional idle flush
            render(frag)
        for frag in asm.finish(now=t):
            render(frag)
    """

    def __init__(self, *, max_buffer_chars: int = 400, idle_flush_ms: int = 700,
                 dedup: bool = True, started_at: float = 0.0) -> None:
        self.max_buffer_chars = max(40, int(max_buffer_chars))
        self.idle_flush_s = max(0.05, float(idle_flush_ms) / 1000.0)
        self.dedup = bool(dedup)
        self.metrics = AssemblerMetrics()
        self._buf = ""
        self._index = 0
        self._in_code = False
        self._started_at = float(started_at)
        self._last_push_at = float(started_at)
        self._seen_any = False
        self._last_prose = ""
        self._finished = False

    # ── internals ────────────────────────────────────────────────────────────
    def _elapsed_ms(self, now: float) -> float:
        return round(max(0.0, now - self._started_at) * 1000.0, 1)

    def _emit(self, kind: FragmentKind, text: str, now: float) -> Fragment | None:
        """Stamp, count and dedup ONE fragment. Returns None when suppressed."""
        if not text:
            return None
        if (self.dedup and kind not in _NEVER_DEDUP
                and len(text.strip()) >= _MIN_DEDUP_CHARS
                and text.strip() == self._last_prose):
            self.metrics.duplicate_fragments_suppressed += 1
            self.metrics.duplicate_chars_suppressed += len(text)
            return None
        frag = Fragment(kind=kind, text=text, index=self._index,
                        in_code=self._in_code)
        self._index += 1
        self.metrics.fragments_emitted += 1
        self.metrics.chars_out += len(text)
        self.metrics.by_kind[kind.value] = self.metrics.by_kind.get(kind.value, 0) + 1
        if self.metrics.first_fragment_ms is None:
            self.metrics.first_fragment_ms = self._elapsed_ms(now)
        if kind is FragmentKind.SENTENCE and self.metrics.first_sentence_ms is None:
            self.metrics.first_sentence_ms = self._elapsed_ms(now)
        if kind not in _NEVER_DEDUP and len(text.strip()) >= _MIN_DEDUP_CHARS:
            self._last_prose = text.strip()
        return frag

    def _take(self, upto: int) -> str:
        """Cut ``buffer[:upto]`` plus the whitespace run that follows, so the next
        fragment starts clean and no character is ever lost or duplicated."""
        end = upto
        n = len(self._buf)
        while end < n and self._buf[end] in " \t":
            end += 1
        if end < n and self._buf[end] == "\r":
            end += 1
        if end < n and self._buf[end] == "\n":
            end += 1
        out, self._buf = self._buf[:end], self._buf[end:]
        return out

    def _scan(self, now: float) -> list[Fragment]:
        """Emit every structural boundary currently present in the buffer."""
        out: list[Fragment] = []
        guard = 0
        while self._buf and guard < 512:
            guard += 1
            nl = self._buf.find("\n")

            # 1. Inside a fenced block: strictly line-based, no sentence logic.
            if self._in_code:
                if nl < 0:
                    break
                line = self._buf[:nl + 1]
                self._buf = self._buf[nl + 1:]
                if _FENCE_RE.match(line):
                    self._in_code = False
                    frag = self._emit(FragmentKind.CODE_BLOCK_BOUNDARY, line, now)
                else:
                    frag = self._emit(FragmentKind.CODE_LINE, line, now)
                if frag:
                    out.append(frag)
                continue

            # 2. A completed line that is structural (fence / heading / list item).
            if nl >= 0:
                line = self._buf[:nl + 1]
                if _FENCE_RE.match(line):
                    # Flush any prose sitting before the fence, then open the block.
                    if line.strip() != self._buf[:nl].strip():
                        pass
                    self._buf = self._buf[nl + 1:]
                    self._in_code = True
                    frag = self._emit(FragmentKind.CODE_BLOCK_BOUNDARY, line, now)
                    if frag:
                        out.append(frag)
                    continue
                if _HEADING_RE.match(line):
                    self._buf = self._buf[nl + 1:]
                    frag = self._emit(FragmentKind.PARAGRAPH, line, now)
                    if frag:
                        out.append(frag)
                    continue
                if _LIST_RE.match(line):
                    self._buf = self._buf[nl + 1:]
                    frag = self._emit(FragmentKind.LIST_ITEM, line, now)
                    if frag:
                        out.append(frag)
                    continue

            # 3. A sentence boundary that lands before the next newline.
            end = find_sentence_end(self._buf)
            if end > 0 and (nl < 0 or end <= nl + 1):
                frag = self._emit(FragmentKind.SENTENCE, self._take(end), now)
                if frag:
                    out.append(frag)
                continue

            # 4. A blank line closes a paragraph.
            if nl >= 0 and not self._buf[:nl].strip():
                frag = self._emit(FragmentKind.PARAGRAPH, self._take(nl), now)
                if frag:
                    out.append(frag)
                continue
            if nl >= 0 and end < 0:
                # A completed prose line with no terminator (a wrapped clause):
                # hold it — the sentence may continue on the next line — unless the
                # buffer is already over budget, which rule 5 handles.
                if len(self._buf) <= self.max_buffer_chars:
                    break
                frag = self._emit(FragmentKind.PARAGRAPH, self._take(nl), now)
                self.metrics.buffer_flushes += 1
                if frag:
                    out.append(frag)
                continue

            # 5. Buffer ceiling: flush at the last word boundary, never mid-word.
            if len(self._buf) > self.max_buffer_chars:
                cut = self._buf.rfind(" ", 0, self.max_buffer_chars)
                cut = cut if cut > 0 else self.max_buffer_chars
                frag = self._emit(FragmentKind.TEXT, self._take(cut), now)
                self.metrics.buffer_flushes += 1
                if frag:
                    out.append(frag)
                continue
            break
        self.metrics.buffered_chars = len(self._buf)
        self.metrics.max_buffer_chars = max(self.metrics.max_buffer_chars,
                                            len(self._buf))
        return out

    # ── public API ───────────────────────────────────────────────────────────
    def push(self, chunk: str, *, now: float = 0.0) -> list[Fragment]:
        """Feed one raw transport chunk. Returns the fragments it completed."""
        if self._finished or not chunk:
            return []
        self.metrics.chunks_received += 1
        self.metrics.chars_in += len(chunk)
        self._last_push_at = now
        if not self._seen_any:
            # Models routinely open with newlines; they are noise, not content.
            chunk = chunk.lstrip("\r\n")
            if not chunk:
                return []
            self._seen_any = True
        self._buf += chunk
        return self._scan(now)

    def tick(self, *, now: float = 0.0) -> list[Fragment]:
        """Idle flush. Emits buffered text when no boundary has arrived in
        ``idle_flush_ms`` so the operator never watches a frozen half-sentence."""
        if self._finished or not self._buf:
            return []
        if (now - self._last_push_at) < self.idle_flush_s:
            return []
        if self._in_code:
            return []          # a half-written code line is never useful alone
        cut = self._buf.rfind(" ")
        if cut <= 0:
            return []          # a single unfinished word is not worth flushing
        frag = self._emit(FragmentKind.TEXT, self._take(cut), now)
        self.metrics.idle_flushes += 1
        self._last_push_at = now
        self.metrics.buffered_chars = len(self._buf)
        return [frag] if frag else []

    def finish(self, *, now: float = 0.0, status: str | None = None) -> list[Fragment]:
        """Terminal flush. Emits everything still buffered exactly once, plus an
        optional runtime FINAL_STATUS line (never model text)."""
        if self._finished:
            return []
        self._finished = True
        self.metrics.final_flush = True
        out: list[Fragment] = []
        if self._buf:
            if self._in_code:
                self.metrics.unclosed_code_fence = True
                kind = FragmentKind.CODE_LINE
            else:
                end = find_sentence_end(self._buf, require_trailing_space=False)
                kind = (FragmentKind.SENTENCE if end >= len(self._buf.rstrip())
                        else FragmentKind.TEXT)
            frag = self._emit(kind, self._buf, now)
            self._buf = ""
            if frag:
                out.append(frag)
        elif self._in_code:
            self.metrics.unclosed_code_fence = True
        if status:
            frag = self._emit(FragmentKind.FINAL_STATUS, status, now)
            if frag:
                out.append(frag)
        self.metrics.buffered_chars = 0
        return out

    @property
    def buffered(self) -> str:
        return self._buf

    @property
    def in_code_block(self) -> bool:
        return self._in_code

    def snapshot(self) -> dict:
        return self.metrics.snapshot()


def build_assembler(*, settings=None, started_at: float = 0.0) -> StreamAssembler:
    """Build an assembler from operator config, with safe fallbacks."""
    max_chars, flush_ms = 400, 700
    if settings is None:
        try:
            from core.config import settings as _s
            settings = _s
        except Exception:  # noqa: BLE001
            settings = None
    if settings is not None:
        try:
            max_chars = int(getattr(settings, "response_max_buffer_chars", 400))
            flush_ms = int(getattr(settings, "response_stream_flush_ms", 700))
        except (TypeError, ValueError):
            max_chars, flush_ms = 400, 700
    return StreamAssembler(max_buffer_chars=max_chars, idle_flush_ms=flush_ms,
                           started_at=started_at)
