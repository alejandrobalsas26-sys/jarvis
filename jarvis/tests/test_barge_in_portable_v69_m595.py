"""V69 M59.5 — portable active-console barge-in. Deterministic, no real terminal.

Proves the whole backend matrix (prompt_toolkit -> Windows msvcrt -> COMMAND_ONLY),
the privacy guarantees (no global hook, no raw key anywhere observable), the terminal
restoration contract, single-owner console input, and the end-to-end interruption
semantics: the stream is cancelled, late chunks are suppressed, stale speech is
cancelled, the partial history stays truthful and the next turn still works.

Nothing here touches a real console, a real terminal mode or a real key.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from core.barge_in import (
    CONSOLE_OWNER_BARGE_IN,
    BargeInBackend,
    BargeInBackendError,
    BargeInController,
    BargeInMode,
    FallbackReason,
    KeyReader,
    PromptToolkitKeyReader,
    WindowsConsoleKeyReader,
    _keypress_to_interrupt,
    is_interrupt_key,
    reset_barge_in_controller,
    select_backend,
)
from core.console import (
    ConsoleChannel,
    ConsoleCoordinator,
    acquire_console_input,
    console_input_owner,
    get_console_input_ownership,
    release_console_input,
    reset_console_input_ownership,
)

_ESC = "\x1b"
_CTRL_G = "\x07"


@pytest.fixture(autouse=True)
def _clean_ownership():
    """Console-input ownership is process-global; a leak between tests would be
    indistinguishable from the real bug this milestone must detect."""
    reset_console_input_ownership()
    yield
    reset_barge_in_controller(None)
    reset_console_input_ownership()


# ══════════════════════════════════════════════════════════════════════════════
#  Fakes
# ══════════════════════════════════════════════════════════════════════════════
class FakeReader(KeyReader):
    """A backend that changes nothing and records everything."""

    def __init__(self, *, supported=True, raise_on_stop=False, raise_on_start=None,
                 backend=BargeInBackend.WINDOWS_MSVCRT):
        self.supported = supported
        self.backend = backend
        self.started = False
        self.stopped = False
        self.restored = False
        self.running = False
        self.ignored_keys = 0
        self.restore_failures = 0
        self._raise_on_stop = raise_on_stop
        self._raise_on_start = raise_on_start
        self._on_key = None

    def start(self, on_key):
        if self._raise_on_start is not None:
            raise self._raise_on_start
        self.started = True
        self.running = True
        self._on_key = on_key

    def stop(self):
        self.stopped = True
        self.running = False
        if self._raise_on_stop:
            raise RuntimeError("backend crash on stop")

    def restore_terminal(self):
        self.restored = True

    def is_running(self):
        return self.running

    def press(self, key):
        """Simulate a keystroke ARRIVING AT THE BACKEND — it goes through the same
        source-level allowlist filter a real reader uses."""
        self._forward(self._on_key, key)


class FakeRawContext:
    """Stands in for prompt_toolkit's ``raw_mode()`` context manager."""

    def __init__(self, owner, *, fail_exit=False):
        self.owner = owner
        self.fail_exit = fail_exit

    def __enter__(self):
        self.owner.raw = True
        return self

    def __exit__(self, *exc):
        if self.fail_exit:
            raise RuntimeError("cannot restore terminal")
        self.owner.raw = False
        return False


class FakePTInput:
    """Stands in for a prompt_toolkit ``Input``: raw_mode() + read_keys() + close()."""

    def __init__(self, *, fail_raw=False, fail_exit=False):
        self.raw = False
        self.closed = False
        self._queue: list = []
        self._lock = threading.Lock()
        self._fail_raw = fail_raw
        self._fail_exit = fail_exit

    def raw_mode(self):
        if self._fail_raw:
            raise RuntimeError("no tty")
        return FakeRawContext(self, fail_exit=self._fail_exit)

    def read_keys(self):
        with self._lock:
            out, self._queue = self._queue, []
        return out

    def close(self):
        self.closed = True

    def feed(self, *keypresses):
        with self._lock:
            self._queue.extend(keypresses)


