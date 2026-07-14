"""
core/host_time.py — V69 M54.8: one deterministic host-clock source.

The live run had JARVIS answer "No tengo acceso a la hora real" even though a
`get_datetime` tool reads the host clock (tools/executor.py:_tool_get_datetime).
The model simply chose not to call it and invented a refusal. Time, date, weekday,
timezone and uptime are host facts — the model may FORMAT them but must never
invent them or claim it cannot see them.

This module is the single grounding point. It reads the real system clock via an
injectable `clock` callable (default: timezone-aware `datetime.now().astimezone()`)
so tests can freeze time, formats a compact bilingual snapshot, and produces a
one-line system-prompt fact that is injected every turn so the model always has the
authoritative value in front of it. No network, no external time service.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

# Spanish weekday / month names so the formatted answer reads naturally without a
# locale dependency (strftime("%A") is locale-sensitive and unreliable on Windows).
_ES_WEEKDAYS = (
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
)
_ES_MONTHS = (
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _default_clock() -> datetime:
    """Real, timezone-aware local host time. astimezone() attaches the host's
    actual UTC offset so the value is unambiguously system-sourced."""
    return datetime.now().astimezone()


@dataclass(frozen=True)
class HostTime:
    """A read-only, deterministic snapshot of the host clock."""

    dt: datetime

    @property
    def iso(self) -> str:
        return self.dt.isoformat()

    @property
    def timezone(self) -> str:
        return self.dt.tzname() or ""

    @property
    def utc_offset(self) -> str:
        return self.dt.strftime("%z")

    @property
    def weekday_es(self) -> str:
        return _ES_WEEKDAYS[self.dt.weekday()]

    def date_es(self) -> str:
        return f"{self.weekday_es}, {self.dt.day} de {_ES_MONTHS[self.dt.month]} de {self.dt.year}"

    def time_hms(self) -> str:
        return self.dt.strftime("%H:%M:%S")

    def to_dict(self) -> dict:
        """Structured payload (mirrors executor._tool_get_datetime's shape)."""
        return {
            "date": self.date_es(),
            "time": self.time_hms(),
            "weekday": self.weekday_es,
            "timezone": self.timezone,
            "utc_offset": self.utc_offset,
            "iso": self.iso,
            "source": "host_system_clock",
        }

    def spanish_sentence(self) -> str:
        """A ready-to-speak Spanish statement of the current date+time."""
        return f"Son las {self.time_hms()} del {self.date_es()} ({self.timezone})."

    def prompt_line(self) -> str:
        """The compact, first-party fact injected into the system prompt every turn
        so the model never claims it lacks real-time access. It is authoritative:
        the model MUST use it verbatim for any time/date answer and MUST NOT say the
        time is unavailable."""
        return (
            "HOST CLOCK (authoritative, system-sourced — never say you lack real-time "
            f"access): current local time is {self.iso} ({self.timezone}). "
            "Use this exact value for any date/time/day question; format it for the "
            "user but never invent or refuse it."
        )


# Injectable clock so tests freeze time; production uses the real host clock.
_clock: Callable[[], datetime] = _default_clock


def set_clock(clock: Callable[[], datetime]) -> None:
    """Override the clock source (tests). Pass `set_clock(host_time._default_clock)`
    or call `reset_clock()` to restore the real host clock."""
    global _clock
    _clock = clock


def reset_clock() -> None:
    global _clock
    _clock = _default_clock


def now() -> HostTime:
    """Current host time as a deterministic snapshot."""
    return HostTime(_clock())


def host_time_prompt_line() -> str:
    """Convenience: the system-prompt grounding line for the current host time."""
    return now().prompt_line()
