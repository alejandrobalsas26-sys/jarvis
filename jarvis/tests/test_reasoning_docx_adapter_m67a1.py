"""V69 M67A.1 — the DOCX adapter, against SYNTHETIC structure only.

Every fixture here is built by :func:`docx_bytes` from paragraph strings written for this
file. None of it is copied from the operator's corpus: §26 requires fixtures that match a
real format's STRUCTURE without carrying its bodies, and a test that pasted real
conversation text would put private material into Git permanently.

The structural facts these fixtures reproduce were measured from the real export and are
recorded in docs/v69_M67A1_REAL_LEGACY_CORPUS_QUALIFICATION.md: a flat run of ``w:p``
paragraphs, no ``w:pStyle`` anywhere, an embedded drawing, and repeated paragraphs.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from reasoning_distillation import adapters
from reasoning_distillation.adapters import docx_export
from reasoning_distillation.adapters.base import AdapterError
from reasoning_distillation.models import ReviewStatus, SourceType, TurnRole

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _para(text: str, *, style: str = "", delete: bool = False) -> str:
    """One ``w:p``. ``style`` adds a ``w:pStyle``; ``delete`` marks the run as deleted."""
    tag = "w:delText" if delete else "w:t"
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{style_xml}<w:r><{tag} xml:space="preserve">{text}</{tag}></w:r></w:p>'


def docx_bytes(body_xml: str, *, include_document: bool = True,
               extra_members: "dict[str, bytes] | None" = None) -> bytes:
    """A minimal structurally-valid DOCX carrying *body_xml* inside ``w:body``."""
    document = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<w:document xmlns:w="{_W}"><w:body>{body_xml}</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        if include_document:
            archive.writestr("word/document.xml", document)
        for name, payload in (extra_members or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def labelled_docx() -> bytes:
    """A Word-saved transcript that DOES carry role labels. The ingestible shape."""
    return docx_bytes(
        _para("Human:") + _para("Deploy the service to staging.")
        + _para("Claude:") + _para("I will run the staging deploy and report the result."))


# ── sniff: structure only, never a role decision ────────────────────────────────────

def test_sniff_claims_a_structurally_valid_docx():
    assert docx_export.DocxExportAdapter().sniff(
        filename="chat.docx", content=labelled_docx()) is True


def test_sniff_refuses_bytes_that_are_not_a_zip():
    adapter = docx_export.DocxExportAdapter()
    assert adapter.sniff(filename="chat.docx", content=b"not a zip at all") is False
    assert adapter.sniff(filename="chat.docx", content=b"PK\x03\x04") is False


def test_sniff_refuses_a_zip_without_a_word_document_member():
    content = docx_bytes("", include_document=False)
    assert docx_export.DocxExportAdapter().sniff(
        filename="notes.docx", content=content) is False


def test_a_zip_without_the_document_member_is_refused_by_name():
    with pytest.raises(AdapterError, match="no\n?\\s*word/document.xml member|word/document.xml"):
        docx_export.DocxExportAdapter().parse(
            filename="notes.docx", content=docx_bytes("", include_document=False))


# ── the real corpus shape: no recoverable role structure → refusal ──────────────────

def test_a_role_less_document_is_refused_with_structural_counts():
    """The shape the real export actually has. §7: never synthesise a conversation id."""
    body = "".join(_para(f"Now I am considering option {i} in some detail.")
                   for i in range(40))
    with pytest.raises(AdapterError) as excinfo:
        adapters.parse(filename="pasted.docx", content=docx_bytes(body))
    message = str(excinfo.value)
    assert "no recoverable role structure" in message
    assert "40 non-empty paragraph(s)" in message
    assert "0 paragraph-style element(s)" in message
    assert "UNKNOWN turns" in message, "the refusal must say what it declined to invent"


def test_one_role_alone_is_not_a_conversation():
    """Two DIFFERENT roles, not two occurrences of one."""
    body = _para("Claude:") + _para("An answer.") + _para("Claude:") + _para("Another.")
    with pytest.raises(AdapterError, match="1 distinct role"):
        adapters.parse(filename="one_sided.docx", content=docx_bytes(body))


def test_the_refusal_names_the_route_forward():
    body = "".join(_para(f"paragraph {i} with enough text to count") for i in range(3))
    with pytest.raises(AdapterError, match="JSON contract"):
        adapters.parse(filename="pasted.docx", content=docx_bytes(body))


# ── the ingestible shape ────────────────────────────────────────────────────────────

def test_a_labelled_word_transcript_is_ingested_with_both_roles():
    result = adapters.parse(filename="chat.docx", content=labelled_docx())
    assert result.source_type is SourceType.DOCX
    assert [s.role for s in result.segments] == [
        TurnRole.USER_REQUEST, TurnRole.ASSISTANT_ANSWER]
    assert "staging" in result.segments[0].text


def test_an_inline_label_and_a_standalone_label_reach_the_same_role():
    inline = adapters.parse(
        filename="a.docx",
        content=docx_bytes(_para("Human: hello there") + _para("Claude: hi back")))
    standalone = adapters.parse(filename="b.docx", content=labelled_docx())
    assert [s.role for s in inline.segments] == [s.role for s in standalone.segments]


def test_markdown_style_labels_normalise_too():
    body = (_para("## Human") + _para("A question.")
            + _para("**Claude**") + _para("An answer."))
    result = adapters.parse(filename="md_ish.docx", content=docx_bytes(body))
    assert [s.role for s in result.segments] == [
        TurnRole.USER_REQUEST, TurnRole.ASSISTANT_ANSWER]


def test_a_multi_turn_conversation_keeps_document_order():
    body = (_para("Human:") + _para("one") + _para("Claude:") + _para("two")
            + _para("Human:") + _para("three") + _para("Claude:") + _para("four"))
    result = adapters.parse(filename="multi.docx", content=docx_bytes(body))
    assert [s.text for s in result.segments] == ["one", "two", "three", "four"]


def test_the_closed_table_is_shared_with_the_transcript_adapter():
    """One table, two containers. A second copy would be a second place to drift."""
    from reasoning_distillation.adapters import transcript
    assert transcript.role_for_label("usuario") is TurnRole.USER_REQUEST
    assert transcript.role_for_label("not-a-known-label") is None


# ── no silent fabrication ───────────────────────────────────────────────────────────

def test_source_model_and_date_stay_unknown_even_though_docprops_carries_dates():
    """§7: missing is UNKNOWN. dcterms:created is when WORD saved, not when they spoke."""
    core = (b'<?xml version="1.0"?><cp:coreProperties '
            b'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            b'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            b'xmlns:dcterms="http://purl.org/dc/terms/">'
            b'<dc:creator>A Real Human Name</dc:creator>'
            b'<dcterms:created>2026-09-25T08:22:00Z</dcterms:created>'
            b'</cp:coreProperties>')
    content = docx_bytes(
        _para("Human:") + _para("q") + _para("Claude:") + _para("a"),
        extra_members={"docProps/core.xml": core})
    result = adapters.parse(filename="dated.docx", content=content)
    assert result.source_date == "", "a Word save date is not a conversation date"
    assert result.source_model == ""


def test_the_operator_identity_in_docprops_never_reaches_the_result():
    """§4: no user identifiers in derived data. The adapter must not open docProps."""
    name = "Identifiable Person"
    core = (f'<?xml version="1.0"?><cp:coreProperties '
            f'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:creator>{name}</dc:creator>'
            f'<cp:lastModifiedBy>{name}</cp:lastModifiedBy>'
            f'</cp:coreProperties>').encode()
    content = docx_bytes(
        _para("Human:") + _para("q") + _para("Claude:") + _para("a"),
        extra_members={"docProps/core.xml": core})
    result = adapters.parse(filename="named.docx", content=content)
    haystack = repr(result) + repr(result.extra) + "".join(
        s.text + s.role_evidence for s in result.segments)
    assert name not in haystack


def test_conversation_id_is_left_for_the_ingest_stage_to_derive():
    """A per-file id invented by the adapter would compete with the digest-derived one."""
    assert adapters.parse(filename="chat.docx", content=labelled_docx()).conversation_id == ""


def test_deleted_text_is_not_resurrected():
    """w:delText is content a human removed under tracked changes."""
    body = (_para("Human:") + _para("kept text")
            + _para("SECRET-REMOVED-SENTENCE", delete=True)
            + _para("Claude:") + _para("answer"))
    result = adapters.parse(filename="tracked.docx", content=docx_bytes(body))
    assert "SECRET-REMOVED-SENTENCE" not in "".join(s.text for s in result.segments)


# ── review reasons report what could not be established ─────────────────────────────

def test_absent_style_markup_is_reported_rather_than_assumed_harmless():
    result = adapters.parse(filename="chat.docx", content=labelled_docx())
    assert result.review_status is ReviewStatus.NEEDS_HUMAN_REVIEW
    assert any("paragraph-style markup" in r for r in result.review_reasons)


def test_an_embedded_drawing_is_reported_as_not_ingested():
    body = (_para("Human:") + _para("see the attached diagram")
            + '<w:p><w:r><w:drawing/></w:r></w:p>'
            + _para("Claude:") + _para("answer"))
    result = adapters.parse(filename="withimage.docx", content=docx_bytes(body))
    assert result.extra["drawing_elements"] == 1
    assert any("drawing" in r for r in result.review_reasons)


def test_text_before_the_first_label_is_reported_not_dropped():
    body = (_para("Some preamble with no role at all.")
            + _para("Human:") + _para("q") + _para("Claude:") + _para("a"))
    result = adapters.parse(filename="preamble.docx", content=docx_bytes(body))
    assert any("precedes the first role label" in r for r in result.review_reasons)


def test_styles_present_means_the_style_reason_is_absent():
    """Non-vacuity for the style reason: it must not fire unconditionally."""
    body = (_para("Human:", style="Heading1") + _para("q")
            + _para("Claude:", style="Heading1") + _para("a"))
    result = adapters.parse(filename="styled.docx", content=docx_bytes(body))
    assert result.extra["paragraph_style_elements"] == 2
    assert not any("paragraph-style markup" in r for r in result.review_reasons)


def test_the_structure_record_carries_counts_and_no_bodies():
    result = adapters.parse(filename="chat.docx", content=labelled_docx())
    extra = result.extra
    assert extra["paragraphs_non_empty"] == 4
    assert extra["docx_adapter_version"] == docx_export.DOCX_ADAPTER_VERSION
    assert "staging" not in repr(extra), "structure metadata must hold no conversation text"


# ── malformed and hostile input ─────────────────────────────────────────────────────

def test_a_corrupt_zip_is_refused_not_partially_recovered():
    with pytest.raises(AdapterError, match="readable ZIP|refused"):
        docx_export.DocxExportAdapter().parse(
            filename="broken.docx", content=b"PK\x03\x04" + b"\x00" * 64)


def test_a_document_that_is_not_well_formed_xml_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><w:body></not-closed>")
    with pytest.raises(AdapterError, match="well-formed|rejected by the hardened"):
        adapters.parse(filename="malformed.docx", content=buffer.getvalue())


def test_an_empty_document_is_refused_rather_than_imported_as_zero_turns():
    with pytest.raises(AdapterError, match="no text|zero turns"):
        adapters.parse(filename="empty.docx", content=docx_bytes(""))


def test_whitespace_only_paragraphs_are_refused_too():
    with pytest.raises(AdapterError, match="no text|zero turns"):
        adapters.parse(filename="blank.docx", content=docx_bytes(_para("   ") * 5))


def test_unknown_xml_elements_are_ignored_rather_than_crashing():
    body = ('<w:customThing w:val="1"/>' + _para("Human:") + _para("q")
            + '<w:somethingElse/>' + _para("Claude:") + _para("a"))
    result = adapters.parse(filename="future.docx", content=docx_bytes(body))
    assert len(result.segments) == 2


def test_a_declared_oversize_document_is_refused_before_decompression(monkeypatch):
    """The bound is on the DECLARED size, so a bomb never reaches the parser."""
    monkeypatch.setattr(docx_export, "MAX_DOCUMENT_XML_BYTES", 256)
    body = "".join(_para("Human:") + _para("x" * 400) + _para("Claude:") + _para("y"))
    with pytest.raises(AdapterError, match="WITHOUT decompressing"):
        docx_export.DocxExportAdapter().parse(
            filename="bomb.docx", content=docx_bytes(body))


def test_defusedxml_absence_is_fail_closed(monkeypatch):
    """A stdlib fallback would remove the hardening without saying so."""
    monkeypatch.setattr(docx_export, "_xml_fromstring", None)
    with pytest.raises(AdapterError, match="FAIL-CLOSED"):
        docx_export.DocxExportAdapter().parse(
            filename="chat.docx", content=labelled_docx())


def test_surrogate_bytes_in_the_document_do_not_crash_the_adapter():
    buffer = io.BytesIO()
    document = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<w:document xmlns:w="{_W}"><w:body>'
                f'{_para("Human:")}{_para("q")}{_para("Claude:")}{_para("a")}'
                f'</w:body></w:document>').encode("utf-8")
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n\xff\xfe\x00")
    result = adapters.parse(filename="bin.docx", content=buffer.getvalue())
    assert len(result.segments) == 2


# ── determinism ─────────────────────────────────────────────────────────────────────

def test_identical_bytes_produce_an_identical_result():
    content = labelled_docx()
    first = adapters.parse(filename="chat.docx", content=content)
    second = adapters.parse(filename="chat.docx", content=content)
    assert [(s.role, s.text, s.role_evidence) for s in first.segments] == \
           [(s.role, s.text, s.role_evidence) for s in second.segments]
    assert first.review_reasons == second.review_reasons
    assert first.extra == second.extra


def test_duplicate_paragraphs_are_preserved_for_the_dedupe_stage_to_judge():
    """The adapter does not deduplicate: §8 gives that to a later stage with its own report."""
    body = (_para("Human:") + _para("same text") + _para("Claude:") + _para("answer")
            + _para("Human:") + _para("same text") + _para("Claude:") + _para("answer"))
    result = adapters.parse(filename="dupes.docx", content=docx_bytes(body))
    assert [s.text for s in result.segments] == [
        "same text", "answer", "same text", "answer"]


def test_the_registry_selects_docx_before_the_heuristic_transcript_adapter():
    names = [a.name for a in adapters.registry()]
    assert names.index("docx_export") < names.index("transcript")
    assert adapters.select(filename="chat.docx", content=labelled_docx()).name == "docx_export"


def test_the_supported_report_publishes_the_docx_adapter_version():
    """§20: a stage whose version is unpublished cannot be told apart across builds."""
    report = adapters.supported()
    assert report["docx_adapter_version"] == docx_export.DOCX_ADAPTER_VERSION
    assert "docx_export" in {entry["name"] for entry in report["implemented"]}
    assert "docx" not in report["deliberately_absent"]
