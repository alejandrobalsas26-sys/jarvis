"""scripts/build_m62_eval_receipt.py — V69 M62 S3Q.0 / S3Q.0.1: the portable evaluation receipt.

READ THIS FIRST — TWO VERSIONS LIVE HERE
----------------------------------------
``m62.eval_receipt.1`` (S3Q.0) is below, unchanged, and is the contract the synthetic
S3Q.0 receipts already hash against. ``m62.eval_receipt.2`` (S3Q.0.1) is the MODERN
receipt and the only one a candidate measured from now on may present. The header that
follows describes the shared design; the ``.2`` section describes what its audit found
missing and what the version therefore had to change.

WHY THIS EXISTS BEFORE THE EVALUATION AND NOT AFTER IT
------------------------------------------------------
S3P had to invent portable training evidence *after* the irreversible operation, which
meant the receipt architecture was designed by somebody who already knew the answer. A
one-shot held-out evaluation is a harder version of the same problem: it can happen
exactly once, and the artefacts it produces live entirely in gitignored runtime trees. A
fresh clone has none of them, so a control plane claiming ``EVALUATED_*`` on their
strength alone would be claiming something no auditor could ever check.

So the machinery is built and qualified against SYNTHETIC evidence now, while nothing is
at stake, and the live run later has only to use it.

WHAT IT PROVES, AND WHAT IT REFUSES TO
---------------------------------------
It distils one completed evaluation generation into a small, tracked, root-independent
document binding the candidate, the adapter, the base model, the holdout, the exact plan,
every policy identity, the three ledger events and the outcome.

It is EVIDENCE OF AN OPERATION, NEVER AUTHORITY FOR ANOTHER. No receipt authorises a
retry, a second evaluation, a promotion, an activation, a registry mutation or a release.
Holding one is being able to prove what happened, not being permitted to do anything.

WHAT IT NEVER CARRIES
---------------------
  * no prompt, no held-out target, no rubric prose and no model response — only digests;
  * no ``EVAL:`` confirmation literal. A receipt proves an authority was spent; printing
    one hands the reader the capability instead of the evidence;
  * no absolute path, no home directory, no cache location and no username;
  * no verdict of its own. ``eligibility`` is copied from the canonical report and the
    receipt has no opinion about it.

DETERMINISM
-----------
Serialised with :func:`scripts.verify_m62_control_plane.canonical_json` — the one
implementation the rest of the control plane hashes with — and carrying no timestamp of
its own. Rebuilding from the same generation reproduces the same bytes. ``receipt_hash``
is the digest of the payload with that field removed, so it is self-checking without
being self-referential: change one bound fact and verification fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

RECEIPT_SCHEMA_VERSION = "m62.eval_receipt.1"

#: The authority form WITHOUT the digest that completes it. Naming the shape documents
#: the ceremony; naming an instance would reproduce a spendable capability.
AUTHORITY_FORM = "EVAL:<plan-hash>"

#: The files whose bytes a receipt binds by digest. ``task-pack.jsonl`` is on the list
#: and is BODY-BEARING: it is hashed and never read for meaning, which is the whole
#: distinction between body-opaque verification and semantic access.
RESULT_SET_FILES: tuple[str, ...] = (
    "task-pack.jsonl", "baseline-results.jsonl", "candidate-results.jsonl",
    "paired-comparisons.jsonl", "baseline-scores.jsonl", "candidate-scores.jsonl")


class ReceiptError(RuntimeError):
    """A generation that will not become a receipt as written."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ReceiptError(f"{path.name}: not a regular file in this generation")
    return json.loads(path.read_text(encoding="utf-8"))


def file_evidence(path: Path) -> dict:
    """Digest, byte count and line count. Never the content.

    This is the body-opaque trust boundary in one function: it opens a file that may be
    full of held-out prompts, and what it returns is three numbers. A caller cannot
    obtain a body from it, however it is used.
    """
    if not path.is_file() or path.is_symlink():
        raise ReceiptError(f"{path.name}: not a regular file in this generation")
    raw = path.read_bytes()
    return {"sha256": _sha256_bytes(raw), "bytes": len(raw),
            # ``record_count``, never ``records``: the value is a COUNT, and a field
            # named for the thing it counts is one refactor from holding it.
            "record_count": sum(1 for line in raw.splitlines() if line.strip())}


def ledger_events(ledger: Path, *, evaluation_id: str, generation: int) -> dict:
    """The three durable events for exactly this run, counted rather than assumed.

    Counted because the counts ARE the property: two ``started`` lines would mean one
    approval ran twice, and two ``holdout_model_facing_committed`` lines would mean a
    held-out corpus was committed to a model twice. A receipt that assumed one of each
    could not notice either.
    """
    from training_gym.evaluation.store import HOLDOUT_COMMIT_EVENT

    counts: dict[str, int] = {}
    plan_hashes: set[str] = set()
    commit: dict = {}
    terminal = ""
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if (entry.get("evaluation_id") != evaluation_id
                    or entry.get("generation") != generation):
                continue
            event = str(entry.get("event", ""))
            counts[event] = counts.get(event, 0) + 1
            plan_hashes.add(str(entry.get("plan_hash", "")))
            if event == HOLDOUT_COMMIT_EVENT:
                commit = dict(entry.get("commit", {}))
            elif event != "started":
                terminal = event
    return {"counts": dict(sorted(counts.items())),
            "plan_hashes": sorted(plan_hashes),
            "commit": commit, "terminal_event": terminal}


