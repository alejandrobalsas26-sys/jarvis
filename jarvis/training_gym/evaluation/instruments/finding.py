"""training_gym/evaluation/instruments/finding.py — V69 M62 S4H: the body-safe finding.

WHY THIS EXISTS
---------------
The historical scorer recorded a security finding as one string: ``secret_pii:secret``.
That is enough to VETO a candidate and not enough to AUDIT the veto. A reviewer holding
it cannot answer any of the three questions the veto turns on:

  * WHICH rule fired?
  * WHAT structure did it match?
  * WAS a sensitive value actually exposed, or was a *label* mentioned?

The obvious fix — attach the matched substring — is the one fix that must never be made.
A finding is exported, committed, and eventually read by people who were never cleared to
see the value; a finding that quotes the credential IS the leak. So the finding is
STRUCTURED instead: it carries the provenance a reviewer needs and nothing a reader could
reconstruct the value from.

WHAT THIS IS NOT
----------------
It is not a policy. A finding says what was observed; whether an observation blocks a
candidate is decided by a gate, in a separate layer, against a frozen threshold. That
separation is deliberate: :data:`SemanticClass.UNKNOWN_SENSITIVE_SHAPE` exists precisely
so an ambiguous match can be RECORDED without silently becoming either a pass or a veto.

HISTORICAL SCOPE
----------------
Nothing in the historical scoring path imports this module. Receipts sealed before S4H
are verified by the code that sealed them, and their digests do not move because this
file exists. See ``V69_M62_S4H_EVALUATION_INSTRUMENT_HARDENING.md``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...schemas import SchemaError, canonical_json, sha256_text

#: Bumped when the finding's SHAPE changes. Separate from any detector's version and
#: from the evaluation protocol version: a detector may be replaced without the record
#: format moving, and the record format may gain a field without a protocol change.
FINDING_SCHEMA_VERSION = "m62.finding_schema.2"

#: Domain separator for :func:`evidence_digest`. See the threat model below.
_DIGEST_DOMAIN = "jarvis-finding-v2"

#: Field separator inside the digest preimage. A control character, so it cannot occur
#: in a rule id or a context class and cannot be produced by concatenation ambiguity
#: (``rule="a", ctx="bc"`` and ``rule="ab", ctx="c"`` must never share a preimage).
_DIGEST_SEP = "\x1f"


class FindingError(SchemaError):
    """A finding that would carry more than its contract allows."""


class Severity(str, Enum):
    """How much a finding matters, BEFORE any policy reads it."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceClass(str, Enum):
    """WHAT KIND of thing matched, stated without saying what it was."""

    #: A label and a value, bound by a structural relation (JSON pair, ``k: v``, a
    #: markdown table cell).
    LABELLED_VALUE = "labelled_value"
    #: A value whose own shape is credential-like, with no label needed.
    STANDALONE_VALUE = "standalone_value"
    #: A label with no value attached anywhere near it.
    LABEL_ONLY = "label_only"
    #: A multi-line block with begin/end delimiters (a key block, a certificate).
    DELIMITED_BLOCK = "delimited_block"
    #: Free prose that named a sensitive field without producing one.
    PROSE_MENTION = "prose_mention"


class ContextClass(str, Enum):
    """WHERE in the response the match sat. Structural, never a quotation."""

    JSON_OBJECT = "json_object"
    JSON_STRING = "json_string"
    MARKDOWN_TABLE = "markdown_table"
    CODE_FENCE = "code_fence"
    PROSE = "prose"
    TOOL_RESULT = "tool_result"
    UNKNOWN_CONTEXT = "unknown_context"


class PlaceholderState(str, Enum):
    """Whether the value position held a stand-in rather than a value."""

    NOT_A_PLACEHOLDER = "not_a_placeholder"
    PLACEHOLDER = "placeholder"
    NO_VALUE_POSITION = "no_value_position"


class RedactionState(str, Enum):
    """Whether the response itself performed a redaction."""

    NOT_REDACTED = "not_redacted"
    REDACTED = "redacted"
    EXPLICITLY_WITHHELD = "explicitly_withheld"
    NOT_APPLICABLE = "not_applicable"


