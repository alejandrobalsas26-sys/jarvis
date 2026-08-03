"""training_gym/training/run_store.py — V69 M62 S3B: what a run is, and when it is one.

WHY THE PLAN LEDGER LIVES OUTSIDE THE RUN DIRECTORY
---------------------------------------------------
S3A's only defence against a second run under one confirmation was
:func:`~training_gym.training.plan.check_output_root`, which refuses an existing run
directory. That is an existence check, and existence checks are reversible: delete the
directory and the plan re-derives byte-identically, the same ``TRAIN:`` token validates,
and the run happens again. The ledger is deliberately at the OUTPUT ROOT rather than
inside ``runs/<run_id>/`` so that deleting the artifacts destroys the evidence of a run
and leaves the record that the plan was spent.

WHY A TRAINING PLAN IS CONSUMED *BEFORE* THE WORK, NOT AFTER
------------------------------------------------------------
:func:`~training_gym.datasets.promotion_plan.promote` consumes its plan after the version
exists, because writing a dataset version is short and all-or-nothing. Training is
neither. Consuming afterwards would mean an interrupt at ninety percent leaves the plan
unconsumed and infinitely replayable, and a crash loop retrains forever. So the plan is
spent immediately before the first irreversible act, and the terminal outcome is appended
as a SECOND line. The ledger is append-only, so the first line is never rewritten and
"this plan was started" survives every possible ending.

WHY THERE IS NO DIRECTORY RENAME
--------------------------------
The obvious design is to train into ``.partial-<id>`` and rename it into place. That does
not work here. ``os.replace`` is atomic for a FILE within one directory; on Windows it
cannot replace an existing directory at all, and it fails with a sharing violation if any
process holds a handle to any file in the tree — which is the normal case for a
memory-mapped ``safetensors`` file, an antivirus scanner or the search indexer. A
mechanism that works on CI Linux and fails on the operator's actual host is worse than no
mechanism, because it fails exactly when it is load-bearing.

The commit point is a file instead, which is the pattern
:func:`~training_gym.datasets.manifests.write_dataset_version` already proved: the run
directory is created with ``exist_ok=False`` (simultaneously the no-overwrite rule and the
mutual exclusion — two concurrent runs race one atomic ``mkdir`` and exactly one wins),
every artifact is written atomically, and the adapter manifest is written LAST. A run
directory without a verifiable manifest is residue, not a run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..atomicio import AtomicIOError, append_jsonl, atomic_write_text, read_jsonl
from ..task_spec import require_timestamp
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    assert_no_private_content,
    canonical_json,
    require_id,
    sha256_obj,
    short,
)
from .config import TrainingRunState, check_training_transition
from .plan import (
    MAX_TRAINING_LEDGER_LINES,
    TRAINING_LEDGER_FILE,
    TrainingPlanError,
    training_run_directory,
)

#: Bumped when the shape of a run record changes.
RUN_RECORD_VERSION = "m62.training_run.1"

#: The in-progress state file. Never authoritative for completion — that is the adapter
#: manifest's job — but it is what an operator reads to find out what a directory is.
RUN_STATE_FILE = "run.json"

#: The append-only event log for one run.
RUN_EVENTS_FILE = "training_log.jsonl"
MAX_RUN_EVENTS = 10_000

#: A single event message may not become a channel for dataset material or a traceback.
MAX_EVENT_MESSAGE_CHARS = 300

#: Where a run that failed or was interrupted is moved so it cannot be mistaken for one
#: that is merely unfinished.
QUARANTINE_DIR = "quarantine"


class RunStoreError(TrainingPlanError):
    """A run record that will not be written or acted on as described."""


class PlanAlreadyConsumed(RunStoreError):
    """This plan's digest has already started a training run."""


# ══════════════════════════════════════════════════════════════════════════════
#  Events
# ══════════════════════════════════════════════════════════════════════════════
class RunEventKind(str, Enum):
    """The closed vocabulary of things a run may report having done."""

    PREFLIGHT_STARTED = "preflight_started"
    PREFLIGHT_PASSED = "preflight_passed"
    PLAN_CONSUMED = "plan_consumed"
    RUNTIME_IMPORT_STARTED = "runtime_import_started"
    RUNTIME_READY = "runtime_ready"
    DATASET_CONVERTED = "dataset_converted"
    MODEL_LOADED = "model_loaded"
    TRAINING_STARTED = "training_started"
    STEP_PROGRESS = "step_progress"
    TRAINING_FINISHED = "training_finished"
    ARTIFACT_VALIDATION_STARTED = "artifact_validation_started"
    ARTIFACT_VALIDATION_PASSED = "artifact_validation_passed"
    INTERRUPTION_REQUESTED = "interruption_requested"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_FAILED = "run_failed"
    RUN_QUARANTINED = "run_quarantined"
    RUN_COMPLETED = "run_completed"