def build_receipt(generation_directory: str | Path, *, candidate: str,
                  evaluation_source_commit: str, ledger: str | Path) -> dict:
    """One receipt, every field re-derived from canonical evidence.

    Nothing here is typed in by a caller except the candidate identity and the commit
    the run executed from — and the candidate identity is CHECKED against the adapter
    reference the report bound, so naming the wrong one is a refusal rather than a
    relabelling.
    """
    from training_gym.evaluation.artifacts import verify_evaluation_generation
    from training_gym.evaluation.store import HOLDOUT_COMMIT_EVENT

    directory = Path(generation_directory)
    plan = _load(directory / "evaluation-plan.json")
    report = _load(directory / "evaluation-report.json")
    manifest = _load(directory / "evaluation-manifest.json")
    metrics = _load(directory / "metrics.json")
    pack_manifest = _load(directory / "task-pack-manifest.json")

    evaluation_id = str(report["evaluation_id"])
    generation = int(report["generation"])
    events = ledger_events(Path(ledger), evaluation_id=evaluation_id,
                           generation=generation)
    commit = events["commit"]

    # ── the obligations a modern receipt may not paper over ──────────────────
    problems = list(verify_evaluation_generation(directory) or ())
    started = events["counts"].get("started", 0)
    committed = events["counts"].get(HOLDOUT_COMMIT_EVENT, 0)
    if started != 1:
        raise ReceiptError(
            f"the ledger records {started} start line(s) for this generation; a receipt "
            f"describes exactly one approved run")
    if committed != 1:
        raise ReceiptError(
            f"the ledger records {committed} model-facing commit line(s); a completed "
            f"evaluation that never durably committed its holdout has no evidence that "
            f"a model read anything, and two commits would mean it was read twice")
    if not events["terminal_event"]:
        raise ReceiptError(
            "the ledger records no terminal line for this generation; how the run ended "
            "is exactly what a receipt exists to carry")
    if str(commit.get("task_pack_hash")) != str(plan["task_pack_hash"]):
        raise ReceiptError(
            "the model-facing commit names a different task pack than the approved "
            "plan; the receipt would bind two different measurements")

    decision = report.get("eligibility", {})
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_version": RECEIPT_SCHEMA_VERSION,
        "evaluation_milestone": "S3Q",
        "evaluation_source_commit": evaluation_source_commit,
        "evaluation_id": evaluation_id,
        "evaluation_generation": generation,

        "candidate": {
            "candidate_id": candidate,
            "status_claim": _status_claim(decision.get("eligibility", "")),
            "adapter_reference_hash": str(report["candidate_adapter_reference_hash"]),
            "adapter_sha256": "",
            "adapter_manifest_hash": "",
        },
        "baseline": {
            "reference_hash": str(report["baseline_reference_hash"]),
            "tokenizer_identity_hash": str(report["tokenizer_identity_hash"]),
        },
        "holdout": {
            "dataset_id": str(pack_manifest["dataset_id"]),
            "dataset_version": str(pack_manifest["dataset_version"]),
            "dataset_manifest_hash": str(report["dataset_manifest_hash"]),
            "task_pack_hash": str(report["task_pack_hash"]),
            "hidden_target_store_hash": str(report["hidden_target_store_hash"]),
            "pack_manifest_shard_hashes": dict(sorted(
                {k: str(v) for k, v in pack_manifest.get("shard_hashes", {}).items()}
                .items())),
            "split_manifest_hashes": dict(sorted(
                {k: str(v) for k, v in report.get("split_manifest_hashes", {}).items()}
                .items())),
            "task_count": int(report["task_count"]),
            "counts_by_split": dict(sorted(pack_manifest.get("counts_by_split", {})
                                           .items())),
            "counts_by_family": dict(sorted(pack_manifest.get("counts_by_family", {})
                                            .items())),
            "counts_by_kind": dict(sorted(pack_manifest.get("counts_by_kind", {})
                                          .items())),
            "spent_by_this_evaluation": True,
        },
        "plan": {
            "plan_hash": str(report["plan_hash"]),
            "plan_schema_version": str(plan["plan_schema_version"]),
            "evaluator_version": str(report["evaluator_version"]),
            "order_policy": str(plan["order_policy"]),
            "order_assignment_hash": str(plan["order_assignment_hash"]),
            "performs_inference": bool(plan.get("performs_inference", False)),
            "binds_exact_pack_identity": (
                str(plan["task_pack_hash"]) == str(pack_manifest["pack_hash"])
                and str(plan["hidden_target_store_hash"])
                == str(pack_manifest["hidden_target_store_hash"])),
        },
        "policies": {
            "generation_policy_hash": str(report["generation_policy_hash"]),
            "grader_policy_hash": str(report["grader_policy_hash"]),
            "metric_policy_hash": str(report["metric_policy_hash"]),
            "statistical_policy_hash": str(report["statistical_policy_hash"]),
            "gate_policy_hash": str(report["gate_policy_hash"]),
            "family_policy_hash": str(report["family_policy_hash"]),
            "dependency_report_hash": str(report["dependency_report_hash"]),
            "hardware_report_hash": str(report["hardware_report_hash"]),
        },
        "authority": {
            "form": AUTHORITY_FORM,
            "bound_plan_hash": str(report["plan_hash"]),
            "creations": started,
            "consumptions": started,
            "token_literal_recorded": False,
            "retry_authorized": False,
            "grants_no_further_authority": True,
        },
        "ledger": {
            "plan_started_count": started,
            "holdout_commit_count": committed,
            "terminal_count": events["counts"].get(events["terminal_event"], 0),
            "terminal_event": events["terminal_event"],
            "events": events["counts"],
            "plan_hashes": events["plan_hashes"],
        },
        "holdout_commit": {
            "commit_schema_version": str(commit.get("commit_schema_version", "")),
            "pack_identity_hash": str(commit.get("pack_identity_hash", "")),
            "order_assignment_hash": str(commit.get("order_assignment_hash", "")),
            "first_task_id": str(commit.get("first_task_id", "")),
            "first_task_hash": str(commit.get("first_task_hash", "")),
            "first_arm": str(commit.get("first_arm", "")),
            "first_request_parity_hash": str(commit.get(
                "first_request_parity_hash", "")),
            "task_count": int(commit.get("task_count", 0)),
            "backend_id": str(commit.get("backend_id", "")),
        },
        "execution": {
            "run_state": str(report["run_state"]),
            "empirical_status": str(report["empirical_status"]),
            "backend_ids": sorted(str(b) for b in report.get("backend_ids", [])),
            "backend_version": str(report.get("backend_version", "")),
            "task_count": int(report["task_count"]),
            "measured_pairs": int(report["measured_pairs"]),
            "missing_pairs": int(report["missing_pairs"]),
            "wins": int(report["wins"]), "ties": int(report["ties"]),
            "losses": int(report["losses"]),
            "artifact_verification": "PASS" if not problems else "FAIL",
            "artifact_problems": list(problems),
        },
        "evidence": {
            "report_hash": str(report["report_hash"]),
            "evaluation_manifest_hash": str(manifest["manifest_hash"]),
            "artifact_tree_hash": str(manifest["tree_hash"]),
            "comparison_manifest_hash": str(report["comparison_manifest_hash"]),
            "metrics_summary_hash": str(metrics["summary_hash"]),
            "pack_manifest_hash": str(pack_manifest["pack_hash"]),
            "files": {name: file_evidence(directory / name)
                      for name in RESULT_SET_FILES
                      if (directory / name).exists()},
        },
        "outcome": {
            "eligibility": str(decision.get("eligibility", "")),
            "human_review_required": bool(decision.get("human_review_required", True)),
            "promotes_model": False,
            "activates_model": False,
            "mutates_model_registry": False,
            "gate_blockers": [str(b) for b in report.get("blockers", [])],
            "limitations": [str(limit) for limit in report.get("limitations", [])],
        },
    }


#: Report eligibility -> the candidate state a receipt supports. Deliberately partial:
#: an eligibility this map does not name supports NO evaluated state, because an
#: unrecognised verdict is an unknown one and UNKNOWN is not a pass.
ELIGIBILITY_TO_CANDIDATE_STATE: dict[str, str] = {
    "not_eligible": "EVALUATED_NOT_ELIGIBLE",
    "eligible_for_human_review": "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
    "needs_more_evidence": "EVALUATED_NEEDS_MORE_EVIDENCE",
    "quarantined": "EVALUATED_QUARANTINED",
}


def _status_claim(eligibility: str) -> str:
    return ELIGIBILITY_TO_CANDIDATE_STATE.get(str(eligibility), "")


def receipt_hash(payload: dict) -> str:
    """The digest of everything except the digest field itself."""
    from scripts.verify_m62_control_plane import canonical_json
    body = {k: v for k, v in payload.items() if k != "receipt_hash"}
    return _sha256_bytes(canonical_json(body).encode("utf-8"))


def seal(payload: dict) -> dict:
    return {**payload, "receipt_hash": receipt_hash(payload)}


def verify_receipt(payload: dict) -> tuple[str, ...]:
    """Re-derive the digest and report every reason this receipt is not evidence."""
    problems: list[str] = []
    stored = str(payload.get("receipt_hash", ""))
    actual = receipt_hash(payload)
    if stored != actual:
        problems.append(
            f"receipt_hash {stored[:12] or '(absent)'} does not match the bytes "
            f"({actual[:12]}); a receipt that can be edited without its digest moving "
            f"is a receipt that can be edited to say anything")
    ledger = payload.get("ledger", {})
    if ledger.get("plan_started_count") != 1:
        problems.append("the receipt does not bind exactly one plan-start event")
    if ledger.get("holdout_commit_count") != 1:
        problems.append(
            "the receipt does not bind exactly one model-facing commit event; without "
            "it there is no evidence a model ever read the holdout")
    if not ledger.get("terminal_event"):
        problems.append("the receipt binds no terminal event")
    if not payload.get("plan", {}).get("binds_exact_pack_identity"):
        problems.append(
            "the approved plan did not bind the exact pack identity that was measured")
    if payload.get("execution", {}).get("artifact_verification") != "PASS":
        problems.append("the generation's artefacts did not verify")
    if payload.get("authority", {}).get("token_literal_recorded"):
        problems.append("the receipt reproduces a spendable confirmation")
    if payload.get("authority", {}).get("retry_authorized"):
        problems.append("no receipt authorises a retry")
    return tuple(problems)




