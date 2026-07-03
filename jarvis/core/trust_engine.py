"""core/trust_engine.py — Dynamic trust scoring for command execution (v25.0)."""

import asyncio, json, math, time
from enum import Enum
from pathlib import Path

PROFILE_PATH = Path(__file__).parent / "trust_profile.json"


class ChallengeLevel(Enum):
    NONE      = "none"       # execute directly
    CONFIRM   = "confirm"    # simple text "yes"/"confirm"
    FULL_NATO = "full_nato"  # full phonetic OTP


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    freq = {c: text.count(c) / len(text) for c in set(text)}
    return -sum(p * math.log2(p) for p in freq.values())


# Genuinely low-risk / read-only binaries whose repeated, trusted use may be
# de-escalated all the way to ChallengeLevel.NONE. Everything NOT on this
# allowlist is treated as execution-capable / state-changing and can never drop
# below CONFIRM on trust score alone (F5 — no high-risk auto-approval decay).
_LOW_RISK_BINARIES: frozenset[str] = frozenset({
    "whois", "dig", "nslookup", "host", "ping", "traceroute", "tracert",
    "whoami", "id", "hostname", "uname", "date", "uptime",
    "ipconfig", "ifconfig", "ip", "netstat", "ss", "arp", "route",
    "ls", "dir", "pwd", "cat", "type", "head", "tail", "wc", "file", "stat",
    "ps", "tasklist", "df", "free", "env", "echo",
})


def is_high_risk_action(binary: str, command: str = "") -> bool:
    """
    True unless *binary* is a known read-only / low-risk tool.

    Execution- or state-capable actions (the default for anything not on the
    read-only allowlist) can never be auto-approved down to ChallengeLevel.NONE
    on trust score alone — they always retain at least a CONFIRM gate.
    """
    name = (binary or "").strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    return name not in _LOW_RISK_BINARIES


def calculate_trust_score(
    binary: str,
    command: str,
    profile: dict,
    session_commands: list[str],
) -> float:
    """Returns 0.0 (no trust) to 1.0 (full trust)."""
    history = profile.get("binaries", {})
    entry   = history.get(binary, {"count": 0, "last_used": 0})

    # Frequency score — saturates at 1.0 after 30 uses
    freq_score = min(entry["count"] / 30, 1.0)

    # Recency score
    age = time.time() - entry.get("last_used", 0)
    if   age < 7200:    recency = 1.0
    elif age < 86400:   recency = 0.7
    else:               recency = 0.3

    # Session familiarity bonus
    session_bonus = 0.2 if binary in session_commands else 0.0

    # Entropy penalty — high entropy = clamp to 0
    entropy = _shannon_entropy(command)
    if entropy > 4.5:
        return 0.0

    return min(freq_score * recency + session_bonus, 1.0)


def get_challenge_level(
    binary: str,
    command: str,
    binary_status: str,
    yara_hits: list,
    profile: dict,
    session_commands: list[str],
) -> tuple[ChallengeLevel, float]:
    """Returns (ChallengeLevel, trust_score)."""
    # Hard rules — no score override
    if binary_status == "unlisted_escalation":
        return ChallengeLevel.FULL_NATO, 0.0
    if yara_hits:
        return ChallengeLevel.FULL_NATO, 0.0
    entropy = _shannon_entropy(command)
    if entropy > 5.0:
        return ChallengeLevel.FULL_NATO, 0.0

    score = calculate_trust_score(binary, command, profile, session_commands)

    if   score >= 0.80: level = ChallengeLevel.NONE
    elif score >= 0.50: level = ChallengeLevel.CONFIRM
    else:               level = ChallengeLevel.FULL_NATO

    # F5 — high-risk floor: execution / state-changing actions never de-escalate
    # to NONE on trust score alone; they keep at least a CONFIRM gate no matter
    # how many times they have been run successfully.
    if level == ChallengeLevel.NONE and is_high_risk_action(binary, command):
        level = ChallengeLevel.CONFIRM

    return level, score


async def update_profile(binary: str, profile: dict) -> dict:
    """Update trust profile after successful execution."""
    binaries = profile.setdefault("binaries", {})
    entry    = binaries.setdefault(binary, {"count": 0, "last_used": 0})
    entry["count"]     += 1
    entry["last_used"]  = time.time()
    await save_profile(profile)
    return profile


async def load_profile() -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _load_sync)


def _load_sync() -> dict:
    try:
        return json.loads(PROFILE_PATH.read_text()) if PROFILE_PATH.exists() else {}
    except Exception:
        return {}


async def save_profile(profile: dict) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: PROFILE_PATH.write_text(json.dumps(profile, indent=2))
    )
