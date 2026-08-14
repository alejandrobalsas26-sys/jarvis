"""training_gym/teachers/sanitization.py — V69 M62: the boundary a packet crosses.

WHY THIS EXISTS
---------------
A teacher packet is the only artifact in the gym designed to be READ BY A THIRD PARTY.
An operator pastes it into ChatGPT or Claude, or a cloud adapter posts it over the
network. Everything the trajectory layer keeps locally — real messages, real tool
arguments, real tool output — is exactly the material that must not make that trip.

The gym already has a redaction authority (:mod:`core.redaction_policy`) and a secret
vocabulary (:mod:`core.memory_router`), and this module DELEGATES to both rather than
inventing a second, weaker definition of "private". What it adds is the part those
scanners cannot know:

  * the operator's username and hostname, which are private but are not secrets and
    appear in no pattern;
  * absolute filesystem paths of every shape, including the ones outside a home
    directory (``D:\\lab\\case-14``, ``/opt/intel/feed``);
  * *hidden* fields — chain-of-thought, internal notes, held-out expected answers —
    which are not secrets either, and whose danger is that they are perfectly
    legitimate content in a LOCAL record and catastrophic in an exported one.

TWO HALVES, DELIBERATELY SEPARATE
---------------------------------
:func:`sanitize_text` / :func:`sanitize_structure` REMOVE things. :func:`assert_clean`
independently RE-READS the result and refuses it if anything private survived. They are
not the same code path on purpose: a redactor can only remove what it knows about, and
the last line of defence must not be the same function that already had its chance.

FAIL CLOSED, ALWAYS
-------------------
When the scanner cannot be imported, or crashes, the answer is "not clean" — never
"clean". A missing scanner has proven nothing about the text it never read, and a
packet is a one-way trip: there is no recall once it is in a chat window.

NOT SO AGGRESSIVE THAT EVIDENCE DIES
------------------------------------
Hashes, evidence ids, task and attempt ids, workspace-RELATIVE fixture paths, grader
statuses and structured tool-call shapes are preserved verbatim. A packet whose
deterministic findings had been scrubbed into ``[REDACTED]`` would ask a reviewer to
audit evidence they cannot see, and the review that came back would be worthless in a
way nobody could detect.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..policies import credential_shaped
from ..schemas import scan_private_content
from .base import TeacherError

#: What replaces a removed span. Distinct markers so a reader of a packet can tell
#: WHY something is missing without being told what it was.
MARK_PATH = "<path>"
MARK_USER = "<operator>"
MARK_HOST = "<host>"
MARK_DROPPED = "[dropped-by-export-policy]"

#: Bound on any single sanitized string in a packet.
MAX_SANITIZED_CHARS = 20_000
#: Bound on the recursion a structure may carry. Deeper than this is not data.
MAX_STRUCTURE_DEPTH = 8
#: Bound on the entries a sanitized container may hold.
MAX_STRUCTURE_ITEMS = 256


class SanitizationError(TeacherError):
    """Sanitization could not establish that content is exportable. Never a warning."""


# ── field names that never leave the host ─────────────────────────────────────
#: Key-name tokens whose VALUE is dropped from an exported structure outright.
#:
#: Three distinct dangers share one mechanism. Hidden reasoning: the operator never saw
#: it and a reviewer must not be handed it as if it were the answer. Held-out targets:
#: an expected answer inside a packet turns "review this attempt" into "copy the key",
#: and every review that comes back is unfalsifiable. Credential-shaped keys: caught
#: here by NAME as well as by value, because a token the pattern scanners do not
#: recognise still sits under a key called ``api_key``.
HIDDEN_KEY_TOKENS: frozenset[str] = frozenset({
    # hidden reasoning
    "think", "thinking", "thoughts", "reasoning", "chainofthought", "cot",
    "scratchpad", "deliberation", "innermonologue", "rationale",
    # held-out evaluation targets
    "expectedanswer", "expectedoutput", "idealanswer", "idealoutput", "answerkey",
    "groundtruth", "goldanswer", "solution", "referenceanswer", "evaltarget",
    "hiddenanswer", "correctanswer",
    # internal-only annotations
    "internalnotes", "operatornotes", "privatenotes", "reviewernotes",
    # environment and credentials
    "env", "environ", "environment", "credential", "credentials", "secret",
    "secrets", "apikey", "accesstoken", "refreshtoken", "bearer", "cookie",
    "cookies", "authorization", "authheader", "password", "passwd", "privatekey",
    "sessionid",
})


def _name_tokens(name: object) -> tuple[str, ...]:
    """A field name split into comparable tokens.

    ``API-Key``, ``api_key`` and ``apiKey`` must all reduce to ``("api", "key")``: a
    denylist that missed one of the three spellings would be decoration.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    return tuple(t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t)


