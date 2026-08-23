"""training_gym/evaluation/backend.py — V69 M62 S3C: the closed inference seam.

WHAT A BACKEND IS ALLOWED TO KNOW
---------------------------------
An evaluation backend turns a model-facing task into a response. The request it receives
is the access-control policy: it carries the role, the verified references, the frozen
task, the generation policy, a cancellation token and a resource policy — and it has no
field for a hidden target, a rubric, a teacher answer, the counterpart arm's output, a
human approval or a registry decision. A backend that wanted to cheat would have to be
handed something this dataclass cannot hold.

WHY THIS IS NOT ``TrainingBackend``
-----------------------------------
S3B's protocol describes something that consumes a dataset and produces an adapter. This
one describes something that consumes a prompt and produces text. Reusing the training
protocol would mean one object claiming both capabilities, and the closed registry that
keeps ``FakeTrainingBackend`` out of production would then be guarding two different
things at once.

WHY THE REGISTRY IS CLOSED
--------------------------
There is no module path, class name, executable, config value or environment variable
that selects a backend. ``get_backend`` takes a name and looks it up in a frozen dict of
two entries, one of which is not in the production map at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..schemas import (
    body_free_repr,
    SchemaError,
    assert_no_private_content,
    require_bool,
    require_int,
    require_str_tuple,
    require_text,
    sha256_obj,
    short,
)
from .generation import GenerationPolicy
from .policy import ResourceCeilings
from .references import (
    AdapterEvaluationReference,
    BaseModelEvaluationReference,
    EvaluationRole,
)
from .task_pack import EvaluationTask

#: Bumped when the request or result shape changes.
EVALUATION_BACKEND_PROTOCOL_VERSION = "m62.evaluation_backend.1"


class EvaluationBackendError(SchemaError):
    """A backend that will not be selected, or a result that will not be believed."""


class InterruptionRequested(EvaluationBackendError):
    """An operator asked for the run to stop; the backend must unwind, not finish."""


class BackendStatus(str, Enum):
    """What happened to one generation. ``SUCCEEDED`` alone means text was produced."""

    SUCCEEDED = "succeeded"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"

    @property
    def produced_output(self) -> bool:
        return self is BackendStatus.SUCCEEDED


class BackendErrorCategory(str, Enum):
    """Why a generation did not succeed, in a vocabulary the report can aggregate.

    ``MODEL_NOT_CACHED`` is deliberately its own category rather than being folded into a
    disk error: with ``local_files_only=True`` a missing model raises an OS-level error,
    and reporting "the disk is full" for "the weights are not here" sends an operator to
    the wrong problem.
    """

    NONE = "none"
    CONFIGURATION = "configuration"
    MODEL_NOT_CACHED = "model_not_cached"
    MODEL_ACCESS = "model_access"
    TOKENIZER = "tokenizer"
    ADAPTER = "adapter"
    CHAT_TEMPLATE = "chat_template"
    DEPENDENCY = "dependency"
    HARDWARE = "hardware"
    OUT_OF_MEMORY = "out_of_memory"
    DISK_FULL = "disk_full"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    TRUNCATION = "truncation"
    CLEANUP = "cleanup"
    BACKEND = "backend"
    INTERNAL = "internal"


class FinishReason(str, Enum):
    """Why generation stopped. Needed to tell "answered" from "ran out of budget"."""

    STOP_SEQUENCE = "stop_sequence"
    END_OF_SEQUENCE = "end_of_sequence"
    MAX_NEW_TOKENS = "max_new_tokens"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNKNOWN = "unknown"

    @property
    def output_budget_exhausted(self) -> bool | None:
        """Whether this termination state means the OUTPUT budget was consumed.

        This is the single authority for that question (D38). Metrics, reports and
        tests call it; nobody else compares a finish reason to a literal.

        Three-valued on purpose, following the same rule as ``ArmScore.schema_valid``:
        ``None`` is UNMEASURED and is never an optimistic ``False``. A generation that
        errored did not demonstrate a clean, self-terminated completion, and reporting
        it as one is exactly how this metric would read clean while measuring nothing.

        It is NOT input truncation (``EvaluationResult.input_truncated``) and it is NOT
        a timeout (``timed_out`` / :attr:`TIMEOUT`). Those are three different events
        with three different fixes, and D38 exists because two of them were being read
        as one.
        """
        try:
            return _OUTPUT_BUDGET_EXHAUSTION[self]
        except KeyError:  # pragma: no cover - unreachable until a member is added
            raise EvaluationBackendError(
                f"finish reason {self.value!r} has no output-budget classification. A "
                f"termination state nobody classified must not default to 'the budget "
                f"was not exhausted', because that makes the metric read clean for a "
                f"state it has never seen") from None


#: Every termination state, classified for D38. Exhaustive by construction: a member
#: absent from this table raises rather than defaulting, so adding a
#: :class:`FinishReason` without deciding what it means fails closed.
#:
#: ``TIMEOUT`` maps to ``False`` because a wall-clock timeout is a *different* event, not
#: an unmeasured one — D33 governs it and D38 must not absorb it. Whether a timed-out
#: generation contributes to the metric's denominator at all is decided one level up, by
#: :attr:`EvaluationResult.output_budget_exhausted`, which admits only results that
#: produced output.
_OUTPUT_BUDGET_EXHAUSTION: dict[FinishReason, bool | None] = {
    FinishReason.STOP_SEQUENCE: False,
    FinishReason.END_OF_SEQUENCE: False,
    FinishReason.MAX_NEW_TOKENS: True,
    FinishReason.TIMEOUT: False,
    FinishReason.ERROR: None,
    FinishReason.UNKNOWN: None,
}


class CleanupStatus(str, Enum):
    """What happened when the backend tried to release the model.

    ``UNKNOWN`` is not a synonym for ``RELEASED``. A cleanup nobody verified is a
    candidate adapter that may still be attached when the next evaluation starts.
    """

    RELEASED = "released"
    NOT_REQUIRED = "not_required"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_safe(self) -> bool:
        return self in (CleanupStatus.RELEASED, CleanupStatus.NOT_REQUIRED)


@dataclass
class CancellationToken:
    """A one-way flag. Set by the runner, read by the backend between generations."""

    _requested: bool = False
    reason: str = ""

    def request(self, reason: str = "") -> None:
        self._requested = True
        self.reason = str(reason or "")[:200]

    @property
    def requested(self) -> bool:
        return self._requested

    def raise_if_requested(self) -> None:
        if self._requested:
            raise InterruptionRequested(
                f"evaluation: interruption requested ({self.reason or 'no reason given'})")


# ══════════════════════════════════════════════════════════════════════════════
#  The request
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EvaluationRequest:
    """Everything a backend is allowed to see. The field list IS the policy.

    There is deliberately no ``hidden_target``, ``expected``, ``rubric``,
    ``counterpart_response``, ``teacher_answer``, ``human_review`` or
    ``registry_decision`` field. Handing a backend the answer is not something a caller
    forgot to avoid — it is something this type cannot express.
    """

    role: EvaluationRole
    task: EvaluationTask
    generation: GenerationPolicy
    baseline: BaseModelEvaluationReference
    #: Present only for the candidate arm. The baseline arm receives ``None``, and a
    #: baseline request that carries an adapter is refused rather than ignored.
    adapter: AdapterEvaluationReference | None = None
    resources: ResourceCeilings = field(default_factory=ResourceCeilings)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    #: Where verified adapter artefacts live. Supplied at invocation, never persisted
    #: into a record, and required only for the candidate arm.
    adapter_directory: Path | None = None
    model_cache_root: Path | None = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "role", EvaluationRole(self.role))
        if not isinstance(self.task, EvaluationTask):
            raise EvaluationBackendError(
                "evaluation request: task must be an EvaluationTask; a raw dictionary "
                "has not been through the answer-exposure screen")
        if not isinstance(self.generation, GenerationPolicy):
            raise EvaluationBackendError(
                "evaluation request: generation must be a GenerationPolicy")
        if not isinstance(self.baseline, BaseModelEvaluationReference):
            raise EvaluationBackendError(
                "evaluation request: baseline must be a verified reference")
        if self.role is EvaluationRole.BASELINE and self.adapter is not None:
            raise EvaluationBackendError(
                "evaluation request: the baseline arm may not carry an adapter. If the "
                "baseline is measured with the adapter attached, the comparison is "
                "between two identical models and the delta is noise")
        if self.role is EvaluationRole.CANDIDATE and self.adapter is None:
            raise EvaluationBackendError(
                "evaluation request: the candidate arm requires the adapter reference "
                "it is meant to be measuring")
        if self.adapter is not None and not isinstance(self.adapter,
                                                       AdapterEvaluationReference):
            raise EvaluationBackendError(
                "evaluation request: adapter must be a verified reference, never a path")

    # -- derived ---------------------------------------------------------------
    @property
    def is_candidate(self) -> bool:
        return self.role is EvaluationRole.CANDIDATE

    def parity_hash(self) -> str:
        """The digest both arms must share.

        Covers exactly what the model is asked and how it is decoded — and deliberately
        NOT the role or the adapter, because those are the one thing that is supposed to
        differ. Two arms whose parity hashes disagree were asked different questions.
        """
        return sha256_obj({
            "task_identity_hash": self.task.identity_hash(),
            "generation_policy_hash": self.generation.policy_hash(),
            "tokenizer_identity_hash": self.baseline.tokenizer_identity_hash,
            "base_model_identity_hash": self.baseline.base_model_identity_hash,
            "base_model_revision": self.baseline.base_model_revision,
        })

    def to_public_dict(self) -> dict:
        """A description carrying no host path and no prompt text."""
        return {
            "protocol_version": EVALUATION_BACKEND_PROTOCOL_VERSION,
            "role": self.role.value,
            "task_id": self.task.task_id,
            "task_hash": self.task.task_hash,
            "task_identity_hash": self.task.identity_hash(),
            "generation_policy_hash": self.generation.policy_hash(),
            "baseline_reference_hash": self.baseline.reference_hash(),
            "adapter_reference_hash": self.adapter.reference_hash() if self.adapter
            else "",
            "parity_hash": self.parity_hash(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  The result
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EvaluationResult:
    """One generation's outcome. A raw dictionary is never authoritative."""

    backend_id: str
    backend_version: str
    role: EvaluationRole
    task_id: str
    task_hash: str
    status: BackendStatus
    response_text: str = ""
    proposed_tool_calls: tuple[dict, ...] = ()
    input_tokens: int = -1
    output_tokens: int = -1
    input_truncated: bool = False
    truncated_tokens: int = 0
    latency_ms: int = 0
    peak_memory_category: str = "unmeasured"
    finish_reason: FinishReason = FinishReason.UNKNOWN
    timed_out: bool = False
    interrupted: bool = False
    error_category: BackendErrorCategory = BackendErrorCategory.NONE
    error_message: str = ""
    cleanup_status: CleanupStatus = CleanupStatus.UNKNOWN
    warnings: tuple[str, ...] = ()
    #: The digest of the request this answers. The runner compares it against the
    #: request it sent, so a result for the wrong task cannot be filed as the right one.
    request_parity_hash: str = ""

    def __repr__(self) -> str:
        """Outcome and accounting only — never ``response_text``.

        A model response to a held-out task is as disclosing as the task itself:
        it is the measurement.
        """
        return body_free_repr(
            self, "backend_id", "role", "task_id", "task_hash", "status",
            "finish_reason", "input_tokens", "output_tokens", "latency_ms",
            "timed_out", "interrupted", "error_category", "request_parity_hash")

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "role", EvaluationRole(self.role))
        set_(self, "status", BackendStatus(self.status))
        set_(self, "finish_reason", FinishReason(self.finish_reason))
        set_(self, "error_category", BackendErrorCategory(self.error_category))
        set_(self, "cleanup_status", CleanupStatus(self.cleanup_status))
        for name in ("timed_out", "interrupted", "input_truncated"):
            set_(self, name, require_bool(getattr(self, name), f"result.{name}"))
        for name in ("input_tokens", "output_tokens"):
            set_(self, name, require_int(getattr(self, name), f"result.{name}",
                                         minimum=-1, maximum=1 << 40))
        set_(self, "truncated_tokens", require_int(self.truncated_tokens,
                                                   "result.truncated_tokens",
                                                   minimum=0, maximum=1 << 40))
        set_(self, "latency_ms", require_int(self.latency_ms, "result.latency_ms",
                                             minimum=0, maximum=1 << 40))
        set_(self, "response_text", require_text(self.response_text,
                                                 "result.response_text", min_len=0,
                                                 max_len=1_000_000))
        set_(self, "warnings", require_str_tuple(self.warnings, "result.warnings",
                                                 max_items=64))
        set_(self, "proposed_tool_calls",
             tuple(dict(c) for c in (self.proposed_tool_calls or ())))
        set_(self, "error_message", require_text(self.error_message,
                                                 "result.error_message", min_len=0,
                                                 max_len=2_000))

        if self.status is BackendStatus.SUCCEEDED:
            if self.error_category is not BackendErrorCategory.NONE:
                raise EvaluationBackendError(
                    f"result {self.task_id!r}: SUCCEEDED with error category "
                    f"{self.error_category.value}; one of those two is describing "
                    f"something that did not happen")
            if self.timed_out or self.interrupted:
                raise EvaluationBackendError(
                    f"result {self.task_id!r}: SUCCEEDED while also reporting a timeout "
                    f"or an interruption")
        elif self.error_category is BackendErrorCategory.NONE and \
                self.status is not BackendStatus.UNSUPPORTED:
            raise EvaluationBackendError(
                f"result {self.task_id!r}: {self.status.value} with no error category; "
                f"a failure nobody categorised aggregates into nothing")
        if self.timed_out and self.status is not BackendStatus.TIMED_OUT:
            raise EvaluationBackendError(
                f"result {self.task_id!r}: timed_out with status {self.status.value}")
        if self.input_truncated and self.truncated_tokens <= 0:
            raise EvaluationBackendError(
                f"result {self.task_id!r}: reports truncation without saying how much; "
                f"a truncation with no count cannot be checked against the evidence the "
                f"task required")

    # -- derived ---------------------------------------------------------------
    @property
    def ok(self) -> bool:
        return self.status.produced_output

    @property
    def empty_output(self) -> bool:
        """An empty successful response is a failure to answer, not a success."""
        return self.status.produced_output and not self.response_text.strip()

    @property
    def output_budget_exhausted(self) -> bool | None:
        """Did this generation consume its whole output budget? ``None`` = unmeasured.

        Delegates the semantics to :attr:`FinishReason.output_budget_exhausted` — there
        is exactly one table — and adds the one thing a finish reason cannot know: a
        result that produced no output at all (failed, blocked, timed out, interrupted,
        unsupported) has not been *measured* for this property. It is excluded from the
        metric's denominator rather than counted as a clean completion, which is the
        distinction :data:`_OUTPUT_BUDGET_EXHAUSTION`'s docstring exists to preserve.

        Derived, never stored: the underlying evidence is ``finish_reason``, which every
        result already persists body-free. D38 adds no new generation-side state.
        """
        if not self.status.produced_output:
            return None
        return self.finish_reason.output_budget_exhausted

    def oversized(self, ceilings: ResourceCeilings) -> bool:
        return len(self.response_text) > ceilings.max_response_chars

    def to_dict(self) -> dict:
        return {
            "protocol_version": EVALUATION_BACKEND_PROTOCOL_VERSION,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "role": self.role.value,
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "status": self.status.value,
            "response_sha256": sha256_obj(self.response_text),
            "response_chars": len(self.response_text),
            "proposed_tool_calls": [dict(sorted(c.items()))
                                    for c in self.proposed_tool_calls],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_truncated": self.input_truncated,
            "truncated_tokens": self.truncated_tokens,
            "latency_ms": self.latency_ms,
            "peak_memory_category": self.peak_memory_category,
            "finish_reason": self.finish_reason.value,
            "timed_out": self.timed_out,
            "interrupted": self.interrupted,
            "error_category": self.error_category.value,
            "error_message": self.error_message,
            "cleanup_status": self.cleanup_status.value,
            "warnings": list(self.warnings),
            "request_parity_hash": self.request_parity_hash,
        }

    def result_hash(self) -> str:
        """Digest over the outcome INCLUDING the response text.

        ``to_dict`` publishes only the response's digest, because a results artefact
        that quoted every model output would be a large file full of unreviewed text.
        The hash covers the text itself so two identical-looking results whose responses
        differ do not share an identity.
        """
        return sha256_obj({**self.to_dict(), "response_text": self.response_text})

    def to_record(self) -> dict:
        record = {**self.to_dict(), "result_hash": self.result_hash()}
        assert_no_private_content(record, label="evaluation result")
        return record