class ErrorCategory(str, Enum):
    """Why a run stopped, in a vocabulary an operator can act on."""

    NONE = "none"
    CONFIGURATION = "configuration"
    DATASET = "dataset"
    DEPENDENCY = "dependency"
    HARDWARE = "hardware"
    STALE_PLAN = "stale_plan"
    CONFIRMATION = "confirmation"
    REPLAY = "replay"
    UNSUPPORTED_METHOD = "unsupported_method"
    MODEL_ACCESS = "model_access"
    TOKENIZER = "tokenizer"
    BACKEND = "backend"
    OUT_OF_MEMORY = "out_of_memory"
    DISK_FULL = "disk_full"
    INTERRUPTED = "interrupted"
    INVALID_METRICS = "invalid_metrics"
    ARTIFACT = "artifact"
    INTERNAL = "internal"


# ══════════════════════════════════════════════════════════════════════════════
#  The plan-consumption ledger
# ══════════════════════════════════════════════════════════════════════════════
def training_ledger_path(root: str | Path) -> Path:
    return Path(root) / TRAINING_LEDGER_FILE


def _refuse_linked_ledger(path: Path) -> None:
    """A ledger behind a link is a ledger somebody else can empty.

    Checked before every read as well as every write: relinking the file elsewhere, or
    deleting the target of a link, would un-consume every plan ever run — which is the
    one thing this file exists to make impossible.
    """
    if path.is_symlink():
        raise RunStoreError(
            f"{path.name}: is a symlink; the ledger that is read and the ledger that is "
            f"written must be the same file, or a relink un-consumes every plan")
    if path.is_file() and getattr(path.stat(), "st_nlink", 1) > 1:
        raise RunStoreError(
            f"{path.name}: has more than one hard link; a second name for the ledger is "
            f"a second way to truncate it")


def training_entries(root: str | Path) -> list[dict]:
    path = training_ledger_path(root)
    _refuse_linked_ledger(path)
    try:
        return read_jsonl(path)
    except AtomicIOError as exc:
        raise RunStoreError(str(exc)) from None


def is_plan_consumed(root: str | Path, plan_hash: str) -> bool:
    """True once ANY line records this plan as started, whatever happened afterwards.

    Keyed on the ``started`` line alone. A failed run, an interrupted run and a completed
    run are all runs: each of them read the dataset, each of them may have written bytes,
    and none of them may be repeated on the same operator approval.
    """
    digest = str(plan_hash).strip().lower()
    if len(digest) != 64:
        raise RunStoreError("training ledger: a plan hash is 64 hex characters")
    return any(str(entry.get("plan_hash", "")) == digest
               and str(entry.get("event", "")) == "started"
               for entry in training_entries(root))


def consume_plan(root: str | Path, *, plan_hash: str, run_id: str, actor: str,
                 at: str, method: str) -> dict:
    """Spend a plan. Refuses if it was already spent, and never spends it twice.

    The check and the append are not atomic against a concurrent process; the run
    directory's ``mkdir(exist_ok=False)`` is what serialises that race, and it happens
    first. This is the durable record, not the lock.
    """
    entry = _ledger_entry(plan_hash=plan_hash, run_id=run_id, actor=actor, at=at,
                          event="started", method=method)
    if is_plan_consumed(root, plan_hash):
        raise PlanAlreadyConsumed(
            f"training: plan {short(plan_hash)} has already started a run; a "
            f"confirmation authorises one run, and replaying it would train again on an "
            f"approval nobody gave twice. Re-plan to obtain a new token")
    _append(root, entry)
    return entry


def record_terminal(root: str | Path, *, plan_hash: str, run_id: str, actor: str,
                    at: str, state: TrainingRunState, method: str) -> dict:
    """Append the outcome as a SECOND line. The ``started`` line is never rewritten."""
    if state in (TrainingRunState.CREATED, TrainingRunState.RUNNING):
        raise RunStoreError(f"training ledger: {state.value} is not a terminal outcome")
    entry = _ledger_entry(plan_hash=plan_hash, run_id=run_id, actor=actor, at=at,
                          event=state.value, method=method)
    _append(root, entry)
    return entry


