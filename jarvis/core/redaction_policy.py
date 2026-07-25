"""core/redaction_policy.py — V69 M60.1.1: the one deterministic redaction scanner.

Everything M60 writes to disk — journalled turns, redacted exports, diagnostics
bundles — passes through THIS module. It is pure, dependency-light and deterministic:
same input, same output, no model involved, no network, no clock.

WHAT IT REMOVES, AND WHY EACH ONE IS NON-NEGOTIABLE
---------------------------------------------------
  hidden reasoning   qwen3 emits ``<think>...</think>``. The operator never saw it and
                     M60 must never restore it as if it were an answer. Unterminated
                     blocks (the crash case — the model was mid-``<think>``) are cut to
                     the end of the string, which is the SAFE direction.
  NATO OTP           ``tools.executor`` authorises high-risk actions with a spoken NATO
                     word. Persisting the challenge word would let a restored journal
                     hand an attacker a live authorisation phrase.
  numeric OTP        a 4-8 digit code in an OTP/2FA/code context.
  credentials        reuses ``core.memory_router``'s proven high-precision patterns
                     (API keys, bearer tokens, JWTs, private keys, cookies) so there is
                     one credential vocabulary in the codebase, not two.
  private paths      the operator's home directory becomes ``<HOME>``; a diagnostics
                     bundle must not carry ``C:\\Users\\<name>``.
  raw command lines  a shell/tool argument blob is summarised, never stored verbatim.

The scanner REPORTS what it did (``RedactionReport``) so a bundle can state its
redaction count truthfully instead of claiming "clean".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.memory_router import redact_secrets

# ── Markers ──────────────────────────────────────────────────────────────────
MARK_REASONING = "[REDACTED-REASONING]"
MARK_OTP = "[REDACTED-OTP]"
MARK_SECRET = "[REDACTED-SECRET]"
MARK_HOME = "<HOME>"
MARK_TRUNCATED = "[TRUNCATED]"

# ── Hidden reasoning ─────────────────────────────────────────────────────────
# Closed blocks first, then any UNTERMINATED opener to end-of-string (crash case).
_REASONING_TAGS = ("think", "thinking", "reasoning", "scratchpad", "analysis")
_CLOSED_REASONING = tuple(
    re.compile(rf"<{t}\b[^>]*>.*?</{t}>", re.DOTALL | re.IGNORECASE)
    for t in _REASONING_TAGS
)
_OPEN_REASONING = tuple(
    re.compile(rf"<{t}\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
    for t in _REASONING_TAGS
)

# ── OTP / authorization tokens ───────────────────────────────────────────────
_NATO_WORDS = (
    "alfa", "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliett", "juliet", "kilo", "lima", "mike", "november",
    "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform", "victor",
    "whiskey", "xray", "yankee", "zulu",
)
# A NATO word only redacts inside an AUTHORIZATION context — "hotel" in ordinary
# conversation is not an OTP, and over-redacting normal Spanish/English text would
# make the restored history useless.
_NATO_CONTEXT = re.compile(
    r"(?i)\b(otp|nato|desaf[íi]o|challenge|autoriza\w*|authoriz\w*|c[óo]digo|code|mfa|"
    r"palabra)\b[^\n]{0,60}?\b(" + "|".join(_NATO_WORDS) + r")\b"
)
_NUMERIC_OTP = re.compile(
    r"(?i)\b(otp|2fa|mfa|token|c[óo]digo|code|pin|verification)\b\s*[:=#]?\s*(\d{4,8})\b"
)

# ── Home directory ───────────────────────────────────────────────────────────
_HOME_PATTERNS = (
    re.compile(r"(?i)[A-Z]:\\Users\\[^\\/:*?\"<>|\r\n]{1,64}"),
    re.compile(r"(?i)/(?:home|Users)/[^/\s:*?\"<>|\r\n]{1,64}"),
)

# ── Raw command lines ────────────────────────────────────────────────────────
_COMMANDISH = re.compile(
    r"(?i)\b(powershell|cmd\.exe|/bin/(?:ba)?sh|nmap|curl|wget|reg\s+add|schtasks|"
    r"sc\s+create|Invoke-\w+|net\s+user)\b"
)


@dataclass
class RedactionReport:
    """What the scanner actually removed. Counts only — never the removed text."""

    reasoning_blocks: int = 0
    otp_tokens: int = 0
    secrets: int = 0
    home_paths: int = 0
    command_lines: int = 0
    truncated_chars: int = 0
    categories: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (self.reasoning_blocks + self.otp_tokens + self.secrets
                + self.home_paths + self.command_lines)

    def merge(self, other: "RedactionReport") -> "RedactionReport":
        self.reasoning_blocks += other.reasoning_blocks
        self.otp_tokens += other.otp_tokens
        self.secrets += other.secrets
        self.home_paths += other.home_paths
        self.command_lines += other.command_lines
        self.truncated_chars += other.truncated_chars
        for c in other.categories:
            if c not in self.categories:
                self.categories.append(c)
        return self

    def snapshot(self) -> dict:
        return {
            "total": self.total,
            "reasoning_blocks": self.reasoning_blocks,
            "otp_tokens": self.otp_tokens,
            "secrets": self.secrets,
            "home_paths": self.home_paths,
            "command_lines": self.command_lines,
            "truncated_chars": self.truncated_chars,
            "categories": sorted(self.categories),
        }


def strip_hidden_reasoning(text: str) -> tuple[str, int]:
    """Remove every reasoning block, closed or unterminated. Returns (text, count)."""
    if not text:
        return text or "", 0
    out, n = text, 0
    for pat in _CLOSED_REASONING:
        out, k = pat.subn(MARK_REASONING, out)
        n += k
    for pat in _OPEN_REASONING:
        # An unterminated opener means the process died mid-reasoning: cut to the end.
        new, k = pat.subn(MARK_REASONING, out)
        if k:
            out, n = new, n + k
    return out, n


def redact_otp(text: str) -> tuple[str, int]:
    """Remove NATO challenge words in an authorization context and numeric OTPs."""
    if not text:
        return text or "", 0
    n = 0
    out, k = _NATO_CONTEXT.subn(lambda m: f"{m.group(1)} {MARK_OTP}", text)
    n += k
    out, k = _NUMERIC_OTP.subn(lambda m: f"{m.group(1)} {MARK_OTP}", out)
    n += k
    return out, n


def redact_home_paths(text: str) -> tuple[str, int]:
    """Replace the operator's home directory with ``<HOME>``."""
    if not text:
        return text or "", 0
    out, n = text, 0
    for pat in _HOME_PATTERNS:
        out, k = pat.subn(MARK_HOME, out)
        n += k
    return out, n