def hidden_key(name: object) -> bool:
    """True when a field name means "this value must never be exported".

    Matching is by CONTIGUOUS TOKEN RUN, not by substring: ``expected_answer``,
    ``student_expected_answer``, ``chainOfThought`` and ``x_api_key_v2`` all match,
    while ``resolution`` does not — even though it contains ``solution``. Substring
    matching would drop innocuous fields, and a sanitizer that mangles ordinary data is
    a sanitizer an operator switches off.

    The credential half delegates to :func:`~training_gym.policies.credential_shaped`,
    the repository's existing credential-name authority, so there is one vocabulary for
    "this name carries a secret" rather than two that can drift apart.
    """
    tokens = _name_tokens(name)
    if not tokens:
        return False
    for start in range(len(tokens)):
        joined = ""
        for token in tokens[start:]:
            joined += token
            if joined in HIDDEN_KEY_TOKENS:
                return True
    return credential_shaped(str(name))


# ── absolute paths ────────────────────────────────────────────────────────────
#: Absolute paths of every shape. ``core.redaction_policy`` removes home directories;
#: these remove the rest, because ``D:\\lab\\case-14`` is the operator's filesystem
#: layout just as much as ``C:\\Users\\alex`` is, and a packet that carries it tells a
#: third party how the host is organised.
_ABSOLUTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b[a-z]:[\\/][^\s\"'<>|]*"),
    re.compile(r"(?i)\\\\[a-z0-9._-]+\\[^\s\"'<>|]*"),
    re.compile(r"(?<![\w.])/(?:home|users|etc|usr|var|tmp|opt|root|srv|mnt|private|"
               r"proc|sys|dev|volumes)(?:/[^\s\"'<>|]*)?"),
    re.compile(r"(?i)\bfile://[^\s\"'<>|]*"),
)

#: The same shapes, as a VERIFICATION scan. Kept separate from the redaction patterns
#: so a bug in one is not automatically a matching blind spot in the other.
_ABSOLUTE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b[a-z]:[\\/]{1,2}[a-z0-9._-]"),
    re.compile(r"(?i)\\\\[a-z0-9._-]+\\"),
    re.compile(r"(?<![\w.])/(?:home|users|etc|usr|var|tmp|opt|root|srv|mnt|private|"
               r"volumes)/[a-z0-9._-]", re.IGNORECASE),
    re.compile(r"(?i)\bfile://"),
)

#: Hidden-reasoning openers, as a verification scan over an already-redacted payload.
_REASONING_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"<{tag}\b", re.IGNORECASE)
    for tag in ("think", "thinking", "reasoning", "scratchpad", "analysis"))

#: Shortest identity string this module will substitute. A three-character username
#: appears inside ordinary words, and a redactor that shredded every occurrence of
#: ``ana`` would make the packet unreadable — which gets redaction switched off.
MIN_IDENTITY_CHARS = 4


def _local_username() -> str:
    """The operator's account name, or ``""`` when the host will not say.

    A host that cannot report its own name is a reason to sanitize LESS, not to fail:
    the generic path patterns and the shared secret scanner still apply, and
    :func:`assert_clean` still runs over the result.
    """
    try:
        import getpass
        return str(getpass.getuser() or "").strip()
    except Exception:  # noqa: BLE001 — unknown identity, sanitized generically
        return ""


def _local_hostname() -> str:
    """This machine's name, or ``""``. Same reasoning as :func:`_local_username`."""
    try:
        import socket
        return str(socket.gethostname() or "").strip()
    except Exception:  # noqa: BLE001 — unknown identity, sanitized generically
        return ""