def _ledger_entry(*, plan_hash: str, run_id: str, actor: str, at: str, event: str,
                  method: str) -> dict:
    digest = str(plan_hash).strip().lower()
    if len(digest) != 64:
        raise RunStoreError("training ledger: a plan hash is 64 hex characters")
    return {
        SCHEMA_KEY: SCHEMA_VERSION,
        "record_version": RUN_RECORD_VERSION,
        "plan_hash": digest,
        "run_id": require_id(run_id, "ledger.run_id"),
        "method": require_id(method, "ledger.method"),
        "event": event,
        # An operator id, never a username or a host. The CLI supplies a safe label.
        "actor": require_id(actor, "ledger.actor"),
        "at": require_timestamp(at, "ledger.at"),
    }


def _append(root: str | Path, entry: dict) -> None:
    path = training_ledger_path(root)
    _refuse_linked_ledger(path)
    assert_no_private_content(entry, label="training ledger entry")
    try:
        append_jsonl(path, entry, max_lines=MAX_TRAINING_LEDGER_LINES)
    except AtomicIOError as exc:
        # Refusing loudly is the only honest outcome. An unrecorded start is a plan that
        # can be replayed, which is the failure this ledger exists to prevent.
        raise RunStoreError(
            f"training: the run ledger could not be appended ({exc}); refusing to "
            f"proceed, because a run nobody recorded is a plan that can be spent "
            f"again") from None


# ══════════════════════════════════════════════════════════════════════════════
#  The run record
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class RunRecord:
    """One run's state and its append-only history. Mutable by design, on purpose.

    Every other record in this milestone is frozen. This one is not, because a run is the
    single thing in the gym that genuinely changes over time — and pretending otherwise
    would mean rebuilding the object on every transition and losing the guarantee that
    the history and the state came from the same place.
    """

    run_id: str
    plan_hash: str
    training_config_hash: str
    method: str
    created_at_utc: str
    state: TrainingRunState = TrainingRunState.CREATED
    history: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    error_category: ErrorCategory = ErrorCategory.NONE

    def __post_init__(self) -> None:
        self.run_id = require_id(self.run_id, "run.run_id")
        for name in ("plan_hash", "training_config_hash"):
            value = str(getattr(self, name)).strip().lower()
            if len(value) != 64:
                raise RunStoreError(f"run.{name}: expected a 64-character digest")
            setattr(self, name, value)

    # -- transitions -----------------------------------------------------------
    def transition(self, target: TrainingRunState, *, at: str, reason: str = "") -> None:
        """Move to *target*, or refuse. The only way this object's state changes.

        Every move is recorded, so ``state`` is never the only evidence that it happened.
        A record whose history does not contain the edge into its current state is a
        record whose state was SET rather than REACHED, and
        :meth:`history_supports_state` is what tells them apart.
        """
        check_training_transition(self.state, target)
        self.history.append({
            "from": self.state.value, "to": target.value,
            "at": _timestamp(at), "reason": _bounded(reason)})
        self.state = target

    def history_supports_state(self) -> bool:
        """Whether the recorded edges actually walk from CREATED to the current state."""
        current = TrainingRunState.CREATED
        for step in self.history:
            if step.get("from") != current.value:
                return False
            try:
                target = TrainingRunState(step.get("to"))
            except ValueError:
                return False
            try:
                check_training_transition(current, target)
            except SchemaError:
                return False
            current = target
        return current is self.state

    @property
    def is_complete(self) -> bool:
        return self.state is TrainingRunState.COMPLETED

    @property
    def interrupted(self) -> bool:
        return self.state in (TrainingRunState.INTERRUPTING,
                              TrainingRunState.INTERRUPTED)

    # -- events ----------------------------------------------------------------
    def emit(self, kind: RunEventKind, *, at: str, message: str = "",
             **numbers: float) -> dict:
        """Append one bounded, typed event.

        Only numbers may accompany an event. A free-form payload is how a prompt, a
        target, a path or a token ends up in a log that an operator later pastes into a
        ticket, so there is no parameter that could carry one.
        """
        if len(self.events) >= MAX_RUN_EVENTS:
            raise RunStoreError(
                f"run {self.run_id}: {MAX_RUN_EVENTS}-event ceiling reached; refusing to "
                f"grow an unbounded log")
        event = {
            "event": RunEventKind(kind).value,
            "at": _timestamp(at),
            "state": self.state.value,
            "message": _bounded(message),
            **{key: _number(value, key) for key, value in sorted(numbers.items())},
        }
        assert_no_private_content(event, label=f"run event {event['event']}")
        self.events.append(event)
        return event

    # -- serialization ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "record_version": RUN_RECORD_VERSION,
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "training_config_hash": self.training_config_hash,
            "method": self.method,
            "created_at_utc": self.created_at_utc,
            "state": self.state.value,
            "error_category": self.error_category.value,
            "history": [dict(sorted(step.items())) for step in self.history],
            "completed": self.is_complete,
            "interrupted": self.interrupted,
        }

    def record_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "record_hash": self.record_hash()}
        assert_no_private_content(record, label="training run record")
        return record


