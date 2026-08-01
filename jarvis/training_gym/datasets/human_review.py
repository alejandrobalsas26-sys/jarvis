"""training_gym/datasets/human_review.py — V69 M62 S2d: the only thing that approves.

WHY A SECOND REVIEW RECORD EXISTS
---------------------------------
:class:`~training_gym.trajectory.HumanReview` already exists and is already hash-bound —
but it answers a different question. It says *"I, the operator, accept this attempt"*,
and it binds to one subject: the attempt. That is exactly right for closing an episode.

Promoting an attempt into a **dataset** asks something the episode review never asked:
*"I accept these exact target bytes, from this exact source, as something the model
should learn to imitate."* Those come apart constantly. An attempt can be a perfectly
good attempt whose answer is not the answer you want trained in; a teacher's correction
can be better than the student's output and still be a cloud model's guess nobody read.

So :class:`DatasetHumanReview` binds FOUR upstream digests instead of one — task,
attempt, deterministic report, consensus report — and adds the two fields the episode
review has no reason to carry: :class:`~training_gym.datasets.candidate.TargetSource` and
the digest of the approved target text. It does not replace, weaken or bypass the episode
review; :func:`~training_gym.datasets.promotion.candidate_blockers` requires BOTH.

REPLAY IS THE THREAT
--------------------
A well-formed APPROVED review is the most valuable object in this subsystem. Anyone who
wants material trained on wants one. The defences, in order:

  * four bindings, so a review cannot be lifted onto another subject;
  * ``target_hash``, so it cannot survive an edit to the bytes it approved;
  * an append-only :class:`HumanReviewLedger`, so one review approves one candidate and
    a second use is refused rather than merely discouraged;
  * the timestamp is RECORDED but never used as a defence — a reviewer writes their own
    timestamp, and a defence someone can type is not one.

WHAT IS NOT A HUMAN REVIEW
--------------------------
Teacher agreement. A local verifier's response. A CLI ``--yes``. A boolean a caller
passed in. A consensus report marked ``eligible_for_human_approval`` — that boolean is
the weakest claim in the milestone and means only *a human may now be asked*.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..atomicio import AtomicIOError, append_jsonl, read_jsonl
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    check_schema_version,
    reject_unknown_fields,
    require_binding,
    require_enum,
    require_id,
    require_mapping,
    sha256_obj,
    short,
)
from ..task_spec import require_timestamp
from .candidate import TargetSource, require_digest

#: Bump when the review SHAPE changes. Recorded in every persisted review.
REVIEW_PROTOCOL_VERSION = "m62.dataset_review.1"

#: Ceiling on the append-only review ledger. An audit trail, not an archive.
MAX_REVIEW_LEDGER_LINES = 100_000

#: Ceiling on a reviewer's free-text note. Long enough to state a reason, short enough
#: that the field cannot become a place to paste material the sanitizer never saw.
MAX_REVIEW_NOTE_CHARS = 2_000


class DatasetReviewError(SchemaError):
    """A dataset review was refused. Never downgraded, never defaulted."""


class ReviewReplayRejected(DatasetReviewError):
    """A review that has already approved a candidate was presented again."""


class DatasetReviewDecision(str, Enum):
    """What the operator decided about promoting this material."""

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"
    QUARANTINED = "quarantined"

    @property
    def approves_dataset(self) -> bool:
        """The single boolean that authorises promotion. Only one member sets it."""
        return self is DatasetReviewDecision.APPROVED

    @property
    def bars_preference_use(self) -> bool:
        """Decisions after which the material may not appear in a preference pair.

        ``REJECTED`` is deliberately absent: a rejected answer is exactly what the
        ``rejected`` side of a preference pair is for. ``QUARANTINED`` is present
        because quarantine means "must never be stored in a trainable artefact, in any
        role", and ``NEEDS_REVISION`` is present because the answer is not final.
        """
        return self in (DatasetReviewDecision.QUARANTINED,
                        DatasetReviewDecision.NEEDS_REVISION)


_REVIEW_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "protocol_version", "review_id", "reviewer", "decision", "timestamp",
    "task_hash", "attempt_hash", "deterministic_report_hash", "consensus_report_hash",
    "target_source", "target_hash", "editor_id", "reason", "review_hash",
)


@dataclass(frozen=True)
class DatasetHumanReview:
    """One operator decision about one candidate's exact target bytes."""

    review_id: str
    reviewer: str
    decision: DatasetReviewDecision
    timestamp: str
    task_hash: str
    attempt_hash: str
    deterministic_report_hash: str
    consensus_report_hash: str
    target_source: TargetSource
    target_hash: str
    editor_id: str = ""
    reason: str = ""
    protocol_version: str = REVIEW_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "review_id", require_id(self.review_id, "review.review_id"))
        # A reviewer id is an identifier, not a name: it becomes part of a persisted,
        # exportable record, and an operator's real name is personal data the dataset
        # has no reason to carry.
        setattr_(self, "reviewer", require_id(self.reviewer, "review.reviewer"))
        setattr_(self, "decision", require_enum(self.decision, DatasetReviewDecision,
                                                "review.decision"))
        setattr_(self, "target_source", require_enum(self.target_source, TargetSource,
                                                     "review.target_source"))
        setattr_(self, "timestamp", require_timestamp(self.timestamp,
                                                      "review.timestamp"))
        for name in ("task_hash", "attempt_hash", "deterministic_report_hash",
                     "consensus_report_hash", "target_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"review.{name}"))
        if self.editor_id:
            setattr_(self, "editor_id", require_id(self.editor_id, "review.editor_id"))
        if len(self.reason) > MAX_REVIEW_NOTE_CHARS:
            raise DatasetReviewError(f"review.reason: too long ({len(self.reason)} > "
                                     f"{MAX_REVIEW_NOTE_CHARS})")
        if not self.decision.approves_dataset and not self.reason.strip():
            raise DatasetReviewError(
                f"review: a {self.decision.value} decision must state a reason; an "
                f"unexplained refusal cannot be acted on and cannot be audited")
        if self.target_source.requires_editor and not self.editor_id:
            raise DatasetReviewError(
                f"review.editor_id: required for target_source="
                f"{self.target_source.value}; a human wrote or altered these bytes and "
                f"the record must say who")

    # -- the properties callers are allowed to trust -----------------------------
    @property
    def approves(self) -> bool:
        return self.decision.approves_dataset

    def review_hash(self) -> str:
        """The digest that identifies this decision. Also its single-use ledger key."""
        return sha256_obj(self.to_dict())

    def verify_bindings(self, *, task_hash: str, attempt_hash: str,
                        deterministic_report_hash: str,
                        consensus_report_hash: str) -> None:
        """Refuse this review unless it was produced for exactly THIS subject.

        All four are checked, not the first that fails, because a review lifted from a
        different episode of the same task would pass a task-hash-only check.
        """
        payload = self.to_dict()
        label = f"dataset review {self.review_id}"
        for field_name, expected in (
                ("task_hash", task_hash),
                ("attempt_hash", attempt_hash),
                ("deterministic_report_hash", deterministic_report_hash),
                ("consensus_report_hash", consensus_report_hash)):
            require_binding(payload, field=field_name, expected=expected, label=label)

    def verify_target(self, *, target_hash: str) -> None:
        """Refuse this review unless it approved these exact bytes."""
        require_binding(self.to_dict(), field="target_hash", expected=target_hash,
                        label=f"dataset review {self.review_id}")

    # -- serialization -----------------------------------------------------------
    def to_dict(self) -> dict:
        """The canonical record. Excludes ``review_hash`` — that is computed over it."""
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "protocol_version": self.protocol_version,
            "review_id": self.review_id,
            "reviewer": self.reviewer,
            "decision": self.decision.value,
            "timestamp": self.timestamp,
            "task_hash": self.task_hash,
            "attempt_hash": self.attempt_hash,
            "deterministic_report_hash": self.deterministic_report_hash,
            "consensus_report_hash": self.consensus_report_hash,
            "target_source": self.target_source.value,
            "target_hash": self.target_hash,
            "editor_id": self.editor_id,
            "reason": self.reason,
        }

    def to_record(self) -> dict:
        return {**self.to_dict(), "review_hash": self.review_hash()}

    @classmethod
    def from_dict(cls, payload: object) -> "DatasetHumanReview":
        """Rebuild a review, verifying its digest when one is present."""
        data = require_mapping(payload, "dataset review")
        reject_unknown_fields(data, _REVIEW_FIELDS, label="dataset review")
        check_schema_version(data, label="dataset review")
        review = cls(
            review_id=str(data.get("review_id", "")),
            reviewer=str(data.get("reviewer", "")),
            decision=require_enum(data.get("decision"), DatasetReviewDecision,
                                  "review.decision"),
            timestamp=str(data.get("timestamp", "")),
            task_hash=str(data.get("task_hash", "")),
            attempt_hash=str(data.get("attempt_hash", "")),
            deterministic_report_hash=str(data.get("deterministic_report_hash", "")),
            consensus_report_hash=str(data.get("consensus_report_hash", "")),
            target_source=require_enum(data.get("target_source"), TargetSource,
                                       "review.target_source"),
            target_hash=str(data.get("target_hash", "")),
            editor_id=str(data.get("editor_id", "")),
            reason=str(data.get("reason", "")),
            protocol_version=str(data.get("protocol_version",
                                          REVIEW_PROTOCOL_VERSION)),
        )
        declared = str(data.get("review_hash", "")).strip().lower()
        if declared and declared != review.review_hash():
            raise DatasetReviewError(
                f"dataset review {review.review_id}: stored digest {short(declared)!r} "
                f"does not match its content {short(review.review_hash())!r}; a review "
                f"edited after the fact is not the decision anyone made")
        return review


