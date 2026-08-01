"""training_gym/datasets/candidate.py — V69 M62 S2d: what a training example IS.

WHY THIS EXISTS
---------------
Every earlier stage of the milestone produced a *verdict about an attempt*. This module
produces the first object that is a *proposed piece of training data*, and that change of
kind is where the milestone's central claim has to be re-proved rather than inherited:

    no conversation, no grader PASS, no teacher approval and no caller's boolean ever
    becomes training data. A human decision, bound by hash to the exact bytes, does.

So a :class:`DatasetCandidate` carries SIX digests — task, attempt, deterministic report,
consensus report, human review, and source episode — and its own digest covers all of
them. Lift any one of those records off a different subject and the binding check fails;
edit any byte of the target and the candidate hash changes and the human review no longer
matches it. That is the whole mechanism, and it is deliberately boring.

THE TARGET IS THE DANGEROUS FIELD
---------------------------------
:class:`TargetSource` exists because "where did the ideal answer come from" is the
question a dataset silently answers wrong. A teacher's ``corrected_answer`` is a cloud
model's opinion; promoting it to *the target* would train the student to imitate the
teacher on exactly the cases the student got wrong, with no human ever having read it.
So a target must name an authorised source, and every source except
``VERIFIED_STUDENT_OUTPUT`` requires a human editor id — and even that one requires the
human to have approved the exact, unmodified bytes.

WHAT IS NEVER IN A CANDIDATE
----------------------------
Credentials, environment dumps, host identity, absolute paths, hidden reasoning, raw
provider responses, held-out expected answers, or unrelated trajectory content. The text
fields arrive pre-sanitized and are re-scanned here; see :mod:`training_gym.datasets.
promotion` for the construction path that does the sanitizing.

FAIL CLOSED, ALWAYS
-------------------
An unknown field, an unknown enum value, a missing digest, an empty target or a target
whose hash does not match the human's approval all raise :class:`CandidateError`. None of
them is recoverable by a default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..schemas import (
    GYM_VERSION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    SensitivityClass,
    check_schema_version,
    reject_unknown_fields,
    require_bool,
    require_enum,
    require_id,
    require_mapping,
    require_str_tuple,
    require_text,
    sha256_obj,
    sha256_text,
    short,
)
from ..task_spec import TaskFamily, require_timestamp

#: Bump when the candidate SHAPE changes in a way a reader cannot tolerate.
CANDIDATE_VERSION = "m62.candidate.1"

#: Bound on any single text field. Matches the teacher layer's sanitization ceiling, so
#: a candidate cannot carry more material than the packet the reviewer actually saw.
MAX_CANDIDATE_TEXT_CHARS = 20_000

#: Bound on the structured tool-call examples one candidate may carry.
MAX_CANDIDATE_TOOL_CALLS = 32

#: Bound on the fixture / artifact digest lists a candidate records for leakage analysis.
MAX_CANDIDATE_HASHES = 128


class CandidateError(SchemaError):
    """A candidate was refused. Never downgraded to a warning, never coerced."""


# ── the authorised sources of an ideal target ─────────────────────────────────
class TargetSource(str, Enum):
    """Where the ideal assistant output came from. There is no fifth option.

    The members are ordered by how much of the target a human personally vouched for,
    and every one of them requires a human decision naming the target's exact digest.
    """

    #: Written by the reviewer from scratch and approved.
    HUMAN_AUTHORED = "human_authored"
    #: The reviewer changed the student's output and approved the exact final bytes.
    HUMAN_EDITED = "human_edited"
    #: The student's own output, deterministically graded, approved UNCHANGED.
    VERIFIED_STUDENT_OUTPUT = "verified_student_output"
    #: A teacher's ``corrected_answer``, shown separately to a human who approved it.
    TEACHER_PROPOSED_HUMAN_APPROVED = "teacher_proposed_human_approved"

    @property
    def requires_editor(self) -> bool:
        """Sources where a named human touched or authored the bytes.

        ``VERIFIED_STUDENT_OUTPUT`` is excluded because nobody edited anything — the
        reviewer's job there was to approve, not to write, and inventing an "editor" for
        it would make the field meaningless everywhere else.
        """
        return self is not TargetSource.VERIFIED_STUDENT_OUTPUT

    @property
    def derived_from_teacher(self) -> bool:
        """True when a cloud model's text reached the target, however indirectly.

        Recorded so a dataset report can state how much of it a teacher wrote. A high
        share is not an error, but it is a fact an operator must be able to see.
        """
        return self is TargetSource.TEACHER_PROPOSED_HUMAN_APPROVED


# ── where the material itself came from ───────────────────────────────────────
class SourceType(str, Enum):
    """The lineage class of the material, for provenance and leakage grouping."""

    GYM_EPISODE = "gym_episode"            # a graded practice attempt
    HUMAN_CURATED = "human_curated"        # written by the operator, not run
    GENERATED_VARIANT = "generated_variant"  # derived from a parent task
    IMPORTED_FIXTURE = "imported_fixture"  # built around third-party material

    @property
    def requires_parent(self) -> bool:
        """A variant with no parent recorded has laundered its own lineage."""
        return self is SourceType.GENERATED_VARIANT


# ── splits ────────────────────────────────────────────────────────────────────
class DatasetSplit(str, Enum):
    """The six logical destinations. A candidate belongs to exactly one."""

    TRAIN = "train"
    VALIDATION = "validation"
    HIDDEN_EVALUATION = "hidden_evaluation"
    SECURITY_REGRESSION = "security_regression"
    ADVERSARIAL = "adversarial"
    QUARANTINE = "quarantine"

    @property
    def trainable(self) -> bool:
        """Only ``TRAIN`` is optimised against. Everything else measures.

        ``VALIDATION`` is excluded deliberately: it is read DURING training to decide
        when to stop, and a split you steer on is a split you have partly fitted.
        """
        return self is DatasetSplit.TRAIN

    @property
    def held_out(self) -> bool:
        """Splits whose expected targets must never reach training or a teacher."""
        return self in (DatasetSplit.HIDDEN_EVALUATION,
                        DatasetSplit.SECURITY_REGRESSION,
                        DatasetSplit.ADVERSARIAL)

    @property
    def exportable_as_sft(self) -> bool:
        return self is DatasetSplit.TRAIN


#: Splits that may never appear in any export, of any kind.
NON_EXPORTABLE_SPLITS: frozenset[DatasetSplit] = frozenset({DatasetSplit.QUARANTINE})


# ── the candidate state machine ───────────────────────────────────────────────
class CandidateState(str, Enum):
    """Where a candidate is in the promotion pipeline. Twelve states, one path in."""

    CREATED = "created"
    VALIDATED = "validated"
    PRIVACY_CHECKED = "privacy_checked"
    PROVENANCE_CHECKED = "provenance_checked"
    LEAKAGE_CHECKED = "leakage_checked"
    READY_FOR_PROMOTION = "ready_for_promotion"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    REVOKED = "revoked"

    @property
    def promotable(self) -> bool:
        return self is CandidateState.READY_FOR_PROMOTION

    @property
    def in_a_dataset(self) -> bool:
        """States whose records are counted as part of a released dataset version.

        ``REVOKED`` is included on purpose: a revoked record was promoted, the version
        that contains it stays immutable, and pretending otherwise would make an old
        manifest unverifiable. Exclusion happens in FUTURE versions, not in the past.
        """
        return self in (CandidateState.PROMOTED, CandidateState.REVOKED)

    @property
    def exportable(self) -> bool:
        """Only a promoted candidate may be written into an export of any kind."""
        return self is CandidateState.PROMOTED


#: The complete legal graph. Anything not listed here is refused.
#:
#: ``READY_FOR_PROMOTION`` has exactly one predecessor, ``LEAKAGE_CHECKED``, which in
#: turn has exactly one — so there is no ordering of calls that reaches promotion without
#: privacy, provenance and leakage each having actually run. ``REJECTED``,
#: ``QUARANTINED`` and ``FAILED`` never lead back into the pipeline: a candidate that
#: failed a gate is re-derived from its episode, not nursed forward.
ALLOWED_CANDIDATE_TRANSITIONS: dict[CandidateState, frozenset[CandidateState]] = {
    CandidateState.CREATED: frozenset({
        CandidateState.VALIDATED, CandidateState.REJECTED,
        CandidateState.NEEDS_REVISION, CandidateState.QUARANTINED,
        CandidateState.FAILED}),
    CandidateState.VALIDATED: frozenset({
        CandidateState.PRIVACY_CHECKED, CandidateState.REJECTED,
        CandidateState.NEEDS_REVISION, CandidateState.QUARANTINED,
        CandidateState.FAILED}),
    CandidateState.PRIVACY_CHECKED: frozenset({
        CandidateState.PROVENANCE_CHECKED, CandidateState.REJECTED,
        CandidateState.NEEDS_REVISION, CandidateState.QUARANTINED,
        CandidateState.FAILED}),
    CandidateState.PROVENANCE_CHECKED: frozenset({
        CandidateState.LEAKAGE_CHECKED, CandidateState.REJECTED,
        CandidateState.NEEDS_REVISION, CandidateState.QUARANTINED,
        CandidateState.FAILED}),
    CandidateState.LEAKAGE_CHECKED: frozenset({
        CandidateState.READY_FOR_PROMOTION, CandidateState.REJECTED,
        CandidateState.NEEDS_REVISION, CandidateState.QUARANTINED,
        CandidateState.FAILED}),
    CandidateState.READY_FOR_PROMOTION: frozenset({
        CandidateState.PROMOTED, CandidateState.REJECTED,
        CandidateState.NEEDS_REVISION, CandidateState.QUARANTINED,
        CandidateState.FAILED}),
    # A promoted record may only ever be revoked. It is never edited, never un-promoted,
    # and never deleted — the version that contains it must stay verifiable forever.
    CandidateState.PROMOTED: frozenset({CandidateState.REVOKED}),
    CandidateState.REJECTED: frozenset(),
    CandidateState.NEEDS_REVISION: frozenset(),
    CandidateState.QUARANTINED: frozenset(),
    CandidateState.FAILED: frozenset(),
    CandidateState.REVOKED: frozenset(),
}


class CandidateTransitionError(CandidateError):
    """An illegal candidate state change was attempted."""


def check_candidate_transition(current: CandidateState,
                               target: CandidateState) -> CandidateState:
    """Return *target* if the move is legal, or raise. The only gate on state changes."""
    now = require_enum(current, CandidateState, "candidate.state")
    want = require_enum(target, CandidateState, "candidate.transition")
    allowed = ALLOWED_CANDIDATE_TRANSITIONS.get(now, frozenset())
    if want not in allowed:
        raise CandidateTransitionError(
            f"candidate: {now.value} -> {want.value} is not a legal transition "
            f"(legal: {sorted(s.value for s in allowed) or 'none — terminal'})")
    return want


# ── the candidate ─────────────────────────────────────────────────────────────
_CANDIDATE_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "gym_version", "candidate_version", "candidate_id", "task_id",
    "task_family", "task_hash", "attempt_id", "attempt_hash",
    "deterministic_report_hash", "consensus_report_hash", "human_review_hash",
    "source_episode_hash", "student_model_id", "student_model_role",
    "prompt_template_hash", "system_prompt_version", "system_message", "user_prompt",
    "target_text", "tool_calls", "input_fixture_hashes", "artifact_hashes",
    "provenance", "sensitivity", "source_type", "lineage_group", "parent_task_hash",
    "state", "dataset_eligible", "preference_eligible", "evaluation_only",
    "target_source", "target_hash", "student_output_changed", "editor_id",
    "approval_hash", "created_at_utc", "candidate_hash",
)

#: Provenance keys a candidate carries. Fixed, so a caller cannot smuggle a free-form
#: dictionary — including one holding a path or a credential — through this field.
#:
#: ``usage_terms`` rather than the obvious ``license``: ``LICENSE`` is in the repository's
#: :data:`~training_gym.policies._CREDENTIAL_TOKENS` set (because ``LICENSE_KEY`` env vars
#: carry credentials), so :func:`~training_gym.teachers.sanitization.hidden_key` matches
#: it and any exporter running ``sanitize_structure`` over a candidate would blank the
#: value. Renaming the field is the honest fix; exempting it from the guard would leave a
#: hole the guard exists to close.
_PROVENANCE_FIELDS: tuple[str, ...] = ("source", "author", "usage_terms",
                                       "upstream_ref")


def require_digest(value: object, field_name: str) -> str:
    """A 64-character lowercase hex digest, or raise.

    Refuses the empty string explicitly: a binding field left blank is the one input
    that would make :func:`~training_gym.schemas.require_binding` compare nothing
    against nothing.
    """
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise CandidateError(f"{field_name}: expected a 64-character hex digest, got "
                             f"{short(text) or '<empty>'!r}; an unbound candidate can "
                             f"be attributed to any attempt")
    return text


@dataclass(frozen=True)
class DatasetCandidate:
    """One proposed training example, bound by hash to everything that authorised it."""

    candidate_id: str
    task_id: str
    task_family: TaskFamily
    task_hash: str
    attempt_id: str
    attempt_hash: str
    deterministic_report_hash: str
    consensus_report_hash: str
    human_review_hash: str
    source_episode_hash: str
    student_model_id: str
    system_message: str
    user_prompt: str
    target_text: str
    target_source: TargetSource
    target_hash: str
    approval_hash: str
    created_at_utc: str
    student_model_role: str = "student"
    prompt_template_hash: str = ""
    system_prompt_version: str = ""
    tool_calls: tuple[dict, ...] = ()
    input_fixture_hashes: tuple[str, ...] = ()
    artifact_hashes: tuple[str, ...] = ()
    provenance: dict = field(default_factory=dict)
    sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC
    source_type: SourceType = SourceType.GYM_EPISODE
    lineage_group: str = ""
    parent_task_hash: str = ""
    state: CandidateState = CandidateState.CREATED
    dataset_eligible: bool = True
    preference_eligible: bool = False
    evaluation_only: bool = False
    student_output_changed: bool = False
    editor_id: str = ""
    candidate_version: str = CANDIDATE_VERSION

    # -- validation -------------------------------------------------------------
    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "candidate_id", require_id(self.candidate_id,
                                                  "candidate.candidate_id"))
        setattr_(self, "task_id", require_id(self.task_id, "candidate.task_id"))
        setattr_(self, "task_family", require_enum(self.task_family, TaskFamily,
                                                   "candidate.task_family"))
        setattr_(self, "sensitivity", require_enum(self.sensitivity, SensitivityClass,
                                                   "candidate.sensitivity"))
        setattr_(self, "source_type", require_enum(self.source_type, SourceType,
                                                   "candidate.source_type"))
        setattr_(self, "target_source", require_enum(self.target_source, TargetSource,
                                                     "candidate.target_source"))
        setattr_(self, "state", require_enum(self.state, CandidateState,
                                             "candidate.state"))
        setattr_(self, "created_at_utc",
                 require_timestamp(self.created_at_utc, "candidate.created_at_utc"))
        for name in ("task_hash", "attempt_hash", "deterministic_report_hash",
                     "consensus_report_hash", "human_review_hash",
                     "source_episode_hash", "target_hash", "approval_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"candidate.{name}"))
        if self.parent_task_hash:
            setattr_(self, "parent_task_hash",
                     require_digest(self.parent_task_hash, "candidate.parent_task_hash"))

        self._validate_identity()
        self._validate_text()
        self._validate_target()
        self._validate_lineage()
        self._validate_eligibility()

    def _validate_identity(self) -> None:
        if not str(self.attempt_id or "").strip():
            raise CandidateError("candidate.attempt_id: required (which attempt this "
                                 "example was derived from)")
        if not str(self.student_model_id or "").strip():
            raise CandidateError("candidate.student_model_id: required (a resolved "
                                 "identifier; an example nobody can attribute to a "
                                 "model is not reproducible)")
        if self.student_model_role != "student":
            raise CandidateError(
                f"candidate.student_model_role: expected 'student', got "
                f"{self.student_model_role!r}; a teacher's or verifier's output is not "
                f"a student attempt and may not be relabelled as one")
        object.__setattr__(self, "provenance", _clean_provenance(self.provenance))

    def _validate_text(self) -> None:
        require_text(self.user_prompt, "candidate.user_prompt", min_len=1,
                     max_len=MAX_CANDIDATE_TEXT_CHARS)
        require_text(self.target_text, "candidate.target_text", min_len=1,
                     max_len=MAX_CANDIDATE_TEXT_CHARS)
        if len(self.system_message) > MAX_CANDIDATE_TEXT_CHARS:
            raise CandidateError(f"candidate.system_message: too long "
                                 f"({len(self.system_message)} chars)")
        if len(self.tool_calls) > MAX_CANDIDATE_TOOL_CALLS:
            raise CandidateError(f"candidate.tool_calls: {len(self.tool_calls)} exceeds "
                                 f"the {MAX_CANDIDATE_TOOL_CALLS} ceiling")
        object.__setattr__(self, "tool_calls",
                           tuple(_clean_tool_call(c, i)
                                 for i, c in enumerate(self.tool_calls)))
        for name in ("input_fixture_hashes", "artifact_hashes"):
            values = require_str_tuple(getattr(self, name), f"candidate.{name}",
                                       max_items=MAX_CANDIDATE_HASHES)
            object.__setattr__(self, name, tuple(sorted(
                require_digest(v, f"candidate.{name}[]") for v in values)))

    def _validate_target(self) -> None:
        """The target must be the exact bytes a human approved, from a named source."""
        if sha256_text(self.target_text) != self.target_hash:
            raise CandidateError(
                "candidate.target_hash: does not match target_text. The human approved "
                "specific bytes; a target that has changed since is a different answer "
                "nobody signed off on")
        if self.target_source.requires_editor and not str(self.editor_id or "").strip():
            raise CandidateError(
                f"candidate.editor_id: required for target_source="
                f"{self.target_source.value} (someone personally wrote or altered these "
                f"bytes, and the record must say who)")
        if self.editor_id:
            object.__setattr__(self, "editor_id", require_id(self.editor_id,
                                                             "candidate.editor_id"))
        if (self.target_source is TargetSource.VERIFIED_STUDENT_OUTPUT
                and self.student_output_changed):
            raise CandidateError(
                "candidate: target_source=verified_student_output claims the student's "
                "own output was approved unchanged, but student_output_changed is true; "
                "that combination is HUMAN_EDITED and must be recorded as such")
        if (self.target_source is TargetSource.HUMAN_EDITED
                and not self.student_output_changed):
            raise CandidateError(
                "candidate: target_source=human_edited but student_output_changed is "
                "false; an edit that changed nothing is verified_student_output")

    def _validate_lineage(self) -> None:
        if self.source_type.requires_parent and not self.parent_task_hash:
            raise CandidateError(
                "candidate.parent_task_hash: required for a generated_variant; a "
                "variant with no recorded parent can be split away from the task it was "
                "derived from, which is leakage the manifest would call clean")
        group = str(self.lineage_group or "").strip()
        if not group:
            raise CandidateError(
                "candidate.lineage_group: required (the unit a split moves as a whole; "
                "an empty group makes every record independently splittable and defeats "
                "lineage isolation)")
        object.__setattr__(self, "lineage_group",
                           require_id(group, "candidate.lineage_group"))

    def _validate_eligibility(self) -> None:
        if self.evaluation_only and self.dataset_eligible:
            raise CandidateError(
                "candidate: evaluation_only tasks are never dataset_eligible; a record "
                "claiming both would be trainable held-out material")
        if not self.sensitivity.dataset_eligible and self.dataset_eligible:
            raise CandidateError(
                f"candidate: sensitivity {self.sensitivity.value} is never trainable, "
                f"but dataset_eligible is true")
        if self.preference_eligible and not self.dataset_eligible:
            raise CandidateError(
                "candidate: preference_eligible requires dataset_eligible; material "
                "barred from training is barred from preference data too")

    # -- identity ---------------------------------------------------------------
    def candidate_hash(self) -> str:
        """The digest over every security-relevant field, including the six bindings.

        Computed from :meth:`to_dict`, which omits only the digest itself — so any edit
        to any field, including a state change, produces a different candidate.
        """
        return sha256_obj(self.to_dict())

    def content_key(self) -> str:
        """The dedup key: what was asked and what the ideal answer is.

        Deliberately EXCLUDES the bindings and the state. Two attempts at the same task
        that produced the same approved answer are one training example, however
        differently they were reached — and a dedup key that included the attempt hash
        would call them two.
        """
        return sha256_obj({"system_message": self.system_message,
                           "user_prompt": self.user_prompt,
                           "target_text": self.target_text})

    # -- transitions ------------------------------------------------------------
    def with_state(self, target: CandidateState) -> "DatasetCandidate":
        """A copy in *target* state, or raise. Candidates are frozen; this is the move.

        Returning a new object rather than mutating is what makes the transition ledger
        in :mod:`training_gym.datasets.candidate_store` meaningful: the old candidate
        hash still names the old record, so an audit reads as a chain and not as an
        object that quietly became something else.
        """
        from dataclasses import replace
        return replace(self, state=check_candidate_transition(self.state, target))

    # -- serialization ----------------------------------------------------------
    def to_dict(self) -> dict:
        """The canonical record. Excludes ``candidate_hash`` — that is computed over it."""
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "gym_version": GYM_VERSION,
            "candidate_version": self.candidate_version,
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "task_family": self.task_family.value,
            "task_hash": self.task_hash,
            "attempt_id": self.attempt_id,
            "attempt_hash": self.attempt_hash,
            "deterministic_report_hash": self.deterministic_report_hash,
            "consensus_report_hash": self.consensus_report_hash,
            "human_review_hash": self.human_review_hash,
            "source_episode_hash": self.source_episode_hash,
            "student_model_id": self.student_model_id,
            "student_model_role": self.student_model_role,
            "prompt_template_hash": self.prompt_template_hash,
            "system_prompt_version": self.system_prompt_version,
            "system_message": self.system_message,
            "user_prompt": self.user_prompt,
            "target_text": self.target_text,
            "tool_calls": [dict(c) for c in self.tool_calls],
            "input_fixture_hashes": list(self.input_fixture_hashes),
            "artifact_hashes": list(self.artifact_hashes),
            "provenance": dict(sorted(self.provenance.items())),
            "sensitivity": self.sensitivity.value,
            "source_type": self.source_type.value,
            "lineage_group": self.lineage_group,
            "parent_task_hash": self.parent_task_hash,
            "state": self.state.value,
            "dataset_eligible": self.dataset_eligible,
            "preference_eligible": self.preference_eligible,
            "evaluation_only": self.evaluation_only,
            "target_source": self.target_source.value,
            "target_hash": self.target_hash,
            "student_output_changed": self.student_output_changed,
            "editor_id": self.editor_id,
            "approval_hash": self.approval_hash,
            "created_at_utc": self.created_at_utc,
        }

    def to_record(self) -> dict:
        """:meth:`to_dict` plus the digest, for persistence and manifests."""
        return {**self.to_dict(), "candidate_hash": self.candidate_hash()}

    @classmethod
    def from_dict(cls, payload: object) -> "DatasetCandidate":
        """Rebuild a candidate, verifying its digest if one is present.

        The digest check is what makes an edited file on disk detectable: every field is
        still well-formed after a tamper, which is precisely why field validation alone
        proves nothing about integrity.
        """
        data = require_mapping(payload, "candidate")
        reject_unknown_fields(data, _CANDIDATE_FIELDS, label="candidate")
        check_schema_version(data, label="candidate")
        candidate = cls(
            candidate_id=str(data.get("candidate_id", "")),
            task_id=str(data.get("task_id", "")),
            task_family=require_enum(data.get("task_family"), TaskFamily,
                                     "candidate.task_family"),
            task_hash=str(data.get("task_hash", "")),
            attempt_id=str(data.get("attempt_id", "")),
            attempt_hash=str(data.get("attempt_hash", "")),
            deterministic_report_hash=str(data.get("deterministic_report_hash", "")),
            consensus_report_hash=str(data.get("consensus_report_hash", "")),
            human_review_hash=str(data.get("human_review_hash", "")),
            source_episode_hash=str(data.get("source_episode_hash", "")),
            student_model_id=str(data.get("student_model_id", "")),
            student_model_role=str(data.get("student_model_role", "student")),
            prompt_template_hash=str(data.get("prompt_template_hash", "")),
            system_prompt_version=str(data.get("system_prompt_version", "")),
            system_message=str(data.get("system_message", "")),
            user_prompt=str(data.get("user_prompt", "")),
            target_text=str(data.get("target_text", "")),
            tool_calls=tuple(require_mapping(c, "candidate.tool_calls[]")
                             for c in data.get("tool_calls") or ()),
            input_fixture_hashes=require_str_tuple(
                data.get("input_fixture_hashes"), "candidate.input_fixture_hashes",
                max_items=MAX_CANDIDATE_HASHES),
            artifact_hashes=require_str_tuple(
                data.get("artifact_hashes"), "candidate.artifact_hashes",
                max_items=MAX_CANDIDATE_HASHES),
            provenance=require_mapping(data.get("provenance") or {},
                                       "candidate.provenance"),
            sensitivity=require_enum(data.get("sensitivity",
                                              SensitivityClass.SYNTHETIC.value),
                                     SensitivityClass, "candidate.sensitivity"),
            source_type=require_enum(data.get("source_type",
                                              SourceType.GYM_EPISODE.value),
                                     SourceType, "candidate.source_type"),
            lineage_group=str(data.get("lineage_group", "")),
            parent_task_hash=str(data.get("parent_task_hash", "")),
            state=require_enum(data.get("state", CandidateState.CREATED.value),
                               CandidateState, "candidate.state"),
            dataset_eligible=require_bool(data.get("dataset_eligible", True),
                                          "candidate.dataset_eligible"),
            preference_eligible=require_bool(data.get("preference_eligible", False),
                                             "candidate.preference_eligible"),
            evaluation_only=require_bool(data.get("evaluation_only", False),
                                         "candidate.evaluation_only"),
            target_source=require_enum(data.get("target_source"), TargetSource,
                                       "candidate.target_source"),
            target_hash=str(data.get("target_hash", "")),
            student_output_changed=require_bool(
                data.get("student_output_changed", False),
                "candidate.student_output_changed"),
            editor_id=str(data.get("editor_id", "")),
            approval_hash=str(data.get("approval_hash", "")),
            created_at_utc=str(data.get("created_at_utc", "")),
            candidate_version=str(data.get("candidate_version", CANDIDATE_VERSION)),
        )
        declared = str(data.get("candidate_hash", "")).strip().lower()
        if declared and declared != candidate.candidate_hash():
            raise CandidateError(
                f"candidate {candidate.candidate_id}: stored digest "
                f"{short(declared)!r} does not match the record's content "
                f"{short(candidate.candidate_hash())!r}; the record has been modified "
                f"since it was written")
        return candidate


def _clean_provenance(value: object) -> dict:
    """A fixed-shape provenance block. Unknown keys are refused, not dropped."""
    data = require_mapping(value or {}, "candidate.provenance")
    reject_unknown_fields(data, _PROVENANCE_FIELDS, label="candidate.provenance")
    source = str(data.get("source", "")).strip()
    if not source:
        raise CandidateError("candidate.provenance.source: required (an example of "
                             "unknown origin may not become training data)")
    return {"source": source,
            "author": str(data.get("author", "unknown")),
            "usage_terms": str(data.get("usage_terms", "internal-use")),
            "upstream_ref": str(data.get("upstream_ref", ""))}


def _clean_tool_call(value: object, index: int) -> dict:
    """One structured tool-call example. Strict: a name and JSON-ready arguments.

    A free-form dictionary is refused because a tool-call target is the one field a model
    learns to emit VERBATIM — anything that reaches it reaches production behaviour.
    """
    data = require_mapping(value, f"candidate.tool_calls[{index}]")
    reject_unknown_fields(data, ("name", "arguments"),
                          label=f"candidate.tool_calls[{index}]")
    name = require_id(data.get("name"), f"candidate.tool_calls[{index}].name")
    arguments = require_mapping(data.get("arguments") or {},
                                f"candidate.tool_calls[{index}].arguments")
    for key in arguments:
        if not isinstance(key, str):
            raise CandidateError(f"candidate.tool_calls[{index}].arguments: non-string "
                                 f"key {key!r}")
    return {"name": name, "arguments": dict(sorted(arguments.items()))}


__all__ = [
    "ALLOWED_CANDIDATE_TRANSITIONS", "CANDIDATE_VERSION", "MAX_CANDIDATE_HASHES",
    "MAX_CANDIDATE_TEXT_CHARS", "MAX_CANDIDATE_TOOL_CALLS", "NON_EXPORTABLE_SPLITS",
    "CandidateError", "CandidateState", "CandidateTransitionError", "DatasetCandidate",
    "DatasetSplit", "SourceType", "TargetSource", "check_candidate_transition",
    "require_digest",
]
