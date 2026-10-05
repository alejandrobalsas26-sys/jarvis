"""reasoning_distillation/adapters/docx_export.py — V69 M67A.1: the DOCX adapter.

WHY THIS EXISTS NOW AND NOT AT M67A
-----------------------------------
M67A recorded ``docx`` in :data:`~reasoning_distillation.adapters.base.DELIBERATELY_ABSENT`
with a precise reason: *the role structure of a Word-saved chat is layout, not markup, and
guessing it would mislabel answers as reasoning.* That was not a placeholder. It was a
statement that the format could not be decided without a sample.

A real sample now exists, so the format can be decided — and the sample CONFIRMED the
original reason rather than refuting it. The observed document carries:

  * 286 ``w:p`` paragraphs and **zero** ``w:pStyle`` elements, so Word stored no style
    information that could separate a user turn from an assistant turn;
  * **zero** paragraph labels matching the closed role table;
  * no delimiter of any kind between what are plainly several distinct conversations.

So this adapter does NOT make that document ingestible. It makes the format *decidable*:
a Word-saved transcript that genuinely carries ``Human:`` / ``Claude:`` labels is now
ingested correctly, and one that carries no role structure is refused with the structural
counts that justify the refusal. The difference between those two outcomes is measured,
not assumed.

WHY A REFUSAL RATHER THAN 281 ``UNKNOWN`` TURNS
----------------------------------------------
:mod:`~reasoning_distillation.adapters.base` provides for a segment whose role cannot be
established: it becomes ``UNKNOWN`` and the conversation becomes ``NEEDS_HUMAN_REVIEW``.
That provision is for an individual turn inside an otherwise-structured conversation. It is
not a licence to import a document with no structure at all, for a reason that is specific
rather than stylistic:

**a boundary-less paragraph run has no conversation identity.** Importing it as one
conversation asserts that it IS one conversation, which the source does not say and which
the observed sample positively contradicts — its topics change completely from Active
Directory forest promotion to eBPF hooks to a Spanish-language curriculum. §7 forbids
synthesising a conversation id and presenting it as source truth, and "one file, therefore
one conversation" is exactly that synthesis. The honest answer is that the number of
conversations in such a document is UNKNOWN, and a corpus cannot contain an unknown number
of items.

:class:`~reasoning_distillation.adapters.transcript.TranscriptAdapter` already made this
call for the same reason, refusing rather than "imported as one unlabelled blob". This
adapter agrees with it, because the container is irrelevant to the question.

WHAT IS DELIBERATELY NOT READ
-----------------------------
``docProps/core.xml`` carries ``dc:creator`` and ``cp:lastModifiedBy`` — the name of the
human who saved the file. This adapter never opens it. Those fields would be the operator's
identity entering corpus provenance through a metadata side door, and §4 forbids user
identifiers in derived data. Nothing downstream needs them.

``dcterms:created`` is likewise NOT used as ``source_date``. It is when the *Word document*
was created, which for a pasted chat is when the operator did the pasting — a confident
wrong number for the conversation's date. §7's rule holds: missing stays UNKNOWN. This is
the same trap as the file mtime, which
:mod:`~reasoning_distillation.adapters.base` already refuses by name.

``w:delText`` is excluded from extracted text. It is content the author DELETED under
tracked changes; including it would resurrect text a human removed on purpose and attribute
it to the conversation.

HARDENING
---------
A ``.docx`` is a ZIP of XML supplied by the operator, and §5 says to treat the corpus as
untrusted private data. Two bounds, both following the convention already established by
:mod:`tools.sysmon_bridge`:

  * **Parsing is ``defusedxml``**, which forbids DTDs, entity declarations and external
    references outright. Its absence is FAIL-CLOSED — this adapter refuses rather than
    falling back to stdlib ``ElementTree``, because a fallback would silently reintroduce
    the entity-expansion surface the hardening exists to remove.
  * **A declared-size bound**, because ``defusedxml`` does not bound input volume and a ZIP
    member reports its uncompressed size before anything is decompressed. The bound is
    checked against that declared size FIRST, so a zip bomb is refused without being
    expanded.
"""
from __future__ import annotations

import io
import zipfile

from ..models import ReviewStatus, SourceType, TurnRole
from .base import AdapterError, AdapterResult, RawSegment
from .transcript import label_shaped, role_for_label

# Hardened XML parsing. NOTE the deliberate absence of an ElementTree fallback, for the
# reason given in the module docstring.
try:  # pragma: no cover - import-time branch, exercised by the fail-closed test
    from defusedxml.ElementTree import ParseError as _XMLParseError
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover
    _xml_fromstring = None

    class _XMLParseError(Exception):  # type: ignore[no-redef]
        """Stand-in so the except clause stays valid when defusedxml is absent."""


#: Bump when this adapter's parsing behaviour changes for identical input.
DOCX_ADAPTER_VERSION = "m67a1.docx.1"