class FakeKeyPress:
    def __init__(self, data="", key=""):
        self.data = data
        self.key = key


def _pt_reader(**kw):
    inp = kw.pop("inp", None) or FakePTInput()
    reader = PromptToolkitKeyReader(input_factory=lambda: inp, poll_s=0.005,
                                    tty_probe=lambda: True, **kw)
    return reader, inp


def _ctrl(*, active=True, stopping=False, reader=None, calls=None,
          mode=BargeInMode.ACTIVE_CONSOLE_KEY, backend=BargeInBackend.WINDOWS_MSVCRT):
    calls = [] if calls is None else calls
    reader = reader if reader is not None else FakeReader()
    ctrl = BargeInController(
        mode=mode, reader=reader, backend=backend,
        interrupt_action=lambda: calls.append("fired"),
        is_turn_active=lambda: active, is_stopping=lambda: stopping)
    return ctrl, calls, reader


# ══════════════════════════════════════════════════════════════════════════════
#  1. Backend selection matrix
# ══════════════════════════════════════════════════════════════════════════════
def test_auto_selects_prompt_toolkit_when_available():
    pt = FakeReader(supported=True, backend=BargeInBackend.PROMPT_TOOLKIT)
    sel = select_backend("AUTO", portable_factory=lambda: pt,
                         msvcrt_factory=lambda: FakeReader(supported=True),
                         portable_available=lambda: True)
    assert sel.backend is BargeInBackend.PROMPT_TOOLKIT
    assert sel.mode is BargeInMode.ACTIVE_CONSOLE_KEY
    assert sel.reader is pt
    assert sel.fallback_reason is FallbackReason.NONE
    assert sel.portable_available is True


def test_auto_falls_back_to_windows_msvcrt():
    ms = FakeReader(supported=True)
    sel = select_backend("AUTO", portable_factory=None,
                         msvcrt_factory=lambda: ms,
                         portable_available=lambda: False)
    assert sel.backend is BargeInBackend.WINDOWS_MSVCRT
    assert sel.mode is BargeInMode.ACTIVE_CONSOLE_KEY
    assert sel.reader is ms
    # The fallback is NAMED, not hidden.
    assert sel.fallback_reason is FallbackReason.PROMPT_TOOLKIT_NOT_INSTALLED
    assert sel.portable_available is False


def test_auto_falls_back_to_command_only():
    sel = select_backend("AUTO", portable_factory=None,
                         msvcrt_factory=lambda: FakeReader(supported=False),
                         portable_available=lambda: False)
    assert sel.backend is BargeInBackend.COMMAND_ONLY
    assert sel.mode is BargeInMode.COMMAND_ONLY
    assert sel.reader is None
    assert sel.fallback_reason is FallbackReason.PROMPT_TOOLKIT_NOT_INSTALLED


def test_prompt_toolkit_installed_but_terminal_incompatible_degrades_honestly():
    """Installed yet unusable (redirected stdin) is a DIFFERENT fact from absent, and
    the operator must be able to tell them apart."""
    sel = select_backend("AUTO",
                         portable_factory=lambda: FakeReader(supported=False),
                         msvcrt_factory=lambda: FakeReader(supported=True),
                         portable_available=lambda: True)
    assert sel.backend is BargeInBackend.WINDOWS_MSVCRT
    assert sel.fallback_reason is FallbackReason.PROMPT_TOOLKIT_TERMINAL_INCOMPATIBLE
    assert sel.portable_available is True


