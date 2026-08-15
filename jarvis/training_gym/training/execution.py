"""training_gym/training/execution.py — V69 M62 S3B: the only path to a training loop.

THE ORDER IS THE CONTRACT
-------------------------
1. Recompute the plan from the CURRENT state of the world. Not the plan the operator was
   shown — the one the inputs produce now. The confirmation is checked against that, so a
   dataset edited after approval, a bumped package, a changed revision or a different
   measured device all invalidate the token rather than silently changing the run.
2. Ask the backend whether it can run this, before anything is spent.
3. Create the run directory with ``exist_ok=False``. This is the mutual exclusion: two
   concurrent processes race one atomic ``mkdir`` and exactly one continues.
4. Consume the plan. From here the approval is spent whatever happens, because a run that
   read the dataset and imported a framework is a run.
5. Convert the corpus, import the framework, train.
6. Validate the artifacts against the plan.
7. Write the adapter manifest LAST. Its presence is what "completed" means.

Steps 1 through 3 are free: a failure there leaves no directory, no ledger line and no
imported framework. Step 4 is the point of no return, and it is deliberately placed after
every check that could have refused.

WHY FAILURE NEVER PRODUCES A COMPLETED DIRECTORY
-------------------------------------------------
There is no rename. The manifest is the commit point, and it is written after the bytes
are already correct. A run that fails, diverges, is interrupted or produces an artifact
the policy refuses is moved into ``quarantine/`` and never holds a manifest — so no
reader, at any later date, can mistake a partial ``adapter_model.safetensors`` for a
finished adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..schemas import SchemaError, assert_no_private_content, canonical_json, short
from ..atomicio import atomic_write_text
from .artifacts import (
    ADAPTER_MANIFEST_FILE,
    adapter_tree_hash,
    AdapterArtifactError,
    AdapterManifest,
    ArtifactFile,
    validate_adapter_directory,
    verify_completed_run,
)
from .backend import (
    BackendError,
    BackendResult,
    BackendStatus,
    CancellationToken,
    ExecutionRequest,
    InterruptionRequested,
    TrainingBackend,
    require_backend,
)
from .config import TrainingConfig, TrainingMethod, TrainingRunState
from .dataset_conversion import (
    DatasetConversionError,
    convert_sft_export,
)
from .plan import (
    CONFIRMATION_PREFIX,
    ConfirmationRejected,
    TrainingPlanError,
    check_training_confirmation,
    output_root_id,
)
from .planner import plan_training
from .run_store import (
    ErrorCategory,
    PlanAlreadyConsumed,
    RunEventKind,
    RunRecord,
    RunStoreError,
    consume_plan,
    create_run_directory,
    is_plan_consumed,
    quarantine_run_directory,
    record_terminal,
    write_run_events,
    write_run_state,
)

#: Bumped when the execution contract changes.
EXECUTION_VERSION = "m62.s3b.1"

#: The methods this stage will actually execute. DPO stays out: preference-data volume
#: and quality have not been qualified, there is no production DPO evaluation gate, and
#: the rejected side of every pair is material a human marked as the wrong answer.
EXECUTABLE_METHODS: frozenset[TrainingMethod] = frozenset({TrainingMethod.SFT_LORA,
                                                           TrainingMethod.SFT_QLORA})

#: Where the backend's own report is written, inside the run directory.
BACKEND_RESULT_FILE = "backend_result.json"


class ExecutionError(TrainingPlanError):
    """A run that will not start, or one whose result will not be recorded as complete."""


class ExecutionRefused(ExecutionError):
    """A preflight gate refused. Nothing was created and no plan was spent."""


@dataclass(frozen=True)
class ExecutionOutcome:
    """What the run did, in a form an operator can read and a ticket can carry."""

    run_id: str
    state: TrainingRunState
    plan_hash: str
    error_category: ErrorCategory = ErrorCategory.NONE
    problems: tuple[str, ...] = ()
    backend_result: BackendResult | None = None
    manifest_hash: str = ""
    adapter_files: tuple[str, ...] = ()
    quarantined_as: str = ""
    plan_consumed: bool = False

    @property
    def ok(self) -> bool:
        return self.state is TrainingRunState.COMPLETED

    def to_dict(self) -> dict:
        record = {
            "execution_version": EXECUTION_VERSION,
            "run_id": self.run_id, "state": self.state.value,
            "completed": self.ok, "plan_hash": self.plan_hash,
            "error_category": self.error_category.value,
            "problems": list(self.problems),
            "manifest_hash": self.manifest_hash,
            "adapter_files": list(self.adapter_files),
            "quarantined_as": self.quarantined_as,
            "plan_consumed": self.plan_consumed,
            "backend_result": (self.backend_result.to_dict()
                               if self.backend_result else None),
        }
        assert_no_private_content(record, label="execution outcome")
        return record


# ══════════════════════════════════════════════════════════════════════════════
#  Preflight
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Preflight:
    """The recomputed plan and everything that had to be true to reach it."""

    planning: object
    problems: tuple[str, ...] = ()
    category: ErrorCategory = ErrorCategory.NONE

    @property
    def ok(self) -> bool:
        return not self.problems


def run_preflight(config: TrainingConfig, *, dataset_root: str | Path,
                  output_root: str | Path, confirmation: object,
                  export_root: str | Path | None = None,
                  model_cache_root: str | Path | None = None,
                  allow_model_download: bool = False,
                  revoked_candidate_ids: frozenset[str] = frozenset(),
                  accelerator_probe=None,
                  backend: TrainingBackend | None = None,
                  train_file: str | Path | None = None,
                  validation_file: str | Path | None = None) -> Preflight:
    """Re-derive the plan and check the token against it. Creates nothing, spends nothing.

    Every refusal here is free. That is the whole point of doing all of it before the
    ``mkdir`` and the ledger append.
    """
    if config.method not in EXECUTABLE_METHODS:
        return Preflight(
            planning=None, category=ErrorCategory.UNSUPPORTED_METHOD,
            problems=(
                f"{config.method.value} is not executable in this stage. The planner "
                f"describes it and the executor refuses it: preference-data volume and "
                f"quality have not been qualified, no production evaluation gate exists "
                f"for it, and the rejected side of every pair is material a human marked "
                f"as the WRONG answer",))

    # Replay is checked against the digest the TOKEN carries, before anything is
    # recomputed. It has to be: the plan hash covers the plan's own blockers, so the
    # second attempt at a spent plan re-derives to a DIFFERENT hash — the run directory
    # now exists, which is itself a blocker — and a replay check keyed only on the
    # recomputed hash would never fire. "This token was already spent" is also the more
    # useful sentence than "that directory exists", because deleting the directory does
    # not make it untrue.
    spent = _token_digest(confirmation)
    if spent and is_plan_consumed(output_root, spent):
        return Preflight(
            planning=None, category=ErrorCategory.REPLAY,
            problems=(f"plan {short(spent)} has already started a run; a confirmation "
                      f"authorises one run, whatever that run's ending. Re-plan to "
                      f"obtain a new token",))

    planning = plan_training(
        config, dataset_root=dataset_root, output_root=output_root,
        model_cache_root=model_cache_root, export_root=export_root,
        accelerator_probe=accelerator_probe,
        allow_model_download_flag=allow_model_download,
        revoked_candidate_ids=revoked_candidate_ids)
    plan = planning.plan

    if plan.blockers:
        return Preflight(planning=planning, category=_blocker_category(plan.blockers),
                         problems=tuple(plan.blockers))

    # The token is checked against the plan recomputed above, never against the one the
    # operator read. This is what turns a signature over an intention into a signature
    # over a state of the world.
    try:
        check_training_confirmation(confirmation, plan)
    except ConfirmationRejected as exc:
        return Preflight(planning=planning, category=ErrorCategory.CONFIRMATION,
                         problems=(str(exc),))

    if plan.expected_output_root_id != output_root_id(output_root):
        return Preflight(  # pragma: no cover - the planner derives both from one root
            planning=planning, category=ErrorCategory.STALE_PLAN,
            problems=("the plan was built against a different output root",))

    if plan.model_download_required and not allow_model_download:
        return Preflight(
            planning=planning, category=ErrorCategory.MODEL_ACCESS,
            problems=(
                "the model weights are not known to be cached and no explicit download "
                "authorisation was supplied; a run that fetched them anyway would be "
                "reaching the network on an approval that did not mention one",))
    if plan.model_download_required and config.model_download_policy.value == "deny":
        return Preflight(
            planning=planning, category=ErrorCategory.MODEL_ACCESS,
            problems=(
                "the download flag was supplied but the reviewed config's policy is "
                "deny; the flag authorises what the config already permits and can never "
                "widen it",))

    if is_plan_consumed(output_root, plan.plan_hash()):
        return Preflight(
            planning=planning, category=ErrorCategory.REPLAY,
            problems=(f"plan {short(plan.plan_hash())} has already started a run; "
                      f"re-plan to obtain a new token",))

    if backend is not None:
        unready = tuple(require_backend(backend).readiness(_request(
            config, planning, Path(output_root), allow_model_download,
            model_cache_root, train_file, validation_file)))
        if unready:
            return Preflight(planning=planning, category=ErrorCategory.DEPENDENCY,
                             problems=unready)
    return Preflight(planning=planning)


def _token_digest(confirmation: object) -> str:
    """The plan digest a syntactically valid token names, or the empty string.

    Deliberately lenient: this is only used to look a digest up in the ledger, and the
    real validation is :func:`check_training_confirmation`. A malformed token simply has
    no digest to look up.
    """
    if isinstance(confirmation, bool) or not isinstance(confirmation, str):
        return ""
    token = confirmation.strip()
    if not token.startswith(CONFIRMATION_PREFIX):
        return ""
    digest = token[len(CONFIRMATION_PREFIX):].lower()
    return digest if len(digest) == 64 and all(
        c in "0123456789abcdef" for c in digest) else ""


def _blocker_category(blockers: tuple[str, ...]) -> ErrorCategory:
    """Map the first blocker onto the vocabulary an exit code is chosen from."""
    first = blockers[0] if blockers else ""
    for prefix, category in (("dataset:", ErrorCategory.DATASET),
                             ("dependency:", ErrorCategory.DEPENDENCY),
                             ("device:", ErrorCategory.HARDWARE),
                             ("precision:", ErrorCategory.HARDWARE),
                             ("output:", ErrorCategory.CONFIGURATION)):
        if first.startswith(prefix):
            return category
    return ErrorCategory.CONFIGURATION


def _request(config: TrainingConfig, planning, run_directory: Path,
             allow_model_download: bool, model_cache_root,
             train_file: str | Path | None = None,
             validation_file: str | Path | None = None) -> ExecutionRequest:
    """The request a backend's readiness check is asked about, before anything is spent.

    ``train_file`` is the corpus the run would actually read. It has to be the real one:
    a backend that checks whether its corpus is readable — which the production backend
    does, and which is the whole point of asking before the plan is spent — is answering
    about whatever it is handed. Passing the run directory here made that check compare a
    directory against ``is_file()`` and refuse every real run, while the fake backend used
    by the structural tests never looks at the field and so never noticed.

    ``validation_file`` is the same argument for the steering corpus, and it is
    ``None`` unless the reviewed config actually asks for train-time validation. It is
    threaded rather than defaulted for the reason the train file was: this is the request
    the readiness check answers about, so a config that enables validation against an
    absent or unreadable validation export must be refused HERE, while refusing is still
    free, rather than after the token is spent and the model is loaded.
    """
    return ExecutionRequest(
        config=config, plan=planning.plan, run_directory=run_directory,
        train_file=Path(train_file) if train_file is not None else run_directory,
        validation_file=(Path(validation_file) if validation_file is not None else None),
        device=planning.device.selected, precision=planning.precision.selected,
        allow_model_download=allow_model_download,
        model_cache_root=Path(model_cache_root) if model_cache_root else None)


# ══════════════════════════════════════════════════════════════════════════════
#  Execution
# ══════════════════════════════════════════════════════════════════════════════
def execute_training(config: TrainingConfig, *, dataset_root: str | Path,
                     output_root: str | Path, confirmation: object,
                     backend: TrainingBackend, train_file: str | Path,
                     actor: str, at: str, nonce: str,
                     validation_file: str | Path | None = None,
                     export_root: str | Path | None = None,
                     model_cache_root: str | Path | None = None,
                     allow_model_download: bool = False,
                     revoked_candidate_ids: frozenset[str] = frozenset(),
                     accelerator_probe=None,
                     expected_export_sha256: str = "",
                     expected_record_count: int = 0,
                     held_out_candidate_ids: frozenset[str] = frozenset(),
                     ) -> ExecutionOutcome:
    """Run one bounded training job under an exact plan-bound confirmation."""
    require_backend(backend)
    preflight = run_preflight(
        config, dataset_root=dataset_root, output_root=output_root,
        confirmation=confirmation, export_root=export_root,
        model_cache_root=model_cache_root,
        allow_model_download=allow_model_download,
        revoked_candidate_ids=revoked_candidate_ids,
        accelerator_probe=accelerator_probe, backend=backend,
        train_file=train_file, validation_file=validation_file)
    if not preflight.ok:
        return ExecutionOutcome(
            run_id=config.run_id, state=TrainingRunState.FAILED,
            plan_hash=(preflight.planning.plan.plan_hash()
                       if preflight.planning is not None else ""),
            error_category=preflight.category, problems=preflight.problems)

    planning = preflight.planning
    plan = planning.plan
    record = RunRecord(run_id=config.run_id, plan_hash=plan.plan_hash(),
                       training_config_hash=config.config_hash(),
                       method=config.method.value, created_at_utc=at)
    for state in (TrainingRunState.CONFIG_VALIDATED, TrainingRunState.DATASET_VERIFIED,
                  TrainingRunState.DEPENDENCIES_VERIFIED,
                  TrainingRunState.HARDWARE_VERIFIED, TrainingRunState.PLANNED,
                  TrainingRunState.AWAITING_CONFIRMATION,
                  TrainingRunState.PREFLIGHT_VERIFIED):
        record.transition(state, at=at, reason="preflight")
    record.emit(RunEventKind.PREFLIGHT_PASSED, at=at)

    # The mkdir is the lock and the ledger is the durable record. In that order: losing
    # the mkdir race must not spend the plan.
    try:
        directory = create_run_directory(output_root, config.run_id)
    except RunStoreError as exc:
        return ExecutionOutcome(
            run_id=config.run_id, state=TrainingRunState.FAILED,
            plan_hash=plan.plan_hash(), error_category=ErrorCategory.CONFIGURATION,
            problems=(str(exc),))

    try:
        consume_plan(output_root, plan_hash=plan.plan_hash(), run_id=config.run_id,
                     actor=actor, at=at, method=config.method.value)
    except (PlanAlreadyConsumed, RunStoreError) as exc:
        _remove_quietly(directory)
        return ExecutionOutcome(
            run_id=config.run_id, state=TrainingRunState.FAILED,
            plan_hash=plan.plan_hash(), error_category=ErrorCategory.REPLAY,
            problems=(str(exc),))

    record.transition(TrainingRunState.STARTING, at=at, reason="plan consumed")
    record.emit(RunEventKind.PLAN_CONSUMED, at=at)
    write_run_state(directory, record)
    return _run(record=record, directory=directory, planning=planning, config=config,
                backend=backend, output_root=Path(output_root),
                train_file=Path(train_file),
                validation_file=(Path(validation_file)
                                 if validation_file is not None else None),
                actor=actor, at=at, nonce=nonce,
                allow_model_download=allow_model_download,
                model_cache_root=model_cache_root,
                expected_export_sha256=expected_export_sha256,
                expected_record_count=expected_record_count,
                revoked_candidate_ids=revoked_candidate_ids,
                held_out_candidate_ids=held_out_candidate_ids)


def _run(*, record: RunRecord, directory: Path, planning, config: TrainingConfig,
         backend: TrainingBackend, output_root: Path, train_file: Path,
         validation_file: Path | None, actor: str,
         at: str, nonce: str, allow_model_download: bool, model_cache_root,
         expected_export_sha256: str, expected_record_count: int,
         revoked_candidate_ids: frozenset[str],
         held_out_candidate_ids: frozenset[str]) -> ExecutionOutcome:
    """Everything after the plan is spent. Every exit through one of the four endings."""
    plan = planning.plan
    cancellation = CancellationToken()
    result: BackendResult | None = None
    # An empty fingerprint fails towards suspicion, not away from it: if control ever
    # reached the escape check without this being taken, every entry in the root reads
    # as new and the run is refused rather than quietly passed.
    escape_before: dict[str, tuple] = {}

    try:
        dataset = convert_sft_export(
            train_file, max_sequence_length=config.max_sequence_length,
            expected_record_count=expected_record_count,
            expected_sha256=expected_export_sha256,
            revoked_candidate_ids=revoked_candidate_ids,
            held_out_candidate_ids=held_out_candidate_ids)
        record.emit(RunEventKind.DATASET_CONVERTED, at=at, records=len(dataset))
        record.transition(TrainingRunState.RUNNING, at=at, reason="backend started")
        write_run_state(directory, record)
        record.emit(RunEventKind.TRAINING_STARTED, at=at)

        request = ExecutionRequest(
            config=config, plan=plan, run_directory=directory, train_file=train_file,
            # The steering corpus, or None when the reviewed config asks for no
            # train-time validation. Hard-coded None here was the whole of defect D31:
            # the split was assigned, promoted, digest-bound into the plan and measured,
            # and the field that would have carried it to the trainer was written by the
            # caller as "there isn't one".
            validation_file=validation_file, device=planning.device.selected,
            precision=planning.precision.selected,
            allow_model_download=allow_model_download,
            model_cache_root=Path(model_cache_root) if model_cache_root else None)
        # Taken here and nowhere earlier: everything this authority writes outside the
        # run directory -- the consumed-plan line above, the terminal line below -- falls
        # outside the interval, so an append-only ledger write is never read as a backend
        # escape and never has to be special-cased into an exemption.
        escape_before, _ = _escape_snapshot(output_root, exclude=directory)
        result = backend.execute(request, cancellation=cancellation)
    except (InterruptionRequested, KeyboardInterrupt) as exc:
        return _interrupted(record=record, directory=directory, output_root=output_root,
                            plan=plan, actor=actor, at=at, nonce=nonce,
                            config=config, detail=str(exc)[:200])
    except DatasetConversionError as exc:
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.DATASET, problems=(str(exc),))
    except BackendError as exc:
        # A backend whose numbers cannot even be represented — a NaN loss, more steps
        # completed than attempted — never becomes a run. The typed result is the check.
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.INVALID_METRICS, problems=(str(exc),))
    except Exception as exc:  # noqa: BLE001 — see below; every ending must be a run state
        # Deliberately every remaining exception type, not an enumerated few. This block
        # used to name (SchemaError, OSError, RuntimeError, ValueError), which omitted
        # exactly the classes a framework API change raises: the first live smoke hit
        # `TypeError: TrainingArguments.__init__() got an unexpected keyword argument`,
        # the exception escaped this function, and the run was left in RUNNING with no
        # terminal state, no quarantine and no terminal ledger line -- residue under
        # `runs/` that the milestone promises can never exist. The plan is already spent
        # by the time control reaches here, so there is no ending that may leave the run
        # record un-terminated. `InterruptionRequested` and `KeyboardInterrupt` are
        # handled above and `KeyboardInterrupt` is a `BaseException`, so neither is
        # swallowed here.
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.BACKEND,
                       problems=(f"{type(exc).__name__}: {str(exc)[:200]}",))

    record.emit(RunEventKind.TRAINING_FINISHED, at=at,
                steps=result.steps_completed, train_loss=result.train_loss)
    atomic_write_text(directory / BACKEND_RESULT_FILE,
                      canonical_json(result.to_record()))

    if result.status is BackendStatus.INTERRUPTED or result.interrupted:
        return _interrupted(record=record, directory=directory, output_root=output_root,
                            plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                            detail=result.error_message, result=result)
    if result.status is not BackendStatus.SUCCEEDED:
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=_result_category(result),
                       problems=(result.error_message or result.status.value,),
                       result=result)
    metric_problems = result.metric_problems()
    if metric_problems:
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.INVALID_METRICS,
                       problems=metric_problems, result=result)

    # Success is a claim. The bytes are the evidence.
    record.transition(TrainingRunState.ARTIFACT_VALIDATION, at=at,
                      reason="backend reported success")
    record.emit(RunEventKind.ARTIFACT_VALIDATION_STARTED, at=at)
    validation = validate_adapter_directory(
        directory, lora=config.lora.to_dict(), base_model_id=config.base_model_id)
    escaped = _escaped_files(directory, output_root, escape_before)
    if escaped:
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.ARTIFACT, problems=escaped, result=result,
                       quarantine=True)
    if not validation.ok:
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.ARTIFACT, problems=validation.problems,
                       result=result, quarantine=True)

    record.emit(RunEventKind.ARTIFACT_VALIDATION_PASSED, at=at,
                files=len(validation.files))
    record.transition(TrainingRunState.COMPLETED, at=at, reason="artifacts validated")
    # Every file the manifest will name is written HERE, before it is hashed. Nothing
    # may be rewritten afterwards: a log line appended after the digest was taken would
    # make the manifest describe a file that no longer exists in that form.
    record.emit(RunEventKind.RUN_COMPLETED, at=at)
    write_run_state(directory, record)
    write_run_events(directory, record)

    try:
        manifest = _build_manifest(config=config, planning=planning, record=record,
                                   result=result, directory=directory, at=at,
                                   dataset=dataset)
    except (AdapterArtifactError, SchemaError) as exc:
        record.state = TrainingRunState.ARTIFACT_VALIDATION
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.ARTIFACT, problems=(str(exc),),
                       result=result, quarantine=True)

    # LAST. Its presence is what "completed" means; everything it names is already on
    # disk and already hashed from disk.
    atomic_write_text(directory / ADAPTER_MANIFEST_FILE,
                      canonical_json(manifest.to_record()))
    problems = verify_completed_run(directory, lora=config.lora.to_dict(),
                                    base_model_id=config.base_model_id)
    if problems:
        record.state = TrainingRunState.ARTIFACT_VALIDATION
        return _failed(record=record, directory=directory, output_root=output_root,
                       plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                       category=ErrorCategory.ARTIFACT, problems=problems,
                       result=result, quarantine=True)

    record_terminal(output_root, plan_hash=plan.plan_hash(), run_id=config.run_id,
                    actor=actor, at=at, state=TrainingRunState.COMPLETED,
                    method=config.method.value)
    return ExecutionOutcome(
        run_id=config.run_id, state=TrainingRunState.COMPLETED,
        plan_hash=plan.plan_hash(), backend_result=result,
        manifest_hash=manifest.manifest_hash(),
        adapter_files=tuple(f.name for f in manifest.files), plan_consumed=True)


def _result_category(result: BackendResult) -> ErrorCategory:
    try:
        return ErrorCategory(result.error_category)
    except ValueError:
        return ErrorCategory.BACKEND


# The escape check fingerprints the trusted output root twice -- once immediately before
# the backend is handed control, once when it returns -- and reports only what moved in
# between. Scanning the tree without that interval was the S3D defect: every sibling run
# read as something THIS backend had written, so a run failed because an earlier run
# existed at all. That is not only stale residue: a completed run's directory also stays
# under `runs/`, so the second run under any output root would have failed. The bound
# matches the resource policy's generated-file ceiling; a trusted root larger than that
# is itself the finding rather than something to scan past in silence.
_ESCAPE_SCAN_MAX_ENTRIES = 4096


def _escape_snapshot(scan_root: Path, *,
                     exclude: Path) -> tuple[dict[str, tuple], bool]:
    """A bounded fingerprint of everything under `scan_root` but the run's own tree.

    Symlinks are recorded by their own identity and never followed, so a link planted in
    the root cannot walk this scan out of it, and neither a socket nor a device node is
    ever opened. Directories carry no timestamp on purpose: a file written into one
    appears as its own new entry, which is the evidence that matters, while directory
    mtimes are the one signal that moves for reasons nobody wrote.
    """
    try:
        excluded = exclude.relative_to(scan_root).as_posix()
    except ValueError:
        excluded = None

    fingerprints: dict[str, tuple] = {}
    pending = [scan_root]
    while pending:
        try:
            entries = sorted(pending.pop().iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if len(fingerprints) >= _ESCAPE_SCAN_MAX_ENTRIES:
                return fingerprints, True
            relative = entry.relative_to(scan_root).as_posix()
            if relative == excluded:
                continue
            if entry.is_symlink():
                fingerprints[relative] = ("symlink",)
            elif entry.is_dir():
                fingerprints[relative] = ("dir",)
                pending.append(entry)
            elif entry.is_file():
                try:
                    status = entry.stat()
                except OSError:
                    fingerprints[relative] = ("unreadable",)
                    continue
                fingerprints[relative] = ("file", status.st_size, status.st_mtime_ns)
            else:
                fingerprints[relative] = ("other",)
    return fingerprints, False


def _escaped_files(directory: Path, output_root: Path,
                   before: dict[str, tuple]) -> tuple[str, ...]:
    """What the backend changed in the trusted root outside its own run directory.

    Only the interval counts. `before` was taken immediately before the backend was
    handed control, so a sibling that already existed -- a completed run, or the stale
    residue of a failed one -- fingerprints identically in both passes and is not a
    finding. Anything created, modified or removed while the backend held control is.
    """
    after, truncated = _escape_snapshot(output_root, exclude=directory)
    strays = sorted({name for name, mark in after.items() if before.get(name) != mark}
                    | {name for name in before if name not in after})
    problems: list[str] = []
    if strays:
        problems.append(
            f"the backend wrote {len(strays)} entr(y/ies) outside its run directory "
            f"({strays[:3]}); a trainer that escapes its workspace has written "
            f"somewhere nobody is auditing")
    if truncated:
        problems.append(
            f"the training root holds more than {_ESCAPE_SCAN_MAX_ENTRIES} entries, so "
            f"what the backend wrote outside its run directory cannot be established")
    return tuple(problems)


def _build_manifest(*, config: TrainingConfig, planning, record: RunRecord,
                    result: BackendResult, directory: Path, at: str,
                    dataset) -> AdapterManifest:
    reference = config.dataset_reference
    identity = planning.identity
    files = tuple(ArtifactFile(name=p.name, sha256=_sha(p), size_bytes=p.stat().st_size)
                  for p in sorted(directory.iterdir())
                  if p.is_file() and not p.is_symlink())
    return AdapterManifest(
        run_id=config.run_id, run_state=record.state.value,
        plan_hash=planning.plan.plan_hash(),
        training_config_hash=config.config_hash(), method=config.method.value,
        backend_id=result.backend_id, backend_version=result.backend_version,
        base_model_id=config.base_model_id,
        base_model_revision=config.base_model_revision,
        base_model_identity_hash=identity.identity_hash(),
        tokenizer_id=config.tokenizer_id,
        tokenizer_revision=config.tokenizer_revision,
        tokenizer_identity_hash=identity.tokenizer_identity_hash(),
        tokenizer_chat_template_hash=str(
            result.evidence.get("tokenizer_chat_template_hash", "")),
        # Empty when the backend bound no reasoning policy, which is what every run up to
        # and including candidate 002 did; the manifest then omits the key entirely and
        # hashes exactly as it always did (defect D37, S3M.1).
        chat_render_policy_hash=str(
            result.evidence.get("chat_render_policy_hash", "")),
        dataset_id=reference.dataset_id, dataset_version=reference.dataset_version,
        dataset_reference_hash=reference.reference_hash(),
        dataset_manifest_hash=reference.dataset_manifest_hash,
        train_shard_hash=reference.train_shard_hash,
        validation_shard_hash=reference.validation_shard_hash,
        hidden_evaluation_hash=reference.hidden_evaluation_manifest_hash,
        security_regression_hash=reference.security_regression_manifest_hash,
        export_manifest_hash=reference.export_manifest_hash,
        split_algorithm_version=reference.dataset_schema_version,
        seed=config.seed, lora=config.lora.to_dict(),
        assistant_only_loss=dict(result.evidence.get("assistant_only_loss_evidence")
                                 or {"strategy": "backend_reported",
                                     "verified": bool(result.evidence.get(
                                         "assistant_only_loss"))}),
        package_versions=dict(result.package_versions),
        device_category=planning.device.selected,
        precision=planning.precision.selected,
        requested_steps=config.max_steps, completed_steps=result.steps_completed,
        epochs_completed=result.epochs_completed, train_loss=result.train_loss,
        eval_loss=result.eval_loss,
        truncated_records=result.truncated_records,
        duration_seconds=result.duration_seconds, files=files,
        total_bytes=sum(f.size_bytes for f in files),
        tree_hash=adapter_tree_hash(files), created_at_utc=at,
        interrupted=False, completed=True,
        known_limitations=(
            "no live smoke run has been performed against this configuration",
            "deterministic reproduction is not claimed on accelerator hardware",
            "adapter quality is unmeasured; evaluation is a later stage"))


def _sha(path: Path) -> str:
    from ..schemas import sha256_file

    return sha256_file(path)


# ══════════════════════════════════════════════════════════════════════════════
#  The three failure endings
# ══════════════════════════════════════════════════════════════════════════════
def _interrupted(*, record: RunRecord, directory: Path, output_root: Path, plan,
                 actor: str, at: str, nonce: str, config: TrainingConfig,
                 detail: str, result: BackendResult | None = None) -> ExecutionOutcome:
    if record.state is TrainingRunState.RUNNING:
        record.transition(TrainingRunState.INTERRUPTING, at=at, reason="stop requested")
        record.emit(RunEventKind.INTERRUPTION_REQUESTED, at=at, message=detail)
        record.transition(TrainingRunState.INTERRUPTED, at=at, reason=detail)
    elif record.state is not TrainingRunState.INTERRUPTED:
        record.transition(TrainingRunState.INTERRUPTED, at=at, reason=detail)
    record.error_category = ErrorCategory.INTERRUPTED
    return _finish_badly(record=record, directory=directory, output_root=output_root,
                         plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                         problems=(f"the run was interrupted: {detail}",),
                         result=result, event=RunEventKind.RUN_INTERRUPTED)


def _failed(*, record: RunRecord, directory: Path, output_root: Path, plan,
            actor: str, at: str, nonce: str, config: TrainingConfig,
            category: ErrorCategory, problems: tuple[str, ...],
            result: BackendResult | None = None,
            quarantine: bool = True) -> ExecutionOutcome:
    del quarantine  # every bad ending is quarantined; the parameter documents intent
    if record.state is not TrainingRunState.FAILED:
        record.transition(TrainingRunState.FAILED, at=at,
                          reason=problems[0] if problems else category.value)
    record.error_category = category
    return _finish_badly(record=record, directory=directory, output_root=output_root,
                         plan=plan, actor=actor, at=at, nonce=nonce, config=config,
                         problems=problems, result=result,
                         event=RunEventKind.RUN_FAILED)


def _finish_badly(*, record: RunRecord, directory: Path, output_root: Path, plan,
                  actor: str, at: str, nonce: str, config: TrainingConfig,
                  problems: tuple[str, ...], result: BackendResult | None,
                  event: RunEventKind) -> ExecutionOutcome:
    """Write the evidence, remove any partial adapter, quarantine, record the outcome.

    The partial weights file is DELETED rather than preserved. A truncated
    ``adapter_model.safetensors`` is not diagnostic material — it is the one artifact
    that could later be mistaken for a finished adapter, and the manifest that would have
    disproved it is exactly the file that was never written.
    """
    record.emit(event, at=at, message=problems[0] if problems else "")
    removed = _remove_partial_weights(directory)
    write_run_state(directory, record)
    write_run_events(directory, record)
    quarantined, note = quarantine_run_directory(output_root, directory,
                                                 run_id=config.run_id, nonce=nonce)
    if note:
        problems = (*problems, note)
    record_terminal(output_root, plan_hash=plan.plan_hash(), run_id=config.run_id,
                    actor=actor, at=at, state=record.state,
                    method=config.method.value)
    return ExecutionOutcome(
        run_id=config.run_id, state=record.state, plan_hash=plan.plan_hash(),
        error_category=record.error_category, problems=tuple(problems),
        backend_result=result, quarantined_as=quarantined, plan_consumed=True,
        adapter_files=removed)


def _remove_partial_weights(directory: Path) -> tuple[str, ...]:
    removed: list[str] = []
    for name in ("adapter_model.safetensors", "adapter_model.bin",
                 ADAPTER_MANIFEST_FILE):
        target = directory / name
        try:
            if target.is_symlink() or target.exists():
                target.unlink()
                removed.append(name)
        except OSError:  # pragma: no cover - a locked file on Windows
            removed.append(f"{name} (could not be removed)")
    return tuple(removed)


def _remove_quietly(directory: Path) -> None:
    try:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
    except OSError:  # pragma: no cover - best effort only
        pass


__all__ = [
    "BACKEND_RESULT_FILE", "EXECUTABLE_METHODS", "EXECUTION_VERSION",
    "ExecutionError", "ExecutionOutcome", "ExecutionRefused", "Preflight",
    "execute_training", "run_preflight",
]
