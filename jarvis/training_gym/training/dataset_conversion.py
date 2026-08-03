"""training_gym/training/dataset_conversion.py — V69 M62 S3B: rows a trainer may read.

WHY CONVERSION IS A SEPARATE, TOKENIZER-FREE STEP
-------------------------------------------------
Everything in this module is pure: it reads the verified SFT export, checks each row
against the shape the trainer expects, and returns typed records. It imports no
tokenizer, no model and no framework, so the decision "is this corpus trainable" is made
and testable before anything heavy is loaded — and a malformed corpus is refused while
refusing is still cheap.

WHY A MALFORMED ROW IS A REFUSAL AND NOT A SKIP
-----------------------------------------------
Silently dropping rows is how a training set becomes something nobody described. If nine
of ten rows parse, the run is not "mostly correct" — it is a run against a corpus whose
record count no longer matches the manifest that was approved, and whose export digest
therefore describes a file the trainer did not read. Every row must convert, or none do.

WHY ASSISTANT-ONLY LOSS IS PLANNED HERE
---------------------------------------
The supervised target is the assistant turn. If the prompt tokens contribute to the loss,
the model is being fitted on the questions as well as the answers — which is a different
experiment from the one the plan describes, and it is invisible in the metrics. The
masking decision is therefore part of conversion: each record carries the exact message
list for the prompt and the exact assistant completion, so a backend can build
``labels`` with the prompt span set to the framework's ignore index and prove it did.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..schemas import SchemaError, canonical_json, sha256_obj, sha256_text, short
from ..teachers.sanitization import hidden_key
from .plan import TrainingPlanError

#: Bumped when the converted record's shape changes.
CONVERSION_VERSION = "m62.dataset_conversion.1"

#: The framework-level convention for "this token contributes nothing to the loss".
#: It is ``torch.nn.CrossEntropyLoss(ignore_index=-100)`` and it has not changed; using
#: the constant rather than a library import is what keeps masking version-stable.
LOSS_IGNORE_INDEX = -100

#: The only roles a converted row may contain, in the only order they may appear in.
SYSTEM_ROLE, USER_ROLE, ASSISTANT_ROLE = "system", "user", "assistant"
ALLOWED_ROLES: frozenset[str] = frozenset({SYSTEM_ROLE, USER_ROLE, ASSISTANT_ROLE})

#: A corpus is a reviewable artefact, not an archive.
MAX_CONVERTED_RECORDS = 100_000
MAX_MESSAGE_CHARS = 40_000

#: Key names that must never reach a trainer. A row carrying one is a row whose export
#: sanitizer would have blanked the value, which means the value was never reviewed.
_FORBIDDEN_ROW_KEYS = ("reasoning", "thinking", "scratchpad", "chain_of_thought",
                       "hidden", "internal_monologue")


class DatasetConversionError(TrainingPlanError):
    """A corpus that will not be trained on as written."""


@dataclass(frozen=True)
class ConvertedRecord:
    """One training example, split at the boundary the loss mask must fall on."""

    row_index: int
    candidate_id: str
    prompt_messages: tuple[dict, ...]
    completion: str

    @property
    def messages(self) -> tuple[dict, ...]:
        """The full conversation, prompt turns followed by the assistant target."""
        return (*self.prompt_messages,
                {"role": ASSISTANT_ROLE, "content": self.completion})

    def row_identity(self) -> str:
        """A deterministic digest of this record, independent of file order."""
        return sha256_obj({"candidate_id": self.candidate_id,
                           "messages": [dict(sorted(m.items())) for m in self.messages]})


@dataclass(frozen=True)
class ConvertedDataset:
    """A corpus a backend may read, and the evidence of what it is."""

    records: tuple[ConvertedRecord, ...]
    export_sha256: str
    max_sequence_length: int
    conversion_version: str = CONVERSION_VERSION

    def __len__(self) -> int:
        return len(self.records)

    def content_hash(self) -> str:
        """Order-sensitive on purpose: the order is what the seed shuffles."""
        return sha256_obj({
            "conversion_version": self.conversion_version,
            "export_sha256": self.export_sha256,
            "max_sequence_length": self.max_sequence_length,
            "rows": [r.row_identity() for r in self.records]})

    def to_dict(self) -> dict:
        return {"conversion_version": self.conversion_version,
                "record_count": len(self.records),
                "export_sha256": self.export_sha256,
                "max_sequence_length": self.max_sequence_length,
                "content_hash": self.content_hash()}


def _check_messages(payload: object, *, index: int) -> tuple[tuple[dict, ...], str]:
    """Return ``(prompt turns, assistant completion)`` or raise.

    The accepted shapes are exactly ``[user, assistant]`` and
    ``[system, user, assistant]``. Anything else — a second user turn, a trailing system
    message, a role the export never emits — is refused rather than coerced, because
    coercion is how a conversation the reviewer approved becomes a different one.
    """
    if not isinstance(payload, list) or not payload:
        raise DatasetConversionError(f"row {index}: 'messages' must be a non-empty list")
    turns: list[dict] = []
    for position, raw in enumerate(payload):
        if not isinstance(raw, dict):
            raise DatasetConversionError(f"row {index}: message {position} is not an "
                                         f"object")
        unknown = sorted(set(raw) - {"role", "content"})
        if unknown:
            raise DatasetConversionError(
                f"row {index}: message {position} carries unexpected field(s) {unknown}")
        role = raw.get("role")
        content = raw.get("content")
        if role not in ALLOWED_ROLES:
            raise DatasetConversionError(
                f"row {index}: message {position} has role {role!r}; the supported roles "
                f"are {sorted(ALLOWED_ROLES)}")
        if not isinstance(content, str):
            raise DatasetConversionError(
                f"row {index}: message {position} content is "
                f"{type(content).__name__}, not text")
        if len(content) > MAX_MESSAGE_CHARS:
            raise DatasetConversionError(
                f"row {index}: message {position} is {len(content)} characters, above "
                f"the {MAX_MESSAGE_CHARS} ceiling")
        turns.append({"role": role, "content": content})

    roles = [t["role"] for t in turns]
    if roles not in ([USER_ROLE, ASSISTANT_ROLE],
                     [SYSTEM_ROLE, USER_ROLE, ASSISTANT_ROLE]):
        raise DatasetConversionError(
            f"row {index}: role order is {roles}; a supervised row is "
            f"[user, assistant] or [system, user, assistant] and nothing else")
    completion = turns[-1]["content"]
    if not completion.strip():
        raise DatasetConversionError(
            f"row {index}: the assistant target is empty; training on an empty "
            f"completion optimises the model towards saying nothing, and the loss would "
            f"not say so")
    if not turns[-2]["content"].strip():
        raise DatasetConversionError(f"row {index}: the user prompt is empty")
    return tuple(turns[:-1]), completion


def _check_no_hidden_material(payload: dict, *, index: int) -> None:
    """Refuse a row whose KEY NAMES the export sanitizer would have blanked."""
    stack: list[object] = [payload]
    depth = 0
    while stack and depth < 64:
        depth += 1
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                lowered = str(key).lower()
                if any(marker in lowered for marker in _FORBIDDEN_ROW_KEYS):
                    raise DatasetConversionError(
                        f"row {index}: carries a {key!r} field; hidden reasoning is not "
                        f"reviewed material and is never a training target")
                if hidden_key(str(key)):
                    raise DatasetConversionError(
                        f"row {index}: carries a {key!r} field, which the export "
                        f"sanitizer blanks; a value nobody could review is a value "
                        f"nobody approved")
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def convert_sft_export(export_file: str | Path, *, max_sequence_length: int,
                       expected_record_count: int = 0,
                       expected_sha256: str = "",
                       revoked_candidate_ids: frozenset[str] = frozenset(),
                       held_out_candidate_ids: frozenset[str] = frozenset(),
                       ) -> ConvertedDataset:
    """Read the verified SFT export and produce the records a backend may fit on.

    The digest is re-derived here even though the reference already checked it. The two
    checks are separated by model loading and tokenization, and a file that was correct
    when the plan was built is not necessarily the file the trainer opens.
    """
    from ..schemas import sha256_file

    path = Path(export_file)
    if path.is_symlink() or not path.is_file():
        raise DatasetConversionError(
            "the training corpus is missing or is a link; a run reads the exact bytes "
            "the reference named, not whatever a link resolves to today")
    actual = sha256_file(path)
    if expected_sha256 and actual != expected_sha256:
        raise DatasetConversionError(
            f"the training corpus hashes to {short(actual)}, the verified export was "
            f"{short(expected_sha256)}; it changed after the plan was approved")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if not lines:
        raise DatasetConversionError(
            "the training corpus holds no rows; a run over zero examples produces the "
            "adapter it started with and reports that it trained")
    if len(lines) > MAX_CONVERTED_RECORDS:
        raise DatasetConversionError(
            f"the training corpus holds {len(lines)} rows, above the "
            f"{MAX_CONVERTED_RECORDS} ceiling")
    if expected_record_count and len(lines) != expected_record_count:
        raise DatasetConversionError(
            f"the training corpus holds {len(lines)} rows, the verified export manifest "
            f"recorded {expected_record_count}")

    records: list[ConvertedRecord] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DatasetConversionError(f"row {index}: is not valid JSON ({exc})") from None
        if not isinstance(payload, dict):
            raise DatasetConversionError(f"row {index}: is not an object")
        _check_no_hidden_material(payload, index=index)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise DatasetConversionError(f"row {index}: carries no metadata object")
        candidate_id = str(metadata.get("candidate_id", ""))
        if not candidate_id:
            raise DatasetConversionError(
                f"row {index}: names no candidate; a record with no provenance cannot be "
                f"checked against a revocation")
        if candidate_id in revoked_candidate_ids:
            raise DatasetConversionError(
                f"row {index}: candidate {candidate_id} has been revoked; a revocation "
                f"that does not remove the record from training is a revocation in name "
                f"only")
        if candidate_id in held_out_candidate_ids:
            raise DatasetConversionError(
                f"row {index}: candidate {candidate_id} is held-out material; a set the "
                f"model was fitted on is not held out")
        if candidate_id in seen:
            raise DatasetConversionError(
                f"row {index}: candidate {candidate_id} appears twice")
        seen.add(candidate_id)
        prompt_messages, completion = _check_messages(payload.get("messages"),
                                                      index=index)
        records.append(ConvertedRecord(row_index=index, candidate_id=candidate_id,
                                       prompt_messages=prompt_messages,
                                       completion=completion))

    if max_sequence_length <= 0:
        raise DatasetConversionError("max_sequence_length must be positive")
    return ConvertedDataset(records=tuple(records), export_sha256=actual,
                            max_sequence_length=max_sequence_length)


# ══════════════════════════════════════════════════════════════════════════════
#  Assistant-only loss
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class MaskingEvidence:
    """Proof that the prompt span was excluded from the supervised loss.

    Recorded in the adapter manifest. ``verified`` is only ever set by a self-test that
    actually tokenized a record and counted the masked positions — a package version is
    not evidence that masking worked, and this milestone does not accept one as such.
    """

    strategy: str
    ignore_index: int = LOSS_IGNORE_INDEX
    verified: bool = False
    probe_prompt_tokens: int = 0
    probe_completion_tokens: int = 0
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "ignore_index": self.ignore_index,
                "verified": self.verified,
                "probe_prompt_tokens": self.probe_prompt_tokens,
                "probe_completion_tokens": self.probe_completion_tokens,
                "problems": list(self.problems)}


def build_labels(prompt_ids: list[int], full_ids: list[int]) -> list[int]:
    """Supervise the completion and nothing else.

    Refuses when the full sequence does not START with the prompt sequence. A chat
    template that does not tokenize as a prefix of itself in context would silently shift
    the boundary, and a mask off by even one token supervises a prompt token while
    reporting that it did not.
    """
    if not prompt_ids:
        raise DatasetConversionError(
            "assistant-only loss: the prompt tokenized to nothing; there would be no "
            "boundary to mask at")
    if len(full_ids) <= len(prompt_ids):
        raise DatasetConversionError(
            "assistant-only loss: the full sequence is no longer than the prompt; there "
            "is no completion to supervise")
    if full_ids[:len(prompt_ids)] != prompt_ids:
        raise DatasetConversionError(
            "assistant-only loss: the prompt does not tokenize as a prefix of the full "
            "conversation, so the mask boundary cannot be located. Refusing rather than "
            "guessing: an off-by-one mask trains on the question and says it did not")
    return [LOSS_IGNORE_INDEX] * len(prompt_ids) + list(full_ids[len(prompt_ids):])


def check_masking(labels: list[int], prompt_length: int) -> tuple[str, ...]:
    """Every reason this label vector does not implement assistant-only loss."""
    problems: list[str] = []
    if len(labels) <= prompt_length:
        problems.append("the label vector is not longer than the prompt")
        return tuple(problems)
    if any(token != LOSS_IGNORE_INDEX for token in labels[:prompt_length]):
        problems.append(
            f"{sum(1 for t in labels[:prompt_length] if t != LOSS_IGNORE_INDEX)} prompt "
            f"token(s) are not masked; the system and user turns would contribute to the "
            f"supervised loss")
    supervised = [t for t in labels[prompt_length:] if t != LOSS_IGNORE_INDEX]
    if not supervised:
        problems.append(
            "no completion token is supervised; the loss would be computed over nothing")
    return tuple(problems)


def chat_template_hash(template: object) -> str:
    """A digest of the tokenizer's chat template, or the empty string when absent.

    Recorded in the adapter manifest because the template is what turns a message list
    into the exact string the model saw. Two runs with the same corpus and different
    templates trained on different text.
    """
    if not isinstance(template, str) or not template.strip():
        return ""
    return sha256_text(template)


def rendered_preview(record: ConvertedRecord) -> str:
    """The canonical JSON of one record, for a self-test that needs deterministic text.

    Deliberately not the rendered chat string: producing that requires a tokenizer, and
    this module never loads one.
    """
    return canonical_json({"messages": [dict(sorted(m.items()))
                                        for m in record.messages]})


__all__ = [
    "ALLOWED_ROLES", "ASSISTANT_ROLE", "CONVERSION_VERSION", "LOSS_IGNORE_INDEX",
    "MAX_CONVERTED_RECORDS", "MAX_MESSAGE_CHARS", "SYSTEM_ROLE", "USER_ROLE",
    "ConvertedDataset", "ConvertedRecord", "DatasetConversionError", "MaskingEvidence",
    "SchemaError", "build_labels", "chat_template_hash", "check_masking",
    "convert_sft_export", "rendered_preview",
]