def test_explicit_unsupported_backend_degrades_honestly():
    """An explicitly requested PROMPT_TOOLKIT that cannot run here keeps interruption
    working via msvcrt AND reports the true reason it is not the requested backend."""
    sel = select_backend("PROMPT_TOOLKIT", portable_factory=None,
                         msvcrt_factory=lambda: FakeReader(supported=True),
                         portable_available=lambda: False)
    assert sel.backend is BargeInBackend.WINDOWS_MSVCRT
    assert sel.fallback_reason is FallbackReason.PROMPT_TOOLKIT_NOT_INSTALLED
    assert sel.mode is BargeInMode.ACTIVE_CONSOLE_KEY


def test_explicit_windows_backend_never_probes_prompt_toolkit():
    probed = []
    sel = select_backend("WINDOWS_MSVCRT",
                         portable_factory=lambda: probed.append(1),
                         msvcrt_factory=lambda: FakeReader(supported=True),
                         portable_available=lambda: True)
    assert probed == []
    assert sel.backend is BargeInBackend.WINDOWS_MSVCRT
    assert sel.fallback_reason is FallbackReason.NONE


def test_operator_forced_command_only_is_terminal():
    sel = select_backend("COMMAND_ONLY",
                         portable_factory=lambda: FakeReader(supported=True),
                         msvcrt_factory=lambda: FakeReader(supported=True),
                         portable_available=lambda: True)
    assert sel.backend is BargeInBackend.COMMAND_ONLY
    assert sel.reader is None
    assert sel.fallback_reason is FallbackReason.OPERATOR_FORCED_COMMAND_ONLY


def test_m58_alias_and_unknown_values_resolve_to_auto():
    for value in ("ACTIVE_CONSOLE_KEY", "garbage", "", None):
        sel = select_backend(value, portable_factory=None,
                             msvcrt_factory=lambda: FakeReader(supported=True),
                             portable_available=lambda: False)
        assert sel.backend is BargeInBackend.WINDOWS_MSVCRT


def test_a_raising_probe_never_breaks_selection():
    def boom():
        raise RuntimeError("probe exploded")
    sel = select_backend("AUTO", portable_factory=boom, msvcrt_factory=boom,
                         portable_available=boom)
    assert sel.backend is BargeInBackend.COMMAND_ONLY
    assert sel.mode is BargeInMode.COMMAND_ONLY


def test_selection_snapshot_is_content_free():
    sel = select_backend("AUTO", portable_factory=None,
                         msvcrt_factory=lambda: FakeReader(supported=True),
                         portable_available=lambda: False)
    snap = sel.snapshot()
    assert set(snap) == {"selected_backend", "mode", "fallback_reason",
                         "portable_backend_available"}
    assert all(isinstance(v, (str, bool)) for v in snap.values())


# ══════════════════════════════════════════════════════════════════════════════
#  2. Privacy — no global hook, no raw key anywhere
# ══════════════════════════════════════════════════════════════════════════════
_FORBIDDEN_INPUT_MODULES = frozenset({
    "keyboard", "pynput", "pyhook", "pyxhook", "win32api", "win32con", "ctypes",
    "evdev", "Xlib", "keyboardlayout",
})
_FORBIDDEN_INPUT_CALLS = frozenset({
    "SetWindowsHookEx", "RegisterHotKey", "add_hotkey", "GetAsyncKeyState",
    "GetKeyState", "hook_keyboard", "on_press", "Listener",
})


def test_no_global_keyboard_hook_or_keylogging_api_is_used():
    """AST-level proof (not a substring scan of prose): the module imports NO global
    input library and calls NO OS-wide hook / hotkey / key-state API. The only inputs
    it may take are this process's own console — msvcrt and prompt_toolkit."""
    import ast
    import inspect

    import core.barge_in as mod
    tree = ast.parse(inspect.getsource(mod))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                called.add(name)
    assert not (imported & _FORBIDDEN_INPUT_MODULES), (
        f"forbidden global-input module imported: {imported & _FORBIDDEN_INPUT_MODULES}")
    assert not (called & _FORBIDDEN_INPUT_CALLS), (
        f"forbidden global-input API called: {called & _FORBIDDEN_INPUT_CALLS}")
    # The console-local backends ARE expected to be there.
    assert {"msvcrt", "prompt_toolkit"} <= imported


