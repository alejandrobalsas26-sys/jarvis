"""training_gym/evaluation/store.py — V69 M62 S3C: the append-only evaluation ledger.

WHY A LEDGER
------------
An ``EVAL:`` token authorises one live evaluation. Without a durable record of which
plans have been spent, the same approval could start a run repeatedly — and each run
loads a model, reads held-out material and writes an output tree. This file is what makes
"one approval, one run" a fact rather than an intention.

Deliberately a direct transliteration of S3B's training ledger, down to the link
refusals: relinking or hard-linking the ledger would give somebody a second way to
truncate it, which would un-spend every plan ever run.

WHAT DOES AND DOES NOT SPEND A PLAN
-----------------------------------
A preflight failure does not spend it — nothing ran. Starting live inference does, and it
stays spent afterwards whatever happened: a failed run read the data, an interrupted run
may have written bytes, and deleting the output directory does not un-read either. A
rerun needs a new generation and therefore a new plan and a new token.

PLAN CONSUMED IS NOT HOLDOUT SPENT — V69 M62 S3Q.0
--------------------------------------------------
The ``started`` line records that one approval began one run. It does NOT record that a
held-out task ever crossed the model-facing boundary: a run can consume its plan and then
fail while building the pack, and no model has read anything.

So this ledger carries a THIRD event, ``holdout_model_facing_committed``, appended once
per run immediately before the first :meth:`backend.generate`. It is the prospective
scientific spend boundary: once it is durable the holdout is ``USED_IMMUTABLE``, even if
the very next instruction crashes. The boundary is deliberately a shade earlier than
proof that a forward pass ran, because there is no atomic transaction between a local
append and an external synchronous call, and the fail-closed side of that gap is to
assume the holdout was read.

It applies PROSPECTIVELY. The S3I and S3L evaluations predate the event, their ledgers
carry only ``started`` and a terminal line, and nothing here synthesises one for them.
"""
from __future__ import annotations

from pathlib import Path

from ..atomicio import AtomicIOError, append_jsonl, read_jsonl
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    assert_no_private_content,
    require_id,
    short,
)
from ..task_spec import require_timestamp
from .config import EvaluationRunState
from .plan import EVALUATION_LEDGER_FILE, MAX_EVALUATION_LEDGER_LINES

#: Bumped when a ledger entry's shape changes.
LEDGER_RECORD_VERSION = "m62.evaluation_run.1"

#: S3Q.0. The prospective model-facing commit line carries strictly more fields than a
#: start or terminal line, so it declares its own record version rather than widening
#: theirs. Historical ``m62.evaluation_run.1`` lines stay valid, unedited and readable by
#: every existing reader, which is what makes this an extension and not a migration.
HOLDOUT_COMMIT_RECORD_VERSION = "m62.evaluation_holdout_commit.1"

#: The event name. NOT ``model_read`` — this record cannot prove a forward pass already
#: happened. NOT ``holdout_exposed`` — nothing was exposed to a human or to an
#: orchestrator. It means exactly: the first held-out request passed parity and the
#: evaluator durably committed to hand it to the production backend.
HOLDOUT_COMMIT_EVENT = "holdout_model_facing_committed"

#: The two events that were legal before S3Q.0 and stay legal unchanged.
LEGACY_LEDGER_EVENTS: frozenset[str] = frozenset({"started"})

#: Where a run that failed artifact validation is moved, so it cannot be mistaken for
#: a completed one and cannot be silently overwritten.
QUARANTINE_DIR = "evaluation_quarantine"


class EvaluationStoreError(SchemaError):
    """The evaluation ledger could not be read or extended safely."""


class PlanAlreadyConsumed(EvaluationStoreError):
    """This plan has already started an evaluation. One approval authorises one run."""


def evaluation_ledger_path(root: str | Path) -> Path:
    return Path(root) / EVALUATION_LEDGER_FILE


def _refuse_linked_ledger(path: Path) -> None:
    """Checked before every read as well as every write.

    Relinking the file elsewhere, or deleting the target of a link, would un-consume
    every plan ever run — the one thing this file exists to make impossible.
    """
    if path.is_symlink():
        raise EvaluationStoreError(
            f"{path.name}: is a symlink; the ledger that is read and the ledger that is "
            f"written must be the same file, or a relink un-consumes every plan")
    if path.is_file() and getattr(path.stat(), "st_nlink", 1) > 1:
        raise EvaluationStoreError(
            f"{path.name}: has more than one hard link; a second name for the ledger is "
            f"a second way to truncate it")


def evaluation_entries(root: str | Path) -> list[dict]:
    path = evaluation_ledger_path(root)
    _refuse_linked_ledger(path)
    try:
        return read_jsonl(path)
    except AtomicIOError as exc:
        raise EvaluationStoreError(str(exc)) from None


def is_plan_consumed(root: str | Path, plan_hash: str) -> bool:
    """True once any line records this plan as started, whatever happened next."""
    digest = str(plan_hash).strip().lower()
    if len(digest) != 64:
        raise EvaluationStoreError("evaluation ledger: a plan hash is 64 hex characters")
    return any(str(entry.get("plan_hash", "")) == digest
               and str(entry.get("event", "")) == "started"
               for entry in evaluation_entries(root))


