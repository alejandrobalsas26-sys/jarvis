"""training_gym/evaluation/score_evidence.py — V69 M62 S3F.2: body-free review evidence.

WHY THIS EXISTS
---------------
S3E.2 produced three differential security findings and a reviewer who could not look at
any of them. ``EvaluationResult`` persists ``response_sha256`` and never the body — a
deliberate privacy property — and ``ArmScore``, which holds the grader statuses, the
security-finding *categories*, the refusal class and the parser outcome, was computed and
then thrown away. Only its digest survived, in ``paired-comparisons.jsonl``.

So almost everything a reviewer needs was body-free by construction and was being
discarded, while the one thing that is not body-free was correctly withheld. This module
persists the first and still withholds the second.

WHAT IS NOT HERE
----------------
No response text, no excerpt, no redacted excerpt, no hidden target, no expected answer,
no teacher answer, no absolute path, no exception text. This milestone deliberately does
NOT introduce raw-response persistence in any form: an operator ruling (H1) turns on
materiality, and materiality is a question body-free evidence cannot answer. Answering it
needs a separate authorisation with a different privacy cost, not a quiet widening here.

WHY A CLOSED FIELD LIST AND A CLOSED VOCABULARY
-----------------------------------------------
``ArmScore.notes`` is prose written for a person, and prose written *about* a response
quotes it: a jsonschema violation message embeds the offending value, and a tool-call
problem embeds the proposed tool's name. Persisting those would be persisting a response
body in instalments. The evidence therefore carries ``note_codes`` — the closed
``scoring.NOTE_CODES`` vocabulary — and drops the prose, which stays covered by
``score_hash`` so it is bound without being published. Same reasoning for the tool review:
``valid``/``critical`` and a problem *count*, never the problem strings.

HOW IT IS BOUND
---------------
Every record carries ``response_sha256`` (the same digest ``baseline-results.jsonl``
records) and ``score_hash`` (the same digest ``paired-comparisons.jsonl`` records). Those
two make the evidence checkable against artefacts that already existed rather than
self-consistent: a line moved between arms, attached to the wrong task, or edited after
sealing contradicts a file the manifest already covers.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..schemas import (
    SchemaError,
    assert_no_private_content,
    reject_unknown_fields,
    require_mapping,
    sha256_obj,
    short,
)
from .comparison import PairedComparison
from .scoring import NOTE_CODES, ArmScore

#: Bumped when the evidence record's shape changes. ``.2`` adds
#: ``output_budget_exhausted`` (D38). ``.3`` adds the S3T.0 termination diagnostics: the
#: JSON parser's error CLASS and location, and one repetition statistic over the
#: response. Records written under ``.1`` and ``.2`` still read: the field list is an
#: ALLOWLIST, so a record that omits the new keys is accepted and a record that carries
#: an undeclared one is refused — historical evidence verifies unchanged, and NOTHING is
#: backfilled onto it, because a diagnosis nobody computed must not be invented for a
#: response nobody kept.
SCORE_EVIDENCE_VERSION = "m62.evaluation_score_evidence.3"

#: The file each arm's evidence is written to.
BASELINE_SCORES_FILE = "baseline-scores.jsonl"
CANDIDATE_SCORES_FILE = "candidate-scores.jsonl"

#: ``role`` -> filename. The role is part of the record as well as of the filename, so a
#: whole file swapped for the other arm's is detectable from the bytes rather than only
#: from the name it happens to be sitting under.
SCORE_EVIDENCE_FILES: dict[str, str] = {
    "baseline": BASELINE_SCORES_FILE,
    "candidate": CANDIDATE_SCORES_FILE,
}

#: Exactly what a record may contain. Anything outside this list is refused on read, so a
#: field added by a future writer cannot arrive in a reviewed file unannounced.
SCORE_EVIDENCE_FIELDS: tuple[str, ...] = (
    "evidence_version", "scoring_version", "evaluation_id", "generation", "arm",
    "task_id", "task_hash", "family", "split", "status", "reward", "refusal",
    "schema_valid", "json_parseable", "evidence_findings", "tool_call_valid",
    "tool_call_critical", "tool_call_problem_count", "security_findings",
    "hygiene_findings", "grader_statuses", "missing_graders", "blocking", "severity",
    "latency_ms", "output_tokens", "truncated", "output_budget_exhausted", "timed_out",
    "empty", "note_codes", "json_parse_error_kind", "json_parse_error_line",
    "json_parse_error_column", "json_parse_error_position",
    "response_unique_char_ngram_ratio", "response_sha256", "score_hash",
)


class ScoreEvidenceError(SchemaError):
    """Review evidence that will not be written, or will not be believed."""


def score_evidence_record(score: ArmScore, *, evaluation_id: str, generation: int,
                          response_sha256: str) -> dict:
    """One arm's body-free verdict on one task.

    *response_sha256* is the digest the results artefact already recorded for this arm
    and task. It is passed in rather than recomputed because recomputing it would require
    the response text, which this layer must never hold.
    """
    unknown = sorted(set(score.note_codes) - NOTE_CODES)
    if unknown:  # pragma: no cover - ArmScore refuses this at construction
        raise ScoreEvidenceError(
            f"score evidence {score.task_id!r}: unknown note code(s) {unknown}")
    review = score.tool_review
    record = {
        "evidence_version": SCORE_EVIDENCE_VERSION,
        "scoring_version": score.to_dict()["scoring_version"],
        "evaluation_id": str(evaluation_id),
        "generation": int(generation),
        "arm": score.role,
        "task_id": score.task_id,
        "task_hash": score.task_hash,
        "family": score.family,
        "split": score.split,
        "status": score.status.value,
        "reward": score.reward,
        "refusal": score.refusal.value,
        "schema_valid": score.schema_valid,
        "json_parseable": score.json_parseable,
        "evidence_findings": [f.value for f in score.evidence_findings],
        # The verdict and a count, never the problem strings: those name the tool the
        # model proposed, which is the model's output.
        "tool_call_valid": bool(review.valid) if review else None,
        "tool_call_critical": bool(review.critical) if review else None,
        "tool_call_problem_count": len(review.problems) if review else 0,
        "security_findings": list(score.security_findings),
        "hygiene_findings": list(score.hygiene_findings),
        "grader_statuses": dict(sorted(score.grader_statuses.items())),
        "missing_graders": list(score.missing_graders),
        "blocking": bool(score.blocking),
        "severity": score.severity.value,
        "latency_ms": int(score.latency_ms),
        "output_tokens": int(score.output_tokens),
        # INPUT truncation. Kept exactly as it was; D38 adds a sibling rather than
        # re-pointing this one at the response.
        "truncated": bool(score.truncated),
        # OUTPUT-budget exhaustion (D38). Tri-state, so it is NOT coerced with bool():
        # None means the arm produced no output and the termination state is unmeasured,
        # and bool(None) would publish that as a clean, non-exhausted completion.
        "output_budget_exhausted": score.output_budget_exhausted,
        "timed_out": bool(score.timed_out),
        "empty": bool(score.empty),
        "note_codes": list(score.note_codes),
        # -- S3T.0 body-free termination diagnostics --------------------------------
        # The parser's error CLASS, never its message: CPython's pure-Python decoder
        # formats the offending character into ``JSONDecodeError.msg``, so persisting
        # the message would persist one character of the response on any build without
        # the C accelerator. ``scoring.JsonParseErrorKind`` is the closed vocabulary
        # that survives instead, and an unrecognised message becomes
        # ``other_json_parse_error`` rather than being stored verbatim.
        #
        # The location is three integer OFFSETS into the response. They quote nothing,
        # and the response length they are bounded by is already published as
        # ``response_chars`` in the results artefact, so an offset discloses strictly
        # less than a field this evaluation has always written.
        #
        # Tri-state, so NOT coerced: ``None`` means no parse FAILED here — the document
        # parsed, the response was empty, it never left its reasoning block, or the
        # family requested no structural check. A zero would publish a position the
        # parser never reached.
        "json_parse_error_kind": (score.json_parse_error_kind.value
                                  if score.json_parse_error_kind else None),
        "json_parse_error_line": score.json_parse_error_line,
        "json_parse_error_column": score.json_parse_error_column,
        "json_parse_error_position": score.json_parse_error_position,
        # One scalar in (0, 1]: distinct 16-character shingles over total shingles. The
        # shingles themselves are counted and discarded inside
        # ``scoring.unique_char_ngram_ratio`` and never reach this record — a ratio
        # cannot be inverted into the text it was measured over. ``None`` where the arm
        # produced no output or the response was shorter than one window.
        "response_unique_char_ngram_ratio": score.response_unique_char_ngram_ratio,
        "response_sha256": str(response_sha256),
        "score_hash": score.score_hash(),
    }
    # Belt and braces over a closed field list: the categories this scan owns (secret,
    # OTP, home path, command line) must not appear even in a field that is supposed to
    # be an enum, because a grader id or a finding category is still a string somebody
    # could widen later.
    assert_no_private_content(record, label="evaluation score evidence")
    return record


def build_score_evidence(comparisons: Sequence[PairedComparison], *, role: str,
                         evaluation_id: str, generation: int,
                         response_digests: Mapping[str, str]) -> tuple[dict, ...]:
    """Every scored task for one arm, in canonical ``task_id`` order.

    Ordering is fixed here rather than inherited from the comparison sequence so the file
    is reproducible from the same inputs whatever order the pairs were executed in — the
    runner deliberately alternates that order.
    """
    if role not in SCORE_EVIDENCE_FILES:
        raise ScoreEvidenceError(
            f"score evidence: unknown arm {role!r}; the arms are "
            f"{sorted(SCORE_EVIDENCE_FILES)}")
    attribute = "baseline_score" if role == "baseline" else "candidate_score"
    records: list[dict] = []
    for comparison in comparisons:
        score = getattr(comparison, attribute)
        if score is None:
            # An arm that produced no scored result has no verdict to review. It is
            # already visible as a missing pair in the comparison record; inventing a
            # placeholder here would put a row in a reviewed file that measures nothing.
            continue
        if score.role != role:
            raise ScoreEvidenceError(
                f"score evidence: the {role} slot of task {comparison.task_id!r} holds "
                f"a {score.role!r} score; filing it would attribute one arm's answer to "
                f"the other")
        digest = response_digests.get(score.task_id, "")
        if not digest:
            raise ScoreEvidenceError(
                f"score evidence: no {role} response digest for task "
                f"{score.task_id!r}; evidence that binds to nothing is not reviewable")
        records.append(score_evidence_record(
            score, evaluation_id=evaluation_id, generation=generation,
            response_sha256=digest))
    records.sort(key=lambda r: r["task_id"])
    return tuple(records)


def response_digests(result_records_: Sequence[Mapping]) -> dict[str, str]:
    """``task_id -> response_sha256`` taken from one arm's already-built result records."""
    return {str(r.get("task_id", "")): str(r.get("response_sha256", ""))
            for r in result_records_ if r.get("task_id")}


