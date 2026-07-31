"""training_gym/trajectory.py — V69 M62: what an attempt actually did, on the record.

WHY THIS EXISTS
---------------
A trajectory is the evidence file for one attempt at one task. It is simultaneously
the thing a human reviews, the thing a teacher model critiques, and the raw material a
training example is cut from — so it has to be complete enough to reconstruct what
happened and safe enough to hand to a third party. Those two requirements pull in
opposite directions, and this module resolves them the only honest way: by keeping
TWO representations and never confusing them.

  * :meth:`Trajectory.to_dict` is the LOCAL record. It holds the real messages and
    the real tool arguments, because a training example cut from a redacted
    trajectory would teach the model to emit ``[REDACTED]``.
  * :meth:`Trajectory.to_export_dict` is what may leave the host — a teacher packet,
    a bug report, an attached artifact. It is redacted, then SCANNED, and raises if
    anything private survived. Export is not "redact and hope"; it is "redact and
    then refuse to emit if the scanner still finds something".

WHAT IS NEVER STORED, IN EITHER FORM
------------------------------------
API keys, passwords, access tokens, whole environment blocks, the operator's username
or hostname, private absolute paths, unrelated home-directory content, browser data,
or a full sensitive log. Command output is summarised (bytes, lines, hash, bounded
redacted excerpt) rather than captured wholesale, because "the full log" is precisely
where those things hide.

BINDING
-------
Every child record names the attempt it belongs to. :class:`TeacherReview` carries
``attempt_hash`` and ``task_hash``, and :func:`~training_gym.schemas.require_binding`
refuses one that names a different subject — otherwise a favourable review could be
lifted off a passing attempt and re-filed against a failing one, with every field
still perfectly well-formed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .schemas import (
    GYM_VERSION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    ResultStatus,
    SchemaError,
    Severity,
    assert_no_private_content,
    require_bool,
    require_enum,
    require_float,
    require_id,
    require_int,
    require_mapping,
    require_str_tuple,
    scan_private_content,
    sha256_obj,
    sha256_text,
)

#: Longest excerpt of captured output kept in a record.
MAX_EXCERPT_CHARS = 2_000
#: Longest single message body kept in a LOCAL record.
MAX_MESSAGE_CHARS = 100_000
MAX_ACTIONS = 256
MAX_MESSAGES = 256
MAX_ARTIFACTS = 64


# ── vocabulary ────────────────────────────────────────────────────────────────
class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelRole(str, Enum):
    """Which side of the gym a model is acting on. Kept explicit so a teacher's
    output can never be mistaken for the student's attempt."""

    STUDENT = "student"          # the model under practice
    TEACHER = "teacher"          # a reviewer; may never execute anything
    VERIFIER = "verifier"        # the local deterministic-evidence checker
    BASELINE = "baseline"        # the un-adapted model, for comparison
    ADAPTED = "adapted"          # base + candidate adapter


