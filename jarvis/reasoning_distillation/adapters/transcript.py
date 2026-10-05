"""reasoning_distillation/adapters/transcript.py — V69 M67A: role-prefixed transcripts.

THE FORMAT THIS HANDLES
-----------------------
The way a human actually saves a chat: a text or Markdown file where each turn is
introduced by a label. ``User:``, ``Assistant:``, ``## Human``, ``**Claude**``,
``<thinking>``, ``Tool result:``. It is the most common hand-saved shape and the only
plain-text one whose role boundaries are recoverable without a provider-specific schema.

WHY IT PARSES CONSERVATIVELY, AND WHAT THAT COSTS
-------------------------------------------------
The marker table is CLOSED. A line that looks like a label but is not in the table does not
start a new turn — it stays inside the current one, and the conversation is flagged
``NEEDS_HUMAN_REVIEW`` with the line number. That is deliberately the expensive direction:

  * **If the parser split too eagerly**, a line like ``Note: the following is wrong`` inside
    an answer would become a new turn with role UNKNOWN. The answer would be truncated, and
    the truncated half would be distilled as though it were the whole decision.
  * **If the parser splits too reluctantly**, two turns merge into one. That is visible —
    the merged turn holds both a question and an answer — and it is caught downstream,
    because a segment whose role is ``ASSISTANT_ANSWER`` while containing the user's
    request fails the distiller's source-verification step.

A visible merge beats an invisible truncation, so the table stays closed and the unrecognised
labels get reported.

THE ``<thinking>`` CASE IS THE IMPORTANT ONE
--------------------------------------------
Older Claude transcripts carry visible rationale in ``<thinking>`` / ``<reasoning>`` /
``<scratchpad>`` tags. Those are the highest-value segments in the whole corpus — they are
the actual reasoning §1 wants to distil — and they are ALSO what
:func:`core.redaction_policy.strip_hidden_reasoning` exists to remove from production output.

Both are correct in their own context, and the distinction is worth stating: the runtime
strips hidden reasoning because it must never *emit* it; M67A *ingests* it because it is
studying it. That is why this adapter reads those tags as
:attr:`~reasoning_distillation.models.TurnRole.ASSISTANT_REASONING` rather than calling the
redaction helper — and why nothing downstream of here may render such a segment: the corpus
root is local (§4) and every record is body-free.
"""
from __future__ import annotations

import re

from ..models import ReviewStatus, SourceType, TurnRole
from .base import AdapterError, AdapterResult, RawSegment, decode

#: Inline reasoning tags, read as ASSISTANT_REASONING. Mirrors
#: ``core.redaction_policy._REASONING_TAGS`` deliberately: the same tag names, read here
#: and stripped there, for the reasons in the module docstring.
_REASONING_TAGS = ("thinking", "think", "reasoning", "scratchpad", "analysis", "rationale")

_TAG_BLOCK = re.compile(
    r"<(" + "|".join(_REASONING_TAGS) + r")\b[^>]*>(.*?)</\1\s*>",
    re.IGNORECASE | re.DOTALL)

#: The CLOSED label table. Each entry maps a normalized label to a role. Keys are compared
#: casefolded with surrounding punctuation and Markdown emphasis stripped, so ``## Human``,
#: ``**human**`` and ``HUMAN:`` all reach the same key.
_LABELS: dict[str, TurnRole] = {
    # user side
    "user": TurnRole.USER_REQUEST,
    "human": TurnRole.USER_REQUEST,
    "me": TurnRole.USER_REQUEST,
    "operator": TurnRole.USER_REQUEST,
    "usuario": TurnRole.USER_REQUEST,
    "prompt": TurnRole.USER_REQUEST,
    # assistant answer
    "assistant": TurnRole.ASSISTANT_ANSWER,
    "claude": TurnRole.ASSISTANT_ANSWER,
    "ai": TurnRole.ASSISTANT_ANSWER,
    "model": TurnRole.ASSISTANT_ANSWER,
    "answer": TurnRole.ASSISTANT_ANSWER,
    "response": TurnRole.ASSISTANT_ANSWER,
    "respuesta": TurnRole.ASSISTANT_ANSWER,
    "final answer": TurnRole.ASSISTANT_ANSWER,
    # assistant process
    "thinking": TurnRole.ASSISTANT_REASONING,
    "reasoning": TurnRole.ASSISTANT_REASONING,
    "rationale": TurnRole.ASSISTANT_REASONING,
    "scratchpad": TurnRole.ASSISTANT_REASONING,
    "analysis": TurnRole.ASSISTANT_REASONING,
    "razonamiento": TurnRole.ASSISTANT_REASONING,
    # corrections
    "correction": TurnRole.ASSISTANT_REVISION,
    "revision": TurnRole.ASSISTANT_REVISION,
    "edit": TurnRole.ASSISTANT_REVISION,
    "corrección": TurnRole.ASSISTANT_REVISION,
    # tools
    "tool": TurnRole.TOOL_REQUEST,
    "tool call": TurnRole.TOOL_REQUEST,
    "tool use": TurnRole.TOOL_REQUEST,
    "function call": TurnRole.TOOL_REQUEST,
    "tool result": TurnRole.TOOL_RESULT,
    "tool output": TurnRole.TOOL_RESULT,
    "function result": TurnRole.TOOL_RESULT,
    "observation": TurnRole.TOOL_RESULT,
    # framing
    "system": TurnRole.SYSTEM_CONTEXT,
    "context": TurnRole.SYSTEM_CONTEXT,
    "developer": TurnRole.SYSTEM_CONTEXT,
    "instructions": TurnRole.SYSTEM_CONTEXT,
    "follow-up": TurnRole.FOLLOW_UP,
    "follow up": TurnRole.FOLLOW_UP,
}