def test_non_interrupt_keys_never_leave_the_backend():
    """The source-level allowlist is the privacy guarantee: an ordinary keystroke is
    counted and dropped INSIDE the reader, so no callback ever sees its value."""
    seen: list = []
    reader = FakeReader()
    reader.start(seen.append)
    for key in ("a", "n", "\n", "\t", "1", "\x03"):
        reader.press(key)
    assert seen == []
    assert reader.ignored_keys == 6
    reader.press(_ESC)
    assert seen == [_ESC]


def test_spanish_and_unicode_input_is_preserved_and_never_captured():
    """Accented Spanish characters are NOT interrupts and are never forwarded, stored
    or logged — so ordinary Spanish typing is unaffected by the backend."""
    seen: list = []
    reader = FakeReader()
    reader.start(seen.append)
    for ch in "áéíóúñÑüàçΩ漢":
        assert is_interrupt_key(ch) is False
        reader.press(ch)
    assert seen == []
    assert reader.ignored_keys == len("áéíóúñÑüàçΩ漢")


def test_ctrl_c_is_never_claimed_as_an_interrupt_key():
    ctrl, calls, _ = _ctrl(active=True)
    assert ctrl.notify_key("\x03") is False
    assert calls == []


def test_snapshot_has_no_key_values_and_only_bounded_types():
    ctrl, _, reader = _ctrl(active=True)
    ctrl.notify_key(_ESC)
    reader.press("ñ")
    snap = ctrl.snapshot()
    blob = repr(snap)
    assert _ESC not in blob and _CTRL_G not in blob and "ñ" not in blob
    assert snap["selected_backend"] == "WINDOWS_MSVCRT"
    assert snap["ignored_keys"] == 1        # a COUNT, never the key itself
    for value in snap.values():
        assert isinstance(value, (str, bool, int, float, type(None)))


# ══════════════════════════════════════════════════════════════════════════════
#  3. Active vs idle key
# ══════════════════════════════════════════════════════════════════════════════
def test_active_key_cancels_active_generation():
    ctrl, calls, reader = _ctrl(active=True)
    ctrl.arm()
    reader.press(_CTRL_G)
    assert calls == ["fired"]
    assert ctrl.active_interruptions == 1
    assert ctrl.cancellation_latency_ms is not None
    ctrl.disarm()


def test_idle_key_does_not_stop_the_runtime():
    ctrl, calls, reader = _ctrl(active=False)
    ctrl.arm()
    reader.press(_ESC)
    reader.press(_CTRL_G)
    assert calls == []
    assert ctrl.ignored_no_active_turn == 2
    # and the runtime is still perfectly usable afterwards
    ctrl.is_turn_active = lambda: True
    reader.press(_ESC)
    assert calls == ["fired"]
    ctrl.disarm()


def test_no_interrupt_after_stopping():
    ctrl, calls, reader = _ctrl(active=True, stopping=True)
    ctrl.arm()               # refuses to arm while stopping
    assert reader.started is False
    assert ctrl.notify_key(_ESC) is False
    assert calls == []


# ══════════════════════════════════════════════════════════════════════════════
#  4. End-to-end interruption semantics (against the real ResponseRuntime)
# ══════════════════════════════════════════════════════════════════════════════
def _fresh_runtime():
    from core.response_runtime import ResponseRuntime, reset_response_runtime
    rr = ResponseRuntime()
    reset_response_runtime(rr)
    return rr


