"""scripts/build_m62_eval_receipt.py — V69 M62 S3Q.0 / S3Q.0.1 / S3Q.0.2: the portable evaluation receipt.

READ THIS FIRST — THREE VERSIONS LIVE HERE, AND NONE OF THEM IS REWRITTEN
-------------------------------------------------------------------------
``m62.eval_receipt.1`` (S3Q.0) is below, unchanged, and is the contract the synthetic
S3Q.0 receipts already hash against. ``m62.eval_receipt.2`` (S3Q.0.1) is unchanged too.
``m62.eval_receipt.3`` (S3Q.0.2) is the POST-LIVE SEAL RECOVERY contract and the one a
real measurement is sealed under from now on.

Each version is left exactly as it was because each is a TRACKED CONTRACT that existing
documents were hashed under. A semantic rewrite in place would silently change what those
documents mean, which is the one thing a versioned evidence form exists to prevent.

WHY `.3` EXISTS
---------------
`.2` was qualified against SYNTHETIC evidence and then met the real S3Q run, which it
refused three times — for a four-verdict production partition it modelled as three, for a
correctly typeset U+2212 in a gate message it required to be ASCII, and for conflating the
commit that MEASURED with the commit that BUILD THE RECEIPT. All three were `.2` being
wrong about production. The repair moved the CONTRACT to the evidence; no report, gate
message, verdict or digest was edited to fit a receipt. See the `.3` section below.

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


# ══════════════════════════════════════════════════════════════════════════════
#  m62.eval_receipt.3 — S3Q.0.2, POST-LIVE SEAL RECOVERY
# ══════════════════════════════════════════════════════════════════════════════
#
# `.2` was qualified against synthetic evidence. It then met the real S3Q measurement and
# refused it three times, and all three refusals were `.2` being wrong about production:
#
#   A. it required `wins + ties + losses == measured_pairs`, and `comparison.py`
#      classifies FOUR comparable verdicts. The fourth, `security_improvement`, is
#      deliberately not a win — the baseline had a blocking finding the candidate fixed,
#      which is reported and never rewarded. On the real run 11 + 12 + 10 = 33 against 36
#      measured pairs, so a correct measurement was called inconsistent;
#   B. it required the canonical receipt text to be ASCII, and a production gate message
#      reads `schema validity fell from 1.0000 to 0.8889 (−0.1111)` with a correctly
#      typeset U+2212 MINUS SIGN. The report is valid; `.2` refused its representation;
#   C. it derived `evaluation_source_commit` from the repository HEAD at RECEIPT-BUILD
#      time. That is the evaluation source only while sealing happens at the unchanged
#      evaluated commit — true until sealing failed. A repair commit moves HEAD, and
#      after it `.2` can only name the REPAIR as the evaluation source or refuse the
#      truthful assertion.
#
# The repair adapts the RECEIPT to valid production evidence. It does NOT adapt production
# evidence to satisfy the receipt: no report was edited, no gate message rewritten, no
# minus sign normalised, no verdict reclassified, no report hash moved.
#
# `.1` and `.2` are untouched. They are tracked, qualified contracts whose documents were
# written and hashed under their rules, and a semantic rewrite in place would change what
# those documents mean.

RECEIPT_V3_SCHEMA_VERSION = "m62.eval_receipt.3"

#: `.3`'s canonical bytes, DEFINED rather than constrained. `.2` closed the "which
#: encoding?" ambiguity by refusing non-ASCII, which removed legitimate evidence to
#: remove a question. `.3` closes it by answering the question: the bytes ARE canonical
#: JSON encoded UTF-8, `receipt_hash` is SHA-256 over exactly those bytes, and no choice
#: is left open. Unicode is PERMITTED, not privileged: the token, private-path,
#: body-symbol and task-id scanners are unchanged.
CANONICAL_RECEIPT_ENCODING = (
    "canonical JSON (scripts.verify_m62_control_plane.canonical_json) encoded UTF-8")

#: The witness form `.3` binds its evaluation source through.
MEASUREMENT_WITNESS_SCHEMA_VERSION = "m62.measurement_witness.1"

#: The tracked scope the witness's source digest covers: the evaluation machinery itself.
#: A subtree rather than an import graph, because a reader must be able to re-derive it
#: from the method string alone -- an import graph reconstructed years later is not the
#: same set, and a digest nobody can reproduce is a number rather than evidence.
WITNESS_SOURCE_SCOPE = "jarvis/training_gym"
WITNESS_SOURCE_DIGEST_METHOD = (
    "sha256(canonical_json({tracked_path: sha256(bytes)})) over every tracked file "
    "under jarvis/training_gym/")

WITNESS_PURPOSE = (
    "record, while the repository is still at the evaluation source commit, which "
    "existing runtime measurement belongs to that unchanged source state, so a later "
    "seal-recovery receipt can bind the evaluation source without deriving it from a "
    "repair-time HEAD")

#: The three `.2` refusals this recovery reproduced against the REAL measurement before
#: any repair was written. Named, so the witness records WHY a second evidence form had
#: to exist rather than leaving a reader to infer it.
RECEIPT_V2_SEAL_FAILURE_CLASSES: tuple[str, ...] = (
    "three_way_verdict_partition_incompatible_with_four_verdict_production_vocabulary",
    "ascii_only_receipt_contract_incompatible_with_canonical_unicode_decision_text",
    "evaluation_source_conflated_with_receipt_build_source",
)


def redact_held_out_task_ids(text: object) -> str:
    """Replace any held-out task identifier with a placeholder naming its corpus.

    D-S4F-4. A receipt is TRACKED and published; the report it seals is gitignored
    evidence. `verify_m62_control_plane` refuses a receipt that names a held-out task id,
    and it is right to: a task id in a published file is a hint about a single-use exam,
    delivered in instalments. But a gate blocker naming the task it fired on is the
    report's own decision text, and dropping the blocker instead would hide WHY a
    candidate failed.

    So the identifier is replaced and the sentence is kept. The corpus is still named, the
    reason is still legible, and the unredacted text stays recoverable from the report the
    receipt binds by digest. The table comes from the verifier that enforces the rule, so
    the two cannot drift into disagreeing about what a held-out id is.
    """
    from scripts.verify_m62_control_plane import HELD_OUT_TASK_IDS

    out = str(text)
    for version, task_ids in HELD_OUT_TASK_IDS.items():
        for task_id in task_ids:
            if task_id in out:
                out = out.replace(task_id, f"<eval-{version} task id redacted>")
    return out


def redact_tree(value: object) -> object:
    """`redact_held_out_task_ids` over every string in a nested structure.

    The gate report and the canonical decision are copied into the receipt wholesale, and
    a blocker's message travels inside both. Redacting only the outcome's copy would leave
    the same id in the evidence block two keys away.
    """
    if isinstance(value, dict):
        return {k: redact_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_tree(v) for v in value]
    if isinstance(value, str):
        return redact_held_out_task_ids(value)
    return value


def production_verdicts() -> tuple[str, ...]:
    """The canonical paired-comparison vocabulary, from the production enum itself.

    Never a literal here. `comparison.py` owns what a verdict is; a second copy in the
    receipt layer is a second thing to forget to update, and the whole reason `.2` refused
    the real run is that it had quietly assumed a three-way vocabulary that production
    never had.
    """
    from training_gym.evaluation.comparison import ComparisonVerdict
    return tuple(sorted(v.value for v in ComparisonVerdict))


def comparison_partitions(generation_directory: str | Path, *,
                          measured_pairs: int) -> dict:
    """The two partitions of the measured pairs, READ rather than recomputed.

    `paired-comparisons.jsonl` is canonically classified BODY_FREE — it carries verdicts,
    digests and deltas, never a prompt, a target or a response — and the `verdict` on each
    line is the one `comparison.py` already assigned. This function READS those verdicts.
    It does not classify: implementing a second classification algorithm here is how the
    receipt layer would acquire an opinion about what a win is, and there may be exactly
    one such opinion in the repository.

    Two partitions come back, and they are NOT the same partition:

      * `verdict_counts` — the production verdict per pair. `security_improvement` is its
        own class and is never folded into `improved`.
      * `numeric_delta_counts` — the SIGN of the reward delta per pair. A pair can carry a
        positive delta and still be classified `security_improvement`, and one classified
        `unchanged` can carry a delta that is not zero.

    Only the TOTALS must agree, and only because both cover the same pairs. Requiring them
    to agree bucket-for-bucket would be asserting that the verdict is a function of the
    delta, which production does not claim.

    D-S4F-1. Both partitions cover every CLASSIFIED pair; ``measured_pairs`` counts only
    the COMPARABLE ones. Those two numbers are equal exactly when no pair carries a
    blocking verdict — true of every run sealed before S4F, which is why `.3` compared
    them directly. A run that produces a ``security_regression`` classifies the pair and
    excludes it from the quality denominator, so the totals legitimately differ by the
    number of blocking pairs, and the old equality made the one outcome the security gates
    exist to produce the one outcome that could not be sealed. The identity below is the
    same check where it used to apply (``blocking == 0`` reduces it to the old one) and
    the correct one where it did not. ``is_comparable`` is READ from the production enum,
    never re-decided here: which verdicts count toward the quality denominator is
    `comparison.py`'s opinion, and there is exactly one such opinion in the repository.
    """
    from training_gym.evaluation.comparison import ComparisonVerdict

    path = Path(generation_directory) / "paired-comparisons.jsonl"
    if path.is_symlink() or not path.is_file():
        raise ReceiptError(
            f"{path.name}: not a regular file in this generation; the verdict partition "
            f"has no source")
    vocabulary = production_verdicts()
    counts = dict.fromkeys(vocabulary, 0)
    positive = zero = negative = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        verdict = str(record.get("verdict", ""))
        if verdict not in counts:
            raise ReceiptError(
                f"a paired comparison carries verdict {verdict!r}, which is not in the "
                f"production vocabulary {list(vocabulary)}; a receipt may not partition "
                f"over classes it does not recognise")
        counts[verdict] += 1
        delta = float(record.get("reward_delta", 0.0))
        positive += delta > 0
        negative += delta < 0
        zero += delta == 0
    total = sum(counts.values())
    blocking = sum(n for verdict, n in counts.items()
                   if not ComparisonVerdict(verdict).is_comparable)
    comparable = total - blocking
    if comparable != measured_pairs:
        raise ReceiptError(
            f"the canonical verdicts account for {comparable} comparable pair(s) "
            f"({total} classified, {blocking} blocking) and the report measured "
            f"{measured_pairs}; the receipt would bind a partition of a different run")
    if positive + zero + negative != total:
        raise ReceiptError(
            f"the numeric deltas account for {positive + zero + negative} pair(s) and the "
            f"canonical verdicts classify {total}")
    # DELIBERATELY NOT RETURNED: the classified/blocking/comparable split. It flows
    # straight into the receipt's `results` block, whose schema is closed, and a closed
    # schema is how a receipt stays body-free. A reader recovers the split from
    # `verdict_counts` and the production enum, so binding it would add no evidence.
    return {
        "verdict_counts": counts,
        "verdict_vocabulary": list(vocabulary),
        "numeric_delta_counts": {"positive": positive, "zero": zero,
                                 "negative": negative},
    }


def read_measurement_witness(path: str | Path) -> dict:
    """Read one witness and check everything about it that needs no repository.

    Separated from the topology check because the ORDER matters: a witness that describes
    a different measurement should be refused for describing a different measurement, not
    for some incidental fact about which commit carries it. Cheap content agreement
    first, Git plumbing second.
    """
    witness_path = Path(path)
    if witness_path.is_symlink() or not witness_path.is_file():
        raise ReceiptError(
            f"{witness_path.name}: the measurement witness is not a regular file; "
            f"without it a post-repair receipt has no truthful evaluation source")
    raw = witness_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"the measurement witness is unreadable ({exc})") from None
    if str(payload.get("schema_version", "")) != MEASUREMENT_WITNESS_SCHEMA_VERSION:
        raise ReceiptError(
            f"the measurement witness declares schema "
            f"{payload.get('schema_version')!r}, not "
            f"{MEASUREMENT_WITNESS_SCHEMA_VERSION!r}")
    if payload.get("witness_kind") != "pre_repair_measurement_witness":
        raise ReceiptError(
            f"{witness_path.name} is a {payload.get('witness_kind')!r}, not a pre-repair "
            f"measurement witness")
    grants = payload.get("grants", {})
    if any(grants.get(flag) for flag in ("candidate_state", "promotion", "activation",
                                         "registry_mutation", "retry_or_rerun",
                                         "is_an_evaluation_receipt")):
        raise ReceiptError(
            "the measurement witness claims to grant something; a witness records facts "
            "and authorises nothing, and one that does is a receipt wearing another name")

    from scripts.verify_m62_control_plane import canonical_json
    body = {k: v for k, v in payload.items() if k != "witness_hash"}
    if payload.get("witness_hash") != _sha256_bytes(canonical_json(body).encode("utf-8")):
        raise ReceiptError(
            "the measurement witness's own digest does not match its bytes")
    return {"payload": payload, "raw": raw, "path": witness_path}


def measurement_witness_evidence(path: str | Path, *, repo_root: Path) -> dict:
    """Bind the PRE-REPAIR witness, and derive the evaluation source from it.

    This is the whole of finding C's remedy in one function. The evaluation source does
    not come from `git rev-parse HEAD` here — it comes from a document that was written
    and committed while HEAD still WAS the evaluation source, whose Git first parent is
    that commit. HEAD at this moment is the seal implementation source and is recorded
    under that name.
    """
    read = read_measurement_witness(path)
    payload, raw, witness_path = read["payload"], read["raw"], read["path"]
    try:
        pointer = witness_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise ReceiptError(
            f"the measurement witness {witness_path} is outside the repository") from None

    # The commit that INTRODUCED this witness, and the parent topology that fixes the
    # evaluation source. Derived from Git, never accepted as a caller string.
    code, commit = _git(repo_root, "rev-list", "--max-count=1", "HEAD", "--", pointer)
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReceiptError(
            f"the commit that carries {pointer} could not be derived; an uncommitted "
            f"witness cannot bridge a repair commit")
    code, parents = _git(repo_root, "rev-list", "--parents", "-n", "1", commit)
    chain = parents.split() if code == 0 else []
    first_parent = chain[1] if len(chain) > 1 else ""
    source = payload.get("evaluation_source", {})
    declared = str(source.get("evaluation_source_commit", ""))
    if first_parent != declared:
        raise ReceiptError(
            f"the measurement witness commit {commit[:12]} has first parent "
            f"{first_parent[:12] or '(none)'} and the witness declares evaluation source "
            f"{declared[:12]}; the Git topology is what fixes the evaluation source and "
            f"it does not agree with the document")
    code, blob = _git(repo_root, "rev-parse", f"{commit}:{pointer}")
    # The RESOLVED path, never the caller's: `git -C <root>` chdirs first, so a relative
    # path would be resolved from somewhere the caller never named.
    code2, current = _git(repo_root, "hash-object", "--",
                          str(witness_path.resolve()))
    if code != 0 or code2 != 0 or blob != current:
        raise ReceiptError(
            f"{pointer} is not the blob its own commit recorded; a bridge edited after it "
            f"was laid is not one")

    return {
        "payload": payload,
        "bound": {
            "path": pointer,
            "measurement_witness_sha256": _sha256_bytes(raw),
            "measurement_witness_hash": str(payload["witness_hash"]),
            "measurement_witness_commit": commit,
            "witness_schema_version": MEASUREMENT_WITNESS_SCHEMA_VERSION,
            "witness_first_parent_is_evaluation_source": True,
            "grants_no_authority": True,
        },
        "evaluation_source": {
            "evaluation_source_commit": declared,
            "evaluation_source_tree_oid": str(source["evaluation_source_tree_oid"]),
            "evaluation_source_digest": str(source["evaluation_source_digest"]),
            "derived_from": "measurement_witness",
            "evidence_level": (
                "the measurement witness was written in a clean worktree at this commit, "
                "before the repair moved HEAD, and its Git first parent is this commit; "
                "that is repository provenance and not proof of which bytes executed"),
        },
    }


def seal_implementation_identity(repo_root: str | Path, *,
                                 evaluation_source_commit: str) -> dict:
    """The tracked code that BUILT this receipt. HEAD, named honestly for once.

    `.2` derived this same value and called it `evaluation_source_commit`. It is not one:
    this commit did not measure anything. Recording it under its own name is finding C's
    other half — the conflation is removed by there being two fields, not by choosing
    which single field to lie in.
    """
    root = Path(repo_root)
    code, commit = _git(root, "rev-parse", "HEAD")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReceiptError(
            f"the seal implementation commit could not be derived from {root} ({commit})")
    code, tree = _git(root, "rev-parse", "HEAD^{tree}")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ReceiptError(f"the seal implementation tree oid could not be derived")
    code, status = _git(root, "status", "--porcelain")
    if code != 0 or status.strip():
        raise ReceiptError(
            "the worktree is not clean; a receipt built from uncommitted code names a "
            "seal implementation source that nobody else can obtain")
    return {
        "seal_implementation_source_commit": commit,
        "seal_implementation_tree_oid": tree,
        "derived_from_repository_head": True,
        "worktree_clean_at_build": True,
        "differs_from_evaluation_source": commit != evaluation_source_commit,
        "evidence_level": (
            "this is the tracked code that BUILT the receipt; it did not measure, and "
            "recording it as the evaluation source is the defect m62.eval_receipt.3 "
            "exists to fix"),
    }


def witness_source_identity(repo_root: str | Path) -> dict:
    """The EVALUATION source, derived while the repository is still standing on it.

    This is the only function in the receipt layer permitted to call `rev-parse HEAD` and
    label the answer an evaluation source, and it may do so only from a CLEAN worktree --
    because a witness is written BEFORE the repair commit exists, when HEAD genuinely is
    the commit that measured. Once a repair lands, `seal_implementation_identity` is what
    reads HEAD, and it calls the result what it is.

    `evaluation_source_digest` is a content digest over the tracked evaluation machinery,
    defined by the method string recorded beside it so any reader can re-derive it with
    two Git commands. It is deliberately NOT an execution attestation: it says which
    tracked bytes were present, never which bytes ran.
    """
    root = Path(repo_root)
    code, commit = _git(root, "rev-parse", "HEAD")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReceiptError(
            f"the evaluation source commit could not be derived from {root} ({commit})")
    code, tree = _git(root, "rev-parse", "HEAD^{tree}")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ReceiptError("the evaluation source tree oid could not be derived")
    code, status = _git(root, "status", "--porcelain")
    if code != 0 or status.strip():
        raise ReceiptError(
            "the worktree is not clean; a witness written over uncommitted changes "
            "records a source state no commit carries")
    code, listing = _git(root, "ls-files", "--", WITNESS_SOURCE_SCOPE)
    tracked = sorted(line for line in listing.split("\n") if line.strip())
    if not tracked:
        raise ReceiptError(
            f"no tracked file under {WITNESS_SOURCE_SCOPE!r}; the evaluation source "
            f"digest would be a digest of nothing")

    from scripts.verify_m62_control_plane import canonical_json
    digests = {rel: _sha256_bytes((root / rel).read_bytes()) for rel in tracked}
    return {
        "evaluation_source_commit": commit,
        "evaluation_source_tree_oid": tree,
        "evaluation_source_digest": _sha256_bytes(
            canonical_json(digests).encode("utf-8")),
        "evaluation_source_digest_method": WITNESS_SOURCE_DIGEST_METHOD,
        "evaluation_source_file_count": len(tracked),
        "worktree_clean_at_witness": True,
        "derived_from_repository_head": True,
        "evidence_level": (
            "the witness was written in a clean worktree at this commit; it establishes "
            "repository provenance and does not prove which bytes executed"),
    }


def build_measurement_witness(generation_directory: str | Path, *,
                              ledger: str | Path,
                              training_receipt: str | Path,
                              evaluation_source: dict,
                              repo_root: str | Path,
                              milestone: str = "S3Q.0.2",
                              seal_failure_classes: "tuple[str, ...]" = ()) -> dict:
    """The pre-repair witness, assembled from EXISTING evidence and sealed.

    Runs nothing. Loads no model. Opens no body. Every value is re-derived from the
    generation directory the run already wrote, the ledger it already appended to and the
    training receipt that already sealed the weights.

    `evaluation_source` is passed IN rather than derived here, for one reason: a witness
    is only truthful if it was written while HEAD still was the evaluation source, and
    `witness_source_identity` is the function that establishes that -- refusing an unclean
    worktree in the process. Separating them is also what lets a later reader RE-DERIVE
    every runtime-borne field of an existing witness without a time machine.
    """
    from scripts.verify_m62_control_plane import canonical_json
    from training_gym.evaluation.artifacts import verify_evaluation_generation
    from training_gym.evaluation.reports import (
        decision_from_evidence,
        verify_report_payload,
    )
    from training_gym.evaluation.store import HOLDOUT_COMMIT_EVENT

    directory = Path(generation_directory)
    report = verify_report_payload(_load(directory / "evaluation-report.json"))
    plan = _load(directory / "evaluation-plan.json")
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
    training = training_receipt_evidence(training_receipt, repo_root=Path(repo_root))

    decision = decision_from_evidence(
        gate_report=report["gate_report"], bootstrap=report["bootstrap"],
        empirical_status=report["empirical_status"], run_state=report["run_state"])
    if decision.to_dict() != report["eligibility"]:
        raise ReceiptError(
            "the canonical decision rederived from this report's own evidence is not the "
            "decision the report recorded")

    measured_pairs = int(report["measured_pairs"])
    partitions = comparison_partitions(directory, measured_pairs=measured_pairs)
    files = {name: file_evidence(directory / name) for name in RESULT_SET_FILES
             if (directory / name).exists()}
    counts = {name: files[source_name]["record_count"]
              for source_name, name in RESULT_COUNT_FILES}

    payload = {
        "schema_version": MEASUREMENT_WITNESS_SCHEMA_VERSION,
        "witness_version": MEASUREMENT_WITNESS_SCHEMA_VERSION,
        "witness_kind": "pre_repair_measurement_witness",
        "milestone": milestone,
        "purpose": WITNESS_PURPOSE,
        "grants": {
            "candidate_state": False,
            "promotion": False,
            "activation": False,
            "registry_mutation": False,
            "retry_or_rerun": False,
            "is_an_evaluation_receipt": False,
            "note": "a witness records facts; it authorises nothing",
        },
        "evaluation_id": evaluation_id,
        "evaluation_generation": generation,
        "candidate_id": training["bound"]["candidate_id"],
        "evaluation_source": dict(evaluation_source),
        "eval_corpus": {
            "dataset_id": str(pack_manifest["dataset_id"]),
            "dataset_version": str(pack_manifest["dataset_version"]),
            "dataset_manifest_hash": str(report["dataset_manifest_hash"]),
            "task_pack_hash": str(report["task_pack_hash"]),
            "hidden_target_store_hash": str(report["hidden_target_store_hash"]),
            "pack_manifest_hash": str(pack_manifest["pack_hash"]),
            "status_claim": "USED_IMMUTABLE",
            "spent_once": True,
        },
        "plan": {
            "plan_hash": str(report["plan_hash"]),
            "plan_schema_version": str(plan["plan_schema_version"]),
            "evaluation_config_hash": str(plan["evaluation_config_hash"]),
            "order_assignment_hash": str(plan["order_assignment_hash"]),
            "expected_task_count": int(plan["expected_task_count"]),
            "performs_inference": bool(plan.get("performs_inference", False)),
        },
        "evidence": {
            "report_hash": str(report["report_hash"]),
            "evaluation_manifest_hash": str(manifest["manifest_hash"]),
            "evaluation_artifact_tree_hash": str(manifest["tree_hash"]),
            "comparison_manifest_hash": str(report["comparison_manifest_hash"]),
            "metrics_summary_hash": str(metrics["summary_hash"]),
            "gate_report_hash": _sha256_obj(report["gate_report"]),
            "bootstrap_report_hash": _sha256_obj(report["bootstrap"]),
            "files": files,
        },
        "ledger": {
            "plan_started_count": events["counts"].get("started", 0),
            "holdout_commit_count": events["counts"].get(HOLDOUT_COMMIT_EVENT, 0),
            "terminal_count": events["counts"].get(events["terminal_event"], 0),
            "terminal_event": events["terminal_event"],
            "unique_plan_hashes": 1,
            "plan_hash": events["plan_hash"],
            "unrecognised_events": list(events["unrecognised_events"]),
            **events["event_hashes"],
        },
        "results": {
            "task_count": int(report["task_count"]),
            "measured_pairs": measured_pairs,
            "missing_pairs": int(report["missing_pairs"]),
            **counts,
            "total_model_result_count": (counts["baseline_result_count"]
                                         + counts["candidate_result_count"]),
            # Only the verdicts that OCCURRED. The receipt carries the exhaustive form;
            # a witness records what it observed.
            "verdict_counts": {k: v for k, v in
                               sorted(partitions["verdict_counts"].items()) if v},
            "numeric_delta_counts": partitions["numeric_delta_counts"],
        },
        "outcome": {
            "canonical_eligibility": decision.eligibility.value,
            "decision_hash": decision.decision_hash(),
            "human_review_required": bool(decision.human_review_required),
            "rederived_by": DECISION_REDERIVER,
            "promotes_model": False,
            "activates_model": False,
            "mutates_model_registry": False,
        },
        "receipt_v2_seal_failure_classes": list(
            seal_failure_classes or RECEIPT_V2_SEAL_FAILURE_CLASSES),
    }
    payload["witness_hash"] = _sha256_bytes(canonical_json(payload).encode("utf-8"))
    return payload


def build_receipt_v3(generation_directory: str | Path, *,
                     training_receipt: str | Path,
                     adapter_run_directory: str | Path,
                     evaluation_config: str | Path,
                     ledger: str | Path,
                     measurement_witness: str | Path,
                     repo_root: str | Path,
                     expected_candidate: str = "",
                     expected_evaluation_source_commit: str = "",
                     ledger_plan_hash: str = "",
                     milestone: str = "S3Q",
                     seal_milestone: str = "S3Q.0.2") -> dict:
    """Seal an EXISTING measurement. Nothing here runs, re-scores or re-generates.

    Every input already exists: the gitignored generation tree the run wrote, the ledger
    it appended to, the tracked training receipt that sealed the weights, the adapter on
    this host, and the pre-repair witness that says which source state measured. No model
    is loaded, no backend is constructed, no token is read and no response is opened.

    The caller assertions -- `expected_candidate` and `expected_evaluation_source_commit`
    -- are ASSERTIONS, compared against DERIVED values. A caller can state what it
    believes and be told it is wrong; it cannot state what the receipt will say. And
    `expected_evaluation_source_commit` is now checked against the WITNESS rather than
    against HEAD, which is the difference between an assertion a recovery can make
    truthfully and one it could only make before the repair existed.
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

    # ── the ledger, the plan and the report must name ONE plan ───────────────
    #
    # D-S4F-2. Under a protocol that WRAPS this one, the ledger records the outer plan
    # while the report records the inner plan it contains, so the two legitimately differ
    # and `.3`'s single-plan identity refused every V4 seal. `ledger_plan_hash` lets the
    # wrapping builder name the plan the ledger is expected to carry — and names nothing
    # else: the caller must independently prove, from the durable attempt record, that the
    # outer plan's inner plan IS the one the report published. Left empty, the check is
    # exactly the one it has always been.
    expected_ledger_plan = str(ledger_plan_hash or report["plan_hash"])
    for label, value, expected in (
            ("the ledger lines", events["plan_hash"], expected_ledger_plan),
            ("the approved plan file", str(plan["plan_hash"]), str(report["plan_hash"])),
            ("the report", str(report["plan_hash"]), str(report["plan_hash"]))):
        if value != expected:
            raise ReceiptError(
                f"{label} names plan {value[:12]} and the receipt expects "
                f"{expected[:12]}; a receipt binding two plans describes "
                f"two measurements")
    if str(commit.get("task_pack_hash")) != str(plan["task_pack_hash"]):
        raise ReceiptError(
            "the model-facing commit names a different task pack than the approved "
            "plan; the receipt would bind two different measurements")
    if str(commit.get("order_assignment_hash")) != str(plan["order_assignment_hash"]):
        raise ReceiptError(
            "the model-facing commit and the approved plan disagree about the order the "
            "arms ran in")

    # ── the candidate, DERIVED from the training receipt ─────────────────────
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

    # ── the adapter, cross-checked in every direction ────────────────────────
    for field, sealed in (("adapter_sha256", training["adapter"]["sha256"]),
                          ("adapter_manifest_hash",
                           training["adapter"]["manifest_hash"]),
                          ("adapter_artifact_set_hash",
                           training["adapter"]["artifact_set_hash"])):
        if not sealed or not re.fullmatch(r"[0-9a-f]{64}", sealed):
            raise ReceiptError(
                f"the training receipt seals no usable {field}; a receipt may not record "
                f"an empty adapter identity")
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

    # ── the config the approved plan bound, and the policies inside it ───────
    config = config_from_dict(_load(Path(evaluation_config)))
    if config.config_hash() != str(plan["evaluation_config_hash"]):
        raise ReceiptError(
            f"the supplied configuration hashes to {config.config_hash()[:12]} and the "
            f"approved plan bound {str(plan['evaluation_config_hash'])[:12]}")
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

    # ── FINDING C. Two sources, and neither is guessed from the other ────────
    # The witness must describe THIS measurement before anything is asked about which
    # commit carries it: a witness for another run should be refused for being a witness
    # for another run, not for an incidental fact about its Git history.
    w = read_measurement_witness(measurement_witness)["payload"]
    if w.get("evaluation_id") != evaluation_id or \
            int(w.get("evaluation_generation", -1)) != generation:
        raise ReceiptError(
            f"the measurement witness describes {w.get('evaluation_id')!r} generation "
            f"{w.get('evaluation_generation')!r} and this is {evaluation_id!r} "
            f"generation {generation}")
    if w.get("candidate_id") != candidate_id:
        raise ReceiptError(
            f"the measurement witness describes candidate {w.get('candidate_id')!r} and "
            f"the training receipt seals {candidate_id!r}")
    w_evidence, w_ledger = w.get("evidence", {}), w.get("ledger", {})
    for label, mine, theirs in (
            ("plan hash", str(report["plan_hash"]), str(w.get("plan", {}).get("plan_hash"))),
            ("report hash", str(report["report_hash"]),
             str(w_evidence.get("report_hash"))),
            ("evaluation manifest hash", str(manifest["manifest_hash"]),
             str(w_evidence.get("evaluation_manifest_hash"))),
            ("artifact tree hash", str(manifest["tree_hash"]),
             str(w_evidence.get("evaluation_artifact_tree_hash"))),
            ("comparison manifest hash", str(report["comparison_manifest_hash"]),
             str(w_evidence.get("comparison_manifest_hash"))),
            ("metrics summary hash", str(metrics["summary_hash"]),
             str(w_evidence.get("metrics_summary_hash"))),
            ("plan-start event hash", events["event_hashes"]["plan_started_event_hash"],
             str(w_ledger.get("plan_started_event_hash"))),
            ("holdout-commit event hash",
             events["event_hashes"]["holdout_commit_event_hash"],
             str(w_ledger.get("holdout_commit_event_hash"))),
            ("terminal event hash", events["event_hashes"]["terminal_event_hash"],
             str(w_ledger.get("terminal_event_hash")))):
        if mine != theirs:
            raise ReceiptError(
                f"the runtime {label} is {mine[:12]} and the pre-repair witness recorded "
                f"{theirs[:12]}; the witness does not describe this measurement and may "
                f"not be used to name its source")

    # Only now the Git plumbing: the topology that fixes the evaluation source, and the
    # HEAD that is honestly named the SEAL source rather than the evaluation one.
    witness = measurement_witness_evidence(measurement_witness, repo_root=root)
    evaluation_source = witness["evaluation_source"]
    if expected_evaluation_source_commit and expected_evaluation_source_commit != \
            evaluation_source["evaluation_source_commit"]:
        raise ReceiptError(
            f"the caller asserts evaluation source "
            f"{expected_evaluation_source_commit[:12]} and the pre-repair witness "
            f"records {evaluation_source['evaluation_source_commit'][:12]}")
    seal_source = seal_implementation_identity(
        root, evaluation_source_commit=evaluation_source["evaluation_source_commit"])

    # ── why the verdict follows, rederived rather than copied ────────────────
    decision = decision_from_evidence(
        gate_report=report["gate_report"], bootstrap=report["bootstrap"],
        empirical_status=report["empirical_status"], run_state=report["run_state"])
    if decision.to_dict() != report["eligibility"]:
        raise ReceiptError(
            "the canonical decision rederived from this report's own gate, bootstrap, "
            "empirical-status and serialisation-state evidence is not the decision the "
            "report recorded; one of the two is describing a different run")

    # D-S4F-4. The check above is made against the UNREDACTED report, because that is what
    # the run actually produced and agreement with it is the point. What the receipt then
    # CARRIES is the redacted form, re-derived from the redacted gate report so that every
    # copy of a blocker in the document says the same thing and a verifier re-deriving the
    # decision from `decision_evidence` reaches exactly this decision. Redaction touches
    # message text only; the counts, gates and thresholds a verdict is computed from are
    # untouched, so the verdict is the same by construction -- asserted, not assumed.
    redacted_gate_report = redact_tree(report["gate_report"])
    redacted_decision = decision_from_evidence(
        gate_report=redacted_gate_report, bootstrap=report["bootstrap"],
        empirical_status=report["empirical_status"], run_state=report["run_state"])
    if redacted_decision.eligibility is not decision.eligibility or \
            len(redacted_decision.blockers) != len(decision.blockers) or \
            bool(redacted_decision.human_review_required) != \
            bool(decision.human_review_required):
        raise ReceiptError(
            "redacting held-out task identifiers changed the decision; redaction may "
            "rewrite what a blocker SAYS and never which blockers there are")

    status_claim = _status_claim(decision.eligibility.value)
    if not status_claim:
        raise ReceiptError(
            f"eligibility {decision.eligibility.value!r} supports no evaluated candidate "
            f"state; an unrecognised verdict is an unknown one, and UNKNOWN is not a pass")

    # ── FINDING A. The production partition, READ and never recomputed ───────
    measured_pairs = int(report["measured_pairs"])
    partitions = comparison_partitions(directory, measured_pairs=measured_pairs)
    verdict_counts = partitions["verdict_counts"]
    for label, alias, verdict in (("wins", int(report["wins"]), "improved"),
                                  ("ties", int(report["ties"]), "unchanged"),
                                  ("losses", int(report["losses"]), "regressed")):
        if alias != verdict_counts[verdict]:
            raise ReceiptError(
                f"the report publishes {label}={alias} and the canonical comparisons "
                f"classify {verdict_counts[verdict]} {verdict!r} pair(s); the report and "
                f"the comparison records describe different measurements")

    # ── FINDING B. Non-ASCII decision text is EVIDENCE, and is recorded ──────
    from scripts.verify_m62_control_plane import canonical_json
    decision_text = canonical_json(
        {"gate_report": report["gate_report"], "bootstrap": report["bootstrap"],
         "canonical_decision": decision.to_dict()})
    non_ascii = sorted({f"U+{ord(ch):04X}" for ch in decision_text if ord(ch) > 127})

    counts = {name: file_evidence(directory / source_name)["record_count"]
              for source_name, name in RESULT_COUNT_FILES}
    terminal_event = events["terminal_event"]
    return {
        "schema_version": RECEIPT_V3_SCHEMA_VERSION,
        "receipt_version": RECEIPT_V3_SCHEMA_VERSION,
        "canonical_encoding": CANONICAL_RECEIPT_ENCODING,
        "evaluation_milestone": milestone,
        "seal_milestone": seal_milestone,
        "evaluation_id": evaluation_id,
        "evaluation_generation": generation,

        "evaluation_source": evaluation_source,
        "seal_implementation_source": seal_source,
        "measurement_witness": witness["bound"],

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
            # D33, unchanged and stated rather than implied.
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
            # Sealing an existing measurement spends nothing. The authority was consumed
            # by the run described here, before this receipt could exist.
            "spent_by_the_run_this_receipt_describes": True,
            "seal_consumed_no_authority": True,
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
            "sealed_from_existing_measurement": True,
            "model_loads_during_seal": 0,
            "model_generations_during_seal": 0,
        },
        "results": {
            "expected_task_count": int(plan["expected_task_count"]),
            "task_count": int(report["task_count"]),
            **counts,
            "total_model_result_count": (counts["baseline_result_count"]
                                         + counts["candidate_result_count"]),
            "measured_pairs": measured_pairs,
            "missing_pairs": int(report["missing_pairs"]),
            **partitions,
            "wins": int(report["wins"]), "ties": int(report["ties"]),
            "losses": int(report["losses"]),
            "wins_ties_losses_are_a_partial_partition": True,
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
            "gate_report": redacted_gate_report,
            "bootstrap": report["bootstrap"],
            "canonical_decision": redacted_decision.to_dict(),
            "decision_hash": redacted_decision.decision_hash(),
            "rederived_by": DECISION_REDERIVER,
            "carries_non_ascii_decision_text": bool(non_ascii),
            "non_ascii_codepoints": non_ascii,
        },
        "outcome": {
            "eligibility": decision.eligibility.value,
            "human_review_required": bool(decision.human_review_required),
            "promotes_model": False,
            "activates_model": False,
            "mutates_model_registry": False,
            "gate_blockers": [redact_held_out_task_ids(b) for b in decision.blockers],
            "gate_warnings": [redact_held_out_task_ids(w) for w in decision.warnings],
            "limitations": [redact_held_out_task_ids(limit)
                            for limit in report.get("limitations", [])],
            "security_blocking_count": int(
                report["gate_report"].get("security_blocking_count", 0)),
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


def verify_receipt_v3(payload: dict) -> tuple[str, ...]:
    """Every reason this seal-recovery receipt is not evidence. Reads nothing but the payload.

    PORTABLE BY CONSTRUCTION. Runs in a clean clone with no runtime evaluation directory,
    no adapter bytes, no model cache and no eval-v4: the generation tree is gitignored and
    long gone by the time anyone audits this.

    Everything `.2` checked is checked here, plus the three repairs:

      * the FOUR-way partition sums to `measured_pairs`, and `wins/ties/losses` are held
        to being the aliases they are rather than summed against the total;
      * the numeric-delta partition is verified SEPARATELY and never compared
        bucket-for-bucket against the verdicts;
      * the evaluation source and the seal implementation source are two fields, and the
        receipt must state which of them it derived from what.

    The bindings that reach OUTSIDE the payload -- the tracked witness file, the Git
    parent topology, the production verdict vocabulary -- belong to the control plane's
    `_check_seal_recovery_receipt`, for the same reason `.2` put its external checks
    there: a standalone verifier that needs a repository is not standalone.
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
    if payload.get("canonical_encoding") != CANONICAL_RECEIPT_ENCODING:
        problems.append(
            f"the receipt states canonical encoding "
            f"{payload.get('canonical_encoding')!r}; m62.eval_receipt.3 exists partly to "
            f"leave no encoding choice open, and a receipt that does not name its own "
            f"encoding has left one")

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
    source = payload.get("evaluation_source", {})
    seal = payload.get("seal_implementation_source", {})
    witness = payload.get("measurement_witness", {})

    # ── FINDING C. Two sources, and neither may stand in for the other ───────
    if source.get("derived_from") != "measurement_witness":
        problems.append(
            "the receipt does not state that its evaluation source came from the "
            "pre-repair measurement witness; deriving it from a repair-time HEAD is the "
            "defect this version exists to fix")
    for field in ("evaluation_source_commit", "evaluation_source_tree_oid"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(source.get(field, ""))):
            problems.append(f"evaluation_source.{field} is not a commit identity")
    if not re.fullmatch(r"[0-9a-f]{64}",
                        str(source.get("evaluation_source_digest", ""))):
        problems.append("the receipt binds no usable evaluation source digest")
    for field in ("seal_implementation_source_commit", "seal_implementation_tree_oid"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(seal.get(field, ""))):
            problems.append(f"seal_implementation_source.{field} is not a commit identity")
    if not seal.get("worktree_clean_at_build"):
        problems.append(
            "the receipt was built from an unclean worktree, so it names a seal "
            "implementation source nobody else can obtain")
    differs = str(seal.get("seal_implementation_source_commit", "")) != \
        str(source.get("evaluation_source_commit", ""))
    if bool(seal.get("differs_from_evaluation_source")) != differs:
        problems.append(
            "the receipt's claim about whether the two sources differ is not what its "
            "own two commit fields say")
    for field in ("measurement_witness_sha256", "measurement_witness_hash"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(witness.get(field, ""))):
            problems.append(f"measurement_witness.{field} binds no witness")
    if not re.fullmatch(r"[0-9a-f]{40}",
                        str(witness.get("measurement_witness_commit", ""))):
        problems.append("the receipt binds no measurement witness commit")
    if not witness.get("witness_first_parent_is_evaluation_source"):
        problems.append(
            "the receipt does not claim the witness commit's first parent is the "
            "evaluation source; that topology is the only thing fixing the evaluation "
            "source across the repair")
    if not witness.get("grants_no_authority"):
        problems.append("the receipt binds a measurement witness that claims authority")

    # ── direct adapter identity is MANDATORY and non-empty ───────────────────
    for field in ("adapter_sha256", "adapter_manifest_hash",
                  "adapter_artifact_set_hash", "adapter_reference_hash"):
        value = str(candidate.get(field, ""))
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            problems.append(
                f"candidate.{field} is {value[:16]!r}; a modern receipt records which "
                f"weights were measured or it is not evidence about an adapter")

    # ── the training receipt is the candidate's identity root ────────────────
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

    # ── the authority, described as the ledger can witness it ────────────────
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
    if not authority.get("seal_consumed_no_authority") or \
            not authority.get("spent_by_the_run_this_receipt_describes"):
        problems.append(
            "the receipt does not state that sealing consumed no authority; a recovery "
            "that cannot say so reads as a second consumption of a single-use plan")
    if not payload.get("execution", {}).get("sealed_from_existing_measurement"):
        problems.append(
            "the receipt does not state that it sealed an EXISTING measurement")
    for field in ("model_loads_during_seal", "model_generations_during_seal"):
        if payload.get("execution", {}).get(field) != 0:
            problems.append(
                f"execution.{field} is "
                f"{payload.get('execution', {}).get(field)!r}; sealing an existing "
                f"measurement loads no model and generates nothing")

    if not plan.get("binds_exact_pack_identity"):
        problems.append(
            "the approved plan did not bind the exact pack identity that was measured")

    # ── direct policy values, RE-DERIVED rather than quoted ──────────────────
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

    # ── the counts a clean clone cannot recompute from absent files ──────────
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

    # ── FINDING A. The FOUR-way partition, and the aliases held to their job ─
    problems.extend(_verdict_partition_problems(results))

    if ledger.get("terminal_is_successful"):
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

    # ── FINDING B. The Unicode claim must match the Unicode carried ──────────
    from scripts.verify_m62_control_plane import canonical_json
    decision_text = canonical_json({
        "gate_report": decision_evidence.get("gate_report"),
        "bootstrap": decision_evidence.get("bootstrap"),
        "canonical_decision": decision_evidence.get("canonical_decision")})
    observed = sorted({f"U+{ord(ch):04X}" for ch in decision_text if ord(ch) > 127})
    if list(decision_evidence.get("non_ascii_codepoints", [])) != observed:
        problems.append(
            f"the receipt records non-ASCII decision codepoints "
            f"{decision_evidence.get('non_ascii_codepoints')} and its own decision "
            f"evidence carries {observed}; the Unicode inventory is evidence about the "
            f"evidence and may not drift from it")
    if bool(decision_evidence.get("carries_non_ascii_decision_text")) != bool(observed):
        problems.append(
            "the receipt's claim about whether it carries non-ASCII decision text is not "
            "what its decision evidence contains")

    # ── the decision, REDERIVED by the production algorithm ──────────────────
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
    # D-S4F-4. Compared against the REDACTED decision text, because that is what a tracked
    # receipt may carry. Redaction is a no-op on every blocker that names no held-out task,
    # so this is the same check it has always been for every receipt sealed before S4F —
    # and it still refuses a receipt whose blockers are not the ones its evidence produced,
    # which dropping the comparison would not.
    if list(outcome.get("gate_blockers", [])) != \
            [redact_held_out_task_ids(b) for b in decision.blockers]:
        problems.append("the receipt's blockers are not the ones its evidence produces")
    if list(outcome.get("gate_warnings", [])) != \
            [redact_held_out_task_ids(w) for w in decision.warnings]:
        problems.append("the receipt's warnings are not the ones its evidence produces")
    gate_report = decision_evidence.get("gate_report") or {}
    if outcome.get("security_blocking_count") != \
            gate_report.get("security_blocking_count"):
        problems.append(
            f"the receipt records {outcome.get('security_blocking_count')!r} security "
            f"blocker(s) and its gate evidence reports "
            f"{gate_report.get('security_blocking_count')!r}")
    for flag in ("promotes_model", "activates_model", "mutates_model_registry"):
        if outcome.get(flag):
            problems.append(f"the receipt claims {flag}; no mechanism in this repository "
                            f"could have performed it")
    return tuple(problems)


def _verdict_partition_problems(results: dict) -> list[str]:
    """FINDING A, in one place so the rule has exactly one implementation.

    Three separate obligations, deliberately not collapsed into one:

      1. the CANONICAL verdict counts are exhaustive over the production vocabulary and
         sum to `measured_pairs`;
      2. the NUMERIC delta counts sum to `measured_pairs` too -- separately, and never
         bucket-for-bucket against the verdicts, because the verdict is not a function of
         the delta;
      3. `wins/ties/losses` equal the three verdicts they alias. They are NOT summed
         against the total: doing so is what made `.2` refuse a correct measurement.
    """
    problems: list[str] = []
    measured = results.get("measured_pairs")
    counts = results.get("verdict_counts")
    if not isinstance(counts, dict) or not counts:
        return ["the receipt carries no canonical verdict partition"]
    try:
        vocabulary = production_verdicts()
    except Exception as exc:  # noqa: BLE001 — an unverifiable vocabulary is a refusal
        return [f"the production verdict vocabulary is not importable ({exc}); the "
                f"receipt's partition is therefore UNVERIFIED"]
    if tuple(sorted(counts)) != vocabulary:
        problems.append(
            f"the receipt partitions its pairs over {sorted(counts)} and production "
            f"classifies {list(vocabulary)}; a partition missing a verdict is not "
            f"exhaustive, and one carrying an unknown verdict is not production's")
    if list(results.get("verdict_vocabulary", [])) != list(vocabulary):
        problems.append(
            f"the receipt records verdict vocabulary "
            f"{results.get('verdict_vocabulary')} and production defines "
            f"{list(vocabulary)}")
    for verdict, value in sorted(counts.items()):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"verdict_counts.{verdict} is {value!r}, not a count")
    total = sum(v for v in counts.values() if isinstance(v, int)
                and not isinstance(v, bool))
    if total != measured:
        problems.append(
            f"the canonical verdict counts sum to {total} and the run measured "
            f"{measured!r} pair(s); every measured pair carries exactly one verdict")

    numeric = results.get("numeric_delta_counts")
    if not isinstance(numeric, dict) or sorted(numeric) != ["negative", "positive",
                                                            "zero"]:
        problems.append(
            f"the numeric delta partition is {numeric!r}; it is a separate partition of "
            f"the same pairs and has its own three buckets")
    else:
        for name, value in sorted(numeric.items()):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(
                    f"numeric_delta_counts.{name} is {value!r}, not a count")
        numeric_total = sum(v for v in numeric.values() if isinstance(v, int)
                            and not isinstance(v, bool))
        if numeric_total != measured:
            problems.append(
                f"the numeric delta counts sum to {numeric_total} and the run measured "
                f"{measured!r} pair(s)")

    if not results.get("wins_ties_losses_are_a_partial_partition"):
        problems.append(
            "the receipt does not state that wins/ties/losses are a PARTIAL partition. "
            "Treating them as exhaustive is exactly the defect m62.eval_receipt.3 fixes")
    for alias, verdict in (("wins", "improved"), ("ties", "unchanged"),
                           ("losses", "regressed")):
        if verdict in counts and results.get(alias) != counts[verdict]:
            problems.append(
                f"results.{alias} is {results.get(alias)!r} and the canonical "
                f"{verdict!r} count is {counts[verdict]!r}; the alias and the verdict it "
                f"names are the same number or one of them is wrong")
    return problems


def _receipt_verifiers() -> dict:
    """Every receipt version this repository can read, and what reads it.

    Built on demand rather than at import time so the module stays importable without the
    control plane loaded. A version outside this map is REFUSED: an unknown contract is
    not a satisfied one.
    """
    from scripts.verify_m62_control_plane import (
        eval_receipt_schema,
        eval_receipt_v2_schema,
        eval_receipt_v3_schema,
    )
    return {
        RECEIPT_SCHEMA_VERSION: (eval_receipt_schema, verify_receipt),
        RECEIPT_V2_SCHEMA_VERSION: (eval_receipt_v2_schema, verify_receipt_v2),
        RECEIPT_V3_SCHEMA_VERSION: (eval_receipt_v3_schema, verify_receipt_v3),
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
        UTF8_CANONICAL_RECEIPT_VERSIONS,
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
    # S3Q.0.2 -- FINDING B. `.1` and `.2` refuse non-ASCII, and that stays: it is the
    # contract their existing documents were written and hashed under, and relaxing it in
    # place would change what they mean. `.3` DEFINES its canonical bytes instead, which
    # closes the same ambiguity without discarding a legitimately typeset U+2212 out of a
    # production gate message. The scanners above are untouched either way -- Unicode is
    # permitted, never privileged, and a token or a private path is still a refusal in
    # any script.
    if version in UTF8_CANONICAL_RECEIPT_VERSIONS:
        try:
            canonical_json(payload).encode("utf-8").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            problems.append(
                f"the receipt does not round-trip through its own declared canonical "
                f"encoding ({exc}); UTF-8 is the contract and a document that cannot be "
                f"expressed in it has no canonical bytes")
    else:
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            problems.append("the receipt is not ASCII, so its canonical bytes depend on "
                            "an encoding choice")
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
                       help="ASSERTION: refuse unless the EVALUATION source is this "
                            "commit. Under --receipt-version 3 that is the commit the "
                            "measurement witness records, NOT the repository HEAD")
    build.add_argument("--measurement-witness", default="",
                       help="the tracked pre-repair measurement witness. REQUIRED by "
                            "--receipt-version 3: it is where the evaluation source "
                            "comes from once a repair commit has moved HEAD")
    build.add_argument("--receipt-version", default="2", choices=("2", "3"),
                       help="2 = m62.eval_receipt.2; 3 = m62.eval_receipt.3, the "
                            "post-live seal-recovery contract")
    build.add_argument("--seal-milestone", default="S3Q.0.2",
                       help="the milestone that BUILT the receipt (version 3 only)")
    build.add_argument("--milestone", default="S3Q", help="the evaluation milestone")
    build.add_argument("--emit", default="",
                       help="write the receipt here atomically; prints to stdout when "
                            "omitted")

    witness = modes.add_parser(
        "witness",
        help="write the pre-repair measurement witness for one existing generation")
    witness.add_argument("--generation-directory", required=True,
                         help="the completed generation directory (gitignored runtime)")
    witness.add_argument("--training-receipt", required=True,
                         help="the tracked S3P training receipt for this candidate")
    witness.add_argument("--ledger", default="",
                         help="the evaluation ledger; defaults to the run tree's own")
    witness.add_argument("--repo-root", default=str(_ROOT.parent),
                         help="the repository the evaluation executed from. Its HEAD "
                              "must STILL be the evaluation source and its worktree "
                              "clean, or there is nothing truthful to witness")
    witness.add_argument("--milestone", default="S3Q.0.2",
                         help="the milestone writing the witness")
    witness.add_argument("--emit", default="",
                         help="write the witness here atomically; prints to stdout when "
                              "omitted")

    verify = modes.add_parser(
        "verify", help="strictly verify an existing receipt. Read-only, standalone")
    verify.add_argument("receipt", help="the receipt to verify")

    args = parser.parse_args(argv)

    if args.mode == "witness":
        directory = Path(args.generation_directory)
        ledger = Path(args.ledger) if args.ledger else \
            directory.parent.parent.parent / "evaluation_runs.jsonl"
        try:
            payload = build_measurement_witness(
                directory, ledger=ledger, training_receipt=args.training_receipt,
                evaluation_source=witness_source_identity(args.repo_root),
                repo_root=args.repo_root, milestone=args.milestone)
            text = canonical_json(payload)
            if args.emit:
                target = Path(args.emit)
                if target.is_symlink() or target.exists():
                    raise ReceiptError(
                        f"{target.name}: already exists or is a link; a witness is "
                        f"written once, before the repair that needs it")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
            print(json.dumps({"status": "refused",
                              "error": f"{type(exc).__name__}: {exc}"}, indent=2))
            return 1
        if args.emit:
            print(json.dumps({"status": "ok", "witness": Path(args.emit).name,
                              "witness_hash": payload["witness_hash"],
                              "sha256": _sha256_bytes(text.encode("utf-8"))}, indent=2))
        else:
            print(text, end="")
        return 0

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
        if args.receipt_version == "3":
            if not args.measurement_witness:
                raise ReceiptError(
                    "--measurement-witness is required for m62.eval_receipt.3. The "
                    "whole point of the version is that the evaluation source comes "
                    "from a witness written before the repair, not from HEAD")
            built = build_receipt_v3(
                directory, training_receipt=args.training_receipt,
                adapter_run_directory=args.adapter_run_directory,
                evaluation_config=args.evaluation_config, ledger=ledger,
                measurement_witness=args.measurement_witness,
                repo_root=args.repo_root,
                expected_candidate=args.expected_candidate,
                expected_evaluation_source_commit=(
                    args.expected_evaluation_source_commit),
                milestone=args.milestone, seal_milestone=args.seal_milestone)
        else:
            if args.measurement_witness:
                raise ReceiptError(
                    "m62.eval_receipt.2 has no measurement witness to bind; passing one "
                    "would record a binding the contract does not carry")
            built = build_receipt_v2(
                directory, training_receipt=args.training_receipt,
                adapter_run_directory=args.adapter_run_directory,
                evaluation_config=args.evaluation_config, ledger=ledger,
                repo_root=args.repo_root,
                expected_candidate=args.expected_candidate,
                expected_evaluation_source_commit=(
                    args.expected_evaluation_source_commit),
                milestone=args.milestone)
        payload = seal(built)
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