# ══════════════════════════════════════════════════════════════════════════════
#  V69 M62 S3Q.0.1 — the MODERN receipt
# ══════════════════════════════════════════════════════════════════════════════
# WHY A SECOND VERSION AND NOT AN EDIT
# ------------------------------------
# `.1` was qualified against synthetic evidence and then, before the one-shot live run,
# audited against the question a receipt exists to answer: can a clean clone holding
# nothing but this file and this repository establish WHAT was measured and WHY the
# verdict follows? Nine findings said no, and four of them are contract changes rather
# than clarifications:
#
#   A  `adapter_sha256` and `adapter_manifest_hash` were emitted EMPTY, and the schema
#      permitted the empty string. The one fact an auditor most needs -- which weights
#      were measured -- had a blank where the answer belongs.
#   B  `authority.creations` counted a durable token-creation event. No such event
#      exists in the ledger. The honest fix is to delete the field, not to invent the
#      event.
#   E  the candidate identity was a caller string, checked against nothing. Passing
#      `--candidate something-else` relabelled a valid generation and still verified.
#   I  `eligibility` was COPIED from the report, so the strongest statement a clean
#      clone could make about an EVALUATED_* claim was "the receipt says so".
#
# `.1` stays exactly as it is. Synthetic S3Q.0 receipts verify against it, no live
# receipt was ever written to it, and mutating a version under documents that already
# hash against it is the drift this repository refuses everywhere else.
#
# WHAT `.2` REFUSES TO DO
# -----------------------
# It does not decide anything. It does not re-implement `decide_eligibility` -- it feeds
# the serialised body-free inputs back into the production function, so an audit and a
# live run can never disagree about what the gates concluded. It does not claim a human
# authorised anything: a builder that could write `human_authorized: true` proves nothing
# by writing it.

RECEIPT_V2_SCHEMA_VERSION = "m62.eval_receipt.2"

#: The ledger's closed TERMINAL vocabulary. `.1` inferred "anything that is not `started`
#: and not the holdout commit is terminal", so a future body-free ledger line would have
#: silently become the terminal witness -- and, appearing last, would have OVERWRITTEN the
#: real one. Reproduced in S3Q.0.1 on a synthetic ledger.
TERMINAL_EVALUATION_EVENTS: frozenset[str] = frozenset({
    "completed", "failed", "interrupted", "quarantined"})

#: The only SUCCESSFUL terminal state. Reaching a terminal event is not reaching a
#: measurement.
SUCCESSFUL_TERMINAL_EVENT = "completed"

#: The result sets whose LINE COUNTS a full evaluated verdict must account for, mapped to
#: the receipt field each becomes. Counting them is not decoration: `.1` bound file
#: digests, and a digest of a file a clean clone does not have cannot tell that reader
#: how many pairs actually completed.
RESULT_COUNT_FILES: tuple[tuple[str, str], ...] = (
    ("baseline-results.jsonl", "baseline_result_count"),
    ("candidate-results.jsonl", "candidate_result_count"),
    ("paired-comparisons.jsonl", "paired_result_count"),
    ("baseline-scores.jsonl", "baseline_score_count"),
    ("candidate-scores.jsonl", "candidate_score_count"),
)

#: How the eligibility a receipt claims is rederived. Named in the receipt so a reader
#: knows which function to run, and asserted by the verifier so the name cannot drift
#: away from the code that actually ran.
DECISION_REDERIVER = "training_gym.evaluation.reports.decision_from_evidence"

MAX_RECEIPT_BYTES = 1 << 20


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    """One git call. Never `--force`, never a write, never an interactive flag."""
    import subprocess  # noqa: PLC0415 - imported where it is used, as the CLIs do
    try:
        done = subprocess.run(["git", "-C", str(repo_root), *args],
                              capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"git unavailable: {exc}"
    return done.returncode, done.stdout.strip()


def source_identity(repo_root: str | Path) -> dict:
    """DERIVE the source the evaluation ran from. Never accept it as a caller string.

    FINDING F. `.1` wrote whatever `--evaluation-source-commit` said. A receipt could
    therefore name any syntactically valid SHA, including one that never existed, and
    verification had no opinion.

    S3Q's execution freeze requires the tree to stay at the evaluated commit while the
    run and its receipt are produced, so the repository HEAD at receipt-build time IS the
    evaluated source commit -- but that is an ARCHITECTURAL guarantee, not a
    cryptographic one, and the receipt says so in `source.evidence_level` rather than
    claiming the commit proves which bytes executed. The tree oid is recorded beside it
    because a commit can be rewritten and a tree digest is the content.
    """
    root = Path(repo_root)
    code, commit = _git(root, "rev-parse", "HEAD")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReceiptError(
            f"the evaluation source commit could not be derived from {root} ({commit}); "
            f"a receipt may not fall back to a caller-supplied identity for the code "
            f"that measured")
    code, tree = _git(root, "rev-parse", "HEAD^{tree}")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ReceiptError(f"the source tree oid could not be derived from {root}")
    code, status = _git(root, "status", "--porcelain")
    return {"evaluation_source_commit": commit, "evaluation_source_tree_oid": tree,
            "derived_from_repository_head": True,
            "worktree_clean_at_build": code == 0 and not status.strip(),
            "evidence_level": "the receipt was built in a worktree at this commit; it "
                              "does not by itself prove which bytes executed"}


def ledger_evidence(ledger: str | Path, *, evaluation_id: str,
                    generation: int) -> dict:
    """The durable events for exactly this run, with the three S3Q.0.1 obligations met.

    FINDING G -- the terminal witness is drawn from the CLOSED vocabulary, so an
    unrecognised future line cannot become one by not being `started`.

    FINDING H -- every line for this run must carry the SAME plan hash. `.1` collected
    them into a set and bound the set; a run whose start line named plan A and whose
    holdout commit named plan B produced a receipt that verified.

    FINDING (S3Q.0.1) -- each critical line is bound by the digest of its canonical
    body-free payload, so the receipt binds the EVIDENCE rather than a count of it.
    """
    from scripts.verify_m62_control_plane import canonical_json
    from training_gym.evaluation.store import HOLDOUT_COMMIT_EVENT

    path = Path(ledger)
    if path.is_symlink() or not path.is_file():
        raise ReceiptError(
            f"{path.name}: the evaluation ledger is not a regular file; the durable "
            f"record of what ran is not optional evidence")

    counts: dict[str, int] = {}
    plan_hashes: set[str] = set()
    started: list[dict] = []
    commits: list[dict] = []
    terminals: list[dict] = []
    unrecognised: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if (entry.get("evaluation_id") != evaluation_id
                or entry.get("generation") != generation):
            continue
        event = str(entry.get("event", ""))
        counts[event] = counts.get(event, 0) + 1
        plan_hashes.add(str(entry.get("plan_hash", "")))
        if event == "started":
            started.append(entry)
        elif event == HOLDOUT_COMMIT_EVENT:
            commits.append(entry)
        elif event in TERMINAL_EVALUATION_EVENTS:
            terminals.append(entry)
        else:
            # Future-compatible and explicitly NON-terminal. Named so it is visible.
            unrecognised.add(event)

    if len(started) != 1:
        raise ReceiptError(
            f"the ledger records {len(started)} start line(s) for this generation; a "
            f"receipt describes exactly one approved run")
    if len(commits) != 1:
        raise ReceiptError(
            f"the ledger records {len(commits)} model-facing commit line(s); a completed "
            f"evaluation that never durably committed its holdout has no evidence that a "
            f"model read anything, and two commits would mean it was read twice")
    if len(terminals) != 1:
        raise ReceiptError(
            f"the ledger records {len(terminals)} recognised terminal line(s) "
            f"({sorted(TERMINAL_EVALUATION_EVENTS)}); how the run ended is exactly what a "
            f"receipt exists to carry, and two endings is not one run")

    hashes = sorted(h for h in plan_hashes if h)
    if len(plan_hashes) != 1 or not hashes:
        raise ReceiptError(
            f"the ledger lines for this run name {len(plan_hashes)} distinct plan "
            f"hash(es) {[h[:12] for h in sorted(plan_hashes)]}; one run is one approved "
            f"plan, and 'latest wins' would let a second plan finish a first plan's run")

    return {
        "counts": dict(sorted(counts.items())),
        "plan_hash": hashes[0],
        "unrecognised_events": sorted(unrecognised),
        "started": started[0], "commit": commits[0], "terminal": terminals[0],
        "terminal_event": str(terminals[0].get("event", "")),
        "event_hashes": {
            "plan_started_event_hash":
                _sha256_bytes(canonical_json(started[0]).encode("utf-8")),
            "holdout_commit_event_hash":
                _sha256_bytes(canonical_json(commits[0]).encode("utf-8")),
            "terminal_event_hash":
                _sha256_bytes(canonical_json(terminals[0]).encode("utf-8")),
        },
    }


def training_receipt_evidence(path: str | Path, *, repo_root: Path) -> dict:
    """Bind the tracked S3P training receipt by DIGEST, not by path.

    The training receipt carries no digest of its own -- `m62.train_receipt.1` has no
    `receipt_hash` field -- so this is the SHA-256 of its canonical tracked bytes, and it
    is named `training_receipt_sha256` for exactly that reason. Calling a file digest a
    receipt-internal hash would be describing evidence this repository does not have.
    """
    receipt_path = Path(path)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ReceiptError(
            f"{receipt_path.name}: the training receipt is not a regular file")
    raw = receipt_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"the training receipt is unreadable ({exc})") from None
    if str(payload.get("schema_version", "")) != "m62.train_receipt.1":
        raise ReceiptError(
            f"the training receipt declares schema "
            f"{payload.get('schema_version')!r}, which this builder does not know how "
            f"to read; an unrecognised evidence form is not a verified one")
    try:
        pointer = receipt_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise ReceiptError(
            f"the training receipt {receipt_path} is outside the repository; portable "
            f"evidence may not point at a host-local file") from None
    adapter = payload.get("adapter", {})
    return {
        "payload": payload,
        "bound": {
            "path": pointer,
            "training_receipt_sha256": _sha256_bytes(raw),
            "schema_version": str(payload["schema_version"]),
            "candidate_id": str(payload["candidate_id"]),
            "training_plan_hash": str(payload["plan_hash"]),
            "training_source_commit": str(payload["training_source_commit"]),
            "training_milestone": str(payload["training_milestone"]),
        },
        "adapter": {
            "sha256": str(adapter.get("sha256", "")),
            "manifest_hash": str(adapter.get("manifest_hash", "")),
            "artifact_set_hash": str(adapter.get("artifact_set_hash", "")),
        },
    }