def consume_plan(root: str | Path, *, plan_hash: str, evaluation_id: str,
                 generation: int, actor: str, at: str) -> dict:
    """Spend a plan. Refuses if it was already spent, and never spends it twice.

    The check and the append are not atomic against a concurrent process; the generation
    directory's ``mkdir(exist_ok=False)`` is what serialises that race, and it happens
    first. This is the durable record, not the lock.
    """
    entry = _entry(plan_hash=plan_hash, evaluation_id=evaluation_id,
                   generation=generation, actor=actor, at=at, event="started")
    if is_plan_consumed(root, plan_hash):
        raise PlanAlreadyConsumed(
            f"evaluation: plan {short(plan_hash)} has already started a run. A "
            f"confirmation authorises one evaluation, and replaying it would measure "
            f"again on an approval nobody gave twice. Re-plan at a new generation to "
            f"obtain a new token")
    _append(root, entry)
    return entry


class HoldoutAlreadyCommitted(EvaluationStoreError):
    """This run already crossed the model-facing boundary. There is no second crossing."""


def holdout_commit_entries(root: str | Path) -> list[dict]:
    """Every model-facing commit line. Empty for every evaluation that predates S3Q.0."""
    return [entry for entry in evaluation_entries(root)
            if str(entry.get("event", "")) == HOLDOUT_COMMIT_EVENT]


def is_holdout_committed(root: str | Path, plan_hash: str) -> bool:
    """True once this plan durably committed a held-out task to the model.

    Distinct from :func:`is_plan_consumed` on purpose. ``consumed and not committed`` is
    a real state — an approval was spent and no model read anything — and collapsing the
    two would either re-spend a holdout or write off one that was never read.
    """
    digest = str(plan_hash).strip().lower()
    if len(digest) != 64:
        raise EvaluationStoreError("evaluation ledger: a plan hash is 64 hex characters")
    return any(str(entry.get("plan_hash", "")) == digest
               for entry in holdout_commit_entries(root))


def record_holdout_commit(root: str | Path, *, plan_hash: str, evaluation_id: str,
                          generation: int, actor: str, at: str,
                          commit: dict) -> dict:
    """Durably commit the holdout to the model. Appended once, before the first call.

    Refuses BEFORE it appends when the run has no ``started`` line, when a commit
    already exists for this run, or when an existing commit for this evaluation and
    generation names a different plan. Every one of those means the caller's idea of
    what is running disagrees with the ledger's, and the fail-closed answer to that is
    to leave the holdout unspent rather than to guess.
    """
    if not isinstance(commit, dict):
        raise EvaluationStoreError(
            "evaluation ledger: the holdout commit body must be a mapping")
    entry = _entry(plan_hash=plan_hash, evaluation_id=evaluation_id,
                   generation=generation, actor=actor, at=at,
                   event=HOLDOUT_COMMIT_EVENT)
    entry["record_version"] = HOLDOUT_COMMIT_RECORD_VERSION
    digest = entry["plan_hash"]

    if not is_plan_consumed(root, digest):
        raise EvaluationStoreError(
            f"evaluation: plan {short(digest)} has no start line, so a model-facing "
            f"commit would record a holdout being spent by a run nobody recorded "
            f"starting. Refusing before the model is called")
    for existing in holdout_commit_entries(root):
        same_run = (str(existing.get("evaluation_id")) == entry["evaluation_id"]
                    and existing.get("generation") == entry["generation"])
        if str(existing.get("plan_hash")) == digest:
            raise HoldoutAlreadyCommitted(
                f"evaluation: plan {short(digest)} has already committed a held-out "
                f"task to the model. The holdout is spent and there is no second "
                f"crossing; a rerun would measure again on material no model may read "
                f"twice")
        if same_run:
            raise HoldoutAlreadyCommitted(
                f"evaluation: generation {entry['generation']} of "
                f"{entry['evaluation_id']!r} already committed a holdout under plan "
                f"{short(str(existing.get('plan_hash')))}, not {short(digest)}")

    entry["commit"] = _commit_body(commit)
    _append(root, entry)
    return entry


#: Everything the commit body may carry. Closed, and every member is a digest, a count,
#: an identifier or a policy name. A field that could hold a prompt, a target, a rubric
#: or a response is absent by construction, so a body-free event is a shape rather than
#: a promise.
HOLDOUT_COMMIT_FIELDS: tuple[str, ...] = (
    "commit_schema_version", "dataset_id", "dataset_version", "dataset_manifest_hash",
    "task_pack_hash", "hidden_target_store_hash", "pack_identity_hash",
    "order_policy", "order_assignment_hash", "task_count", "target_count",
    "first_task_id", "first_task_hash", "first_arm", "first_request_parity_hash",
    "baseline_reference_hash", "candidate_adapter_reference_hash",
    "generation_policy_hash", "backend_id", "performs_inference",
)