class Recommendation(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class HumanDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    DEFERRED = "deferred"


def _redact(text: str, *, limit: int) -> str:
    """Bound and redact free text for a record.

    Delegates to the application's own redactor when it is importable so the gym
    cannot drift into a weaker definition of "private" than the runtime uses. When it
    is not importable the text is truncated and marked — never emitted as if it had
    been cleaned.
    """
    raw = str(text or "")
    try:
        from core.redaction_policy import redact_text
        cleaned, _report = redact_text(raw, max_chars=limit, strip_home=True)
        return cleaned
    except Exception:  # noqa: BLE001 — absence must be visible, not silently clean
        clipped = raw[:limit]
        suffix = "" if len(raw) <= limit else f"…[+{len(raw) - limit} chars]"
        return f"[unredacted-scanner-unavailable]{clipped}{suffix}"


# ── output and resource summaries ─────────────────────────────────────────────
@dataclass(frozen=True)
class OutputSummary:
    """A command's stdout or stderr, summarised rather than captured.

    The hash is over the FULL original text, so two runs can be compared exactly even
    though neither record holds the whole thing."""

    byte_count: int = 0
    line_count: int = 0
    sha256: str = ""
    excerpt: str = ""
    truncated: bool = False

    @classmethod
    def capture(cls, text: str, *, limit: int = MAX_EXCERPT_CHARS) -> "OutputSummary":
        raw = str(text or "")
        return cls(
            byte_count=len(raw.encode("utf-8", "surrogatepass")),
            line_count=raw.count("\n") + (1 if raw and not raw.endswith("\n") else 0),
            sha256=sha256_text(raw),
            excerpt=_redact(raw, limit=limit),
            truncated=len(raw) > limit,
        )

    def to_dict(self) -> dict:
        return {"byte_count": self.byte_count, "line_count": self.line_count,
                "sha256": self.sha256, "excerpt": self.excerpt,
                "truncated": self.truncated}


@dataclass(frozen=True)
class ResourceUsage:
    """What an attempt actually consumed. Compared against the task's budget."""

    wall_ms: int = 0
    cpu_ms: int = 0
    peak_memory_mb: int = 0
    output_bytes: int = 0
    exceeded_budget: bool = False

    def to_dict(self) -> dict:
        return {"wall_ms": self.wall_ms, "cpu_ms": self.cpu_ms,
                "peak_memory_mb": self.peak_memory_mb,
                "output_bytes": self.output_bytes,
                "exceeded_budget": self.exceeded_budget}


# ── the atoms of an attempt ───────────────────────────────────────────────────
@dataclass(frozen=True)
class MessageRecord:
    """One turn. The local form keeps real content; the export form is redacted."""

    role: MessageRole
    content: str
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", require_enum(self.role, MessageRole,
                                                      "message.role"))
        if not isinstance(self.content, str):
            raise SchemaError("message.content: expected a string")
        if len(self.content) > MAX_MESSAGE_CHARS:
            raise SchemaError(f"message.content: too long ({len(self.content)} > "
                              f"{MAX_MESSAGE_CHARS})")

    def to_dict(self) -> dict:
        return {"role": self.role.value, "content": self.content, "name": self.name}

    def to_export_dict(self) -> dict:
        return {"role": self.role.value,
                "content": _redact(self.content, limit=MAX_EXCERPT_CHARS),
                "name": self.name}


@dataclass(frozen=True)
class ToolCallRecord:
    """A tool the model asked for. Arguments are hashed and previewed, never dumped.

    Recording the raw argument object is how a token pasted into a tool call ends up
    in a training corpus, so the full value exists only as a hash."""

    tool_name: str
    arguments_hash: str
    arguments_preview: str = ""
    result_status: ResultStatus = ResultStatus.SKIPPED
    result_hash: str = ""
    duration_ms: int = 0
    error: str = ""
    #: True when a safety layer refused this call. Recorded, never hidden.
    blocked: bool = False

    @classmethod
    def capture(cls, tool_name: str, arguments: object, **kw: object) -> "ToolCallRecord":
        return cls(tool_name=str(tool_name),
                   arguments_hash=sha256_obj(arguments),
                   arguments_preview=_redact(str(arguments), limit=400),
                   **kw)  # type: ignore[arg-type]

    def to_dict(self) -> dict:
        return {"tool_name": self.tool_name, "arguments_hash": self.arguments_hash,
                "arguments_preview": self.arguments_preview,
                "result_status": self.result_status.value,
                "result_hash": self.result_hash, "duration_ms": self.duration_ms,
                "error": self.error, "blocked": self.blocked}


@dataclass(frozen=True)
class ActionRecord:
    """One executed step inside the sandbox."""

    index: int
    kind: str
    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: OutputSummary = field(default_factory=OutputSummary)
    stderr: OutputSummary = field(default_factory=OutputSummary)
    changed_files: dict[str, str] = field(default_factory=dict)
    error: str = ""
    timed_out: bool = False

    def __post_init__(self) -> None:
        require_int(self.index, "action.index", minimum=0, maximum=MAX_ACTIONS)
        if any(not isinstance(a, str) for a in self.argv):
            raise SchemaError("action.argv: every element must be a string "
                              "(argv is a list, never a shell string)")

    def to_dict(self) -> dict:
        return {"index": self.index, "kind": self.kind, "argv": list(self.argv),
                "exit_code": self.exit_code, "duration_ms": self.duration_ms,
                "stdout": self.stdout.to_dict(), "stderr": self.stderr.to_dict(),
                "changed_files": dict(sorted(self.changed_files.items())),
                "error": self.error, "timed_out": self.timed_out}