class SemanticClass(str, Enum):
    """The classification that separates a LEAK from a MENTION.

    This is the vocabulary the historical single ``secret`` category could not express.
    Every member is a statement about the VALUE and its structural context, never about
    whether the word "secret" appeared.
    """

    #: A real-looking sensitive value was produced.
    ACTUAL_DISCLOSURE = "actual_disclosure"
    #: The value's SHAPE is credential-like but its provenance is undetermined.
    CREDENTIAL_SHAPED_DISCLOSURE = "credential_shaped_disclosure"
    #: Personal data was produced.
    PII_DISCLOSURE = "pii_disclosure"
    #: A sensitive FIELD was named; no value accompanied it.
    FIELD_MENTION_ONLY = "field_mention_only"
    #: The value position held a placeholder (``[REDACTED]``, ``<removed>``, ``***``).
    REDACTED_PLACEHOLDER = "redacted_placeholder"
    #: The response said in words that it was not going to produce the value.
    WITHHELD_VALUE = "withheld_value"
    #: A value the source marks as a public example, or a well-known documentation value.
    SYNTHETIC_PUBLIC_EXAMPLE = "synthetic_public_example"
    #: Something sensitive-SHAPED that this instrument cannot classify. Recorded, never
    #: silently passed and never automatically a blocker.
    UNKNOWN_SENSITIVE_SHAPE = "unknown_sensitive_shape"

    @property
    def is_disclosure(self) -> bool:
        """Whether this class asserts a sensitive value actually reached the output."""
        return self in (SemanticClass.ACTUAL_DISCLOSURE,
                        SemanticClass.CREDENTIAL_SHAPED_DISCLOSURE,
                        SemanticClass.PII_DISCLOSURE)

    @property
    def is_undetermined(self) -> bool:
        return self is SemanticClass.UNKNOWN_SENSITIVE_SHAPE


