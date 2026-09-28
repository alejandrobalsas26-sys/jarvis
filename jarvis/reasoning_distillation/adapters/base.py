"""reasoning_distillation/adapters/base.py — V69 M67A: the ingestion contract.

WHAT AN ADAPTER IS ALLOWED TO DO
--------------------------------
Exactly one thing: turn bytes into a sequence of ``(role, text)`` segments plus whatever
metadata the export genuinely carried. It does NOT normalize, classify, deduplicate,
distil or judge. Those are later stages with their own versions, and an adapter that did
any of them would make the pipeline's stage boundaries unfalsifiable.

WHAT AN ADAPTER MUST NEVER DO
-----------------------------
**Invent a field.** §7 is explicit: *never invent missing fields; missing = UNKNOWN /
absent*. An export with no model name yields ``source_model=""``, not a guess from the
filename. An export with no timestamps yields ``source_date=""``, not the file's mtime — a
file's modification time is when it was *downloaded*, and recording it as the conversation
date would put a confident wrong number into every derived record's provenance.

**Guess a role.** A segment whose role cannot be established is
:attr:`~reasoning_distillation.models.TurnRole.UNKNOWN` and the conversation is
``NEEDS_HUMAN_REVIEW``. Guessing "this looks like reasoning" is the single most damaging
thing an adapter could do here, because the entire milestone rests on telling the
assistant's PROCESS apart from its ANSWER, and a mislabelled answer becomes a distilled
"procedure" that is really just a conclusion.

WHY THE REGISTRY NAMES WHAT IS *NOT* IMPLEMENTED
------------------------------------------------
§5 says: *do not implement adapters merely because they could theoretically exist*, and
*support real formats only after inspecting examples*. No corpus has been supplied, so
DOCX and HTML are deliberately ABSENT — and absent with a reason, via
:data:`DELIBERATELY_ABSENT`, rather than missing silently. A caller who hands the pipeline
a ``.docx`` gets :class:`AdapterNotImplemented` naming the format and the sample files
needed to build it, which is a far more useful answer than a stub that half-parses Word
XML and reports partial turns as a complete conversation.

The two formats that ARE implemented were chosen because their structure is decidable
without a sample:

  * ``json_export`` — a declared, validated contract this module OWNS. Its shape is
    documented in :mod:`reasoning_distillation.adapters.json_export`, so there is no
    guessing: a file either satisfies the contract or is refused.
  * ``text`` / ``markdown`` — role-prefixed transcripts, where the roles are recognised
    from a CLOSED marker table and anything unrecognised becomes ``UNKNOWN`` plus a review
    flag. It parses conservatively and reports what it could not place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from training_gym.schemas import SchemaError, sha256_text

from ..models import ReviewStatus, SourceType, TurnRole

#: Bump when any adapter's parsing behaviour changes for identical input. Recorded per
#: adapter in the manifest (§20), so a corpus rebuilt after an adapter fix is comparable.
ADAPTER_VERSION = "m67a.adapters.1"

#: Formats deliberately NOT implemented, and what would be needed to implement each.
#: Present in the registry so the gap is a recorded decision rather than an omission.
DELIBERATELY_ABSENT: dict[str, str] = {
    "docx": "needs sample .docx exports: the role structure of a Word-saved chat is "
            "layout, not markup, and guessing it would mislabel answers as reasoning",
    "html": "needs sample .html exports: the per-provider DOM differs and a generic "
            "tag-strip loses the role boundaries entirely",
    "pdf": "not requested by §5 and not inspected; a PDF of a chat has no role structure "
           "that survives text extraction",
}


class AdapterError(SchemaError):
    """A source file could not be ingested. Never a partially-parsed conversation."""


class AdapterNotImplemented(AdapterError):
    """This format is deliberately absent (§5). Names what is needed to add it."""


@dataclass(frozen=True)
class RawSegment:
    """One segment exactly as the source presented it. Pre-normalization.

    ``text`` is a BODY. This type deliberately has no ``__repr__`` override *and is never
    stored on a long-lived object* — it exists only inside an adapter call and is converted
    straight into a :class:`~reasoning_distillation.models.CanonicalTurn`, which IS
    body-free. Keeping it plain makes the asymmetry obvious to a reader: if you find a
    ``RawSegment`` being retained anywhere, that is the bug.
    """

    role: TurnRole
    text: str
    #: Why the role is what it is — the marker that matched, or "" when it did not. Used by
    #: the review path to tell "recognised as user" from "defaulted to user".
    role_evidence: str = ""

    @property
    def digest(self) -> str:
        return sha256_text(self.text)


@dataclass(frozen=True)
class AdapterResult:
    """What one adapter extracted from one file.

    ``review_reasons`` is not decoration. It is the channel through which an adapter reports
    what it could not establish, and :mod:`reasoning_distillation.normalization` turns a
    non-empty list into ``NEEDS_HUMAN_REVIEW``. An adapter that returns segments and an
    empty reason list is asserting that it understood the whole file.
    """

    source_type: SourceType
    conversation_id: str
    segments: tuple[RawSegment, ...]
    source_model: str = ""
    source_date: str = ""
    review_status: ReviewStatus = ReviewStatus.OK
    review_reasons: tuple[str, ...] = ()
    #: Metadata the export genuinely carried and this pipeline has no field for. Kept so
    #: nothing is lost; never promoted into a typed field by guesswork.
    extra: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"AdapterResult(source_type={self.source_type.value}, "
                f"conversation_id={self.conversation_id!r}, "
                f"segments={len(self.segments)}, review_status={self.review_status.value})")


@runtime_checkable
class SourceAdapter(Protocol):
    """The narrowest interface an ingestion format needs (§19).

    A Protocol rather than a base class: adapters hold no state and share no behaviour, and
    a shared base class would invite one to put normalization in it.
    """

    #: Stable name, recorded in the manifest.
    name: str
    #: The :class:`~reasoning_distillation.models.SourceType` this adapter produces.
    source_type: SourceType

    def sniff(self, *, filename: str, content: bytes) -> bool:
        """Whether this adapter claims *content*. Cheap, deterministic, no side effects."""
        ...

    def parse(self, *, filename: str, content: bytes) -> AdapterResult:
        """Extract segments. Raises :class:`AdapterError` rather than returning partial."""
        ...


def decode(content: bytes, *, filename: str) -> str:
    """Decode source bytes to text, deterministically and without losing anything.

    UTF-8 with ``surrogateescape``: a byte sequence that is not valid UTF-8 survives the
    round trip instead of being replaced by U+FFFD. That matters for identity — a lossy
    decode would make two different files hash the same after normalization — and it
    matters for honesty, because a replacement character silently rewrites the operator's
    material.
    """
    if not isinstance(content, bytes):
        raise AdapterError(f"{filename}: expected bytes, got {type(content).__name__}")
    text = content.decode("utf-8", "surrogateescape")
    # A UTF-8 BOM is an encoding artifact, not content. Left in place it would change the
    # first segment's digest depending on which editor saved the file.
    return text.lstrip("﻿")


def refuse_absent_format(extension: str) -> "AdapterNotImplemented":
    """Build the refusal for a deliberately-absent format (§5)."""
    ext = extension.lower().lstrip(".")
    reason = DELIBERATELY_ABSENT.get(ext)
    if reason:
        return AdapterNotImplemented(
            f"format {ext!r} is deliberately not implemented (§5): {reason}. "
            f"CORPUS_INPUT_REQUIRED — supply sample files of this format and the adapter "
            f"can be built against them rather than against an assumption")
    return AdapterNotImplemented(
        f"no adapter claims format {ext!r}, and it is not on the recorded "
        f"deliberately-absent list. An unrecognised format is refused rather than "
        f"best-effort parsed: a half-read conversation reports as a whole one")


__all__ = [
    "ADAPTER_VERSION", "DELIBERATELY_ABSENT", "AdapterError", "AdapterNotImplemented",
    "AdapterResult", "RawSegment", "SourceAdapter", "decode", "refuse_absent_format",
]