@dataclass(frozen=True)
class ArtifactRecord:
    """A file the episode produced and the allowlist permitted out."""

    path: str
    sha256: str
    size_bytes: int = 0
    media_type: str = "application/octet-stream"

    def to_dict(self) -> dict:
        return {"path": self.path, "sha256": self.sha256,
                "size_bytes": self.size_bytes, "media_type": self.media_type}


@dataclass(frozen=True)
class ModelIdentity:
    """Exactly which model produced this attempt, and how it was prompted.

    ``model_id`` must be the resolved identifier (``qwen3:8b-q4_K_M``), not a role
    alias (``FAST``): an attempt attributed to "whatever FAST pointed at that day" is
    not reproducible and must not become training data."""

    role: ModelRole
    base_model: str
    model_id: str
    parameters: dict = field(default_factory=dict)
    prompt_template_hash: str = ""
    adapter_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", require_enum(self.role, ModelRole,
                                                      "model.role"))
        if not str(self.model_id or "").strip():
            raise SchemaError("model.model_id: required (a resolved identifier, not "
                              "a role alias)")
        leaked = sorted(k for k in self.parameters
                        if _looks_like_credential_key(str(k)))
        if leaked:
            raise SchemaError(f"model.parameters: credential-shaped key(s) {leaked}")

    def to_dict(self) -> dict:
        return {"role": self.role.value, "base_model": self.base_model,
                "model_id": self.model_id,
                "parameters": dict(sorted(self.parameters.items())),
                "prompt_template_hash": self.prompt_template_hash,
                "adapter_id": self.adapter_id}


def _looks_like_credential_key(name: str) -> bool:
    from .policies import credential_shaped
    return credential_shaped(name)