def test_interruption_marks_turn_and_suppresses_late_chunks():
    from core.response_runtime import TurnState, reset_response_runtime
    rr = _fresh_runtime()
    try:
        handle = rr.begin_turn(language="es")
        assert rr.accepts(handle.turn_id) is True      # live output flows
        cancelled = {"llm": 0, "tts": 0}

        def _interrupt():
            cancelled["llm"] += 1
            cancelled["tts"] += 1
            if rr.current is not None and rr.current.is_active():
                rr.end_turn(TurnState.INTERRUPTED_BY_OPERATOR)

        ctrl = BargeInController(
            mode=BargeInMode.ACTIVE_CONSOLE_KEY, reader=FakeReader(),
            backend=BargeInBackend.WINDOWS_MSVCRT, interrupt_action=_interrupt,
            is_turn_active=lambda: bool(rr.current and rr.current.is_active()))
        assert ctrl.notify_key(_ESC) is True
        # stream + stale speech cancelled exactly once
        assert cancelled == {"llm": 1, "tts": 1}
        # history is truthful: INTERRUPTED, not COMPLETED
        assert handle.state is TurnState.INTERRUPTED_BY_OPERATOR
        assert rr.interrupted_turns == 1 and rr.turns_completed == 0
        # every late chunk from that turn is refused and counted
        before = rr.late_chunks_suppressed
        assert rr.accepts(handle.turn_id) is False
        assert rr.accepts(handle.turn_id) is False
        assert rr.late_chunks_suppressed == before + 2
    finally:
        reset_response_runtime(None)


def test_a_completed_end_turn_after_interruption_cannot_rewrite_history():
    """The turn loop's `finally` still calls end_turn(COMPLETED). It must NOT be able
    to overwrite the truthful INTERRUPTED state the key already recorded."""
    from core.response_runtime import TurnState, reset_response_runtime
    rr = _fresh_runtime()
    try:
        handle = rr.begin_turn(language="es")
        rr.end_turn(TurnState.INTERRUPTED_BY_OPERATOR)
        rr.end_turn(TurnState.COMPLETED)          # the loop's finally
        assert handle.state is TurnState.INTERRUPTED_BY_OPERATOR
        assert rr.turns_completed == 0
    finally:
        reset_response_runtime(None)


def test_next_turn_works_after_an_interruption():
    from core.response_runtime import TurnState, reset_response_runtime
    rr = _fresh_runtime()
    try:
        first = rr.begin_turn(language="es")
        rr.end_turn(TurnState.INTERRUPTED_BY_OPERATOR)
        second = rr.begin_turn(language="es")
        assert second.turn_id != first.turn_id
        assert rr.accepts(second.turn_id) is True
        assert rr.accepts(first.turn_id) is False
        rr.end_turn(TurnState.COMPLETED)
        assert rr.turns_completed == 1 and rr.interrupted_turns == 1
    finally:
        reset_response_runtime(None)


def test_interrupt_action_exception_is_swallowed_and_latency_still_recorded():
    def boom():
        raise RuntimeError("teardown failed")
    ctrl = BargeInController(mode=BargeInMode.ACTIVE_CONSOLE_KEY,
                             reader=FakeReader(), interrupt_action=boom,
                             is_turn_active=lambda: True)
    assert ctrl.notify_key(_ESC) is True
    assert ctrl.cancellation_latency_ms is not None


# ══════════════════════════════════════════════════════════════════════════════
#  5. Console ownership — exactly one input backend at a time
# ══════════════════════════════════════════════════════════════════════════════
def test_console_input_ownership_is_exclusive_and_reentrant():
    assert acquire_console_input("line_reader") is True
    assert acquire_console_input("line_reader") is True       # re-entrant
    assert acquire_console_input(CONSOLE_OWNER_BARGE_IN) is False
    assert console_input_owner() == "line_reader"
    release_console_input(CONSOLE_OWNER_BARGE_IN)             # foreign: ignored
    assert console_input_owner() == "line_reader"
    assert get_console_input_ownership().foreign_releases == 1
    release_console_input("line_reader")
    assert console_input_owner() is None
    assert acquire_console_input(CONSOLE_OWNER_BARGE_IN) is True


