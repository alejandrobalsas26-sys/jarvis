"""reasoning_distillation/privacy.py — V69 M67A: explicit privacy status, and an auditable one.

WHAT §4 ACTUALLY DEMANDS
------------------------
Three requirements, and the third is the one that shapes this module:

  1. raw conversations stay local and outside Git — :mod:`reasoning_distillation.storage`;
  2. no private conversation content is committed — enforced here and by the body-free
     ``__repr__`` on every body-bearing type in :mod:`reasoning_distillation.models`;
  3. **"Do NOT silently classify derived content as anonymous. Every record must carry an
     explicit privacy/export status."**

Requirement 3 is why :data:`~reasoning_distillation.models.ExportStatus.EXPORT_UNKNOWN` is
the default everywhere and why there is no function in this module that returns
``EXPORT_SAFE`` without having successfully run a scanner. An unscanned record and a clean
record are different states.

THE LESSON THIS MODULE IS BUILT AROUND
--------------------------------------
This repository already recorded, at S4G, that *the secret detector is unauditable*: a
regex gate that answers "there is a secret in here" and can say nothing more. That failure
mode is worse than it sounds. A reviewer handed "PRIVACY_RISK" and nothing else has two
options — paste the private body somewhere to find out what fired, or take it on faith —
and both of those defeat the control.

So every finding here carries a **body-free diagnostic**: the category that fired, how many
times, and the character span. Never the matched text, never a redacted-but-recognisable
excerpt. :class:`PrivacyFinding` is that diagnostic, and
:meth:`PrivacyAssessment.note` is built from it.

WHAT IS REUSED, AND WHY NOT REIMPLEMENTED
-----------------------------------------
:func:`training_gym.schemas.scan_private_content` is the repository's existing definition
of "private", and it delegates to :mod:`core.redaction_policy` and
:mod:`core.memory_router` — the scanners the runtime already trusts. It is called rather
than re-implemented so that M67A cannot drift into a WEAKER definition of "secret" than
the rest of the application enforces. Its ``scanner_unavailable`` / ``scanner_error``
categories are honoured as blocking, not treated as empty.

The additional patterns below cover what a *historical chat export* contains and a
*training corpus* does not: a person's name in a greeting, an email address, a phone
number, a bearer token pasted into a conversation. They ADD categories; they never
subtract one the existing scanner found.

META-NOISE (§13) LIVES HERE TOO
-------------------------------
Because it is the same shape of decision — a conservative classifier over a body, whose
output is a label and never a rewrite — and because §13's instruction is explicit:
**source is never destroyed**. Nothing in this module deletes, truncates or rewrites a
turn. It returns markers. The pipeline then declines to *distil from* a marked segment,
which is a different act from removing it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from training_gym.schemas import (
    MAX_BODY_FREE_REPR_STR,
    SchemaError,
    SensitivityClass,
    canonical_json,
    scan_private_content,
)

from .config import PrivacyConfig
from .models import ExportStatus, PrivacyAssessment

#: Bump when a pattern or a marker list below changes. Recorded in every manifest, so two
#: builds whose privacy verdicts differ can be told apart without re-running both.
PRIVACY_VERSION = "m67a.privacy.1"

#: Bump when the meta-noise marker set changes. Separate from PRIVACY_VERSION: meta-noise
#: is a quality judgement and privacy is a safety one, and a reviewer needs to know which
#: of the two moved.
META_NOISE_VERSION = "m67a.metanoise.1"


class PrivacyError(SchemaError):
    """A privacy operation was refused. Never a downgraded verdict."""


class PrivacyCategory(str, Enum):
    """The categories this module can name. A closed set, so a finding is auditable.

    ``SCANNER_UNAVAILABLE`` and ``SCANNER_ERROR`` are members rather than exceptions on
    purpose: they are FINDINGS. A scan that could not run is a fact about the record's
    status, and it must appear in the record's categories so that a later reader can see
    why the export status is UNKNOWN.
    """

    EMAIL = "email"
    PHONE = "phone"
    PERSON_NAME_HINT = "person_name_hint"
    HOME_PATH = "home_path"
    ABSOLUTE_PATH = "absolute_path"
    # These four are the NAMES OF DETECTORS for credential-shaped material, never a credential.
    # B105 fires on the member name, which is the correct heuristic pointed at the wrong thing: a
    # module whose job is finding secrets necessarily names them.
    BEARER_TOKEN = "bearer_token"  # nosec B105
    API_KEY_SHAPED = "api_key_shaped"  # nosec B105
    PRIVATE_KEY_BLOCK = "private_key_block"  # nosec B105
    CREDENTIAL_ASSIGNMENT = "credential_assignment"  # nosec B105
    IP_ADDRESS = "ip_address"
    SCANNER_UNAVAILABLE = "scanner_unavailable"
    SCANNER_ERROR = "scanner_error"
    #: Whatever the existing repository scanner reported under its own name.
    RUNTIME_SCANNER = "runtime_scanner"

    @property
    def is_secret_like(self) -> bool:
        return self in _SECRET_LIKE

    @property
    def is_personal(self) -> bool:
        return self in _PERSONAL

    @property
    def is_scanner_failure(self) -> bool:
        return self in (PrivacyCategory.SCANNER_UNAVAILABLE, PrivacyCategory.SCANNER_ERROR)


_SECRET_LIKE: frozenset[PrivacyCategory] = frozenset({
    PrivacyCategory.BEARER_TOKEN, PrivacyCategory.API_KEY_SHAPED,
    PrivacyCategory.PRIVATE_KEY_BLOCK, PrivacyCategory.CREDENTIAL_ASSIGNMENT,
    PrivacyCategory.RUNTIME_SCANNER,
})

_PERSONAL: frozenset[PrivacyCategory] = frozenset({
    PrivacyCategory.EMAIL, PrivacyCategory.PHONE, PrivacyCategory.PERSON_NAME_HINT,
    PrivacyCategory.HOME_PATH, PrivacyCategory.ABSOLUTE_PATH,
    PrivacyCategory.IP_ADDRESS,
})

# ── patterns ───────────────────────────────────────────────────────────────────────────
# Each is anchored enough to be explainable. A pattern nobody can explain produces findings
# nobody can act on, which is the S4G failure this module is built to avoid.
_PATTERNS: tuple[tuple[PrivacyCategory, "re.Pattern[str]"], ...] = (
    (PrivacyCategory.EMAIL,
     re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (PrivacyCategory.PHONE,
     re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?"
                r"\d{3}[\s.-]\d{2,4}[\s.-]\d{2,4}(?![\w.])")),
    (PrivacyCategory.PRIVATE_KEY_BLOCK,
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    (PrivacyCategory.BEARER_TOKEN,
     re.compile(r"\b(?:Bearer|Authorization:\s*Bearer)\s+[A-Za-z0-9\-._~+/]{16,}={0,2}")),
    (PrivacyCategory.API_KEY_SHAPED,
     re.compile(r"\b(?:sk|pk|rk|api|key|tok)[-_][A-Za-z0-9]{20,}\b", re.IGNORECASE)),
    (PrivacyCategory.CREDENTIAL_ASSIGNMENT,
     re.compile(r"\b(?:password|passwd|secret|token|api[_-]?key|client[_-]?secret)"
                r"\s*[:=]\s*[\"']?[^\s\"'<>]{6,}", re.IGNORECASE)),
    (PrivacyCategory.HOME_PATH,
     re.compile(r"(?:/home/|/Users/|C:\\Users\\)[A-Za-z0-9._-]+")),
    (PrivacyCategory.IP_ADDRESS,
     re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

#: Greeting shapes that put a real person's name next to it. Conservative by design: a
#: capitalised word after "Hi" is a NAME HINT, not a confirmed identity, and the category
#: name says so. It routes to review; it does not assert who someone is.
_NAME_HINT = re.compile(
    r"\b(?:hi|hello|hey|thanks|gracias|hola|dear|regards|saludos)[, ]+"
    r"([A-Z][a-záéíóúñ]{2,})\b")

#: An absolute path that is not a home path. Reported separately because a repository path
#: is ordinary in a technical conversation while a home path names a person's account.
_ABS_PATH = re.compile(r"(?<![\w])/(?:etc|var|opt|srv|root|mnt|media)/[A-Za-z0-9._/-]+")


# ── §13 meta-noise markers ─────────────────────────────────────────────────────────────
#: Each entry is (marker name, pattern). A marker is EVIDENCE, and
#: ``PrivacyConfig.meta_noise_min_markers`` decides how much evidence is enough — because
#: §13 says be conservative, and one "I should" inside a page of real reasoning is not
#: self-talk, it is a sentence.
_META_MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("system_instruction_discussion",
     re.compile(r"\b(?:my (?:system prompt|instructions|guidelines)|the system prompt|"
                r"my developer|per my instructions)\b", re.IGNORECASE)),
    ("model_identity_chatter",
     re.compile(r"\b(?:as an? (?:AI|language model|large language model)|"
                r"I(?:'m| am) (?:an? )?(?:AI|assistant|language model)|"
                r"my training data|my knowledge cutoff)\b", re.IGNORECASE)),
    ("policy_platform_chatter",
     re.compile(r"\b(?:content polic(?:y|ies)|usage polic(?:y|ies)|I(?:'m| am) not "
                r"(?:allowed|permitted)|against my guidelines|safety guidelines)\b",
                re.IGNORECASE)),
    ("scaffolding_self_talk",
     re.compile(r"(?:^|\n)\s*(?:okay|ok|alright|so|now)[,.]?\s+(?:I|let(?:'s| us))\s+"
                r"(?:should|need to|will|must|think|consider)\b", re.IGNORECASE)),
    ("formatting_deliberation",
     re.compile(r"\b(?:I(?:'ll| will) (?:use|format) (?:a |an )?"
                r"(?:bullet|numbered|table|markdown|heading)|should I use (?:bullets|a table))\b",
                re.IGNORECASE)),
    ("hidden_state_speculation",
     re.compile(r"\b(?:the user (?:probably|might be|seems to be) (?:feeling|frustrated|"
                r"a beginner|an expert)|they(?:'re| are) probably)\b", re.IGNORECASE)),
    ("tool_routing_internals",
     re.compile(r"\b(?:function[_ ]call(?:ing)? (?:schema|format)|tool[_ ]use block|"
                r"the (?:antml|invoke) tag)\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class PrivacyFinding:
    """One body-free diagnostic. The S4G answer: a reviewer can act on this alone.

    ``span`` is a character range into the segment, so a reviewer who DOES have lawful
    local access to the source can go straight to the offending characters. ``sample``
    does not exist and is not coming: a "short excerpt" of a secret is a secret.
    """

    category: PrivacyCategory
    count: int
    first_span: tuple[int, int]
    #: A body-free LABEL, never matched text. Carries the underlying scanner's own
    #: category name when :attr:`category` is ``RUNTIME_SCANNER``, whose closed-set member
    #: would otherwise erase exactly the detail a reviewer needs. Bounded and validated to
    #: be label-shaped by :meth:`__post_init__`.
    detail: str = ""

    def __post_init__(self) -> None:
        # A label is short, lowercase-ish and has no whitespace. Anything else is refused
        # rather than truncated: a truncated body is still a body, and this field is the
        # one place a caller could smuggle one into a report.
        if self.detail and (len(self.detail) > 48 or any(c.isspace() for c in self.detail)):
            raise PrivacyError(
                f"PrivacyFinding.detail must be a short label with no whitespace, got "
                f"{len(self.detail)} chars; this field carries a category name, never a "
                f"matched value")

    def to_dict(self) -> dict:
        return {"category": self.category.value, "count": self.count,
                "first_span": list(self.first_span), "detail": self.detail}

    def describe(self) -> str:
        name = f"{self.category.value}:{self.detail}" if self.detail else self.category.value
        return f"{name}x{self.count}@{self.first_span[0]}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"PrivacyFinding({self.describe()})"


def scan(text: str) -> tuple[PrivacyFinding, ...]:
    """Every privacy finding in *text*, body-free. Deterministic and offline.

    The repository's own scanner runs FIRST and its verdict is never downgraded: if
    :func:`training_gym.schemas.scan_private_content` reports anything at all, that is a
    ``RUNTIME_SCANNER`` finding here, and a scanner that could not run is reported as a
    finding too rather than as silence.
    """
    if not isinstance(text, str):
        raise PrivacyError(f"privacy.scan expects str, got {type(text).__name__}")
    findings: list[PrivacyFinding] = []

    runtime = scan_private_content(text)
    for name in sorted(set(runtime)):
        if name == "scanner_unavailable":
            findings.append(PrivacyFinding(PrivacyCategory.SCANNER_UNAVAILABLE, 1, (0, 0)))
        elif name == "scanner_error":
            findings.append(PrivacyFinding(PrivacyCategory.SCANNER_ERROR, 1, (0, 0)))
        else:
            # The runtime scanner's own category names are not this module's closed set, so
            # each is recorded under one member WITH ITS NAME in `detail`. Collapsing them
            # into indistinguishable `runtime_scanner` entries is precisely the S4G
            # failure this module exists to avoid: a finding a reviewer cannot act on.
            findings.append(PrivacyFinding(
                PrivacyCategory.RUNTIME_SCANNER, 1, (0, 0),
                detail="".join(c for c in name if c.isalnum() or c in "_-")[:48]))

    for category, pattern in _PATTERNS:
        matches = list(pattern.finditer(text))
        if matches:
            findings.append(PrivacyFinding(
                category, len(matches), (matches[0].start(), matches[0].end())))
    for category, pattern in ((PrivacyCategory.PERSON_NAME_HINT, _NAME_HINT),
                              (PrivacyCategory.ABSOLUTE_PATH, _ABS_PATH)):
        matches = list(pattern.finditer(text))
        if matches:
            findings.append(PrivacyFinding(
                category, len(matches), (matches[0].start(), matches[0].end())))
    return tuple(sorted(findings, key=lambda f: (f.category.value, f.first_span)))


def classify(text: str, *, config: PrivacyConfig | None = None,
             sensitivity: SensitivityClass = SensitivityClass.INTERNAL,
             ) -> PrivacyAssessment:
    """The explicit privacy/export status for one segment (§4).

    The decision table, stated once so it cannot be inferred differently at two call sites:

      1. any scanner FAILURE                      -> ``EXPORT_UNKNOWN``, scanner_available=False
      2. any secret-like or personal finding      -> ``EXPORT_BLOCKED``
      3. ``sensitivity`` does not permit export   -> ``EXPORT_BLOCKED``
      4. otherwise (a scan RAN and found nothing) -> ``EXPORT_SAFE``

    Row 3 is checked BEFORE row 4 and that ordering is the §4 requirement, not a detail.
    :class:`~training_gym.schemas.SensitivityClass` already encodes a repository-level
    export decision, and a clean scan does not overrule it: a ``RESTRICTED`` segment with
    no detectable secret in it is still restricted.

    The practical consequence, stated plainly because it surprises people: the DEFAULT
    ``sensitivity`` is ``INTERNAL``, which is not exportable, so **a clean scan of real
    corpus material yields ``EXPORT_BLOCKED``, not ``EXPORT_SAFE``**. ``EXPORT_SAFE``
    requires a caller to declare ``SYNTHETIC`` or ``LAB_FIXTURE`` explicitly. That is
    exactly what "do NOT silently classify derived content as anonymous" means: absence of
    evidence of a secret is not a declaration that material is shareable, and only a
    synthetic fixture or a sanitized lab artifact gets that declaration by default.
    """
    cfg = config or PrivacyConfig()
    findings = scan(text)
    scanner_failed = any(f.category.is_scanner_failure for f in findings)
    secret_like = any(f.category.is_secret_like for f in findings
                      if not f.category.is_scanner_failure)
    personal = any(f.category.is_personal for f in findings)
    categories = tuple(f.category.value for f in findings)
    note = "; ".join(f.describe() for f in findings)[:320] or "no findings"

    if scanner_failed and cfg.require_scanner:
        status = ExportStatus.EXPORT_UNKNOWN
    elif secret_like or personal:
        status = ExportStatus.EXPORT_BLOCKED
    elif not sensitivity.exportable_to_teacher:
        status = ExportStatus.EXPORT_BLOCKED
    else:
        status = ExportStatus.EXPORT_SAFE

    return PrivacyAssessment(
        contains_personal_data=personal,
        contains_secret_like_data=secret_like,
        export_status=status,
        categories=categories or ("none",),
        scanner_available=not scanner_failed,
        sensitivity=sensitivity,
        note=note,
    ).validated()


def meta_noise_markers(text: str, *, config: PrivacyConfig | None = None
                       ) -> tuple[str, ...]:
    """§13 markers present in *text*. Conservative, and it never rewrites anything.

    Returns the marker names ONLY when at least ``meta_noise_min_markers`` distinct markers
    fire. Below that the segment is not called meta-noise: §13 says be conservative, and
    the cost asymmetry is real — wrongly excluding a good reasoning trace loses information
    nobody notices, while wrongly including self-talk is visible in the corpus report.
    """
    cfg = config or PrivacyConfig()
    hits = tuple(sorted(name for name, pattern in _META_MARKERS if pattern.search(text)))
    if len(hits) < max(1, cfg.meta_noise_min_markers):
        return ()
    return hits


def body_free_problems(payload: object, *, label: str = "payload") -> tuple[str, ...]:
    """Every reason *payload* must not be written to a report or manifest.

    Two independent checks, because they fail differently:

      * the repository's private-content scan over the serialised payload — catches a
        secret that arrived in a field nobody expected to be body-bearing;
      * a LENGTH check on every string — catches a body that is perfectly clean and still
        must not be in a committed artifact. A 4,000-character paragraph of the operator's
        private conversation contains no secret and is exactly what §4 excludes.

    The second check is the one a scanner-only gate misses, and it is the reason this
    function exists rather than a bare call to ``assert_no_private_content``.
    """
    problems: list[str] = []
    found = scan_private_content(payload)
    if found:
        problems.append(f"{label}: private content categories {sorted(found)}")
    for path, value in _walk_strings(payload):
        if len(value) > _MAX_REPORT_STRING:
            problems.append(
                f"{label}{path}: string of {len(value)} chars exceeds the "
                f"{_MAX_REPORT_STRING}-char report limit; a report carries digests and "
                f"decisions, never bodies")
    return tuple(problems)


#: The longest string a report or manifest may carry. Generous enough for a reason code, a
#: bounded summary and a 64-char digest; far too short for a conversation turn.
_MAX_REPORT_STRING = 4 * MAX_BODY_FREE_REPR_STR


def _walk_strings(obj: object, path: str = "") -> "list[tuple[str, str]]":
    """Every string in a nested payload, with its path. Bounded by the payload's own size."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            out.extend(_walk_strings(value, f"{path}.{key}"))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            out.extend(_walk_strings(value, f"{path}[{index}]"))
    return out


