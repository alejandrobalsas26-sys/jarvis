"""core/barge_in.py — V69 M58.8/.8.1 + M59.5: portable active-console barge-in.

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
stores, logs or surfaces the key value. Each backend filters at the SOURCE: a key
that is not on the two-entry allowlist never leaves the reader thread at all, so a
non-interrupt keystroke cannot be observed anywhere downstream — not in a callback,
not in a metric, not in a log line.

BACKEND SELECTION (M59.5)
-------------------------
M58 hard-coded the Windows console reader because ``prompt_toolkit`` is not installed
on this host. M59.5 turns that into a real selection with an honest fallback chain:

    AUTO  →  1. PROMPT_TOOLKIT   installed + enabled + terminal-compatible
             2. WINDOWS_MSVCRT   a real Windows console
             3. COMMAND_ONLY     the line-mode /stop fallback (always functional)

An explicitly requested backend that cannot run here degrades through the SAME chain
and reports why via ``fallback_reason`` — the selected backend and the reason it was
not the requested one are both exposed, so a degraded host is never silently reported
as a working one. ``prompt_toolkit`` stays strictly OPTIONAL: it is detected with
``importlib.util.find_spec`` (no import cost when absent), it is never installed
automatically, and the production fallback keeps working without it.

TERMINAL SAFETY
---------------
A backend that CHANGES the terminal mode (prompt_toolkit's raw mode) always restores
it — on stop, on shutdown, and on an exception raised anywhere inside the reader. The
msvcrt reader changes no terminal mode at all, which is exactly why it is the safe
default on Windows. Exactly one input backend owns the console at a time
(``core.console.ConsoleInputOwnership``); a backend that cannot take ownership
degrades instead of becoming a second reader.

Modes: COMMAND_ONLY · ACTIVE_CONSOLE_KEY · VOICE_ACTIVITY · UNAVAILABLE.
Backends: AUTO · PROMPT_TOOLKIT · WINDOWS_MSVCRT · COMMAND_ONLY.
"""
from __future__ import annotations

import importlib.util
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


class BargeInBackend(str, Enum):
    """WHICH console-local backend provides the key interrupt. AUTO is a REQUEST,
    never a result: a resolved selection is always one of the three concrete values."""

    AUTO = "AUTO"
    PROMPT_TOOLKIT = "PROMPT_TOOLKIT"
    WINDOWS_MSVCRT = "WINDOWS_MSVCRT"
    COMMAND_ONLY = "COMMAND_ONLY"


class FallbackReason(str, Enum):
    """WHY the selected backend is not the preferred/requested one. Deterministic,
    content-free, and always paired with the backend that WAS selected."""

    NONE = "NONE"
    OPERATOR_FORCED_COMMAND_ONLY = "OPERATOR_FORCED_COMMAND_ONLY"
    PROMPT_TOOLKIT_NOT_INSTALLED = "PROMPT_TOOLKIT_NOT_INSTALLED"
    PROMPT_TOOLKIT_TERMINAL_INCOMPATIBLE = "PROMPT_TOOLKIT_TERMINAL_INCOMPATIBLE"
    PROMPT_TOOLKIT_BACKEND_ERROR = "PROMPT_TOOLKIT_BACKEND_ERROR"
    NOT_WINDOWS_CONSOLE = "NOT_WINDOWS_CONSOLE"
    NO_INTERACTIVE_CONSOLE = "NO_INTERACTIVE_CONSOLE"
    BACKEND_START_FAILED = "BACKEND_START_FAILED"
    CONSOLE_INPUT_BUSY = "CONSOLE_INPUT_BUSY"


class BargeInBackendError(RuntimeError):
    """A backend could not start here. Carries a FallbackReason, never a key."""

    def __init__(self, reason: FallbackReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}{(': ' + detail) if detail else ''}")
        self.reason = reason


# The allowlisted interrupt keys. Escape and Ctrl+G only — never Ctrl+C (which stays
# the graceful-shutdown signal) and never a printable character.
_ESC = "\x1b"
_CTRL_G = "\x07"
_INTERRUPT_KEYS = frozenset({_ESC, _CTRL_G})
# Ctrl+C is named ONLY to state that it is deliberately not claimed: it remains the
# console's own SIGINT, so the interrupt key can never be confused with shutdown.
_CTRL_C = "\x03"

