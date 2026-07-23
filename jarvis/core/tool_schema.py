"""core/tool_schema.py — V69 M58.7.1: deterministic tool-schema fingerprinting.

WHY A TOOL SCHEMA CAN DESTROY PREFIX REUSE
------------------------------------------
When a turn sends a ``tools`` array, that array is part of the prompt the server
prefills. If its ordering is non-deterministic (dict iteration, an MCP registry that
appends on connect, a set), two otherwise-identical tool-enabled turns produce
different prefixes and the server cannot reuse either. Worse, a volatile field
(a timestamp, a per-process id) in a schema silently changes the bytes every run.

This module makes the tool schema DETERMINISTIC and gives it a content-free
fingerprint, so prefix-cache identity (M58.5) can invalidate exactly when the schema
actually changes and never merely because a dict re-ordered.

GUARANTEES
----------
  * canonical tool ordering (by function name) and canonical field ordering
    (recursive key sort) — nonfunctional ordering is removed;
  * a schema version so an intentional change is a clean invalidation signal;
  * no timestamps / volatile data enter the fingerprint (only structural content);
  * DIRECT_FAST sends NO tools — an empty schema has a stable, distinct fingerprint;
  * a bounded token/char estimate BEFORE and AFTER TurnPolicy filtering, so the cost
    of the per-turn eligible-tool subset is measurable.

Pure and I/O-free. Never emits secret defaults or raw arguments — it fingerprints
the SCHEMA (names, descriptions, parameter shapes), which is first-party static text.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

TOOL_SCHEMA_VERSION = "m58.1"


def _canonicalize(obj):
    """Recursively sort dict keys and normalise so serialization is deterministic.

    Lists preserve order (a tool's ``required`` array and ``enum`` are semantic), but
    every mapping is key-sorted. This removes dict-insertion-order noise without
    changing meaning.
    """
    if isinstance(obj, dict):
        return {k: _canonicalize(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v) for v in obj]
    return obj


def _tool_name(tool: dict) -> str:
    try:
        return str(tool.get("function", {}).get("name") or "")
    except Exception:  # noqa: BLE001
        return ""


def canonical_tools(tools: list[dict]) -> list[dict]:
    """Return the tools sorted by function name with every mapping key-sorted.

    Deterministic and stable: the SAME set of tools always yields the same list,
    regardless of the order they were registered or an MCP bridge connected.
    """
    cleaned = [t for t in (tools or []) if isinstance(t, dict) and _tool_name(t)]
    cleaned.sort(key=_tool_name)
    return [_canonicalize(t) for t in cleaned]


def _serialize(tools: list[dict]) -> str:
    return json.dumps(canonical_tools(tools), ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))


def tool_schema_fingerprint(tools: list[dict] | None) -> str:
    """A content-free 16-hex fingerprint of the canonical tool schema.

    An empty/None schema (the DIRECT_FAST case) fingerprints the empty canonical
    form — a stable, distinct value, never confused with a populated schema.
    """
    payload = f"{TOOL_SCHEMA_VERSION}\x1f{_serialize(tools or [])}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def estimate_schema_tokens(tools: list[dict] | None) -> int:
    """Bounded ~4-chars/token estimate of the serialized schema."""
    return max(0, len(_serialize(tools or [])) // 4)


@dataclass(frozen=True)
class ToolSchemaFingerprint:
    """The bounded, content-free tool-schema view for one turn / one registry."""

    fingerprint: str
    schema_version: str
    tool_count: int
    estimated_tokens: int
    eligible_tool_count: int = 0
    eligible_estimated_tokens: int = 0

    def snapshot(self) -> dict:
        return {
            "tool_schema_fingerprint": self.fingerprint,
            "tool_schema_version": self.schema_version,
            "tool_count": self.tool_count,
            "schema_estimated_tokens": self.estimated_tokens,
            "eligible_tool_count": self.eligible_tool_count,
            "eligible_schema_estimated_tokens": self.eligible_estimated_tokens,
        }


def build_tool_schema_fingerprint(
    tools: list[dict] | None,
    *,
    eligible_tools: list[dict] | None = None,
) -> ToolSchemaFingerprint:
    """Fingerprint the full registry and, when given, the per-turn eligible subset.

    ``eligible_tools`` is the TurnPolicy-filtered subset actually sent to the model.
    Measuring both makes the M58.7.1 "measure schema token size before/after
    filtering" requirement a real number, and the fingerprint is taken over the
    ELIGIBLE subset (what the model sees) so prefix identity tracks the real prompt.
    """
    full = list(tools or [])
    eligible = eligible_tools if eligible_tools is not None else full
    return ToolSchemaFingerprint(
        fingerprint=tool_schema_fingerprint(eligible),
        schema_version=TOOL_SCHEMA_VERSION,
        tool_count=len([t for t in full if isinstance(t, dict) and _tool_name(t)]),
        estimated_tokens=estimate_schema_tokens(full),
        eligible_tool_count=len(
            [t for t in eligible if isinstance(t, dict) and _tool_name(t)]),
        eligible_estimated_tokens=estimate_schema_tokens(eligible),
    )


# The fingerprint of the tool-free FAST prompt: DIRECT_FAST sends no tools, so its
# schema identity is the empty-schema fingerprint. Exposed so callers assert it.
EMPTY_TOOL_SCHEMA_FINGERPRINT = tool_schema_fingerprint([])
