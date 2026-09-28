"""reasoning_distillation/adapters/json_export.py — V69 M67A: the declared JSON contract.

WHY THIS FORMAT IS THE ONE THAT NEEDS NO SAMPLES
------------------------------------------------
Every other export format has to be reverse-engineered, which §5 forbids doing blind. JSON
is different: this module does not GUESS a provider's schema, it PUBLISHES one and refuses
anything that does not satisfy it. That inverts the risk — instead of a parser that quietly
mis-reads an unfamiliar dialect, there is a contract a converter can be written against,
and a file either meets it or is rejected with the field that was wrong.

So this is the format to convert a real corpus INTO, and the contract below is the answer
to "what exactly do you need from me" in the ``CORPUS_INPUT_REQUIRED`` report.

THE CONTRACT
------------
A single JSON object::

    {
      "schema": "m67a.conversation.1",        # REQUIRED, exact
      "conversation_id": "<stable id>",       # REQUIRED, non-empty
      "source_model": "claude-3-opus",        # OPTIONAL. Absent => UNKNOWN. NOT guessed.
      "source_date": "2024-05-01",            # OPTIONAL. Absent => UNKNOWN. NOT the mtime.
      "turns": [                              # REQUIRED, non-empty
        {"role": "user_request",     "text": "..."},
        {"role": "assistant_reasoning", "text": "..."},
        {"role": "tool_request",     "text": "..."},
        {"role": "tool_result",      "text": "..."},
        {"role": "assistant_answer", "text": "..."}
      ]
    }

``role`` must be a member of :class:`~reasoning_distillation.models.TurnRole`. An
unrecognised role is **not** an error — it becomes ``UNKNOWN`` and flags the conversation
for human review, because a converter that met 90% of the contract should not lose the
whole conversation. An unrecognised TOP-LEVEL key IS an error: a typo in a metadata key is
indistinguishable from metadata that had no effect.

WHAT THE CONTRACT DELIBERATELY DOES NOT HAVE
--------------------------------------------
No ``quality``, ``rating``, ``correct`` or ``success`` field. A converter must not be able
to assert that a historical answer was good: §26 forbids M67A claiming the traces are
ground truth, and a field that carried a success label would let that claim in through the
import path. Observed success is derived — or left UNKNOWN — by the distiller, from what
the conversation actually shows.
"""
from __future__ import annotations

import json

from ..models import ReviewStatus, SourceType, TurnRole
from .base import AdapterError, AdapterResult, RawSegment, decode

#: The exact schema string a conforming file must declare. Versioned separately from the
#: distilled schema: an input contract and an output record change for different reasons.
CONVERSATION_SCHEMA = "m67a.conversation.1"

#: Top-level keys the contract defines. Anything else is refused.
_ALLOWED_TOP = frozenset({
    "schema", "conversation_id", "source_model", "source_date", "turns", "extra"})

#: Per-turn keys. ``extra`` carries anything the export had that this contract does not
#: model, so nothing is silently dropped.
_ALLOWED_TURN = frozenset({"role", "text", "extra"})