def test_barge_in_refuses_to_become_a_second_reader():
    """While the line reader owns the console, the key backend must degrade rather
    than consume the same keystrokes."""
    acquire_console_input("line_reader")
    ctrl, _, reader = _ctrl(active=True)
    ctrl.arm()
    assert reader.started is False
    assert ctrl.console_busy_denials == 1
    assert ctrl.fallback_reason is FallbackReason.CONSOLE_INPUT_BUSY
    release_console_input("line_reader")
    ctrl.arm()
    assert reader.started is True
    ctrl.disarm()
    assert console_input_owner() is None


def test_no_duplicate_readers_across_arm_calls():
    ctrl, _, reader = _ctrl(active=True)
    ctrl.arm()
    ctrl.arm()
    ctrl.arm()
    assert ctrl.orphan_reader_count == 1     # exactly ONE live reader
    ctrl.disarm()
    assert ctrl.orphan_reader_count == 0


def test_disarm_releases_ownership_so_the_line_reader_can_read():
    ctrl, _, _ = _ctrl(active=True)
    ctrl.arm()
    assert console_input_owner() == CONSOLE_OWNER_BARGE_IN
    ctrl.disarm()
    assert acquire_console_input("line_reader") is True


# ══════════════════════════════════════════════════════════════════════════════
#  6. Terminal restoration, backend errors, shutdown, orphan readers
# ══════════════════════════════════════════════════════════════════════════════
def test_backend_start_crash_restores_terminal_and_degrades_safely():
    reader = FakeReader(raise_on_start=BargeInBackendError(
        FallbackReason.PROMPT_TOOLKIT_BACKEND_ERROR, "no tty"))
    ctrl, calls, _ = _ctrl(active=True, reader=reader,
                           backend=BargeInBackend.PROMPT_TOOLKIT)
    ctrl.arm()
    assert reader.restored is True                       # terminal reclaimed
    assert ctrl.arm_failures == 1
    assert ctrl.mode is BargeInMode.COMMAND_ONLY         # safe fallback
    assert ctrl.backend is BargeInBackend.COMMAND_ONLY
    assert ctrl.fallback_reason is FallbackReason.PROMPT_TOOLKIT_BACKEND_ERROR
    assert ctrl.orphan_reader_count == 0                 # nothing left running
    assert console_input_owner() is None                 # ownership released
    # /stop still records interruptions after the degradation
    ctrl.note_command_interrupt()
    assert ctrl.command_interruptions == 1


def test_generic_backend_start_error_degrades_with_a_named_reason():
    reader = FakeReader(raise_on_start=RuntimeError("boom"))
    ctrl, _, _ = _ctrl(active=True, reader=reader)
    ctrl.arm()
    assert ctrl.fallback_reason is FallbackReason.BACKEND_START_FAILED
    assert ctrl.mode is BargeInMode.COMMAND_ONLY


def test_terminal_restored_when_backend_stop_raises():
    reader = FakeReader(raise_on_stop=True)
    ctrl, _, _ = _ctrl(active=True, reader=reader)
    ctrl.arm()
    ctrl.disarm()
    assert ctrl.terminal_restore_failures == 1
    assert reader.restored is True
    assert ctrl.orphan_reader_count == 0
    assert console_input_owner() is None


def test_shutdown_closes_the_backend_and_leaves_no_orphan():
    ctrl, _, reader = _ctrl(active=True)
    ctrl.arm()
    ctrl.shutdown()
    assert reader.stopped is True
    assert reader.running is False
    assert ctrl.orphan_reader_count == 0
    assert console_input_owner() is None
    ctrl.shutdown()                       # idempotent
    assert ctrl.orphan_reader_count == 0


def test_disarm_is_idempotent_so_the_next_turn_works():
    ctrl, calls, reader = _ctrl(active=True)
    ctrl.arm()
    ctrl.disarm()
    ctrl.disarm()
    ctrl.arm()
    reader.press(_ESC)
    assert calls == ["fired"]
    ctrl.disarm()


