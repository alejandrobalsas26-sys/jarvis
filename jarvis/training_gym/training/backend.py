"""training_gym/training/backend.py — V69 M62 S3B: the seam a trainer plugs into.

WHY A CLOSED PROTOCOL AND NO PLUGIN PATH
----------------------------------------
There is no config field and no CLI flag that names a backend module, class or executable.
A configurable backend is an arbitrary-code-execution primitive wearing a settings file:
whoever can write the config chooses what runs inside the process that just loaded a
verified dataset and holds the operator's approval. Backends are selected by an internal
call, from a fixed registry, and the test double is reachable only from test code.

WHY THE RESULT IS TYPED
-----------------------
A backend returns a :class:`BackendResult`, not a dictionary. A raw mapping is a shape
nobody validates, so "did it work" becomes a key lookup that defaults to something. Here
the ceiling checks, the finiteness checks and the step-count checks happen in
``__post_init__``, which means a backend cannot report a NaN loss or more completed steps
than it attempted, whatever it believes.

WHY SUCCESS IS NOT COMPLETION
-----------------------------
``BackendStatus.SUCCEEDED`` means the trainer returned without raising. It does not mean
an adapter exists, that the adapter is a LoRA adapter, or that the files are safe. The
run state machine has no edge from RUNNING to COMPLETED for exactly this reason: the
claim goes to :mod:`training_gym.training.artifacts`, and the bytes decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..schemas import SchemaError, assert_no_private_content, require_id, sha256_obj
from .config import TrainingConfig, TrainingMethod
from .plan import TrainingPlan

#: Bumped when the backend contract changes.
BACKEND_PROTOCOL_VERSION = "m62.training_backend.1"

#: A backend may not report more progress events than this, whatever it is doing.
MAX_PROGRESS_EVENTS = 5_000


class BackendError(SchemaError):
    """A backend that will not be used, or a result that will not be believed."""


class BackendStatus(str, Enum):
    """What the trainer did. Never whether an adapter is valid — bytes decide that."""

    SUCCEEDED = "succeeded"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ExecutionRequest:
    """Everything one bounded run is a function of.

    One object rather than a parameter list, because the guarantee the execution stage
    makes is that the plan it recomputed is the plan the backend acts on. Two signatures
    that could drift apart would let those become two different descriptions.
    """

    config: TrainingConfig
    plan: TrainingPlan
    run_directory: Path
    train_file: Path
    validation_file: Path | None
    device: str
    precision: str
    allow_model_download: bool = False
    model_cache_root: Path | None = None

    def __post_init__(self) -> None:
        if self.config.method is not TrainingMethod(self.plan.method):
            raise BackendError(
                "execution request: the config's method and the plan's method differ; "
                "the plan is what the operator approved and the config is what would "
                "run, and they must be the same thing")


class InterruptionRequested(BackendError):
    """Cooperative cancellation. Raised by a backend that noticed a stop request."""


@dataclass
class CancellationToken:
    """The only channel through which a backend is asked to stop.

    A flag rather than a signal or a thread kill: a handler racing a trainer over the
    same files is worse than the interrupt it is handling. The backend checks between
    steps and returns, so every partial write is already finished when it does.
    """

    _requested: bool = False
    reason: str = ""

    def request(self, reason: str = "") -> None:
        self._requested = True
        self.reason = str(reason)[:200]

    @property
    def requested(self) -> bool:
        return self._requested

    def raise_if_requested(self) -> None:
        if self._requested:
            raise InterruptionRequested(
                f"training: stop requested ({self.reason or 'no reason given'})")


@dataclass(frozen=True)
class BackendResult:
    """What a run actually did, including the parts that did not work."""

    backend_id: str
    backend_version: str
    status: BackendStatus
    steps_attempted: int = 0
    steps_completed: int = 0
    epochs_completed: float = 0.0
    train_loss: float = 0.0
    eval_loss: float = 0.0
    duration_seconds: float = 0.0
    interrupted: bool = False
    error_category: str = "none"
    error_message: str = ""
    output_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    package_versions: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    truncated_records: int = 0
    converted_records: int = 0

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "backend_id", require_id(self.backend_id, "backend.backend_id"))
        setattr_(self, "status", BackendStatus(self.status))
        for name in ("steps_attempted", "steps_completed", "truncated_records",
                     "converted_records"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BackendError(f"backend.{name}: expected a non-negative integer")
        if self.steps_completed > self.steps_attempted:
            raise BackendError(
                f"backend: reports {self.steps_completed} completed steps out of "
                f"{self.steps_attempted} attempted; a backend cannot finish more work "
                f"than it started")
        for name in ("epochs_completed", "train_loss", "eval_loss", "duration_seconds"):
            value = float(getattr(self, name))
            if value != value or value in (float("inf"), float("-inf")):
                raise BackendError(
                    f"backend.{name}: is {value}; a run whose metrics are not finite "
                    f"diverged, and a diverged run has no result to report")
            setattr_(self, name, value)
        if self.duration_seconds < 0:
            raise BackendError("backend.duration_seconds: is negative")
        for name in ("output_files", "warnings"):
            setattr_(self, name, tuple(str(v) for v in getattr(self, name)))
        for name in self.output_files:
            if "/" in name or "\\" in name or Path(name).is_absolute():
                raise BackendError(
                    f"backend.output_files: {name!r} is a path; a backend reports the "
                    f"names it wrote inside its run directory and never where that is")
        setattr_(self, "package_versions", dict(sorted(
            (str(k), str(v)) for k, v in dict(self.package_versions).items())))
        setattr_(self, "evidence", dict(sorted(dict(self.evidence).items())))

    @property
    def ok(self) -> bool:
        """The trainer returned cleanly. Says nothing about the artifacts."""
        return self.status is BackendStatus.SUCCEEDED

    def metric_problems(self) -> tuple[str, ...]:
        """Every reason these numbers describe a run that must not be completed."""
        problems: list[str] = []
        if self.status is BackendStatus.SUCCEEDED:
            if self.steps_completed <= 0:
                problems.append(
                    "the backend reports success with zero completed steps; a run that "
                    "took no optimisation step produced the adapter it started with")
            if self.converted_records <= 0:
                problems.append(
                    "the backend reports success over zero training records")
            if self.train_loss <= 0.0:
                problems.append(
                    f"the backend reports a train loss of {self.train_loss}; a "
                    f"non-positive cross-entropy is not a measurement of anything")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "backend_id": self.backend_id, "backend_version": self.backend_version,
            "protocol_version": BACKEND_PROTOCOL_VERSION,
            "status": self.status.value,
            "steps_attempted": self.steps_attempted,
            "steps_completed": self.steps_completed,
            "epochs_completed": round(self.epochs_completed, 6),
            "train_loss": round(self.train_loss, 6),
            "eval_loss": round(self.eval_loss, 6),
            "duration_seconds": round(self.duration_seconds, 3),
            "interrupted": self.interrupted,
            "error_category": self.error_category,
            "error_message": self.error_message[:300],
            "output_files": list(self.output_files),
            "warnings": list(self.warnings),
            "package_versions": dict(self.package_versions),
            "evidence": dict(self.evidence),
            "truncated_records": self.truncated_records,
            "converted_records": self.converted_records,
        }

    def to_record(self) -> dict:
        record = {**self.to_dict(), "result_hash": sha256_obj(self.to_dict())}
        assert_no_private_content(record, label="backend result")
        return record


@runtime_checkable
class TrainingBackend(Protocol):
    """The complete surface a training backend exposes. There is no other entry point."""

    backend_id: str

    def version(self) -> str:
        """The backend's own version string. Recorded in the adapter manifest."""

    def supports(self, method: TrainingMethod) -> bool:
        """Whether this backend can execute *method* at all, on any host."""

    def readiness(self, request: ExecutionRequest) -> tuple[str, ...]:
        """Every reason this exact request cannot run here. Empty means ready.

        Called before the plan is spent, so a backend that cannot run must say so while
        refusing is still free.
        """

    def execute(self, request: ExecutionRequest, *,
                cancellation: CancellationToken) -> BackendResult:
        """Perform one bounded run and return what happened. Never raises for a
        training failure — a failure is a result, and only a broken contract raises."""


def require_backend(candidate: object) -> TrainingBackend:
    """Structural validation, mirroring the sandbox layer's ``require_backend``.

    ``isinstance`` against a ``Protocol`` only checks that the names exist; this checks
    they are callable, which is the difference between an object that satisfies a type
    checker and one that can actually be run.
    """
    if not isinstance(getattr(candidate, "backend_id", None), str):
        raise BackendError("backend: must expose a string backend_id")
    for name in ("version", "supports", "readiness", "execute"):
        if not callable(getattr(candidate, name, None)):
            raise BackendError(f"backend: must implement {name}()")
    return candidate  # type: ignore[return-value]


__all__ = [
    "BACKEND_PROTOCOL_VERSION", "MAX_PROGRESS_EVENTS", "BackendError", "BackendResult",
    "BackendStatus", "CancellationToken", "ExecutionRequest", "InterruptionRequested",
    "TrainingBackend", "require_backend",
]