#: A line that MIGHT be a label. Used only to decide whether to REPORT an unrecognised
#: label, never to create a turn.
#:
#: The decoration is REQUIRED, not optional: the line must either end in a colon or be a
#: Markdown heading (``## X``) or a fully-emphasised run (``**X**``). An earlier version made
#: the decoration optional, which matched any short prose line — measured on a four-turn
#: fixture it reported ``'some trailing prose'`` as a candidate label. Those false positives
#: cost nothing structurally (nothing is ever split on them) but they fill the review queue
#: with noise, and a review reason a human learns to skip is a control that has stopped
#: working.
_LABEL_SHAPED = re.compile(
    r"^\s{0,3}(?:"
    r"#{1,4}\s*([A-Za-zÁÉÍÓÚÑáéíóúñ][\w .'\-/]{0,28}?)\s*\**\s*:?"       # ## Heading
    r"|\*{2}([A-Za-zÁÉÍÓÚÑáéíóúñ][\w .'\-/]{0,28}?)\*{2}\s*:?"            # **Bold**
    r"|_{2}([A-Za-zÁÉÍÓÚÑáéíóúñ][\w .'\-/]{0,28}?)_{2}\s*:?"               # __Bold__
    r"|([A-Za-zÁÉÍÓÚÑáéíóúñ][\w .'\-/]{0,28}?)\s*:"                        # Label:
    r")\s*$")


def _shaped_label(line: str) -> str:
    """The candidate label text from a label-shaped line, or ``""``.

    One helper because :data:`_LABEL_SHAPED` has four alternatives and only one group ever
    matches; picking the non-empty group in two places would be two places to get it wrong.
    """
    match = _LABEL_SHAPED.match(line)
    if not match:
        return ""
    return next((g for g in match.groups() if g), "")

#: The longest a label may be once normalized. A 60-character "label" is a sentence.
_MAX_LABEL_CHARS = 30


def _normalize_label(raw: str) -> str:
    """Reduce a candidate label to its comparison key. Pure and total."""
    text = raw.strip()
    text = text.lstrip("#").strip()
    text = text.strip("*_").strip()
    text = text.rstrip(":").strip()
    text = text.strip("[]()<>").strip()
    return " ".join(text.split()).casefold()


