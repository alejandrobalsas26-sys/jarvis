"""reasoning_distillation/normalization.py — V69 M67A: source to canonical, losing nothing.

WHAT NORMALIZATION IS FOR
-------------------------
An adapter answers "what did this file say". Normalization answers "what is this, in the
pipeline's own terms", and it is the stage that attaches the three things every later stage
depends on:

  * **identity** (§6) — a :class:`~reasoning_distillation.models.SourceProvenance` per
    segment, derived from the file's bytes and the segment's bytes, never from a filename;
  * **an explicit privacy status** (§4) — via :mod:`reasoning_distillation.privacy`, so no
    record exists without one;
  * **a review disposition** (§7) — malformed or ambiguous source becomes
    ``NEEDS_HUMAN_REVIEW`` rather than being repaired into looking fine.

WHY WHITESPACE NORMALIZATION IS ALMOST ABSENT HERE
--------------------------------------------------
The instinct is to tidy the text: collapse blank lines, strip trailing spaces, unify
quotes. This module does almost none of that, and the reason is recorded in
:mod:`training_gym.datasets.similarity` at length: **every normalization step deletes
information, and the question is always whose.** In this corpus specifically —

  * indentation IS the meaning in a Python snippet, and historical reasoning traces are
    full of them. A ``return`` inside an ``if`` and the same ``return`` outside it are the
    bug and the fix;
  * a paragraph break separates ``"I cannot help."`` from ``"But here is how:"``;
  * the shape of a stack trace is how a trace demonstrates a diagnostic procedure.

So normalization here does exactly three things to the text, all of them reversible in
meaning if not in bytes: it fixes line endings, strips a BOM (done in the adapter), and
trims leading/trailing blank lines from a segment. Nothing else. The aggressive,
family-aware normalization belongs to the SIMILARITY stage, where it is applied to a
throwaway comparison key and never to the stored text — which is why
:mod:`reasoning_distillation.dedupe` can afford to casefold and this module cannot.

IDEMPOTENCE (§6)
----------------
:func:`ingest_bytes` is a pure function of ``(filename-extension, bytes)``. The same bytes
always produce the same ``conversation_id``, the same ``unit_id`` per turn and the same
conversation ``digest``. Re-ingesting a file the corpus already holds therefore produces an
IDENTICAL record, which is what makes "repeated ingestion must be idempotent" a property of
the data rather than a duty of the caller.

The one input that is NOT part of identity is the filename. A conversation downloaded twice
under different names is one conversation; two different conversations saved under the same
name are two. That is the §6 rule "do not use filename alone as identity", and here it is
strengthened: the filename is not part of identity at all, only its extension influences
which adapter claims the file.
"""
from __future__ import annotations

from dataclasses import replace

from training_gym.schemas import SensitivityClass, sha256_text

from . import adapters
from .config import PrivacyConfig
from .models import (
    CanonicalConversation,
    CanonicalTurn,
    DistillationError,
    ReviewStatus,
    SourceProvenance,
    TurnRole,
)
from .privacy import classify, meta_noise_markers

#: Bump when this module changes what a canonical conversation looks like for identical
#: input. Mirrors :data:`reasoning_distillation.models.NORMALIZATION_VERSION`, which is what
#: gets stamped onto each record; this constant exists so the manifest can record the stage.
from .models import NORMALIZATION_VERSION  # noqa: E402  (re-exported deliberately)

#: Segments shorter than this, after trimming, carry no recoverable decision. They are kept
#: (source is never destroyed) and marked, so the corpus report can show how much of an
#: export was "ok", "sure", "thanks".
MIN_MEANINGFUL_CHARS = 12


