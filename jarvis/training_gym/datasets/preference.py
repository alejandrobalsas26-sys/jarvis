"""training_gym/datasets/preference.py — V69 M62 S2d: preference pairs, not DPO.

WHY THIS IS THE MOST DANGEROUS EXPORT IN THE MILESTONE
------------------------------------------------------
An SFT row says "imitate this". A preference pair says "prefer this OVER that", and to say
it the file must CONTAIN the thing the model should not do. Every other artefact in this
subsystem is built by removing unsafe material; this one keeps a copy of it on purpose.

That inverts several assumptions at once:

  * a rejected response is retained rather than discarded, so "was it screened" becomes a
    question about the rejected side too — and the answer must be yes for BOTH sides;
  * a rejected response that is genuinely dangerous to keep (a working exploit, a real
    credential, an instruction that would be harmful if a model reproduced part of it) is
    not preference data at all. It is quarantine material, and this module refuses it;
  * approval cannot be inherited. A human approving a TARGET has not approved a PAIR.

WHAT MAY NOT STAND IN FOR A PAIR APPROVAL
-----------------------------------------
The chosen candidate's :class:`~training_gym.datasets.human_review.DatasetHumanReview`.
Teacher agreement. Membership in TRAIN. A ``preference_eligible`` flag. A caller's
boolean. Each of those answers a different question, and each of them is available at the
moment somebody wants a pair built. So :class:`PreferenceHumanReview` exists, and it binds
the task, the chosen digest AND the rejected digest — three bindings, because a pair
approval lifted onto a different rejected response would authorise text nobody read.

A PAIR IS NOT A DIFFERENCE OF OPINION
-------------------------------------
Two responses that are the same answer written differently teach a preference model
nothing and are refused: identical digests, identical normalized text, and near-duplicates
above the configured similarity threshold all fail. The rejected side must also carry
STRUCTURED reasons and deterministic evidence — "it was worse" is a claim, and a dataset
built on claims is a dataset nobody can re-derive.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not run DPO, IPO, KTO or any preference optimisation; it does not load a model,
a tokenizer or a trainer; and producing a valid pair file is NOT a claim that preference
training is production-ready. It is a claim about the file.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..atomicio import atomic_write_text
from ..schemas import (
    GYM_VERSION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    SensitivityClass,
    canonical_json,
    check_schema_version,
    reject_unknown_fields,
    require_binding,
    require_bool,
    require_enum,
    require_id,
    require_int,
    require_mapping,
    require_str_tuple,
    require_text,
    scan_private_content,
    sha256_file,
    sha256_obj,
    sha256_text,
    short,
)
from ..task_spec import TaskFamily, require_timestamp
from ..teachers.sanitization import assert_clean
from .candidate import (
    MAX_CANDIDATE_TEXT_CHARS,
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    require_digest,
)
from .export import EXPORT_DIR, export_dir
from .manifests import RevocationSnapshot
from .promotion import refuse_hidden_field_names
from .similarity import BLOCK_THRESHOLD, jaccard, normalized_key, signature

#: Bump when the pair or the review SHAPE changes.
PREFERENCE_VERSION = "m62.preference.1"
PREFERENCE_REVIEW_VERSION = "m62.preference_review.1"

#: Fixed filenames. Same reasoning as the SFT export: a caller-supplied filename is a
#: caller-supplied write destination.
PREFERENCE_FILENAME = "preference_pairs.jsonl"
PREFERENCE_MANIFEST_FILENAME = "preference_pairs.manifest.json"

#: Above this normalized similarity the two sides are the same answer and the pair is
#: refused. Deliberately the leakage layer's BLOCKING threshold: "too similar to be two
#: examples" and "too similar to be two answers" are the same measurement.
PAIR_SIMILARITY_CEILING = BLOCK_THRESHOLD

#: Splits whose material may never appear in a preference pair, in either role.
_FORBIDDEN_SPLITS: frozenset[DatasetSplit] = frozenset({
    DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
    DatasetSplit.ADVERSARIAL, DatasetSplit.QUARANTINE})


class PreferenceError(SchemaError):
    """A preference pair was refused. Never repaired, never downgraded."""


class RejectionReason(str, Enum):
    """Why the rejected side is the rejected side. Structured, so it is queryable.

    Free text is deliberately not enough: a preference dataset whose rejections are prose
    cannot be filtered, cannot be counted, and cannot be re-derived by anybody who was
    not in the room.
    """

    INCORRECT_ANSWER = "incorrect_answer"
    SCHEMA_VIOLATION = "schema_violation"
    HALLUCINATED_EVIDENCE = "hallucinated_evidence"
    INCOMPLETE = "incomplete"
    UNSAFE_ACTION = "unsafe_action"
    POLICY_VIOLATION = "policy_violation"
    REFUSED_WHEN_CAPABLE = "refused_when_capable"
    IGNORED_TOOL_CONTRACT = "ignored_tool_contract"

    @property
    def requires_safety_review(self) -> bool:
        """Reasons whose rejected text is about behaviour rather than correctness.

        These are the ones where retaining the text is itself a decision: an unsafe action
        or a policy violation, written out in full, is a demonstration.
        """
        return self in (RejectionReason.UNSAFE_ACTION, RejectionReason.POLICY_VIOLATION)


# ── the rejected side ─────────────────────────────────────────────────────────
_REJECTED_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "rejected_id", "task_hash", "attempt_hash", "text", "text_hash",
    "reasons", "detail", "evidence_refs", "teacher_evidence_refs", "source_model_id",
    "sensitivity", "provenance", "safe_to_retain", "created_at_utc")


@dataclass(frozen=True)
class RejectedResponse:
    """The half of a pair that must never be imitated, kept deliberately and screened."""

    rejected_id: str
    task_hash: str
    attempt_hash: str
    text: str
    reasons: tuple[RejectionReason, ...]
    evidence_refs: tuple[str, ...]
    source_model_id: str
    created_at_utc: str
    detail: str = ""
    teacher_evidence_refs: tuple[str, ...] = ()
    sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC
    provenance: str = ""
    safe_to_retain: bool = False

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "rejected_id", require_id(self.rejected_id,
                                                 "rejected.rejected_id"))
        for name in ("task_hash", "attempt_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"rejected.{name}"))
        require_text(self.text, "rejected.text", min_len=1,
                     max_len=MAX_CANDIDATE_TEXT_CHARS)
        setattr_(self, "sensitivity", require_enum(self.sensitivity, SensitivityClass,
                                                   "rejected.sensitivity"))
        setattr_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                           "rejected.created_at_utc"))
        setattr_(self, "safe_to_retain", require_bool(self.safe_to_retain,
                                                      "rejected.safe_to_retain"))
        setattr_(self, "reasons", tuple(sorted(
            {require_enum(r, RejectionReason, "rejected.reasons[]").value
             for r in self.reasons})))
        if not self.reasons:
            raise PreferenceError(
                "rejected.reasons: at least one structured reason is required; a "
                "rejection nobody can query is a rejection nobody can audit")
        setattr_(self, "reasons",
                 tuple(RejectionReason(value) for value in self.reasons))
        setattr_(self, "evidence_refs", tuple(sorted(set(require_str_tuple(
            self.evidence_refs, "rejected.evidence_refs", max_items=64)))))
        if not self.evidence_refs:
            raise PreferenceError(
                "rejected.evidence_refs: required; without a deterministic reference the "
                "claim that this response is worse cannot be re-derived by anyone who "
                "was not present when it was made")
        setattr_(self, "teacher_evidence_refs", tuple(sorted(set(require_str_tuple(
            self.teacher_evidence_refs, "rejected.teacher_evidence_refs",
            max_items=64)))))
        if not str(self.source_model_id or "").strip():
            raise PreferenceError("rejected.source_model_id: required")
        if not str(self.provenance or "").strip():
            raise PreferenceError(
                "rejected.provenance: required; retained material of unknown origin is "
                "exactly what must not be kept")

    @property
    def text_hash(self) -> str:
        return sha256_text(self.text)

    @property
    def needs_safety_review(self) -> bool:
        return any(reason.requires_safety_review for reason in self.reasons)

    def blockers(self) -> tuple[str, ...]:
        """Every reason this response may not be RETAINED. Empty tuple = safe to keep."""
        problems: list[str] = []
        if not self.safe_to_retain:
            problems.append(
                "safe_to_retain is false; a rejected response that is dangerous to keep "
                "is quarantine material, not the negative half of a training pair")
        if not self.sensitivity.dataset_eligible:
            problems.append(f"sensitivity {self.sensitivity.value} is never stored in a "
                            f"trainable artefact, in any role")
        found = scan_private_content(self.text)
        if found:
            problems.append(f"the retained text carries private content "
                            f"({', '.join(found)})")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: SCHEMA_VERSION, "rejected_id": self.rejected_id,
            "task_hash": self.task_hash, "attempt_hash": self.attempt_hash,
            "text": self.text, "text_hash": self.text_hash,
            "reasons": [r.value for r in self.reasons], "detail": self.detail[:1_000],
            "evidence_refs": list(self.evidence_refs),
            "teacher_evidence_refs": list(self.teacher_evidence_refs),
            "source_model_id": self.source_model_id,
            "sensitivity": self.sensitivity.value, "provenance": self.provenance,
            "safe_to_retain": self.safe_to_retain,
            "created_at_utc": self.created_at_utc,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "RejectedResponse":
        data = require_mapping(payload, "rejected response")
        reject_unknown_fields(data, _REJECTED_FIELDS, label="rejected response")
        check_schema_version(data, label="rejected response")
        response = cls(
            rejected_id=str(data.get("rejected_id", "")),
            task_hash=str(data.get("task_hash", "")),
            attempt_hash=str(data.get("attempt_hash", "")),
            text=str(data.get("text", "")),
            reasons=tuple(require_enum(r, RejectionReason, "rejected.reasons[]")
                          for r in data.get("reasons") or ()),
            evidence_refs=require_str_tuple(data.get("evidence_refs"),
                                            "rejected.evidence_refs", max_items=64),
            source_model_id=str(data.get("source_model_id", "")),
            created_at_utc=str(data.get("created_at_utc", "")),
            detail=str(data.get("detail", "")),
            teacher_evidence_refs=require_str_tuple(
                data.get("teacher_evidence_refs"), "rejected.teacher_evidence_refs",
                max_items=64),
            sensitivity=require_enum(data.get("sensitivity",
                                              SensitivityClass.SYNTHETIC.value),
                                     SensitivityClass, "rejected.sensitivity"),
            provenance=str(data.get("provenance", "")),
            safe_to_retain=data.get("safe_to_retain", False))
        declared = str(data.get("text_hash", "")).strip().lower()
        if declared and declared != response.text_hash:
            raise PreferenceError(
                f"rejected response {response.rejected_id}: text_hash does not match the "
                f"text; the rejected side has been edited since it was judged")
        return response


# ── the pair approval ─────────────────────────────────────────────────────────
class PreferenceDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def approves_pair(self) -> bool:
        return self is PreferenceDecision.APPROVED


_PAIR_REVIEW_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "protocol_version", "review_id", "reviewer", "decision", "timestamp",
    "task_hash", "chosen_hash", "rejected_hash", "reason", "review_hash")


@dataclass(frozen=True)
class PreferenceHumanReview:
    """One operator decision about one PAIR, bound to both sides and to the task.

    Three bindings rather than one. A pair approval that named only the chosen side could
    be re-filed against any rejected text; one that named only the rejected side could be
    re-filed against any target. Both are the same attack from opposite ends.
    """

    review_id: str
    reviewer: str
    decision: PreferenceDecision
    timestamp: str
    task_hash: str
    chosen_hash: str
    rejected_hash: str
    reason: str = ""
    protocol_version: str = PREFERENCE_REVIEW_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "review_id", require_id(self.review_id, "pair review.review_id"))
        setattr_(self, "reviewer", require_id(self.reviewer, "pair review.reviewer"))
        setattr_(self, "decision", require_enum(self.decision, PreferenceDecision,
                                                "pair review.decision"))
        setattr_(self, "timestamp", require_timestamp(self.timestamp,
                                                      "pair review.timestamp"))
        for name in ("task_hash", "chosen_hash", "rejected_hash"):
            setattr_(self, name, require_digest(getattr(self, name),
                                                f"pair review.{name}"))
        if self.chosen_hash == self.rejected_hash:
            raise PreferenceError(
                "pair review: the chosen and rejected digests are identical; a review "
                "that approves one text over itself approves nothing")
        if len(self.reason) > 2_000:
            raise PreferenceError("pair review.reason: too long")
        if not self.decision.approves_pair and not self.reason.strip():
            raise PreferenceError("pair review: a rejection must state a reason")

    @property
    def approves(self) -> bool:
        return self.decision.approves_pair

    def review_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def verify_bindings(self, *, task_hash: str, chosen_hash: str,
                        rejected_hash: str) -> None:
        """Refuse this approval unless it was made for exactly THIS pair."""
        payload = self.to_dict()
        label = f"preference review {self.review_id}"
        for field_name, expected in (("task_hash", task_hash),
                                     ("chosen_hash", chosen_hash),
                                     ("rejected_hash", rejected_hash)):
            require_binding(payload, field=field_name, expected=expected, label=label)

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "protocol_version": self.protocol_version,
                "review_id": self.review_id, "reviewer": self.reviewer,
                "decision": self.decision.value, "timestamp": self.timestamp,
                "task_hash": self.task_hash, "chosen_hash": self.chosen_hash,
                "rejected_hash": self.rejected_hash, "reason": self.reason}

    def to_record(self) -> dict:
        return {**self.to_dict(), "review_hash": self.review_hash()}

    @classmethod
    def from_dict(cls, payload: object) -> "PreferenceHumanReview":
        data = require_mapping(payload, "preference review")
        reject_unknown_fields(data, _PAIR_REVIEW_FIELDS, label="preference review")
        check_schema_version(data, label="preference review")
        review = cls(
            review_id=str(data.get("review_id", "")),
            reviewer=str(data.get("reviewer", "")),
            decision=require_enum(data.get("decision"), PreferenceDecision,
                                  "pair review.decision"),
            timestamp=str(data.get("timestamp", "")),
            task_hash=str(data.get("task_hash", "")),
            chosen_hash=str(data.get("chosen_hash", "")),
            rejected_hash=str(data.get("rejected_hash", "")),
            reason=str(data.get("reason", "")),
            protocol_version=str(data.get("protocol_version",
                                          PREFERENCE_REVIEW_VERSION)))
        declared = str(data.get("review_hash", "")).strip().lower()
        if declared and declared != review.review_hash():
            raise PreferenceError(
                f"preference review {review.review_id}: stored digest does not match its "
                f"content")
        return review


# ── the pair ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PreferencePair:
    """One approved (chosen, rejected) pair, bound to the review that authorised it."""

    pair_id: str
    chosen: DatasetCandidate
    rejected: RejectedResponse
    review: PreferenceHumanReview
    similarity: float
    created_at_utc: str
    dataset_version: str = ""
    source_manifest_hash: str = ""
    pair_version: str = PREFERENCE_VERSION

    @property
    def task_family(self) -> TaskFamily:
        return self.chosen.task_family

    def to_dict(self) -> dict:
        """The canonical pair. Excludes ``pair_hash`` — that is computed over it."""
        return {
            SCHEMA_KEY: SCHEMA_VERSION, "pair_version": self.pair_version,
            "pair_id": self.pair_id, "created_at_utc": self.created_at_utc,
            "task_hash": self.chosen.task_hash,
            "task_family": self.chosen.task_family.value,
            "chosen_candidate_id": self.chosen.candidate_id,
            "chosen_candidate_hash": self.chosen.candidate_hash(),
            "chosen_hash": sha256_text(self.chosen.target_text),
            "rejected_id": self.rejected.rejected_id,
            "rejected_hash": self.rejected.text_hash,
            "rejection_reasons": [r.value for r in self.rejected.reasons],
            "evidence_refs": list(self.rejected.evidence_refs),
            "teacher_evidence_refs": list(self.rejected.teacher_evidence_refs),
            "review_hash": self.review.review_hash(),
            "reviewer": self.review.reviewer,
            "similarity": round(self.similarity, 6),
            "dataset_version": self.dataset_version,
            "source_manifest_hash": self.source_manifest_hash,
        }

    def pair_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_row(self) -> dict:
        """The exported row. Prompt, both answers, and the provenance to trace them."""
        messages: list[dict] = []
        if self.chosen.system_message.strip():
            messages.append({"role": "system", "content": self.chosen.system_message})
        messages.append({"role": "user", "content": self.chosen.user_prompt})
        return {"prompt": {"messages": messages},
                "chosen": self.chosen.target_text,
                "rejected": self.rejected.text,
                "metadata": {**self.to_dict(), "pair_hash": self.pair_hash()}}


def pair_blockers(*, chosen: DatasetCandidate, rejected: RejectedResponse,
                  review: PreferenceHumanReview, split: DatasetSplit,
                  revocation: RevocationSnapshot,
                  similarity_ceiling: float = PAIR_SIMILARITY_CEILING
                  ) -> tuple[str, ...]:
    """Every reason this may not become a preference pair. Empty tuple = eligible.

    Every check is re-derived here rather than inherited from whatever produced the two
    sides, for the same reason the candidate gates are re-derived: each of these has a
    caller upstream that believes it already ran.
    """
    problems: list[str] = []

    # -- the chosen side is genuinely approved training material ----------------
    if chosen.state is not CandidateState.PROMOTED:
        problems.append(f"the chosen side is {chosen.state.value}; only a promoted "
                        f"candidate is an approved answer")
    if revocation.is_revoked(chosen):
        problems.append("the chosen side has been revoked")
    if chosen.evaluation_only or not chosen.dataset_eligible:
        problems.append("the chosen side is evaluation-only or dataset-ineligible")
    if not chosen.sensitivity.dataset_eligible:
        problems.append(f"the chosen side's sensitivity {chosen.sensitivity.value} is "
                        f"never stored in a trainable artefact")
    if split in _FORBIDDEN_SPLITS:
        problems.append(
            f"the material is in {split.value}; held-out and quarantined records may "
            f"never appear in a preference pair, in either role — a pair file carries "
            f"the expected answer in plain text")

    # -- the two sides are actually two answers to one question -----------------
    if chosen.task_hash != rejected.task_hash:
        problems.append("the two sides answer different tasks; a preference pair whose "
                        "halves are not comparable teaches an arbitrary ranking")
    chosen_hash = sha256_text(chosen.target_text)
    if chosen_hash == rejected.text_hash:
        problems.append("both sides are byte-identical")
    elif (normalized_key(chosen.target_text, family=chosen.task_family)
            == normalized_key(rejected.text, family=chosen.task_family)):
        problems.append("both sides normalize to the same text; the difference is "
                        "whitespace or casing, not an answer")
    else:
        score = pair_similarity(chosen.target_text, rejected.text,
                                family=chosen.task_family)
        if score >= similarity_ceiling:
            problems.append(
                f"the two sides are {score:.3f} similar, at or above the "
                f"{similarity_ceiling:.3f} ceiling; near-identical halves teach a "
                f"preference model to discriminate on noise")

    # -- the rejected side is safe to keep --------------------------------------
    problems.extend(f"the rejected side: {reason}" for reason in rejected.blockers())
    found = scan_private_content(chosen.target_text)
    if found:
        problems.append(f"the chosen side carries private content ({', '.join(found)})")

    # -- the pair itself was approved, by a human, for this exact pair ----------
    if not review.approves:
        problems.append(f"the pair review decision is {review.decision.value}")
    try:
        review.verify_bindings(task_hash=chosen.task_hash, chosen_hash=chosen_hash,
                               rejected_hash=rejected.text_hash)
    except SchemaError as exc:
        problems.append(f"the pair approval is not for this pair: {exc}")
    return tuple(problems)


def pair_similarity(left: str, right: str, *, family: TaskFamily | None = None) -> float:
    """The stronger of the character and token views, as the leakage layer computes it."""
    a = signature("left", left, family=family)
    b = signature("right", right, family=family)
    return max(jaccard(a.ngrams, b.ngrams), jaccard(a.shingles, b.shingles))


def build_preference_pair(*, pair_id: str, chosen: DatasetCandidate,
                          rejected: RejectedResponse, review: PreferenceHumanReview,
                          split: DatasetSplit, revocation: RevocationSnapshot,
                          created_at_utc: str, dataset_version: str = "",
                          source_manifest_hash: str = "",
                          similarity_ceiling: float = PAIR_SIMILARITY_CEILING
                          ) -> PreferencePair:
    """Build one pair, or raise with every blocker at once."""
    problems = pair_blockers(chosen=chosen, rejected=rejected, review=review,
                             split=split, revocation=revocation,
                             similarity_ceiling=similarity_ceiling)
    if problems:
        raise PreferenceError(f"preference pair {pair_id}: refusing to create — "
                              + "; ".join(problems))
    pair = PreferencePair(
        pair_id=require_id(pair_id, "pair.pair_id"), chosen=chosen, rejected=rejected,
        review=review,
        similarity=pair_similarity(chosen.target_text, rejected.text,
                                   family=chosen.task_family),
        created_at_utc=require_timestamp(created_at_utc, "pair.created_at_utc"),
        dataset_version=dataset_version, source_manifest_hash=source_manifest_hash)
    row = pair.to_row()
    refuse_hidden_field_names(row, label=f"preference pair {pair_id}")
    assert_clean(row, label=f"preference pair {pair_id}")
    return pair


# ── the export ────────────────────────────────────────────────────────────────
_PREFERENCE_MANIFEST_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "gym_version", "preference_version", "dataset_id", "dataset_version",
    "dataset_manifest_hash", "filename", "sha256_file", "size_bytes", "pair_count",
    "created_at_utc", "task_family_distribution", "rejection_reason_distribution",
    "pair_hashes_hash", "export_hash")


@dataclass(frozen=True)
class PreferenceExportManifest:
    """What the pair file is, and which dataset version its chosen sides came from."""

    dataset_id: str
    dataset_version: str
    dataset_manifest_hash: str
    filename: str
    sha256_file: str
    size_bytes: int
    pair_count: int
    created_at_utc: str
    pair_hashes_hash: str
    task_family_distribution: dict = None  # type: ignore[assignment]
    rejection_reason_distribution: dict = None  # type: ignore[assignment]
    preference_version: str = PREFERENCE_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "dataset_id", require_id(self.dataset_id, "pref.dataset_id"))
        setattr_(self, "dataset_version", require_id(self.dataset_version,
                                                     "pref.dataset_version"))
        for name in ("dataset_manifest_hash", "sha256_file", "pair_hashes_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"pref.{name}"))
        setattr_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                           "pref.created_at_utc"))
        setattr_(self, "size_bytes", require_int(self.size_bytes, "pref.size_bytes",
                                                 minimum=1, maximum=1 << 40))
        setattr_(self, "pair_count", require_int(self.pair_count, "pref.pair_count",
                                                 minimum=1, maximum=1_000_000))
        if self.filename != PREFERENCE_FILENAME:
            raise PreferenceError(f"pref.filename: {self.filename!r} is not the one legal "
                                  f"name ({PREFERENCE_FILENAME!r})")
        for name in ("task_family_distribution", "rejection_reason_distribution"):
            setattr_(self, name, dict(sorted((getattr(self, name) or {}).items())))

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "gym_version": GYM_VERSION,
                "preference_version": self.preference_version,
                "dataset_id": self.dataset_id, "dataset_version": self.dataset_version,
                "dataset_manifest_hash": self.dataset_manifest_hash,
                "filename": self.filename, "sha256_file": self.sha256_file,
                "size_bytes": self.size_bytes, "pair_count": self.pair_count,
                "created_at_utc": self.created_at_utc,
                "task_family_distribution": dict(self.task_family_distribution),
                "rejection_reason_distribution":
                    dict(self.rejection_reason_distribution),
                "pair_hashes_hash": self.pair_hashes_hash}

    def to_record(self) -> dict:
        return {**self.to_dict(), "export_hash": self.export_hash()}

    def export_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "PreferenceExportManifest":
        data = require_mapping(payload, "preference manifest")
        reject_unknown_fields(data, _PREFERENCE_MANIFEST_FIELDS,
                              label="preference manifest")
        check_schema_version(data, label="preference manifest")
        declared = str(data.get("export_hash", "")).strip()
        if not declared:
            raise PreferenceError("preference manifest: missing export_hash")
        manifest = cls(
            dataset_id=str(data.get("dataset_id", "")),
            dataset_version=str(data.get("dataset_version", "")),
            dataset_manifest_hash=str(data.get("dataset_manifest_hash", "")),
            filename=str(data.get("filename", "")),
            sha256_file=str(data.get("sha256_file", "")),
            size_bytes=data.get("size_bytes", 0),
            pair_count=data.get("pair_count", 0),
            created_at_utc=str(data.get("created_at_utc", "")),
            pair_hashes_hash=str(data.get("pair_hashes_hash", "")),
            task_family_distribution=data.get("task_family_distribution") or {},
            rejection_reason_distribution=data.get("rejection_reason_distribution") or {},
            preference_version=str(data.get("preference_version", PREFERENCE_VERSION)))
        if require_digest(declared, "pref.export_hash") != manifest.export_hash():
            raise PreferenceError(
                f"preference manifest: stored digest {short(declared)!r} does not match "
                f"its content {short(manifest.export_hash())!r}")
        return manifest


@dataclass(frozen=True)
class PreferenceExport:
    manifest: PreferenceExportManifest
    relative_paths: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"pair_count": self.manifest.pair_count,
                "sha256_file": self.manifest.sha256_file,
                "export_hash": self.manifest.export_hash(),
                "dataset_id": self.manifest.dataset_id,
                "dataset_version": self.manifest.dataset_version,
                "relative_paths": list(self.relative_paths),
                "runs_dpo": False}


def _distribution(values: Sequence[str]) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def export_preference(*, pairs: Sequence[PreferencePair], root: str | Path,
                      dataset_id: str, dataset_version: str,
                      dataset_manifest_hash: str, created_at_utc: str,
                      out_root: str | Path | None = None) -> PreferenceExport:
    """Write the preference-pair file for one dataset version.

    Writing this file is not preference training and is not a claim that preference
    training is ready. It is a claim that these pairs were each approved by a human who
    saw both sides.
    """
    if not pairs:
        raise PreferenceError(
            "preference export: no approved pairs; refusing to write an empty file "
            "rather than producing an artefact that verifies and contains nothing")
    ordered = sorted(pairs, key=lambda p: p.pair_id)
    seen: set[str] = set()
    for pair in ordered:
        if pair.pair_id in seen:
            raise PreferenceError(f"preference export: pair {pair.pair_id!r} appears "
                                  f"twice")
        seen.add(pair.pair_id)

    rows = [pair.to_row() for pair in ordered]
    for pair, row in zip(ordered, rows, strict=True):
        refuse_hidden_field_names(row, label=f"preference row {pair.pair_id}")
        assert_clean(row, label=f"preference row {pair.pair_id}")
    text = "".join(f"{canonical_json(row)}\n" for row in rows)

    destination = export_dir(out_root if out_root is not None else root, dataset_id,
                             dataset_version)
    path = destination / PREFERENCE_FILENAME
    if path.exists() or path.is_symlink():
        raise PreferenceError(
            f"preference export: {PREFERENCE_FILENAME} already exists for "
            f"{dataset_id}/{dataset_version}; an export is as immutable as its source")
    atomic_write_text(path, text)

    manifest = PreferenceExportManifest(
        dataset_id=dataset_id, dataset_version=dataset_version,
        dataset_manifest_hash=dataset_manifest_hash, filename=PREFERENCE_FILENAME,
        sha256_file=sha256_file(path), size_bytes=path.stat().st_size,
        pair_count=len(ordered), created_at_utc=created_at_utc,
        pair_hashes_hash=sha256_obj([p.pair_hash() for p in ordered]),
        task_family_distribution=_distribution([p.task_family.value for p in ordered]),
        rejection_reason_distribution=_distribution(
            [r.value for p in ordered for r in p.rejected.reasons]))
    atomic_write_text(destination / PREFERENCE_MANIFEST_FILENAME,
                      canonical_json(manifest.to_record()))
    return PreferenceExport(manifest=manifest, relative_paths=(
        f"{EXPORT_DIR}/{dataset_id}/{dataset_version}/{PREFERENCE_FILENAME}",
        f"{EXPORT_DIR}/{dataset_id}/{dataset_version}/{PREFERENCE_MANIFEST_FILENAME}"))


def verify_preference_export(*, out_root: str | Path, dataset_id: str,
                             dataset_version: str) -> tuple[str, ...]:
    """Re-hash a written pair file against its manifest. Empty tuple = intact."""
    directory = export_dir(out_root, dataset_id, dataset_version)
    manifest_path = directory / PREFERENCE_MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return ("the preference manifest is missing",)
    import json
    try:
        manifest = PreferenceExportManifest.from_dict(
            json.loads(manifest_path.read_text(encoding="utf-8")))
    except (SchemaError, json.JSONDecodeError, ValueError, OSError) as exc:
        return (f"the preference manifest is unusable ({exc})",)
    path = directory / manifest.filename
    if path.is_symlink():
        return (f"{manifest.filename}: is a symlink",)
    if not path.is_file():
        return (f"{manifest.filename}: recorded but missing",)
    problems: list[str] = []
    if path.stat().st_size != manifest.size_bytes:
        problems.append(f"{manifest.filename}: size does not match the manifest")
    actual = sha256_file(path)
    if actual != manifest.sha256_file:
        problems.append(f"{manifest.filename}: content digest {short(actual)} does not "
                        f"match the manifest's {short(manifest.sha256_file)}")
    else:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) != manifest.pair_count:
            problems.append(f"{manifest.filename}: holds {len(lines)} pairs, the manifest "
                            f"says {manifest.pair_count}")
    return tuple(problems)


__all__ = [
    "PAIR_SIMILARITY_CEILING", "PREFERENCE_FILENAME", "PREFERENCE_MANIFEST_FILENAME",
    "PREFERENCE_REVIEW_VERSION", "PREFERENCE_VERSION", "PreferenceDecision",
    "PreferenceError", "PreferenceExport", "PreferenceExportManifest",
    "PreferenceHumanReview", "PreferencePair", "RejectedResponse", "RejectionReason",
    "build_preference_pair", "export_preference", "pair_blockers", "pair_similarity",
    "verify_preference_export",
]
