"""training_gym/teachers/review_import.py — V69 M62: accepting a teacher's answer.

WHY THIS EXISTS
---------------
This is the module where content written by a model the operator does not control
enters the gym's records. It is the highest-value target in the milestone: everything
upstream is deterministic and local, and everything downstream — the consensus engine,
the reward penalty, the human-review queue — will treat whatever passes through here as
a fact about the attempt.

So nothing is trusted, in this order:

1. **The bytes.** :func:`strict_json_object` parses. It refuses ``NaN``, ``Infinity``
   and ``-Infinity`` (which Python's ``json`` accepts by default and which no strict
   reader on the other side of this record can parse back), refuses duplicate keys
   (where ``{"recommendation": "reject", "recommendation": "approve"}`` silently becomes
   the last one), refuses anything that is not a single top-level object, and refuses a
   response larger than the ceiling before it is decoded.
2. **The fields.** Unknown keys are refused, not ignored. A model that added
   ``"override": true`` must not have it silently dropped and the rest accepted — the
   operator would be reading a review that answered a different question.
3. **The bindings.** Task, attempt, deterministic report, packet id, packet hash,
   provider, model and rubric version are each compared against the packet. Every one
   of them is a separate replay vector and every one is checked.
4. **The privacy.** The response is scanned before it is stored. A reviewer that echoed
   a secret back — or that was fed one by a prompt-injection attempt inside the reviewed
   answer — must not have it persisted by the very layer that exists to keep it out.
5. **The single use.** A packet id is consumed exactly once when the policy says so, so
   the same favourable review cannot be filed twice to satisfy a two-provider quorum.

WHAT IS NOT USED AS A DEFENCE
-----------------------------
Timestamps. A ``created_at_utc`` is recorded because an audit trail needs one, but it
never decides whether a review is fresh: it is a value the reviewing model writes, and
a defence an attacker can type is not a defence. Freshness comes from the
deterministic-report hash — change the graders and every prior review stops binding.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..schemas import (
    ResultStatus,
    canonical_json,
    require_float,
    require_str_tuple,
    sha256_text,
)
from ..task_spec import require_timestamp
from ..trajectory import Recommendation, TeacherReview
from .base import (
    MAX_TEACHER_RESPONSE_BYTES,
    RUBRIC_DIMENSIONS,
    ReviewMode,
    TeacherError,
    TeacherKind,
    TeacherReviewRecord,
)
from .sanitization import scan_export_payload

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime
    from .manual_packet import ManualReviewPacket

#: The complete set of keys a teacher response may carry. Anything else is refused.
TEACHER_RESPONSE_FIELDS: tuple[str, ...] = (
    "packet_id", "packet_hash", "task_hash", "attempt_hash",
    "deterministic_report_hash", "rubric_version", "provider", "model",
    "overall_score", "dimension_scores", "recommendation", "confidence",
    "factual_errors", "unsupported_claims", "missing_evidence", "unsafe_behavior",
    "style_issues", "corrected_answer",
)

#: The keys a response must carry. A review with no recommendation and no score is not
#: a review, and defaulting either one would be inventing the reviewer's opinion.
REQUIRED_RESPONSE_FIELDS: tuple[str, ...] = (
    "packet_id", "packet_hash", "task_hash", "attempt_hash",
    "deterministic_report_hash", "rubric_version", "provider", "model",
    "overall_score", "recommendation",
)

#: Ceiling on a corrected answer a reviewer may propose. It is a suggestion for a human
#: to read, never something applied automatically, so it does not need to be large.
MAX_CORRECTED_ANSWER_CHARS = 8_000
MAX_LIST_ITEMS = 32


class ReviewImportError(TeacherError):
    """A teacher response could not be accepted. Never partially accepted."""


class ReplayRejected(ReviewImportError):
    """This review has already been used, or belongs to another subject.

    A distinct type because the operator action differs: a malformed review is
    re-requested, while a replayed one means something tried to reuse evidence.
    """


# ── strict JSON ───────────────────────────────────────────────────────────────
def strict_json_object(text: object) -> dict:
    """Parse exactly one JSON object, refusing everything a lenient reader accepts.

    ``json.loads`` is lenient in three ways that matter here. It accepts the
    non-standard constants ``NaN``, ``Infinity`` and ``-Infinity``, which cannot be
    re-serialised and which would make a score neither in range nor out of it. It keeps
    the LAST of a duplicated key, so a response can state a rejection and an approval
    and be read as the approval. And it happily returns a list, a number or a string —
    none of which is a review, but each of which a caller doing ``data.get(...)`` on a
    dict-shaped assumption may mishandle.
    """
    if isinstance(text, (bytes, bytearray)):
        raise ReviewImportError("teacher response: expected text, got bytes")
    if not isinstance(text, str):
        raise ReviewImportError(f"teacher response: expected a JSON string, got "
                                f"{type(text).__name__}")
    size = len(text.encode("utf-8", "surrogatepass"))
    if size > MAX_TEACHER_RESPONSE_BYTES:
        raise ReviewImportError(f"teacher response: {size} bytes exceeds the "
                                f"{MAX_TEACHER_RESPONSE_BYTES}-byte ceiling")
    stripped = text.strip()
    if not stripped:
        raise ReviewImportError("teacher response: empty; a reviewer that said nothing "
                                "has not reviewed anything")

    def _reject_constant(name: str) -> float:
        raise ReviewImportError(
            f"teacher response: the non-standard JSON constant {name!r} is not a "
            f"number; a score that is not finite is not a score")

    def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                raise ReviewImportError(
                    f"teacher response: duplicate key {key!r}. A lenient reader keeps "
                    f"the last one, so a response could state a rejection and an "
                    f"approval and be read as the approval.")
            seen.add(key)
        return dict(pairs)

    try:
        data = json.loads(stripped, parse_constant=_reject_constant,
                          object_pairs_hook=_reject_duplicates)
    except ReviewImportError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError) as exc:
        # The reason is summarised, never echoed: the raw text may hold anything, and
        # an error message is a place people forget can leak.
        raise ReviewImportError(
            f"teacher response: not valid JSON ({type(exc).__name__}). A reviewing "
            f"model that answered in prose has not answered.") from None
    if not isinstance(data, dict):
        raise ReviewImportError(f"teacher response: expected one JSON object, got "
                                f"{type(data).__name__}")
    return data


# ── field-level validation ────────────────────────────────────────────────────
def _require_score(data: Mapping[str, object], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool):
        raise ReviewImportError(f"teacher response: {key} is a boolean ({value!r}); a "
                                f"score must be a number")
    if not isinstance(value, (int, float)):
        raise ReviewImportError(f"teacher response: {key} must be a number, got "
                                f"{type(value).__name__}")
    try:
        # ``require_float`` is the frozen authority: it refuses NaN, both infinities and
        # anything outside the range, so this layer cannot become more permissive.
        return require_float(value, f"teacher response: {key}",
                             minimum=0.0, maximum=1.0)
    except Exception as exc:  # noqa: BLE001 — normalised to this layer's error type
        raise ReviewImportError(str(exc)) from None


def _require_strings(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    try:
        return require_str_tuple(data.get(key), f"teacher response: {key}",
                                 max_items=MAX_LIST_ITEMS)
    except Exception as exc:  # noqa: BLE001 — normalised to this layer's error type
        raise ReviewImportError(str(exc)) from None


def _require_dimension_scores(data: Mapping[str, object]) -> dict[str, float]:
    raw = data.get("dimension_scores")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ReviewImportError(f"teacher response: dimension_scores must be an "
                                f"object, got {type(raw).__name__}")
    unknown = sorted(str(k) for k in raw if str(k) not in RUBRIC_DIMENSIONS)
    if unknown:
        raise ReviewImportError(
            f"teacher response: unknown rubric dimension(s) {unknown}; allowed "
            f"{list(RUBRIC_DIMENSIONS)}. A dimension nobody asked about would be "
            f"averaged into a score by someone downstream.")
    return {str(k): _require_score(raw, str(k)) for k in raw}


def _require_recommendation(data: Mapping[str, object]) -> Recommendation:
    value = data.get("recommendation")
    if not isinstance(value, str):
        raise ReviewImportError(f"teacher response: recommendation must be a string, "
                                f"got {type(value).__name__}")
    try:
        return Recommendation(value.strip().lower())
    except ValueError:
        raise ReviewImportError(
            f"teacher response: unknown recommendation {value!r}; allowed "
            f"{[r.value for r in Recommendation]}") from None


# ── the import ────────────────────────────────────────────────────────────────
def parse_review_json(text: object, *, packet: "ManualReviewPacket",
                      provider_id: str, provider_version: str,
                      provider_kind: TeacherKind, mode: ReviewMode,
                      created_at_utc: str) -> TeacherReviewRecord:
    """Turn one raw teacher answer into a bound, validated record, or refuse.

    Pure: no clock, no I/O, no ledger. Replay bookkeeping is
    :func:`register_review`'s job, so this function can be used to inspect a response
    without consuming the packet — which is what an operator wants when a paste went
    wrong.
    """
    from .manual_packet import ManualReviewPacket

    if not isinstance(packet, ManualReviewPacket):
        raise ReviewImportError(f"review import: expected a ManualReviewPacket, got "
                                f"{type(packet).__name__}")
    # The packet is the yardstick every binding below is measured against, so it is
    # re-verified first. Measuring against a tampered packet would validate a review
    # against the tampering.
    packet.verify_integrity()
    require_timestamp(created_at_utc, "review import: created_at_utc")

    data = strict_json_object(text)
    unknown = sorted(set(data) - set(TEACHER_RESPONSE_FIELDS))
    if unknown:
        raise ReviewImportError(
            f"teacher response: unknown field(s) {unknown}. A field nobody asked for is "
            f"refused rather than dropped — a response carrying an unexpected key is "
            f"answering a different question than the one in the packet.")
    missing = sorted(set(REQUIRED_RESPONSE_FIELDS) - set(data))
    if missing:
        raise ReviewImportError(f"teacher response: missing required field(s) "
                                f"{missing}")

    # Privacy before persistence. The response is untrusted content that may echo the
    # packet back or may have been steered by an injection inside the reviewed answer.
    leaks = scan_export_payload(data)
    if leaks:
        raise ReviewImportError(
            f"teacher response: refusing to store — private content present "
            f"({', '.join(leaks)}). A reviewer that echoed a secret back must not have "
            f"it persisted by the layer that exists to keep it out.")

    corrected = str(data.get("corrected_answer", "") or "")
    if len(corrected) > MAX_CORRECTED_ANSWER_CHARS:
        raise ReviewImportError(f"teacher response: corrected_answer is "
                                f"{len(corrected)} chars (max "
                                f"{MAX_CORRECTED_ANSWER_CHARS})")

    declared_provider = str(data.get("provider", "")).strip()
    declared_model = str(data.get("model", "")).strip()
    review = TeacherReview(
        provider=declared_provider,
        model=declared_model,
        task_hash=str(data.get("task_hash", "")).strip().lower(),
        attempt_hash=str(data.get("attempt_hash", "")).strip().lower(),
        rubric_version=str(data.get("rubric_version", "")).strip(),
        overall_score=_require_score(data, "overall_score"),
        recommendation=_require_recommendation(data),
        timestamp=created_at_utc,
        dimension_scores=_require_dimension_scores(data),
        factual_errors=_require_strings(data, "factual_errors"),
        unsupported_claims=_require_strings(data, "unsupported_claims"),
        missing_evidence=_require_strings(data, "missing_evidence"),
        unsafe_behavior=_require_strings(data, "unsafe_behavior"),
        style_issues=_require_strings(data, "style_issues"),
        corrected_answer=corrected,
        confidence=_require_score(data, "confidence") if "confidence" in data else 0.0,
    )
    try:
        record = TeacherReviewRecord(
            provider=declared_provider or provider_id,
            provider_version=provider_version,
            provider_kind=provider_kind,
            model=declared_model,
            review_mode=mode,
            task_hash=review.task_hash,
            attempt_hash=review.attempt_hash,
            deterministic_report_hash=str(
                data.get("deterministic_report_hash", "")).strip().lower(),
            packet_id=str(data.get("packet_id", "")).strip(),
            packet_hash=str(data.get("packet_hash", "")).strip().lower(),
            rubric_version=review.rubric_version,
            created_at_utc=created_at_utc,
            review=review,
        )
    except ReviewImportError:
        raise
    except TeacherError as exc:
        raise ReviewImportError(str(exc)) from None
    # A binding failure is not a malformed review — it is a review about a DIFFERENT
    # subject being filed against this packet, which is the definition of a replay.
    # Naming it as one is what lets an operator tell "the model answered badly" apart
    # from "someone tried to reuse an answer".
    try:
        record.verify_binding(
            task_hash=packet.task_hash,
            attempt_hash=packet.attempt_hash,
            deterministic_report_hash=packet.deterministic_report_hash,
            packet_id=packet.packet_id,
            packet_hash=packet.packet_hash,
            provider=packet.requested_provider,
            model=packet.requested_model,
            rubric_version=packet.rubric_version,
        )
    except TeacherError as exc:
        raise ReplayRejected(str(exc)) from None
    if record.provider != provider_id:
        raise ReviewImportError(
            f"teacher response: claims provider {record.provider!r} but was imported "
            f"through the {provider_id!r} adapter; a provider label is not a field a "
            f"response gets to choose")
    return record


def parse_teacher_response(text: object, *, packet: object, provider: object,
                           created_at_utc: str, mode: ReviewMode,
                           ledger: object | None = None) -> TeacherReviewRecord:
    """Normalise a provider's raw answer, consuming the packet when a ledger is given.

    The seam :meth:`~training_gym.teachers.base.TeacherProvider.review` calls, so every
    provider — mock, local, cloud, manual — reaches the records layer through exactly
    this validation. A provider cannot construct a record itself: the constructor is
    reachable, but no provider ever holds the packet's expectations and the ledger at the
    same time except through here.
    """
    record = parse_review_json(
        text, packet=packet,  # type: ignore[arg-type]
        provider_id=getattr(provider, "provider_id", ""),
        provider_version=getattr(provider, "provider_version", ""),
        provider_kind=getattr(provider, "provider_kind", TeacherKind.MOCK),
        mode=mode, created_at_utc=created_at_utc)
    if ledger is not None:
        register_review(record, ledger=ledger)
    return record


def register_review(record: TeacherReviewRecord, *, ledger: object) -> str:
    """Consume this record's packet, refusing a second use. Returns the review hash.

    Single-use is enforced on the PACKET id rather than on the review hash. Hashing the
    review would let a second import of the same packet through as long as one character
    differed — a re-run of the same reviewer, or a hand-edited score — and the whole
    point is that one packet buys one opinion.
    """
    review_hash = record.review_hash()
    consume = getattr(ledger, "consume", None)
    if not callable(consume):
        raise ReviewImportError("review import: the replay ledger does not implement "
                                "consume(); refusing to import without replay "
                                "protection")
    consume(packet_id=record.packet_id, review_hash=review_hash,
            provider=record.provider, created_at_utc=record.created_at_utc)
    return review_hash


def outcome_status_for(record: TeacherReviewRecord | None) -> ResultStatus:
    """The status a provider outcome carries for *record*.

    A record exists or it does not; there is no partial credit. Kept as a function so
    the mapping lives beside the import rules rather than being restated by every
    provider.
    """
    return ResultStatus.PASS if record is not None else ResultStatus.INSUFFICIENT_EVIDENCE


def response_fingerprint(text: object) -> str:
    """A digest of a raw response, for an audit trail that stores no raw response."""
    if isinstance(text, str):
        return sha256_text(text)
    return sha256_text(canonical_json(text) if text is not None else "")


__all__ = [
    "MAX_CORRECTED_ANSWER_CHARS", "MAX_LIST_ITEMS", "REQUIRED_RESPONSE_FIELDS",
    "TEACHER_RESPONSE_FIELDS", "ReplayRejected", "ReviewImportError",
    "outcome_status_for", "parse_review_json", "parse_teacher_response",
    "register_review", "response_fingerprint", "strict_json_object",
]