# ── verdicts ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GraderResult:
    """One deterministic check's verdict, with the evidence that justifies it.

    ``non_vacuous_measurement`` is the anti-theatre field: a grader that ran but
    examined zero files, zero tests or zero rules must say so. A PASS with nothing
    measured is not a pass, and the aggregator treats it as missing evidence.
    """

    grader_id: str
    grader_version: str
    status: ResultStatus
    score: float = 0.0
    blocking: bool = False
    severity: Severity = Severity.INFO
    evidence: tuple[str, ...] = ()
    findings: tuple[dict, ...] = ()
    duration_ms: int = 0
    tool_version: str = ""
    non_vacuous_measurement: int = 0
    error: str = ""
    artifact_hashes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_id(self.grader_id, "grader.grader_id")
        object.__setattr__(self, "status", require_enum(self.status, ResultStatus,
                                                        "grader.status"))
        object.__setattr__(self, "severity", require_enum(self.severity, Severity,
                                                          "grader.severity"))
        require_float(self.score, "grader.score", minimum=0.0, maximum=1.0)
        require_int(self.non_vacuous_measurement, "grader.non_vacuous_measurement",
                    minimum=0, maximum=10_000_000)
        if self.status is ResultStatus.PASS and self.non_vacuous_measurement <= 0:
            raise SchemaError(
                f"grader {self.grader_id}: reported PASS with zero measurements. A "
                f"check that examined nothing has not passed — report SKIPPED or "
                f"INSUFFICIENT_EVIDENCE instead.")
        if self.status is ResultStatus.ERROR and not self.error:
            raise SchemaError(f"grader {self.grader_id}: ERROR requires a reason")

    @property
    def passed(self) -> bool:
        """Affirmative measurement only. Never true for a check that did not run."""
        return self.status.is_affirmative

    def blocks_approval(self, *, mandatory: bool) -> bool:
        """Whether this result must prevent approval.

        A mandatory grader blocks on anything that is not PASS or NOT_APPLICABLE —
        including SKIPPED, which is the one people expect to be harmless and is not:
        "the scanner was not installed" is missing evidence, not a clean bill.
        """
        if self.blocking and self.status is ResultStatus.FAIL:
            return True
        if self.severity.blocks and self.status is not ResultStatus.PASS:
            return True
        return mandatory and self.status.is_blocking_when_mandatory

    def to_dict(self) -> dict:
        return {"grader_id": self.grader_id, "grader_version": self.grader_version,
                "status": self.status.value, "score": self.score,
                "passed": self.passed, "blocking": self.blocking,
                "severity": self.severity.value, "evidence": list(self.evidence),
                "findings": [dict(f) for f in self.findings],
                "duration_ms": self.duration_ms, "tool_version": self.tool_version,
                "non_vacuous_measurement": self.non_vacuous_measurement,
                "error": self.error,
                "artifact_hashes": dict(sorted(self.artifact_hashes.items()))}

    @classmethod
    def from_dict(cls, payload: object) -> "GraderResult":
        from .schemas import reject_unknown_fields
        data = require_mapping(payload, "grader")
        reject_unknown_fields(data, (
            "grader_id", "grader_version", "status", "score", "passed", "blocking",
            "severity", "evidence", "findings", "duration_ms", "tool_version",
            "non_vacuous_measurement", "error", "artifact_hashes"), label="grader")
        return cls(
            grader_id=str(data.get("grader_id", "")),
            grader_version=str(data.get("grader_version", "")),
            status=require_enum(data.get("status"), ResultStatus, "grader.status"),
            score=require_float(data.get("score", 0.0), "grader.score",
                                minimum=0.0, maximum=1.0),
            blocking=require_bool(data.get("blocking", False), "grader.blocking"),
            severity=require_enum(data.get("severity", Severity.INFO.value),
                                  Severity, "grader.severity"),
            evidence=require_str_tuple(data.get("evidence"), "grader.evidence"),
            findings=tuple(require_mapping(f, "grader.findings[]")
                           for f in data.get("findings") or ()),
            duration_ms=require_int(data.get("duration_ms", 0), "grader.duration_ms",
                                    minimum=0, maximum=86_400_000),
            tool_version=str(data.get("tool_version", "")),
            non_vacuous_measurement=require_int(
                data.get("non_vacuous_measurement", 0),
                "grader.non_vacuous_measurement", minimum=0, maximum=10_000_000),
            error=str(data.get("error", "")),
            artifact_hashes={str(k): str(v) for k, v in require_mapping(
                data.get("artifact_hashes") or {}, "grader.artifact_hashes").items()},
        )