def _identity_pattern(literal: str) -> re.Pattern[str]:
    """Match *literal* as an identity, not as a syllable inside an ordinary word.

    **Why this is not a plain substring match (V69 M62 S3J, defect D36).** It used to
    be, and the consequence was that on a host whose account name happened to spell a
    fragment of English, every corpus containing that fragment was silently rewritten
    at promotion time — so a promoted dataset's ``manifest_hash`` became a function of
    the building account's name. Measured: on a host whose account is a common
    four-letter sequence, one row of ``m62-defensive-quality-train v1`` had an ordinary
    word rewritten, and that version could not be rebuilt to the digest it was trained
    under. A dataset identity that depends on host state is the D34 failure class, and
    this module's own contract already forbade the behaviour: *"a sanitizer that mangles
    ordinary data is a sanitizer an operator switches off."*

    The guard is deliberately NARROWER than a word boundary. ``\\b`` would stop matching
    ``kali`` in ``kali123`` and ``kali_2``, which are exactly the shapes a real leak
    takes. This refuses the match only when the literal is flanked by ASCII letters on
    BOTH sides — the one case where the hit cannot be an identity because it is the
    middle of a longer word. Everything else — start or end of string, punctuation, a
    digit, a separator, a path component — still matches and is still redacted.

    :func:`sanitize_text` and :func:`scan_export_payload` both use this, so the redactor
    and the independent verifier can never disagree about what an identity is.
    """
    return re.compile(
        r"(?<![A-Za-z])" + re.escape(literal) + r"(?![A-Za-z])", re.IGNORECASE)


def _identities() -> tuple[tuple[str, str], ...]:
    """``(literal, replacement)`` pairs for the operator's own identity strings.

    Read lazily: importing this module must touch no environment. Short names are
    skipped — see :data:`MIN_IDENTITY_CHARS`.
    """
    pairs: list[tuple[str, str]] = []
    user = _local_username()
    if len(user) >= MIN_IDENTITY_CHARS:
        pairs.append((user, MARK_USER))
    host = _local_hostname()
    if len(host) >= MIN_IDENTITY_CHARS:
        pairs.append((host, MARK_HOST))
        head = host.split(".", 1)[0]
        if len(head) >= MIN_IDENTITY_CHARS and head != host:
            pairs.append((head, MARK_HOST))
    # Longest first: replacing a short form first would leave the longer one partially
    # rewritten and therefore unmatched by the verification scan.
    return tuple(sorted(pairs, key=lambda p: len(p[0]), reverse=True))


# ── the report ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SanitizationReport:
    """What sanitization actually did. Counts and category names only — never the
    removed text, because a report that quoted what it removed would be the leak."""

    categories: tuple[str, ...] = ()
    dropped_keys: tuple[str, ...] = ()
    redactions: int = 0
    truncated_chars: int = 0
    scanner_available: bool = True

    @property
    def clean(self) -> bool:
        """True only when a scanner actually ran. An absent scanner is not a pass."""
        return self.scanner_available

    def merge(self, other: "SanitizationReport") -> "SanitizationReport":
        return SanitizationReport(
            categories=tuple(sorted(set(self.categories) | set(other.categories))),
            dropped_keys=tuple(sorted(set(self.dropped_keys)
                                      | set(other.dropped_keys))),
            redactions=self.redactions + other.redactions,
            truncated_chars=self.truncated_chars + other.truncated_chars,
            scanner_available=self.scanner_available and other.scanner_available,
        )

    def to_dict(self) -> dict:
        return {"categories": list(self.categories),
                "dropped_keys": list(self.dropped_keys),
                "redactions": self.redactions,
                "truncated_chars": self.truncated_chars,
                "scanner_available": self.scanner_available,
                "clean": self.clean}