# The console-input ownership label this module claims. Exactly one input backend may
# hold it; the interactive line reader holds its own.
CONSOLE_OWNER_BARGE_IN = "barge_in"


def is_interrupt_key(key: str) -> bool:
    """True only for an allowlisted interrupt key. A raw key is never logged here."""
    return key in _INTERRUPT_KEYS


class KeyReader:
    """Abstract single-key active-console reader. Subclasses read ONLY this process's
    console. ``supported`` is False when the backend cannot run here.

    CONTRACT (M59.5): a reader forwards a key to ``on_key`` ONLY when it is on the
    interrupt allowlist. Everything else is counted (``ignored_keys``) and dropped
    inside the reader, so a non-interrupt keystroke never becomes observable data.
    """

    supported: bool = False
    backend: BargeInBackend = BargeInBackend.COMMAND_ONLY
    ignored_keys: int = 0
    restore_failures: int = 0

    def start(self, on_key: Callable[[str], None]) -> None:  # pragma: no cover - iface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def restore_terminal(self) -> None:
        """Restore any terminal mode this backend changed. Default: nothing to do."""
        return None

    def is_running(self) -> bool:
        """Whether a reader thread/task is still live. Used to prove no orphan."""
        return False

    # ── shared source-level allowlist filter ─────────────────────────────────
    def _forward(self, on_key: Callable[[str], None] | None, key: str) -> None:
        """Forward ONE key iff it is an allowlisted interrupt key. A non-interrupt
        key is counted and discarded HERE — it is never passed on, stored or logged."""
        if not key:
            return
        if not is_interrupt_key(key):
            self.ignored_keys += 1
            return
        if on_key is not None:
            on_key(key)


def _stdin_is_tty() -> bool:
    try:
        return bool(getattr(sys.stdin, "isatty", lambda: False)())
    except Exception:  # noqa: BLE001
        return False


def prompt_toolkit_available() -> bool:
    """Is ``prompt_toolkit`` importable on this host? Uses ``find_spec`` so an absent
    package costs nothing and a present one is not imported until it is actually used.
    Never raises."""
    try:
        return importlib.util.find_spec("prompt_toolkit") is not None
    except Exception:  # noqa: BLE001
        return False


class WindowsConsoleKeyReader(KeyReader):
    """msvcrt-based reader: polls THIS process's console buffer on a daemon thread
    while armed. Never blocks the event loop, never logs the key, changes no terminal
    mode (so nothing to restore), and stops cleanly. Only usable on Windows with a
    real console."""

    backend = BargeInBackend.WINDOWS_MSVCRT

    def __init__(self, poll_s: float = 0.02) -> None:
        self._poll_s = poll_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_key: Callable[[str], None] | None = None
        self.ignored_keys = 0
        self.restore_failures = 0
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
        return _stdin_is_tty()

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
                    # A Windows function/arrow key arrives as a two-part sequence:
                    # a NUL/0xE0 prefix followed by a scan code. The scan code must be
                    # consumed here, otherwise it surfaces as a second, meaningless
                    # "key" — and a scan code is never an interrupt.
                    if ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        self.ignored_keys += 1
                        continue
                    # Source-level allowlist: a non-interrupt key never leaves here.
                    self._forward(self._on_key, ch)
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

    def is_running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def restore_terminal(self) -> None:
        # msvcrt changes no terminal mode — nothing to restore. Present so the
        # interface guarantee ("terminal restored on stop/exception") holds for every
        # backend, including the raw prompt_toolkit one.
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  PORTABLE BACKEND — prompt_toolkit (optional, never mandatory)
# ══════════════════════════════════════════════════════════════════════════════
def _default_prompt_toolkit_input():
    """Create prompt_toolkit's console Input for THIS process's terminal.

    Imported lazily and only when the backend is actually selected, so a host without
    prompt_toolkit pays nothing. Returns None when it cannot be created — the caller
    then falls back; it never raises into boot.
    """
    try:  # pragma: no cover - exercised only where prompt_toolkit is installed
        from prompt_toolkit.input.defaults import create_input
        return create_input()
    except Exception:  # noqa: BLE001
        return None


