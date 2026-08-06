"""training_gym/datasets/promotion.py — V69 M62 S2d: the gate into a dataset.

WHY THIS EXISTS
---------------
Everything upstream is *evidence*. This module is the one place that turns evidence into
a proposal, and it is written on the assumption that every upstream boolean is either
stale, forgeable, or answering a subtly different question. So nothing here is inherited:

  * ``EpisodeOutcome.dataset_eligible`` is NOT read. It is derived from
    ``state.is_dataset_source`` and a CACHED reward, and neither survives contact with a
    directly-constructed :class:`~training_gym.episode.Episode` or a grader set that was
    swapped after scoring. :func:`candidate_blockers` re-runs
    :meth:`~training_gym.episode.Episode.approval_blockers` and re-runs
    :func:`~training_gym.graders.aggregate.aggregate` instead.
  * ``episode.state == APPROVED`` is not accepted on its own. ``Episode`` is a plain
    dataclass whose ``__post_init__`` validates an id and an enum, so the state can be
    set at construction with an empty history and no transition ever having happened.
    A ``NEEDS_HUMAN_REVIEW -> APPROVED`` record must be present in ``episode.history``.
  * ``ConsensusReport.eligible_for_human_approval`` is the weakest claim in the
    milestone. It means *a human may now be asked*, and it is treated as exactly that.
  * a teacher's ``corrected_answer`` is never the target unless a human separately
    approved its exact digest, and the check is positive rather than by omission: when
    the declared source is not the teacher one, the target is compared against every
    ``corrected_answer`` on the trajectory and refused if it matches.

THE TARGET THE HUMAN APPROVED IS THE TARGET THAT SHIPS
------------------------------------------------------
Sanitization CHANGES text. If a reviewer approved the raw answer and the export shipped
a redacted transform of it, the signature would cover bytes nobody trained on. So
:func:`prepare_target_text` sanitizes FIRST, the operator is shown and approves that
result, and :func:`build_candidate` re-derives the same bytes and requires the review's
``target_hash`` to match them. A review of the pre-sanitization text fails loudly here
rather than quietly authorising something else.

WHAT THIS MODULE REFUSES
------------------------
A missing or non-affirmative deterministic report; a missing consensus; a consensus that
has not reached human-review eligibility; an episode human review that is absent, bound
elsewhere, or not APPROVED; a dataset human review that is absent, bound elsewhere, not
APPROVED, or replayed; an evaluation-only or restricted task; a fixture that was declared
but never staged; an expected-output schema that carries the answer; and a target that is
empty, unsanitizable, or not the one that was signed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..episode import Episode, EpisodeState
from ..graders.aggregate import AggregationReport, aggregate
from ..schemas import SchemaError, sha256_text
from ..task_spec import TaskSpec
from ..teachers.consensus import ConsensusReport
from ..teachers.sanitization import (
    SanitizationError,
    assert_clean,
    hidden_key,
    sanitize_text,
)
from ..trajectory import Trajectory
from .candidate import (
    CandidateError,
    CandidateState,
    DatasetCandidate,
    SourceType,
    TargetSource,
)
from .human_review import DatasetHumanReview, ReviewReplayRejected

#: Bump when a rule in this module changes. Recorded on every candidate built here.
PROMOTION_POLICY_VERSION = "m62.promotion.1"

#: JSON-Schema keywords that can carry a held-out answer inside a task's declared output
#: schema. ``const``, ``default`` and ``examples`` name a specific value outright; a
#: single-member ``enum`` is a ``const`` wearing a different keyword.
ANSWER_BEARING_SCHEMA_KEYWORDS: tuple[str, ...] = ("const", "default", "examples")

#: How deep the schema screen walks. Deeper than this is not a schema.
MAX_SCHEMA_DEPTH = 12


def prepare_target_text(text: object) -> str:
    """Sanitize a proposed target and return the exact bytes that would be trained on.

    This is what the operator must be shown before deciding. Sanitization is not
    cosmetic — it rewrites paths and identity strings — so approving the raw model output
    and shipping the sanitized transform would mean the signature covers text that was
    never trained on and the trained text was never signed.
    """
    clean, report = sanitize_text(text)
    if not report.scanner_available:
        raise SanitizationError(
            "target: the redaction scanner is unavailable, so nothing can be asserted "
            "about these bytes; refusing to prepare a target rather than reporting a "
            "clean result nobody measured")
    if not clean.strip():
        raise CandidateError(
            "target: empty after sanitization. An example whose answer was entirely "
            "redacted teaches the model to emit nothing, and it would pass every "
            "structural check on the way there")
    return clean


def schema_answer_exposure(schema: object, *, _depth: int = 0,
                           _path: str = "expected_output_schema") -> tuple[str, ...]:
    """Locations in a task's output schema that name an expected value.

    A schema is rendered into the prompt so the model knows what shape to emit. If it
    also says ``{"verdict": {"const": "malicious"}}`` then the prompt contains the answer,
    every grade against it is unfalsifiable, and the resulting training example teaches
    the model to read the answer out of its own instructions.

    A multi-member ``enum`` is NOT reported: constraining a verdict to one of three
    allowed strings is what an output schema is for. A single-member ``enum`` is.
    """
    if _depth > MAX_SCHEMA_DEPTH:
        return (f"{_path}: nested deeper than {MAX_SCHEMA_DEPTH}; refusing to certify a "
                f"schema this layer did not fully read",)
    found: list[str] = []
    if isinstance(schema, Mapping):
        for keyword in ANSWER_BEARING_SCHEMA_KEYWORDS:
            if keyword in schema:
                found.append(f"{_path}.{keyword}")
        enum = schema.get("enum")
        if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes)):
            if len(list(enum)) == 1:
                found.append(f"{_path}.enum (single member — a const in disguise)")
        for key, value in schema.items():
            if hidden_key(key):
                found.append(f"{_path}.{key} (held-out field name)")
            found.extend(schema_answer_exposure(value, _depth=_depth + 1,
                                                _path=f"{_path}.{key}"))
    elif isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
        for index, value in enumerate(schema):
            found.extend(schema_answer_exposure(value, _depth=_depth + 1,
                                                _path=f"{_path}[{index}]"))
    return tuple(found)


def _approval_transition_recorded(episode: Episode) -> bool:
    """True only if the episode actually WALKED into approval.

    ``Episode(episode_id=..., spec=..., state=EpisodeState.APPROVED)`` is a legal
    construction: ``__post_init__`` validates the id and coerces the enum, and nothing
    else runs. The state field therefore proves nothing on its own — the history does.
    """
    return any(record.from_state is EpisodeState.NEEDS_HUMAN_REVIEW
               and record.to_state is EpisodeState.APPROVED
               for record in episode.history)


def _unstaged_fixtures(spec: TaskSpec, trajectory: Trajectory) -> tuple[str, ...]:
    """Fixtures the task declares that the attempt never actually received.

    :meth:`~training_gym.workspace.EpisodeWorkspace.stage_fixtures` records a rejection
    and CONTINUES when a source is a symlink, missing or oversized. That is right for the
    workspace — the episode should run and be graded on what it got — but it is fatal
    here: the model answered a different question than the spec describes, so any leakage
    index keyed on the declared fixture set would be indexing material that was never
    present.
    """
    staged = dict(trajectory.input_hashes)
    problems: list[str] = []
    for fixture in spec.fixtures:
        actual = str(staged.get(fixture.path, "")).strip().lower()
        if not actual:
            problems.append(f"{fixture.path}: declared by the task but never staged")
        elif actual != fixture.sha256:
            problems.append(f"{fixture.path}: staged content does not match the "
                            f"declared digest")
    return tuple(problems)


def candidate_blockers(*, episode: Episode, spec: TaskSpec, trajectory: Trajectory,
                       consensus: ConsensusReport | None,
                       review: DatasetHumanReview | None,
                       target_text: str,
                       target_source: TargetSource,
                       aggregation: AggregationReport | None = None,
                       evaluation_only: bool = False) -> tuple[str, ...]:
    """Every reason this material may not become a candidate. Empty tuple = eligible.

    Re-derived from the evidence at call time. Nothing here trusts that an earlier stage
    already checked, because "an earlier stage already checked" is how a gate becomes
    decorative — and every one of these checks has a caller upstream that believes it
    already ran.
    """
    problems: list[str] = []

    # -- the episode actually walked the state machine --------------------------
    if episode.state is not EpisodeState.APPROVED:
        problems.append(f"episode state is {episode.state.value}; only an approved "
                        f"episode may contribute a positive training example")
    elif not _approval_transition_recorded(episode):
        problems.append("episode is marked approved but its history records no "
                        "needs_human_review -> approved transition; the state was set, "
                        "not reached")
    problems.extend(episode.approval_blockers())

    # -- the deterministic evidence, recomputed rather than read back ------------
    report = aggregation if aggregation is not None else aggregate(spec, trajectory)
    if report.integrity_violations:
        problems.append(f"deterministic integrity violations: "
                        f"{list(report.integrity_violations)}")
    if not report.eligible_for_review:
        problems.append(f"deterministic aggregation is not eligible for review: "
                        f"{list(report.blockers) or ['no reason recorded']}")
    if report.missing_graders:
        problems.append(f"mandatory grader evidence incomplete: "
                        f"{list(report.missing_graders)}")

    # -- the teacher consensus ---------------------------------------------------
    if consensus is None:
        problems.append("no consensus report; a candidate needs the teacher stage to "
                        "have run, even when no teacher objected")
    else:
        if consensus.deterministic_report_hash != report.report_hash():
            problems.append("consensus report describes a different deterministic "
                            "report than the one recomputed here")
        if not consensus.eligible_for_human_approval:
            problems.append(f"consensus outcome {consensus.outcome.value} has not "
                            f"reached human-review eligibility")

    # -- the dataset human review ------------------------------------------------
    if review is None:
        problems.append("no dataset human review; teacher agreement, a verifier "
                        "response and a caller's boolean are none of them an approval")
    else:
        if not review.approves:
            problems.append(f"dataset human decision is {review.decision.value}")
        if review.target_source is not target_source:
            problems.append(f"the review approved a {review.target_source.value} target "
                            f"but this candidate declares {target_source.value}")
        try:
            review.verify_bindings(
                task_hash=spec.spec_hash(),
                attempt_hash=trajectory.attempt_hash(),
                deterministic_report_hash=report.report_hash(),
                consensus_report_hash=(consensus.report_hash() if consensus else ""))
        except SchemaError as exc:
            problems.append(str(exc))
        try:
            review.verify_target(target_hash=sha256_text(target_text))
        except SchemaError as exc:
            problems.append(f"the approved target is not these bytes: {exc}")

    # -- task-level eligibility, asserted independently --------------------------
    # The two intents are mutually exclusive and each refuses the other's material.
    # Held-out evidence must be unusable for fitting, and training material must not
    # quietly become the yardstick it is measured against.
    if evaluation_only:
        if not spec.evaluation_only:
            problems.append(
                "task is not marked evaluation_only but is being built as held-out "
                "evaluation evidence; the task authority, not the caller, decides "
                "whether material may be trained on")
        if spec.dataset_eligible:
            problems.append(
                "task is dataset_eligible and may not become evaluation-only evidence; "
                "a record that is both is a training example scoring itself")
    else:
        if spec.evaluation_only:
            problems.append("task is evaluation_only and may never be trained on")
        if not spec.dataset_eligible:
            problems.append("task is marked not dataset eligible")
        if not spec.sensitivity.dataset_eligible:
            problems.append(
                f"task sensitivity {spec.sensitivity.value} is never trainable")

    # -- the material itself -----------------------------------------------------
    problems.extend(_unstaged_fixtures(spec, trajectory))
    exposure = schema_answer_exposure(spec.expected_output_schema)
    if exposure:
        problems.append(f"the task's output schema names the expected answer at "
                        f"{list(exposure)}; a prompt carrying its own answer key is not "
                        f"a training example")
    if not str(target_text or "").strip():
        problems.append("target is empty")

    # -- a teacher correction may not arrive by the back door --------------------
    if target_source is not TargetSource.TEACHER_PROPOSED_HUMAN_APPROVED:
        for teacher in trajectory.teacher_reviews:
            corrected = str(teacher.corrected_answer or "").strip()
            if corrected and corrected == str(target_text).strip():
                problems.append(
                    f"the target is {teacher.provider}'s corrected_answer but is "
                    f"declared {target_source.value}; a teacher's correction may only "
                    f"be a target under teacher_proposed_human_approved, which requires "
                    f"the human to have been shown it separately")
    return tuple(problems)


def build_candidate(*, episode: Episode, spec: TaskSpec, trajectory: Trajectory,
                    consensus: ConsensusReport, review: DatasetHumanReview,
                    candidate_id: str, created_at_utc: str, target_text: str,
                    target_source: TargetSource, lineage_group: str,
                    system_message: str = "", system_prompt_version: str = "",
                    source_type: SourceType = SourceType.GYM_EPISODE,
                    parent_task_hash: str = "", editor_id: str = "",
                    student_output_changed: bool = False,
                    aggregation: AggregationReport | None = None,
                    ledger: object | None = None,
                    evaluation_only: bool = False) -> DatasetCandidate:
    """Build a candidate in ``CREATED`` state, or raise with every blocker.

    *target_text* must already be the output of :func:`prepare_target_text` — the exact
    bytes the reviewer saw. It is re-sanitized here and refused if that changes anything,
    because a target that sanitizes differently on the second pass is a target whose
    approved digest describes something else.

    When *ledger* is supplied its ``consume`` is called, so the review is spent. A ledger
    is not optional in production; it is optional in this signature only so a dry run can
    validate without consuming an approval it is not going to use.

    *evaluation_only* builds held-out evidence instead of a training example. It requires
    the same episode, the same deterministic aggregation, the same teacher consensus and
    the same human approval — the evidence chain is not the thing that differs. What
    differs is where the record may go afterwards: ``dataset_eligible`` is false, so the
    split authority may only place it in a held-out split, and the leakage analyser's
    evaluation-only contamination check refuses it anywhere a model is fitted or steered.
    """
    report = aggregation if aggregation is not None else aggregate(spec, trajectory)
    clean_target = prepare_target_text(target_text)
    if clean_target != target_text:
        raise CandidateError(
            "target: re-sanitizing changed the bytes. The reviewer must approve the "
            "output of prepare_target_text(); approving raw text and shipping a "
            "redacted transform would mean the signature covers text nobody trained on")

    blockers = candidate_blockers(
        episode=episode, spec=spec, trajectory=trajectory, consensus=consensus,
        review=review, target_text=clean_target, target_source=target_source,
        aggregation=report, evaluation_only=evaluation_only)
    if blockers:
        raise CandidateError(
            f"candidate {candidate_id}: refusing to create — " + "; ".join(blockers))

    clean_prompt = prepare_target_text(spec.prompt)
    clean_system, system_report = sanitize_text(system_message)
    if not system_report.scanner_available:
        raise SanitizationError("system_message: the redaction scanner is unavailable")

    candidate = DatasetCandidate(
        candidate_id=candidate_id,
        task_id=spec.task_id,
        task_family=spec.task_family,
        task_hash=spec.spec_hash(),
        attempt_id=trajectory.attempt_hash(),
        attempt_hash=trajectory.attempt_hash(),
        deterministic_report_hash=report.report_hash(),
        consensus_report_hash=consensus.report_hash(),
        human_review_hash=review.review_hash(),
        source_episode_hash=episode.outcome().outcome_hash(),
        student_model_id=trajectory.model.model_id,
        student_model_role=trajectory.model.role.value,
        prompt_template_hash=trajectory.model.prompt_template_hash,
        system_prompt_version=system_prompt_version,
        system_message=clean_system,
        user_prompt=clean_prompt,
        target_text=clean_target,
        target_source=target_source,
        target_hash=sha256_text(clean_target),
        approval_hash=review.review_hash(),
        created_at_utc=created_at_utc,
        input_fixture_hashes=tuple(f.sha256 for f in spec.fixtures),
        artifact_hashes=tuple(a.sha256 for a in trajectory.artifacts if a.sha256),
        provenance={"source": spec.provenance.source,
                    "author": spec.provenance.author,
                    "usage_terms": spec.provenance.license,
                    "upstream_ref": spec.provenance.upstream_ref},
        sensitivity=spec.sensitivity,
        source_type=source_type,
        lineage_group=lineage_group,
        parent_task_hash=parent_task_hash,
        state=CandidateState.CREATED,
        dataset_eligible=not evaluation_only,
        preference_eligible=False,
        evaluation_only=evaluation_only,
        editor_id=editor_id or review.editor_id,
        student_output_changed=student_output_changed,
    )
    refuse_hidden_field_names(candidate.to_record(), label=f"candidate {candidate_id}")
    assert_clean(candidate.to_record(), label=f"candidate {candidate_id}")
    if ledger is not None:
        consume = getattr(ledger, "consume", None)
        if not callable(consume):
            raise CandidateError("ledger: must implement consume(review, candidate_id=)")
        consume(review, candidate_id=candidate.candidate_id)
    return candidate


def refuse_hidden_field_names(payload: object, *, label: str,
                              _depth: int = 0) -> None:
    """Refuse a record whose KEY NAMES the export sanitizer would blank.

    :func:`~training_gym.teachers.sanitization.sanitize_structure` replaces the value of
    any key matching :func:`~training_gym.teachers.sanitization.hidden_key` with a marker
    — ``idealoutput``, ``groundtruth``, ``solution``, ``answerkey`` and the credential
    names are all in that set. A dataset row whose target lived under a key called
    ``ideal_output`` would therefore export as the literal string
    ``[dropped-by-export-policy]``, silently, having passed every other gate on the way.

    Catching it by NAME at build time is the only place the mistake is cheap. The target
    field is called ``target_text`` for exactly this reason.
    """
    if _depth > MAX_SCHEMA_DEPTH:
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if hidden_key(key):
                raise CandidateError(
                    f"{label}: field {str(key)!r} matches the export sanitizer's "
                    f"hidden-key list, so its value would be silently replaced by a "
                    f"marker on export; rename the field")
            refuse_hidden_field_names(value, label=label, _depth=_depth + 1)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for value in payload:
            refuse_hidden_field_names(value, label=label, _depth=_depth + 1)


__all__ = [
    "ANSWER_BEARING_SCHEMA_KEYWORDS", "MAX_SCHEMA_DEPTH", "PROMOTION_POLICY_VERSION",
    "ReviewReplayRejected", "build_candidate", "candidate_blockers",
    "prepare_target_text", "refuse_hidden_field_names", "schema_answer_exposure",
]
