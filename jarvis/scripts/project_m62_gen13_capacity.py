"""scripts/project_m62_gen13_capacity.py — V69 M62 S3X.1: does generation 13 still fit?

WHY THIS EXISTS
---------------
Generation 12 closed at 33 739 bytes against a 34 816-byte budget: **1 077 bytes of
headroom**, and the policy floor is 1 024. Generation 13 has to carry strictly MORE truth
than that one — a sixth held-out dataset entry that did not exist before — and a dataset
entry costs roughly 490 bytes on its own. Discovering that after authoring eval-v6 would
leave the repository holding a frozen corpus it cannot record, which is the one outcome a
freeze may not produce.

So capacity is proved BEFORE a single byte of eval-v6 is written, and the projection is
not a guess: :func:`project_gen13` is the same transform ``--emit`` writes, so the bytes
measured here are the bytes that land on disk.

WHAT MAKES THE PROJECTION FIT
-----------------------------
Not a budget raise. ``SNAPSHOT_MAX_BYTES`` was already raised once, at generation 11, and
raising it again to absorb every generation's growth would turn a bounded control plane
into an unbounded one. It fits because of **lossless compaction of prose that generation
13 genuinely supersedes or duplicates**, and nothing else:

* ``next_milestone`` is PROSPECTIVE by construction — it is rewritten at every generation
  and describes the milestone that has not happened yet. Generation 12's described S3X.1,
  which is now DONE. Its replacement is written tight, because the permanent rules it used
  to restate at length live on the ``frozen_invariants`` surface, which is where permanent
  rules belong. ``_check_ruled_out`` enforces the coverage as substrings precisely so that
  the wording stays free while the coverage does not.
* Four limitation entries are MERGED with the entry that already carried their subject:
  the holdout root-independence pair, the two environmental optional-dependency facts, the
  D33 corollary and its own defect, and the two holdout-authoring firewalls.

Every clause that must survive that compaction is listed in :data:`CARRIED_FORWARD` and
checked HERE, fail-closed, before the projection is even measured — a compaction that
drops a standing prohibition is refused rather than reported.

NOTHING HERE WRITES STATE UNLESS ``--emit`` IS PASSED, and nothing here ever reads a
held-out task body, loads a model, touches an adapter or names a candidate-004 result.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

from scripts.verify_m62_control_plane import (  # noqa: E402
    PROGRESS_MAX_BYTES,
    PROGRESS_MAX_LINES,
    SNAPSHOT_MAX_BYTES,
    canonical_bytes,
    sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The headroom the projected generation must clear. Not the schema budget: a snapshot
#: that fits with nothing to spare is one truthful sentence away from not fitting.
REQUIRED_HEADROOM_BYTES = 1024

EXPECTED_PARENT_SHA256 = (
    "6d04f5ee05f60c497e9396d861e7074deb5d02790219ff9bbba8cd85be81dc54")
EXPECTED_PARENT_GENERATION = 12

GEN13_LABEL = "M62_S3X1_FRESH_EVAL_V6_FROZEN"
GEN13_MILESTONE = "S3X.1"
GEN13_EVIDENCE = "jarvis/docs/V69_M62_S3X1_EVAL_V6_FREEZE.md"

#: eval-v5's frozen manifest digest — eval-v6's DECLARED parent under D34.
#:
#: That eval-v5 is retired from ELIGIBILITY use does not change its ANCESTRY role, for
#: exactly the reason eval-v5 itself declared the spent eval-v4 as its parent: a parent is
#: an ancestry statement, not a reusable exam. Declaring it neither rehabilitates it nor
#: reopens it, and the retirement checks are unaffected.
EVAL_V5_MANIFEST = (
    "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c")

#: Clauses that MUST still appear somewhere in the projected snapshot.
#:
#: The failure mode of a compaction is never that the rewrite is wrong — it is that a
#: rewrite aimed at one clause quietly drops four others nobody was thinking about. Each
#: entry is ``(substring, surface)``; the substring is searched in the joined text of that
#: surface, so wording stays free and coverage does not. This is the machine-checked half
#: of "lossless": :mod:`verify_m62_control_plane` independently re-checks the standing
#: ``ruled_out`` subjects and the retirement markers.
CARRIED_FORWARD: tuple[tuple[str, str], ...] = (
    # The eval-v5 retirement, in all six places the firewall carries it.
    ("ELIGIBILITY_USE: RETIRED", "frozen_invariants"),
    ("FRESH_V6_REQUIRED", "frozen_invariants"),
    ("NO MODEL EVER SAW IT", "frozen_invariants"),
    ("eval-v5", "next_milestone.ruled_out"),
    ("USED_IMMUTABLE", "next_milestone.ruled_out"),
    # Body-blindness, the gate that failed once and may not be relaxed.
    ("ORCHESTRATOR BODY-BLINDNESS IS A GATE", "frozen_invariants"),
    ("repr of a bound method", "frozen_invariants"),
    # The standing training prohibitions.
    ("epoch", "next_milestone.ruled_out"),
    ("rank", "next_milestone.ruled_out"),
    ("alpha", "next_milestone.ruled_out"),
    ("dropout", "next_milestone.ruled_out"),
    ("ATTENTION_ONLY", "next_milestone.ruled_out"),
    ("train-v3", "next_milestone.ruled_out"),
    ("eval-v4", "next_milestone.ruled_out"),
    ("max_new_tokens", "next_milestone.ruled_out"),
    ("grader", "next_milestone.ruled_out"),
    ("threshold", "next_milestone.ruled_out"),
    ("refusal detector", "next_milestone.ruled_out"),
    ("candidate 003", "next_milestone.ruled_out"),
    ("promotion", "next_milestone.ruled_out"),
    ("D39", "next_milestone.ruled_out"),
    ("5e-5", "next_milestone.ruled_out"),
    # The budget rule itself, which a capacity milestone is the likeliest to erode.
    ("raising a reviewed budget", "next_milestone.ruled_out"),
    # The merged limitations. Every subject that entered a merge must leave it.
    ("promotion_plan_hash", "limitations"),
    ("openai", "limitations"),
    ("transformers", "limitations"),
    ("timeout_enforced false", "limitations"),
    ("authored eval-v5", "limitations"),
    ("authored eval-v6", "limitations"),
    ("D44 exposure is PERMANENT", "limitations"),
)


def _surface_text(payload: dict, surface: str) -> str:
    node: object = payload
    for part in surface.split("."):
        node = node[part]  # type: ignore[index]
    if isinstance(node, list):
        return " | ".join(str(x) for x in node)
    return str(node)


def check_carried_forward(payload: dict) -> list[str]:
    """Clauses that did NOT survive the compaction. Empty means lossless."""
    return [f"{substring!r} no longer appears in {surface}"
            for substring, surface in CARRIED_FORWARD
            if substring not in _surface_text(payload, surface)]


def _replace(entries: list[str], index: int, starts_with: str, text: str) -> None:
    """Rewrite one entry in place, refusing if it is not the entry expected.

    Index-addressed edits against a parent snapshot are only safe while the parent is the
    one they were written for. The prefix assertion is what makes that a refusal rather
    than a silent rewrite of whatever happened to be at that position.
    """
    if not entries[index].startswith(starts_with):
        raise RuntimeError(
            f"entry {index} does not start with {starts_with!r}; the parent snapshot is "
            f"not the one this projection was written against")
    entries[index] = text


def project_gen13(parent: dict, *, subject_commit: str, parent_sha256: str,
                  v6_manifest: str, v6_pack: str,
                  passed: int, skipped: int, failed: int) -> dict:
    """Generation 12 -> the minimum truthful generation 13. Pure; writes nothing."""
    g = json.loads(json.dumps(parent))

    g["state_generation"] = 13
    g["generation_label"] = GEN13_LABEL
    g["subject_state_milestone"] = GEN13_MILESTONE
    g["subject_state_commit"] = subject_commit
    g["parent_snapshot_sha256"] = parent_sha256
    g["control_plane_note"] = (
        "S3X.1 froze a FRESH eval-v6 candidate-blind and measured NOTHING: 0 weight "
        "loads, 0 generations, 0 eval attempts, 0 spends, 0 EVAL authority. eval-v6 is "
        "FROZEN_UNUSED, spent_by null. eval-v5 stays FROZEN_UNUSED, spent_by null, "
        "RETIRED FROM ELIGIBILITY USE. Candidate 004 stays TRAINED_UNEVALUATED.")

    # ── The one mandatory addition: the sixth held-out corpus ────────────────────
    g["datasets"].append({
        "dataset_id": "m62-defensive-eval",
        "evidence": GEN13_EVIDENCE,
        "manifest_hash": v6_manifest,
        "pack_hash": v6_pack,
        "parent_manifest_hash": EVAL_V5_MANIFEST,
        "role": "EVALUATION_HOLDOUT",
        "spent_by": None,
        "status": "FROZEN_UNUSED",
        "task_count": 36,
        "version": "v6",
    })
    g["datasets"].sort(key=lambda d: (d["dataset_id"], d["version"]))

    # ── Invariants: one amendment, one extension, no deletion ────────────────────
    inv = g["frozen_invariants"]
    _replace(inv, 25, "ORCHESTRATOR BODY-BLINDNESS IS A GATE",
             inv[25].rstrip() + " A session that LEGITIMATELY authored a holdout is "
                                "permanently disqualified from evaluating any candidate "
                                "against it.")
    _replace(inv, 26, "EVAL_V5_ELIGIBILITY_USE: RETIRED",
             "EVAL_V5_ELIGIBILITY_USE: RETIRED, prospectively from generation 12. "
             "eval-v5 may never be eligibility evidence for candidate 004 or any later "
             "candidate. It stays FROZEN_UNUSED, spent_by null, because NO MODEL EVER "
             "SAW IT; its preregistered body-blindness precondition failed "
             "pre-authorisation. FRESH_V6_REQUIRED, satisfied at generation 13 by "
             "eval-v6 frozen unspent; the retirement survives it unconditionally.")

    # ── Limitations: four lossless merges, two factual advances ──────────────────
    lim = g["limitations"]
    _replace(lim, 4, "eval-v5's promotion_plan_hash",
             "A holdout's promotion_plan_hash binds output_root_id and is deliberately "
             "NOT part of dataset identity: it differs between roots by design, while "
             "eval-v5's and eval-v6's manifest, pack and three set digests are identical "
             "in every root.")
    _replace(lim, 5, "Candidates 001-004 were each measured",
             lim[5].replace("004 v5 next", "004 eval-v6 next"))
    _replace(lim, 11, "openai is a declared base dependency",
             "The authoritative interpreter lacks two declared dependencies: openai's "
             "absence alone fails 62 tests in three files, and transformers' absence "
             "skips the two S3O tokenizer tests. Environmental, reproduced at pristine "
             "HEAD, never reconciled across interpreters by arithmetic; the training venv "
             "has no pytest and installing either is forbidden.")
    _replace(lim, 12, "The two S3O tokenizer tests skip", "")
    _replace(lim, 19, "D28, D29 and D33 stay OPEN",
             "D28, D29 and D33 stay OPEN and bound every measurement: "
             "tool_call_validity_rate is VACUOUS so the six tool_call_schema tasks decide "
             "nothing; timeout_s is hashed into the generation policy but never enforced, "
             "so timeout_rate is VACUOUS and every S3Q.0 surface quoting the 300 s "
             "ceiling reports timeout_enforced false beside it; D29 bounds refusal "
             "figures in BOTH directions.")
    _replace(lim, 20, "Every S3Q.0 surface quoting", "")
    _replace(lim, 26, "eval-v4 is spent",
             "eval-v4 is spent: no ablation, retry or re-scoring of candidate 003 against "
             "it is possible in any circumstance. The fifth holdout D35 requires WAS "
             "eval-v5; eval-v5 is retired from eligibility use UNSPENT, so D35's "
             "requirement passed to eval-v6, frozen unspent at generation 13.")
    _replace(lim, 28, "The session that authored eval-v5",
             "The session that authored eval-v5 has seen its bodies and is disqualified "
             "from designing candidate 004; the session that authored eval-v6 is likewise "
             "disqualified from evaluating candidate 004 against it. That firewall is "
             "PROCEDURAL, enforced by using a new session; no check here can detect a "
             "breach.")
    _replace(lim, 31, "A successful S3V means",
             "A successful S3V means only that training completed and the adapter is "
             "structurally valid. Train and validation loss are diagnostic, never "
             "eligibility evidence, and candidate 004's eligibility is UNKNOWN until "
             "eval-v6 is spent exactly once. Qualification and a frozen holdout are "
             "readiness, not evidence.")
    g["limitations"] = [x for x in lim if x]

    # ── The prospective contract, rewritten for the milestone that has not happened ──
    g["next_milestone"] = {
        "name": "S3Y qualify and run the candidate 004 evaluation against eval-v6, in "
                "a NEW session that did NOT author v6, under a fresh single-use human "
                "EVAL authority.",
        "requires_new_session": True,
        "primary_axis": "Candidate 004's learning_rate axis against candidate 003, 1e-4 "
                        "-> 5e-5, one dial with alpha not slaved so alpha/r stays 2.0, is "
                        "TRAINED and still UNMEASURED. Eligibility is UNKNOWN and no loss "
                        "is evidence of it.",
        "lora_scope": "ATTENTION_AND_MLP",
        "training_corpus": "m62-defensive-quality-train v2, spent on candidates 003 and "
                           "004, reused UNCHANGED. No train-v3 exists or is proposed; "
                           "S3Y trains nothing.",
        "evaluation_holdout": "eval-v6, FROZEN_UNUSED, spent_by null, frozen "
                              "candidate-blind at generation 13 and the ONLY fresh "
                              "eligibility corpus that exists. eval-v5 is RETIRED from "
                              "eligibility use and eval-v4 is USED_IMMUTABLE.",
        "holdout_access": "Only the evaluation runtime may render an eval-v6 body; D44's "
                          "body-blindness gate applies unchanged to every orchestration "
                          "session. eval-v4 and eval-v5 bodies stay unread permanently.",
        "authority_required": [
            "a fresh single-use human EVAL authority, form EVAL:<plan-hash>, bound to a "
            "plan derived from a post-S3X.1 HEAD and naming eval-v6. None has ever "
            "existed for candidate 004",
            "a fresh single-use human TRAIN authority, form TRAIN:<plan-hash>, before any "
            "further training. The S3V one is SPENT and carries nothing forward",
            "a separate explicit human decision before any promotion, which no evaluation "
            "result implies",
        ],
        "ruled_out": [
            "using eval-v5 as eligibility evidence, creating EVAL authority naming it, "
            "calling it fresh, marking it USED_IMMUTABLE or setting spent_by non-null. "
            "NO MODEL SAW IT: the retirement is prospective and unconditional, and the "
            "lifecycle fact and the eligibility ruling may not be forged into each other",
            "reusing eval-v4 for any candidate, or reading, quoting or reconstructing "
            "eval-v4, eval-v5 or leaked material in any session",
            "the eval-v6 authoring session evaluating candidate 004, or any session "
            "rendering an eval-v6 body outside the evaluation runtime",
            "recording candidate 004 as evaluated, eligible or promoted without a valid "
            "portable receipt re-derived by the production decide_eligibility",
            "relaxing body-blindness or any preregistered gate now that one has failed; "
            "weakening a gate after it fails is post-hoc",
            "any epoch, rank, alpha or dropout change, any dial slaved to the learning "
            "rate, a second axis, ATTENTION_ONLY, or any module-surface change",
            "creating train-v3, adding, deleting or rebalancing train-v2 rows, "
            "strengthening the response schema, or changing gates, graders, thresholds, "
            "the refusal detector, max_new_tokens, the seed or the reasoning policy",
            "reading candidate 004's learning-rate permission as general: the S3U ruling "
            "superseded the generation-8 clause for candidate 004 only, prospectively, "
            "and only to 5e-5",
            "raising a reviewed budget again, or deleting recorded defects, limitations "
            "or invariants to make room",
            "retraining, resuming, re-seeding or patching candidate 003 or 004, calling "
            "either production-ready, or fixing D39 as a rider",
            "promotion, activation, registry mutation, merge, tag, release or version "
            "bump",
        ],
    }

    g["test_baseline"] = dict(g["test_baseline"])
    g["test_baseline"]["milestone"] = GEN13_MILESTONE
    g["test_baseline"]["passed"] = passed
    g["test_baseline"]["skipped"] = skipped
    g["test_baseline"]["failed"] = failed

    lost = check_carried_forward(g)
    if lost:
        raise RuntimeError(
            f"the compaction is NOT lossless: {lost}. A rewrite that supersedes one "
            f"clause may not drop another")
    return g


# ── measurement ──────────────────────────────────────────────────────────────────────
def measure(payload: dict) -> tuple[int, int]:
    size = len(canonical_bytes(payload))
    return size, SNAPSHOT_MAX_BYTES - size


def progress_headroom() -> tuple[int, int, int, int]:
    raw = (REPO_ROOT / "PROGRESS.md").read_bytes()
    lines = raw.count(b"\n")
    return (len(raw), PROGRESS_MAX_BYTES - len(raw),
            lines, PROGRESS_MAX_LINES - lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject-commit", default="0" * 40,
                        help="the commit generation 13 is a claim about")
    parser.add_argument("--v6-manifest", default="0" * 64,
                        help="eval-v6's manifest digest; a stand-in of the right SHAPE "
                             "until the corpus exists, which is what makes this a "
                             "PREFLIGHT rather than a record")
    parser.add_argument("--v6-pack", default="0" * 64, help="eval-v6's task-pack digest")
    parser.add_argument("--passed", type=int, default=0)
    parser.add_argument("--skipped", type=int, default=0)
    parser.add_argument("--failed", type=int, default=0)
    parser.add_argument("--emit", default="",
                        help="write the projected generation 13 to this path")
    parser.add_argument("--parent", default="",
                        help="the generation 12 snapshot to project from. Defaults to "
                             "whatever state/m62/current.json points at; the digest guard "
                             "below is what actually decides, either way")
    args = parser.parse_args(argv)

    if args.parent:
        parent_path = Path(args.parent)
    else:
        current = json.loads(
            (REPO_ROOT / "state/m62/current.json").read_text("utf-8"))
        parent_path = REPO_ROOT / current["latest_snapshot_path"]
    parent_bytes = parent_path.read_bytes()
    parent_sha = sha256_bytes(parent_bytes)
    parent = json.loads(parent_bytes.decode("utf-8"))

    if parent_sha != EXPECTED_PARENT_SHA256:
        print(f"PARENT_SNAPSHOT_MISMATCH: {parent_sha}")
        return 2
    if parent["state_generation"] != EXPECTED_PARENT_GENERATION:
        print(f"PARENT_GENERATION_MISMATCH: {parent['state_generation']}")
        return 2

    gen13 = project_gen13(parent, subject_commit=args.subject_commit,
                          parent_sha256=parent_sha, v6_manifest=args.v6_manifest,
                          v6_pack=args.v6_pack, passed=args.passed,
                          skipped=args.skipped, failed=args.failed)
    size, head = measure(gen13)
    p_bytes, p_head, p_lines, p_line_head = progress_headroom()

    print(f"PARENT_GENERATION: {parent['state_generation']}")
    print(f"PARENT_SNAPSHOT_BYTES: {len(parent_bytes)}")
    print(f"SNAPSHOT_BUDGET_BYTES: {SNAPSHOT_MAX_BYTES}")
    print(f"REQUIRED_HEADROOM_BYTES: {REQUIRED_HEADROOM_BYTES}")
    print(f"PROJECTED_GEN13_SNAPSHOT_BYTES: {size}")
    print(f"PROJECTED_GEN13_HEADROOM_BYTES: {head}")
    print(f"PROJECTED_GEN13_CAPACITY: "
          f"{'PASS' if head >= REQUIRED_HEADROOM_BYTES else 'FAIL'}")
    print(f"COMPACTION_LOSSLESS: {'PASS' if not check_carried_forward(gen13) else 'FAIL'}")
    print(f"CARRIED_FORWARD_CLAUSES: {len(CARRIED_FORWARD)}")
    print(f"PROGRESS_BYTES: {p_bytes} HEADROOM: {p_head}")
    print(f"PROGRESS_LINES: {p_lines} HEADROOM: {p_line_head}")
    print(f"PROGRESS_HEADROOM_INVARIANT: "
          f"{'PASS' if p_head > 0 and p_line_head > 0 else 'FAIL'}")

    ok = head >= REQUIRED_HEADROOM_BYTES and p_head > 0 and p_line_head > 0

    if args.emit and not ok:
        # Fail closed. A snapshot written past its own gate is a state nobody proved room
        # for, and the next generation is the one that pays for it.
        print("EMIT_REFUSED: the capacity gate did not pass")
        return 1
    if args.emit:
        if args.v6_manifest == "0" * 64 or args.v6_pack == "0" * 64:
            print("EMIT_REFUSED: refusing to write a snapshot carrying a stand-in digest")
            return 1
        Path(args.emit).write_bytes(canonical_bytes(gen13))
        print(f"EMITTED: {args.emit}")
        print(f"EMITTED_SHA256: {sha256_bytes(canonical_bytes(gen13))}")

    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