def contains_command_line(text: str) -> bool:
    return bool(text) and bool(_COMMANDISH.search(text))


def redact_text(text: str, *, max_chars: int = 4000,
                strip_home: bool = True) -> tuple[str, RedactionReport]:
    """The full deterministic pipeline. ALWAYS applied before any M60 disk write.

    Order matters: hidden reasoning is removed FIRST (so a secret quoted only inside a
    ``<think>`` block disappears with the block rather than surviving as a marker),
    then OTPs, then credentials, then home paths, then the length bound.
    """
    rep = RedactionReport()
    if not text:
        return "", rep
    out, rep.reasoning_blocks = strip_hidden_reasoning(str(text))
    if rep.reasoning_blocks:
        rep.categories.append("reasoning")
    out, rep.otp_tokens = redact_otp(out)
    if rep.otp_tokens:
        rep.categories.append("otp")
    before = out
    out = redact_secrets(out)
    if out != before:
        rep.secrets = out.count(MARK_SECRET)
        rep.categories.append("secret")
    if strip_home:
        out, rep.home_paths = redact_home_paths(out)
        if rep.home_paths:
            rep.categories.append("home_path")
    if contains_command_line(out):
        rep.command_lines = 1
        rep.categories.append("command_line")
    limit = max(0, int(max_chars))
    if len(out) > limit:
        rep.truncated_chars = len(out) - limit
        out = out[:limit] + MARK_TRUNCATED
    return out, rep


# ── Secret scanner (the M60.6 pre-finalize gate) ─────────────────────────────
_LEAK_MARKERS = (
    ("reasoning", tuple(re.compile(rf"<{t}\b", re.IGNORECASE) for t in _REASONING_TAGS)),
    ("secret", (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
                re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
                re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
                re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
                re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
                re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
                re.compile(r"(?i)\beyJ[A-Za-z0-9_\-]{10,}\."
                           r"[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"))),
    ("home_path", _HOME_PATTERNS),
)


def scan_for_leaks(payload: str) -> list[str]:
    """Return the CATEGORY names still present in *payload*. Empty list = clean.

    This is a verification gate, not a redactor: a bundle whose scan is non-empty is
    refused rather than silently patched, so a redaction bug is loud instead of quiet.
    """
    if not payload:
        return []
    found: list[str] = []
    for name, patterns in _LEAK_MARKERS:
        if any(p.search(payload) for p in patterns):
            found.append(name)
    return found


def scan_structure(obj, *, _depth: int = 0) -> list[str]:
    """Recursive leak scan over a JSON-ready structure. Bounded depth."""
    if _depth > 12:
        return []
    if isinstance(obj, str):
        return scan_for_leaks(obj)
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            for c in scan_for_leaks(str(k)) + scan_structure(v, _depth=_depth + 1):
                if c not in out:
                    out.append(c)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            for c in scan_structure(v, _depth=_depth + 1):
                if c not in out:
                    out.append(c)
    return out