def output_budget_consistency_problems(result: EvaluationResult, *,
                                       max_new_tokens: int) -> tuple[str, ...]:
    """Cross-check a result's finish reason against its own generated-token count.

    The relationship was read from the production backend rather than assumed:
    ``transformers_peft`` sets ``MAX_NEW_TOKENS`` exactly when
    ``output_tokens >= policy.max_new_tokens`` — a ``>=``, not an ``==``, so a backend
    that appends a terminator inside the budget is still classified correctly.

    Deliberately a diagnostic that RETURNS problems rather than a production assertion.
    The backend that owns the comparison cannot check itself with it without being
    circular; what this is for is a *foreign* result — a replayed record, a second
    backend — where the two facts could genuinely disagree. A disagreement is reported,
    never silently relabelled.

    ``output_tokens < 0`` means the backend did not count them, so nothing is claimed.
    """
    if not result.status.produced_output or result.output_tokens < 0:
        return ()
    limit = int(max_new_tokens)
    exhausted = result.finish_reason is FinishReason.MAX_NEW_TOKENS
    reached = result.output_tokens >= limit
    if exhausted and not reached:
        return (f"result {result.task_id!r}: finish_reason says the output budget was "
                f"exhausted but only {result.output_tokens} of {limit} tokens were "
                f"generated; one of the two is describing a different generation",)
    if reached and not exhausted:
        return (f"result {result.task_id!r}: {result.output_tokens} of {limit} output "
                f"tokens were generated and finish_reason says "
                f"{result.finish_reason.value}; a generation that reached its ceiling "
                f"and reports terminating by itself would be counted as clean",)
    return ()