def _trim(text: str) -> str:
    """Line endings unified; leading and trailing blank lines removed. Nothing else.

    Deliberately NOT ``.strip()``: that would remove the leading indentation of a segment
    whose first line is inside a code block, which changes what the code means.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = unified.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def derive_conversation_id(*, source_file_hash: str, declared: str = "") -> str:
    """The conversation's stable id.

    A declared id from the source is preferred — a real export knows its own id, and honouring
    it means two files exported from the same conversation collapse correctly. When there is
    none, the id is derived from the FILE DIGEST, which keeps it deterministic and keeps the
    filename out of identity (§6).

    The derived form is prefixed so the two cases are distinguishable forever. A reviewer
    asking "did this conversation declare its own id or did we invent one" can answer from the
    id alone, which matters because a declared id is evidence about provenance and a derived
    one is not.
    """
    if declared.strip():
        return declared.strip()[:200]
    return f"derived-{source_file_hash[:32]}"


def normalize(result: adapters.AdapterResult, *, source_file_hash: str,
              path_hint: str = "", config: PrivacyConfig | None = None,
              sensitivity: SensitivityClass = SensitivityClass.INTERNAL,
              ) -> CanonicalConversation:
    """Turn one :class:`~reasoning_distillation.adapters.AdapterResult` into canonical form.

    Every segment gets a validated provenance and an explicit privacy assessment. Segments
    are NOT reordered, merged or dropped: ``turn_index`` is the position in the source, and
    that ordering is the only evidence the corpus has that a correction came after the thing
    it corrected.
    """
    cfg = config or PrivacyConfig()
    conversation_id = derive_conversation_id(
        source_file_hash=source_file_hash, declared=result.conversation_id)

    turns: list[CanonicalTurn] = []
    reasons: list[str] = list(result.review_reasons)
    for index, segment in enumerate(result.segments):
        text = _trim(segment.text)
        provenance = SourceProvenance(
            source_file_hash=source_file_hash,
            source_type=result.source_type,
            conversation_id=conversation_id,
            turn_index=index,
            role=segment.role,
            raw_segment_hash=sha256_text(segment.text),
            source_model=result.source_model,
            source_date=result.source_date,
            source_path_hint=path_hint,
        ).validated()

        turn_reasons: list[str] = []
        if segment.role is TurnRole.UNKNOWN:
            turn_reasons.append(
                "role could not be established from the source; recorded as UNKNOWN "
                "rather than guessed (§7)")
        if len(text) < MIN_MEANINGFUL_CHARS:
            turn_reasons.append(
                f"segment is {len(text)} chars, below the {MIN_MEANINGFUL_CHARS}-char "
                f"meaningful floor; kept, and excluded from distillation input")

        turns.append(CanonicalTurn(
            provenance=provenance,
            role=segment.role,
            text=text,
            privacy=classify(text, config=cfg, sensitivity=sensitivity),
            review_status=(ReviewStatus.NEEDS_HUMAN_REVIEW if turn_reasons
                           else ReviewStatus.OK),
            review_reasons=tuple(turn_reasons),
            meta_noise_markers=meta_noise_markers(text, config=cfg),
        ))

    unknown_roles = sum(1 for t in turns if t.role is TurnRole.UNKNOWN)
    if unknown_roles:
        reasons.append(
            f"{unknown_roles} of {len(turns)} turn(s) have an UNKNOWN role, so the "
            f"reasoning/answer boundary is not established for the whole conversation")
    if not any(t.role is TurnRole.USER_REQUEST for t in turns):
        reasons.append(
            "no USER_REQUEST turn: the task this conversation was solving is not in the "
            "export, so goal and constraints cannot be extracted from it (§7)")

    status = result.review_status
    if reasons and status is ReviewStatus.OK:
        status = ReviewStatus.NEEDS_HUMAN_REVIEW

    return CanonicalConversation(
        conversation_id=conversation_id,
        source_file_hash=source_file_hash,
        source_type=result.source_type,
        turns=tuple(turns),
        review_status=status,
        review_reasons=tuple(reasons[:32]),
        source_model=result.source_model,
        source_date=result.source_date,
        family_id="",  # assigned by dedupe; a conversation cannot compute its own family
    )


def ingest_bytes(*, filename: str, content: bytes,
                 config: PrivacyConfig | None = None,
                 sensitivity: SensitivityClass = SensitivityClass.INTERNAL,
                 ) -> CanonicalConversation:
    """Select an adapter, parse and normalize. Pure, deterministic, idempotent (§6).

    ``filename`` influences adapter SELECTION and the recorded ``source_path_hint``
    (basename only), never identity. Passing the same bytes under a different name yields a
    conversation with the same id, the same turn ids and the same digest.
    """
    if not isinstance(content, bytes):
        raise DistillationError(
            f"ingest expects bytes, got {type(content).__name__}; decoding at the call site "
            f"would make the file digest depend on the caller's codec choice")
    source_file_hash = sha256_text(content.decode("utf-8", "surrogateescape"))
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    result = adapters.parse(filename=basename, content=content)
    return normalize(result, source_file_hash=source_file_hash, path_hint=basename,
                     config=config, sensitivity=sensitivity)


def with_family(conversation: CanonicalConversation, family_id: str) -> CanonicalConversation:
    """Attach the family assigned by :mod:`reasoning_distillation.dedupe`.

    A separate function rather than a mutable field: :class:`CanonicalConversation` is
    frozen, and a family assignment is an event that produces a new value. That keeps the
    pre-grouping and post-grouping objects distinguishable, which is what lets the leakage
    tests assert that nothing reaches the split stage ungrouped.
    """
    if not family_id:
        raise DistillationError(
            f"{conversation.conversation_id}: refusing to attach an empty family_id; an "
            f"ungrouped conversation can cross a split boundary (§9)")
    return replace(conversation, family_id=family_id)


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {
        "normalization_version": NORMALIZATION_VERSION,
        "min_meaningful_chars": MIN_MEANINGFUL_CHARS,
        "text_transforms": ["unify_line_endings", "trim_blank_edges"],
        "note": "deliberately does NOT casefold, collapse whitespace or reflow: "
                "indentation and blank lines carry meaning in this corpus. Aggressive "
                "normalization happens only on throwaway similarity keys (dedupe stage).",
    }


__all__ = [
    "MIN_MEANINGFUL_CHARS", "NORMALIZATION_VERSION", "derive_conversation_id",
    "ingest_bytes", "normalize", "versions", "with_family",
]
