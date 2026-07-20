"""
core/console.py — V69 M54.1: the single interactive console coordinator.

The live run corrupted the terminal because three writers touched one TTY with no
serialization: background `logger` lines (stderr), the LLM stream's raw
`print(chunk)`, and the blocking `input("Tú: ")`. Log lines interleaved with the
half-typed prompt ("09:4explicame algo de pythonTEL...") and streamed tool-call
JSON was read back as user input (symptoms #1, #2).

This coordinator makes ONE object own every interactive write. Producers (loggers,
boot narration, assistant stream, tool status, warnings) enqueue typed
`ConsoleEvent`s onto a bounded queue; a single renderer thread drains it, and it is
the ONLY thing that writes to the real stream. When a prompt is active it erases the
input line, prints the message on its own clean line, then redraws the prompt +
whatever the user has typed so far — the input line is never clobbered.

Design constraints honored:
  * bounded queue with a drop/coalesce policy for low-value repeated logs;
  * a high-priority lane for errors / HITL prompts that is never dropped;
  * usable with plain `sys.stdout` (no Rich/prompt_toolkit dependency);
  * Windows-friendly (ANSI erase-line with a CR fallback; no curses);
  * never blocks the asyncio event loop (enqueue is lock-guarded + non-blocking,
    rendering happens on a dedicated daemon thread);
  * a bounded shutdown flush; no orphan renderer thread (daemon + sentinel).

It deliberately does NOT own reading input — a thin `read_line()` helper coordinates
with the renderer so the prompt is shown/cleared around the blocking read. Real
input still comes from `input()` run in an executor by the caller.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, TextIO


class ConsoleChannel(IntEnum):
    """What kind of line this is — drives styling, priority, and drop policy.
    Higher value = higher priority (never dropped above NORMAL)."""

    LOG = 0          # background INFO/DEBUG log line (droppable/coalesceable)
    BOOT = 1         # boot progress line
    ASSISTANT = 2    # assistant-visible text (streamed answer)
    TOOL = 3         # tool status / summary (never raw payloads)
    WARNING = 4      # WARNING-level; must remain visible via safe redraw
    ERROR = 5        # ERROR/CRITICAL; high-priority lane, never dropped
    PROMPT_INFO = 6  # HITL / authorization prompt context; never dropped


@dataclass(frozen=True)
class ConsoleEvent:
    text: str
    channel: ConsoleChannel = ConsoleChannel.LOG
    # Coalescing key: repeated low-value events sharing a key collapse to the last
    # one while queued (e.g. a monitor heartbeat). None disables coalescing.
    coalesce_key: str | None = None
    ts: float = 0.0


# Channels that must never be dropped or coalesced away — operator safety.
_PROTECTED = frozenset({ConsoleChannel.WARNING, ConsoleChannel.ERROR,
                        ConsoleChannel.PROMPT_INFO})

# ANSI: erase whole line + carriage return. On a cp1252 Windows terminal these are
# plain ASCII control bytes, safe to emit. Fallback (no ANSI) uses spaces + CR.
_ERASE_LINE = "\x1b[2K\r"


def _supports_ansi(stream: TextIO) -> bool:
    if os.environ.get("JARVIS_CONSOLE_NO_ANSI", "").strip() in ("1", "true", "yes"):
        return False
    # Windows 10+ terminals and any TTY generally handle the erase-line sequence;
    # a redirected (non-tty) stream should not receive control bytes.
    return bool(getattr(stream, "isatty", lambda: False)())


@dataclass
class ConsoleCoordinator:
    """Single-owner interactive console renderer.

    Not started until `start()` is called (so importing is cheap and tests can
    drive `render_now()` synchronously without a thread). All public methods are
    safe to call from any thread or from async code (enqueue is non-blocking).
    """

    stream: TextIO = field(default_factory=lambda: sys.stdout)
    max_queue: int = 512
    clock: Callable[[], float] = time.monotonic

    _q: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _wake: threading.Event = field(default_factory=threading.Event)
    _worker: threading.Thread | None = field(default=None)
    _closed: bool = field(default=False)
    _started: bool = field(default=False)

    # Active input-line state (protected by _lock).
    _prompt: str | None = field(default=None)     # e.g. "Tú: "
    _prompt_active: bool = field(default=False)

    # Whether an assistant stream is mid-line (so we know to break before a log).
    _stream_open: bool = field(default=False)

    # Drop/coalesce accounting for health metrics.
    _dropped: int = field(default=0)
    _coalesced: int = field(default=0)
    _emitted: int = field(default=0)

    _ansi: bool = field(default=False)

    def __post_init__(self) -> None:
        self._ansi = _supports_ansi(self.stream)

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Spin up the single daemon renderer thread (idempotent)."""
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            self._worker = threading.Thread(
                target=self._run, name="console-renderer", daemon=True,
            )
            self._worker.start()

    def stop(self, timeout: float = 1.5) -> None:
        """Flush remaining events (bounded) and stop the renderer. Idempotent; no
        orphan thread (daemon + sentinel). Safe to call from shutdown."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._wake.set()
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=timeout)

    # ── Enqueue (non-blocking, any thread) ───────────────────────────────────
    def post(self, text: str, channel: ConsoleChannel = ConsoleChannel.LOG,
             coalesce_key: str | None = None) -> bool:
        """Enqueue one console line. Returns False if it was dropped by policy.

        Drop/coalesce policy (only ever applied to non-protected channels):
          * coalesce: if an unrendered event with the same key is queued, replace
            its text (collapse repeats) instead of appending a new line;
          * backpressure: when the queue is full, drop the OLDEST droppable event
            (LOG/BOOT) to make room; if none exists, drop this one.
        Protected channels (WARNING/ERROR/PROMPT_INFO) are always enqueued.
        """
        if not text:
            return False
        ev = ConsoleEvent(text=text, channel=channel,
                          coalesce_key=coalesce_key, ts=self.clock())
        with self._lock:
            if self._closed:
                return False
            if coalesce_key is not None and channel not in _PROTECTED:
                for i, existing in enumerate(self._q):
                    if existing.coalesce_key == coalesce_key:
                        self._q[i] = ev
                        self._coalesced += 1
                        self._wake.set()
                        return True
            if len(self._q) >= self.max_queue:
                if not self._make_room(protected=channel in _PROTECTED):
                    self._dropped += 1
                    return False
            self._q.append(ev)
        self._wake.set()
        return True

    def _make_room(self, *, protected: bool) -> bool:
        """Evict one droppable (LOG/BOOT) event to admit a new one. Returns True if
        room was made (or the incoming event is protected and we force-admit by
        evicting the oldest droppable). Caller holds _lock."""
        for i, ev in enumerate(self._q):
            if ev.channel not in _PROTECTED and ev.channel in (
                ConsoleChannel.LOG, ConsoleChannel.BOOT
            ):
                del self._q[i]
                self._dropped += 1
                return True
        # Nothing droppable. Admit protected events by evicting the oldest anyway
        # (operator safety beats an old queued line); drop non-protected.
        if protected and self._q:
            self._q.popleft()
            self._dropped += 1
            return True
        return False

    # ── Prompt coordination ──────────────────────────────────────────────────
    def set_prompt(self, prompt: str | None) -> None:
        """Declare the active input prompt (e.g. "Tú: "). When set, incoming log
        lines erase+redraw around it. Pass None to clear before a blocking read
        submits."""
        with self._lock:
            self._prompt = prompt
            self._prompt_active = prompt is not None
        # Draw the fresh prompt immediately so the user sees it.
        if prompt is not None:
            self.post("", ConsoleChannel.PROMPT_INFO, coalesce_key="__prompt_draw__")

    def begin_stream(self) -> None:
        """Mark that an assistant answer is about to stream inline (so a log that
        arrives mid-answer breaks to its own line first)."""
        with self._lock:
            self._stream_open = True

    def end_stream(self) -> None:
        with self._lock:
            self._stream_open = False

    # ── Renderer thread ──────────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            drained = self.render_now()
            with self._lock:
                closed = self._closed
                remaining = len(self._q)
            if closed and remaining == 0 and drained == 0:
                return

    def render_now(self) -> int:
        """Drain and render all currently-queued events. Returns the count
        rendered. Exposed for deterministic tests (call directly, no thread)."""
        rendered = 0
        while True:
            with self._lock:
                if not self._q:
                    break
                ev = self._q.popleft()
                prompt = self._prompt if self._prompt_active else None
                stream_open = self._stream_open
            # A pure prompt-draw marker just redraws the prompt line.
            if ev.channel == ConsoleChannel.PROMPT_INFO and ev.coalesce_key == "__prompt_draw__" and not ev.text:
                self._write_prompt(prompt)
                rendered += 1
                continue
            self._render_one(ev, prompt, stream_open)
            rendered += 1
        return rendered

    def _render_one(self, ev: ConsoleEvent, prompt: str | None, stream_open: bool) -> None:
        try:
            # Assistant stream text is written inline verbatim (no newline framing);
            # everything else is a full framed line that protects the prompt.
            if ev.channel == ConsoleChannel.ASSISTANT:
                if prompt is not None:
                    self._erase_line()
                self.stream.write(ev.text)
                self.stream.flush()
                self._emitted += 1
                return

            # A framed line: if a prompt is active OR an inline stream is open,
            # erase the current line first so we start clean.
            if prompt is not None or stream_open:
                self._erase_line()
            line = ev.text if ev.text.endswith("\n") else ev.text + "\n"
            self.stream.write(line)
            # Redraw the prompt (with nothing typed — the caller's input() owns the
            # actual echoed characters; we restore the visible prompt marker).
            if prompt is not None:
                self.stream.write(prompt)
            self.stream.flush()
            self._emitted += 1
        except Exception:
            # The console must never crash a producer; a failed write is swallowed.
            pass

    def _write_prompt(self, prompt: str | None) -> None:
        if prompt is None:
            return
        try:
            self._erase_line()
            self.stream.write(prompt)
            self.stream.flush()
        except Exception:
            pass

    def _erase_line(self) -> None:
        try:
            if self._ansi:
                self.stream.write(_ERASE_LINE)
            else:
                # Best-effort clear without ANSI: CR + pad + CR.
                self.stream.write("\r" + " " * 80 + "\r")
        except Exception:
            pass

    # ── Metrics ──────────────────────────────────────────────────────────────
    def metrics(self) -> dict:
        with self._lock:
            return {
                "queued": len(self._q),
                "max_queue": self.max_queue,
                "emitted": self._emitted,
                "dropped": self._dropped,
                "coalesced": self._coalesced,
                "prompt_active": self._prompt_active,
                "ansi": self._ansi,
            }


# ── Process-global singleton + logger sink integration ───────────────────────
_console: ConsoleCoordinator | None = None


def get_console() -> ConsoleCoordinator | None:
    """The active console coordinator, or None if not installed (headless/tests)."""
    return _console


def install_console(stream: TextIO | None = None, *, start: bool = True) -> ConsoleCoordinator:
    """Install the process-global console coordinator (idempotent)."""
    global _console
    if _console is None:
        _console = ConsoleCoordinator(stream=stream or sys.stdout)
        if start:
            _console.start()
    return _console


def reset_console() -> None:
    """Tear down the global console (tests / shutdown)."""
    global _console
    remove_stdlib_logging_bridge()
    if _console is not None:
        _console.stop()
    _console = None


# Loguru level -> console channel mapping for the sink.
_LEVEL_CHANNEL = {
    "TRACE": ConsoleChannel.LOG, "DEBUG": ConsoleChannel.LOG,
    "INFO": ConsoleChannel.LOG, "SUCCESS": ConsoleChannel.LOG,
    "WARNING": ConsoleChannel.WARNING, "ERROR": ConsoleChannel.ERROR,
    "CRITICAL": ConsoleChannel.ERROR,
}


def console_log_sink(message) -> None:
    """A loguru sink that routes formatted log records through the coordinator
    instead of writing straight to stderr. Repeated same-level, same-first-token
    INFO lines coalesce so a chatty monitor cannot flood the input line.

    `message` is a loguru Message (str subclass carrying `.record`). Falls back to
    plain stderr when no console is installed.
    """
    console = _console
    text = str(message).rstrip("\n")
    if console is None:
        try:
            sys.stderr.write(text + "\n")
        except Exception:
            pass
        return
    try:
        level = message.record["level"].name
    except Exception:
        level = "INFO"
    channel = _LEVEL_CHANNEL.get(level, ConsoleChannel.LOG)
    # Coalesce only low-value INFO/DEBUG lines, keyed by their subsystem tag (the
    # "TAG:" prefix JARVIS logs use). WARNING+ is never coalesced.
    coalesce_key = None
    if channel == ConsoleChannel.LOG:
        try:
            raw = message.record["message"]
            coalesce_key = raw.split(":", 1)[0][:32] if ":" in raw[:40] else None
        except Exception:
            coalesce_key = None
    console.post(text, channel, coalesce_key=coalesce_key)


# ── V69 M55.1.2 — stdlib `logging` bridge (db_manager & ~30 core modules) ──────
# The ConsoleCoordinator only intercepted loguru. Modules using the standard
# library's `logging.getLogger(...)` (e.g. `jarvis.db_manager`) had NO handler, so
# their records hit logging's *lastResort* handler and were written straight to
# stderr in `LEVELNAME:name:message` form — exactly the live corruption
# `Tú: ERROR:jarvis.db_manager: PostgreSQL unavailable ...`. Attaching a single
# handler to the ROOT logger routes those records through the coordinator (which
# erases/redraws around the prompt) AND disables lastResort, so no stdlib log can
# ever share the input line again. It never hides errors — WARNING/ERROR are
# rendered on their own clean line via the protected lane.
class _ConsoleLoggingHandler(logging.Handler):
    """Forward stdlib `logging` records into the single ConsoleCoordinator. A
    logging handler must never raise into the caller, so every failure is swallowed
    and falls back to a clean stderr line."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            channel = _LEVEL_CHANNEL.get(record.levelname, ConsoleChannel.LOG)
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)
            text = f"{record.name}: {msg}" if record.name else msg
            console = _console
            if console is None:
                # No coordinator installed (headless/tests): keep it on its own line.
                try:
                    sys.stderr.write(text + "\n")
                except Exception:
                    pass
                return
            coalesce_key = (record.name or "")[:32] or None if channel == ConsoleChannel.LOG else None
            console.post(text, channel, coalesce_key=coalesce_key)
        except Exception:
            pass


_stdlib_bridge_handler: "_ConsoleLoggingHandler | None" = None


def install_stdlib_logging_bridge(level: int = logging.WARNING) -> None:
    """Route stdlib `logging` through the ConsoleCoordinator (idempotent).

    Attaches ONE handler to the root logger and removes logging's default stderr
    path (any root handler disables lastResort). ``level`` defaults to WARNING so
    INFO stays exactly as quiet as before — only the lines that previously reached
    stderr via lastResort are now rendered prompt-safely. Never raises."""
    global _stdlib_bridge_handler
    if _stdlib_bridge_handler is not None:
        return
    try:
        root = logging.getLogger()
        handler = _ConsoleLoggingHandler()
        handler.setLevel(level)
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        _stdlib_bridge_handler = handler
    except Exception:
        _stdlib_bridge_handler = None


def remove_stdlib_logging_bridge() -> None:
    """Detach the stdlib bridge (tests / shutdown). Idempotent, never raises."""
    global _stdlib_bridge_handler
    if _stdlib_bridge_handler is not None:
        try:
            logging.getLogger().removeHandler(_stdlib_bridge_handler)
        except Exception:
            pass
        _stdlib_bridge_handler = None
