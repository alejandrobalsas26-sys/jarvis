"""training_gym/graders/evidence_grader.py — V69 M62: did it cite, and does it exist.

WHY THIS EXISTS
---------------
A triage verdict, a DFIR timeline and an evidence request are all judged on the same
property: every factual conclusion traces back to something that was actually
supplied. The failure this guards against is not "the model was wrong" — it is "the
model was fluent". A confident sentence naming ``ev_9f2c1a4b7e30`` reads exactly like
a supported one whether or not that identifier was ever in the material, and a corpus
that rewards the shape teaches the shape.

So this grader does two separable things, and reports them separately because they are
different faults:

  * **fabrication** — a reference that resolves to nothing. An invented event id, an
    invented hash, an invented artifact path. Blocking: a model that invents citations
    is worse than one that omits them, and a training example demonstrating it is
    actively harmful.
  * **omission** — a factual claim with no reference at all. A quality failure, scored
    down, not a security one.

WHAT IT DOES NOT CLAIM
----------------------
Nothing here checks whether a claim is TRUE. "The cited event exists and the sentence
points at it" is a deterministic, checkable property; "the sentence is a correct
inference from that event" is not, and pretending otherwise would put a confident
number on a judgement this layer cannot make. Semantic correctness is what the teacher
review and the human decision are for.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from ..schemas import ResultStatus, Severity
from ..task_spec import TaskFamily
from .base import (
    GRADER_PROTOCOL_VERSION,
    Grader,
    GraderContext,
    insufficient,
    make_result,
    not_applicable,
)

#: The application's own short-id shape (``core.incident_workspace._short_id``):
#: a short lowercase prefix, an underscore, and a hex digest slice.
_SHORT_ID_RE = re.compile(r"\b([a-z]{2,10}_[0-9a-f]{8,64})\b")
#: A full content digest.
_SHA256_RE = re.compile(r"\b([0-9a-f]{64})\b")
#: An explicitly bracketed reference: ``[ev_1a2b3c4d5e6f]``, ``[file:output/x.json]``.
_BRACKET_RE = re.compile(r"\[(?:ref:|file:|evidence:)?([^\]\s]{3,180})\]")
#: A bare workspace-relative path with an extension.
_PATH_RE = re.compile(r"\b((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8})\b")

#: A sentence must have at least this many words before it is treated as a factual
#: claim. Below it, the text is a heading, a label or a fragment — counting those as
#: uncited claims would bury the real ones in noise.
MIN_CLAIM_WORDS = 4
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUESTION_RE = re.compile(r"\?\s*$")
#: Cues that mark a sentence as a conclusion about the material rather than a
#: description of what the model is about to do.
_HEDGE_RE = re.compile(r"(?i)^\s*(?:i will|let me|next,|first,|to (?:do|begin)|"
                       r"here(?:'s| is) (?:how|what)|in summary,?\s*$)")


class EvidenceCitationGrader(Grader):
    """Checks that conclusions point at evidence, and that the evidence exists."""

    grader_id = "evidence_citation"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.evidence_citation.1"
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset({
        TaskFamily.SOC_TRIAGE, TaskFamily.DFIR_TIMELINE, TaskFamily.EVIDENCE_REQUEST,
        TaskFamily.STRUCTURED_REPORT})

    def __init__(self, min_coverage: float = 0.6) -> None:
        self.min_coverage = max(0.0, min(1.0, float(min_coverage)))

    def measure(self, ctx: GraderContext):
        if ctx.refused and ctx.spec.scoring.expect_refusal:
            return not_applicable(
                self, "the attempt refused, as the task expected; there are no "
                      "conclusions to trace back to evidence")

        universe = self._known_references(ctx)
        if not universe:
            return insufficient(
                self, "the task supplied no evidence identifiers, fixtures or "
                      "artifacts, so no citation could be resolved either way")

        claims = self._claims(ctx)
        structured_refs = _references_in(_flatten(ctx.structured_output))
        if not claims and not structured_refs:
            return insufficient(
                self, "the attempt made no evaluable factual claim and cited nothing; "
                      "an answer with no assertions has not been assessed",
                measured=0)

        findings: list[dict] = []
        supported = 0
        fabricated: set[str] = set()
        checked_refs = 0

        for index, claim in enumerate(claims):
            refs = _references_in(claim)
            checked_refs += len(refs)
            unknown = sorted(r for r in refs if r not in universe)
            if unknown:
                fabricated.update(unknown)
                findings.append({
                    "kind": "fabricated_reference", "claim_index": index,
                    "blocking": True, "references": unknown[:8],
                    "detail": "the claim cites an identifier, hash or artifact that "
                              "does not exist in this task or attempt",
                    "claim": ctx.sanitize(claim, limit=160)})
            elif refs:
                supported += 1
            else:
                findings.append({
                    "kind": "uncited_claim", "claim_index": index, "blocking": False,
                    "detail": "a factual conclusion with no reference to the supplied "
                              "material",
                    "claim": ctx.sanitize(claim, limit=160)})

        for ref in sorted(structured_refs):
            checked_refs += 1
            if ref not in universe:
                fabricated.add(ref)
                findings.append({
                    "kind": "fabricated_reference", "location": "structured_output",
                    "blocking": True, "references": [ref],
                    "detail": "the structured output cites an identifier that does not "
                              "exist in this task or attempt"})

        measured = len(claims) + checked_refs
        if measured <= 0:
            return insufficient(self, "no claim and no reference were evaluated")

        if fabricated:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, blocking=True,
                severity=Severity.HIGH, measured=measured, findings=findings,
                evidence=(f"{len(fabricated)} invented reference(s) across "
                          f"{len(claims)} claim(s); a fabricated citation is worse "
                          f"than a missing one",))

        coverage = supported / len(claims) if claims else 1.0
        if coverage < self.min_coverage:
            return make_result(
                self, ResultStatus.FAIL, score=round(coverage, 6),
                severity=Severity.MEDIUM, measured=measured, findings=findings,
                evidence=(f"{supported}/{len(claims)} claim(s) cite supplied evidence; "
                          f"the task requires {self.min_coverage:.2f}",))

        return make_result(
            self, ResultStatus.PASS, score=round(coverage, 6), measured=measured,
            findings=findings,
            evidence=(f"{supported}/{len(claims)} claim(s) cite evidence that exists; "
                      f"{checked_refs} reference(s) resolved against "
                      f"{len(universe)} known identifier(s)",))

    # -- inputs ----------------------------------------------------------------
    def _known_references(self, ctx: GraderContext) -> frozenset[str]:
        """Everything an answer is allowed to point at.

        Deliberately built from the task and the attempt only. A reference that
        resolves against some other episode's material is a fabrication as far as THIS
        attempt is concerned."""
        known: set[str] = set(ctx.evidence_ids)
        known.update(ctx.input_hashes)
        known.update(ctx.input_hashes.values())
        known.update(ctx.output_hashes)
        known.update(ctx.output_hashes.values())
        for fixture in ctx.spec.fixtures:
            known.add(fixture.path)
            known.add(fixture.sha256)
        for changed in ctx.changed_files:
            known.add(changed.path)
        return frozenset(k for k in known if k)

    def _claims(self, ctx: GraderContext) -> tuple[str, ...]:
        """Sentences that assert something about the material."""
        claims: list[str] = []
        for raw in _SENTENCE_SPLIT_RE.split(str(ctx.answer or "")):
            sentence = raw.strip(" \t-*•")
            if not sentence or _QUESTION_RE.search(sentence):
                continue
            if _HEDGE_RE.match(sentence):
                continue
            if len(sentence.split()) < MIN_CLAIM_WORDS:
                continue
            claims.append(sentence)
        return tuple(claims)


def _references_in(text: str) -> frozenset[str]:
    """Every citation-shaped token in *text*."""
    blob = str(text or "")
    found: set[str] = set()
    for pattern in (_SHORT_ID_RE, _SHA256_RE, _PATH_RE):
        found.update(m.group(1) for m in pattern.finditer(blob))
    for match in _BRACKET_RE.finditer(blob):
        token = match.group(1).strip()
        # A bracketed token only counts as a citation when it looks like an
        # identifier; ``[optional]`` in prose is not a reference to anything.
        if _SHORT_ID_RE.fullmatch(token) or _SHA256_RE.fullmatch(token) \
                or _PATH_RE.fullmatch(token):
            found.add(token)
    return frozenset(found)


def _flatten(value: Any, *, _depth: int = 0) -> str:
    """A scannable text form of a structured document. Bounded depth."""
    if _depth > 12 or value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(f"{k} {_flatten(v, _depth=_depth + 1)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_flatten(v, _depth=_depth + 1) for v in value)
    return str(value)


__all__ = ["MIN_CLAIM_WORDS", "EvidenceCitationGrader"]