class TranscriptAdapter:
    """Role-prefixed plain-text and Markdown transcripts.

    One class serves both ``.txt`` and ``.md``: the label grammar is identical and the only
    difference is that Markdown wraps labels in ``#`` or ``**``, which
    :func:`_normalize_label` removes. Two near-identical classes would be two places for the
    table to drift.
    """

    name = "transcript"
    source_type = SourceType.TXT

    def sniff(self, *, filename: str, content: bytes) -> bool:
        """Claim the file when at least two DIFFERENT known labels appear.

        Two different labels, not two occurrences of one: a document that says ``Note:``
        forty times is not a transcript, and a real conversation always has at least a user
        side and an assistant side.
        """
        del filename
        try:
            text = decode(content, filename="<sniff>")
        except (AdapterError, UnicodeError):
            return False
        if _TAG_BLOCK.search(text):
            return True
        seen: set[TurnRole] = set()
        for line in text.splitlines():
            inline = line.split(":", 1)[0] if ":" in line else line
            if len(inline) > _MAX_LABEL_CHARS + 8:
                continue
            role = _LABELS.get(_normalize_label(inline))
            if role is not None:
                seen.add(role)
            if len(seen) >= 2:
                return True
        return False

    def parse(self, *, filename: str, content: bytes) -> AdapterResult:
        text = decode(content, filename=filename)
        if not text.strip():
            raise AdapterError(
                f"{filename}: the file is empty or whitespace only; an empty conversation "
                f"is refused rather than imported as a conversation with zero turns")
        source_type = (SourceType.MARKDOWN if filename.lower().endswith((".md", ".markdown"))
                       else SourceType.TXT)

        reasons: list[str] = []
        segments: list[RawSegment] = []
        # Tag blocks are extracted FIRST and replaced by a placeholder, so a <thinking>
        # block that spans several lines cannot be chopped up by the line-oriented label
        # scan below. The placeholder keeps the block's position in the transcript, which is
        # what makes the turn ORDER faithful — and order is the only evidence that a
        # correction came after the thing it corrected.
        held: list[RawSegment] = []

        def _stash(match: "re.Match[str]") -> str:
            held.append(RawSegment(role=TurnRole.ASSISTANT_REASONING,
                                   text=match.group(2).strip(),
                                   role_evidence=f"tag:{match.group(1).lower()}"))
            return f"\n\x00HELD{len(held) - 1}\x00\n"

        text = _TAG_BLOCK.sub(_stash, text)

        current_role: TurnRole | None = None
        current_evidence = ""
        buffer: list[str] = []

        def flush() -> None:
            if current_role is None:
                return
            body = "\n".join(buffer).strip()
            if body:
                segments.append(RawSegment(role=current_role, text=body,
                                           role_evidence=current_evidence))

        for lineno, line in enumerate(text.splitlines(), start=1):
            placeholder = line.strip()
            if placeholder.startswith("\x00HELD") and placeholder.endswith("\x00"):
                flush()
                buffer = []
                index = int(placeholder[5:-1])
                segments.append(held[index])
                current_role = None
                current_evidence = ""
                continue

            label_role, evidence, remainder = self._match_label(line)
            if label_role is not None:
                flush()
                buffer = [remainder] if remainder else []
                current_role = label_role
                current_evidence = evidence
                continue

            shaped_text = _shaped_label(line)
            if shaped_text and current_role is not None:
                candidate = _normalize_label(shaped_text)
                if candidate and candidate not in _LABELS and len(candidate) <= _MAX_LABEL_CHARS:
                    # Reported, NOT split on. See the module docstring: an invisible
                    # truncation is worse than a visible merge. The candidate label is the
                    # operator's own heading text, bounded hard so a body cannot ride out.
                    reasons.append(
                        f"line {lineno}: {candidate[:30]!r} is label-shaped but is not a "
                        f"known role marker; kept inside the current turn, not split")
            buffer.append(line)

        flush()

        if not segments:
            raise AdapterError(
                f"{filename}: no role-labelled turn was found. The label table is closed "
                f"(§5), so this is reported as 'no adapter understands this file' rather "
                f"than imported as one unlabelled blob")

        leading_unlabelled = text.split("\x00")[0].strip()
        if leading_unlabelled and segments and segments[0].role_evidence.startswith("label:"):
            first_label_pos = text.find(segments[0].role_evidence.split(":", 1)[1])
            if first_label_pos > 0 and text[:first_label_pos].strip():
                reasons.append(
                    "text precedes the first role label; it is not imported as a turn "
                    "because its role is unknown, and it is reported rather than dropped "
                    "silently")

        # source_model and source_date are NOT derivable from a transcript. §7: missing is
        # UNKNOWN. The filename is not evidence and neither is the mtime.
        return AdapterResult(
            source_type=source_type,
            conversation_id="",  # assigned by the ingest stage from the file digest
            segments=tuple(segments),
            source_model="",
            source_date="",
            review_status=ReviewStatus.NEEDS_HUMAN_REVIEW if reasons else ReviewStatus.OK,
            review_reasons=tuple(reasons[:32]),
        )

    def _match_label(self, line: str) -> "tuple[TurnRole | None, str, str]":
        """Whether *line* opens a new turn. Returns ``(role, evidence, remainder)``."""
        stripped = line.strip()
        if not stripped:
            return (None, "", "")
        head, sep, tail = stripped.partition(":")
        if sep and len(head) <= _MAX_LABEL_CHARS + 8:
            key = _normalize_label(head)
            role = _LABELS.get(key)
            if role is not None:
                return (role, f"label:{key}", tail.strip())
        # A heading or bold-only line with no colon: `## Human`, `**Claude**`.
        shaped_text = _shaped_label(line)
        if shaped_text:
            key = _normalize_label(shaped_text)
            role = _LABELS.get(key)
            if role is not None:
                return (role, f"label:{key}", "")
        return (None, "", "")


def known_labels() -> dict:
    """The closed label table, for the docs and the input-contract report."""
    out: dict[str, list[str]] = {}
    for label, role in sorted(_LABELS.items()):
        out.setdefault(role.value, []).append(label)
    return out


def role_for_label(raw: str) -> "TurnRole | None":
    """The role a candidate label denotes, or ``None`` when the table does not know it.

    Public so that a second adapter over a different CONTAINER can reuse this table
    instead of restating it. M67A.1 added the DOCX adapter, and a Word-saved chat uses the
    same ``Human:`` / ``Claude:`` vocabulary as a ``.txt`` one — only the bytes around it
    differ. Two copies of the table would be two places for it to drift, and a role table
    that drifts between containers means the same conversation classifies differently
    depending on which file the operator happened to save it as.

    The table stays CLOSED: this returns ``None`` rather than a guess, and every caller
    turns ``None`` into ``UNKNOWN`` plus a review reason.
    """
    return _LABELS.get(_normalize_label(raw))


def label_shaped(line: str) -> str:
    """The candidate label text from a label-shaped line, or ``""``. See :func:`_shaped_label`."""
    return _shaped_label(line)


__all__ = ["TranscriptAdapter", "known_labels", "label_shaped", "role_for_label"]