def _commit_body(commit: dict) -> dict:
    """Refuse anything outside the closed field list, then canonicalise."""
    unknown = sorted(set(commit) - set(HOLDOUT_COMMIT_FIELDS))
    if unknown:
        raise EvaluationStoreError(
            f"evaluation ledger: the holdout commit body names {unknown}, which is not "
            f"in the closed body-free field list; a widened event is how held-out "
            f"material reaches an append-only file nobody can retract")
    missing = sorted(set(HOLDOUT_COMMIT_FIELDS) - set(commit))
    if missing:
        raise EvaluationStoreError(
            f"evaluation ledger: the holdout commit body omits {missing}; a commit that "
            f"does not say what was committed is not evidence")
    return dict(sorted(commit.items()))


def record_terminal(root: str | Path, *, plan_hash: str, evaluation_id: str,
                    generation: int, actor: str, at: str,
                    state: EvaluationRunState) -> dict:
    """Append the outcome as a SECOND line. The ``started`` line is never rewritten."""
    if not state.is_terminal:
        raise EvaluationStoreError(
            f"evaluation ledger: {state.value} is not a terminal outcome")
    entry = _entry(plan_hash=plan_hash, evaluation_id=evaluation_id,
                   generation=generation, actor=actor, at=at, event=state.value)
    _append(root, entry)
    return entry


def _entry(*, plan_hash: str, evaluation_id: str, generation: int, actor: str, at: str,
           event: str) -> dict:
    digest = str(plan_hash).strip().lower()
    if len(digest) != 64:
        raise EvaluationStoreError("evaluation ledger: a plan hash is 64 hex characters")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise EvaluationStoreError("evaluation ledger: generation must be positive")
    return {
        SCHEMA_KEY: SCHEMA_VERSION,
        "record_version": LEDGER_RECORD_VERSION,
        "plan_hash": digest,
        "evaluation_id": require_id(evaluation_id, "ledger.evaluation_id"),
        "generation": generation,
        "event": event,
        # An operator id, never a username or a host. The CLI supplies a safe label.
        "actor": require_id(actor, "ledger.actor"),
        "at": require_timestamp(at, "ledger.at"),
    }


def _append(root: str | Path, entry: dict) -> None:
    path = evaluation_ledger_path(root)
    _refuse_linked_ledger(path)
    assert_no_private_content(entry, label="evaluation ledger entry")
    try:
        append_jsonl(path, entry, max_lines=MAX_EVALUATION_LEDGER_LINES)
    except AtomicIOError as exc:
        # Refusing loudly is the only honest outcome: an unrecorded start is a plan that
        # can be spent again, which is the failure this ledger exists to prevent.
        raise EvaluationStoreError(
            f"evaluation: the run ledger could not be appended ({exc}); refusing to "
            f"proceed, because a run nobody recorded is a plan that can be spent "
            f"again") from None


def create_generation_directory(root: str | Path, evaluation_id: str,
                                generation: int) -> Path:
    """Create the output directory, refusing to reuse one.

    ``exist_ok=False`` is the mutual exclusion: two processes cannot both create the
    same generation, and a completed generation can never be silently overwritten.
    """
    from .plan import evaluation_run_directory
    directory = evaluation_run_directory(root, evaluation_id, generation)
    if directory.is_symlink():
        raise EvaluationStoreError(
            f"the generation directory for {evaluation_id!r} is a symlink")
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise EvaluationStoreError(
            f"generation {generation} of evaluation {evaluation_id!r} already exists; a "
            f"completed generation is never overwritten, so raise the generation"
        ) from None
    except OSError as exc:
        raise EvaluationStoreError(
            f"the generation directory could not be created ({exc.strerror})") from None
    return directory


def quarantine_generation(root: str | Path, directory: Path, *, evaluation_id: str,
                          generation: int, nonce: str) -> Path:
    """Move a failed generation aside so it cannot be mistaken for a completed one."""
    target_root = Path(root) / QUARANTINE_DIR
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{require_id(evaluation_id, 'evaluation_id')}-gen{generation}-{require_id(nonce, 'nonce')}"
    try:
        Path(directory).rename(target)
    except OSError as exc:
        raise EvaluationStoreError(
            f"the generation could not be quarantined ({exc.strerror}); it remains at "
            f"its original location and must not be read as completed") from None
    return target


__all__ = [
    "HOLDOUT_COMMIT_EVENT", "HOLDOUT_COMMIT_FIELDS", "HOLDOUT_COMMIT_RECORD_VERSION",
    "LEDGER_RECORD_VERSION", "LEGACY_LEDGER_EVENTS", "QUARANTINE_DIR",
    "EvaluationStoreError", "HoldoutAlreadyCommitted", "PlanAlreadyConsumed",
    "consume_plan", "create_generation_directory", "evaluation_entries",
    "evaluation_ledger_path", "holdout_commit_entries", "is_holdout_committed",
    "is_plan_consumed", "quarantine_generation", "record_holdout_commit",
    "record_terminal",
]
