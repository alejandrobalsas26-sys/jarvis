"""
tests/test_host_time_v69.py — V69 M54.8 deterministic host-clock grounding.

Locks that time/date come from the host clock, never the model, and that the
system-prompt fact forbids the "no tengo acceso a la hora real" refusal. Clock is
frozen via set_clock so assertions are deterministic across timezones.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import core.host_time as host_time


def _panama(dt_naive: datetime) -> datetime:
    # Panama is UTC-5, no DST.
    return dt_naive.replace(tzinfo=timezone(timedelta(hours=-5)))


def teardown_function(_):
    host_time.reset_clock()


def test_now_reads_injected_clock_not_model():
    fixed = _panama(datetime(2026, 7, 13, 9, 30, 15))
    host_time.set_clock(lambda: fixed)
    ht = host_time.now()
    assert ht.time_hms() == "09:30:15"
    assert "2026-07-13" in ht.iso
    assert ht.utc_offset == "-0500"
    assert ht.to_dict()["source"] == "host_system_clock"


def test_spanish_date_formatting_is_locale_independent():
    fixed = _panama(datetime(2026, 7, 13, 9, 30, 0))   # 13 July 2026 = Monday
    host_time.set_clock(lambda: fixed)
    ht = host_time.now()
    assert ht.weekday_es == "lunes"
    assert ht.date_es() == "lunes, 13 de julio de 2026"
    assert "9 de septiembre" not in ht.date_es()


def test_utc_timezone_supported():
    fixed = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    host_time.set_clock(lambda: fixed)
    ht = host_time.now()
    assert ht.utc_offset == "+0000"
    assert ht.time_hms() == "00:00:00"


def test_date_rollover():
    fixed = _panama(datetime(2026, 12, 31, 23, 59, 59))
    host_time.set_clock(lambda: fixed)
    assert host_time.now().date_es() == "jueves, 31 de diciembre de 2026"


def test_prompt_line_forbids_no_access_claim():
    fixed = _panama(datetime(2026, 7, 13, 9, 30, 0))
    host_time.set_clock(lambda: fixed)
    line = host_time.host_time_prompt_line()
    assert "2026-07-13" in line
    assert "never say you lack real-time access" in line
    assert "authoritative" in line.lower()


def test_spanish_sentence_ready_to_speak():
    fixed = _panama(datetime(2026, 7, 13, 9, 5, 0))
    host_time.set_clock(lambda: fixed)
    s = host_time.now().spanish_sentence()
    assert s.startswith("Son las 09:05:00")
    assert "13 de julio de 2026" in s