# ── text ──────────────────────────────────────────────────────────────────────
def sanitize_text(text: object, *,
                  limit: int = MAX_SANITIZED_CHARS) -> tuple[str, SanitizationReport]:
    """Redact one string for export, and report what was removed.

    Order matters and mirrors the application's own pipeline: hidden reasoning first
    (so a secret quoted only inside a ``<think>`` block disappears with the block
    rather than surviving as a marker), then the shared redactor's OTP/credential/home
    passes, then this module's additions — identity strings, then absolute paths — then
    the length bound.

    When the shared redactor cannot be imported the text is still processed by
    everything in this module, but the report records ``scanner_available=False`` and
    :func:`assert_clean` will refuse the payload. A packet is never emitted on the
    strength of a redactor that did not run.
    """
    raw = "" if text is None else str(text)
    if not raw:
        return "", SanitizationReport()
    categories: list[str] = []
    redactions = 0
    scanner_available = True

    try:
        from core.redaction_policy import redact_text
        # The shared pipeline strips hidden reasoning, OTPs, credentials and home
        # paths. A generous bound is passed because this function applies its own.
        cleaned, report = redact_text(raw, max_chars=max(limit, len(raw)),
                                      strip_home=True)
        categories.extend(report.categories)
        redactions += report.total
    except Exception:  # noqa: BLE001 — absence must be visible, never silently clean
        scanner_available = False
        cleaned = raw
        categories.append("scanner_unavailable")

    for literal, replacement in _identities():
        cleaned, hits = _identity_pattern(literal).subn(replacement, cleaned)
        if hits:
            redactions += hits
            categories.append("identity")

    for pattern in _ABSOLUTE_PATTERNS:
        cleaned, hits = pattern.subn(MARK_PATH, cleaned)
        if hits:
            redactions += hits
            categories.append("absolute_path")

    truncated = 0
    bound = max(0, int(limit))
    if len(cleaned) > bound:
        truncated = len(cleaned) - bound
        cleaned = cleaned[:bound] + "[TRUNCATED]"
        categories.append("truncated")

    return cleaned, SanitizationReport(
        categories=tuple(sorted(set(categories))), redactions=redactions,
        truncated_chars=truncated, scanner_available=scanner_available)


# ── structures ────────────────────────────────────────────────────────────────
def sanitize_structure(obj: object, *, limit: int = MAX_SANITIZED_CHARS,
                       _depth: int = 0) -> tuple[object, SanitizationReport]:
    """Sanitize a JSON-ready structure, dropping every field that may not be exported.

    A hidden or credential-shaped KEY has its value replaced by a marker rather than
    being deleted, so a reviewer — and an auditor reading the packet later — can see
    that something was withheld. Silent deletion would make an incomplete packet
    indistinguishable from a complete one.
    """
    if _depth > MAX_STRUCTURE_DEPTH:
        return MARK_DROPPED, SanitizationReport(categories=("depth_exceeded",),
                                                dropped_keys=("<deep>",))
    if obj is None or isinstance(obj, bool):
        return obj, SanitizationReport()
    if isinstance(obj, (int, float)):
        return obj, SanitizationReport()
    if isinstance(obj, str):
        return sanitize_text(obj, limit=limit)
    if isinstance(obj, Mapping):
        out: dict[str, object] = {}
        report = SanitizationReport()
        dropped: list[str] = []
        for index, (key, value) in enumerate(sorted(obj.items(), key=lambda kv: str(kv[0]))):
            if index >= MAX_STRUCTURE_ITEMS:
                dropped.append("<overflow>")
                break
            name = str(key)
            clean_key, key_report = sanitize_text(name, limit=200)
            report = report.merge(key_report)
            if hidden_key(name):
                out[clean_key] = MARK_DROPPED
                dropped.append(clean_key)
                continue
            clean_value, value_report = sanitize_structure(value, limit=limit,
                                                           _depth=_depth + 1)
            out[clean_key] = clean_value
            report = report.merge(value_report)
        if dropped:
            report = report.merge(SanitizationReport(categories=("dropped_field",),
                                                    dropped_keys=tuple(dropped)))
        return out, report
    if isinstance(obj, (list, tuple)):
        items: list[object] = []
        report = SanitizationReport()
        for value in list(obj)[:MAX_STRUCTURE_ITEMS]:
            clean_value, value_report = sanitize_structure(value, limit=limit,
                                                           _depth=_depth + 1)
            items.append(clean_value)
            report = report.merge(value_report)
        if len(list(obj)) > MAX_STRUCTURE_ITEMS:
            report = report.merge(SanitizationReport(categories=("truncated",),
                                                     dropped_keys=("<overflow>",)))
        return items, report
    # An object of unknown type is not data. Describing it by type name rather than by
    # ``repr`` matters: a ``repr`` is how a live object's attributes — a client holding
    # an API key, an open path — end up inside an exported packet.
    return f"{MARK_DROPPED}<{type(obj).__name__}>", SanitizationReport(
        categories=("unsupported_type",), dropped_keys=(type(obj).__name__,))