def _timestamp(value: str) -> str:
    return require_timestamp(value, "run.at")


def _bounded(text: object) -> str:
    """A message, truncated. Never a traceback and never a dataset row."""
    return str(text or "")[:MAX_EVENT_MESSAGE_CHARS]


def _number(value: object, field_name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunStoreError(
            f"run event: {field_name} must be a number; an event carries measurements "
            f"and never text, because text is how a prompt reaches a log")
    if value != value or value in (float("inf"), float("-inf")):
        raise RunStoreError(f"run event: {field_name} is not a finite number")
    return value


# ══════════════════════════════════════════════════════════════════════════════
#  The run directory
# ══════════════════════════════════════════════════════════════════════════════
def create_run_directory(root: str | Path, run_id: str) -> Path:
    """Create ``<root>/runs/<run_id>`` exclusively, or refuse.

    ``exist_ok=False`` is doing two jobs: it is the rule that a run id never silently
    overwrites another, and it is the mutual exclusion between two concurrent runs of the
    same plan. Exactly one process wins the ``mkdir``; the loser refuses before it has
    spent anything.
    """
    directory = training_run_directory(root, run_id)
    parent = directory.parent
    for component in (Path(root), parent):
        if component.is_symlink():
            raise RunStoreError(
                f"training: {component.name!r} on the output path is a symlink; the "
                f"directory that was reviewed and the directory that is written must be "
                f"the same one")
    if directory.exists() or directory.is_symlink():
        raise RunStoreError(
            f"training: a run directory for {run_id!r} already exists; choose a new run "
            f"id rather than overwriting artifacts nobody can then attribute")
    parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)
    return directory


def write_run_state(directory: Path, record: RunRecord) -> None:
    """Persist ``run.json``. Never the completion evidence — only the current story."""
    atomic_write_text(directory / RUN_STATE_FILE, canonical_json(record.to_record()))


def write_run_events(directory: Path, record: RunRecord) -> None:
    """Rewrite the bounded event log. Small, capped, and never the audit authority."""
    atomic_write_text(directory / RUN_EVENTS_FILE,
                      "".join(f"{canonical_json(e)}\n" for e in record.events))


def quarantine_run_directory(root: str | Path, directory: Path, *,
                             run_id: str, nonce: str) -> tuple[str, str]:
    """Move a failed or interrupted run out of ``runs/`` and report where it went.

    Renaming a directory is unreliable on this platform, so a failure to move is
    reported rather than swallowed: residue an operator knows about is recoverable, and
    residue they were told was cleaned up is not.
    """
    target = Path(root) / QUARANTINE_DIR / f"{require_id(run_id, 'run_id')}-{nonce}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.rename(target)
    except OSError as exc:
        return ("not_moved",
                f"the run directory could not be quarantined ({type(exc).__name__}); it "
                f"remains under runs/ and holds no adapter manifest, so it reads as "
                f"residue rather than as a run — remove it by hand")
    return (f"{QUARANTINE_DIR}/{target.name}", "")


__all__ = [
    "MAX_EVENT_MESSAGE_CHARS", "MAX_RUN_EVENTS", "QUARANTINE_DIR",
    "RUN_EVENTS_FILE", "RUN_RECORD_VERSION", "RUN_STATE_FILE", "ErrorCategory",
    "PlanAlreadyConsumed", "RunEventKind", "RunRecord", "RunStoreError",
    "consume_plan", "create_run_directory", "is_plan_consumed",
    "quarantine_run_directory", "record_terminal", "training_entries",
    "training_ledger_path", "write_run_events", "write_run_state",
]
