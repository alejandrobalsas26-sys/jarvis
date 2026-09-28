"""reasoning_distillation/adapters — V69 M67A: the ingestion registry.

The registry is an ALLOWLIST. A file is ingested only when an adapter positively claims it
via :meth:`~reasoning_distillation.adapters.base.SourceAdapter.sniff`; there is no fallback
adapter that accepts whatever is left. §29 asks for positive contracts and allowlists, and
the alternative here is concrete rather than theoretical: a permissive fallback would import
a PDF of a chat as one enormous UNKNOWN turn and report the corpus as larger than it is.

Selection is by CONTENT, with the extension used only to produce a better refusal. A file
saved as ``notes.txt`` that actually holds the JSON contract is ingested correctly; a
``.json`` file holding some other provider's dialect is refused with the reason, not parsed
hopefully.
"""
from __future__ import annotations

from ..models import SourceType
from .base import (
    ADAPTER_VERSION,
    AdapterError,
    AdapterNotImplemented,
    AdapterResult,
    RawSegment,
    SourceAdapter,
    decode,
    refuse_absent_format,
)
from .json_export import CONVERSATION_SCHEMA, JsonExportAdapter, contract_description
from .transcript import TranscriptAdapter, known_labels


def registry() -> "tuple[SourceAdapter, ...]":
    """Every implemented adapter, in claim-priority order.

    JSON first: its ``sniff`` is exact (it requires the declared schema string), so putting
    it first costs nothing and means a conforming file is never examined by the heuristic
    transcript parser.
    """
    return (JsonExportAdapter(), TranscriptAdapter())


def select(*, filename: str, content: bytes) -> "SourceAdapter":
    """The one adapter that claims this file, or a refusal naming why none does."""
    for adapter in registry():
        if adapter.sniff(filename=filename, content=content):
            return adapter
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    raise refuse_absent_format(extension)


def parse(*, filename: str, content: bytes) -> AdapterResult:
    """Select and run. The single entrypoint the ingest stage uses."""
    return select(filename=filename, content=content).parse(
        filename=filename, content=content)


def supported() -> dict:
    """What this build can and cannot ingest (§5, §20). Recorded in every manifest."""
    from .base import DELIBERATELY_ABSENT
    return {
        "adapter_version": ADAPTER_VERSION,
        "implemented": [
            {"name": a.name, "source_type": a.source_type.value} for a in registry()],
        "deliberately_absent": dict(sorted(DELIBERATELY_ABSENT.items())),
        "json_contract": contract_description(),
        "transcript_labels": known_labels(),
    }


__all__ = [
    "ADAPTER_VERSION", "CONVERSATION_SCHEMA", "AdapterError", "AdapterNotImplemented",
    "AdapterResult", "JsonExportAdapter", "RawSegment", "SourceAdapter", "SourceType",
    "TranscriptAdapter", "contract_description", "decode", "known_labels", "parse",
    "refuse_absent_format", "registry", "select", "supported",
]