#: WordprocessingML main namespace, as ElementTree spells a qualified name.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: The one ZIP member that holds the document body.
_DOCUMENT_MEMBER = "word/document.xml"

#: ZIP local-file-header magic. Checked before any ZIP machinery is invoked.
_ZIP_MAGIC = b"PK\x03\x04"

#: Refuse a ``word/document.xml`` whose DECLARED uncompressed size exceeds this. Generous
#: for prose (the observed sample declares ~180 KiB) and far below anything that threatens
#: the host. Checked before decompression, so the bound is what makes the check meaningful.
MAX_DOCUMENT_XML_BYTES = 32 * 1024 * 1024

#: A transcript needs at least a user side and an assistant side. One repeated label is a
#: document that happens to say ``Note:``, not a conversation. Mirrors
#: :meth:`TranscriptAdapter.sniff`'s rule deliberately.
MIN_DISTINCT_ROLES = 2


def _paragraph_text(paragraph) -> str:
    """The visible text of one ``w:p``, in document order.

    ``w:t`` carries text; ``w:tab`` and ``w:br``/``w:cr`` carry whitespace that is part of
    the layout and would otherwise silently join two lines into one word. ``w:delText`` is
    skipped — see the module docstring.
    """
    parts: list[str] = []
    for node in paragraph.iter():
        tag = node.tag
        if tag == f"{_W}t":
            parts.append(node.text or "")
        elif tag == f"{_W}tab":
            parts.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _leading_label(text: str) -> "tuple[TurnRole | None, str, str]":
    """Whether *text* opens a labelled turn. Returns ``(role, evidence, remainder)``.

    One paragraph is one line for labelling purposes: Word stores a paragraph break as a
    new ``w:p``, so a label and its body are separate paragraphs far more often than they
    share one. Both shapes are handled — ``Human: hello`` and ``Human:`` followed by the
    body — and the closed table is the single authority in either case.
    """
    stripped = text.strip()
    if not stripped:
        return (None, "", "")
    head, sep, tail = stripped.partition(":")
    if sep and len(head) <= 38:
        role = role_for_label(head)
        if role is not None:
            return (role, f"label:{head.strip().casefold()}", tail.strip())
    shaped = label_shaped(stripped)
    if shaped:
        role = role_for_label(shaped)
        if role is not None:
            return (role, f"label:{shaped.strip().casefold()}", "")
    return (None, "", "")


