"""
tests/test_console_v69.py — V69 M54.1 single console coordinator.

Locks the terminal-integrity guarantees the live run violated: background logs
cannot clobber the active input line, internal tool JSON never reaches the input
parser, low-value repeats coalesce, the queue is bounded, and errors/HITL prompts
are never dropped. Rendering is driven synchronously via `render_now()` — no
renderer thread, no real TTY.
"""
from __future__ import annotations

import io

from core.console import (
    ConsoleCoordinator,
    ConsoleChannel,
)


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a TTY so ANSI erase sequences are exercised."""

    def isatty(self) -> bool:
        return True


def _cc(tty: bool = True, max_queue: int = 512) -> ConsoleCoordinator:
    stream = FakeTTY() if tty else io.StringIO()
    return ConsoleCoordinator(stream=stream, max_queue=max_queue)


# ── Prompt protection (symptom #1) ────────────────────────────────────────────

def test_log_arriving_during_prompt_redraws_prompt():
    cc = _cc()
    cc.set_prompt("Tú: ")
    cc.post("THREAT_FEED: sync complete", ConsoleChannel.LOG)
    cc.render_now()
    out = cc.stream.getvalue()
    # The log line is present AND the prompt is redrawn after it, so the input line
    # is never left merged with a background log.
    assert "THREAT_FEED: sync complete" in out
    assert out.rstrip().endswith("Tú:")


def test_erase_sequence_used_on_tty():
    cc = _cc(tty=True)
    cc.set_prompt("Tú: ")
    cc.post("BOOT: memory online", ConsoleChannel.BOOT)
    cc.render_now()
    assert "\x1b[2K\r" in cc.stream.getvalue()   # ANSI erase-line emitted


def test_no_ansi_on_plain_stream():
    cc = _cc(tty=False)
    cc.set_prompt("Tú: ")
    cc.post("BOOT: memory online", ConsoleChannel.BOOT)
    cc.render_now()
    assert "\x1b[" not in cc.stream.getvalue()   # no control bytes on non-tty


# ── Coalescing low-value repeats ──────────────────────────────────────────────

def test_repeated_low_value_logs_coalesce():
    cc = _cc()
    for i in range(5):
        cc.post(f"MONITOR: heartbeat {i}", ConsoleChannel.LOG, coalesce_key="monitor")
    # All five collapsed to a single queued event (the last wins) before render.
    assert cc.metrics()["coalesced"] == 4
    cc.render_now()
    out = cc.stream.getvalue()
    assert "heartbeat 4" in out
    assert "heartbeat 0" not in out


def test_warning_never_coalesced():
    cc = _cc()
    cc.post("disk pressure", ConsoleChannel.WARNING, coalesce_key="w")
    cc.post("disk pressure", ConsoleChannel.WARNING, coalesce_key="w")
    assert cc.metrics()["coalesced"] == 0   # protected channels always distinct


# ── Bounded queue + drop policy ───────────────────────────────────────────────

def test_queue_bounded_drops_oldest_droppable():
    cc = _cc(max_queue=4)
    for i in range(4):
        cc.post(f"LOG {i}", ConsoleChannel.LOG)
    # Queue full; a new LOG evicts the oldest droppable.
    assert cc.post("LOG 4", ConsoleChannel.LOG) is True
    m = cc.metrics()
    assert m["queued"] == 4
    assert m["dropped"] == 1


def test_error_admitted_even_when_full():
    cc = _cc(max_queue=2)
    cc.post("LOG a", ConsoleChannel.LOG)
    cc.post("LOG b", ConsoleChannel.LOG)
    # A full queue still admits an ERROR (evicting an old droppable) — never lost.
    assert cc.post("boom", ConsoleChannel.ERROR) is True
    cc.render_now()
    assert "boom" in cc.stream.getvalue()


# ── Tool JSON never becomes input ─────────────────────────────────────────────

def test_tool_status_is_framed_line_not_prompt_echo():
    cc = _cc()
    cc.set_prompt("Tú: ")
    # A tool-call status is posted on the TOOL channel — it renders as its own
    # framed line and redraws the prompt; it can never be fed to the input parser
    # because producers post events, they do not write to the input stream.
    cc.post('{"name": "code_execute"}', ConsoleChannel.TOOL)
    cc.render_now()
    out = cc.stream.getvalue()
    assert '{"name": "code_execute"}' in out
    assert out.rstrip().endswith("Tú:")


# ── Assistant streaming stays inline ──────────────────────────────────────────

def test_assistant_stream_written_inline():
    cc = _cc()
    cc.begin_stream()
    cc.post("Hola", ConsoleChannel.ASSISTANT)
    cc.post(", ", ConsoleChannel.ASSISTANT)
    cc.post("Alejandro", ConsoleChannel.ASSISTANT)
    cc.render_now()
    assert "Hola, Alejandro" in cc.stream.getvalue()
    cc.end_stream()


# ── Metrics surface for health ────────────────────────────────────────────────

def test_metrics_report_drops_and_coalesced():
    cc = _cc(max_queue=2)
    cc.post("a", ConsoleChannel.LOG, coalesce_key="k")
    cc.post("b", ConsoleChannel.LOG, coalesce_key="k")   # coalesced
    m = cc.metrics()
    assert set(("queued", "dropped", "coalesced", "emitted", "max_queue")).issubset(m)
    assert m["coalesced"] == 1
