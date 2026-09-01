"""training_gym/evaluation/store_v4.py — V69 M62 S4E: spending a holdout exactly once.

WHY THIS IS A SEPARATE MODULE AND NOT AN EDIT TO ``store.py``
-------------------------------------------------------------
``tests/test_training_gym_m62_s3q01_control_plane.py::
test_the_live_execution_machinery_was_not_edited`` freezes ``runner.py``, ``store.py``,
``execution.py``, ``preflight.py``, ``generation.py``, ``gates.py`` and ``policy.py``
against the S3Q.0 subject commit. That guard exists because the machinery which produced
the sealed S3Q and S3Y measurements must stay the machinery those receipts describe.

S4E needed two things ``store.py`` does not do, and took them as a reason to build a
layer rather than a reason to move a freeze. The frozen files are byte-identical to
``b928f9d4``; everything below sits on top of them and calls them.

THE GAP THIS CLOSES, AND THE GAP IT DOES NOT
---------------------------------------------
``record_holdout_commit`` refuses a second commit on exactly two keys: the same
``plan_hash``, or the same ``(evaluation_id, generation)``. Neither notices a second
attempt that simply renames itself — a new evaluation id produces a new plan hash, a new
valid token, and a second model-facing commit AGAINST THE SAME CORPUS. The holdout is
what is spent, so the holdout is what must be refused twice, and
:func:`assert_holdout_never_spent` refuses on dataset identity and on pack digest.

WHAT IS NOT CLOSED, AND IS REPORTED RATHER THAN PAPERED OVER: this guard runs on the V4
path only. A v1-v3 run still has the original two keys, because closing it there means
editing a frozen file. The ledger is also per-``output_root``, so a caller who points a
run at a different root gets a different ledger — which is why the V4 plan binds
``expected_output_root_id`` and why :func:`assert_holdout_never_spent` is called with the
root the plan authorised, not one chosen at the moment of spending.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..schemas import SchemaError, sha256_obj
from .store import (
    HoldoutAlreadyCommitted,
    holdout_commit_entries,
    is_plan_consumed,
    record_holdout_commit,
)

#: Bumped when the attempt record's shape changes.
V4_ATTEMPT_RECORD_VERSION = "m62.evaluation_v4_attempt.1"

#: The durable, append-only record of a Protocol V4 paired attempt. Deliberately a
#: SEPARATE file from the evaluation ledger: the ledger's shape is frozen, and a paired
#: attempt has facts (two arms, a pairing) the frozen shape has no field for.
V4_ATTEMPT_FILE = "protocol-v4-attempts.jsonl"

#: Everything a V4 attempt record may carry. Closed, and every member is a digest, a
#: count or an identifier. There is no field a prompt, a target or a response could
#: occupy, so body-freeness is a shape rather than a promise.
V4_ATTEMPT_FIELDS: tuple[str, ...] = (
    "record_version", "event", "plan_hash", "inner_plan_hash", "evaluation_id",
    "generation", "protocol_version", "pairing_hash", "reference_arm_hash",
    "candidate_arm_hash", "reference_adapter_sha256", "candidate_adapter_sha256",
    "common_base_model_id", "common_base_model_revision", "dataset_id",
    "dataset_version", "dataset_manifest_hash", "task_pack_hash", "task_order_hash",
    "task_count", "expected_total_generations", "holdout_spends", "runtime_report_sha256",
    "evaluation_source_commit", "actor", "at",
)


class StoreV4Error(SchemaError):
    """A paired attempt that cannot be recorded as exactly one spend."""


def v4_attempt_path(root: str | Path) -> Path:
    return Path(root) / V4_ATTEMPT_FILE


def v4_attempt_entries(root: str | Path) -> tuple[dict, ...]:
    """Every recorded paired attempt. A malformed line is a refusal, never a skip."""
    path = v4_attempt_path(root)
    if not path.is_file():
        return ()
    entries: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreV4Error(
                f"{V4_ATTEMPT_FILE}: line {number} is not JSON ({exc}); an append-only "
                f"record nobody can parse is not evidence, and treating it as absent is "
                f"how a spend disappears") from None
        if not isinstance(entry, dict):
            raise StoreV4Error(f"{V4_ATTEMPT_FILE}: line {number} is not an object")
        entries.append(entry)
    return tuple(entries)


def assert_holdout_never_spent(root: str | Path, *, dataset_id: str,
                               dataset_version: str, task_pack_hash: str) -> None:
    """Refuse if ANY prior crossing named this corpus, whatever the attempt called itself.

    Checked against BOTH ledgers — the frozen evaluation ledger and the V4 attempt file —
    because a v1-v3 run and a V4 run spending the same corpus are the same event twice,
    and a guard that only reads its own file would not see the other.
    """
    key = (str(dataset_id), str(dataset_version))
    digest = str(task_pack_hash)
    for entry in holdout_commit_entries(root):
        body = entry.get("commit") or {}
        if (str(body.get("dataset_id", "")), str(body.get("dataset_version", ""))) == key:
            raise HoldoutAlreadyCommitted(
                f"evaluation: {key[0]} {key[1]} was already committed to a model by "
                f"{str(entry.get('evaluation_id'))!r} generation "
                f"{entry.get('generation')}. A holdout is spent ONCE, whatever the "
                f"attempt calls itself; renaming a run does not restore material a "
                f"model has already read")
        if digest and str(body.get("task_pack_hash", "")) == digest:
            raise HoldoutAlreadyCommitted(
                f"evaluation: task pack {digest[:12]} was already committed to a model "
                f"by {str(entry.get('evaluation_id'))!r} generation "
                f"{entry.get('generation')}; the corpus is identical whatever version "
                f"label the attempt carries")
    for entry in v4_attempt_entries(root):
        if (str(entry.get("dataset_id", "")), str(entry.get("dataset_version", ""))) == key:
            raise HoldoutAlreadyCommitted(
                f"evaluation: {key[0]} {key[1]} already has a recorded Protocol V4 "
                f"paired attempt under plan {str(entry.get('plan_hash'))[:12]}. ONE "
                f"paired attempt, ONE spend")
        if digest and str(entry.get("task_pack_hash", "")) == digest:
            raise HoldoutAlreadyCommitted(
                f"evaluation: task pack {digest[:12]} already has a recorded Protocol V4 "
                f"paired attempt")


def _attempt_body(record: dict) -> dict:
    """Refuse anything outside the closed field list, then canonicalise."""
    unknown = sorted(set(record) - set(V4_ATTEMPT_FIELDS))
    if unknown:
        raise StoreV4Error(
            f"{V4_ATTEMPT_FILE}: the attempt record names {unknown}, which is not in "
            f"the closed body-free field list; a widened event is how held-out material "
            f"reaches an append-only file nobody can retract")
    missing = sorted(set(V4_ATTEMPT_FIELDS) - set(record))
    if missing:
        raise StoreV4Error(
            f"{V4_ATTEMPT_FILE}: the attempt record omits {missing}; a record that does "
            f"not say what was attempted is not evidence")
    return dict(sorted(record.items()))


def _append_durable(path: Path, entry: dict) -> None:
    """Append one line and FLUSH IT TO THE PLATTER before returning.

    ``fsync`` is the difference between "the process believes it wrote a spend" and "the
    spend survives the power going out one statement later". This record exists
    precisely for the crash case, so buffering it would defeat the only reason it exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def record_v4_paired_attempt(root: str | Path, *, plan_hash: str, inner_plan_hash: str,
                             evaluation_id: str, generation: int, actor: str, at: str,
                             attempt: dict) -> dict:
    """Durably record ONE paired attempt, then commit the holdout through the frozen path.

    Order is the whole design:

      1. refuse unless the plan was consumed — a spend by a run nobody recorded starting
         is a spend nobody authorised;
      2. refuse if this corpus was ever committed before, under any name;
      3. write the V4 attempt record and fsync it;
      4. call the FROZEN ``record_holdout_commit``.

    Step 3 before step 4 is deliberate. If the process dies between them the corpus is
    recorded as attempted and no ledger commit exists, which reads as "an attempt began
    and did not reach a model" — the fail-CLOSED direction, because the next run is
    refused at step 2 and an operator has to rule. The reverse order could lose the
    record of an attempt whose model calls had already started.
    """
    digest = str(plan_hash).strip().lower()
    if len(digest) != 64:
        raise StoreV4Error("v4 attempt: a plan hash is 64 hex characters")
    if not is_plan_consumed(root, digest):
        raise StoreV4Error(
            f"v4 attempt: plan {digest[:12]} has no start line, so recording a paired "
            f"attempt would record a holdout being spent by a run nobody recorded "
            f"starting. Refusing before the model is called")

    body = _attempt_body({
        **attempt,
        "record_version": V4_ATTEMPT_RECORD_VERSION,
        "event": "protocol_v4_paired_attempt",
        "plan_hash": digest,
        "inner_plan_hash": str(inner_plan_hash),
        "evaluation_id": str(evaluation_id),
        "generation": int(generation),
        "actor": str(actor),
        "at": str(at),
    })
    assert_holdout_never_spent(
        root, dataset_id=body["dataset_id"], dataset_version=body["dataset_version"],
        task_pack_hash=body["task_pack_hash"])
    if int(body["holdout_spends"]) != 1:
        raise StoreV4Error(
            f"v4 attempt: holdout_spends is {body['holdout_spends']}; a paired attempt "
            f"spends its holdout exactly once. Two spends is two evaluations")
    for entry in v4_attempt_entries(root):
        if str(entry.get("plan_hash")) == digest:
            raise HoldoutAlreadyCommitted(
                f"v4 attempt: plan {digest[:12]} has already recorded a paired attempt")
    _append_durable(v4_attempt_path(root), body)
    return body