class DocxExportAdapter:
    """Word ``.docx`` exports of a chat.

    Claims any structurally valid DOCX and then decides it on CONTENT: ingested when the
    closed role table finds at least :data:`MIN_DISTINCT_ROLES` distinct roles, refused with
    structural counts when it does not. The registry documents why selection works this way
    — a file's extension produces a better refusal, never a parse.
    """

    name = "docx_export"
    source_type = SourceType.DOCX

    def sniff(self, *, filename: str, content: bytes) -> bool:
        """Whether this is a DOCX at all. Structure only; no role decision here."""
        del filename
        if not isinstance(content, bytes) or not content.startswith(_ZIP_MAGIC):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return _DOCUMENT_MEMBER in archive.namelist()
        except (zipfile.BadZipFile, OSError, ValueError):
            return False

    def _read_document_xml(self, *, filename: str, content: bytes) -> bytes:
        """The body member's bytes, with the zip-bomb bound applied before decompression."""
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                try:
                    info = archive.getinfo(_DOCUMENT_MEMBER)
                except KeyError as exc:
                    raise AdapterError(
                        f"{filename}: not a WordprocessingML document — the archive has no "
                        f"{_DOCUMENT_MEMBER} member. A ZIP that is not a .docx is refused "
                        f"rather than searched for something parseable") from exc
                if info.file_size > MAX_DOCUMENT_XML_BYTES:
                    raise AdapterError(
                        f"{filename}: {_DOCUMENT_MEMBER} declares "
                        f"{info.file_size} uncompressed bytes, over the "
                        f"{MAX_DOCUMENT_XML_BYTES} bound. Refused WITHOUT decompressing: "
                        f"the declared size is checked first precisely so a compression "
                        f"bomb never reaches the parser")
                return archive.read(_DOCUMENT_MEMBER)
        except zipfile.BadZipFile as exc:
            raise AdapterError(
                f"{filename}: not a readable ZIP container ({exc}). A truncated or "
                f"corrupt .docx is refused, never partially recovered") from exc

    def parse(self, *, filename: str, content: bytes) -> AdapterResult:
        if _xml_fromstring is None:
            raise AdapterError(
                f"{filename}: defusedxml is not importable, so DOCX parsing is refused. "
                f"This is FAIL-CLOSED by design: the stdlib XML parser would accept entity "
                f"declarations in operator-supplied data, and an adapter that quietly fell "
                f"back to it would remove the hardening without saying so")

        raw = self._read_document_xml(filename=filename, content=content)
        try:
            root = _xml_fromstring(raw)
        except _XMLParseError as exc:
            raise AdapterError(
                f"{filename}: {_DOCUMENT_MEMBER} is not well-formed XML ({exc}); refused "
                f"rather than regex-scraped for text") from exc
        except Exception as exc:  # defusedxml raises its own types for DTDs/entities
            raise AdapterError(
                f"{filename}: {_DOCUMENT_MEMBER} was rejected by the hardened XML parser "
                f"({type(exc).__name__}: {exc}). A document declaring DTDs, entities or "
                f"external references is refused, not parsed with the guard removed"
            ) from exc

        paragraphs = [_paragraph_text(p) for p in root.iter(f"{_W}p")]
        non_empty = [t for t in paragraphs if t.strip()]
        style_count = sum(1 for _ in root.iter(f"{_W}pStyle"))
        drawing_count = sum(1 for _ in root.iter(f"{_W}drawing"))

        if not non_empty:
            raise AdapterError(
                f"{filename}: the document holds {len(paragraphs)} paragraph(s) and no "
                f"text; an empty conversation is refused rather than imported as a "
                f"conversation with zero turns")

        # Pass one: what roles does the CLOSED table actually find? Decided before anything
        # is built, so the refusal path never half-constructs a conversation.
        labelled: list[tuple[int, TurnRole, str, str]] = []
        for index, text in enumerate(paragraphs):
            role, evidence, remainder = _leading_label(text)
            if role is not None:
                labelled.append((index, role, evidence, remainder))
        distinct_roles = {role for _, role, _, _ in labelled}

        structure = {
            "docx_adapter_version": DOCX_ADAPTER_VERSION,
            "paragraphs_total": len(paragraphs),
            "paragraphs_non_empty": len(non_empty),
            "paragraph_style_elements": style_count,
            "drawing_elements": drawing_count,
            "labelled_paragraphs": len(labelled),
            "distinct_roles_found": sorted(r.value for r in distinct_roles),
        }

        if len(distinct_roles) < MIN_DISTINCT_ROLES:
            raise AdapterError(
                f"{filename}: no recoverable role structure. "
                f"{len(non_empty)} non-empty paragraph(s), {style_count} paragraph-style "
                f"element(s), {len(labelled)} paragraph(s) matching the closed role table, "
                f"{len(distinct_roles)} distinct role(s) — below the {MIN_DISTINCT_ROLES} a "
                f"conversation requires. Word stored layout, not roles, so user turns "
                f"cannot be told from assistant turns and the document carries no "
                f"conversation boundary. Refused rather than imported as one synthetic "
                f"conversation of {len(non_empty)} UNKNOWN turns: that would assert a "
                f"conversation identity the source does not state. Supply the material as "
                f"the JSON contract, or add role labels from the closed table")

        # Pass two: group paragraphs under the most recent label. Only reached when the
        # document genuinely carries role structure.
        reasons: list[str] = []
        segments: list[RawSegment] = []
        current_role: TurnRole | None = None
        current_evidence = ""
        buffer: list[str] = []

        def flush() -> None:
            if current_role is None:
                return
            body = "\n".join(buffer).strip()
            if body:
                segments.append(RawSegment(
                    role=current_role, text=body, role_evidence=current_evidence))

        label_at = {index: (role, evidence, remainder)
                    for index, role, evidence, remainder in labelled}
        for index, text in enumerate(paragraphs):
            if index in label_at:
                flush()
                current_role, current_evidence, remainder = label_at[index]
                buffer = [remainder] if remainder else []
                continue
            buffer.append(text)
        flush()

        if not segments:
            raise AdapterError(
                f"{filename}: {len(labelled)} role label(s) were found but no turn had a "
                f"body; refused rather than imported as empty turns")

        leading = "\n".join(paragraphs[:labelled[0][0]]).strip()
        if leading:
            reasons.append(
                "text precedes the first role label; it is not imported as a turn because "
                "its role is unknown, and it is reported rather than dropped silently")
        if style_count == 0:
            reasons.append(
                "the document carries no paragraph-style markup, so role recovery rested "
                "entirely on textual labels; a Word export cannot corroborate them")
        if drawing_count:
            reasons.append(
                f"{drawing_count} embedded drawing(s)/image(s) are present and are NOT "
                f"ingested; any reasoning that depends on their content is incomplete")

        # source_model and source_date stay UNKNOWN. docProps is deliberately not read -
        # see the module docstring.
        return AdapterResult(
            source_type=SourceType.DOCX,
            conversation_id="",  # assigned by the ingest stage from the file digest
            segments=tuple(segments),
            source_model="",
            source_date="",
            review_status=(ReviewStatus.NEEDS_HUMAN_REVIEW if reasons else ReviewStatus.OK),
            review_reasons=tuple(reasons[:32]),
            extra=structure,
        )


__all__ = [
    "DOCX_ADAPTER_VERSION", "MAX_DOCUMENT_XML_BYTES", "MIN_DISTINCT_ROLES",
    "DocxExportAdapter",
]