def adapter_runtime_evidence(run_directory: str | Path) -> dict:
    """Re-verify the adapter on this host through the EXISTING artefact verifier.

    `verify_completed_run` re-parses the manifest, re-hashes every file it names, refuses
    any file it does not name and re-derives the tree digest. Reusing it -- rather than
    hashing weights here -- is what stops an adapter becoming evaluable through a second,
    weaker validator, and it is where `adapter_sha256` comes from: the manifest's own
    entry for the weights file, re-hashed from disk by that verifier.
    """
    from training_gym.evaluation.references import reference_from_manifest
    from training_gym.training.artifacts import (
        ADAPTER_MANIFEST_FILE,
        ADAPTER_WEIGHTS_FILE,
        AdapterManifest,
        verify_completed_run,
    )

    directory = Path(run_directory)
    manifest = AdapterManifest.from_dict(_load(directory / ADAPTER_MANIFEST_FILE))
    problems = verify_completed_run(directory, lora=dict(manifest.lora),
                                    base_model_id=manifest.base_model_id)
    if problems:
        raise ReceiptError(
            f"the adapter run directory does not verify on this host: "
            f"{'; '.join(problems[:4])}")
    weights = [f for f in manifest.files if f.name == ADAPTER_WEIGHTS_FILE]
    if len(weights) != 1:
        raise ReceiptError(
            f"the adapter manifest names {len(weights)} {ADAPTER_WEIGHTS_FILE} "
            f"entries; exactly one set of weights is what was evaluated")
    reference = reference_from_manifest(manifest, artifact_verified=True)
    return {
        "candidate_id": str(manifest.run_id),
        "adapter_sha256": str(weights[0].sha256),
        "adapter_manifest_hash": str(reference.adapter_manifest_hash),
        "adapter_artifact_set_hash": str(reference.adapter_artifact_tree_hash),
        "adapter_reference_hash": str(reference.reference_hash()),
    }