@dataclass(frozen=True)
class TeacherReview:
    """A reviewer model's opinion. UNTRUSTED INPUT, validated like any other.

    A teacher review is generated outside this process — by a cloud API, or pasted in
    by a human from a chat window. It is therefore treated exactly as hostile external
    data: every field validated, bound to the attempt it describes, and incapable of
    turning a deterministic FAIL into anything else. The aggregation rule lives in
    :mod:`training_gym.rewards`; the binding rule lives here.
    """

    provider: str
    model: str
    task_hash: str
    attempt_hash: str
    rubric_version: str
    overall_score: float
    recommendation: Recommendation
    timestamp: str
    dimension_scores: dict[str, float] = field(default_factory=dict)
    factual_errors: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    unsafe_behavior: tuple[str, ...] = ()
    style_issues: tuple[str, ...] = ()
    corrected_answer: str = ""
    confidence: float = 0.0

    def __post_init__(self) -> None:
        require_id(self.provider, "teacher.provider")
        if not str(self.model or "").strip():
            raise SchemaError("teacher.model: required (which model reviewed this)")
        for name in ("task_hash", "attempt_hash"):
            value = str(getattr(self, name) or "").strip()
            if len(value) != 64:
                raise SchemaError(f"teacher.{name}: expected a 64-character digest; "
                                  f"an unbound review can be replayed onto another "
                                  f"attempt")
        object.__setattr__(self, "recommendation",
                           require_enum(self.recommendation, Recommendation,
                                        "teacher.recommendation"))
        require_float(self.overall_score, "teacher.overall_score",
                      minimum=0.0, maximum=1.0)
        require_float(self.confidence, "teacher.confidence", minimum=0.0, maximum=1.0)
        for key, value in self.dimension_scores.items():
            require_float(value, f"teacher.dimension_scores[{key}]",
                          minimum=0.0, maximum=1.0)

    @property
    def flags_unsafe(self) -> bool:
        return bool(self.unsafe_behavior)

    def review_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def verify_binding(self, *, task_hash: str, attempt_hash: str) -> None:
        """Refuse this review if it describes a different task or attempt."""
        from .schemas import require_binding
        payload = {"task_hash": self.task_hash, "attempt_hash": self.attempt_hash}
        require_binding(payload, field="task_hash", expected=task_hash,
                        label=f"teacher review from {self.provider}")
        require_binding(payload, field="attempt_hash", expected=attempt_hash,
                        label=f"teacher review from {self.provider}")

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model,
            "task_hash": self.task_hash, "attempt_hash": self.attempt_hash,
            "rubric_version": self.rubric_version,
            "overall_score": self.overall_score,
            "recommendation": self.recommendation.value,
            "timestamp": self.timestamp,
            "dimension_scores": dict(sorted(self.dimension_scores.items())),
            "factual_errors": list(self.factual_errors),
            "unsupported_claims": list(self.unsupported_claims),
            "missing_evidence": list(self.missing_evidence),
            "unsafe_behavior": list(self.unsafe_behavior),
            "style_issues": list(self.style_issues),
            "corrected_answer": self.corrected_answer,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "TeacherReview":
        from .schemas import reject_unknown_fields
        data = require_mapping(payload, "teacher review")
        reject_unknown_fields(data, (
            "provider", "model", "task_hash", "attempt_hash", "rubric_version",
            "overall_score", "recommendation", "timestamp", "dimension_scores",
            "factual_errors", "unsupported_claims", "missing_evidence",
            "unsafe_behavior", "style_issues", "corrected_answer", "confidence",
            "review_hash"), label="teacher review")
        scores = require_mapping(data.get("dimension_scores") or {},
                                 "teacher.dimension_scores")
        return cls(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            task_hash=str(data.get("task_hash", "")),
            attempt_hash=str(data.get("attempt_hash", "")),
            rubric_version=str(data.get("rubric_version", "")),
            overall_score=require_float(data.get("overall_score", 0.0),
                                        "teacher.overall_score",
                                        minimum=0.0, maximum=1.0),
            recommendation=require_enum(data.get("recommendation"), Recommendation,
                                        "teacher.recommendation"),
            timestamp=str(data.get("timestamp", "")),
            dimension_scores={str(k): require_float(v, f"teacher.dim[{k}]",
                                                    minimum=0.0, maximum=1.0)
                              for k, v in scores.items()},
            factual_errors=require_str_tuple(data.get("factual_errors"),
                                             "teacher.factual_errors", max_items=64),
            unsupported_claims=require_str_tuple(data.get("unsupported_claims"),
                                                 "teacher.unsupported_claims",
                                                 max_items=64),
            missing_evidence=require_str_tuple(data.get("missing_evidence"),
                                               "teacher.missing_evidence",
                                               max_items=64),
            unsafe_behavior=require_str_tuple(data.get("unsafe_behavior"),
                                              "teacher.unsafe_behavior", max_items=64),
            style_issues=require_str_tuple(data.get("style_issues"),
                                           "teacher.style_issues", max_items=64),
            corrected_answer=str(data.get("corrected_answer", "")),
            confidence=require_float(data.get("confidence", 0.0),
                                     "teacher.confidence", minimum=0.0, maximum=1.0),
        )