# ── verification ──────────────────────────────────────────────────────────────
def scan_export_payload(payload: object) -> tuple[str, ...]:
    """Category names of content that must not be exported. Empty tuple = clean.

    Runs the application's own scanner first — so this layer can never be more
    permissive than the runtime — and then adds the checks that scanner does not make:
    absolute paths outside a home directory, the operator's identity strings, and
    hidden-reasoning openers. An unavailable or crashed scanner is reported as a
    category, so a caller cannot mistake it for a clean result.
    """
    found: list[str] = list(scan_private_content(payload))
    blob = _flatten(payload)
    if any(pattern.search(blob) for pattern in _ABSOLUTE_MARKERS):
        found.append("absolute_path")
    if any(pattern.search(blob) for pattern in _REASONING_MARKERS):
        found.append("hidden_reasoning")
    for literal, _replacement in _identities():
        # The same definition the redactor uses -- see `_identity_pattern`. A verifier
        # that read "identity" more broadly than the redactor writes it would refuse
        # every payload containing an ordinary word that spells the account name.
        if _identity_pattern(literal).search(blob):
            found.append("identity")
            break
    return tuple(sorted(set(found)))


def _flatten(payload: object, *, _depth: int = 0) -> str:
    """Every string in a structure, including KEYS, joined for scanning.

    Keys are included because ``{"C:\\\\Users\\\\alex": 1}`` hides a private path in a
    position a value-only scan never reads.
    """
    if _depth > 12:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        return "\n".join([*(str(k) for k in payload),
                          *(_flatten(v, _depth=_depth + 1) for v in payload.values())])
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return "\n".join(_flatten(v, _depth=_depth + 1) for v in payload)
    return "" if payload is None or isinstance(payload, (bool, int, float)) else str(payload)


def assert_clean(payload: object, *, label: str) -> None:
    """Raise unless *payload* is exportable. The last gate before anything leaves.

    Deliberately not a redactor: a payload that fails here is REFUSED, not patched, so
    a redaction bug is loud instead of quiet. And deliberately not skippable when the
    scanner is missing — ``scanner_unavailable`` is one of the categories that fails
    this check, because "we could not look" is not "there was nothing there".
    """
    found = scan_export_payload(payload)
    if found:
        raise SanitizationError(
            f"{label}: refusing to export — private content present "
            f"({', '.join(found)}). A packet is a one-way trip; there is no recall "
            f"once it is in a chat window.")


def sanitize_for_export(payload: object, *, label: str,
                        limit: int = MAX_SANITIZED_CHARS
                        ) -> tuple[object, SanitizationReport]:
    """Sanitize, then INDEPENDENTLY verify, then return — or raise.

    The one entry point a packet builder should use: it is impossible to call this and
    receive an unverified result, which is the mistake a two-step API invites.
    """
    clean, report = sanitize_structure(payload, limit=limit)
    if not report.scanner_available:
        raise SanitizationError(
            f"{label}: the redaction scanner is unavailable, so nothing can be "
            f"asserted about this payload; refusing to export rather than reporting a "
            f"clean result nobody measured")
    assert_clean(clean, label=label)
    return clean, report


__all__ = [
    "HIDDEN_KEY_TOKENS", "MARK_DROPPED", "MARK_HOST", "MARK_PATH", "MARK_USER",
    "MAX_SANITIZED_CHARS", "MAX_STRUCTURE_DEPTH", "MAX_STRUCTURE_ITEMS",
    "MIN_IDENTITY_CHARS", "SanitizationError", "SanitizationReport", "assert_clean",
    "hidden_key", "sanitize_for_export", "sanitize_structure", "sanitize_text",
    "scan_export_payload",
]