def build_receipt_v2(generation_directory: str | Path, *,
                     training_receipt: str | Path,
                     adapter_run_directory: str | Path,
                     evaluation_config: str | Path,
                     ledger: str | Path,
                     repo_root: str | Path,
                     expected_candidate: str = "",
                     expected_evaluation_source_commit: str = "",
                     milestone: str = "S3Q") -> dict:
    """One modern receipt. Every identity DERIVED from canonical evidence, none typed in.

    The two caller strings that remain -- `expected_candidate` and
    `expected_evaluation_source_commit` -- are ASSERTIONS. They are compared against the
    derived values and a mismatch is a refusal. A caller can therefore state what it
    believes and be told it is wrong; it cannot state what the receipt will say.
    """
    from training_gym.evaluation.artifacts import verify_evaluation_generation
    from training_gym.evaluation.config import config_from_dict
    from training_gym.evaluation.reports import (
        decision_from_evidence,
        verify_report_payload,
    )
    from training_gym.evaluation.store import HOLDOUT_COMMIT_EVENT

    directory = Path(generation_directory)
    root = Path(repo_root)
    plan = _load(directory / "evaluation-plan.json")
    report = verify_report_payload(_load(directory / "evaluation-report.json"))
    manifest = _load(directory / "evaluation-manifest.json")
    metrics = _load(directory / "metrics.json")
    pack_manifest = _load(directory / "task-pack-manifest.json")

    problems = list(verify_evaluation_generation(directory) or ())
    if problems:
        raise ReceiptError(
            f"the generation's artefacts do not verify: {'; '.join(problems[:4])}")

    evaluation_id = str(report["evaluation_id"])
    generation = int(report["generation"])
    events = ledger_evidence(ledger, evaluation_id=evaluation_id, generation=generation)
    commit = dict(events["commit"].get("commit", {}))

    # ── the ledger, the plan and the report must name ONE plan (FINDING H) ────
    for label, value in (("the ledger lines", events["plan_hash"]),
                         ("the approved plan file", str(plan["plan_hash"])),
                         ("the report", str(report["plan_hash"]))):
        if value != str(report["plan_hash"]):
            raise ReceiptError(
                f"{label} names plan {value[:12]} and the report names "
                f"{str(report['plan_hash'])[:12]}; a receipt binding two plans describes "
                f"two measurements")
    if str(commit.get("task_pack_hash")) != str(plan["task_pack_hash"]):
        raise ReceiptError(
            "the model-facing commit names a different task pack than the approved "
            "plan; the receipt would bind two different measurements")
    if str(commit.get("order_assignment_hash")) != str(plan["order_assignment_hash"]):
        raise ReceiptError(
            "the model-facing commit and the approved plan disagree about the order the "
            "arms ran in")

    # ── the candidate, DERIVED from the training receipt (FINDING E) ──────────
    training = training_receipt_evidence(training_receipt, repo_root=root)
    adapter = adapter_runtime_evidence(adapter_run_directory)
    candidate_id = training["bound"]["candidate_id"]
    if expected_candidate and expected_candidate != candidate_id:
        raise ReceiptError(
            f"the caller asserts candidate {expected_candidate!r} and the training "
            f"receipt seals {candidate_id!r}; a free caller string may never rename "
            f"evidence")
    if adapter["candidate_id"] != candidate_id:
        raise ReceiptError(
            f"the adapter run directory belongs to {adapter['candidate_id']!r} and the "
            f"training receipt seals {candidate_id!r}")

    # ── the adapter, cross-checked in every direction (S3Q.0.1 section 13) ────
    for field, sealed in (("adapter_sha256", training["adapter"]["sha256"]),
                          ("adapter_manifest_hash",
                           training["adapter"]["manifest_hash"]),
                          ("adapter_artifact_set_hash",
                           training["adapter"]["artifact_set_hash"])):
        if not sealed or not re.fullmatch(r"[0-9a-f]{64}", sealed):
            raise ReceiptError(
                f"the training receipt seals no usable {field}; a receipt may not record "
                f"an empty adapter identity, which is exactly what m62.eval_receipt.1 "
                f"did")
        if adapter[field] != sealed:
            raise ReceiptError(
                f"{field}: the adapter on this host is {adapter[field][:12]} and the "
                f"training receipt sealed {sealed[:12]}; these are not the same weights")
    for label, value in (("the approved plan",
                          str(plan["candidate_adapter_reference_hash"])),
                         ("the report",
                          str(report["candidate_adapter_reference_hash"])),
                         ("the model-facing commit",
                          str(commit.get("candidate_adapter_reference_hash", "")))):
        if value != adapter["adapter_reference_hash"]:
            raise ReceiptError(
                f"{label} names candidate adapter reference {value[:12]} and the "
                f"verified runtime adapter is {adapter['adapter_reference_hash'][:12]}")

    # ── the config the approved plan bound, and the policy values inside it ───
    config = config_from_dict(_load(Path(evaluation_config)))
    if config.config_hash() != str(plan["evaluation_config_hash"]):
        raise ReceiptError(
            f"the supplied configuration hashes to {config.config_hash()[:12]} and the "
            f"approved plan bound {str(plan['evaluation_config_hash'])[:12]}; the "
            f"direct policy values would describe a configuration this run never used")
    generation_policy = config.generation.to_dict()
    if config.generation.policy_hash() != str(report["generation_policy_hash"]):
        raise ReceiptError(
            "the configuration's generation policy does not hash to the one the report "
            "recorded")
    baseline_identity = config.to_dict()["baseline_model"]
    if str(baseline_identity["tokenizer_identity_hash"]) != \
            str(report["tokenizer_identity_hash"]):
        raise ReceiptError(
            "the configuration's baseline tokenizer identity is not the one the report "
            "measured against")

    # ── the source the code came from (FINDING F) ────────────────────────────
    source = source_identity(root)
    if expected_evaluation_source_commit and \
            expected_evaluation_source_commit != source["evaluation_source_commit"]:
        raise ReceiptError(
            f"the caller asserts source commit "
            f"{expected_evaluation_source_commit[:12]} and this worktree is at "
            f"{source['evaluation_source_commit'][:12]}")

    # ── why the verdict follows, rederived rather than copied (FINDING I) ─────
    decision = decision_from_evidence(
        gate_report=report["gate_report"], bootstrap=report["bootstrap"],
        empirical_status=report["empirical_status"], run_state=report["run_state"])
    if decision.to_dict() != report["eligibility"]:
        raise ReceiptError(
            "the canonical decision rederived from this report's own gate, bootstrap, "
            "empirical-status and serialisation-state evidence is not the decision the "
            "report recorded; one of the two is describing a different run")
    status_claim = _status_claim(decision.eligibility.value)
    if not status_claim:
        raise ReceiptError(
            f"eligibility {decision.eligibility.value!r} supports no evaluated candidate "
            f"state; an unrecognised verdict is an unknown one, and UNKNOWN is not a pass")

    counts = {name: file_evidence(directory / source_name)["record_count"]
              for source_name, name in RESULT_COUNT_FILES}
    terminal_event = events["terminal_event"]
    return {
        "schema_version": RECEIPT_V2_SCHEMA_VERSION,
        "receipt_version": RECEIPT_V2_SCHEMA_VERSION,
        "evaluation_milestone": milestone,
        "evaluation_id": evaluation_id,
        "evaluation_generation": generation,
        "source": source,

        "candidate": {
            "candidate_id": candidate_id,
            "status_claim": status_claim,
            "identity_source": "training_receipt",
            "adapter_reference_hash": adapter["adapter_reference_hash"],
            "adapter_sha256": adapter["adapter_sha256"],
            "adapter_manifest_hash": adapter["adapter_manifest_hash"],
            "adapter_artifact_set_hash": adapter["adapter_artifact_set_hash"],
        },
        "training_receipt": training["bound"],
        "baseline": {
            "model_id": str(baseline_identity["model_id"]),
            "revision": str(baseline_identity["revision"]),
            "reference_hash": str(report["baseline_reference_hash"]),
            "tokenizer_identity_hash": str(report["tokenizer_identity_hash"]),
            "base_model_identity_hash": str(baseline_identity["identity_hash"]),
        },

        "holdout": {
            "dataset_id": str(pack_manifest["dataset_id"]),
            "dataset_version": str(pack_manifest["dataset_version"]),
            "dataset_manifest_hash": str(report["dataset_manifest_hash"]),
            "task_pack_hash": str(report["task_pack_hash"]),
            "hidden_target_store_hash": str(report["hidden_target_store_hash"]),
            "pack_manifest_shard_hashes": dict(sorted(
                {k: str(v) for k, v in pack_manifest.get("shard_hashes", {}).items()}
                .items())),
            "split_manifest_hashes": dict(sorted(
                {k: str(v) for k, v in report.get("split_manifest_hashes", {}).items()}
                .items())),
            "task_count": int(report["task_count"]),
            "counts_by_split": dict(sorted(pack_manifest.get("counts_by_split", {})
                                           .items())),
            "counts_by_family": dict(sorted(pack_manifest.get("counts_by_family", {})
                                            .items())),
            "counts_by_kind": dict(sorted(pack_manifest.get("counts_by_kind", {})
                                          .items())),
        },
        "plan": {
            "plan_hash": str(report["plan_hash"]),
            "plan_schema_version": str(plan["plan_schema_version"]),
            "evaluator_version": str(report["evaluator_version"]),
            "evaluation_config_hash": str(plan["evaluation_config_hash"]),
            "order_policy": str(plan["order_policy"]),
            "order_assignment_hash": str(plan["order_assignment_hash"]),
            "performs_inference": bool(plan.get("performs_inference", False)),
            "binds_exact_pack_identity": (
                str(plan["task_pack_hash"]) == str(pack_manifest["pack_hash"])
                and str(plan["hidden_target_store_hash"])
                == str(pack_manifest["hidden_target_store_hash"])),
            "expected_task_count": int(plan["expected_task_count"]),
        },
        "policies": {
            "generation_policy_hash": str(report["generation_policy_hash"]),
            "grader_policy_hash": str(report["grader_policy_hash"]),
            "metric_policy_hash": str(report["metric_policy_hash"]),
            "statistical_policy_hash": str(report["statistical_policy_hash"]),
            "gate_policy_hash": str(report["gate_policy_hash"]),
            "family_policy_hash": str(report["family_policy_hash"]),
            "dependency_report_hash": str(report["dependency_report_hash"]),
            "hardware_report_hash": str(report["hardware_report_hash"]),
            "configured_timeout_s": int(generation_policy["timeout_s"]),
            # D33, unchanged and stated rather than implied. Nothing in this subsystem
            # interrupts a generation that overruns; the number is what was CONFIGURED.
            "timeout_enforced": False,
            "generation_policy": generation_policy,
        },

        "authority": {
            "form": AUTHORITY_FORM,
            "bound_plan_hash": str(report["plan_hash"]),
            "plan_consumption_count": events["counts"].get("started", 0),
            "holdout_commit_count": events["counts"].get(HOLDOUT_COMMIT_EVENT, 0),
            "token_literal_recorded": False,
            "retry_authorized": False,
            "grants_no_further_authority": True,
            "human_authorization": "external_milestone_authority",
        },
        "ledger": {
            "plan_started_count": events["counts"].get("started", 0),
            "holdout_commit_count": events["counts"].get(HOLDOUT_COMMIT_EVENT, 0),
            "terminal_count": events["counts"].get(terminal_event, 0),
            "terminal_event": terminal_event,
            "terminal_state": terminal_event,
            "terminal_is_successful": terminal_event == SUCCESSFUL_TERMINAL_EVENT,
            "events": events["counts"],
            "unrecognised_events": list(events["unrecognised_events"]),
            "plan_hash": events["plan_hash"],
            "unique_plan_hashes": 1,
            **events["event_hashes"],
        },
        "holdout_commit": {
            "commit_schema_version": str(commit.get("commit_schema_version", "")),
            "pack_identity_hash": str(commit.get("pack_identity_hash", "")),
            "order_assignment_hash": str(commit.get("order_assignment_hash", "")),
            "first_task_hash": str(commit.get("first_task_hash", "")),
            "first_arm": str(commit.get("first_arm", "")),
            "first_request_parity_hash": str(commit.get(
                "first_request_parity_hash", "")),
            "task_count": int(commit.get("task_count", 0)),
            "target_count": int(commit.get("target_count", 0)),
            "backend_id": str(commit.get("backend_id", "")),
            "performs_inference": bool(commit.get("performs_inference", False)),
        },

        "execution": {
            "report_serialization_state": str(report["run_state"]),
            "empirical_status": str(report["empirical_status"]),
            "backend_ids": sorted(str(b) for b in report.get("backend_ids", [])),
            "backend_version": str(report.get("backend_version", "")),
            "artifact_verification": "PASS",
            "artifact_problems": [],
        },
        "results": {
            "expected_task_count": int(plan["expected_task_count"]),
            "task_count": int(report["task_count"]),
            **counts,
            "total_model_result_count": (counts["baseline_result_count"]
                                         + counts["candidate_result_count"]),
            "measured_pairs": int(report["measured_pairs"]),
            "missing_pairs": int(report["missing_pairs"]),
            "wins": int(report["wins"]), "ties": int(report["ties"]),
            "losses": int(report["losses"]),
        },
        "evidence": {
            "report_hash": str(report["report_hash"]),
            "evaluation_manifest_hash": str(manifest["manifest_hash"]),
            "evaluation_artifact_tree_hash": str(manifest["tree_hash"]),
            "comparison_manifest_hash": str(report["comparison_manifest_hash"]),
            "metrics_summary_hash": str(metrics["summary_hash"]),
            "pack_manifest_hash": str(pack_manifest["pack_hash"]),
            "gate_report_hash": _sha256_obj(report["gate_report"]),
            "bootstrap_report_hash": _sha256_obj(report["bootstrap"]),
            "files": {name: file_evidence(directory / name)
                      for name in RESULT_SET_FILES
                      if (directory / name).exists()},
        },

        "decision_evidence": {
            "empirical_status": str(report["empirical_status"]),
            "report_serialization_state": str(report["run_state"]),
            "gate_report": report["gate_report"],
            "bootstrap": report["bootstrap"],
            "canonical_decision": decision.to_dict(),
            "decision_hash": decision.decision_hash(),
            "rederived_by": DECISION_REDERIVER,
        },
        "outcome": {
            "eligibility": decision.eligibility.value,
            "human_review_required": bool(decision.human_review_required),
            "promotes_model": False,
            "activates_model": False,
            "mutates_model_registry": False,
            "gate_blockers": [str(b) for b in decision.blockers],
            "gate_warnings": [str(w) for w in decision.warnings],
            "limitations": [str(limit) for limit in report.get("limitations", [])],
        },
    }