def read_score_evidence(payload: object, *, label: str) -> dict:
    """Validate one persisted record. Unknown fields are refused, not ignored."""
    data = dict(require_mapping(payload, label))
    reject_unknown_fields(data, SCORE_EVIDENCE_FIELDS, label=label)
    for required in ("task_id", "arm", "response_sha256", "score_hash"):
        if not str(data.get(required, "")).strip():
            raise ScoreEvidenceError(f"{label}: {required} is required")
    unknown = sorted(set(data.get("note_codes", ()) or ()) - NOTE_CODES)
    if unknown:
        raise ScoreEvidenceError(
            f"{label}: unknown note code(s) {unknown}; the vocabulary is closed")
    return data


def verify_score_evidence(records: Sequence[Mapping], *, role: str,
                          comparison_records: Sequence[Mapping],
                          result_records_: Sequence[Mapping],
                          evaluation_id: str = "",
                          generation: int | None = None) -> tuple[str, ...]:
    """Re-derive every claim one arm's evidence file makes. Empty tuple = verified.

    Checked against the OTHER artefacts rather than against itself: a file that only
    agreed with its own contents would prove it was written by one program, not that it
    describes this evaluation.
    """
    filename = SCORE_EVIDENCE_FILES.get(role, role)
    problems: list[str] = []
    parsed: list[dict] = []
    for index, payload in enumerate(records):
        try:
            parsed.append(read_score_evidence(payload, label=f"{filename}[{index}]"))
        except SchemaError as exc:
            problems.append(str(exc))
    if problems:
        return tuple(problems)

    seen: set[str] = set()
    order = [str(r["task_id"]) for r in parsed]
    if order != sorted(order):
        problems.append(
            f"{filename}: the records are not in canonical task_id order; the file is "
            f"written sorted, so a different order is a rewritten file")
    for record in parsed:
        task_id = str(record["task_id"])
        if task_id in seen:
            problems.append(f"{filename}: task {task_id!r} appears more than once")
        seen.add(task_id)
        if record["arm"] != role:
            problems.append(
                f"{filename}: task {task_id!r} records arm {record['arm']!r}; the "
                f"file is the {role} arm's, so this line belongs to the other arm")
        if evaluation_id and str(record["evaluation_id"]) != evaluation_id:
            problems.append(
                f"{filename}: task {task_id!r} names evaluation "
                f"{record['evaluation_id']!r} and the manifest says {evaluation_id!r}")
        if generation is not None and int(record["generation"]) != int(generation):
            problems.append(
                f"{filename}: task {task_id!r} names generation "
                f"{record['generation']} and the manifest says {generation}")

    # -- against the results artefact -----------------------------------------
    digests = response_digests(result_records_)
    for record in parsed:
        task_id = str(record["task_id"])
        want = digests.get(task_id)
        if want is None:
            problems.append(
                f"{filename}: task {task_id!r} has no {role} result; the evidence "
                f"describes a generation that is not in the results artefact")
        elif want != str(record["response_sha256"]):
            problems.append(
                f"{filename}: task {task_id!r} binds response digest "
                f"{short(str(record['response_sha256']))} and the {role} result records "
                f"{short(want)}; the evidence is about a different response")

    # -- against the comparison artefact --------------------------------------
    key = f"{role}_score_hash"
    expected = {str(c.get("task_id", "")): str(c.get(key, ""))
                for c in comparison_records}
    for record in parsed:
        task_id = str(record["task_id"])
        want = expected.get(task_id)
        if want is None:
            problems.append(
                f"{filename}: task {task_id!r} is not in the paired comparison")
        elif want and want != str(record["score_hash"]):
            problems.append(
                f"{filename}: task {task_id!r} records score digest "
                f"{short(str(record['score_hash']))} and the comparison sealed "
                f"{short(want)}; the score has been edited since it was compared")
    for task_id, digest in sorted(expected.items()):
        if digest and task_id not in seen:
            problems.append(
                f"{filename}: task {task_id!r} was scored on the {role} arm and has no "
                f"evidence line; a reviewer would read the omission as a task that was "
                f"never run")
    return tuple(problems)


def score_evidence_hash(records: Sequence[Mapping]) -> str:
    """Digest over one arm's evidence, for a caller that wants one value to compare."""
    return sha256_obj([dict(sorted(dict(r).items())) for r in records])


__all__ = [
    "BASELINE_SCORES_FILE", "CANDIDATE_SCORES_FILE", "SCORE_EVIDENCE_FIELDS",
    "SCORE_EVIDENCE_FILES", "SCORE_EVIDENCE_VERSION", "ScoreEvidenceError",
    "build_score_evidence", "read_score_evidence", "response_digests",
    "score_evidence_hash", "score_evidence_record", "verify_score_evidence",
]
