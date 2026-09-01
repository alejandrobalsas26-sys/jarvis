#!/usr/bin/env python3
"""scripts/project_m62_s4e_capacity.py — V69 M62 S4E: can the result be recorded?

WHY THIS RUNS BEFORE THE HOLDOUT IS SPENT
------------------------------------------
Discovering AFTER eval-v7 is spent that the control plane cannot record what was measured
would be the one outcome this evaluation may not produce. The holdout is single-use, a
rerun to "fix the recording" is forbidden by the same rules that make it single-use, and
raising a reviewed budget is barred by generation 22's own ``ruled_out``. So capacity is
proved first, with zero weight loads and no EVAL authority in existence.

Same method as ``project_m62_gen14_capacity.py``, which S3Y used for the same reason:
a PURE TRANSFORM of the real current snapshot, measured through the same
``canonical_bytes`` the verifier and the emitter use, so the bytes counted here are the
bytes that would land on disk.

RESULT NEUTRALITY IS THE POINT
-------------------------------
Capacity is proved for the LARGEST truthful ending, not the most convenient one, and the
projection may not assume candidate 005 wins. A projection that only fits when the result
is good is a projection that pressures the result.

THE TERMINAL STATES, ALL OF THEM LEGITIMATE
--------------------------------------------
``ELIGIBLE``            every gate passes. Candidate 005 becomes
                        EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW — a request for a human
                        decision, never a promotion.
``NOT_ELIGIBLE``        one or more gates fail. EVALUATED_NOT_ELIGIBLE.
``INCONCLUSIVE``        the run completes and the statistics exclude nothing. The
                        candidate is measured and the axis stays OPEN, which needs the
                        most prose of the three because it must say what was and was not
                        established.
``ABORTED``             model-facing access began and stopped. eval-v7 is STILL SPENT —
                        the boundary is the durable commit, deliberately earlier than
                        proof a forward pass finished — but no receipt exists, so
                        candidate 005 may NOT claim an EVALUATED_* state. It stays
                        TRAINED_UNEVALUATED beside a spent holdout: an ugly state, and a
                        true one.
``DURABILITY_FAILURE``  a valid measurement whose evidence did not land. Not a clean
                        success; the answer is recovery, and the state says so.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.verify_m62_control_plane import (  # noqa: E402
    CURRENT_PATH,
    PROGRESS_MAX_BYTES,
    PROGRESS_MAX_LINES,
    PROGRESS_PATH,
    SNAPSHOT_MAX_BYTES,
    canonical_bytes,
)

#: The policy floor. A generation that fits with less than this is one nobody may write.
REQUIRED_HEADROOM_BYTES = 1_024

#: The label and note each terminal state would carry. Written out at realistic length —
#: a projection against optimistically short prose proves nothing.
_NOTES = {
    "ELIGIBLE": (
        "S4E spent eval-v7 ONCE on the first reference-adapter paired evaluation: "
        "candidate 004 as REFERENCE against candidate 005 as CANDIDATE, both adapters on "
        "the same pinned base, 36 tasks, 72 generations, one attempt. Every gate passed "
        "and candidate 005 is EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW, which requests a human "
        "decision and is not a promotion. Candidate 004 keeps its historical HOLD and its "
        "eval-v6 provenance; production model assignment is unchanged."),
    "NOT_ELIGIBLE": (
        "S4E spent eval-v7 ONCE on the first reference-adapter paired evaluation: "
        "candidate 004 as REFERENCE against candidate 005 as CANDIDATE, both adapters on "
        "the same pinned base, 36 tasks, 72 generations, one attempt. One or more "
        "preregistered gates failed and candidate 005 is EVALUATED_NOT_ELIGIBLE. The "
        "result stands; no gate moved and no retry is authorised. Candidate 004 keeps its "
        "HOLD; production model assignment is unchanged."),
    "INCONCLUSIVE": (
        "S4E spent eval-v7 ONCE on the first reference-adapter paired evaluation: "
        "candidate 004 as REFERENCE against candidate 005 as CANDIDATE, both adapters on "
        "the same pinned base, 36 tasks, 72 generations, one attempt. The run completed "
        "and the paired interval excludes neither improvement nor regression, so the "
        "learning-rate axis stays OPEN and UNRESOLVED with the holdout spent. That is a "
        "measured result, not a failure, and it authorises no second attempt."),
    "ABORTED": (
        "S4E consumed one EVAL authority and began model-facing access on eval-v7, which "
        "is therefore USED_IMMUTABLE from the durable commit onward whatever followed. "
        "The attempt did not reach a sealed receipt, so candidate 005 stays "
        "TRAINED_UNEVALUATED beside a spent holdout. No rerun is authorised; only an "
        "explicit operator recovery ruling remains. Candidate 004 is unchanged."),
    "DURABILITY_FAILURE": (
        "S4E measured eval-v7 once and its durable evidence did not land completely. This "
        "is NOT a clean success: the holdout is spent, the measurement exists and the "
        "record of it is incomplete, so candidate 005 may claim no EVALUATED_* state "
        "until an operator rules on recovery. No rerun is authorised. Candidate 004 is "
        "unchanged and production model assignment is untouched."),
}

#: Prose the next milestone must carry in each ending. The INCONCLUSIVE and ABORTED
#: endings are the long ones, which is exactly why the projection may not assume a win.
_NEXT = {
    "ELIGIBLE": (
        "Candidate 005 is EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW against candidate 004 on "
        "eval-v7 under Protocol V4. Eligibility is a request for a human decision and "
        "implies no promotion: promotion needs a separate explicit human authority that "
        "no result creates. eval-v7 is USED_IMMUTABLE and may never decide again; a "
        "further candidate needs a fresh holdout and a fresh EVAL authority."),
    "NOT_ELIGIBLE": (
        "Candidate 005 is EVALUATED_NOT_ELIGIBLE against candidate 004 on eval-v7 under "
        "Protocol V4. The learning-rate axis is MEASURED and answered in the negative for "
        "this value. No gate may be relaxed now that the result is known, which is "
        "post-hoc weakening. eval-v7 is USED_IMMUTABLE; a further candidate needs a fresh "
        "holdout, a fresh design ruling and a fresh EVAL authority."),
    "INCONCLUSIVE": (
        "Candidate 005 is measured against candidate 004 on eval-v7 and the result is "
        "INCONCLUSIVE: the preregistered paired interval excludes neither improvement nor "
        "regression at the frozen 36-pair sample size. The learning-rate axis stays OPEN "
        "and the holdout is spent, so it cannot be reopened on this corpus. A larger "
        "sample, a second seed and a re-measurement are all separate operator decisions "
        "that no result implies, and none is authorised here. eval-v7 is USED_IMMUTABLE."),
    "ABORTED": (
        "eval-v7 is USED_IMMUTABLE and candidate 005 is still TRAINED_UNEVALUATED. The "
        "corpus was committed to a model and no sealed receipt exists, which is the one "
        "state the four-event separation was built to make visible rather than hide. "
        "Recovery is an explicit operator ruling; a fresh attempt on the same corpus is "
        "forbidden, and a fresh corpus needs its own authoring session and authority."),
    "DURABILITY_FAILURE": (
        "eval-v7 is USED_IMMUTABLE, a measurement exists and its durable evidence is "
        "incomplete. Candidate 005 claims no EVALUATED_* state until an operator rules. "
        "The measurement is not re-taken to repair the record: re-measuring spent "
        "material is the thing single-use exists to prevent, and the honest options are "
        "recovering the evidence or recording the loss."),
}


def project(snapshot: dict, outcome: str, *, receipt_path: str) -> dict:
    """A PURE transform of the real snapshot into one terminal generation."""
    projected = json.loads(json.dumps(snapshot))
    projected["state_generation"] = int(snapshot["state_generation"]) + 1
    projected["parent_snapshot_sha256"] = "0" * 64
    projected["subject_state_commit"] = "0" * 40
    projected["subject_state_milestone"] = "S4E"
    projected["generation_label"] = f"M62_S4E_REFERENCE_PAIR_{outcome}"
    projected["control_plane_note"] = _NOTES[outcome][:320]
    projected["project"] = dict(projected.get("project", {}))
    projected["project"]["milestone"] = (
        "V69 M62 S4E - Reference-Adapter Paired Evaluation of Candidate 005")

    milestone = dict(projected.get("next_milestone", {}))
    milestone["name"] = _NEXT[outcome]
    milestone["evaluation_holdout"] = (
        "eval-v7: USED_IMMUTABLE, spent_by the S4E Protocol V4 paired attempt "
        "(reference candidate 004, candidate candidate 005), 36 tasks, 72 generations, "
        "ONE spend. eval-v4 and eval-v6 remain USED_IMMUTABLE; eval-v5 remains "
        "FROZEN_UNUSED and RETIRED. No holdout remains that may decide eligibility, and "
        "no authority exists to create or spend one.")
    milestone["primary_axis"] = (
        "MEASURED. Candidate 005's learning_rate 5e-5 -> 2.5e-5 was compared against "
        "candidate 004 head to head on one fresh corpus under one runtime, which is the "
        "first true single-axis comparison in this lineage.")
    # The result-specific ruled_out clauses every ending must add.
    ruled_out = list(milestone.get("ruled_out", ()))
    ruled_out.insert(0, (
        "any second measurement of eval-v7: a rerun, a re-score, an alternate seed, a "
        "partial re-measurement, a second paired attempt, or reading, quoting or "
        "reconstructing one of its bodies. It is USED_IMMUTABLE and single-use, and a "
        "result nobody likes is not permission to look again"))
    ruled_out.insert(1, (
        "promoting candidate 005, activating it, mutating the model registry, merging, "
        "tagging, releasing or bumping a version on the strength of this result. "
        "Promotion needs a separate explicit human authority that no result implies"))
    ruled_out.insert(2, (
        "relaxing any preregistered gate, grader, threshold or rubric now that the "
        "result is known, which is post-hoc weakening whichever direction it moves"))
    milestone["ruled_out"] = ruled_out
    milestone["authority_required"] = [
        "a separate explicit human decision before any promotion, which no result implies",
        "a fresh single-use human TRAIN authority, form TRAIN:<plan-hash>, before any "
        "further training; the S4C one is consumed and is never replayed",
        "a fresh single-use human EVAL authority for any future evaluation, against a "
        "holdout that does not yet exist; the S4E one is consumed and is never replayed",
    ]
    projected["next_milestone"] = milestone

    # Records: the same eight pointers. Their CONTENT changes (candidate 005's status,
    # eval-v7's spend, new limitations) but they are content-addressed, so the snapshot
    # carries a 64-character digest either way and its size does not move with them.
    records = dict(projected.get("records", {}))
    for block in records:
        records[block] = "0" * 64
    projected["records"] = records
    projected["evaluation_receipt_path_projected"] = receipt_path
    projected.pop("evaluation_receipt_path_projected")
    return projected


def measure(snapshot_path: Path) -> dict:
    snapshot = json.loads(Path(snapshot_path).read_bytes())
    rows = {}
    for outcome in _NOTES:
        projected = project(snapshot, outcome,
                            receipt_path="state/m62/receipts/"
                                          "qwen3-06b-lora-quality-live-005.eval.json")
        size = len(canonical_bytes(projected))
        rows[outcome] = {
            "bytes": size,
            "headroom": SNAPSHOT_MAX_BYTES - size,
            "fits": size <= SNAPSHOT_MAX_BYTES - REQUIRED_HEADROOM_BYTES,
        }
    worst = max(rows.values(), key=lambda r: r["bytes"])
    progress = REPO_ROOT / PROGRESS_PATH
    text = progress.read_text(encoding="utf-8")
    return {
        "snapshot_budget_bytes": SNAPSHOT_MAX_BYTES,
        "required_headroom_bytes": REQUIRED_HEADROOM_BYTES,
        "current_snapshot_bytes": len(Path(snapshot_path).read_bytes()),
        "projected": rows,
        "worst_case_bytes": worst["bytes"],
        "worst_case_headroom": worst["headroom"],
        "control_plane_posteval_capacity": "PASS" if all(
            r["fits"] for r in rows.values()) else "FAIL",
        "result_neutral": len({r["fits"] for r in rows.values()}) == 1,
        "progress_md_bytes": progress.stat().st_size,
        "progress_md_lines": text.count("\n"),
        "progress_md_budget_bytes": PROGRESS_MAX_BYTES,
        "progress_md_budget_lines": PROGRESS_MAX_LINES,
        "progress_md_headroom_bytes": PROGRESS_MAX_BYTES - progress.stat().st_size,
        "progress_md_headroom_lines": PROGRESS_MAX_LINES - text.count("\n"),
        "current_json_bytes": (REPO_ROOT / CURRENT_PATH).stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project the post-evaluation control plane. Loads no model.")
    parser.add_argument("--snapshot", default="")
    args = parser.parse_args(argv)
    snapshot = Path(args.snapshot) if args.snapshot else (
        REPO_ROOT / json.loads((REPO_ROOT / CURRENT_PATH).read_bytes())
        ["latest_snapshot_path"])
    report = measure(snapshot)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["control_plane_posteval_capacity"] == "PASS" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