def _sha256_obj(payload: object) -> str:
    """The repository's canonical object digest — the one `report_hash()` uses."""
    from training_gym.schemas import sha256_obj
    return sha256_obj(payload)


def verify_receipt_v2(payload: dict) -> tuple[str, ...]:
    """Every reason this modern receipt is not evidence. Reads nothing but the payload.

    Runs in a clean clone with no runtime evaluation directory, no adapter bytes and no
    eval-v4: the generation tree is gitignored and long gone by the time anyone audits
    this. What it CAN do is re-derive the digest, re-derive the generation policy hash
    from the policy the receipt carries, re-derive the gate and bootstrap digests, check
    the counts against each other, and — the point of the whole version — feed the
    body-free decision evidence back into the production decision function and require
    the answer to be the status the receipt claims.
    """
    from training_gym.evaluation.reports import ReportError, decision_from_evidence

    problems: list[str] = []
    stored = str(payload.get("receipt_hash", ""))
    actual = receipt_hash(payload)
    if stored != actual:
        problems.append(
            f"receipt_hash {stored[:12] or '(absent)'} does not match the bytes "
            f"({actual[:12]}); a receipt that can be edited without its digest moving "
            f"is a receipt that can be edited to say anything")

    candidate = payload.get("candidate", {})
    training = payload.get("training_receipt", {})
    ledger = payload.get("ledger", {})
    authority = payload.get("authority", {})
    plan = payload.get("plan", {})
    policies = payload.get("policies", {})
    results = payload.get("results", {})
    evidence = payload.get("evidence", {})
    decision_evidence = payload.get("decision_evidence", {})
    outcome = payload.get("outcome", {})

    # ── direct adapter identity is MANDATORY and non-empty (FINDING A) ────────
    for field in ("adapter_sha256", "adapter_manifest_hash",
                  "adapter_artifact_set_hash", "adapter_reference_hash"):
        value = str(candidate.get(field, ""))
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            problems.append(
                f"candidate.{field} is {value[:16]!r}; a modern receipt records which "
                f"weights were measured or it is not evidence about an adapter")

    # ── the training receipt is the candidate's identity root (FINDING E) ─────
    if candidate.get("identity_source") != "training_receipt":
        problems.append("the receipt does not state that its candidate identity came "
                        "from the training receipt")
    if training.get("candidate_id") != candidate.get("candidate_id"):
        problems.append(
            f"the receipt evaluates {candidate.get('candidate_id')!r} and binds a "
            f"training receipt for {training.get('candidate_id')!r}")
    if not re.fullmatch(r"[0-9a-f]{64}",
                        str(training.get("training_receipt_sha256", ""))):
        problems.append("the receipt binds no usable training-receipt digest")

    # ── one plan, one start, one crossing, one recognised ending ─────────────
    if ledger.get("plan_started_count") != 1:
        problems.append("the receipt does not bind exactly one plan-start event")
    if ledger.get("holdout_commit_count") != 1:
        problems.append(
            "the receipt does not bind exactly one model-facing commit event; without "
            "it there is no evidence a model ever read the holdout")
    if ledger.get("terminal_count") != 1:
        problems.append("the receipt does not bind exactly one terminal event")
    if str(ledger.get("terminal_event", "")) not in TERMINAL_EVALUATION_EVENTS:
        problems.append(
            f"terminal event {ledger.get('terminal_event')!r} is outside the closed "
            f"vocabulary {sorted(TERMINAL_EVALUATION_EVENTS)}; an unrecognised line is "
            f"not a terminal witness")
    if ledger.get("terminal_state") != ledger.get("terminal_event"):
        problems.append("the receipt's terminal event and terminal state disagree")
    if bool(ledger.get("terminal_is_successful")) != (
            str(ledger.get("terminal_event")) == SUCCESSFUL_TERMINAL_EVENT):
        problems.append(
            f"the receipt calls {ledger.get('terminal_event')!r} "
            f"{'successful' if ledger.get('terminal_is_successful') else 'unsuccessful'}; "
            f"only {SUCCESSFUL_TERMINAL_EVENT!r} is a successful ending")
    if str(ledger.get("terminal_event", "")) in \
            [str(e) for e in ledger.get("unrecognised_events", [])]:
        problems.append("an unrecognised event is being used as the terminal witness")
    if ledger.get("unique_plan_hashes") != 1:
        problems.append(
            "the receipt does not bind exactly one plan hash across its ledger lines")
    for label, value in (("authority.bound_plan_hash",
                          str(authority.get("bound_plan_hash", ""))),
                         ("plan.plan_hash", str(plan.get("plan_hash", "")))):
        if value != str(ledger.get("plan_hash", "")):
            problems.append(
                f"{label} is {value[:12]} and the ledger lines name "
                f"{str(ledger.get('plan_hash'))[:12]}")
    for field in ("plan_started_event_hash", "holdout_commit_event_hash",
                  "terminal_event_hash"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(ledger.get(field, ""))):
            problems.append(f"ledger.{field} binds no durable event")

    # ── the authority, described as the ledger can witness it (FINDING B) ─────
    if "creations" in authority:
        problems.append(
            "the receipt counts an authority CREATION event. The ledger owns plan "
            "start and consumption; there is no durable token-creation record to count")
    if authority.get("plan_consumption_count") != 1:
        problems.append("the receipt does not record exactly one plan consumption")
    if authority.get("holdout_commit_count") != ledger.get("holdout_commit_count"):
        problems.append("the authority and ledger sections disagree about the crossing")
    if authority.get("retry_authorized") or authority.get("token_literal_recorded") \
            or not authority.get("grants_no_further_authority"):
        problems.append("the receipt asserts an authority; evidence of an operation "
                        "never authorises another one")
    if authority.get("human_authorization") != "external_milestone_authority":
        problems.append(
            "the receipt claims to establish human authorisation. A builder can write "
            "that field itself, which is exactly why it may not mean anything")

    if not plan.get("binds_exact_pack_identity"):
        problems.append(
            "the approved plan did not bind the exact pack identity that was measured")

    # ── direct policy values, RE-DERIVED rather than quoted (section 36) ──────
    policy = policies.get("generation_policy", {})
    try:
        from training_gym.evaluation.generation import GenerationPolicy
        rebuilt = GenerationPolicy.from_dict(policy).policy_hash()
    except Exception as exc:  # noqa: BLE001 — an unusable policy is a refusal
        rebuilt = ""
        problems.append(f"the receipt's generation policy is not one this repository "
                        f"can rebuild ({type(exc).__name__}: {exc})")
    if rebuilt and rebuilt != str(policies.get("generation_policy_hash", "")):
        problems.append(
            f"the receipt's generation policy hashes to {rebuilt[:12]} and it records "
            f"{str(policies.get('generation_policy_hash'))[:12]}; the human-readable "
            f"values and the digest describe different policies")
    if policies.get("configured_timeout_s") != policy.get("timeout_s"):
        problems.append(
            "the receipt's configured timeout and its generation policy disagree")
    if policies.get("timeout_enforced"):
        problems.append(
            "the receipt claims the configured timeout was ENFORCED. D33 is open and "
            "unchanged: nothing in this subsystem interrupts a generation that overruns")

    # ── the counts a clean clone cannot recompute from absent files ───────────
    expected = results.get("expected_task_count")
    task_count = results.get("task_count")
    if expected != task_count:
        problems.append(
            f"the approved plan expected {expected} task(s) and the run reports "
            f"{task_count}")
    for field in ("measured_pairs", "missing_pairs", "baseline_result_count",
                  "candidate_result_count", "paired_result_count",
                  "baseline_score_count", "candidate_score_count"):
        value = results.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 \
                or (isinstance(task_count, int) and value > task_count):
            problems.append(f"results.{field} is {value!r}, which is not a count of at "
                            f"most {task_count!r} task(s)")
    if results.get("total_model_result_count") != (
            (results.get("baseline_result_count") or 0)
            + (results.get("candidate_result_count") or 0)):
        problems.append("results.total_model_result_count is not the two arms summed")
    if isinstance(task_count, int) and (results.get("measured_pairs") or 0) + \
            (results.get("missing_pairs") or 0) != task_count:
        problems.append("measured and missing pairs do not account for every task")
    if (results.get("wins") or 0) + (results.get("ties") or 0) + \
            (results.get("losses") or 0) != results.get("measured_pairs"):
        problems.append("wins, ties and losses do not account for every measured pair")
    if ledger.get("terminal_is_successful"):
        # A SUCCESSFUL terminal state means the whole pack completed on both arms. A
        # short count under a `completed` ending is a run claiming more than it measured.
        for field in ("baseline_result_count", "candidate_result_count",
                      "paired_result_count", "baseline_score_count",
                      "candidate_score_count", "measured_pairs"):
            if results.get(field) != task_count:
                problems.append(
                    f"the run ended {SUCCESSFUL_TERMINAL_EVENT!r} with "
                    f"results.{field} = {results.get(field)!r} against {task_count!r} "
                    f"task(s); a completed evaluation measured every one of them")
        if results.get("total_model_result_count") != 2 * (task_count or 0):
            problems.append(
                "a completed paired evaluation produces two model results per task")

    # ── the decision, REDERIVED by the production algorithm (FINDING I) ───────
    if decision_evidence.get("rederived_by") != DECISION_REDERIVER:
        problems.append(
            f"the receipt does not name {DECISION_REDERIVER} as the authority that "
            f"rederives its verdict")
    for field, digest in (("gate_report", evidence.get("gate_report_hash")),
                          ("bootstrap", evidence.get("bootstrap_report_hash"))):
        if field in decision_evidence and \
                _sha256_obj(decision_evidence[field]) != digest:
            problems.append(
                f"evidence.{field}_hash does not match the {field} evidence carried")
    try:
        decision = decision_from_evidence(
            gate_report=decision_evidence.get("gate_report"),
            bootstrap=decision_evidence.get("bootstrap"),
            empirical_status=decision_evidence.get("empirical_status"),
            run_state=decision_evidence.get("report_serialization_state"))
    except (ReportError, KeyError, TypeError, ValueError) as exc:
        problems.append(
            f"the canonical decision could not be rederived from the receipt's own "
            f"evidence ({type(exc).__name__}: {exc}); a status claim nobody can check "
            f"is the claim this version exists to remove")
        return tuple(problems)

    if decision.to_dict() != decision_evidence.get("canonical_decision"):
        problems.append(
            "the canonical decision rederived from this receipt's body-free evidence is "
            "not the decision the receipt records")
    if decision.decision_hash() != decision_evidence.get("decision_hash"):
        problems.append("the receipt's decision_hash is not the digest of the decision "
                        "its evidence produces")
    if decision.eligibility.value != outcome.get("eligibility"):
        problems.append(
            f"the receipt's outcome is {outcome.get('eligibility')!r} and its evidence "
            f"produces {decision.eligibility.value!r}")
    if bool(decision.human_review_required) != bool(
            outcome.get("human_review_required")):
        problems.append("the receipt and its evidence disagree about human review")
    expected_state = _status_claim(decision.eligibility.value)
    if candidate.get("status_claim") != expected_state:
        problems.append(
            f"the receipt claims candidate state {candidate.get('status_claim')!r} and "
            f"its own evidence supports {expected_state or 'no evaluated state'}")
    if list(outcome.get("gate_blockers", [])) != list(decision.blockers):
        problems.append("the receipt's blockers are not the ones its evidence produces")
    if list(outcome.get("gate_warnings", [])) != list(decision.warnings):
        problems.append("the receipt's warnings are not the ones its evidence produces")
    for flag in ("promotes_model", "activates_model", "mutates_model_registry"):
        if outcome.get(flag):
            problems.append(f"the receipt claims {flag}; no mechanism in this repository "
                            f"could have performed it")
    return tuple(problems)