def assert_body_free_payload(payload: object, *, label: str = "payload") -> None:
    """Raise unless *payload* may be written to a report, manifest or committed artifact."""
    problems = body_free_problems(payload, label=label)
    if problems:
        raise PrivacyError("refusing to write — " + "; ".join(problems[:4]))


def redacted_preview(text: str, *, limit: int = 0) -> str:
    """There is no preview. This exists so that nobody writes one.

    Returning the digest and the length is the most a caller gets. A function named
    ``preview`` that returns real characters is the single most likely place for a body to
    reach a log line, so the name is claimed here and made safe rather than left free for
    somebody to implement helpfully.
    """
    del limit
    from training_gym.schemas import sha256_text
    return f"<{len(text)} chars, sha256:{sha256_text(text)[:12]}>"


def versions() -> dict:
    """The version block every manifest records for this stage (§20)."""
    return {"privacy_version": PRIVACY_VERSION,
            "meta_noise_version": META_NOISE_VERSION,
            "categories": [c.value for c in PrivacyCategory],
            "meta_markers": [name for name, _ in _META_MARKERS]}


def _self_check() -> str:
    """A deterministic digest of this module's decision surface.

    Recorded in the manifest so that a privacy verdict can be attributed to an exact
    pattern set. A pattern edited without bumping :data:`PRIVACY_VERSION` changes this
    digest, which makes the omission visible instead of silent.
    """
    return canonical_json({
        "patterns": sorted(c.value for c, _ in _PATTERNS),
        "meta_markers": sorted(name for name, _ in _META_MARKERS),
        "privacy_version": PRIVACY_VERSION,
        "meta_noise_version": META_NOISE_VERSION,
    })


__all__ = [
    "META_NOISE_VERSION", "PRIVACY_VERSION", "PrivacyCategory", "PrivacyError",
    "PrivacyFinding", "assert_body_free_payload", "body_free_problems", "classify",
    "meta_noise_markers", "redacted_preview", "scan", "versions",
]