class JsonExportAdapter:
    """Parses the declared M67A conversation contract. Strict by design."""

    name = "json_export"
    source_type = SourceType.JSON_EXPORT

    def sniff(self, *, filename: str, content: bytes) -> bool:
        """Claim the file only if it parses AND declares our schema.

        Both halves matter. Claiming every ``.json`` would swallow a provider export this
        adapter cannot read and report it as malformed, when the honest answer is that no
        adapter for that dialect exists yet (§5).
        """
        del filename
        try:
            payload = json.loads(decode(content, filename="<sniff>"))
        except (json.JSONDecodeError, AdapterError, UnicodeError):
            return False
        if not isinstance(payload, dict):
            return False
        declared = payload.get("schema")
        # Claim any version of THIS contract, not only the supported one. Claiming only the exact
        # match sent a file declaring `m67a.conversation.999` to the registry's "no adapter claims
        # format 'json'" refusal — technically true and actively unhelpful, because the file is
        # plainly ours and the real problem is the version. Claiming the family means the caller
        # gets "schema is 'm67a.conversation.999', expected 'm67a.conversation.1'" instead.
        return isinstance(declared, str) and declared.startswith("m67a.conversation.")

    def parse(self, *, filename: str, content: bytes) -> AdapterResult:
        text = decode(content, filename=filename)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{filename}: not valid JSON ({exc.msg} at line "
                               f"{exc.lineno})") from exc
        if not isinstance(payload, dict):
            raise AdapterError(
                f"{filename}: expected a JSON object, got {type(payload).__name__}")

        declared = payload.get("schema")
        if declared != CONVERSATION_SCHEMA:
            raise AdapterError(
                f"{filename}: schema is {declared!r}, expected {CONVERSATION_SCHEMA!r}. An "
                f"undeclared or mismatched schema is refused rather than read hopefully")
        unknown = sorted(set(payload) - _ALLOWED_TOP)
        if unknown:
            raise AdapterError(
                f"{filename}: unknown top-level key(s) {unknown}; the contract is closed so "
                f"that a mistyped metadata key is a failure rather than a silent no-op")

        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise AdapterError(
                f"{filename}: conversation_id must be a non-empty string; without one the "
                f"turns cannot be family-grouped and an ungrouped turn can cross a split")

        raw_turns = payload.get("turns")
        if not isinstance(raw_turns, list) or not raw_turns:
            raise AdapterError(f"{filename}: 'turns' must be a non-empty list")

        reasons: list[str] = []
        segments: list[RawSegment] = []
        valid_roles = {r.value for r in TurnRole}
        for index, entry in enumerate(raw_turns):
            if not isinstance(entry, dict):
                raise AdapterError(
                    f"{filename}: turns[{index}] must be an object, got "
                    f"{type(entry).__name__}")
            extra_keys = sorted(set(entry) - _ALLOWED_TURN)
            if extra_keys:
                raise AdapterError(
                    f"{filename}: turns[{index}] has unknown key(s) {extra_keys}")
            body = entry.get("text")
            if not isinstance(body, str):
                raise AdapterError(
                    f"{filename}: turns[{index}].text must be a string, got "
                    f"{type(body).__name__}")
            role_name = entry.get("role")
            if isinstance(role_name, str) and role_name in valid_roles:
                role = TurnRole(role_name)
                evidence = f"declared:{role_name}"
            else:
                role = TurnRole.UNKNOWN
                evidence = ""
                # The role NAME is echoed because it is the converter's own label, not
                # conversation content. Bounded, so a body pasted into `role` cannot ride
                # out through the review reason.
                shown = role_name if isinstance(role_name, str) else type(role_name).__name__
                reasons.append(
                    f"turns[{index}] role {shown[:32]!r} is not a known TurnRole; recorded "
                    f"as UNKNOWN rather than guessed")
            segments.append(RawSegment(role=role, text=body, role_evidence=evidence))

        model = payload.get("source_model")
        date = payload.get("source_date")
        # Absent means UNKNOWN. A non-string present value is a contract violation, not a
        # reason to coerce: `source_model: 0` would stringify to "0" and become a model name.
        for label, value in (("source_model", model), ("source_date", date)):
            if value is not None and not isinstance(value, str):
                raise AdapterError(
                    f"{filename}: {label} must be a string when present, got "
                    f"{type(value).__name__}")

        return AdapterResult(
            source_type=self.source_type,
            conversation_id=conversation_id.strip(),
            segments=tuple(segments),
            source_model=(model or "").strip(),
            source_date=(date or "").strip(),
            review_status=ReviewStatus.NEEDS_HUMAN_REVIEW if reasons else ReviewStatus.OK,
            review_reasons=tuple(reasons),
            extra=dict(payload.get("extra") or {}),
        )


def contract_description() -> dict:
    """The machine-readable input contract, for the CORPUS_INPUT_REQUIRED report (§5)."""
    return {
        "schema": CONVERSATION_SCHEMA,
        "required_top_level": ["schema", "conversation_id", "turns"],
        "optional_top_level": ["source_model", "source_date", "extra"],
        "turn_keys": sorted(_ALLOWED_TURN),
        "roles": [r.value for r in TurnRole],
        "notes": [
            "absent source_model/source_date mean UNKNOWN and are never inferred",
            "an unknown role becomes UNKNOWN and flags the conversation for human review",
            "an unknown top-level key is refused, so a mistyped key cannot be a silent no-op",
            "there is no quality/success field: a converter may not assert that a "
            "historical answer was correct",
        ],
    }


__all__ = ["CONVERSATION_SCHEMA", "JsonExportAdapter", "contract_description"]