def _keypress_to_interrupt(keypress) -> str:
    """Map ONE prompt_toolkit ``KeyPress`` to an allowlisted interrupt key, or "".

    Deliberately total and deliberately narrow: anything that is not Escape or Ctrl+G
    maps to the empty string, so the raw ``.data`` of an ordinary keystroke (including
    an accented Spanish character) is never returned, copied or retained.
    """
    data = getattr(keypress, "data", None)
    if isinstance(data, str) and data in _INTERRUPT_KEYS:
        return data
    key = getattr(keypress, "key", None)
    name = getattr(key, "value", None) or getattr(key, "name", None) or key
    token = str(name or "").strip().lower().replace("keys.", "").replace("-", "")
    if token in ("escape", "c\x1b", "\x1b"):
        return _ESC
    if token in ("cg", "controlg", "ctrlg"):
        return _CTRL_G
    return ""


class PromptToolkitKeyReader(KeyReader):
    """The portable active-console backend, built on ``prompt_toolkit``'s low-level
    ``Input`` — the same terminal this process already owns, never a global hook.

    It is OPTIONAL by construction: ``input_factory`` is injectable (tests drive it
    with a fake), and when prompt_toolkit is absent ``supported`` is False and the
    selection chain falls through to the Windows console reader or COMMAND_ONLY.

    This is the ONE backend that changes the terminal mode (raw mode), so it owns the
    matching guarantee: raw mode is exited on stop, on shutdown, and on ANY exception
    inside start or the reader thread. A restore that itself fails is counted, never
    raised.
    """

    backend = BargeInBackend.PROMPT_TOOLKIT

    def __init__(self, *, input_factory: Callable[[], object] | None = None,
                 poll_s: float = 0.02, enabled: bool = True,
                 tty_probe: Callable[[], bool] | None = None) -> None:
        self._poll_s = max(0.005, float(poll_s))
        self._injected = input_factory is not None
        self._factory = input_factory or _default_prompt_toolkit_input
        self._tty_probe = tty_probe or _stdin_is_tty
        self._enabled = bool(enabled)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_key: Callable[[str], None] | None = None
        self._input = None
        self._raw_ctx = None
        self._raw_entered = False
        self.ignored_keys = 0
        self.restore_failures = 0
        self.raw_mode_entries = 0
        self.raw_mode_exits = 0
        self.supported = self._detect()

    def _detect(self) -> bool:
        if not self._enabled:
            return False
        # An injected factory IS the availability proof (tests / an embedder).
        if not self._injected and not prompt_toolkit_available():
            return False
        return bool(self._tty_probe())

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, on_key: Callable[[str], None]) -> None:
        """Enter raw mode and spawn the daemon reader. Raises BargeInBackendError when
        the backend cannot start — and restores the terminal before it does, so a
        failed start never leaves the console in raw mode."""
        if not self.supported or self.is_running():
            return
        self._on_key = on_key
        self._stop.clear()
        try:
            self._input = self._factory()
            if self._input is None:
                raise BargeInBackendError(FallbackReason.PROMPT_TOOLKIT_BACKEND_ERROR,
                                          "no input")
            self._raw_ctx = self._input.raw_mode()
            self._raw_ctx.__enter__()
            self._raw_entered = True
            self.raw_mode_entries += 1
        except BargeInBackendError:
            self.restore_terminal()
            raise
        except Exception as exc:  # noqa: BLE001
            self.restore_terminal()
            raise BargeInBackendError(FallbackReason.PROMPT_TOOLKIT_BACKEND_ERROR,
                                      type(exc).__name__) from exc
        self._thread = threading.Thread(target=self._run, name="barge-in-key-pt",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        inp = self._input
        while not self._stop.is_set():
            try:
                keys = inp.read_keys() if inp is not None else ()
                if not keys:
                    time.sleep(self._poll_s)
                    continue
                for kp in keys:
                    # Mapped to the allowlist INSIDE the backend; a non-interrupt key
                    # never becomes a value anyone downstream can see.
                    mapped = _keypress_to_interrupt(kp)
                    if mapped:
                        self._forward(self._on_key, mapped)
                    else:
                        self.ignored_keys += 1
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

    def is_running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def restore_terminal(self) -> None:
        """Exit raw mode and close the input. Idempotent and never raises: a restore
        failure is counted so the health surface can show it."""
        ctx, self._raw_ctx = self._raw_ctx, None
        if ctx is not None and self._raw_entered:
            try:
                ctx.__exit__(None, None, None)
                self.raw_mode_exits += 1
            except Exception:  # noqa: BLE001
                self.restore_failures += 1
        self._raw_entered = False
        inp, self._input = self._input, None
        if inp is not None:
            try:
                close = getattr(inp, "close", None)
                if callable(close):
                    close()
            except Exception:  # noqa: BLE001
                self.restore_failures += 1

    @property
    def terminal_restored(self) -> bool:
        """True when no raw-mode change is currently outstanding."""
        return not self._raw_entered


# ══════════════════════════════════════════════════════════════════════════════
#  BACKEND SELECTION — deterministic, injectable, honest
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BackendSelection:
    """The resolved backend for THIS host. ``fallback_reason`` is NONE only when the
    preferred (or explicitly requested) backend was the one selected."""

    backend: BargeInBackend
    mode: BargeInMode
    reader: KeyReader | None
    fallback_reason: FallbackReason = FallbackReason.NONE
    portable_available: bool = False

    def snapshot(self) -> dict:
        return {"selected_backend": self.backend.value, "mode": self.mode.value,
                "fallback_reason": self.fallback_reason.value,
                "portable_backend_available": self.portable_available}


def select_backend(
    configured: str = "AUTO",
    *,
    portable_factory: Callable[[], KeyReader | None] | None = None,
    msvcrt_factory: Callable[[], KeyReader | None] | None = None,
    portable_available: Callable[[], bool] | None = None,
) -> BackendSelection:
    """Resolve the active-console backend deterministically.

    AUTO order — prompt_toolkit (installed + enabled + terminal-compatible), then a
    real Windows console (msvcrt), then COMMAND_ONLY. An explicitly requested backend
    is tried FIRST and, when it cannot run here, the chain continues while
    ``fallback_reason`` names the true cause: the operator sees both the backend that
    is actually running and why the requested one is not.

    Every probe is injectable, so the whole matrix is testable on any host without a
    terminal, without prompt_toolkit and without Windows.
    """
    want = str(configured or "AUTO").strip().upper().replace("-", "_")
    if want == "ACTIVE_CONSOLE_KEY":      # M58 alias: "pick the best key backend"
        want = "AUTO"
    if want not in {b.value for b in BargeInBackend}:
        want = "AUTO"

    available = (portable_available or prompt_toolkit_available)
    try:
        pt_installed = bool(available())
    except Exception:  # noqa: BLE001
        pt_installed = False

    if want == BargeInBackend.COMMAND_ONLY.value:
        return BackendSelection(
            BargeInBackend.COMMAND_ONLY, BargeInMode.COMMAND_ONLY, None,
            FallbackReason.OPERATOR_FORCED_COMMAND_ONLY, pt_installed)

    def _make(factory, default):
        try:
            return (factory or default)()
        except Exception:  # noqa: BLE001 — a probe never breaks selection
            return None

    # ── 1. prompt_toolkit ──
    reason = FallbackReason.NONE
    if want in (BargeInBackend.AUTO.value, BargeInBackend.PROMPT_TOOLKIT.value):
        if not pt_installed and portable_factory is None:
            reason = FallbackReason.PROMPT_TOOLKIT_NOT_INSTALLED
        else:
            pt = _make(portable_factory, PromptToolkitKeyReader)
            if pt is not None and getattr(pt, "supported", False):
                return BackendSelection(BargeInBackend.PROMPT_TOOLKIT,
                                        BargeInMode.ACTIVE_CONSOLE_KEY, pt,
                                        FallbackReason.NONE, True)
            reason = FallbackReason.PROMPT_TOOLKIT_TERMINAL_INCOMPATIBLE

    # ── 2. Windows console (msvcrt) ──
    ms = _make(msvcrt_factory, WindowsConsoleKeyReader)
    if ms is not None and getattr(ms, "supported", False):
        if reason is FallbackReason.NONE:
            # Reached without a prompt_toolkit attempt (explicit WINDOWS_MSVCRT).
            reason = FallbackReason.NONE
        return BackendSelection(BargeInBackend.WINDOWS_MSVCRT,
                                BargeInMode.ACTIVE_CONSOLE_KEY, ms, reason,
                                pt_installed)

    # ── 3. COMMAND_ONLY (the /stop fallback is ALWAYS functional) ──
    if reason is FallbackReason.NONE:
        reason = (FallbackReason.NOT_WINDOWS_CONSOLE
                  if not sys.platform.startswith("win")
                  else FallbackReason.NO_INTERACTIVE_CONSOLE)
    return BackendSelection(BargeInBackend.COMMAND_ONLY, BargeInMode.COMMAND_ONLY,
                            None, reason, pt_installed)


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
    # M59.5 — which backend is actually running, and why it is not a better one.
    backend: BargeInBackend = BargeInBackend.COMMAND_ONLY
    fallback_reason: FallbackReason = FallbackReason.NONE
    portable_backend_available: bool = False
    # Console-input ownership hooks: injectable so the whole ownership invariant is
    # testable without a terminal. Default to the process ConsoleInputOwnership.
    acquire_console: Callable[[str], bool] | None = None
    release_console: Callable[[str], None] | None = None

    _armed: bool = field(default=False)
    active_interruptions: int = 0
    command_interruptions: int = 0
    ignored_no_active_turn: int = 0
    cancellation_latency_ms: float | None = None
    terminal_restore_failures: int = 0
    # M59.5 — reader accounting. orphan_reader_count is (started - stopped): a
    # nonzero value is a LEAKED input reader, which is the exact failure this
    # milestone must be able to prove absent.
    _readers_started: int = 0
    _readers_stopped: int = 0
    arm_failures: int = 0
    console_busy_denials: int = 0

    @property
    def supported(self) -> bool:
        return self.mode is BargeInMode.ACTIVE_CONSOLE_KEY and bool(
            self.reader and getattr(self.reader, "supported", False))

    @property
    def orphan_reader_count(self) -> int:
        """Readers started but never stopped. Always 0 in a healthy runtime."""
        return max(0, self._readers_started - self._readers_stopped)

    @property
    def ignored_keys(self) -> int:
        """Non-interrupt keys the backend dropped at the source. A COUNT only — the
        key values themselves never leave the reader thread."""
        return int(getattr(self.reader, "ignored_keys", 0) or 0)

    # ── console-input ownership (exactly one backend at a time) ──────────────
    def _acquire_console(self) -> bool:
        fn = self.acquire_console
        if fn is None:
            try:
                from core.console import acquire_console_input
                fn = acquire_console_input
            except Exception:  # noqa: BLE001 — headless: ownership is uncontended
                return True
        try:
            return bool(fn(CONSOLE_OWNER_BARGE_IN))
        except Exception:  # noqa: BLE001
            return True

    def _release_console(self) -> None:
        fn = self.release_console
        if fn is None:
            try:
                from core.console import release_console_input
                fn = release_console_input
            except Exception:  # noqa: BLE001
                return
        try:
            fn(CONSOLE_OWNER_BARGE_IN)
        except Exception:  # noqa: BLE001
            pass

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
        supported and we are not stopping.

        Takes console-input ownership first: if another backend holds the console the
        arm is REFUSED (counted, never queued) so two readers can never consume the
        same keystrokes. A backend that fails to start restores the terminal, releases
        ownership and degrades this session to COMMAND_ONLY — /stop keeps working."""
        if self._armed or not self.supported or self.is_stopping():
            return
        if not self._acquire_console():
            self.console_busy_denials += 1
            self.fallback_reason = FallbackReason.CONSOLE_INPUT_BUSY
            return
        try:
            self.reader.start(self._on_key_threadsafe)
            self._armed = True
            self._readers_started += 1
        except BargeInBackendError as exc:
            self._armed = False
            self.arm_failures += 1
            self._degrade(exc.reason)
        except Exception:  # noqa: BLE001 — a backend fault never breaks a turn
            self._armed = False
            self.arm_failures += 1
            self._degrade(FallbackReason.BACKEND_START_FAILED)

    def _degrade(self, reason: FallbackReason) -> None:
        """Fall back safely after a backend error: restore the terminal, release the
        console, and drop to COMMAND_ONLY for the rest of the session. Never raises."""
        try:
            if self.reader is not None:
                self.reader.restore_terminal()
        except Exception:  # noqa: BLE001
            self.terminal_restore_failures += 1
        self._release_console()
        self.mode = BargeInMode.COMMAND_ONLY
        self.backend = BargeInBackend.COMMAND_ONLY
        self.fallback_reason = reason
        logger.warning(
            f"BARGE_IN: backend degraded to COMMAND_ONLY ({reason.value}); "
            "/stop remains the interrupt path")

    def disarm(self) -> None:
        """Disarm at the end of a turn. Always restores the terminal and releases
        console-input ownership; a restore failure is counted, never raised."""
        if not self._armed:
            return
        self._armed = False
        try:
            self.reader.stop()
            self._readers_stopped += 1
        except Exception:  # noqa: BLE001
            self.terminal_restore_failures += 1
            try:
                self.reader.restore_terminal()
                # The terminal was reclaimed, so the reader is not an orphan.
                self._readers_stopped += 1
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._release_console()

    def shutdown(self) -> None:
        """Close the input backend on shutdown: no reader thread survives, the terminal
        is restored and console ownership is released. Idempotent and never raises."""
        try:
            self.disarm()
        finally:
            if self.reader is not None:
                try:
                    # Belt-and-braces: a reader that was started outside arm() (or
                    # survived a failed disarm) is stopped here too. Only an actually
                    # live reader is counted, so orphan accounting stays honest.
                    was_running = bool(self.reader.is_running())
                    self.reader.stop()
                    if was_running and self.orphan_reader_count > 0:
                        self._readers_stopped += 1
                except Exception:  # noqa: BLE001
                    self.terminal_restore_failures += 1
                    try:
                        self.reader.restore_terminal()
                    except Exception:  # noqa: BLE001
                        pass
            self._release_console()

    def snapshot(self) -> dict:
        """Bounded, content-free barge-in health. Contains counts, milliseconds and
        enum names ONLY — never a key value, a prompt, an answer or typed text."""
        return {
            "mode": self.mode.value,
            "supported": self.supported,
            # ── M59.5 portable backend ──
            "selected_backend": self.backend.value,
            "portable_backend_available": bool(self.portable_backend_available),
            "fallback_reason": self.fallback_reason.value,
            "orphan_reader_count": self.orphan_reader_count,
            "arm_failures": self.arm_failures,
            "console_busy_denials": self.console_busy_denials,
            "ignored_keys": self.ignored_keys,
            # ── interruption accounting ──
            "active_interruptions": self.active_interruptions,
            "command_interruptions": self.command_interruptions,
            "ignored_no_active_turn": self.ignored_no_active_turn,
            "cancellation_latency_ms": self.cancellation_latency_ms,
            "terminal_restore_failures": self.terminal_restore_failures,
        }


def configured_backend() -> str:
    """The operator's requested backend from config. Never raises."""
    try:
        from core.config import settings
        return str(getattr(settings, "barge_in_mode", "AUTO") or "AUTO").upper()
    except Exception:  # noqa: BLE001
        return "AUTO"


def resolve_backend_selection() -> BackendSelection:
    """Resolve the active-console backend for THIS host from operator config."""
    return select_backend(configured_backend())


def resolve_backend_mode() -> tuple[BargeInMode, KeyReader | None]:
    """The M58 two-tuple form, preserved for existing callers. New code should use
    :func:`resolve_backend_selection`, which also carries the selected backend and the
    honest fallback reason."""
    sel = resolve_backend_selection()
    return sel.mode, sel.reader


# ── Process-global singleton ─────────────────────────────────────────────────
_controller: BargeInController | None = None


def get_barge_in_controller() -> BargeInController:
    """The process barge-in controller, built from host capabilities on first use.

    The interrupt action and turn-active predicate are wired to the response runtime
    and cancel bus, so a key interrupt does exactly what /stop does — immediately."""
    global _controller
    if _controller is None:
        sel = resolve_backend_selection()

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
            mode=sel.mode, reader=sel.reader, interrupt_action=_interrupt,
            is_turn_active=_turn_active, is_stopping=_stopping,
            backend=sel.backend, fallback_reason=sel.fallback_reason,
            portable_backend_available=sel.portable_available)
        logger.info(
            "BARGE_IN: backend={} mode={} portable_available={} fallback={}".format(
                sel.backend.value, sel.mode.value, sel.portable_available,
                sel.fallback_reason.value))
    return _controller


def reset_barge_in_controller(instance: BargeInController | None = None) -> None:
    """Tests / a fresh process. Releases any console-input ownership the outgoing
    controller still held, so a replaced controller can never leave the console
    claimed by a backend that no longer exists."""
    global _controller
    previous = _controller
    _controller = instance
    if previous is not None and previous is not instance:
        try:
            previous._release_console()
        except Exception:  # noqa: BLE001
            pass