# ══════════════════════════════════════════════════════════════════════════════
#  7. The prompt_toolkit backend (driven entirely through fakes)
# ══════════════════════════════════════════════════════════════════════════════
def test_prompt_toolkit_reader_is_unsupported_without_the_package():
    r = PromptToolkitKeyReader(tty_probe=lambda: True)
    # prompt_toolkit is not installed on this host; the reader must say so instead of
    # pretending to work.
    from core.barge_in import prompt_toolkit_available
    assert r.supported is prompt_toolkit_available()


def test_prompt_toolkit_reader_is_unsupported_without_a_tty():
    reader, _ = _pt_reader()
    assert reader.supported is True
    off = PromptToolkitKeyReader(input_factory=FakePTInput, tty_probe=lambda: False)
    assert off.supported is False


def test_prompt_toolkit_reader_enters_and_exits_raw_mode():
    reader, inp = _pt_reader()
    seen: list = []
    reader.start(seen.append)
    try:
        assert inp.raw is True
        assert reader.is_running() is True
        inp.feed(FakeKeyPress(data=_ESC))
        _wait_for(lambda: seen)
    finally:
        reader.stop()
    assert seen == [_ESC]
    assert inp.raw is False              # terminal restored
    assert inp.closed is True
    assert reader.is_running() is False  # no orphan thread
    assert reader.terminal_restored is True
    assert reader.raw_mode_entries == reader.raw_mode_exits == 1


def test_prompt_toolkit_reader_drops_non_interrupt_keys_at_the_source():
    reader, inp = _pt_reader()
    seen: list = []
    reader.start(seen.append)
    try:
        inp.feed(FakeKeyPress(data="ñ"), FakeKeyPress(data="a"),
                 FakeKeyPress(key="Keys.Up"), FakeKeyPress(data="\x03"))
        _wait_for(lambda: reader.ignored_keys >= 4)
    finally:
        reader.stop()
    assert seen == []
    assert inp.raw is False


def test_prompt_toolkit_reader_maps_named_keys():
    assert _keypress_to_interrupt(FakeKeyPress(data=_ESC)) == _ESC
    assert _keypress_to_interrupt(FakeKeyPress(key="escape")) == _ESC
    assert _keypress_to_interrupt(FakeKeyPress(key="Keys.ControlG")) == _CTRL_G
    assert _keypress_to_interrupt(FakeKeyPress(key="c-g")) == _CTRL_G
    # everything else — including a raw printable — maps to nothing
    assert _keypress_to_interrupt(FakeKeyPress(data="ñ")) == ""
    assert _keypress_to_interrupt(FakeKeyPress(data="\x03")) == ""
    assert _keypress_to_interrupt(FakeKeyPress(key="Keys.Up")) == ""
    assert _keypress_to_interrupt(object()) == ""


def test_prompt_toolkit_start_failure_restores_terminal_and_raises_typed_error():
    inp = FakePTInput(fail_raw=True)
    reader, _ = _pt_reader(inp=inp)
    with pytest.raises(BargeInBackendError) as exc:
        reader.start(lambda k: None)
    assert exc.value.reason is FallbackReason.PROMPT_TOOLKIT_BACKEND_ERROR
    assert inp.raw is False
    assert reader.is_running() is False
    assert reader.terminal_restored is True


def test_prompt_toolkit_missing_input_raises_typed_error():
    reader = PromptToolkitKeyReader(input_factory=lambda: None,
                                    tty_probe=lambda: True)
    with pytest.raises(BargeInBackendError):
        reader.start(lambda k: None)


def test_prompt_toolkit_restore_failure_is_counted_never_raised():
    inp = FakePTInput(fail_exit=True)
    reader, _ = _pt_reader(inp=inp)
    reader.start(lambda k: None)
    reader.stop()                       # must not raise
    assert reader.restore_failures >= 1
    assert reader.terminal_restored is True   # never left "still in raw mode"


