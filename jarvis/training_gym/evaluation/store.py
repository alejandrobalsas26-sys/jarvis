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
    "LEDGER_RECORD_VERSION", "QUARANTINE_DIR", "EvaluationStoreError",
    "PlanAlreadyConsumed", "consume_plan", "create_generation_directory",
    "evaluation_entries", "evaluation_ledger_path", "is_plan_consumed",
    "quarantine_generation", "record_terminal",
]
