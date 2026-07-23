"""V69 M58.8/.8.1 — active-console barge-in. Deterministic, no real terminal."""
from __future__ import annotations

from core.barge_in import (
    BargeInController,
    BargeInMode,
    KeyReader,
    is_interrupt_key,
)


class FakeReader(KeyReader):
    def __init__(self, *, supported=True, raise_on_stop=False):
        self.supported = supported
        self.started = False
        self.stopped = False
        self.restored = False
        self._raise_on_stop = raise_on_stop
        self._on_key = None

    def start(self, on_key):
        self.started = True
        self._on_key = on_key

    def stop(self):
        self.stopped = True
        if self._raise_on_stop:
            raise RuntimeError("backend crash on stop")

    def restore_terminal(self):
        self.restored = True

    def press(self, key):
        if self._on_key:
            self._on_key(key)


def _ctrl(*, active=True, stopping=False, supported=True, reader=None, calls=None):
    calls = [] if calls is None else calls
    reader = reader or FakeReader(supported=supported)
    return BargeInController(
        mode=BargeInMode.ACTIVE_CONSOLE_KEY if supported else BargeInMode.COMMAND_ONLY,
        reader=reader,
        interrupt_action=lambda: calls.append("fired"),
        is_turn_active=lambda: active,
        is_stopping=lambda: stopping,
    ), calls, reader


# ── allowlist ─────────────────────────────────────────────────────────────────
def test_only_esc_and_ctrl_g_are_interrupt_keys():
    assert is_interrupt_key("\x1b") is True   # Esc
    assert is_interrupt_key("\x07") is True   # Ctrl+G
    assert is_interrupt_key("\x03") is False  # Ctrl+C stays shutdown
    assert is_interrupt_key("a") is False
    assert is_interrupt_key("\n") is False


# ── key interrupts the active turn ────────────────────────────────────────────
def test_key_interrupts_active_generation():
    ctrl, calls, _ = _ctrl(active=True)
    assert ctrl.notify_key("\x1b") is True
    assert calls == ["fired"]
    assert ctrl.active_interruptions == 1
    assert ctrl.cancellation_latency_ms is not None


def test_no_active_turn_does_not_stop_the_app():
    ctrl, calls, _ = _ctrl(active=False)
    assert ctrl.notify_key("\x1b") is False
    assert calls == []  # interrupt action never fired
    assert ctrl.ignored_no_active_turn == 1


def test_printable_key_never_interrupts():
    ctrl, calls, _ = _ctrl(active=True)
    assert ctrl.notify_key("h") is False
    assert calls == []


def test_no_interrupt_after_stopping():
    ctrl, calls, _ = _ctrl(active=True, stopping=True)
    assert ctrl.notify_key("\x1b") is False
    assert calls == []


# ── arm/disarm lifecycle ──────────────────────────────────────────────────────
def test_arm_starts_reader_and_disarm_stops_and_restores():
    ctrl, calls, reader = _ctrl(active=True)
    ctrl.arm()
    assert reader.started is True
    # a key routed through the live threadsafe path fires the interrupt
    reader.press("\x1b")
    assert calls == ["fired"]
    ctrl.disarm()
    assert reader.stopped is True


def test_disarm_is_idempotent_so_next_turn_works():
    ctrl, _, reader = _ctrl(active=True)
    ctrl.arm()
    ctrl.disarm()
    ctrl.disarm()  # no crash, idempotent
    # re-arm for the next turn
    ctrl.arm()
    assert reader.started is True


# ── crash recovery: terminal restoration on backend exception ─────────────────
def test_terminal_restored_when_backend_stop_raises():
    reader = FakeReader(supported=True, raise_on_stop=True)
    ctrl, _, _ = _ctrl(active=True, reader=reader)
    ctrl.arm()
    ctrl.disarm()  # stop() raises → must count failure AND call restore_terminal()
    assert ctrl.terminal_restore_failures == 1
    assert reader.restored is True


def test_shutdown_closes_the_backend():
    ctrl, _, reader = _ctrl(active=True)
    ctrl.arm()
    ctrl.shutdown()
    assert reader.stopped is True


# ── COMMAND_ONLY fallback ─────────────────────────────────────────────────────
def test_unsupported_backend_reports_command_only():
    ctrl, calls, _ = _ctrl(active=True, supported=False)
    assert ctrl.mode is BargeInMode.COMMAND_ONLY
    assert ctrl.supported is False
    # arm() is a no-op when unsupported; /stop fallback still records interruptions
    ctrl.arm()
    ctrl.note_command_interrupt()
    assert ctrl.command_interruptions == 1


# ── privacy: no raw key in health ─────────────────────────────────────────────
def test_snapshot_is_content_free_and_has_no_key_values():
    ctrl, _, _ = _ctrl(active=True)
    ctrl.notify_key("\x1b")
    snap = ctrl.snapshot()
    blob = repr(snap)
    assert "\x1b" not in blob and "\x07" not in blob
    assert snap["mode"] == "ACTIVE_CONSOLE_KEY"
    assert "cancellation_latency_ms" in snap


def test_interrupt_action_exception_is_swallowed():
    def boom():
        raise RuntimeError("teardown failed")
    ctrl = BargeInController(mode=BargeInMode.ACTIVE_CONSOLE_KEY,
                             reader=FakeReader(), interrupt_action=boom,
                             is_turn_active=lambda: True)
    # a failing teardown must not raise into the reader
    assert ctrl.notify_key("\x1b") is True
