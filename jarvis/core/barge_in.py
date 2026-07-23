"""core/barge_in.py — V69 M58.8/.8.1: active-console immediate barge-in.

WHAT M57 LEFT
-------------
M57.5 gave the operator barge-in, but only by SUBMITTING a line (``/stop``): text
input is line-buffered, so there was no way to interrupt a generating/speaking answer
with a single key. This module adds an immediate key interrupt — strictly inside the
active JARVIS console.

WHAT THIS IS NOT
----------------
NOT a global keyboard hook. NOT background keylogging. NOT an OS-wide hotkey. NOT
keystroke persistence or raw-key logging. It reads ONLY this process's own console
input buffer, ONLY while an answer is actively generating/speaking, and it never
stores, logs or surfaces the key value.

BACKEND SELECTION
-----------------
``prompt_toolkit`` is not installed here, so the active-console backend is the
Windows console reader (``msvcrt``), which reads this process's console — not a hook.
When neither a raw backend nor msvcrt is available, the mode is COMMAND_ONLY and the
line-mode ``/stop`` fallback remains the interrupt path. A backend that DID change the
terminal mode restores it on stop/exception; the msvcrt reader changes nothing, so it
has nothing to restore — which is exactly why it is the safe choice on Windows.

Modes: COMMAND_ONLY · ACTIVE_CONSOLE_KEY · VOICE_ACTIVITY · UNAVAILABLE.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from loguru import logger


class BargeInMode(str, Enum):
    COMMAND_ONLY = "COMMAND_ONLY"            # only /stop; no key backend
    ACTIVE_CONSOLE_KEY = "ACTIVE_CONSOLE_KEY"  # a single console key interrupts
    VOICE_ACTIVITY = "VOICE_ACTIVITY"        # reuse the existing voice seam (optional)
    UNAVAILABLE = "UNAVAILABLE"              # no interruption path at all


# The allowlisted interrupt keys. Escape and Ctrl+G only — never Ctrl+C (which stays
# the graceful-shutdown signal) and never a printable character.
_ESC = "\x1b"
_CTRL_G = "\x07"
_INTERRUPT_KEYS = frozenset({_ESC, _CTRL_G})


def is_interrupt_key(key: str) -> bool:
    """True only for an allowlisted interrupt key. A raw key is never logged here."""
    return key in _INTERRUPT_KEYS


class KeyReader:
    """Abstract single-key active-console reader. Subclasses read ONLY this process's
    console. ``supported`` is False when the backend cannot run here."""

    supported: bool = False

    def start(self, on_key: Callable[[str], None]) -> None:  # pragma: no cover - iface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def restore_terminal(self) -> None:
        """Restore any terminal mode this backend changed. Default: nothing to do."""
        return None


class WindowsConsoleKeyReader(KeyReader):
    """msvcrt-based reader: polls THIS process's console buffer on a daemon thread
    while armed. Never blocks the event loop, never logs the key, changes no terminal
    mode (so nothing to restore), and stops cleanly. Only usable on Windows with a
    real console."""

    def __init__(self, poll_s: float = 0.02) -> None:
        self._poll_s = poll_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_key: Callable[[str], None] | None = None
        self.supported = self._detect()

    @staticmethod
    def _detect() -> bool:
        if not sys.platform.startswith("win"):
            return False
        try:
            import msvcrt  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        # A real interactive console is required; a redirected stdin cannot be read
        # key-by-key and must degrade to COMMAND_ONLY.
        try:
            return bool(getattr(sys.stdin, "isatty", lambda: False)())
        except Exception:  # noqa: BLE001
            return False

    def start(self, on_key: Callable[[str], None]) -> None:
        if not self.supported or (self._thread is not None and self._thread.is_alive()):
            return
        self._on_key = on_key
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="barge-in-key",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import msvcrt
        except Exception:  # noqa: BLE001
            return
        while not self._stop.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()  # reads one char, no echo, no logging
                    cb = self._on_key
                    if cb is not None and ch:
                        # The key value is passed to the controller and never stored
                        # or logged here.
                        cb(ch)
                else:
                    time.sleep(self._poll_s)
            except Exception:  # noqa: BLE001 — a reader fault must never crash a turn
                time.sleep(self._poll_s)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None
        self._on_key = None
        self.restore_terminal()

    def restore_terminal(self) -> None:
        # msvcrt changes no terminal mode — nothing to restore. Present so the
        # interface guarantee ("terminal restored on stop/exception") holds for every
        # backend, including future raw ones.
        return None


@dataclass
class BargeInController:
    """The single active-console barge-in decision point. Bounded and content-free.

    ``interrupt_action`` performs the actual teardown (cancel LLM stream, cancel
    answer TTS, mark the turn INTERRUPTED_BY_OPERATOR) — injected so the state machine
    is testable without a live turn. ``is_turn_active`` gates the key: when no answer
    is active, an interrupt key must NOT kill JARVIS and is simply ignored.
    """

    mode: BargeInMode = BargeInMode.COMMAND_ONLY
    reader: KeyReader | None = None
    interrupt_action: Callable[[], None] | None = None
    is_turn_active: Callable[[], bool] = lambda: False
    is_stopping: Callable[[], bool] = lambda: False
    clock: Callable[[], float] = time.monotonic
    loop: object | None = None  # asyncio loop for threadsafe marshalling (live path)

    _armed: bool = field(default=False)
    active_interruptions: int = 0
    command_interruptions: int = 0
    ignored_no_active_turn: int = 0
    cancellation_latency_ms: float | None = None
    terminal_restore_failures: int = 0

    @property
    def supported(self) -> bool:
        return self.mode is BargeInMode.ACTIVE_CONSOLE_KEY and bool(
            self.reader and getattr(self.reader, "supported", False))

    # ── the key decision (synchronous, testable) ─────────────────────────────
    def notify_key(self, key: str) -> bool:
        """Process ONE key. Interrupts the active turn iff it is an allowlisted key
        AND a turn is active AND we are not stopping. Returns whether it interrupted.
        The key value is never stored or logged."""
        if self.is_stopping():
            return False
        if not is_interrupt_key(key):
            return False
        if not self.is_turn_active():
            self.ignored_no_active_turn += 1
            return False
        return self._fire(source="key")

    def _fire(self, *, source: str) -> bool:
        t0 = self.clock()
        try:
            if self.interrupt_action is not None:
                self.interrupt_action()
        except Exception:  # noqa: BLE001 — teardown must never raise into the reader
            logger.warning("BARGE_IN: interrupt action raised; suppressed")
        self.cancellation_latency_ms = round((self.clock() - t0) * 1000.0, 1)
        if source == "key":
            self.active_interruptions += 1
        else:
            self.command_interruptions += 1
        return True

    def note_command_interrupt(self) -> None:
        """Record a /stop line-mode interruption (the COMMAND_ONLY fallback path)."""
        self.command_interruptions += 1

    # ── live wiring (marshals the reader thread onto the loop) ────────────────
    def _on_key_threadsafe(self, key: str) -> None:
        """Called from the reader daemon thread. Marshals onto the event loop so the
        cancel path runs where the turn lives. Never logs the key."""
        interrupt = is_interrupt_key(key) and not self.is_stopping()
        if not interrupt:
            return
        loop = self.loop
        if loop is not None and not getattr(loop, "is_closed", lambda: True)():
            try:
                loop.call_soon_threadsafe(lambda: self.notify_key(key))
                return
            except Exception:  # noqa: BLE001
                pass
        # No loop to marshal onto — best-effort direct (tests / degraded).
        self.notify_key(key)

    def arm(self) -> None:
        """Arm the key reader for an active turn. No-op unless ACTIVE_CONSOLE_KEY is
        supported and we are not stopping."""
        if self._armed or not self.supported or self.is_stopping():
            return
        try:
            self.reader.start(self._on_key_threadsafe)
            self._armed = True
        except Exception:  # noqa: BLE001
            self._armed = False

    def disarm(self) -> None:
        """Disarm at the end of a turn. Always restores the terminal; a restore
        failure is counted, never raised."""
        if not self._armed:
            return
        self._armed = False
        try:
            self.reader.stop()
        except Exception:  # noqa: BLE001
            self.terminal_restore_failures += 1
            try:
                self.reader.restore_terminal()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        """Close the input backend on shutdown. Idempotent, bounded, never raises."""
        try:
            self.disarm()
        finally:
            if self.reader is not None:
                try:
                    self.reader.stop()
                except Exception:  # noqa: BLE001
                    self.terminal_restore_failures += 1

    def snapshot(self) -> dict:
        return {
            "mode": self.mode.value,
            "supported": self.supported,
            "active_interruptions": self.active_interruptions,
            "command_interruptions": self.command_interruptions,
            "ignored_no_active_turn": self.ignored_no_active_turn,
            "cancellation_latency_ms": self.cancellation_latency_ms,
            "terminal_restore_failures": self.terminal_restore_failures,
        }


def resolve_backend_mode() -> tuple[BargeInMode, KeyReader | None]:
    """Pick the barge-in mode + backend for THIS host. prompt_toolkit is absent here,
    so ACTIVE_CONSOLE_KEY uses the msvcrt reader when a real Windows console exists;
    otherwise COMMAND_ONLY (the /stop fallback stays functional)."""
    # Operator override: allow forcing COMMAND_ONLY.
    try:
        from core.config import settings
        configured = str(getattr(settings, "barge_in_mode", "AUTO") or "AUTO").upper()
    except Exception:  # noqa: BLE001
        configured = "AUTO"
    if configured == "COMMAND_ONLY":
        return BargeInMode.COMMAND_ONLY, None
    reader = WindowsConsoleKeyReader()
    if reader.supported and configured in ("AUTO", "ACTIVE_CONSOLE_KEY"):
        return BargeInMode.ACTIVE_CONSOLE_KEY, reader
    return BargeInMode.COMMAND_ONLY, None


# ── Process-global singleton ─────────────────────────────────────────────────
_controller: BargeInController | None = None


def get_barge_in_controller() -> BargeInController:
    """The process barge-in controller, built from host capabilities on first use.

    The interrupt action and turn-active predicate are wired to the response runtime
    and cancel bus, so a key interrupt does exactly what /stop does — immediately."""
    global _controller
    if _controller is None:
        mode, reader = resolve_backend_mode()

        def _interrupt() -> None:
            # Same teardown as the /stop command: cancel the stream, cancel answer
            # TTS, mark the turn INTERRUPTED_BY_OPERATOR. Late chunks are suppressed by
            # the response-runtime turn id; partial displayed text stays.
            try:
                from core.cancel_bus import cancel_llm_only
                cancel_llm_only()
            except Exception:  # noqa: BLE001
                pass
            try:
                from core.speech_stream import cancel_answer_speech
                cancel_answer_speech(None)
            except Exception:  # noqa: BLE001
                pass
            try:
                from core.response_runtime import TurnState, get_response_runtime
                rr = get_response_runtime()
                if rr.current is not None and rr.current.is_active():
                    rr.end_turn(TurnState.INTERRUPTED_BY_OPERATOR)
            except Exception:  # noqa: BLE001
                pass

        def _turn_active() -> bool:
            try:
                from core.response_runtime import get_response_runtime
                cur = get_response_runtime().current
                return bool(cur is not None and cur.is_active())
            except Exception:  # noqa: BLE001
                return False

        def _stopping() -> bool:
            try:
                from core.lifecycle import get_lifecycle
                return bool(get_lifecycle().is_stopping())
            except Exception:  # noqa: BLE001
                return False

        _controller = BargeInController(
            mode=mode, reader=reader, interrupt_action=_interrupt,
            is_turn_active=_turn_active, is_stopping=_stopping)
    return _controller


def reset_barge_in_controller(instance: BargeInController | None = None) -> None:
    """Tests / a fresh process."""
    global _controller
    _controller = instance