def test_prompt_toolkit_reader_survives_a_faulting_input():
    class Exploding(FakePTInput):
        def read_keys(self):
            raise RuntimeError("terminal went away")

    inp = Exploding()
    reader, _ = _pt_reader(inp=inp)
    reader.start(lambda k: None)
    time.sleep(0.05)
    assert reader.is_running() is True    # a read fault never kills the reader
    reader.stop()
    assert reader.is_running() is False
    assert inp.raw is False


def test_prompt_toolkit_backend_through_the_controller_end_to_end():
    reader, inp = _pt_reader()
    calls: list = []
    ctrl = BargeInController(
        mode=BargeInMode.ACTIVE_CONSOLE_KEY, reader=reader,
        backend=BargeInBackend.PROMPT_TOOLKIT, portable_backend_available=True,
        interrupt_action=lambda: calls.append("fired"),
        is_turn_active=lambda: True)
    ctrl.arm()
    try:
        inp.feed(FakeKeyPress(data=_CTRL_G))
        _wait_for(lambda: calls)
    finally:
        ctrl.disarm()
    assert calls == ["fired"]
    assert ctrl.active_interruptions == 1
    assert ctrl.orphan_reader_count == 0
    assert inp.raw is False
    snap = ctrl.snapshot()
    assert snap["selected_backend"] == "PROMPT_TOOLKIT"
    assert snap["portable_backend_available"] is True


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError("condition not reached within the bound")


# ══════════════════════════════════════════════════════════════════════════════
#  8. The Windows console reader
# ══════════════════════════════════════════════════════════════════════════════
def test_windows_reader_reports_its_backend_and_restores_nothing():
    r = WindowsConsoleKeyReader()
    assert r.backend is BargeInBackend.WINDOWS_MSVCRT
    assert r.restore_terminal() is None       # it changes no terminal mode
    assert r.is_running() is False
    r.stop()                                   # safe when never started
    assert r.is_running() is False


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="msvcrt detection is Windows-only")
def test_windows_reader_detection_requires_a_real_console():
    assert isinstance(WindowsConsoleKeyReader().supported, bool)


# ══════════════════════════════════════════════════════════════════════════════
#  9. ConsoleCoordinator stays coherent around an interruption
# ══════════════════════════════════════════════════════════════════════════════
class _Sink:
    def __init__(self):
        self.buf: list = []

    def write(self, s):
        self.buf.append(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    @property
    def text(self):
        return "".join(self.buf)


def test_console_redraw_stays_coherent_and_restores_the_prompt_once():
    """After an interruption the coordinator must leave exactly ONE prompt on screen —
    not a duplicate, and not a prompt smeared into the partial answer."""
    sink = _Sink()
    console = ConsoleCoordinator(stream=sink)
    console.begin_stream()
    console.post("Los factores son ", ConsoleChannel.ASSISTANT)
    console.post("2, 3 y 5", ConsoleChannel.ASSISTANT)
    console.render_now()
    console.end_stream()
    # the operator interrupts: a warning line arrives, then the prompt comes back
    console.post("BARGE_IN: generacion interrumpida", ConsoleChannel.WARNING)
    console.set_prompt("Tu: ")
    console.render_now()
    text = sink.text
    assert "Los factores son 2, 3 y 5" in text        # partial text RETAINED
    assert text.count("Tu: ") == 1                     # prompt restored exactly once
    assert console.metrics()["prompt_active"] is True


def test_console_unicode_output_is_preserved_through_the_coordinator():
    sink = _Sink()
    console = ConsoleCoordinator(stream=sink)
    console.post("Explicación con acentos: ñ, á, ü", ConsoleChannel.ASSISTANT)
    console.render_now()
    assert "Explicación con acentos: ñ, á, ü" in sink.text