def check_result_matches_request(result: EvaluationResult,
                                 request: EvaluationRequest) -> None:
    """Refuse a result that does not answer the request that was sent.

    Three independent bindings, because each catches a different mistake: the task id
    catches a misfiled result, the task hash catches a result for a stale version of the
    task, and the parity hash catches a result produced under a different prompt or
    decoding policy than the one this request carried.
    """
    if result.task_id != request.task.task_id:
        raise EvaluationBackendError(
            f"evaluation: a result for {result.task_id!r} was returned for a request "
            f"about {request.task.task_id!r}; filing it would attribute one model's "
            f"answer to another question")
    if result.task_hash != request.task.task_hash:
        raise EvaluationBackendError(
            f"evaluation: result for {result.task_id!r} names task "
            f"{short(result.task_hash)} but the request carried "
            f"{short(request.task.task_hash)}")
    if result.role is not request.role:
        raise EvaluationBackendError(
            f"evaluation: a {result.role.value} result was returned for a "
            f"{request.role.value} request; the arms would be swapped and the reported "
            f"delta would have the wrong sign")
    expected = request.parity_hash()
    if result.request_parity_hash and result.request_parity_hash != expected:
        raise EvaluationBackendError(
            f"evaluation: result for {result.task_id!r} was produced under request "
            f"{short(result.request_parity_hash)} but this request is {short(expected)}; "
            f"the two arms were not asked the same question")