# ── single use, enforced on disk ──────────────────────────────────────────────
class HumanReviewLedger:
    """The append-only record of which reviews have already approved a candidate.

    A file rather than a set in memory, for the reason the teacher layer's packet ledger
    is a file: a set forgets on restart, and the consequence of forgetting here is silent
    re-approval — the one failure nobody notices, because everything keeps working.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> dict[str, dict]:
        """Every consumed review digest, mapped to the entry that consumed it."""
        try:
            lines = read_jsonl(self._path)
        except AtomicIOError as exc:
            raise DatasetReviewError(str(exc)) from None
        return {str(e.get("review_hash", "")): e for e in lines
                if str(e.get("review_hash", ""))}

    def is_consumed(self, review_hash: str) -> bool:
        return require_digest(review_hash, "ledger.review_hash") in self.entries()

    def consume(self, review: DatasetHumanReview, *, candidate_id: str) -> dict:
        """Record a first use, or raise :class:`ReviewReplayRejected` on a second.

        Keyed by the review's own digest, so the SAME decision cannot approve two
        candidates — which is how one careful approval would otherwise be spread across
        a dataset that nobody read.
        """
        digest = review.review_hash()
        existing = self.entries().get(digest)
        if existing is not None:
            raise ReviewReplayRejected(
                f"dataset review {review.review_id} ({short(digest)}) already approved "
                f"candidate {existing.get('candidate_id', 'an earlier record')!r} at "
                f"{existing.get('consumed_at_utc', 'an earlier time')}; one approval "
                f"authorises one candidate, and reusing it would let a single human "
                f"decision stand behind material nobody read")
        entry = {SCHEMA_KEY: SCHEMA_VERSION, "review_hash": digest,
                 "review_id": review.review_id, "reviewer": review.reviewer,
                 "decision": review.decision.value,
                 "candidate_id": require_id(candidate_id, "ledger.candidate_id"),
                 "consumed_at_utc": review.timestamp}
        try:
            append_jsonl(self._path, entry, max_lines=MAX_REVIEW_LEDGER_LINES)
        except AtomicIOError as exc:
            raise DatasetReviewError(str(exc)) from None
        return entry


class InMemoryHumanReviewLedger:
    """A ledger for tests and dry runs. Deliberately a separate class, not a mode.

    A store that could silently be in-memory is a store whose replay protection could
    silently be nothing.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def entries(self) -> dict[str, dict]:
        return dict(self._entries)

    def is_consumed(self, review_hash: str) -> bool:
        return require_digest(review_hash, "ledger.review_hash") in self._entries

    def consume(self, review: DatasetHumanReview, *, candidate_id: str) -> dict:
        digest = review.review_hash()
        existing = self._entries.get(digest)
        if existing is not None:
            raise ReviewReplayRejected(
                f"dataset review {review.review_id} ({short(digest)}) already approved "
                f"candidate {existing.get('candidate_id')!r}; one approval authorises "
                f"one candidate")
        entry = {SCHEMA_KEY: SCHEMA_VERSION, "review_hash": digest,
                 "review_id": review.review_id, "reviewer": review.reviewer,
                 "decision": review.decision.value,
                 "candidate_id": require_id(candidate_id, "ledger.candidate_id"),
                 "consumed_at_utc": review.timestamp}
        self._entries[digest] = entry
        return entry


__all__ = [
    "MAX_REVIEW_LEDGER_LINES", "MAX_REVIEW_NOTE_CHARS", "REVIEW_PROTOCOL_VERSION",
    "DatasetHumanReview", "DatasetReviewDecision", "DatasetReviewError",
    "HumanReviewLedger", "InMemoryHumanReviewLedger", "ReviewReplayRejected",
]