def _receipt_verifiers() -> dict:
    """Every receipt version this repository can read, and what reads it.

    Built on demand rather than at import time so the module stays importable without the
    control plane loaded. A version outside this map is REFUSED: an unknown contract is
    not a satisfied one.
    """
    from scripts.verify_m62_control_plane import (
        eval_receipt_schema,
        eval_receipt_v2_schema,
    )
    return {
        RECEIPT_SCHEMA_VERSION: (eval_receipt_schema, verify_receipt),
        RECEIPT_V2_SCHEMA_VERSION: (eval_receipt_v2_schema, verify_receipt_v2),
    }


def verify_receipt_payload(payload: object) -> tuple[str, ...]:
    """Strict SCHEMA first, then semantics, then the content rules. In that order.

    FINDING C. `.1`'s standalone `--verify` called `verify_receipt()` and nothing else,
    so a document that was not a receipt at all — unknown schema version, arbitrary keys,
    missing every adapter identity — printed `"status": "verified"` as long as its
    `receipt_hash` had been recomputed over its own nonsense. Malformed evidence was
    being called verified.
    """
    from scripts.verify_m62_control_plane import (
        EVAL_V4_TASK_IDS,
        FORBIDDEN_BODY_SYMBOLS,
        PRIVATE_PATH_RE,
        TOKEN_LITERAL_RE,
        canonical_json,
        validate_against_schema,
    )

    if not isinstance(payload, dict):
        return (f"the document is a {type(payload).__name__}, not a receipt object",)
    version = str(payload.get("schema_version", ""))
    verifiers = _receipt_verifiers()
    if version not in verifiers:
        return (f"receipt schema version {version or '(absent)'!r} is not one this "
                f"repository knows how to verify {sorted(verifiers)}; an unknown "
                f"contract is not a satisfied one",)
    if str(payload.get("receipt_version", "")) != version:
        return (f"schema_version {version!r} and receipt_version "
                f"{payload.get('receipt_version')!r} disagree",)

    schema_builder, semantic = verifiers[version]
    problems = [f"schema: {p}" for p in validate_against_schema(schema_builder(),
                                                                payload)]
    if problems:
        # A payload that does not satisfy its own contract has no semantics to check;
        # running them anyway would produce findings about a document shape nobody
        # promised.
        return tuple(problems)
    problems.extend(semantic(payload))

    text = canonical_json(payload)
    if TOKEN_LITERAL_RE.search(text):
        problems.append("the receipt carries something shaped like a spendable plan "
                        "token; a receipt proves an authority was spent, never "
                        "reproduces one")
    for match in PRIVATE_PATH_RE.findall(text):
        problems.append(f"the receipt carries a private host path {match!r}")
    for symbol in FORBIDDEN_BODY_SYMBOLS:
        if symbol in text:
            problems.append(f"the receipt references {symbol!r}, an eval-v4 body source")
    named = sorted({tid for tid in EVAL_V4_TASK_IDS if tid in text})
    if named:
        problems.append(f"the receipt names eval-v4 task(s) {named[:4]}")
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        problems.append("the receipt is not ASCII, so its canonical bytes depend on an "
                        "encoding choice")
    return tuple(problems)