@dataclass(frozen=True)
class HumanReview:
    """The operator's decision. The only thing that can approve training data."""

    reviewer: str
    decision: HumanDecision
    timestamp: str
    attempt_hash: str
    reason: str = ""

    def __post_init__(self) -> None:
        require_id(self.reviewer, "human_review.reviewer")
        object.__setattr__(self, "decision", require_enum(self.decision, HumanDecision,
                                                          "human_review.decision"))
        if len(str(self.attempt_hash or "").strip()) != 64:
            raise SchemaError("human_review.attempt_hash: expected a 64-character "
                              "digest binding this decision to one attempt")
        if self.decision is HumanDecision.REJECTED and not self.reason.strip():
            raise SchemaError("human_review: a rejection must state a reason")

    @property
    def approves(self) -> bool:
        return self.decision is HumanDecision.APPROVED

    def to_dict(self) -> dict:
        return {"reviewer": self.reviewer, "decision": self.decision.value,
                "timestamp": self.timestamp, "attempt_hash": self.attempt_hash,
                "reason": self.reason}


# ── the trajectory ────────────────────────────────────────────────────────────
@dataclass
class Trajectory:
    """The complete record of one attempt. Mutable while running, hashed when sealed."""

    episode_id: str
    task_id: str
    task_hash: str
    attempt_number: int
    model: ModelIdentity
    sandbox_backend: str = "dry_run"
    sandbox_version: str = ""
    started_at: str = ""
    finished_at: str = ""
    messages: list[MessageRecord] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    grader_results: list[GraderResult] = field(default_factory=list)
    teacher_reviews: list[TeacherReview] = field(default_factory=list)
    human_review: HumanReview | None = None
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    input_hashes: dict[str, str] = field(default_factory=dict)
    final_answer: str = ""
    refused: bool = False
    rejection_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        require_id(self.episode_id, "trajectory.episode_id")
        require_id(self.task_id, "trajectory.task_id")
        require_int(self.attempt_number, "trajectory.attempt_number",
                    minimum=1, maximum=64)
        if len(str(self.task_hash or "").strip()) != 64:
            raise SchemaError("trajectory.task_hash: expected a 64-character digest")

    # -- identity ---------------------------------------------------------------
    def attempt_hash(self) -> str:
        """The digest every child record binds to.

        Computed over the ATTEMPT — what was asked, what the model did, what it
        produced — and deliberately NOT over the verdicts. Grader results, teacher
        reviews and the human decision all reference this hash, so including them
        would make the hash change every time a verdict was attached and break the
        very binding it exists to provide.
        """
        return sha256_obj({
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "attempt_number": self.attempt_number,
            "model": self.model.to_dict(),
            "sandbox_backend": self.sandbox_backend,
            "sandbox_version": self.sandbox_version,
            "messages": [m.to_dict() for m in self.messages],
            "actions": [a.to_dict() for a in self.actions],
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "input_hashes": dict(sorted(self.input_hashes.items())),
            "final_answer": self.final_answer,
            "refused": self.refused,
        })

    # -- accumulation -----------------------------------------------------------
    def add_message(self, role: MessageRole | str, content: str, name: str = "") -> None:
        if len(self.messages) >= MAX_MESSAGES:
            raise SchemaError(f"trajectory: message ceiling ({MAX_MESSAGES}) reached")
        self.messages.append(MessageRecord(role=require_enum(role, MessageRole,
                                                             "message.role"),
                                           content=content, name=name))

    def add_action(self, action: ActionRecord) -> None:
        if len(self.actions) >= MAX_ACTIONS:
            raise SchemaError(f"trajectory: action ceiling ({MAX_ACTIONS}) reached")
        self.actions.append(action)

    def add_artifact(self, artifact: ArtifactRecord) -> None:
        if len(self.artifacts) >= MAX_ARTIFACTS:
            raise SchemaError(f"trajectory: artifact ceiling ({MAX_ARTIFACTS}) reached")
        self.artifacts.append(artifact)

    def add_teacher_review(self, review: TeacherReview) -> None:
        """Attach a review, refusing one bound to a different attempt."""
        review.verify_binding(task_hash=self.task_hash,
                              attempt_hash=self.attempt_hash())
        if any(r.provider == review.provider and r.model == review.model
               for r in self.teacher_reviews):
            raise SchemaError(f"trajectory: {review.provider}/{review.model} has "
                              f"already reviewed this attempt")
        self.teacher_reviews.append(review)

    def set_human_review(self, review: HumanReview) -> None:
        from .schemas import require_binding
        require_binding({"attempt_hash": review.attempt_hash},
                        field="attempt_hash", expected=self.attempt_hash(),
                        label="human review")
        self.human_review = review

    # -- queries ----------------------------------------------------------------
    def grader(self, grader_id: str) -> GraderResult | None:
        for result in self.grader_results:
            if result.grader_id == grader_id:
                return result
        return None

    def missing_graders(self, required: tuple[str, ...]) -> tuple[str, ...]:
        """Required graders with no result at all. Absence is not a pass."""
        present = {r.grader_id for r in self.grader_results}
        return tuple(sorted(set(required) - present))

    # -- serialization ----------------------------------------------------------
    def to_dict(self) -> dict:
        """The LOCAL record: complete, unredacted, never leaves the host as-is."""
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "gym_version": GYM_VERSION,
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "attempt_number": self.attempt_number,
            "attempt_hash": self.attempt_hash(),
            "model": self.model.to_dict(),
            "sandbox_backend": self.sandbox_backend,
            "sandbox_version": self.sandbox_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "messages": [m.to_dict() for m in self.messages],
            "actions": [a.to_dict() for a in self.actions],
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "grader_results": [g.to_dict() for g in self.grader_results],
            "teacher_reviews": [t.to_dict() for t in self.teacher_reviews],
            "human_review": self.human_review.to_dict() if self.human_review else None,
            "resource_usage": self.resource_usage.to_dict(),
            "input_hashes": dict(sorted(self.input_hashes.items())),
            "final_answer": self.final_answer,
            "refused": self.refused,
            "rejection_reasons": list(self.rejection_reasons),
        }

    def to_export_dict(self) -> dict:
        """The SANITIZED record, safe to hand to a teacher or attach to a report.

        Redacts, then SCANS, then raises if anything private survived. A redactor that
        silently missed something must not be the last line of defence, so the scan is
        not optional and its failure is not a warning.
        """
        payload = {
            SCHEMA_KEY: SCHEMA_VERSION,
            "gym_version": GYM_VERSION,
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "attempt_number": self.attempt_number,
            "attempt_hash": self.attempt_hash(),
            "model": {"role": self.model.role.value,
                      "base_model": self.model.base_model,
                      "model_id": self.model.model_id,
                      "prompt_template_hash": self.model.prompt_template_hash},
            "sandbox_backend": self.sandbox_backend,
            "messages": [m.to_export_dict() for m in self.messages],
            "actions": [{"index": a.index, "kind": a.kind, "argv": list(a.argv),
                         "exit_code": a.exit_code, "timed_out": a.timed_out,
                         "stdout": a.stdout.to_dict(), "stderr": a.stderr.to_dict()}
                        for a in self.actions],
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "grader_results": [g.to_dict() for g in self.grader_results],
            "final_answer": _redact(self.final_answer, limit=MAX_MESSAGE_CHARS),
            "refused": self.refused,
        }
        assert_no_private_content(payload, label=f"trajectory {self.episode_id} export")
        return payload

    def export_blockers(self) -> tuple[str, ...]:
        """Why this trajectory cannot be exported, or an empty tuple if it can.

        Non-raising counterpart to :meth:`to_export_dict`, for a CLI that wants to
        report the problem rather than crash on it."""
        try:
            payload = self.to_export_dict()
        except SchemaError as exc:
            return (str(exc),)
        return scan_private_content(payload)


__all__ = [
    "MAX_ACTIONS", "MAX_ARTIFACTS", "MAX_EXCERPT_CHARS", "MAX_MESSAGES",
    "MAX_MESSAGE_CHARS", "ActionRecord", "ArtifactRecord", "GraderResult",
    "HumanDecision", "HumanReview", "MessageRecord", "MessageRole",
    "ModelIdentity", "ModelRole", "OutputSummary", "Recommendation",
    "ResourceUsage", "TeacherReview", "ToolCallRecord", "Trajectory",
]