# ══════════════════════════════════════════════════════════════════════════════
#  The protocol
# ══════════════════════════════════════════════════════════════════════════════
class EvaluationBackend:
    """What every evaluation backend must be. Deliberately a small surface.

    Not a ``typing.Protocol`` alone: :func:`require_backend` checks the shape
    structurally, because a Protocol's ``isinstance`` check is satisfied by an object
    that merely has attributes of the right names, and a backend that will be handed a
    verified adapter deserves more than a name check.
    """

    backend_id: str = ""

    def version(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def readiness(self, request: EvaluationRequest) -> tuple[str, ...]:  # pragma: no cover
        raise NotImplementedError

    def generate(self, request: EvaluationRequest) -> EvaluationResult:  # pragma: no cover
        raise NotImplementedError

    def release(self) -> CleanupStatus:  # pragma: no cover - interface
        raise NotImplementedError


def require_backend(candidate: object) -> EvaluationBackend:
    """Structural check. Refuses an object that only looks like a backend by name."""
    backend_id = getattr(candidate, "backend_id", None)
    if not isinstance(backend_id, str) or not backend_id:
        raise EvaluationBackendError(
            "evaluation backend: backend_id must be a non-empty string; a backend that "
            "cannot name itself cannot be attributed in a report")
    for name in ("version", "readiness", "generate", "release"):
        if not callable(getattr(candidate, name, None)):
            raise EvaluationBackendError(
                f"evaluation backend {backend_id!r}: missing callable {name!r}")
    return candidate  # type: ignore[return-value]


def blocked_result(request: EvaluationRequest, *, backend_id: str,
                   backend_version: str, reasons: tuple[str, ...],
                   category: BackendErrorCategory = BackendErrorCategory.CONFIGURATION
                   ) -> EvaluationResult:
    """The result a backend returns when a preflight gate refused it.

    A refusal is a RESULT, not an exception: the task must stay in the denominator, and
    a raised exception is how a refused task quietly leaves the sample.
    """
    return EvaluationResult(
        backend_id=backend_id, backend_version=backend_version, role=request.role,
        task_id=request.task.task_id, task_hash=request.task.task_hash,
        status=BackendStatus.BLOCKED, error_category=category,
        error_message="; ".join(reasons)[:2_000], finish_reason=FinishReason.ERROR,
        cleanup_status=CleanupStatus.NOT_REQUIRED,
        request_parity_hash=request.parity_hash())


__all__ = [
    "EVALUATION_BACKEND_PROTOCOL_VERSION", "BackendErrorCategory", "BackendStatus",
    "CancellationToken", "CleanupStatus", "EvaluationBackend",
    "EvaluationBackendError", "EvaluationRequest", "EvaluationResult",
    "FinishReason", "InterruptionRequested", "blocked_result",
    "check_result_matches_request", "output_budget_consistency_problems",
    "require_backend",
]