def read_receipt_file(path: str | Path) -> dict:
    """Read a receipt for VERIFICATION. Creates nothing, writes nothing, follows no link."""
    target = Path(path)
    if target.is_symlink():
        raise ReceiptError(
            f"{target.name}: is a symlink; verifying evidence through a link verifies "
            f"whatever the link points at today")
    if not target.is_file():
        raise ReceiptError(f"{target.name}: is not a regular file")
    size = target.stat().st_size
    if size > MAX_RECEIPT_BYTES:
        raise ReceiptError(
            f"{target.name}: {size} bytes exceeds the {MAX_RECEIPT_BYTES}-byte ceiling "
            f"for a body-free receipt")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{target.name}: is not readable JSON ({exc})") from None


def emit_receipt(destination: str | Path, text: str) -> dict:
    """Write the receipt atomically, then re-read the bytes that landed and verify THEM.

    WHY THIS IS NOT AN ORDINARY `write_text`
    ----------------------------------------
    Portable evidence is written AFTER an irreversible act. If the process dies mid-write
    there is no second holdout to run again, and a truncated file that parses far enough
    to look plausible is worse than no file: it is a document nobody can distinguish from
    the real one without the run it describes.

    So the final bytes never exist in a partial state. The receipt is written to a
    temporary file in the SAME directory (same filesystem, so `os.replace` is atomic),
    flushed, fsynced, re-read, strictly verified, and only then moved into place. A
    failure at any point before the move leaves NO file at the destination.

    An existing destination is refused rather than overwritten, and a symlink destination
    is refused rather than followed: the point of writing evidence atomically is lost if
    the last step is "clobber whatever was there".
    """
    target = Path(destination)
    if target.is_symlink():
        raise ReceiptError(
            f"{target.name}: the destination is a symlink; writing evidence through a "
            f"link writes it wherever the link points, which is not where the caller "
            f"said")
    if target.exists():
        raise ReceiptError(
            f"{target.name}: already exists. A receipt describes one irreversible "
            f"operation and silently replacing one destroys the only copy of the "
            f"evidence for another")
    target.parent.mkdir(parents=True, exist_ok=True)

    data = text.encode("utf-8")
    handle, temporary = tempfile.mkstemp(dir=str(target.parent),
                                         prefix=".m62-eval-receipt-", suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        # Re-read and verify the bytes ON DISK before they become the receipt. A
        # serialiser or write defect must not be able to emit a success for bytes the
        # verifier would refuse.
        written = temporary_path.read_bytes()
        if written != data:
            raise ReceiptError(
                f"{target.name}: the bytes on disk are not the bytes serialised "
                f"({len(written)} vs {len(data)})")
        try:
            candidate = json.loads(written.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptError(
                f"{target.name}: the bytes written are not readable JSON ({exc}); a "
                f"truncated or mis-encoded receipt must never reach the destination"
            ) from None
        problems = verify_receipt_payload(candidate)
        if problems:
            raise ReceiptError(
                f"the receipt just built does not verify: {'; '.join(problems[:4])}")

        os.replace(temporary_path, target)
        temporary_path = None  # type: ignore[assignment]
    finally:
        # ONLY the exact temporary file this invocation created. Never a directory,
        # never a glob, never the destination's parent.
        if temporary_path is not None and temporary_path.is_file():
            temporary_path.unlink()

    try:
        directory = os.open(str(target.parent), os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        directory = -1
    if directory >= 0:
        try:
            os.fsync(directory)
        except OSError:  # pragma: no cover - filesystems that refuse directory fsync
            pass
        finally:
            os.close(directory)

    final = target.read_bytes()
    problems = verify_receipt_payload(json.loads(final.decode("utf-8")))
    if problems:
        # The destination was created by THIS invocation (a pre-existing one was
        # refused above), so removing it removes only our own failed write, and a
        # refusal must not leave a file a later reader would take for evidence.
        target.unlink()
        raise ReceiptError(
            f"the receipt at its destination does not verify: "
            f"{'; '.join(problems[:4])}")
    return {"receipt": target.name, "bytes": len(final),
            "sha256": _sha256_bytes(final)}


def main(argv: list[str] | None = None) -> int:
    """Two modes, and they no longer pretend to be one.

    FINDING D. `.1` marked `--generation-directory`, `--candidate` and
    `--evaluation-source-commit` REQUIRED, so verifying an existing receipt meant passing
    three dummy build arguments the verifier then ignored. Anything a caller has to
    invent to run a check is something the check is not really using, and a read-only
    operation that demands write-mode arguments teaches operators to supply fiction.
    """
    from scripts.verify_m62_control_plane import canonical_json

    parser = argparse.ArgumentParser(
        description="Build or verify the tracked, root-independent receipt for one M62 "
                    "evaluation generation. Evaluates nothing and spends nothing.")
    modes = parser.add_subparsers(dest="mode", required=True)

    build = modes.add_parser(
        "build", help="distil one completed generation into a receipt")
    build.add_argument("--generation-directory", required=True,
                       help="the completed generation directory (gitignored runtime)")
    build.add_argument("--training-receipt", required=True,
                       help="the tracked S3P training receipt for this candidate")
    build.add_argument("--adapter-run-directory", required=True,
                       help="the training run directory holding the adapter that was "
                            "evaluated")
    build.add_argument("--evaluation-config", required=True,
                       help="the evaluation configuration the approved plan bound")
    build.add_argument("--ledger", default="",
                       help="the evaluation ledger; defaults to the run tree's own")
    build.add_argument("--repo-root", default=str(_ROOT.parent),
                       help="the repository the evaluation executed from")
    build.add_argument("--expected-candidate", default="",
                       help="ASSERTION: refuse unless the training receipt seals this "
                            "candidate. The receipt DERIVES the identity either way")
    build.add_argument("--expected-evaluation-source-commit", default="",
                       help="ASSERTION: refuse unless the repository HEAD is this commit")
    build.add_argument("--milestone", default="S3Q", help="the evaluation milestone")
    build.add_argument("--emit", default="",
                       help="write the receipt here atomically; prints to stdout when "
                            "omitted")

    verify = modes.add_parser(
        "verify", help="strictly verify an existing receipt. Read-only, standalone")
    verify.add_argument("receipt", help="the receipt to verify")

    args = parser.parse_args(argv)

    if args.mode == "verify":
        # Nothing here needs a generation directory, a candidate, a source commit, a
        # ledger, an adapter or eval-v4. It reads one file and checks it.
        try:
            payload = read_receipt_file(args.receipt)
        except ReceiptError as exc:
            print(json.dumps({"status": "REFUSED", "problems": [str(exc)]}, indent=2))
            return 1
        problems = verify_receipt_payload(payload)
        print(json.dumps({
            "status": "PASS" if not problems else "REFUSED",
            "receipt_version": str(payload.get("receipt_version", "")),
            "candidate": str(payload.get("candidate", {}).get("candidate_id", "")),
            "status_claim": str(payload.get("candidate", {}).get("status_claim", "")),
            "problems": list(problems)}, indent=2))
        return 0 if not problems else 1

    directory = Path(args.generation_directory)
    ledger = Path(args.ledger) if args.ledger else \
        directory.parent.parent.parent / "evaluation_runs.jsonl"
    try:
        payload = seal(build_receipt_v2(
            directory, training_receipt=args.training_receipt,
            adapter_run_directory=args.adapter_run_directory,
            evaluation_config=args.evaluation_config, ledger=ledger,
            repo_root=args.repo_root, expected_candidate=args.expected_candidate,
            expected_evaluation_source_commit=(
                args.expected_evaluation_source_commit),
            milestone=args.milestone))
        text = canonical_json(payload)
        result = emit_receipt(args.emit, text) if args.emit else {}
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
        print(json.dumps({"status": "refused",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1

    if args.emit:
        print(json.dumps({"status": "ok", **result}, indent=2))
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
