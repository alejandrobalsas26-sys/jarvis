"""
core/personality.py — JARVIS character & contextual responses (v46.0).

Subtle character. Dry humor. Context awareness.
Never annoying — max 1 personality remark per 60 seconds.

JARVIS sounds like a competent professional with quiet wit,
not a chatbot pretending to be sentient.

Contexts:
  morning   (05-12)   — focused, energetic
  afternoon (12-18)   — operational, dry
  evening   (18-23)   — casual, slightly relaxed
  late_night(23-05)   — concerned, suggests rest

Special days: birthday (if set), christmas, new year
"""

import random, time
from datetime import datetime
from pathlib import Path

_LAST_REMARK_TS = 0.0
_REMARK_COOLDOWN = 60.0


# ── Time-of-day boot greetings ────────────────────────────────────────────────

_GREETINGS_MORNING = [
    "Good morning. JARVIS online.",
    "Good morning. Coverage held overnight.",
    "Morning. The lab is quiet. So far.",
    "Good morning. Hunt schedule executed while you slept.",
]

_GREETINGS_AFTERNOON = [
    "Good afternoon. JARVIS at your service.",
    "Afternoon. All systems nominal.",
    "JARVIS online. The watch continues.",
]

_GREETINGS_EVENING = [
    "Good evening. JARVIS online.",
    "Evening. Ready when you are.",
    "Good evening. All sensors green.",
]

_GREETINGS_LATE_NIGHT = [
    "Working late again. JARVIS online.",
    "It is late, sir. JARVIS at your service nonetheless.",
    "Online. The world is asleep. We are not.",
]


# ── Contextual remarks for specific events ───────────────────────────────────

_REMARKS_INCIDENT_CRITICAL = [
    "Critical incident. I assume this was not on the schedule.",
    "Severity nine. Worth your immediate attention.",
    "This one matters.",
]

_REMARKS_DETECTION_FAST = [
    "Detected in under a second. Not bad.",
    "Quick work.",
    "Caught instantly.",
]

_REMARKS_COVERAGE_GAP = [
    "Another gap. I will draft a rule.",
    "Coverage hole identified. Working on it.",
    "We have a blind spot here.",
]

_REMARKS_QUIET_HOURS = [
    "Quiet. Almost suspiciously quiet.",
    "Lab activity nominal. Almost too nominal.",
    "Nothing to report. Yet.",
]


def _can_remark() -> bool:
    """Cooldown enforcement — never spam remarks."""
    global _LAST_REMARK_TS
    now = time.monotonic()
    if (now - _LAST_REMARK_TS) < _REMARK_COOLDOWN:
        return False
    _LAST_REMARK_TS = now
    return True


def get_time_context() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:  return "morning"
    if 12 <= h < 18: return "afternoon"
    if 18 <= h < 23: return "evening"
    return "late_night"


def get_boot_greeting() -> str:
    """Greeting spoken during boot sequence."""
    context = get_time_context()
    options = {
        "morning":    _GREETINGS_MORNING,
        "afternoon":  _GREETINGS_AFTERNOON,
        "evening":    _GREETINGS_EVENING,
        "late_night": _GREETINGS_LATE_NIGHT,
    }.get(context, _GREETINGS_AFTERNOON)
    return random.choice(options)


def get_incident_remark(severity: float) -> str | None:
    """Optional remark for an incident."""
    if severity < 8.0 or not _can_remark():
        return None
    return random.choice(_REMARKS_INCIDENT_CRITICAL)


def get_detection_remark(latency_ms: float) -> str | None:
    """Remark for fast detections."""
    if latency_ms > 1000 or not _can_remark():
        return None
    return random.choice(_REMARKS_DETECTION_FAST)


def get_gap_remark() -> str | None:
    """Remark for coverage gaps."""
    if not _can_remark():
        return None
    return random.choice(_REMARKS_COVERAGE_GAP)


def get_holiday_remark() -> str | None:
    """Special remarks for holidays."""
    now = datetime.now()
    if now.month == 12 and now.day == 25:
        return "Merry Christmas. Even today, the watch continues."
    if now.month == 1 and now.day == 1:
        return "Happy new year. New threats, same JARVIS."
    if now.month == 10 and now.day == 31:
        return "Happy Halloween. The real ghosts are in your network."
    return None