def evidence_digest(*, rule_id: str, detector_version: str, source_component: str,
                    context_class: str, raw_value: str) -> str:
    """A digest of sensitive evidence that a dictionary attack cannot invert cheaply.

    THREAT MODEL
    ------------
    A bare ``sha256(value)`` over a short secret is not a one-way function in practice.
    Credentials are drawn from small, guessable spaces — a four-digit PIN, a default
    password, a token with a known 8-character prefix — and an attacker holding the
    digest can enumerate that space offline in seconds. Publishing such a digest beside
    a finding that says "this is an API key" publishes the key.

    So the preimage is DOMAIN-SEPARATED and CONTEXT-BOUND: a fixed domain tag, then the
    rule, the detector version, the component and the structural context, then the value,
    joined by a control character that none of the other fields may contain. Two
    consequences follow. A rainbow table built for plain SHA-256 is useless. And the same
    value found by a different rule, or in a different component, produces a DIFFERENT
    digest — so a digest cannot be used to correlate one occurrence with another across
    contexts, which is the correlation an attacker would otherwise get for free.

    WHAT THIS STILL DOES NOT DO
    ---------------------------
    It is not a KDF and it is not keyed. An attacker who knows the rule id, the detector
    version, the component and the context — all of which the finding publishes — can
    still enumerate a SMALL value space against this digest. It raises the cost of an
    offline attack and narrows correlation; it does not make a four-digit PIN safe. The
    protection that actually holds is that the value is never stored, and the digest is
    OPTIONAL: a caller with no need for occurrence-matching should pass none.
    """
    for name, part in (("rule_id", rule_id), ("detector_version", detector_version),
                       ("source_component", source_component),
                       ("context_class", context_class)):
        if _DIGEST_SEP in str(part):
            raise FindingError(
                f"evidence_digest: {name} contains the field separator, which would "
                f"make two different findings share a preimage")
    preimage = _DIGEST_SEP.join((_DIGEST_DOMAIN, str(rule_id), str(detector_version),
                                 str(source_component), str(context_class),
                                 str(raw_value)))
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstrumentFinding:
    """One structured observation by one versioned instrument rule.

    Every field is a CLOSED VOCABULARY, an identifier, a count or a digest. There is no
    field that can hold response text, and :meth:`__post_init__` refuses one that tries.
    """

    detector_version: str
    rule_id: str
    rule_class: str
    family: str
    category: str
    evidence_class: EvidenceClass
    context_class: ContextClass
    semantic_class: SemanticClass
    severity: Severity
    source_component: str
    match_length: int
    placeholder_state: PlaceholderState = PlaceholderState.NOT_A_PLACEHOLDER
    redaction_state: RedactionState = RedactionState.NOT_APPLICABLE
    sensitive_value_present: bool = False
    #: Optional. Absent by default: see the threat model on :func:`evidence_digest`.
    evidence_digest: str = ""
    schema_version: str = FINDING_SCHEMA_VERSION
    #: Body-free structural counts a reviewer may want (occurrences, line index, ...).
    counters: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("detector_version", "rule_id", "rule_class", "family", "category",
                     "source_component"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise FindingError(f"finding: {name} must be a non-empty identifier")
            if len(value) > 120:
                raise FindingError(
                    f"finding: {name} is {len(value)} characters; an identifier this "
                    f"long is a body wearing a field name")
        for name, enum_type in (("evidence_class", EvidenceClass),
                                ("context_class", ContextClass),
                                ("semantic_class", SemanticClass),
                                ("severity", Severity),
                                ("placeholder_state", PlaceholderState),
                                ("redaction_state", RedactionState)):
            if not isinstance(getattr(self, name), enum_type):
                raise FindingError(f"finding: {name} must be a {enum_type.__name__}")
        if not isinstance(self.match_length, int) or self.match_length < 0:
            raise FindingError("finding: match_length must be a non-negative integer")
        if self.evidence_digest and (len(self.evidence_digest) != 64
                                     or not all(c in "0123456789abcdef"
                                                for c in self.evidence_digest)):
            raise FindingError(
                "finding: evidence_digest must be 64 lowercase hex characters or "
                "absent; anything else is a value in a digest's clothing")
        if not isinstance(self.counters, dict):
            raise FindingError("finding: counters must be a mapping")
        for key, value in self.counters.items():
            if not isinstance(key, str) or not isinstance(value, int):
                raise FindingError(
                    "finding: counters map identifiers to integers only; a string "
                    "counter is a body-shaped hole")
        if self.sensitive_value_present and not self.semantic_class.is_disclosure:
            raise FindingError(
                f"finding: sensitive_value_present is true but the semantic class is "
                f"{self.semantic_class.value}, which asserts no value reached the "
                f"output; the two must agree or a reviewer is told two things")

    @property
    def finding_id(self) -> str:
        """A stable identity for this finding's SHAPE. Derived, never stored twice."""
        return sha256_text(canonical_json(self.to_dict()))[:32]

    def to_dict(self) -> dict:
        """The canonical body-free mapping. Deterministic across hosts and runs.

        Contains no timestamp: a content hash that moved every time it was taken would
        make the round-trip in :meth:`canonical_bytes` untestable, and would say a
        finding changed when only the clock did.
        """
        return {
            "category": self.category,
            "context_class": self.context_class.value,
            "counters": dict(sorted(self.counters.items())),
            "detector_version": self.detector_version,
            "evidence_class": self.evidence_class.value,
            "evidence_digest": self.evidence_digest,
            "family": self.family,
            "match_length": self.match_length,
            "placeholder_state": self.placeholder_state.value,
            "redaction_state": self.redaction_state.value,
            "rule_class": self.rule_class,
            "rule_id": self.rule_id,
            "schema_version": self.schema_version,
            "semantic_class": self.semantic_class.value,
            "sensitive_value_present": self.sensitive_value_present,
            "severity": self.severity.value,
            "source_component": self.source_component,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")

    @classmethod
    def from_dict(cls, payload: Any) -> "InstrumentFinding":
        """Rebuild from :meth:`to_dict`. Refuses an unknown vocabulary member."""
        if not isinstance(payload, dict):
            raise FindingError("finding: a serialized finding must be a mapping")
        version = payload.get("schema_version")
        if version != FINDING_SCHEMA_VERSION:
            raise FindingError(
                f"finding: schema_version {version!r} is not {FINDING_SCHEMA_VERSION}; "
                f"a reader that guessed would decode a different record's fields")
        try:
            return cls(
                detector_version=payload["detector_version"],
                rule_id=payload["rule_id"],
                rule_class=payload["rule_class"],
                family=payload["family"],
                category=payload["category"],
                evidence_class=EvidenceClass(payload["evidence_class"]),
                context_class=ContextClass(payload["context_class"]),
                semantic_class=SemanticClass(payload["semantic_class"]),
                severity=Severity(payload["severity"]),
                source_component=payload["source_component"],
                match_length=payload["match_length"],
                placeholder_state=PlaceholderState(payload["placeholder_state"]),
                redaction_state=RedactionState(payload["redaction_state"]),
                sensitive_value_present=bool(payload["sensitive_value_present"]),
                evidence_digest=payload.get("evidence_digest", ""),
                schema_version=version,
                counters=dict(payload.get("counters", {})),
            )
        except KeyError as exc:  # noqa: PERF203 - one clear message beats a KeyError
            raise FindingError(f"finding: serialized record omits {exc.args[0]!r}") from None
        except ValueError as exc:
            raise FindingError(f"finding: {exc}") from None

    def __repr__(self) -> str:
        """Body-free by construction, because a repr is how bodies escape.

        Every field is already a closed vocabulary or an identifier, so this could have
        been the default. It is written out anyway: the default renders whatever fields
        a future contributor adds, and this one renders only what is listed here.
        """
        return (f"InstrumentFinding(rule_id={self.rule_id!r}, "
                f"semantic_class={self.semantic_class.value!r}, "
                f"severity={self.severity.value!r}, "
                f"source_component={self.source_component!r}, "
                f"match_length={self.match_length})")


def findings_to_payload(findings: "tuple[InstrumentFinding, ...]") -> list:
    """A deterministic list form. Sorted, so two equal sets serialize identically."""
    return sorted((f.to_dict() for f in findings),
                  key=lambda d: (d["source_component"], d["rule_id"],
                                 d["semantic_class"], d["match_length"],
                                 d["evidence_digest"]))


__all__ = ["ContextClass", "EvidenceClass", "FINDING_SCHEMA_VERSION", "FindingError",
           "InstrumentFinding", "PlaceholderState", "RedactionState", "SemanticClass",
           "Severity", "evidence_digest", "findings_to_payload"]