def v4_attempt_exists(root: str | Path, *, dataset_id: str,
                      dataset_version: str) -> bool:
    """Whether this corpus already has a recorded paired attempt. Crash-recovery reads this."""
    return any((str(e.get("dataset_id", "")), str(e.get("dataset_version", "")))
               == (str(dataset_id), str(dataset_version))
               for e in v4_attempt_entries(root))


def commit_v4_holdout(root: str | Path, *, plan_hash: str, evaluation_id: str,
                      generation: int, actor: str, at: str, commit: dict) -> dict:
    """The frozen ledger commit, called unchanged. Kept here so one module owns the seam."""
    return record_holdout_commit(root, plan_hash=plan_hash, evaluation_id=evaluation_id,
                                 generation=generation, actor=actor, at=at,
                                 commit=commit)


def attempt_record_hash(record: dict) -> str:
    return sha256_obj(dict(sorted(record.items())))


__all__ = [
    "V4_ATTEMPT_FIELDS", "V4_ATTEMPT_FILE", "V4_ATTEMPT_RECORD_VERSION", "StoreV4Error",
    "assert_holdout_never_spent", "attempt_record_hash", "commit_v4_holdout",
    "record_v4_paired_attempt", "v4_attempt_entries", "v4_attempt_exists",
    "v4_attempt_path",
]
